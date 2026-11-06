# Hospital Bulk Processing API: notes for Claude

FastAPI service that bulk-creates hospitals from a CSV through the external
Hospital Directory API (`https://hospital-directory.onrender.com`, docs at `/docs`).
Read `README.md` for the design and `PLAN.md` for how it was built.

## Layout
- `app/csv_parser.py`: CSV validation. Collects every error in one pass; nothing upstream is called for an invalid file.
- `app/client.py`: async httpx client for the upstream. Owns retries and the safe/ambiguous error split.
- `app/processor.py`: runs a batch (parallel creates, activate-if-complete, resume, rollback, adopting already-saved records).
- `app/store.py`: in-memory batch state; `app/main.py`: routes; `app/config.py`: env settings.
- `tests/`: unit and API tests against a fake upstream (respx). No network.
- `e2e/`: checks against a running deployment and the real upstream.

## Commands
- Setup: `python3.12 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt`
- Unit/API tests: `.venv/bin/python -m pytest -q`
- Run locally: `.venv/bin/uvicorn app.main:app --reload`
- Live checks (positive and negative): `.venv/bin/python e2e/live_check.py https://paribus-hospital-bulk-8941.onrender.com`
- Failure-path checks via fault proxy: `sh e2e/run_failure_checks.sh`

## Rules that must keep holding
- **Never create a duplicate hospital.** `POST /hospitals/` is not idempotent. Retry a create blindly only when the upstream certainly did nothing (connect error, 429, 503). After an ambiguous failure (500/502/504, read timeout), look for the record in `GET /hospitals/batch/{id}` and adopt it before posting again.
- **All-or-nothing activation.** Activate a batch only when every row exists upstream. A partial batch stays inactive until it is resumed or rolled back.
- **Validate before side effects.** A bad CSV must not cause a single upstream call.
- **Keep the response shape from the brief** (`batch_id`, `total_hospitals`, `processed_hospitals`, `failed_hospitals`, `processing_time_seconds`, `batch_activated`, `hospitals[]`). Add fields; never rename or remove these.
- **One process.** Batch state is in memory, so run a single uvicorn worker.
- Upstream facts measured on 2026-11-06: each create takes ~5.3 s and parallel creates do not slow each other; reads take ~0.3 s; the service sleeps when idle (first call up to ~60 s); an empty batch answers 404.

## Testing changes
- Any change to `client.py` or `processor.py` needs a test in `tests/` that drives the fake upstream (`tests/conftest.py: FakeDirectory`) through the failure it handles.
- Before pushing, run pytest; for retry/resume/activation changes also run `e2e/run_failure_checks.sh`.
- e2e scripts create real records upstream and delete them at the end; keep it that way.

## Deploy
Render web service `paribus-hospital-bulk` (free plan, Singapore). Pushing does **not** deploy (Render's GitHub app isn't installed on the repo); deploy explicitly after pushing:
`render deploys create srv-dbmus5rrjlhs73chojkg --commit $(git rev-parse HEAD) --wait --confirm`.
`render deploys list srv-dbmus5rrjlhs73chojkg -o json` shows deploy status and the deployed commit. After a deploy, run `e2e/live_check.py` against the live URL.
