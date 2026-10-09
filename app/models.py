"""SQLAlchemy 2.x models: GenerationJob -> Recipient -> Certificate."""
from __future__ import annotations

import enum
import uuid
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    """Naive UTC timestamp (SQLite does not store tzinfo)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class JobStatus(str, enum.Enum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    COMPLETED_WITH_ERRORS = "COMPLETED_WITH_ERRORS"
    FAILED = "FAILED"


TERMINAL_JOB_STATUSES = {JobStatus.COMPLETED, JobStatus.COMPLETED_WITH_ERRORS, JobStatus.FAILED}


class RecipientStatus(str, enum.Enum):
    INVALID = "INVALID"
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


def _enum_column(enum_cls: type[enum.Enum]) -> Enum:
    # Stored as VARCHAR with a CHECK constraint (portable, readable in SQLite).
    return Enum(enum_cls, native_enum=False, length=32, create_constraint=True, validate_strings=True)


class GenerationJob(Base):
    __tablename__ = "generation_jobs"
    __table_args__ = (
        CheckConstraint("total_recipients >= 0", name="ck_jobs_total_non_negative"),
        Index("ix_jobs_status_created", "status", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    event_name: Mapped[str] = mapped_column(String(255))
    issuer_name: Mapped[str] = mapped_column(String(255))
    issue_date: Mapped[date] = mapped_column(Date)
    status: Mapped[JobStatus] = mapped_column(_enum_column(JobStatus), default=JobStatus.PENDING)
    total_recipients: Mapped[int] = mapped_column(Integer, default=0)
    attempts: Mapped[int] = mapped_column(Integer, default=0)  # times a worker claimed this job
    locked_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    recipients: Mapped[list["Recipient"]] = relationship(
        back_populates="job", cascade="all, delete-orphan", order_by="Recipient.position"
    )
    certificates: Mapped[list["Certificate"]] = relationship(
        back_populates="job", cascade="all, delete-orphan", order_by="Certificate.created_at"
    )


class Recipient(Base):
    __tablename__ = "recipients"
    __table_args__ = (
        UniqueConstraint("job_id", "position", name="uq_recipient_job_position"),
        Index("ix_recipients_job_status", "job_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("generation_jobs.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer)  # 0-based index in the submitted list
    # For INVALID rows these hold a truncated representation of what was submitted.
    name: Mapped[str] = mapped_column(String(255))
    email: Mapped[str] = mapped_column(String(320))
    status: Mapped[RecipientStatus] = mapped_column(_enum_column(RecipientStatus), default=RecipientStatus.PENDING)
    validation_errors: Mapped[list[Any] | None] = mapped_column(JSON, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)  # generation failure
    # Reserved when the job is created so a retry overwrites the same file instead of orphaning one.
    reserved_certificate_id: Mapped[str | None] = mapped_column(String(32), unique=True, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    job: Mapped[GenerationJob] = relationship(back_populates="recipients")
    certificate: Mapped["Certificate | None"] = relationship(back_populates="recipient", uselist=False)


class Certificate(Base):
    __tablename__ = "certificates"

    certificate_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("generation_jobs.id", ondelete="CASCADE"), index=True)
    recipient_id: Mapped[int] = mapped_column(ForeignKey("recipients.id", ondelete="CASCADE"), unique=True)
    file_path: Mapped[str] = mapped_column(String(512))  # relative to STORAGE_DIR, POSIX separators
    file_size: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    job: Mapped[GenerationJob] = relationship(back_populates="certificates")
    recipient: Mapped[Recipient] = relationship(back_populates="certificate")
