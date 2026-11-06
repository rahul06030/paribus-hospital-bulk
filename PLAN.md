# Implementation plan and log

This assignment was built with Claude Code as a pair programmer. This file is
the plan agreed before writing code, kept up to date as work went on, plus the
decisions and findings along the way. `CLAUDE.md` holds the standing project
rules, and `.claude/agents/` holds the reviewer and end-to-end tester agents used
during development.

## 1. Understand the upstream (done)

Read the Hospital Directory API's OpenAPI schema and probed it directly before
designing anything:

| Finding | How measured | Consequence |
|---|---|---|
| `POST /hospitals/` takes ~5.3 s | timed single calls | sequential processing of 20 rows would take ~110 s |
| 10 parallel creates also finish in ~5.3 s | timed 10 concurrent calls | parallelism is the main performance lever |
| Reads take ~0.3 s | timed `GET /`, `GET /hospitals/batch/{id}` | checking the batch before a retry is cheap |
| Service sleeps when idle; first call up to ~60 s | cold call | warm up once per batch before fanning out |
| Empty batch answers `404` | `PATCH .../activate` on an unknown batch | client treats 404 on batch reads/deletes as "empty" |
| `POST /hospitals/` has no idempotency key | schema | retries must not blindly re-post a create |

## 2. Build the service (done)

- [x] `POST /hospitals/bulk`: validate CSV, new batch UUID, create rows, activate batch, return the brief's response shape
- [x] CSV validation: required columns, header order and case, BOM, CRLF, quoted fields, blank rows, duplicate rows, 20-row limit, 256 KB limit, 255-char fields, UTF-8 only; every error reported at once; no upstream call for a bad file
- [x] Parallel creates with an `asyncio.Semaphore` (`MAX_CONCURRENCY`, default 20 after measuring, see above)
- [x] Retries with exponential backoff for transient errors; no retries for 4xx
- [x] All-or-nothing activation; partial batches stay inactive

## 3. Bonus features (done)

- [x] Progress tracking: `?async=true` returns 202; poll `GET /hospitals/bulk/{batch_id}`
- [x] Resume: `POST /hospitals/bulk/{batch_id}/resume` retries only failed rows, or a failed activation
- [x] CSV validation endpoint: `POST /hospitals/bulk/validate`
- [x] Rollback: `DELETE /hospitals/bulk/{batch_id}` deletes the batch upstream
- [x] Batch list: `GET /hospitals/bulk`
- [x] Dockerfile and docker-compose.yml
- [x] Tests (see section 5)

## 4. Deploy (done)

- [x] Public GitHub repo
- [x] Render web service (free plan, Singapore) created with the Render CLI; deploys are triggered with `render deploys create` after each push
- [x] Live smoke test against the real upstream

## 5. Testing (done)

Three layers:

1. **Unit and API tests** (`tests/`, 86 tests, 99% coverage, no network; one test file per endpoint group plus the client, processor and store). A fake upstream (`FakeDirectory`) can fail on demand: 500s, 503s, 422s, failed activation, and "saved but answered 500".
2. **Live checks** (`e2e/live_check.py`, 33 checks, including health, idempotency and WebSocket checks added in section 7) against the deployed URL and the real upstream: 10 positive flows (sync, 20-row limit, flexible CSV, async with polling, WebSocket stream, idempotent replay, upstream health, list, validate, rollback) and 23 negative cases (unknown batch on the WebSocket, empty, header-only, missing, unknown and duplicate columns, extra cells, duplicate rows, more than 20 rows, more than 256 KB, non-UTF-8, binary, field too long, every error listed at once, nothing created upstream for a bad file, unknown or malformed batch IDs, resume of a completed or rolled-back batch, resume or delete while processing, bad list limits). Each run verifies results on the upstream and deletes what it created.
3. **Failure paths against the real upstream** (`e2e/run_failure_checks.sh`, 6 checks). A small fault-injecting proxy sits between the service and the real upstream: partial failure then resume, transient errors retried, saved-but-errored create adopted, activation failure then resume, every row failing, rollback of a partial batch.

## 6. Bug found by the failure-path tests (fixed)

**Symptom:** with the proxy saving a hospital upstream and then answering 500,
the batch ended up with extra copies of that hospital.

**Cause:** the client retried every 5xx, including on `POST /hospitals/`. If
the upstream saved the record before failing, each retry created another copy,
and resume only adopted one of them.

**Fix:**
- The client now separates errors where the upstream certainly did nothing (connect error, 429, 503) from ambiguous ones (500, 502, 504, read timeout, dropped connection). Creates are retried automatically only after the former.
- After an ambiguous failure, the processor reads `GET /hospitals/batch/{id}` and adopts a matching record (same name, address and phone) that no other row has claimed. It posts again only when no record matches.
- Matching only works if no two rows in a file are identical, so the CSV validator now rejects exact duplicate rows (case-insensitive name, address and phone) with "Duplicate of row N".
- Regression tests: `test_ambiguous_create_failure_does_not_create_a_duplicate`, `test_ambiguous_failure_without_a_saved_record_is_retried`, `test_identical_rows_are_rejected`, plus the "Saved-but-errored create not duplicated" failure-path check.

## 7. Second round: review against other submissions (done)

Over 30 candidates have published this same assignment on GitHub. Reading the
strongest (their READMEs and file trees) showed four things they had that this
repo didn't, plus one flaw in our own code:

- [x] **WebSocket progress.** The brief says "WebSocket or polling"; we now offer both: `WS /hospitals/bulk/{id}/ws` pushes a snapshot on every change and closes with 1000 when done, 4404 for an unknown batch. Testing it on Render showed that Render's Cloudflare proxy does not pass on a close frame sent by the server (clients saw 1006 after a 10 s wait), so every message now carries `"final"` and clients stop on the final one instead of relying on the close code.
- [x] **`Idempotency-Key` on uploads**, so a client re-sending an upload whose response it lost gets the original batch rather than a second copy of every hospital.
- [x] **Upstream-aware health check** (`GET /health` → `ok` or `degraded`), plus a `Location` header and `links` on batch results.
- [x] **CI and coverage**: GitHub Actions runs pytest on Python 3.9 and 3.12 (failing under 95% coverage) and builds and starts the Docker image.
- [x] **Flaw fixed:** a row held its concurrency slot while sleeping between retries, so one retrying row could stall the others. It now holds a slot only while a request is in flight (`test_a_row_backing_off_does_not_hold_a_concurrency_slot`).
- [x] **Docker in production:** a second Render service builds from the `Dockerfile`; it is now the submitted URL.

No public reviews of Paribus's evaluation were found, so the brief's rubric is
the guide; the README opens with a table mapping each point of the brief to
where it is implemented.

## 8. Known limits and next steps

- State is in memory and lives in one process. Next step: Redis or Postgres for batch state, and a worker queue (arq, RQ or Celery) for processing, so batches survive restarts and the service can scale out.
- Matching a saved record by name, address and phone is a stand-in for an idempotency key on the upstream create, which would be the robust fix. It relies on rows in a file being distinct, which the validator enforces.
- No authentication or rate limiting, matching the upstream. Add API keys and per-client limits before real use.
