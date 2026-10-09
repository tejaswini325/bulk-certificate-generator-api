"""PDF generation, storage and filename safety (requirements 5, 6 support)."""
from __future__ import annotations

import re
from pathlib import Path

from pypdf import PdfReader
from sqlalchemy import select

from app.models import Certificate


def pdf_text(path: Path) -> str:
    return "\n".join(page.extract_text() for page in PdfReader(str(path)).pages)


def test_pdf_is_generated_with_all_required_fields(client, submit, worker, settings, session_factory):
    job_id = submit()
    worker.run_until_idle()

    body = client.get(f"/api/jobs/{job_id}/").json()
    assert body["status"] == "COMPLETED"
    with session_factory() as session:
        certs = session.scalars(select(Certificate).order_by(Certificate.created_at)).all()
    assert len(certs) == 2

    by_id = {r["certificate_id"]: r["name"] for r in body["recipients"]}
    for cert in certs:
        pdf_path = settings.storage_dir / cert.file_path
        assert pdf_path.read_bytes().startswith(b"%PDF")
        text = pdf_text(pdf_path)
        assert by_id[cert.certificate_id] in text          # recipient name
        assert "Python Workshop 2026" in text              # event name
        assert "ABC Organization" in text                  # issuer name
        assert "9 October 2026" in text                    # issue date
        assert cert.certificate_id in text                 # unique certificate identifier
        assert cert.file_size == pdf_path.stat().st_size


def test_certificate_ids_are_unique_and_files_use_server_paths(client, submit, worker, settings, session_factory):
    job_id = submit()
    worker.run_until_idle()
    with session_factory() as session:
        certs = session.scalars(select(Certificate)).all()

    assert len({c.certificate_id for c in certs}) == len(certs) == 2
    for cert in certs:
        assert re.fullmatch(rf"certificates/{job_id}/CERT-[0-9A-F]{{16}}\.pdf", cert.file_path)
        assert not Path(cert.file_path).is_absolute()  # stored relative to STORAGE_DIR


def test_hostile_names_never_reach_the_filesystem(client, submit, worker, settings, payload_factory, tmp_path):
    names = ["../../etc/passwd", "..\\..\\windows\\system32", "a/b/c", "evil.pdf; rm -rf /", "%2e%2e%2f"]
    recipients = [{"name": n, "email": f"user{i}@example.com"} for i, n in enumerate(names)]
    job_id = submit(payload_factory(recipients))
    worker.run_until_idle()

    body = client.get(f"/api/jobs/{job_id}/").json()
    assert body["status"] == "COMPLETED"
    produced = sorted(p for p in settings.storage_dir.rglob("*") if p.is_file())
    assert len(produced) == len(names)
    assert all(p.parent == settings.storage_dir / "certificates" / job_id for p in produced)
    assert not list(tmp_path.glob("*.pdf"))                      # nothing escaped next to the DB
    assert not any(p.suffix == ".tmp" for p in produced)         # no leftover temp files


def test_api_responses_never_expose_filesystem_paths(client, submit, worker, settings):
    job_id = submit()
    worker.run_until_idle()
    for url in (f"/api/jobs/{job_id}/", f"/api/jobs/{job_id}/certificates/"):
        text = client.get(url).text
        assert str(settings.storage_dir) not in text
        assert "file_path" not in text and "storage" not in text.lower()


def test_unicode_latin_name_is_rendered(client, submit, worker, settings, session_factory, payload_factory):
    submit(payload_factory([{"name": "José Müller", "email": "jose@example.com"}]))
    worker.run_until_idle()
    with session_factory() as session:
        cert = session.scalars(select(Certificate)).one()
    assert "Jos" in pdf_text(settings.storage_dir / cert.file_path)


def test_very_long_name_still_produces_a_pdf(client, submit, worker, settings, session_factory, payload_factory):
    submit(payload_factory([{"name": "W" * 100, "email": "wide@example.com"}], event_name="E" * 120))
    worker.run_until_idle()
    with session_factory() as session:
        cert = session.scalars(select(Certificate)).one()
    assert (settings.storage_dir / cert.file_path).read_bytes().startswith(b"%PDF")
