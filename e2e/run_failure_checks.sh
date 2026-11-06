#!/usr/bin/env sh
# Failure-path checks against the real Hospital Directory API:
#   app (:8801) -> fault proxy (:8802) -> https://hospital-directory.onrender.com
# The proxy injects 500/503/502 responses on demand so partial failure, retry,
# duplicate-safe adoption, activation failure and rollback can be exercised.
set -e
cd "$(dirname "$0")/.."
PY=${PYTHON:-.venv/bin/python}

$PY -m uvicorn --app-dir e2e fault_proxy:app --port 8802 --log-level warning &
PROXY=$!
HOSPITAL_API_BASE_URL=http://127.0.0.1:8802 MAX_RETRIES=2 RETRY_BACKOFF_SECONDS=0.1 \
  $PY -m uvicorn app.main:app --port 8801 --log-level warning &
APP=$!
trap 'kill $PROXY $APP 2>/dev/null' EXIT
sleep 3

$PY e2e/failure_check.py
