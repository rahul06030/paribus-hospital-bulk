"""End-to-end checks against a running deployment. Usage: python live_check.py BASE_URL"""
import asyncio
import json
import sys
import time
import uuid

import httpx
import websockets

BASE = sys.argv[1].rstrip("/")
UPSTREAM = "https://hospital-directory.onrender.com"
c = httpx.Client(base_url=BASE, timeout=120)
results = []
batches = []


def csv(*lines):
    return ("\n".join(lines) + "\n").encode()


def post_file(path, content, params=None, filename="h.csv"):
    return c.post(path, files={"file": (filename, content, "text/csv")}, params=params)


def check(kind, name, fn):
    try:
        detail = fn()
        results.append((kind, name, "PASS", detail or ""))
    except AssertionError as e:
        results.append((kind, name, "FAIL", str(e)))
    except Exception as e:
        results.append((kind, name, "ERROR", repr(e)[:150]))


def expect(r, code):
    assert r.status_code == code, f"expected {code}, got {r.status_code}: {r.text[:200]}"
    return r.json() if r.content else None


GOOD = csv(
    "name,address,phone",
    "QA Alpha Hospital,1 Test Road,555-0001",
    'QA "Beta" Clinic,"2 Test Road, Suite 4",',
    "QA Gamma Centre,3 Test Road,555-0003",
)

# ---------- positive ----------

def p_health():
    assert expect(c.get("/"), 200) == {"status": "ok"}
    assert c.get("/docs").status_code == 200
    assert "/hospitals/bulk" in c.get("/openapi.json").json()["paths"]
    return "/, /docs, /openapi.json"


def p_validate_good():
    d = expect(post_file("/hospitals/bulk/validate", GOOD), 200)
    assert d["valid"] and d["total_rows"] == 3
    assert d["rows"][1]["phone"] is None and d["rows"][1]["address"] == "2 Test Road, Suite 4"
    return "valid=true, 3 rows, quoted comma kept, empty phone -> null"


def p_bulk_sync():
    d = expect(post_file("/hospitals/bulk", GOOD), 200)
    batches.append(d["batch_id"])
    assert d["status"] == "completed" and d["batch_activated"] is True
    assert (d["total_hospitals"], d["processed_hospitals"], d["failed_hospitals"]) == (3, 3, 0)
    assert [h["row"] for h in d["hospitals"]] == [1, 2, 3]
    assert all(h["status"] == "created_and_activated" and h["hospital_id"] for h in d["hospitals"])
    up = httpx.get(f"{UPSTREAM}/hospitals/batch/{d['batch_id']}", timeout=60).json()
    assert len(up) == 3 and all(h["active"] for h in up), "upstream not all active"
    ids = {h["hospital_id"] for h in d["hospitals"]}
    assert ids == {h["id"] for h in up}, "hospital ids differ from upstream"
    return f"3/3 created_and_activated in {d['processing_time_seconds']}s; upstream active, ids match"


def p_max_rows():
    lines = ["name,address,phone"] + [f"QA Max {i},{i} Max Road,555-{i:04d}" for i in range(1, 21)]
    d = expect(post_file("/hospitals/bulk", csv(*lines)), 200)
    batches.append(d["batch_id"])
    assert d["processed_hospitals"] == 20 and d["batch_activated"]
    return f"20/20 in {d['processing_time_seconds']}s"


def p_flexible_header():
    content = "﻿Address,NAME\r\n9 Order Road,QA Reordered\r\n\r\n".encode()
    d = expect(post_file("/hospitals/bulk", content), 200)
    batches.append(d["batch_id"])
    assert d["processed_hospitals"] == 1 and d["hospitals"][0]["name"] == "QA Reordered"
    return "BOM, CRLF, reordered/uppercase headers, no phone column, blank line"


def p_async_poll():
    r = post_file("/hospitals/bulk", GOOD, params={"async": "true"})
    d = expect(r, 202)
    batches.append(d["batch_id"])
    assert d["status"] == "queued" and d["pending_hospitals"] == 3
    seen = []
    for _ in range(30):
        s = expect(c.get(f"/hospitals/bulk/{d['batch_id']}"), 200)
        seen.append(s["status"])
        if s["status"] not in ("queued", "processing"):
            break
        time.sleep(1)
    assert s["status"] == "completed" and s["batch_activated"], s["status"]
    return "202 queued -> " + " -> ".join(dict.fromkeys(seen))


def p_list():
    d = expect(c.get("/hospitals/bulk", params={"limit": 5}), 200)
    assert d[0]["batch_id"] == batches[-1], "newest batch not first"
    assert len(d) <= 5
    return f"{len(d)} batches, newest first"


def p_rollback():
    bid = batches[0]
    d = expect(c.delete(f"/hospitals/bulk/{bid}"), 200)
    assert d["status"] == "rolled_back" and all(h["status"] == "rolled_back" for h in d["hospitals"])
    up = httpx.get(f"{UPSTREAM}/hospitals/batch/{bid}", timeout=60)
    assert up.status_code == 404, f"upstream still has the batch ({up.status_code})"
    again = expect(c.delete(f"/hospitals/bulk/{bid}"), 200)
    assert again["status"] == "rolled_back"
    return "batch gone upstream; second DELETE is a no-op"


def p_health_upstream():
    d = expect(c.get("/health"), 200)
    assert d["status"] == "ok" and d["upstream"]["reachable"], d
    return f"upstream reachable, {d['upstream']['latency_ms']} ms"


def p_idempotency():
    key = f"live-{uuid.uuid4()}"
    hdr = {"Idempotency-Key": key}
    first = c.post("/hospitals/bulk", files={"file": ("h.csv", GOOD, "text/csv")}, headers=hdr)
    d1 = expect(first, 200)
    batches.append(d1["batch_id"])
    again = c.post("/hospitals/bulk", files={"file": ("h.csv", GOOD, "text/csv")}, headers=hdr)
    d2 = expect(again, 200)
    assert d2["batch_id"] == d1["batch_id"] and again.headers.get("idempotent-replayed") == "true"
    up = httpx.get(f"{UPSTREAM}/hospitals/batch/{d1['batch_id']}", timeout=60).json()
    assert len(up) == 3, f"{len(up)} upstream records after a replay"
    other = c.post("/hospitals/bulk", files={"file": ("h.csv", csv("name,address", "X,Y"), "text/csv")}, headers=hdr)
    expect(other, 409)
    return "replay returns the same batch (3 records upstream, not 6); same key + other file -> 409"


def p_websocket():
    r = post_file("/hospitals/bulk", GOOD, params={"async": "true"})
    d = expect(r, 202)
    batches.append(d["batch_id"])
    assert r.headers["location"] == f"/hospitals/bulk/{d['batch_id']}"
    ws_url = BASE.replace("https://", "wss://").replace("http://", "ws://") + d["links"]["progress_ws"]

    async def stream():
        events = []
        async with websockets.connect(ws_url, open_timeout=60) as ws:
            async for message in ws:
                events.append(json.loads(message))
            return events, ws.close_code

    events, code = asyncio.run(stream())
    statuses = [e["status"] for e in events]
    assert statuses[-1] == "completed" and events[-1]["batch_activated"], statuses
    assert code == 1000, code
    return f"{len(events)} events: " + " -> ".join(dict.fromkeys(statuses)) + "; server closed 1000"


def n_websocket_unknown():
    url = BASE.replace("https://", "wss://").replace("http://", "ws://") + "/hospitals/bulk/00000000-0000-0000-0000-000000000000/ws"

    async def run():
        async with websockets.connect(url, open_timeout=60) as ws:
            msg = json.loads(await ws.recv())
            try:
                await ws.recv()
            except websockets.ConnectionClosed:
                pass
            return msg, ws.close_code

    msg, code = asyncio.run(run())
    assert msg == {"error": "Batch not found"} and code == 4404, (msg, code)
    return "error message, then close code 4404"


# ---------- negative ----------

def n(path_params, content, code, must_contain, filename="h.csv", path="/hospitals/bulk"):
    def run():
        r = post_file(path, content, params=path_params, filename=filename)
        expect(r, code)
        assert must_contain.lower() in r.text.lower(), f"missing '{must_contain}' in {r.text[:200]}"
        return f"{code}: {must_contain}"
    return run


def n_no_file():
    r = c.post("/hospitals/bulk")
    expect(r, 422)
    return "422: file required"


def n_too_many():
    lines = ["name,address"] + [f"H{i},A{i}" for i in range(25)]
    r = post_file("/hospitals/bulk", csv(*lines))
    d = expect(r, 422)
    assert "25 hospitals; the maximum is 20" in d["errors"][0]["message"]
    return "422: 25 hospitals; the maximum is 20"


def n_all_errors_listed():
    d = expect(post_file("/hospitals/bulk", csv("name,address,phone", ",1 Rd,", "B,,", "C,3 Rd,", ",,555", ",,")), 422)  # ",," is an empty spreadsheet row: skipped
    got = [(e["row"], e["field"]) for e in d["errors"]]
    assert got == [(1, "name"), (2, "address"), (4, "name"), (4, "address")], got
    return f"all 4 errors in one response: {got}"


def n_no_side_effect():
    before = len(httpx.get(f"{UPSTREAM}/hospitals/", timeout=60).json())
    post_file("/hospitals/bulk", csv("name,address", "OK,1 Rd", ",2 Rd"))
    after = len(httpx.get(f"{UPSTREAM}/hospitals/", timeout=60).json())
    assert before == after, f"upstream count changed {before} -> {after}"
    return "upstream hospital count unchanged after a rejected file"


def n_long_field():
    d = expect(post_file("/hospitals/bulk", csv("name,address", "x" * 300 + ",1 Rd")), 422)
    assert "exceeds 255" in d["errors"][0]["message"]
    return "422: name exceeds 255 characters"


def n_unknown_batch():
    expect(c.get("/hospitals/bulk/00000000-0000-0000-0000-000000000000"), 404)
    expect(c.post("/hospitals/bulk/00000000-0000-0000-0000-000000000000/resume"), 404)
    expect(c.delete("/hospitals/bulk/00000000-0000-0000-0000-000000000000"), 404)
    expect(c.get("/hospitals/bulk/not-a-uuid"), 422)
    return "GET/resume/DELETE unknown -> 404; malformed id -> 422"


def n_resume_completed():
    d = expect(c.post(f"/hospitals/bulk/{batches[1]}/resume"), 409)
    assert "nothing to resume" in d["detail"]
    return "409: nothing to resume"


def n_resume_rolled_back():
    expect(c.post(f"/hospitals/bulk/{batches[0]}/resume"), 409)
    return "409 for a rolled-back batch"


def n_resume_while_running():
    d = expect(post_file("/hospitals/bulk", GOOD, params={"async": "true"}), 202)
    batches.append(d["batch_id"])
    r1 = c.post(f"/hospitals/bulk/{d['batch_id']}/resume")
    r2 = c.delete(f"/hospitals/bulk/{d['batch_id']}")
    assert r1.status_code == 409 and r2.status_code == 409, (r1.status_code, r2.status_code)
    for _ in range(30):
        if c.get(f"/hospitals/bulk/{d['batch_id']}").json()["status"] == "completed":
            break
        time.sleep(1)
    return "resume and DELETE during processing -> 409"


def n_bad_limit():
    expect(c.get("/hospitals/bulk", params={"limit": 0}), 422)
    expect(c.get("/hospitals/bulk", params={"limit": 101}), 422)
    return "limit=0 and limit=101 -> 422"


checks = [
    ("positive", "Health, Swagger docs, OpenAPI", p_health),
    ("positive", "Validate endpoint, valid CSV", p_validate_good),
    ("positive", "Bulk create (sync), verified upstream", p_bulk_sync),
    ("positive", "Bulk create, 20 rows (the limit)", p_max_rows),
    ("positive", "Flexible CSV format", p_flexible_header),
    ("positive", "Async mode + progress polling", p_async_poll),
    ("positive", "List batches", p_list),
    ("positive", "Health incl. upstream check", p_health_upstream),
    ("positive", "Idempotency-Key replay", p_idempotency),
    ("positive", "WebSocket progress stream", p_websocket),
    ("negative", "WebSocket for unknown batch", n_websocket_unknown),
    ("negative", "No file uploaded", n_no_file),
    ("negative", "Empty file", n(None, b"", 422, "CSV file is empty")),
    ("negative", "Header only", n(None, b"name,address\n", 422, "no data rows")),
    ("negative", "Missing address column", n(None, csv("name,phone", "A,1"), 422, "Missing required column(s): address")),
    ("negative", "Unknown column", n(None, csv("name,address,email", "A,B,c@d"), 422, "Unknown column(s): email")),
    ("negative", "Duplicate column", n(None, csv("name,address,name", "A,B,C"), 422, "Duplicate column")),
    ("negative", "Row with extra columns", n(None, csv("name,address", "A,B,C"), 422, "Expected 2 columns")),
    ("negative", "More than 20 rows", n_too_many),
    ("negative", "Every row error reported at once", n_all_errors_listed),
    ("negative", "Rejected file creates nothing upstream", n_no_side_effect),
    ("negative", "Field longer than 255 chars", n_long_field),
    ("negative", "Duplicate rows in one file", n(None, csv("name,address", "A,1 Rd", "a,1 rd"), 422, "Duplicate of row 1")),
    ("negative", "Not UTF-8", n(None, "name,address\nCaf\xe9,1 Rd\n".encode("latin-1"), 422, "UTF-8")),
    ("negative", "Binary file", n(None, b"\x89PNG\r\n\x1a\n\x00\x00\xff\xfe", 422, "UTF-8", filename="x.png")),
    ("negative", "File over 256 KB", n(None, b"name,address\n" + b"x" * 300_000, 413, "exceeds")),
    ("negative", "Validate endpoint, bad CSV", n(None, csv("name,address", ",1 Rd"), 200, '"valid":false', path="/hospitals/bulk/validate")),
    ("negative", "Unknown / malformed batch id", n_unknown_batch),
    ("negative", "Resume a completed batch", n_resume_completed),
    ("negative", "Resume/delete while processing", n_resume_while_running),
    ("negative", "Bad list limit", n_bad_limit),
    ("positive", "Rollback (DELETE) a batch", p_rollback),
    ("negative", "Resume a rolled-back batch", n_resume_rolled_back),
]

for kind, name, fn in checks:
    check(kind, name, fn)

# clean up everything this run created
for bid in batches:
    c.delete(f"/hospitals/bulk/{bid}")

w = max(len(r[1]) for r in results)
for kind, name, status, detail in results:
    print(f"{status:5}  {kind:8}  {name:{w}}  {detail}")
fails = [r for r in results if r[2] != "PASS"]
print(f"\n{len(results) - len(fails)}/{len(results)} passed; cleaned up {len(batches)} batches")
sys.exit(1 if fails else 0)
