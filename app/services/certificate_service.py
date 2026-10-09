"""Certificate file handling: safe paths, atomic writes, lookup for downloads."""
from __future__ import annotations

import logging
import os
import re
import uuid
from collections.abc import Callable
from pathlib import Path, PurePosixPath

from sqlalchemy.orm import Session

from app.models import Certificate, GenerationJob, Recipient
from app.services.pdf_generator import CertificateGenerationError
from templates.certificate_template import CertificateData

logger = logging.getLogger(__name__)

PdfGenerator = Callable[[CertificateData, Path], None]
_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$|^CERT-[0-9A-F]{16}$")
_JOB_ID_PATTERN = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


class UnsafePathError(Exception):
    """A stored path resolves outside the storage root."""


def new_certificate_id() -> str:
    return f"CERT-{uuid.uuid4().hex[:16].upper()}"


def resolve_storage_path(storage_root: Path, relative_path: str) -> Path:
    """Resolve ``relative_path`` under ``storage_root`` and refuse anything that escapes it."""
    root = storage_root.resolve()
    candidate = (root / relative_path).resolve()
    if not candidate.is_relative_to(root):
        raise UnsafePathError("path escapes storage root")
    return candidate


def build_relative_path(job_id: str, certificate_id: str) -> str:
    """Server-controlled path. Both parts are server-generated; recipient data is never used."""
    if not _JOB_ID_PATTERN.match(job_id) or not _ID_PATTERN.match(certificate_id):
        raise CertificateGenerationError("Invalid internal identifier")
    return str(PurePosixPath("certificates") / job_id / f"{certificate_id}.pdf")


def describe_generation_error(exc: Exception) -> str:
    """Client-safe error text; full details (and traceback) go to the server log only."""
    if isinstance(exc, CertificateGenerationError):
        return str(exc)
    return f"Unexpected error while generating certificate ({type(exc).__name__})"


def create_certificate_file(
    job: GenerationJob, recipient: Recipient, generator: PdfGenerator, storage_root: Path
) -> tuple[str, int]:
    """Generate the PDF atomically (temp file + rename). Returns (relative_path, size)."""
    certificate_id = recipient.reserved_certificate_id
    if certificate_id is None:
        raise CertificateGenerationError("Recipient has no reserved certificate id")
    relative_path = build_relative_path(job.id, certificate_id)
    final_path = resolve_storage_path(storage_root, relative_path)
    final_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = final_path.with_name(final_path.name + ".tmp")
    data = CertificateData(
        recipient_name=recipient.name,
        event_name=job.event_name,
        issuer_name=job.issuer_name,
        issue_date=job.issue_date,
        certificate_id=certificate_id,
    )
    try:
        generator(data, temp_path)
        if not temp_path.is_file() or temp_path.stat().st_size == 0:
            raise CertificateGenerationError("Generator produced no output")
        os.replace(temp_path, final_path)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise
    return relative_path, final_path.stat().st_size


def remove_certificate_file(storage_root: Path, relative_path: str) -> None:
    """Best-effort cleanup of a file whose database record could not be saved."""
    try:
        resolve_storage_path(storage_root, relative_path).unlink(missing_ok=True)
    except (OSError, UnsafePathError):
        logger.warning("Could not remove orphan certificate file %s", relative_path)


def get_certificate(session: Session, certificate_id: str) -> Certificate | None:
    return session.get(Certificate, certificate_id)
