"""Failure isolation, error details, recovery and worker exclusivity (requirements 8, 9 + engineering notes)."""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from sqlalchemy import select, update

from app.models import Certificate, GenerationJob, JobStatus, Recipient, RecipientStatus, utcnow
from app.services import job_service
from app.services.pdf_generator import CertificateGenerationError
from tests.conftest import failing_generator


def test_one_failed_generation_does_not_stop_other_recipients(client, submit, make_worker, payload_factory, settings):
    names = ["Alpha", "Bravo", "Charlie", "Delta", "Echo"]
    job_id = submit(payload_factory([{"name": n, "email": f"{n.lower()}@example.com"} for n in names]))

    make_worker(failing_generator("Charlie")).run_until_idle()

    body = client.get(f"/api/jobs/{job_id}/").json()
    by_name = {r["name"]: r for r in body["recipients"]}
    assert by_name["Charlie"]["status"] == "FAILED"
    assert by_name["Charlie"]["certificate_id"] is None
    for name in ("Alpha", "Bravo", "Delta", "Echo"):  # recipients after the failure still succeed
        assert by_name[name]["status"] == "COMPLETED"
        assert by_name[name]["certificate_id"]
    assert body["status"] == "COMPLETED_WITH_ERRORS"
    assert len(list(settings.storage_dir.rglob("*.pdf"))) == 4
    assert not list(settings.storage_dir.rglob("*.tmp"))  # partial files are cleaned up


def test_failed_recipient_has_a_useful_but_safe_error_message(client, submit, make_worker, payload_factory):
    secret_path = "/srv/secret/location.pdf"
    job_id = submit(payload_factory([{"name": "Boom", "email": "b@example.com"}]))

    make_worker(failing_generator("Boom", error=RuntimeError(f"disk exploded at {secret_path}"))).run_until_idle()

    recipient = client.get(f"/api/jobs/{job_id}/").json()["recipients"][0]
    assert recipient["status"] == "FAILED"
    assert "RuntimeError" in recipient["error_message"]
    assert secret_path not in recipient["error_message"]  # internal details stay in the log


def test_known_generation_errors_expose_their_safe_message(client, submit, make_worker, payload_factory):
    job_id = submit(payload_factory([{"name": "Boom", "email": "b@example.com"}]))
    error = CertificateGenerationError("Could not write the certificate file")

    make_worker(failing_generator("Boom", error=error)).run_until_idle()

    recipient = client.get(f"/api/jobs/{job_id}/").json()["recipients"][0]
    assert recipient["error_message"] == "Could not write the certificate file"


def test_failure_is_logged_with_traceback(client, submit, make_worker, payload_factory, caplog):
    submit(payload_factory([{"name": "Boom", "email": "b@example.com"}]))
    with caplog.at_level("ERROR"):
        make_worker(failing_generator("Boom")).run_until_idle()
    assert any("certificate generation failed" in r.message and r.exc_info for r in caplog.records)


def test_generator_that_produces_no_file_is_a_failure_not_a_crash(client, submit, make_worker):
    job_id = submit()
    make_worker(lambda data, destination: None).run_until_idle()  # writes nothing

    body = client.get(f"/api/jobs/{job_id}/").json()
    assert body["status"] == "FAILED"
    assert all(r["status"] == "FAILED" and "no output" in r["error_message"] for r in body["recipients"])


def test_no_recipient_is_left_processing_after_a_job(client, submit, make_worker, session_factory, payload_factory):
    job_id = submit(payload_factory([{"name": "Boom", "email": "b@example.com"}, {"name": "Fine", "email": "f@example.com"}]))
    make_worker(failing_generator("Boom")).run_until_idle()
    with session_factory() as session:
        stuck = session.scalars(
            select(Recipient).where(Recipient.job_id == job_id, Recipient.status.in_(["PENDING", "PROCESSING"]))
        ).all()
    assert stuck == []


def test_unexpected_job_level_error_fails_the_job_consistently(client, submit, worker, monkeypatch):
    job_id = submit()

    def explode(*args, **kwargs):
        raise RuntimeError("database went away")

    monkeypatch.setattr(job_service, "next_pending_recipient_ids", explode)
    worker.run_until_idle()

    body = client.get(f"/api/jobs/{job_id}/").json()
    assert body["status"] == "FAILED"
    assert "RuntimeError" in body["error_message"]
    assert all(r["status"] == "FAILED" for r in body["recipients"])
    assert body["progress"]["progress_percentage"] == 100.0


# ---------------------------------------------------------------- restart / recovery

def _simulate_crashed_worker(session_factory, job_id: str, heartbeat_age: timedelta, attempts: int = 1) -> None:
    """Leave the DB exactly as a worker killed mid-job would: job PROCESSING, one recipient PROCESSING."""
    with session_factory() as session:
        session.execute(
            update(GenerationJob)
            .where(GenerationJob.id == job_id)
            .values(status=JobStatus.PROCESSING, locked_by="dead-worker", attempts=attempts,
                    heartbeat_at=utcnow() - heartbeat_age)
        )
        first = session.scalars(select(Recipient).where(Recipient.job_id == job_id).order_by(Recipient.position)).first()
        first.status = RecipientStatus.PROCESSING
        session.commit()


def test_stale_job_is_recovered_and_finished(client, submit, worker, session_factory):
    job_id = submit()
    _simulate_crashed_worker(session_factory, job_id, heartbeat_age=timedelta(hours=1))

    worker.run_until_idle()

    body = client.get(f"/api/jobs/{job_id}/").json()
    assert body["status"] == "COMPLETED"
    assert [r["status"] for r in body["recipients"]] == ["COMPLETED", "COMPLETED"]


def test_recovery_keeps_already_finished_work(client, submit, make_worker, session_factory, payload_factory, settings):
    job_id = submit(payload_factory([{"name": f"P{i}", "email": f"p{i}@example.com"} for i in range(3)]))
    first_worker = make_worker()
    with session_factory() as session:
        job_service.claim_next_job(session, "w1")
    first_worker._process_recipient(job_id, _recipient_id(session_factory, job_id, 0))  # one done, then "crash"
    with session_factory() as session:
        session.execute(update(GenerationJob).where(GenerationJob.id == job_id).values(heartbeat_at=utcnow() - timedelta(hours=1)))
        session.commit()
    files_before = {p.name: p.stat().st_mtime_ns for p in settings.storage_dir.rglob("*.pdf")}
    assert len(files_before) == 1

    make_worker().run_until_idle()

    body = client.get(f"/api/jobs/{job_id}/").json()
    assert body["status"] == "COMPLETED"
    assert {p.name: p.stat().st_mtime_ns for p in settings.storage_dir.rglob("*.pdf")}.items() >= files_before.items()
    assert len(list(settings.storage_dir.rglob("*.pdf"))) == 3  # no duplicate certificate for P0


def test_job_with_fresh_heartbeat_is_not_stolen(client, submit, worker, session_factory):
    job_id = submit()
    _simulate_crashed_worker(session_factory, job_id, heartbeat_age=timedelta(seconds=1))

    assert worker.run_until_idle() == 0  # looks alive, so it is left alone

    assert client.get(f"/api/jobs/{job_id}/").json()["status"] == "PROCESSING"


def test_job_exceeding_max_attempts_is_failed_instead_of_looping(client, submit, worker, session_factory, settings):
    job_id = submit()
    _simulate_crashed_worker(session_factory, job_id, heartbeat_age=timedelta(hours=1), attempts=settings.max_job_attempts)

    worker.run_until_idle()

    body = client.get(f"/api/jobs/{job_id}/").json()
    assert body["status"] == "FAILED"
    assert "abandoned" in body["error_message"]
    assert all(r["status"] == "FAILED" for r in body["recipients"])


def test_a_job_cannot_be_claimed_by_two_workers(client, submit, session_factory):
    job_id = submit()
    with session_factory() as first, session_factory() as second:
        assert job_service.claim_next_job(first, "worker-1") == job_id
        assert job_service.claim_next_job(second, "worker-2") is None
    with session_factory() as session:
        job = session.get(GenerationJob, job_id)
        assert job.status == JobStatus.PROCESSING and job.locked_by == "worker-1" and job.attempts == 1


def test_recipient_can_only_be_claimed_once(client, submit, session_factory):
    job_id = submit()
    recipient_id = _recipient_id(session_factory, job_id, 0)
    with session_factory() as a, session_factory() as b:
        assert job_service.claim_recipient(a, job_id, recipient_id) is True
        assert job_service.claim_recipient(b, job_id, recipient_id) is False


def test_graceful_stop_returns_job_to_pending(client, submit, make_worker, session_factory):
    job_id = submit()
    worker = make_worker()
    worker._stop.set()  # as if shutdown was requested right after the job was claimed

    worker.run_once()

    assert client.get(f"/api/jobs/{job_id}/").json()["status"] == "PENDING"
    worker._stop.clear()
    worker.run_until_idle()
    assert client.get(f"/api/jobs/{job_id}/").json()["status"] == "COMPLETED"


def test_failed_recipient_does_not_overwrite_a_completed_one(client, submit, worker, session_factory):
    job_id = submit()
    worker.run_until_idle()
    recipient_id = _recipient_id(session_factory, job_id, 0)
    with session_factory() as session:
        job_service.mark_recipient_failed(session, recipient_id, "late failure")  # guarded: status isn't PROCESSING
        assert session.get(Recipient, recipient_id).status == RecipientStatus.COMPLETED
        assert session.scalar(select(Certificate).where(Certificate.recipient_id == recipient_id)) is not None


def _recipient_id(session_factory, job_id: str, position: int) -> int:
    with session_factory() as session:
        return session.scalars(select(Recipient.id).where(Recipient.job_id == job_id, Recipient.position == position)).one()
