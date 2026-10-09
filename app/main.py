"""Application factory. Run with: uvicorn app.main:app"""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api import certificates, jobs
from app.config import Settings
from app.database import create_db_engine, create_session_factory, init_db
from app.workers.job_worker import JobWorker

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    engine = create_db_engine(settings.database_url)
    session_factory = create_session_factory(engine)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        settings.storage_dir.mkdir(parents=True, exist_ok=True)
        init_db(engine)
        worker: JobWorker | None = None
        if settings.worker_enabled:
            worker = JobWorker(session_factory, settings)
            worker.start()
        app.state.worker = worker
        yield
        if worker:
            worker.stop()
        engine.dispose()

    app = FastAPI(title="Bulk Certificate Generator API", version="1.0.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.worker = None

    @app.middleware("http")
    async def limit_request_size(request: Request, call_next):  # type: ignore[no-untyped-def]
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > settings.max_request_bytes:
            return JSONResponse({"detail": "Request body too large"}, status_code=413)
        return await call_next(request)

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse({"detail": "Internal server error"}, status_code=500)

    app.include_router(jobs.router)
    app.include_router(certificates.router)

    @app.get("/health", tags=["health"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
