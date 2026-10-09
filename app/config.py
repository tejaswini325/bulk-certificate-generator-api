"""Application settings, read from environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    database_url: str = "sqlite:///./certificates.db"
    storage_dir: Path = Path("./storage")
    max_recipients_per_job: int = 500
    max_name_length: int = 100
    max_request_bytes: int = 1_048_576
    worker_enabled: bool = True
    worker_poll_interval_seconds: float = 1.0
    worker_batch_size: int = 50  # recipients loaded per DB round trip
    stale_job_seconds: int = 60  # PROCESSING job with older heartbeat is considered abandoned
    max_job_attempts: int = 3
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "Settings":
        defaults = cls()
        return cls(
            database_url=os.environ.get("DATABASE_URL", defaults.database_url),
            storage_dir=Path(os.environ.get("STORAGE_DIR", str(defaults.storage_dir))),
            max_recipients_per_job=int(os.environ.get("MAX_RECIPIENTS_PER_JOB", defaults.max_recipients_per_job)),
            max_name_length=int(os.environ.get("MAX_NAME_LENGTH", defaults.max_name_length)),
            max_request_bytes=int(os.environ.get("MAX_REQUEST_BYTES", defaults.max_request_bytes)),
            worker_enabled=_env_bool("WORKER_ENABLED", defaults.worker_enabled),
            worker_poll_interval_seconds=float(
                os.environ.get("WORKER_POLL_INTERVAL_SECONDS", defaults.worker_poll_interval_seconds)
            ),
            worker_batch_size=int(os.environ.get("WORKER_BATCH_SIZE", defaults.worker_batch_size)),
            stale_job_seconds=int(os.environ.get("STALE_JOB_SECONDS", defaults.stale_job_seconds)),
            max_job_attempts=int(os.environ.get("MAX_JOB_ATTEMPTS", defaults.max_job_attempts)),
            log_level=os.environ.get("LOG_LEVEL", defaults.log_level).upper(),
        )
