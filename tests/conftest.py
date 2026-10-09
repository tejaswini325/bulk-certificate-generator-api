"""Shared fixtures. Every test gets its own temp SQLite DB and temp storage directory.

The worker is never started as a thread in tests: tests call ``worker.run_once()`` /
``run_until_idle()`` directly, so processing is deterministic (no sleeps, no races).
"""
from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.services.certificate_service import PdfGenerator
from app.services.pdf_generator import generate_certificate_pdf
from app.workers.job_worker import JobWorker
from templates.certificate_template import CertificateData


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        storage_dir=tmp_path / "storage",
        max_recipients_per_job=10,
        worker_enabled=False,
        worker_batch_size=2,  # small, to exercise batching
        stale_job_seconds=60,
        max_job_attempts=3,
        max_request_bytes=100_000,
    )


@pytest.fixture
def app(settings: Settings):
    return create_app(settings)


@pytest.fixture
def client(app) -> Iterator[TestClient]:
    with TestClient(app) as test_client:  # runs lifespan: creates tables + storage dir
        yield test_client


@pytest.fixture
def session_factory(app, client):  # `client` ensures tables exist
    return app.state.session_factory


@pytest.fixture
def make_worker(session_factory, settings) -> Callable[..., JobWorker]:
    def _make(generator: PdfGenerator = generate_certificate_pdf, worker_id: str = "test-worker") -> JobWorker:
        return JobWorker(session_factory, settings, generator=generator, worker_id=worker_id)

    return _make


@pytest.fixture
def worker(make_worker) -> JobWorker:
    return make_worker()


@pytest.fixture
def settings_factory(settings: Settings) -> Callable[..., Settings]:
    return lambda **changes: replace(settings, **changes)


def make_payload(recipients: list[Any] | None = None, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "event_name": "Python Workshop 2026",
        "issuer_name": "ABC Organization",
        "issue_date": "2026-10-09",
        "recipients": recipients
        if recipients is not None
        else [
            {"name": "Ananya Rao", "email": "ananya@example.com"},
            {"name": "Rahul Kumar", "email": "rahul@example.com"},
        ],
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def payload_factory() -> Callable[..., dict[str, Any]]:
    return make_payload


@pytest.fixture
def submit(client: TestClient) -> Callable[..., str]:
    """POST a job and return its id (asserting HTTP 202)."""

    def _submit(payload: dict[str, Any] | None = None) -> str:
        response = client.post("/api/jobs/", json=payload or make_payload())
        assert response.status_code == 202, response.text
        return response.json()["job_id"]

    return _submit


def failing_generator(*failing_names: str, error: Exception | None = None) -> PdfGenerator:
    """Wraps the real generator but raises for the given recipient names."""

    def _generate(data: CertificateData, destination: Path) -> None:
        if data.recipient_name in failing_names:
            raise error or RuntimeError(f"simulated failure for {data.recipient_name}")
        generate_certificate_pdf(data, destination)

    return _generate
