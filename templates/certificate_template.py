"""The single predefined certificate design (A4 landscape, drawn with ReportLab)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import A4, landscape
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen.canvas import Canvas

PAGE_WIDTH, PAGE_HEIGHT = landscape(A4)
NAVY = HexColor("#1B2A49")
GOLD = HexColor("#B8923A")
GREY = HexColor("#5A6270")
MAX_TEXT_WIDTH = PAGE_WIDTH - 140


@dataclass(frozen=True)
class CertificateData:
    recipient_name: str
    event_name: str
    issuer_name: str
    issue_date: date
    certificate_id: str


def format_date(value: date) -> str:
    return f"{value.day} {value:%B %Y}"


def _fit_font_size(text: str, font: str, preferred: float, minimum: float = 8.0) -> float:
    """Shrink the font until the text fits the printable width."""
    size = preferred
    while size > minimum and stringWidth(text, font, size) > MAX_TEXT_WIDTH:
        size -= 0.5
    return size


def _centered(c: Canvas, y: float, text: str, font: str, size: float, color=NAVY, fit: bool = False) -> None:
    if fit:
        size = _fit_font_size(text, font, size)
    c.setFont(font, size)
    c.setFillColor(color)
    c.drawCentredString(PAGE_WIDTH / 2, y, text)


def _draw_border(c: Canvas) -> None:
    c.setStrokeColor(NAVY)
    c.setLineWidth(6)
    c.rect(24, 24, PAGE_WIDTH - 48, PAGE_HEIGHT - 48)
    c.setStrokeColor(GOLD)
    c.setLineWidth(1.5)
    c.rect(36, 36, PAGE_WIDTH - 72, PAGE_HEIGHT - 72)
    # Small gold squares in the corners
    c.setFillColor(GOLD)
    for x in (30, PAGE_WIDTH - 42):
        for y in (30, PAGE_HEIGHT - 42):
            c.rect(x, y, 12, 12, stroke=0, fill=1)


def draw_certificate(c: Canvas, data: CertificateData) -> None:
    _draw_border(c)
    _centered(c, PAGE_HEIGHT - 120, "CERTIFICATE", "Times-Bold", 46)
    _centered(c, PAGE_HEIGHT - 148, "OF PARTICIPATION", "Helvetica", 15, GOLD)

    c.setStrokeColor(GOLD)
    c.setLineWidth(1)
    c.line(PAGE_WIDTH / 2 - 90, PAGE_HEIGHT - 164, PAGE_WIDTH / 2 + 90, PAGE_HEIGHT - 164)

    _centered(c, PAGE_HEIGHT - 205, "This is to certify that", "Helvetica-Oblique", 14, GREY)
    _centered(c, PAGE_HEIGHT - 260, data.recipient_name, "Times-BoldItalic", 40, fit=True)

    name_size = _fit_font_size(data.recipient_name, "Times-BoldItalic", 40)
    name_width = stringWidth(data.recipient_name, "Times-BoldItalic", name_size)
    c.setStrokeColor(NAVY)
    c.setLineWidth(0.8)
    c.line(PAGE_WIDTH / 2 - name_width / 2 - 20, PAGE_HEIGHT - 270, PAGE_WIDTH / 2 + name_width / 2 + 20, PAGE_HEIGHT - 270)

    _centered(c, PAGE_HEIGHT - 305, "has successfully participated in", "Helvetica-Oblique", 14, GREY)
    _centered(c, PAGE_HEIGHT - 345, data.event_name, "Helvetica-Bold", 24, fit=True)

    _centered(c, PAGE_HEIGHT - 395, "Issued by", "Helvetica", 11, GREY)
    _centered(c, PAGE_HEIGHT - 415, data.issuer_name, "Helvetica-Bold", 16, fit=True)

    _centered(c, 88, f"Date of issue: {format_date(data.issue_date)}", "Helvetica", 12)
    _centered(c, 64, f"Certificate ID: {data.certificate_id}", "Helvetica", 10, GREY)
