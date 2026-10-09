"""Downloads and certificate listing (requirements 11, 12, 13)."""
from __future__ import annotations

from sqlalchemy import select, update

from app.models import Certificate


def _completed_job(submit, worker, client):
    job_id = submit()
    worker.run_until_idle()
    return job_id, client.get(f"/api/jobs/{job_id}/").json()


def test_generated_certificate_can_be_downloaded_as_pdf(client, submit, worker):
    _, body = _completed_job(submit, worker, client)
    recipient = body["recipients"][0]

    response = client.get(recipient["download_url"])

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.content.startswith(b"%PDF")
    disposition = response.headers["content-disposition"]
    assert f"certificate-{recipient['certificate_id']}.pdf" in disposition
    assert "Ananya" not in disposition  # file name is server-controlled, not user data


def test_list_job_certificates(client, submit, worker, payload_factory):
    job_id = submit(payload_factory([
        {"name": "Ok One", "email": "1@example.com"},
        {"name": "", "email": "bad"},
        {"name": "Ok Two", "email": "2@example.com"},
    ]))
    assert client.get(f"/api/jobs/{job_id}/certificates/").json() == {"job_id": job_id, "count": 0, "certificates": []}

    worker.run_until_idle()
    body = client.get(f"/api/jobs/{job_id}/certificates/").json()

    assert body["count"] == 2
    assert [c["recipient_name"] for c in body["certificates"]] == ["Ok One", "Ok Two"]
    for cert in body["certificates"]:
        assert cert["download_url"] == f"/api/certificates/{cert['certificate_id']}/download"
        assert client.get(cert["download_url"]).status_code == 200


def test_certificates_are_scoped_to_their_job(client, submit, worker, payload_factory):
    first = submit(payload_factory([{"name": "First Job", "email": "f@example.com"}]))
    second = submit(payload_factory([{"name": "Second Job", "email": "s@example.com"}]))
    worker.run_until_idle()
    names = lambda job_id: [c["recipient_name"] for c in client.get(f"/api/jobs/{job_id}/certificates/").json()["certificates"]]
    assert names(first) == ["First Job"] and names(second) == ["Second Job"]


def test_unknown_certificate_returns_404(client):
    assert client.get("/api/certificates/CERT-0000000000000000/download").status_code == 404
    assert client.get("/api/certificates/nope/download").status_code == 404


def test_missing_file_returns_410_without_leaking_paths(client, submit, worker, settings, session_factory):
    _, body = _completed_job(submit, worker, client)
    cert_id = body["recipients"][0]["certificate_id"]
    with session_factory() as session:
        cert = session.get(Certificate, cert_id)
        (settings.storage_dir / cert.file_path).unlink()

    response = client.get(f"/api/certificates/{cert_id}/download")

    assert response.status_code == 410
    assert response.json() == {"detail": "Certificate file is no longer available"}
    assert str(settings.storage_dir) not in response.text


def test_path_traversal_in_url_cannot_read_files(client, submit, worker):
    _completed_job(submit, worker, client)
    for attempt in (
        "../../../etc/passwd",
        "..%2f..%2f..%2fetc%2fpasswd",
        "%2e%2e%2f%2e%2e%2fetc%2fpasswd",
        "....//....//etc/passwd",
    ):
        response = client.get(f"/api/certificates/{attempt}/download")
        assert response.status_code in (404, 405)
        assert "root:" not in response.text


def test_tampered_database_path_outside_storage_is_not_served(client, submit, worker, settings, session_factory, tmp_path):
    _, body = _completed_job(submit, worker, client)
    cert_id = body["recipients"][0]["certificate_id"]
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("TOP SECRET")
    with session_factory() as session:
        session.execute(update(Certificate).where(Certificate.certificate_id == cert_id).values(file_path="../../outside-secret.txt"))
        session.commit()

    response = client.get(f"/api/certificates/{cert_id}/download")

    assert response.status_code == 500
    assert response.json() == {"detail": "Certificate cannot be served"}
    assert "TOP SECRET" not in response.text
