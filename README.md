# Hospital Bulk Processing API

A FastAPI service that bulk-creates hospitals from a CSV file through the
[Hospital Directory API](https://hospital-directory.onrender.com/docs).

Each upload becomes a **batch**. Hospitals are created inactive under one batch
ID, and the whole batch is activated in a single call once every row succeeds,
so the directory never shows half an upload as active.

[![CI](https://github.com/rahul06030/paribus-hospital-bulk/actions/workflows/ci.yml/badge.svg)](https://github.com/rahul06030/paribus-hospital-bulk/actions/workflows/ci.yml)

- **Live:** https://paribus-hospital-bulk-docker.onrender.com/docs (interactive Swagger UI), built on Render from this repo's `Dockerfile`.
  It runs on Render's free plan, which sleeps when idle, so the first request may take up to a minute.
- **Stack:** Python 3.12 (3.9+ supported, tested in CI), FastAPI, httpx (async), Pydantic v2, pytest + respx

## The brief, point by point

| Brief | Where |
|---|---|
| `POST /hospitals/bulk`, multipart CSV `name,address,phone` (phone optional) | [`app/main.py`](app/main.py), [`app/csv_parser.py`](app/csv_parser.py) |
| Validate the CSV, generate a batch UUID, `POST /hospitals/` per row with the batch ID | [`app/processor.py`](app/processor.py), [`app/client.py`](app/client.py) |
| `PATCH /hospitals/batch/{id}/activate` once all rows are created | `BulkProcessor._finish`; activation never runs for a partial batch |
| Response with `batch_id`, `total_hospitals`, `processed_hospitals`, `failed_hospitals`, `processing_time_seconds`, `batch_activated`, `hospitals[]` (`row`, `hospital_id`, `name`, `status`) | [`app/models.py`](app/models.py): `BulkResult`, same field names |
| Maximum 20 hospitals per CSV | `MAX_CSV_ROWS=20`, rejected with `422` before any upstream call |
| Python 3.8+, FastAPI, in-memory storage | Python 3.9 and 3.12 in CI; [`app/store.py`](app/store.py) |
| Deployed online | Render, built from the [`Dockerfile`](Dockerfile): https://paribus-hospital-bulk-docker.onrender.com/docs |
| ✦ Performance optimization | Parallel creates (20 rows in ~7 s instead of ~110 s), pooled connections, one cold-start warm-up per batch; see [Decisions](#decisions-and-trade-offs) |
| ✦ Progress tracking: WebSocket **or** polling | **Both**: `WS /hospitals/bulk/{id}/ws` streams every change; `GET /hospitals/bulk/{id}` for polling |
| ✦ Resume capability | `POST /hospitals/bulk/{id}/resume`: retries only failed rows, never duplicates |
| ✦ CSV validation endpoint | `POST /hospitals/bulk/validate` |
| ✦ Comprehensive testing | 86 unit/API tests (99% coverage), 33 live checks, 6 failure-path checks against the real upstream; see [Tests](#tests) |
| ✦ Dockerization | [`Dockerfile`](Dockerfile), [`docker-compose.yml`](docker-compose.yml); the live service and CI both build it |
| Git repository + public URL | This repo; the live URL above |

Extras beyond the brief: rollback (`DELETE /hospitals/bulk/{id}`), `Idempotency-Key`
on uploads, a `/health` endpoint that checks the upstream, a batch list, and GitHub Actions CI.

## Quick start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload            # http://localhost:8000/docs
pytest --cov                             # 86 tests, no network needed
```

With Docker:

```bash
docker compose up --build                # http://localhost:8000/docs
```

Try it:

```bash
curl -F file=@samples/hospitals.csv http://localhost:8000/hospitals/bulk

# or against the live deployment
curl -F file=@samples/hospitals.csv https://paribus-hospital-bulk-docker.onrender.com/hospitals/bulk

# async: returns 202 at once; then poll, or stream progress over WebSocket
curl -F file=@samples/hospitals.csv "https://paribus-hospital-bulk-docker.onrender.com/hospitals/bulk?async=true"
curl https://paribus-hospital-bulk-docker.onrender.com/hospitals/bulk/<batch_id>
python -m websockets wss://paribus-hospital-bulk-docker.onrender.com/hospitals/bulk/<batch_id>/ws
```

## Endpoints

| Method | Path | What it does |
|---|---|---|
| `POST` | `/hospitals/bulk` | Upload a CSV; create every hospital, then activate the batch. `?async=true` returns `202` with a `Location` header and processes in the background. Optional `Idempotency-Key` header: repeating the same key and file returns the original batch (`Idempotent-Replayed: true`); the same key with another file gets `409` |
| `POST` | `/hospitals/bulk/validate` | Check a CSV and report every problem, without creating anything |
| `GET` | `/hospitals/bulk/{batch_id}` | Progress and per-row results; poll this for async batches |
| `WS` | `/hospitals/bulk/{batch_id}/ws` | Progress stream: a JSON snapshot on connect and after every change, each with `"final"`; the last one has `"final": true`. An unknown batch gets `{"error": ..., "final": true}`. Stop reading on `final`: the server then closes (1000, or 4404), but Render's proxy doesn't forward a server-initiated close frame |
| `GET` | `/hospitals/bulk` | Recent batches, newest first (`?limit=1..100`) |
| `POST` | `/hospitals/bulk/{batch_id}/resume` | Retry only the failed rows (or a failed activation), then activate. `409` if the batch is running, completed or rolled back |
| `DELETE` | `/hospitals/bulk/{batch_id}` | Roll back: delete every hospital the batch created. `409` while running, `502` if the upstream fails |
| `GET` | `/health` | Readiness: `ok`, or `degraded` when the Hospital Directory API does not answer |
| `GET` | `/` | Liveness |

### CSV format

```csv
name,address,phone
City General Hospital,12 MG Road Bengaluru,080-2222-1111
Green Valley Clinic,88 Hill View Shimla,
```

- `name` and `address` are required; `phone` is optional (the column may be left out).
- Headers are case-insensitive and may be in any order. UTF-8 (with or without BOM), quoted fields and blank lines are handled.
- At most **20 hospitals** and 256 KB per file; fields up to 255 characters.
- Exact duplicate rows (same name, address and phone, ignoring case) are rejected.

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
  ],
  "links": {
    "self": "/hospitals/bulk/c599cd14-...",
    "progress_ws": "/hospitals/bulk/c599cd14-.../ws",
    "resume": "/hospitals/bulk/c599cd14-.../resume"
  }
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
     │  BulkProcessor      │◀──────▶│  BatchStore (memory) │◀── GET / WS progress
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
on the upstream stays predictable if the row limit is raised. A row holds a
slot only while a request is in flight, not while it backs off before a retry,
so one slow row never holds up the others.

**All-or-nothing activation.** The batch is activated only when every row was
created. A partial batch stays inactive, so nothing half-uploaded appears as
active. The client then chooses: **resume** (retry failed rows) or **roll back**
(delete the batch upstream).

**Retries without duplicates.** `POST /hospitals/` is not idempotent: a create
can fail with a 500 or time out *after* the upstream saved the record, and
re-posting it would create a second copy. So errors are split in two:

- *Not processed* (connection refused, `429`, `503`): retried automatically with exponential backoff (`MAX_RETRIES`, default 3).
- *Ambiguous* (`500`, `502`, `504`, read timeout, dropped connection): the processor first reads `GET /hospitals/batch/{id}` and adopts a matching record (same name, address, phone) that no other row has claimed. It posts again only if there is no such record.

Resume runs the same adoption step before retrying failed rows. Because
matching relies on rows being distinct, the validator rejects exact duplicate
rows in a file. Other `4xx` responses mean the data is wrong, so they fail
immediately and are reported on the row. GETs, the activate PATCH and DELETE
are idempotent and are retried on any transient error.

**Cold starts.** The upstream runs on Render's free tier and sleeps when idle;
the first request can take close to a minute. Each batch first sends one
health-check request with a long timeout, so the parallel creates don't all
time out against a sleeping server.

**Validate before any side effect.** The whole file is validated first; one
bad row rejects the upload with every error listed, rather than creating some
hospitals and failing on others.

**Sync by default, async on request.** `POST /hospitals/bulk` returns the full
result shown in the brief. With `?async=true` it returns `202` right away; the
client follows progress by polling `GET /hospitals/bulk/{batch_id}` or over the
WebSocket, which pushes a snapshot whenever a row changes. That suits larger
limits or clients with short HTTP timeouts.

**Safe client retries.** A client that loses the response to an upload can't
tell whether the batch was created. Sending an `Idempotency-Key` makes the
retry safe: the same key and file return the original batch instead of
creating every hospital again.

### Limits and what I would change for production

- **State is in memory**, as the brief allows. It is lost on restart, and the
  service must run as one process (the Dockerfile runs a single uvicorn worker).
  For production I would keep batch state in Redis or Postgres and move
  processing to a queue (Celery, RQ or arq), so any instance can serve progress
  and batches survive deploys.
- **Matching saved records** uses name, address and phone. An idempotency key
  on the upstream create would be the robust fix.
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
| `WARMUP_TIMEOUT_SECONDS` | `90` | Timeout of the one warm-up call per batch |
| `MAX_STORED_BATCHES` | `500` | Finished batches kept in memory |

## Tests

Three layers, all passing:

| Layer | Command | What it covers |
|---|---|---|
| Unit and API tests (86, no network, 99% coverage) | `pytest --cov` | Every endpoint (see below), the client's retry rules, the store, the processor's failure paths |
| Live checks (33) | `python e2e/live_check.py https://paribus-hospital-bulk-docker.onrender.com` | 10 positive flows and 23 negative cases against the deployment and the real upstream, verifying results upstream and deleting what it created |
| Failure paths (6) | `sh e2e/run_failure_checks.sh` | A fault-injecting proxy between the service and the real upstream: partial failure then resume, transient retries, saved-but-errored create, activation failure then resume, every row failing, rollback |

Unit and API tests by endpoint:

| Endpoint / module | Test file | Cases |
|---|---|---|
| `POST /hospitals/bulk` | `test_bulk_api.py` | create and activate; invalid CSV, too many rows, too large; transient retry; no retry on 4xx; saved-but-errored create adopted; partial failure; async 202 |
| `Idempotency-Key` | `test_api_idempotency.py` | replay returns the same batch; other file → 409; no key → new batch; async replay; invalid file not remembered; key too long |
| `POST /hospitals/bulk/validate` | `test_bulk_api.py`, `test_csv_parser.py` | valid file; every CSV rule (columns, BOM, quoting, blank rows, duplicates, sizes, encoding, malformed CSV) |
| `GET /hospitals/bulk`, `GET /hospitals/bulk/{{id}}` | `test_bulk_api.py` | newest first; progress polling; unknown → 404; malformed → 422 |
| `WS /hospitals/bulk/{{id}}/ws` | `test_api_websocket.py` | finished batch; live stream of each change; unknown and malformed → 4404; client disconnects; links and `Location` |
| `POST /hospitals/bulk/{{id}}/resume` | `test_bulk_api.py` | retries only failed rows; adopts saved records; activation failure; completed → 409; running → 409 |
| `DELETE /hospitals/bulk/{{id}}` | `test_bulk_api.py` | deletes upstream; upstream error → 502; running → 409 |
| `GET /`, `GET /health`, `/docs` | `test_api_health.py` | liveness; upstream reachable, unreachable, erroring; OpenAPI lists every endpoint |
| `app/client.py` | `test_client.py` | which errors are retried, which are ambiguous; 404 handling; error details |
| `app/processor.py` | `test_processor.py` | concurrency limit; slot released during backoff; ambiguous retries; crash handling; resume and rollback rules |
| `app/store.py` | `test_store.py` | eviction never drops a running batch; idempotency keys; counts; timing |

The failure-path layer found a real bug during development (blind POST retries
creating duplicates), described with its fix in [PLAN.md](PLAN.md#6-bug-found-by-the-failure-path-tests-fixed).

## Built with Claude Code

Developed with [Claude Code](https://claude.com/claude-code) as a pair programmer:

- [`PLAN.md`](PLAN.md): the plan agreed before coding, upstream measurements, test strategy, the bug log and next steps.
- [`CLAUDE.md`](CLAUDE.md): project rules the agent follows (no duplicates, all-or-nothing activation, validate before side effects, response contract) and the commands it runs.
- [`.claude/agents/`](.claude/agents): a `code-reviewer` subagent that checks changes against those rules, and an `e2e-tester` subagent that runs all three test layers and reports.

## Deploying on Render

The live service (https://paribus-hospital-bulk-docker.onrender.com) uses Render's **Docker** runtime, so it builds
and runs this repo's `Dockerfile`. `render.yaml` describes the same service as a
Blueprint (**New → Blueprint**, pick this repository).

With the Render CLI:

```bash
render services create --name hospital-bulk --type web_service --runtime docker \
  --repo https://github.com/rahul06030/paribus-hospital-bulk --branch main \
  --plan free --health-check-path / --confirm
```
