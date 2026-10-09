"""PDF generation with ReportLab. Writes to a caller-supplied path; never derives paths from user data."""
from __future__ import annotations

from pathlib import Path

from reportlab.lib.pagesizes import A4, landscape
from reportlab.pdfgen.canvas import Canvas

from templates.certificate_template import CertificateData, draw_certificate


class CertificateGenerationError(Exception):
    """A generation failure whose message is safe to show to API clients."""


def generate_certificate_pdf(data: CertificateData, destination: Path) -> None:
    """Render one certificate to ``destination`` (a server-controlled path)."""
    try:
        canvas = Canvas(str(destination), pagesize=landscape(A4), pageCompression=1)
        canvas.setTitle(f"Certificate - {data.certificate_id}")
        canvas.setAuthor(data.issuer_name)
        canvas.setSubject(data.event_name)
        draw_certificate(canvas, data)
        canvas.showPage()
        canvas.save()
    except OSError as exc:
        raise CertificateGenerationError("Could not write the certificate file") from exc
