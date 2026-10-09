"""Request and recipient validation (requirements 2, 3, 4, 9)."""
from __future__ import annotations

import pytest

from app.models import JobStatus
from app.validation import validate_recipient


@pytest.mark.parametrize(
    "mutation",
    [
        {"recipients": None},                      # wrong type
        {"recipients": "not-a-list"},
        {"recipients": {"name": "x"}},
        {"recipients": []},
        {"event_name": ""},
        {"event_name": "   "},
        {"event_name": 123},
        {"issuer_name": ""},
        {"issue_date": "not-a-date"},
        {"issue_date": "2026-13-45"},
        {"event_name": "x" * 500},
        {"event_name": "bad\x00name"},
    ],
)
def test_invalid_overall_structure_is_rejected_with_422(client, payload_factory, mutation):
    payload = payload_factory()
    payload.update(mutation)
    response = client.post("/api/jobs/", json=payload)
    assert response.status_code == 422


@pytest.mark.parametrize("missing", ["event_name", "issuer_name", "issue_date", "recipients"])
def test_missing_required_field_is_rejected(client, payload_factory, missing):
    payload = payload_factory()
    del payload[missing]
    assert client.post("/api/jobs/", json=payload).status_code == 422


def test_non_json_and_non_object_bodies_are_rejected(client):
    assert client.post("/api/jobs/", content="not json", headers={"content-type": "application/json"}).status_code == 422
    assert client.post("/api/jobs/", json=["a", "list"]).status_code == 422
    assert client.post("/api/jobs/").status_code == 422


def test_valid_recipients_are_processed_even_when_others_are_invalid(client, submit, worker, payload_factory):
    recipients = [
        {"name": "Valid One", "email": "one@example.com"},
        {"name": "", "email": "blank-name@example.com"},
        {"name": "Bad Email", "email": "not-an-email"},
        {"name": "Valid Two", "email": "two@example.com"},
        "just a string",
        {"name": "No Email"},
    ]
    job_id = submit(payload_factory(recipients))
    worker.run_until_idle()

    body = client.get(f"/api/jobs/{job_id}/").json()
    statuses = {r["position"]: r["status"] for r in body["recipients"]}
    assert statuses == {0: "COMPLETED", 1: "INVALID", 2: "INVALID", 3: "COMPLETED", 4: "INVALID", 5: "INVALID"}
    assert body["progress"]["valid_recipients"] == 2
    assert body["progress"]["invalid_recipients"] == 4
    assert body["status"] == JobStatus.COMPLETED_WITH_ERRORS.value


def test_invalid_recipients_have_useful_errors(client, submit, payload_factory):
    job_id = submit(
        payload_factory(
            [
                {"name": "", "email": "nope"},
                {"name": 42, "email": None},
                ["a", "b"],
            ]
        )
    )
    recipients = client.get(f"/api/jobs/{job_id}/").json()["recipients"]

    both = recipients[0]["validation_errors"]
    assert any("name must not be empty" in e for e in both)
    assert any("email is not valid" in e for e in both)
    wrong_types = recipients[1]["validation_errors"]
    assert "name must be a string" in wrong_types and "email must be a string" in wrong_types
    assert "recipient must be an object" in recipients[2]["validation_errors"][0]


@pytest.mark.parametrize(
    "name,expected_ok",
    [
        ("Ananya Rao", True),
        ("  Rahul   Kumar  ", True),          # whitespace collapsed
        ("José Müller", True),                 # Latin-1 / cp1252 characters render fine
        ("x" * 100, True),
        ("x" * 101, False),                    # over MAX_NAME_LENGTH (100)
        ("", False),
        ("   ", False),
        ("Bad\x07Bell", False),               # control character
        ("Zero\u200bWidth", False),           # invisible character
        ("अनन्या", False),                     # cannot be rendered by the built-in fonts
    ],
)
def test_name_validation(name, expected_ok):
    result = validate_recipient({"name": name, "email": "a@example.com"}, max_name_length=100)
    assert result.is_valid is expected_ok
    if expected_ok:
        assert result.name == " ".join(name.split())


@pytest.mark.parametrize(
    "email,expected_ok",
    [
        ("user@example.com", True),
        ("User.Name+tag@Example.COM", True),
        ("  spaced@example.com  ", True),
        ("plainaddress", False),
        ("@example.com", False),
        ("user@", False),
        ("user@@example.com", False),
        ("user@exa mple.com", False),
        ("", False),
        ("a" * 250 + "@example.com", False),
    ],
)
def test_email_validation(email, expected_ok):
    assert validate_recipient({"name": "Valid", "email": email}, 100).is_valid is expected_ok


def test_email_is_normalised(client, submit, payload_factory):
    job_id = submit(payload_factory([{"name": "A", "email": "  Someone@EXAMPLE.com "}]))
    assert client.get(f"/api/jobs/{job_id}/").json()["recipients"][0]["email"] == "Someone@example.com"


def test_recipient_limit_is_enforced(client, payload_factory, settings):
    ok = [{"name": f"P{i}", "email": f"p{i}@example.com"} for i in range(settings.max_recipients_per_job)]
    assert client.post("/api/jobs/", json=payload_factory(ok)).status_code == 202

    too_many = ok + [{"name": "Extra", "email": "extra@example.com"}]
    response = client.post("/api/jobs/", json=payload_factory(too_many))
    assert response.status_code == 422
    assert str(settings.max_recipients_per_job) in response.json()["detail"]


def test_duplicate_emails_within_a_request_are_marked_invalid(client, submit, payload_factory):
    job_id = submit(
        payload_factory(
            [
                {"name": "First", "email": "same@example.com"},
                {"name": "Second", "email": "SAME@example.com"},
                {"name": "Same Name Different Email", "email": "other@example.com"},
                {"name": "First", "email": "different@example.com"},  # duplicate *name* is allowed
            ]
        )
    )
    recipients = client.get(f"/api/jobs/{job_id}/").json()["recipients"]
    assert [r["status"] for r in recipients] == ["PENDING", "INVALID", "PENDING", "PENDING"]
    assert "duplicate email" in recipients[1]["validation_errors"][0]


def test_oversized_request_body_is_rejected_with_413(client, payload_factory):
    huge = payload_factory([{"name": "x" * 200_000, "email": "a@example.com"}])
    assert client.post("/api/jobs/", json=huge).status_code == 413


def test_huge_invalid_values_are_truncated_before_storage(client, submit, payload_factory):
    job_id = submit(payload_factory([{"name": "n" * 5000, "email": "e" * 5000}]))
    recipient = client.get(f"/api/jobs/{job_id}/").json()["recipients"][0]
    assert recipient["status"] == "INVALID"
    assert len(recipient["name"]) <= 255 and len(recipient["email"]) <= 320


def test_all_invalid_job_is_immediately_failed(client, payload_factory):
    response = client.post("/api/jobs/", json=payload_factory([{"name": "", "email": "x"}]))
    assert response.status_code == 202
    assert response.json()["status"] == "FAILED"
