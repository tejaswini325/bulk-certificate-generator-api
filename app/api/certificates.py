from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.config import Settings
from app.dependencies import get_db, get_settings
from app.services import certificate_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/certificates", tags=["certificates"])


@router.get("/{certificate_id}/download")
def download_certificate(
    certificate_id: str,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> FileResponse:
    """The URL parameter is only ever used as a database key, never as a filesystem path."""
    certificate = certificate_service.get_certificate(db, certificate_id)
    if certificate is None:
        raise HTTPException(status_code=404, detail="Certificate not found")
    try:
        path = certificate_service.resolve_storage_path(settings.storage_dir, certificate.file_path)
    except certificate_service.UnsafePathError:
        logger.error("Certificate %s has a stored path outside the storage root", certificate_id)
        raise HTTPException(status_code=500, detail="Certificate cannot be served") from None
    if not path.is_file():
        logger.error("Certificate %s: database record exists but file is missing", certificate_id)
        raise HTTPException(status_code=410, detail="Certificate file is no longer available")
    return FileResponse(
        path,
        media_type="application/pdf",
        filename=f"certificate-{certificate.certificate_id}.pdf",  # server-controlled name
    )
