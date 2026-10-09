# Bulk Certificate Generator API

## 1. Project overview and objectives

A small FastAPI backend that accepts **one request containing many recipients** and generates a PDF certificate for each of them **in the background**. The client gets a job ID immediately (HTTP 202), then polls for progress and downloads the finished PDFs.

Objectives: bulk input without one API call per certificate, independent handling of every recipient (one bad recipient or one failed PDF never affects the others), progress that is always derived from persisted data, and simple, explainable code (no Redis/Celery/Docker).

## 2. Features and requirement coverage

| Requirement | Where it is implemented |
|---|---|
| `POST /api/jobs/` → 202 + job id, no generation in the request | `app/api/jobs.py`, `job_service.create_job` |
| Per-recipient validation, invalid ones recorded, valid ones kept | `app/validation.py`, `job_service.create_job` |
| Single ReportLab certificate template | `templates/certificate_template.py`, `app/services/pdf_generator.py` |
| Background processing, DB-backed worker, progress persisted | `app/workers/job_worker.py`, `job_service.py` |
| Job + recipient statuses, progress statistics | `models.py`, `job_service.compute_progress` |
| `GET /api/jobs/{job_id}/` (404 if unknown) | `app/api/jobs.py` |
| `GET /api/certificates/{certificate_id}/download` | `app/api/certificates.py` |
| `GET /api/jobs/{job_id}/certificates/` | `app/api/jobs.py` |
| SQLAlchemy 2.x models with constraints/indexes | `app/models.py` |
| 92 automated tests, temp DB + temp storage, deterministic worker | `tests/` |

Extras beyond the brief (small and intentional): `/health`, request body size limit (413), duplicate-email detection, stale-job recovery with an attempt cap, graceful worker shutdown.

## 3. Technology stack

Python 3.11+ · FastAPI · SQLAlchemy 2.x · SQLite · Pydantic v2 (+ `email-validator`) · ReportLab · Uvicorn · Pytest + FastAPI `TestClient` (+ `pypdf` to read PDF text in tests).

## 4. Prerequisites

Python 3.11 or newer and `pip`. Nothing else (no Docker, Redis, or external services).

## 5. Virtual environment setup

Linux / macOS:
```bash
python3 -m venv .venv
source .venv/bin/activate
```
Windows (PowerShell):
```powershell
py -3 -m venv .venv
.venv\Scripts\Activate.ps1
```
Windows (cmd): `.venv\Scripts\activate.bat`

## 6. Dependency installation

```bash
pip install -r requirements.txt
```

## 7. Environment configuration

All settings are environment variables with defaults (see `.env.example`; the app reads `os.environ` and does **not** load `.env` files — export the variables or set them in your shell/process manager).

| Variable | Default | Meaning |
|---|---|---|
| `DATABASE_URL` | `sqlite:///./certificates.db` | SQLAlchemy URL |
| `STORAGE_DIR` | `./storage` | Root directory for generated PDFs |
| `MAX_RECIPIENTS_PER_JOB` | `500` | Max recipients in one request (HTTP 422 above it) |
| `MAX_NAME_LENGTH` | `100` | Max recipient name length |
| `MAX_REQUEST_BYTES` | `1048576` | Max declared request body size (HTTP 413 above it) |
| `WORKER_ENABLED` | `true` | Start the background worker thread with the app |
| `WORKER_POLL_INTERVAL_SECONDS` | `1.0` | Sleep between polls when idle |
| `WORKER_BATCH_SIZE` | `50` | Recipient ids loaded per DB round trip |
| `STALE_JOB_SECONDS` | `60` | A PROCESSING job whose heartbeat is older than this is considered abandoned |
| `MAX_JOB_ATTEMPTS` | `3` | Times a job may be claimed before it is failed permanently |
| `LOG_LEVEL` | `INFO` | Python logging level |

## 8. Database initialization

Automatic and idempotent: on startup the app creates the storage directory and runs `Base.metadata.create_all` (tables are created only if missing). There are no migrations; schema changes need a new database or manual migration (see Known limitations).

## 9. Application startup command

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8000
```
Interactive docs: <http://127.0.0.1:8000/docs>. **Run exactly one Uvicorn process** (no `--workers N`); see section 17.

## 10. Test execution command

```bash
python -m pytest
```
Tests use a temporary SQLite file and temporary storage directory per test; they never touch `certificates.db` or `storage/`.

## 11. API documentation

### `POST /api/jobs/` → `202 Accepted`

Request:
```json
{
  "event_name": "Python Workshop 2026",
  "issuer_name": "ABC Organization",
  "issue_date": "2026-10-09",
  "recipients": [
    {"name": "Ananya Rao", "email": "ananya@example.com"},
    {"name": "Rahul Kumar", "email": "rahul@example.com"}
  ]
}
```
Response (header `Location: /api/jobs/{job_id}/`):
```json
{
  "job_id": "3054db30-102b-426c-8387-1646e0689548",
  "status": "PENDING",
  "total_recipients": 2,
  "valid_recipients": 2,
  "invalid_recipients": 0,
  "status_url": "/api/jobs/3054db30-102b-426c-8387-1646e0689548/",
  "certificates_url": "/api/jobs/3054db30-102b-426c-8387-1646e0689548/certificates/"
}
```
Errors: `422` invalid request structure or too many recipients, `413` body too large.

### `GET /api/jobs/{job_id}/` → `200` / `404`

```json
{
  "job_id": "3054db30-...",
  "event_name": "Python Workshop 2026",
  "issuer_name": "ABC Organization",
  "issue_date": "2026-10-09",
  "status": "COMPLETED_WITH_ERRORS",
  "error_message": null,
  "created_at": "2026-10-09T06:20:53.256495",
  "started_at": "2026-10-09T06:20:53.804648",
  "completed_at": "2026-10-09T06:20:53.842016",
  "progress": {
    "total_recipients": 4, "valid_recipients": 3, "invalid_recipients": 1,
    "successful": 3, "failed": 0, "pending": 0, "processing": 0,
    "progress_percentage": 100.0
  },
  "recipients": [
    {"position": 0, "name": "Ananya Rao", "email": "ananya@example.com", "status": "COMPLETED",
     "validation_errors": [], "error_message": null,
     "certificate_id": "CERT-00A5DD52E59C48C4",
     "download_url": "/api/certificates/CERT-00A5DD52E59C48C4/download"},
    {"position": 2, "name": "", "email": "bad", "status": "INVALID",
     "validation_errors": ["name must not be empty", "email is not valid: ..."],
     "error_message": null, "certificate_id": null, "download_url": null}
  ],
  "certificates_url": "/api/jobs/3054db30-.../certificates/"
}
```
Timestamps are UTC (naive ISO-8601).

### `GET /api/jobs/{job_id}/certificates/` → `200` / `404`

```json
{
  "job_id": "3054db30-...",
  "count": 2,
  "certificates": [
    {"certificate_id": "CERT-00A5DD52E59C48C4", "recipient_name": "Ananya Rao",
     "download_url": "/api/certificates/CERT-00A5DD52E59C48C4/download",
     "created_at": "2026-10-09T06:20:53.827151"}
  ]
}
```
Only successfully generated certificates are listed.

### `GET /api/certificates/{certificate_id}/download`

* `200` — `application/pdf`, `Content-Disposition: attachment; filename="certificate-<id>.pdf"`
* `404` — unknown certificate id
* `410 Gone` — database record exists but the file is missing
* `500` (`"Certificate cannot be served"`) — stored path would escape the storage root (only possible if the database was tampered with)

### `GET /health` → `{"status": "ok"}`

## 12. Creating a generation job

```bash
curl -i -X POST http://127.0.0.1:8000/api/jobs/ \
  -H "Content-Type: application/json" \
  -d '{"event_name":"Python Workshop 2026","issuer_name":"ABC Organization","issue_date":"2026-10-09",
       "recipients":[{"name":"Ananya Rao","email":"ananya@example.com"},{"name":"Rahul Kumar","email":"rahul@example.com"}]}'
```

## 13. Checking job progress

```bash
curl http://127.0.0.1:8000/api/jobs/<job_id>/
```
Poll until `status` is `COMPLETED`, `COMPLETED_WITH_ERRORS` or `FAILED`.

**Job statuses**

| Status | Meaning |
|---|---|
| `PENDING` | Queued (or returned to the queue after recovery/shutdown) |
| `PROCESSING` | A worker has claimed it |
| `COMPLETED` | Every submitted recipient got a certificate |
| `COMPLETED_WITH_ERRORS` | At least one certificate generated, and at least one recipient INVALID or FAILED |
| `FAILED` | No certificate generated (all invalid, all failed), or the job was aborted/abandoned |

**Recipient statuses:** `INVALID` (rejected at submission), `PENDING`, `PROCESSING`, `COMPLETED`, `FAILED`.

**Progress fields** (computed with one `GROUP BY` over persisted recipient rows on every request): `total_recipients`, `valid_recipients`, `invalid_recipients`, `successful` (COMPLETED), `failed`, `pending` (PENDING only), `processing`, `progress_percentage = (COMPLETED + FAILED + INVALID) / total × 100` — invalid recipients count as already finished, so a job with 1 invalid of 5 starts at 20 %.

## 14. Listing and downloading certificates

```bash
curl http://127.0.0.1:8000/api/jobs/<job_id>/certificates/
curl -o certificate.pdf http://127.0.0.1:8000/api/certificates/<certificate_id>/download
```

## 15. Database schema

```
generation_jobs 1 ──< recipients 1 ──0..1 certificates
        └─────────────────────────────────< (certificates.job_id)
```

**`generation_jobs`**: `id` (UUID, PK), `event_name`, `issuer_name`, `issue_date`, `status` (CHECK-constrained enum), `total_recipients` (CHECK ≥ 0), `attempts`, `locked_by`, `error_message`, `created_at`, `updated_at`, `started_at`, `heartbeat_at`, `completed_at`. Index `(status, created_at)` serves the worker's "oldest PENDING job" query.

**`recipients`**: `id` (PK), `job_id` (FK, `ON DELETE CASCADE`, indexed), `position` (index in the submitted list; `UNIQUE(job_id, position)`), `name`, `email` (for INVALID rows: truncated copy of what was sent), `status`, `validation_errors` (JSON list), `error_message` (generation failure), `reserved_certificate_id` (UNIQUE), `attempts`, timestamps. Index `(job_id, status)` serves progress counting and "next pending recipients".

**`certificates`**: `certificate_id` (PK, `CERT-` + 16 hex chars), `job_id` (FK, indexed), `recipient_id` (FK, **UNIQUE** → at most one certificate per recipient), `file_path` (**relative** to `STORAGE_DIR`, POSIX separators), `file_size`, `created_at`. PDF bytes are never stored in the database.

SQLite is opened with `foreign_keys=ON`, `busy_timeout=5000` and WAL journal mode.

## 16. Architecture and processing flow

```
Client ──POST──▶ route (thin) ──▶ job_service.create_job ──▶ SQLite  (job PENDING, recipients PENDING/INVALID)
   ◀── 202 + job_id                                              ▲
                                                                 │ poll / claim / update
JobWorker thread ─ recover_stale_jobs ─ claim_next_job (atomic UPDATE) ─ per recipient:
      claim_recipient (PENDING→PROCESSING, commit) → generator writes <id>.pdf.tmp → rename → 
      Certificate row + status COMPLETED (one commit)   |   exception → FAILED + error_message
   ─ when no PENDING recipients remain: finalize_job → terminal status
Client ──GET job / certificates / download──▶ read from SQLite + storage
```

Layers: `api/` (HTTP only) → `services/` (business logic) → `models.py`; `workers/job_worker.py` orchestrates; `templates/` holds the drawing code only.

## 17. Background-processing design and limitations

**Design.** A daemon thread inside the application process polls the `generation_jobs` table (default every 1 s). All queue state — job status, recipient status, heartbeat, attempt count — lives in SQLite, not in memory.

* **No double processing.** A job is claimed with `UPDATE ... SET status='PROCESSING' WHERE id=? AND status='PENDING'`; only the caller that sees `rowcount == 1` owns it. Recipients are claimed the same way (`PENDING → PROCESSING`). Tests verify two workers cannot claim the same job/recipient.
* **Isolation.** Each recipient is its own unit of work with its own transaction. Any exception is caught, logged with a traceback, and stored as `FAILED` with a client-safe message; the loop continues. A PDF is written to a `.tmp` file and renamed, so a crash never leaves a half-written certificate at the final path.
* **Completion rule.** `finalize_job` sets a terminal status only if no recipient is `PENDING`/`PROCESSING`.
* **Memory.** Recipients are fetched in batches of `WORKER_BATCH_SIZE` ids; the job size is additionally capped by `MAX_RECIPIENTS_PER_JOB`.
* **Unexpected job-level errors** (e.g. database failure inside the loop): the job and all its non-terminal recipients are marked `FAILED`, so nothing stays `PROCESSING` and progress reaches 100 %.
* **Graceful shutdown:** on stop the worker finishes the current recipient, returns the job to `PENDING`, and exits.

**Restart behaviour (honest version).** The *data* is durable; the *thread* is not. If the process is killed mid-job, the job stays `PROCESSING` with the recipient being worked on in `PROCESSING`. After restart the worker's `recover_stale_jobs` notices that the job's `heartbeat_at` (refreshed before every recipient) is older than `STALE_JOB_SECONDS` (default 60 s), moves stuck recipients back to `PENDING`, and re-queues the job. Finished recipients are kept and not regenerated. Because each recipient's certificate id is reserved at job creation, a retry rewrites the same file instead of leaving an orphan. After `MAX_JOB_ATTEMPTS` claims a job is failed (`abandoned after N processing attempts`) so a poison job cannot loop forever. Consequence: after a crash, recovery starts up to `STALE_JOB_SECONDS` after the restart.

**Supported deployment: one Uvicorn process.** Claims are atomic so a second process would not process the same *job* twice, but stale-job recovery assumes a stuck job's worker is dead; with several live processes a very slow job could be re-queued while still running. Don't use `--workers N` with this design.

## 18. Validation and failure-handling strategy

**Invalid request vs invalid recipient** (the distinction the brief asks for):

| | Completely invalid API request | Invalid recipient record |
|---|---|---|
| Examples | missing/empty `event_name`, `issuer_name`; bad `issue_date`; `recipients` missing, not a list, or empty; more than `MAX_RECIPIENTS_PER_JOB` recipients; body not JSON or too large | empty/over-long/non-string `name`; invalid `email`; recipient that is not an object; duplicate email within the request |
| Result | HTTP **422** (413 for size), **nothing is stored** | HTTP **202**; the recipient is stored with status `INVALID` and `validation_errors`; valid recipients are processed normally |

Recipient rules: name is whitespace-collapsed, 1–`MAX_NAME_LENGTH` chars, no control/invisible characters, and must be representable in Windows-1252 (ReportLab's built-in fonts); email is validated and normalised with `email-validator` (no DNS lookup), max 254 chars. Event/issuer names follow the same text rules (max 120).

**Chosen behaviours**
* *Duplicate recipients:* the same email (case-insensitive) twice in one request → first is kept, later ones are `INVALID` (`duplicate email: ...`). Same name with different emails is allowed.
* *Duplicate requests:* every POST creates a new job; there is no idempotency key. Posting twice yields two jobs and two sets of certificates.
* *Job with zero valid recipients:* created directly in `FAILED` (`No valid recipients in request`), progress 100 %, no worker involved. The POST still returns 202.
* *Generation failures:* recorded per recipient; client message is either a known safe message or `Unexpected error while generating certificate (<ExceptionType>)`. Details and tracebacks are only in the server log.
* *Unhandled exceptions* return `{"detail":"Internal server error"}` with no stack trace.

## 19. Storage and security considerations

* File path = `STORAGE_DIR/certificates/<job-uuid>/<CERT-id>.pdf`; both parts are server-generated and pattern-checked. Recipient data is never part of a path (tests submit names like `../../etc/passwd`).
* The download route uses the URL parameter only as a database key. The stored relative path is resolved and must stay inside the storage root (`UnsafePathError` otherwise); the download filename is `certificate-<id>.pdf`.
* Responses never contain filesystem paths.
* Certificate ids have 64 random bits, but **there is no authentication**: anyone who knows an id/URL can download it. Do not expose this service publicly as is.
* `.gitignore` excludes `.env`, virtualenvs, `*.db*` and everything under `storage/` except `.gitkeep`.
* Request size is limited via the `Content-Length` header only (chunked uploads without it are not checked; put a reverse proxy in front for hard limits).

## 20. Important design decisions and alternatives considered

| Decision | Why | Alternative |
|---|---|---|
| DB-backed polling worker thread | State survives restarts, simple to explain, no new infrastructure | `BackgroundTasks` (lost on restart, no recovery); Celery/Redis (overkill) |
| Atomic conditional `UPDATE` as the claim | Works on SQLite without row locks | `SELECT ... FOR UPDATE` (not supported by SQLite) |
| Reserve certificate ids at job creation | Retries are idempotent and leave no orphan files | Generate ids at render time (orphans after a crash) |
| Heartbeat + attempt cap for recovery | Handles crashes without a supervisor, bounds poison jobs | Reset everything at startup (unsafe with several processes) |
| Recipients typed `Any` in the request schema | Lets each recipient be validated alone instead of failing the whole request | `list[RecipientIn]` (one bad item → 422 for all) |
| Invalid recipients stored as rows | Client sees exactly what was rejected and why | Dropping them silently |
| Progress computed from rows | Always consistent with persisted state | Counters on the job row (can drift) |
| Injectable PDF generator, `run_once()` | Tests are deterministic, no sleeps | Thread + `sleep` in tests |
| Non-Latin names rejected as INVALID | Built-in fonts cannot draw them; failing early is clearer than a broken PDF | Bundle a Unicode TTF font |

## 21. Known limitations

* No authentication/authorisation; no rate limiting.
* One process only (section 17); recovery delay up to `STALE_JOB_SECONDS` after a crash.
* Latin script names only (Windows-1252). Names are not transliterated.
* No migrations (Alembic) — schema is created with `create_all`.
* Job results and certificate files are never deleted automatically.
* Timestamps are naive UTC.
* Duplicate detection is per request, not across jobs.
* `Content-Length`-based size limit only. Test client emits an `httpx` deprecation warning from Starlette; harmless.

## 22. Learning outcomes

Designing an async-style API (202 + polling) without a message broker; modelling a job/recipient/result schema with constraints and indexes; making state transitions atomic and idempotent; isolating failures per item; recovering from crashes honestly; defending file serving against path traversal; writing deterministic tests for background work via dependency injection.

## 23. Future scope

Authentication and per-user job ownership; Alembic migrations; Unicode fonts; idempotency keys; cleanup/retention job; ZIP download of a whole job; webhook or email notification on completion; a second worker process with lease-based locking; PostgreSQL for multi-process deployments.

## Project layout

```
app/        main.py config.py database.py models.py schemas.py validation.py dependencies.py
            api/{jobs,certificates}.py  services/{job_service,certificate_service,pdf_generator}.py
            workers/job_worker.py
templates/  certificate_template.py
tests/      conftest.py test_jobs.py test_validation.py test_generation.py test_progress.py
            test_failures.py test_downloads.py
storage/    .gitkeep   (generated PDFs go here; git-ignored)
```
