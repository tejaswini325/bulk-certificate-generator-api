"""Pydantic request/response schemas."""
from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Any

from pydantic import BaseModel, Field, field_validator

from app.models import JobStatus, RecipientStatus
from app.validation import check_text

# Fixed limit for event_name / issuer_name; the per-job recipient maximum is configurable and enforced in the route.
MAX_TITLE_LENGTH = 120


class JobCreate(BaseModel):
    """Whole-request structure. A failure here rejects the request with HTTP 422.

    ``recipients`` items are intentionally untyped (``Any``): each one is validated
    individually by the service so one bad recipient never rejects the whole batch.
    """

    event_name: str
    issuer_name: str
    issue_date: date
    recipients: Annotated[list[Any], Field(min_length=1)]

    @field_validator("event_name", "issuer_name")
    @classmethod
    def _validate_title(cls, value: str, info: Any) -> str:
        cleaned, errors = check_text(value, info.field_name, MAX_TITLE_LENGTH)
        if errors:
            raise ValueError("; ".join(errors))
        assert cleaned is not None
        return cleaned


class JobCreatedResponse(BaseModel):
    job_id: str
    status: JobStatus
    total_recipients: int
    valid_recipients: int
    invalid_recipients: int
    status_url: str
    certificates_url: str


class ProgressOut(BaseModel):
    total_recipients: int
    valid_recipients: int
    invalid_recipients: int
    successful: int
    failed: int
    pending: int
    processing: int
    progress_percentage: float


class RecipientOut(BaseModel):
    position: int
    name: str
    email: str
    status: RecipientStatus
    validation_errors: list[str] = []
    error_message: str | None = None
    certificate_id: str | None = None
    download_url: str | None = None


class JobDetailResponse(BaseModel):
    job_id: str
    event_name: str
    issuer_name: str
    issue_date: date
    status: JobStatus
    error_message: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    progress: ProgressOut
    recipients: list[RecipientOut]
    certificates_url: str


class CertificateOut(BaseModel):
    certificate_id: str
    recipient_name: str
    download_url: str
    created_at: datetime


class JobCertificatesResponse(BaseModel):
    job_id: str
    count: int
    certificates: list[CertificateOut]
