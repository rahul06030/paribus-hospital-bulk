"""Forwards to the real Hospital Directory API, injecting faults set via /_faults."""
import json

import httpx
from fastapi import FastAPI, Request, Response

UPSTREAM = "https://hospital-directory.onrender.com"
app = FastAPI()
client = httpx.AsyncClient(base_url=UPSTREAM, timeout=60)
faults = {"fail_names": [], "flaky": {}, "save_then_fail": [], "fail_activate": False}
log = []


@app.post("/_faults")
async def set_faults(body: dict):
    faults.update(body)
    return faults


@app.get("/_log")
async def get_log():
    return log


@app.api_route("/{path:path}", methods=["GET", "POST", "PATCH", "DELETE"])
async def proxy(path: str, request: Request):
    body = await request.body()
    name = json.loads(body).get("name") if request.method == "POST" and body else None
    log.append({"method": request.method, "path": "/" + path, "name": name})

    if name in faults["fail_names"]:
        return Response(json.dumps({"detail": "injected 500"}), 500, media_type="application/json")
    if faults["flaky"].get(name, 0) > 0:
        faults["flaky"][name] -= 1
        return Response(json.dumps({"detail": "injected 503"}), 503, media_type="application/json")
    if request.method == "PATCH" and faults["fail_activate"]:
        return Response(json.dumps({"detail": "injected 502"}), 502, media_type="application/json")

    upstream = await client.request(
        request.method, "/" + path, content=body, headers={"content-type": "application/json"}
    )
    if name in faults["save_then_fail"]:
        # The record is saved upstream, but the caller sees an error.
        return Response(json.dumps({"detail": "injected 500 after save"}), 500, media_type="application/json")
    return Response(upstream.content, upstream.status_code, media_type="application/json")
