"""Job lifecycle: creation, progress, claiming, recovery, finalisation."""
from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session, selectinload

from app.config import Settings
from app.models import (
    Certificate,
    GenerationJob,
    JobStatus,
    Recipient,
    RecipientStatus,
    utcnow,
)
from app.schemas import (
    CertificateOut,
    JobCertificatesResponse,
    JobCreate,
    JobDetailResponse,
    ProgressOut,
    RecipientOut,
)
from app.services.certificate_service import new_certificate_id
from app.validation import validate_recipient

logger = logging.getLogger(__name__)

_NO_SYNC = {"synchronize_session": False}


def download_url(certificate_id: str) -> str:
    return f"/api/certificates/{certificate_id}/download"


def job_url(job_id: str) -> str:
    return f"/api/jobs/{job_id}/"


def certificates_url(job_id: str) -> str:
    return f"/api/jobs/{job_id}/certificates/"


# ---------------------------------------------------------------- terminal status rule

def derive_terminal_status(completed: int, failed: int, invalid: int) -> JobStatus:
    """Final status once every recipient is terminal.

    * no certificate generated            -> FAILED
    * every submitted recipient succeeded -> COMPLETED
    * some succeeded, some failed/invalid -> COMPLETED_WITH_ERRORS
    """
    if completed == 0:
        return JobStatus.FAILED
    if failed == 0 and invalid == 0:
        return JobStatus.COMPLETED
    return JobStatus.COMPLETED_WITH_ERRORS


# ---------------------------------------------------------------- creation

def create_job(session: Session, payload: JobCreate, settings: Settings) -> GenerationJob:
    """Validate recipients one by one and persist job + recipients in a single transaction."""
    job = GenerationJob(
        event_name=payload.event_name,
        issuer_name=payload.issuer_name,
        issue_date=payload.issue_date,
        status=JobStatus.PENDING,
        total_recipients=len(payload.recipients),
    )
    seen_emails: dict[str, int] = {}
    valid_count = 0
    for position, raw in enumerate(payload.recipients):
        result = validate_recipient(raw, settings.max_name_length)
        errors = list(result.errors)
        if result.is_valid:
            assert result.email is not None
            key = result.email.lower()
            if key in seen_emails:
                errors.append(f"duplicate email: already used by recipient at position {seen_emails[key]}")
            else:
                seen_emails[key] = position
        if errors:
            job.recipients.append(
                Recipient(
                    position=position,
                    name=result.display_name,
                    email=result.display_email,
                    status=RecipientStatus.INVALID,
                    validation_errors=errors,
                )
            )
        else:
            valid_count += 1
            job.recipients.append(
                Recipient(
                    position=position,
                    name=result.display_name,
                    email=result.display_email,
                    status=RecipientStatus.PENDING,
                    reserved_certificate_id=new_certificate_id(),
                )
            )

    if valid_count == 0:
        # Nothing to process: reach a terminal state immediately instead of waking a worker.
        job.status = JobStatus.FAILED
        job.error_message = "No valid recipients in request"
        job.completed_at = utcnow()

    session.add(job)
    session.commit()
    logger.info(
        "Job %s created: total=%d valid=%d invalid=%d status=%s",
        job.id, len(payload.recipients), valid_count, len(payload.recipients) - valid_count, job.status.value,
    )
    return job


# ---------------------------------------------------------------- progress / queries

def status_counts(session: Session, job_id: str) -> dict[RecipientStatus, int]:
    rows = session.execute(
        select(Recipient.status, func.count()).where(Recipient.job_id == job_id).group_by(Recipient.status)
    ).all()
    counts = {status: 0 for status in RecipientStatus}
    for status, count in rows:
        counts[status] = count
    return counts


def compute_progress(session: Session, job_id: str) -> ProgressOut:
    """Always derived from the persisted recipient rows."""
    counts = status_counts(session, job_id)
    total = sum(counts.values())
    terminal = counts[RecipientStatus.COMPLETED] + counts[RecipientStatus.FAILED] + counts[RecipientStatus.INVALID]
    percentage = round(terminal / total * 100, 2) if total else 100.0
    return ProgressOut(
        total_recipients=total,
        valid_recipients=total - counts[RecipientStatus.INVALID],
        invalid_recipients=counts[RecipientStatus.INVALID],
        successful=counts[RecipientStatus.COMPLETED],
        failed=counts[RecipientStatus.FAILED],
        pending=counts[RecipientStatus.PENDING],
        processing=counts[RecipientStatus.PROCESSING],
        progress_percentage=percentage,
    )


def get_job(session: Session, job_id: str) -> GenerationJob | None:
    return session.get(GenerationJob, job_id)


def get_job_detail(session: Session, job: GenerationJob) -> JobDetailResponse:
    recipients = session.scalars(
        select(Recipient)
        .where(Recipient.job_id == job.id)
        .options(selectinload(Recipient.certificate))
        .order_by(Recipient.position)
    ).all()
    items = [
        RecipientOut(
            position=r.position,
            name=r.name,
            email=r.email,
            status=r.status,
            validation_errors=list(r.validation_errors or []),
            error_message=r.error_message,
            certificate_id=r.certificate.certificate_id if r.certificate else None,
            download_url=download_url(r.certificate.certificate_id) if r.certificate else None,
        )
        for r in recipients
    ]
    return JobDetailResponse(
        job_id=job.id,
        event_name=job.event_name,
        issuer_name=job.issuer_name,
        issue_date=job.issue_date,
        status=job.status,
        error_message=job.error_message,
        created_at=job.created_at,
        started_at=job.started_at,
        completed_at=job.completed_at,
        progress=compute_progress(session, job.id),
        recipients=items,
        certificates_url=certificates_url(job.id),
    )


def list_job_certificates(session: Session, job: GenerationJob) -> JobCertificatesResponse:
    rows = session.execute(
        select(Certificate, Recipient.name)
        .join(Recipient, Recipient.id == Certificate.recipient_id)
        .where(Certificate.job_id == job.id)
        .order_by(Recipient.position)
    ).all()
    certs = [
        CertificateOut(
            certificate_id=cert.certificate_id,
            recipient_name=name,
            download_url=download_url(cert.certificate_id),
            created_at=cert.created_at,
        )
        for cert, name in rows
    ]
    return JobCertificatesResponse(job_id=job.id, count=len(certs), certificates=certs)


# ---------------------------------------------------------------- worker-side operations

def claim_next_job(session: Session, worker_id: str) -> str | None:
    """Atomically move the oldest PENDING job to PROCESSING.

    The conditional UPDATE (``WHERE status = 'PENDING'``) is the lock: only one caller can
    change the row, so two workers can never process the same job.
    """
    candidates = session.scalars(
        select(GenerationJob.id)
        .where(GenerationJob.status == JobStatus.PENDING)
        .order_by(GenerationJob.created_at, GenerationJob.id)
        .limit(5)
    ).all()
    now = utcnow()
    for job_id in candidates:
        result = session.execute(
            update(GenerationJob)
            .where(GenerationJob.id == job_id, GenerationJob.status == JobStatus.PENDING)
            .values(
                status=JobStatus.PROCESSING,
                locked_by=worker_id,
                attempts=GenerationJob.attempts + 1,
                started_at=func.coalesce(GenerationJob.started_at, now),
                heartbeat_at=now,
                updated_at=now,
            )
            .execution_options(**_NO_SYNC)
        )
        session.commit()
        if result.rowcount == 1:  # type: ignore[attr-defined]
            logger.info("Job %s claimed by %s", job_id, worker_id)
            return job_id
    return None


def next_pending_recipient_ids(session: Session, job_id: str, limit: int) -> list[int]:
    """A bounded batch of recipient ids, so memory use does not grow with job size."""
    return list(
        session.scalars(
            select(Recipient.id)
            .where(Recipient.job_id == job_id, Recipient.status == RecipientStatus.PENDING)
            .order_by(Recipient.position)
            .limit(limit)
        )
    )


def claim_recipient(session: Session, job_id: str, recipient_id: int) -> bool:
    """PENDING -> PROCESSING (committed immediately) and refresh the job heartbeat."""
    now = utcnow()
    result = session.execute(
        update(Recipient)
        .where(Recipient.id == recipient_id, Recipient.status == RecipientStatus.PENDING)
        .values(status=RecipientStatus.PROCESSING, attempts=Recipient.attempts + 1, updated_at=now)
        .execution_options(**_NO_SYNC)
    )
    session.execute(
        update(GenerationJob)
        .where(GenerationJob.id == job_id)
        .values(heartbeat_at=now, updated_at=now)
        .execution_options(**_NO_SYNC)
    )
    session.commit()
    return result.rowcount == 1  # type: ignore[attr-defined]


def mark_recipient_completed(session: Session, recipient: Recipient, file_path: str, file_size: int) -> None:
    """Certificate row + status change; the caller commits both atomically."""
    if recipient.status != RecipientStatus.PROCESSING or recipient.reserved_certificate_id is None:
        raise RuntimeError("recipient is not in a completable state")
    session.add(
        Certificate(
            certificate_id=recipient.reserved_certificate_id,
            job_id=recipient.job_id,
            recipient_id=recipient.id,
            file_path=file_path,
            file_size=file_size,
        )
    )
    recipient.status = RecipientStatus.COMPLETED
    recipient.error_message = None


def mark_recipient_failed(session: Session, recipient_id: int, message: str) -> None:
    """PROCESSING -> FAILED (guarded, so it can never overwrite a COMPLETED recipient)."""
    session.execute(
        update(Recipient)
        .where(Recipient.id == recipient_id, Recipient.status == RecipientStatus.PROCESSING)
        .values(status=RecipientStatus.FAILED, error_message=message[:1000], updated_at=utcnow())
        .execution_options(**_NO_SYNC)
    )
    session.commit()


def finalize_job(session: Session, job_id: str) -> JobStatus | None:
    """Set the terminal status, but only when every recipient is terminal."""
    counts = status_counts(session, job_id)
    if counts[RecipientStatus.PENDING] or counts[RecipientStatus.PROCESSING]:
        return None
    final = derive_terminal_status(
        counts[RecipientStatus.COMPLETED], counts[RecipientStatus.FAILED], counts[RecipientStatus.INVALID]
    )
    now = utcnow()
    values: dict[str, object] = {"status": final, "completed_at": now, "updated_at": now, "locked_by": None}
    if final is JobStatus.FAILED:
        values["error_message"] = "No certificates were generated"
    result = session.execute(
        update(GenerationJob)
        .where(GenerationJob.id == job_id, GenerationJob.status == JobStatus.PROCESSING)
        .values(**values)
        .execution_options(**_NO_SYNC)
    )
    session.commit()
    if result.rowcount == 1:  # type: ignore[attr-defined]
        logger.info("Job %s finished with status %s (%s)", job_id, final.value, {k.value: v for k, v in counts.items()})
        return final
    return None


def release_job(session: Session, job_id: str) -> None:
    """Return a PROCESSING job to PENDING (graceful shutdown). Finished work is kept."""
    session.execute(
        update(GenerationJob)
        .where(GenerationJob.id == job_id, GenerationJob.status == JobStatus.PROCESSING)
        .values(status=JobStatus.PENDING, locked_by=None, updated_at=utcnow())
        .execution_options(**_NO_SYNC)
    )
    session.commit()
    logger.info("Job %s released back to PENDING", job_id)


def abort_job(session: Session, job_id: str, reason: str) -> None:
    """Fail the job and every non-terminal recipient so state stays consistent."""
    session.rollback()
    now = utcnow()
    session.execute(
        update(Recipient)
        .where(
            Recipient.job_id == job_id,
            Recipient.status.in_([RecipientStatus.PENDING, RecipientStatus.PROCESSING]),
        )
        .values(status=RecipientStatus.FAILED, error_message=f"Job aborted: {reason}"[:1000], updated_at=now)
        .execution_options(**_NO_SYNC)
    )
    session.execute(
        update(GenerationJob)
        .where(GenerationJob.id == job_id)
        .values(
            status=JobStatus.FAILED,
            error_message=reason[:1000],
            completed_at=now,
            updated_at=now,
            locked_by=None,
        )
        .execution_options(**_NO_SYNC)
    )
    session.commit()
    logger.error("Job %s aborted: %s", job_id, reason)


def recover_stale_jobs(session: Session, settings: Settings) -> list[str]:
    """Recover jobs whose worker died (PROCESSING with an old heartbeat).

    * Recipients stuck in PROCESSING go back to PENDING; COMPLETED/FAILED ones are kept.
    * The job returns to PENDING and is picked up again, until ``max_job_attempts`` claims,
      after which it is aborted (FAILED) so a poison job cannot loop forever.
    """
    cutoff = utcnow() - timedelta(seconds=settings.stale_job_seconds)
    stale = session.scalars(
        select(GenerationJob).where(
            GenerationJob.status == JobStatus.PROCESSING,
            or_(GenerationJob.heartbeat_at.is_(None), GenerationJob.heartbeat_at <= cutoff),
        )
    ).all()
    recovered: list[str] = []
    for job in stale:
        job_id, attempts = job.id, job.attempts
        if attempts >= settings.max_job_attempts:
            abort_job(session, job_id, f"abandoned after {attempts} processing attempts")
        else:
            session.execute(
                update(Recipient)
                .where(Recipient.job_id == job_id, Recipient.status == RecipientStatus.PROCESSING)
                .values(status=RecipientStatus.PENDING, updated_at=utcnow())
                .execution_options(**_NO_SYNC)
            )
            session.execute(
                update(GenerationJob)
                .where(GenerationJob.id == job_id, GenerationJob.status == JobStatus.PROCESSING)
                .values(status=JobStatus.PENDING, locked_by=None, updated_at=utcnow())
                .execution_options(**_NO_SYNC)
            )
            session.commit()
            logger.warning("Job %s was stale; re-queued (attempt %d)", job_id, attempts)
        recovered.append(job_id)
    return recovered
