from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from app.config import Settings
from app.dependencies import get_db, get_settings
from app.schemas import (
    JobCertificatesResponse,
    JobCreate,
    JobCreatedResponse,
    JobDetailResponse,
)
from app.services import job_service

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


@router.post("/", status_code=202, response_model=JobCreatedResponse)
def create_job(
    payload: JobCreate,
    response: Response,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> JobCreatedResponse:
    """Validate, persist and queue a bulk job. Certificates are generated later by the worker."""
    if len(payload.recipients) > settings.max_recipients_per_job:
        raise HTTPException(
            status_code=422,
            detail=(
                f"recipients: at most {settings.max_recipients_per_job} recipients are allowed per job "
                f"(received {len(payload.recipients)})"
            ),
        )
    job = job_service.create_job(db, payload, settings)
    progress = job_service.compute_progress(db, job.id)
    response.headers["Location"] = job_service.job_url(job.id)
    return JobCreatedResponse(
        job_id=job.id,
        status=job.status,
        total_recipients=progress.total_recipients,
        valid_recipients=progress.valid_recipients,
        invalid_recipients=progress.invalid_recipients,
        status_url=job_service.job_url(job.id),
        certificates_url=job_service.certificates_url(job.id),
    )


@router.get("/{job_id}/", response_model=JobDetailResponse)
def get_job(job_id: str, db: Session = Depends(get_db)) -> JobDetailResponse:
    job = job_service.get_job(db, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job_service.get_job_detail(db, job)


@router.get("/{job_id}/certificates/", response_model=JobCertificatesResponse)
def list_job_certificates(job_id: str, db: Session = Depends(get_db)) -> JobCertificatesResponse:
    job = job_service.get_job(db, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job_service.list_job_certificates(db, job)
