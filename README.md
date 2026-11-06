# Hospital Bulk Processing API

A FastAPI service that bulk-creates hospitals from a CSV file through the
[Hospital Directory API](https://hospital-directory.onrender.com/docs).

Each upload becomes a **batch**. Hospitals are created inactive under one batch
ID, and the whole batch is activated in a single call once every row succeeds,
so the directory never shows half an upload as active.

- **Live:** https://paribus-hospital-bulk-8941.onrender.com/docs (interactive Swagger UI).
  It runs on Render's free plan, which sleeps when idle, so the first request may take up to a minute.
- **Stack:** Python 3.12, FastAPI, httpx (async), Pydantic v2, pytest + respx

## Quick start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload            # http://localhost:8000/docs
pytest                                   # 26 tests, no network needed
```

With Docker:

```bash
docker compose up --build                # http://localhost:8000/docs
```

Try it:

```bash
curl -F file=@samples/hospitals.csv http://localhost:8000/hospitals/bulk

# or against the live deployment
curl -F file=@samples/hospitals.csv https://paribus-hospital-bulk-8941.onrender.com/hospitals/bulk
```

## Endpoints

| Method | Path | What it does |
|---|---|---|
| `POST` | `/hospitals/bulk` | Upload a CSV; create every hospital, then activate the batch. `?async=true` returns `202` at once and processes in the background |
| `POST` | `/hospitals/bulk/validate` | Check a CSV and report every problem, without creating anything |
| `GET` | `/hospitals/bulk/{batch_id}` | Progress and per-row results; poll this for async batches |
| `GET` | `/hospitals/bulk` | Recent batches, newest first |
| `POST` | `/hospitals/bulk/{batch_id}/resume` | Retry only the failed rows (or a failed activation), then activate |
| `DELETE` | `/hospitals/bulk/{batch_id}` | Roll back: delete every hospital the batch created |
| `GET` | `/` | Health check |

### CSV format

```csv
name,address,phone
City General Hospital,12 MG Road Bengaluru,080-2222-1111
Green Valley Clinic,88 Hill View Shimla,
```

- `name` and `address` are required; `phone` is optional (the column may be left out).
- Headers are case-insensitive and may be in any order. UTF-8 (with or without BOM), quoted fields and blank lines are handled.
- At most **20 hospitals** and 256 KB per file.

### Response

```json
{
  "batch_id": "c599cd14-d6e3-444d-be9b-bf8e227d4f7d",
  "status": "completed",
  "total_hospitals": 20,
  "processed_hospitals": 20,
  "failed_hospitals": 0,
  "pending_hospitals": 0,
  "processing_time_seconds": 7.255,
  "batch_activated": true,
  "created_at": "2026-11-06T14:31:02.118Z",
  "hospitals": [
    { "row": 1, "hospital_id": 684, "name": "City General Hospital",
      "status": "created_and_activated", "error": null, "attempts": 1 }
  ]
}
```

Batch `status`: `queued` → `processing` → one of `completed`, `partially_failed`
(some rows failed; batch left inactive), `activation_failed` (all rows exist but
the activate call failed), `failed` (no rows could be created), `rolled_back`.

Row `status`: `pending`, `created` (exists, still inactive), `created_and_activated`, `failed`, `rolled_back`.

An invalid CSV gets `422` with every error listed, before any upstream call is made:

```json
{ "valid": false, "total_rows": 3, "max_rows": 20,
  "errors": [ { "row": 2, "field": "name", "message": "name is required" },
              { "row": 3, "field": "address", "message": "address is required" } ] }
```

## Design

```
            CSV upload
                │
     ┌──────────▼──────────┐   invalid → 422 with every error
     │  csv_parser         │──────────────────────────────▶
     └──────────┬──────────┘
                │ rows + new batch UUID
     ┌──────────▼──────────┐        ┌──────────────────────┐
     │  BulkProcessor      │◀──────▶│  BatchStore (memory) │◀── GET progress
     │  semaphore-bounded  │        └──────────────────────┘
     │  asyncio.gather     │
     └──────────┬──────────┘
                │ POST /hospitals/ ×N (parallel, retried)
                │ PATCH /hospitals/batch/{id}/activate (only if all N succeeded)
     ┌──────────▼──────────┐
     │ HospitalDirectory   │  httpx.AsyncClient: pooled connections,
     │ Client              │  timeouts, exponential-backoff retries
     └─────────────────────┘
```

| Module | Responsibility |
|---|---|
| `app/csv_parser.py` | Parse and validate the file; collect all errors in one pass |
| `app/client.py` | Talk to the Hospital Directory API; retry what is worth retrying |
| `app/processor.py` | Run a batch: parallel creates, activate-if-complete, resume, rollback |
| `app/store.py` | In-memory batch state used for progress and resume |
| `app/main.py` | HTTP routes |
| `app/config.py` | Settings from environment variables |

### Decisions and trade-offs

**Parallel creates.** Measured against the live API, each `POST /hospitals/`
takes ~5.3 s regardless of load, and ten simultaneous calls also finish in
~5.3 s. Creating rows one after another would take ~110 s for 20 rows; creating
them concurrently takes **~7 s** (one round trip plus activation). Concurrency
is bounded by an `asyncio.Semaphore` (`MAX_CONCURRENCY`, default 20) so load
on the upstream stays predictable if the row limit is raised.

**All-or-nothing activation.** The batch is activated only when every row was
created. A partial batch stays inactive, so nothing half-uploaded appears as
active. The client then chooses: **resume** (retry failed rows) or **roll back**
(delete the batch upstream).

**Retries.** Network errors, timeouts, `429` and `5xx` are retried with
exponential backoff (`MAX_RETRIES`, default 3). Other `4xx` responses mean the
data is wrong, so they fail immediately and are reported on the row.

**Duplicate-safe resume.** A create can time out *after* the upstream saved
the record. Before retrying, resume reads `GET /hospitals/batch/{id}` and adopts
any record that matches a failed row (name, address, phone) instead of creating
it again.

**Cold starts.** The upstream runs on Render's free tier and sleeps when idle;
the first request can take close to a minute. Each batch first sends one
health-check request with a long timeout, so the parallel creates don't all
time out against a sleeping server.

**Validate before any side effect.** The whole file is validated first; one
bad row rejects the upload with every error listed, rather than creating some
hospitals and failing on others.

**Sync by default, async on request.** `POST /hospitals/bulk` returns the full
result shown in the brief. With `?async=true` it returns `202` right away and
the client polls `GET /hospitals/bulk/{batch_id}`, which suits larger limits or
clients with short HTTP timeouts.

### Limits and what I would change for production

- **State is in memory**, as the brief allows. It is lost on restart, and the
  service must run as one process (the Dockerfile runs a single uvicorn worker).
  For production I would keep batch state in Redis or Postgres and move
  processing to a queue (Celery, RQ or arq), so any instance can serve progress
  and batches survive deploys.
- **Duplicate matching on resume** uses name, address and phone. Two genuinely
  identical rows in one file are handled (each claims one upstream record), but
  an idempotency key on the upstream create would be the robust fix.
- **No authentication**, matching the upstream API. I would add API keys and
  per-client rate limits before exposing this publicly.

## Configuration

All settings are environment variables (see `app/config.py`):

| Variable | Default | |
|---|---|---|
| `HOSPITAL_API_BASE_URL` | `https://hospital-directory.onrender.com` | Upstream API |
| `MAX_CSV_ROWS` | `20` | Maximum hospitals per file |
| `MAX_CSV_BYTES` | `262144` | Maximum upload size |
| `MAX_CONCURRENCY` | `20` | Parallel upstream calls per batch |
| `REQUEST_TIMEOUT_SECONDS` | `30` | Per-request timeout |
| `MAX_RETRIES` | `3` | Retries for transient errors |
| `RETRY_BACKOFF_SECONDS` | `0.5` | Base backoff, doubled each retry |

## Tests

```bash
pytest
```

26 tests, run against a fake Hospital Directory API (respx), covering CSV
validation edge cases, the happy path, retries of transient errors, no retries
of client errors, partial failure, resume (including adopting records saved
despite an error), activation failure, rollback, async mode, and the
concurrency limit.

## Deploying on Render

`render.yaml` defines the service. In Render: **New → Blueprint**, pick this
repository, apply. Or **New → Web Service** with build command
`pip install -r requirements.txt` and start command
`uvicorn app.main:app --host 0.0.0.0 --port $PORT`.
