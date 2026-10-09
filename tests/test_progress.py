"""Status transitions, progress numbers and terminal states (requirements 6, 7, 14, 15)."""
from __future__ import annotations

import pytest

from app.models import JobStatus
from app.services.job_service import derive_terminal_status
from app.services.pdf_generator import generate_certificate_pdf
from tests.conftest import failing_generator


def test_job_status_changes_as_processing_proceeds(client, submit, make_worker, payload_factory):
    recipients = [{"name": f"Person {i}", "email": f"p{i}@example.com"} for i in range(4)]
    job_id = submit(payload_factory(recipients))
    snapshots: list[dict] = []

    def observing_generator(data, destination):
        # Called mid-job: capture exactly what a client polling right now would see.
        snapshots.append(client.get(f"/api/jobs/{job_id}/").json())
        generate_certificate_pdf(data, destination)

    assert client.get(f"/api/jobs/{job_id}/").json()["status"] == "PENDING"
    make_worker(observing_generator).run_until_idle()

    assert [s["status"] for s in snapshots] == ["PROCESSING"] * 4
    assert [s["progress"]["successful"] for s in snapshots] == [0, 1, 2, 3]
    assert [s["progress"]["progress_percentage"] for s in snapshots] == [0.0, 25.0, 50.0, 75.0]
    # The recipient being worked on is PROCESSING while the PDF is being drawn.
    assert [s["progress"]["processing"] for s in snapshots] == [1, 1, 1, 1]
    assert [s["progress"]["pending"] for s in snapshots] == [3, 2, 1, 0]
    assert all(s["completed_at"] is None for s in snapshots)

    final = client.get(f"/api/jobs/{job_id}/").json()
    assert final["status"] == "COMPLETED"
    assert final["progress"]["progress_percentage"] == 100.0
    assert final["completed_at"] is not None and final["started_at"] is not None


def test_progress_counts_and_percentages_for_a_mixed_job(client, submit, make_worker, payload_factory):
    recipients = [
        {"name": "Good A", "email": "a@example.com"},
        {"name": "Good B", "email": "b@example.com"},
        {"name": "Will Fail", "email": "c@example.com"},
        {"name": "", "email": "invalid@example.com"},
        {"name": "Good C", "email": "d@example.com"},
    ]
    job_id = submit(payload_factory(recipients))

    before = client.get(f"/api/jobs/{job_id}/").json()["progress"]
    assert before == {
        "total_recipients": 5, "valid_recipients": 4, "invalid_recipients": 1,
        "successful": 0, "failed": 0, "pending": 4, "processing": 0,
        "progress_percentage": 20.0,  # the invalid recipient is already terminal
    }

    make_worker(failing_generator("Will Fail")).run_until_idle()

    after = client.get(f"/api/jobs/{job_id}/").json()["progress"]
    assert after == {
        "total_recipients": 5, "valid_recipients": 4, "invalid_recipients": 1,
        "successful": 3, "failed": 1, "pending": 0, "processing": 0,
        "progress_percentage": 100.0,
    }


def test_progress_numbers_always_add_up(client, submit, make_worker, payload_factory):
    recipients = [{"name": f"P{i}", "email": f"p{i}@example.com"} for i in range(7)]
    recipients.append({"name": "", "email": "bad"})
    job_id = submit(payload_factory(recipients))
    seen: list[dict] = []

    def generator(data, destination):
        seen.append(client.get(f"/api/jobs/{job_id}/").json()["progress"])
        generate_certificate_pdf(data, destination)

    make_worker(generator).run_until_idle()
    seen.append(client.get(f"/api/jobs/{job_id}/").json()["progress"])
    for p in seen:
        assert p["successful"] + p["failed"] + p["pending"] + p["processing"] + p["invalid_recipients"] == p["total_recipients"]
        assert p["valid_recipients"] + p["invalid_recipients"] == p["total_recipients"]


def test_mixed_success_and_failure_is_completed_with_errors(client, submit, make_worker, payload_factory):
    job_id = submit(payload_factory([{"name": "OK", "email": "ok@example.com"}, {"name": "Boom", "email": "boom@example.com"}]))
    make_worker(failing_generator("Boom")).run_until_idle()
    assert client.get(f"/api/jobs/{job_id}/").json()["status"] == "COMPLETED_WITH_ERRORS"


def test_all_failed_job_is_failed(client, submit, make_worker, payload_factory):
    job_id = submit(payload_factory([{"name": "Boom", "email": "boom@example.com"}]))
    make_worker(failing_generator("Boom")).run_until_idle()
    body = client.get(f"/api/jobs/{job_id}/").json()
    assert body["status"] == "FAILED"
    assert body["progress"]["progress_percentage"] == 100.0
    assert body["completed_at"] is not None


def test_job_with_no_valid_recipients_reaches_a_terminal_state(client, submit, worker, payload_factory):
    job_id = submit(payload_factory([{"name": "", "email": "x"}, {"name": "Y", "email": "bad"}]))

    body = client.get(f"/api/jobs/{job_id}/").json()
    assert body["status"] == "FAILED"
    assert body["error_message"] == "No valid recipients in request"
    assert body["progress"]["progress_percentage"] == 100.0
    assert body["progress"]["valid_recipients"] == 0
    assert body["completed_at"] is not None
    assert worker.run_until_idle() == 0  # there is nothing for a worker to pick up


def test_job_is_not_completed_before_all_recipients_are_terminal(client, submit, session_factory, worker):
    from app.services import job_service

    job_id = submit()
    with session_factory() as session:
        job_service.claim_next_job(session, "w")
        # Nothing processed yet: finalisation must refuse.
        assert job_service.finalize_job(session, job_id) is None
    assert client.get(f"/api/jobs/{job_id}/").json()["status"] == "PROCESSING"


@pytest.mark.parametrize(
    "completed,failed,invalid,expected",
    [
        (2, 0, 0, JobStatus.COMPLETED),
        (2, 1, 0, JobStatus.COMPLETED_WITH_ERRORS),
        (2, 0, 1, JobStatus.COMPLETED_WITH_ERRORS),
        (0, 2, 0, JobStatus.FAILED),
        (0, 0, 3, JobStatus.FAILED),
        (0, 2, 1, JobStatus.FAILED),
    ],
)
def test_terminal_status_rule(completed, failed, invalid, expected):
    assert derive_terminal_status(completed, failed, invalid) is expected


def test_multiple_jobs_are_processed_in_creation_order(client, submit, worker, payload_factory):
    first, second = submit(), submit()
    assert worker.run_until_idle() == 2
    for job_id in (first, second):
        assert client.get(f"/api/jobs/{job_id}/").json()["status"] == "COMPLETED"
