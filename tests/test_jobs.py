"""Job creation and retrieval (requirements 1, 10)."""
from __future__ import annotations

from sqlalchemy import func, select

from app.models import Certificate


def test_create_job_returns_202_and_job_id_without_generating(client, payload_factory, session_factory):
    response = client.post("/api/jobs/", json=payload_factory())

    assert response.status_code == 202
    body = response.json()
    assert body["job_id"]
    assert body["status"] == "PENDING"
    assert body["total_recipients"] == 2
    assert body["valid_recipients"] == 2
    assert body["invalid_recipients"] == 0
    assert response.headers["location"] == f"/api/jobs/{body['job_id']}/"
    # Nothing was generated inside the request.
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Certificate)) == 0


def test_job_detail_has_metadata_progress_and_recipients(client, submit):
    job_id = submit()

    body = client.get(f"/api/jobs/{job_id}/").json()

    assert body["job_id"] == job_id
    assert body["event_name"] == "Python Workshop 2026"
    assert body["issuer_name"] == "ABC Organization"
    assert body["issue_date"] == "2026-10-09"
    assert body["status"] == "PENDING"
    assert body["progress"]["pending"] == 2
    assert body["progress"]["progress_percentage"] == 0.0
    assert [r["name"] for r in body["recipients"]] == ["Ananya Rao", "Rahul Kumar"]
    assert all(r["status"] == "PENDING" for r in body["recipients"])
    assert body["certificates_url"] == f"/api/jobs/{job_id}/certificates/"


def test_unknown_job_returns_404(client):
    assert client.get("/api/jobs/does-not-exist/").status_code == 404
    assert client.get("/api/jobs/00000000-0000-0000-0000-000000000000/").status_code == 404
    assert client.get("/api/jobs/00000000-0000-0000-0000-000000000000/certificates/").status_code == 404


def test_each_post_creates_a_new_job(client, payload_factory):
    first = client.post("/api/jobs/", json=payload_factory()).json()["job_id"]
    second = client.post("/api/jobs/", json=payload_factory()).json()["job_id"]
    assert first != second  # documented: no idempotency / request de-duplication across requests


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}
