"""Database-backed worker loop (runs as a daemon thread inside the application process)."""
from __future__ import annotations

import logging
import threading
import uuid

from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.models import GenerationJob, Recipient
from app.services import certificate_service, job_service
from app.services.certificate_service import PdfGenerator
from app.services.pdf_generator import generate_certificate_pdf

logger = logging.getLogger(__name__)


class JobWorker:
    """Polls the ``generation_jobs`` table. All coordination state lives in SQLite.

    ``run_once`` / ``run_until_idle`` execute synchronously, which is what the tests use
    (no threads, no sleeps). ``start`` / ``stop`` run the same code in a background thread.
    """

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        settings: Settings,
        generator: PdfGenerator = generate_certificate_pdf,
        worker_id: str | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings
        self.generator = generator
        self.worker_id = worker_id or f"worker-{uuid.uuid4().hex[:8]}"
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name=self.worker_id, daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)

    def _loop(self) -> None:
        logger.info("Worker %s started", self.worker_id)
        while not self._stop.is_set():
            try:
                did_work = self.run_once()
            except Exception:  # keep the loop alive whatever happens
                logger.exception("Unexpected worker loop error")
                did_work = False
            if not did_work:
                self._stop.wait(self.settings.worker_poll_interval_seconds)
        logger.info("Worker %s stopped", self.worker_id)

    # ------------------------------------------------------------ processing
    def run_once(self) -> bool:
        """Recover stale jobs, then claim and fully process at most one job."""
        with self.session_factory() as session:
            job_service.recover_stale_jobs(session, self.settings)
            job_id = job_service.claim_next_job(session, self.worker_id)
        if job_id is None:
            return False
        self.process_job(job_id)
        return True

    def run_until_idle(self, max_jobs: int = 1000) -> int:
        processed = 0
        while processed < max_jobs and self.run_once():
            processed += 1
        return processed

    def process_job(self, job_id: str) -> None:
        try:
            while True:
                with self.session_factory() as session:
                    batch = job_service.next_pending_recipient_ids(session, job_id, self.settings.worker_batch_size)
                if not batch:
                    break
                for recipient_id in batch:
                    if self._stop.is_set():
                        with self.session_factory() as session:
                            job_service.release_job(session, job_id)
                        return
                    self._process_recipient(job_id, recipient_id)
            with self.session_factory() as session:
                job_service.finalize_job(session, job_id)
        except Exception as exc:
            logger.exception("Job %s failed unexpectedly", job_id)
            with self.session_factory() as session:
                job_service.abort_job(session, job_id, f"internal error ({type(exc).__name__})")

    def _process_recipient(self, job_id: str, recipient_id: int) -> None:
        """One recipient = one isolated unit of work. Never raises for generation problems."""
        with self.session_factory() as session:
            if not job_service.claim_recipient(session, job_id, recipient_id):
                return
            job = session.get(GenerationJob, job_id)
            recipient = session.get(Recipient, recipient_id)
            assert job is not None and recipient is not None
            relative_path: str | None = None
            try:
                relative_path, size = certificate_service.create_certificate_file(
                    job, recipient, self.generator, self.settings.storage_dir
                )
                job_service.mark_recipient_completed(session, recipient, relative_path, size)
                session.commit()
                logger.info("Job %s recipient %s: certificate generated", job_id, recipient_id)
            except Exception as exc:
                session.rollback()
                logger.exception("Job %s recipient %s: certificate generation failed", job_id, recipient_id)
                if relative_path is not None:
                    certificate_service.remove_certificate_file(self.settings.storage_dir, relative_path)
                job_service.mark_recipient_failed(
                    session, recipient_id, certificate_service.describe_generation_error(exc)
                )
