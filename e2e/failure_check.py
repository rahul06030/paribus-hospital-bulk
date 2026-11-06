"""Failure-path checks: app at :8801 -> fault proxy at :8802 -> real Hospital Directory API."""
import sys

import httpx

APP, PROXY, UPSTREAM = "http://127.0.0.1:8801", "http://127.0.0.1:8802", "https://hospital-directory.onrender.com"
c = httpx.Client(base_url=APP, timeout=120)
results, batches = [], []

ROWS = ["QA F1 Hospital", "QA F2 Hospital", "QA F3 Hospital"]
CSV = ("name,address,phone\n" + "\n".join(f"{n},{i} Fault Rd,555-{i}" for i, n in enumerate(ROWS, 1)) + "\n").encode()


def faults(**kw):
    base = {"fail_names": [], "flaky": {}, "save_then_fail": [], "fail_activate": False}
    httpx.post(f"{PROXY}/_faults", json={**base, **kw})


def creates():
    return sum(1 for e in httpx.get(f"{PROXY}/_log").json() if e["method"] == "POST")


def upstream(bid):
    r = httpx.get(f"{UPSTREAM}/hospitals/batch/{bid}", timeout=60)
    return [] if r.status_code == 404 else r.json()


def upload():
    d = c.post("/hospitals/bulk", files={"file": ("h.csv", CSV, "text/csv")}).json()
    batches.append(d["batch_id"])
    return d


def check(name, fn):
    try:
        results.append(("PASS", name, fn()))
    except AssertionError as e:
        results.append(("FAIL", name, str(e)))
    except Exception as e:
        results.append(("ERROR", name, repr(e)[:200]))


def partial_then_resume():
    faults(fail_names=["QA F2 Hospital"])
    d = upload()
    assert d["status"] == "partially_failed", d["status"]
    assert (d["processed_hospitals"], d["failed_hospitals"], d["batch_activated"]) == (2, 1, False)
    assert d["hospitals"][1]["status"] == "failed" and "500" in d["hospitals"][1]["error"]
    up = upstream(d["batch_id"])
    assert len(up) == 2 and not any(h["active"] for h in up), "partial batch must stay inactive"
    first = f"partially_failed 2/3, row 2 error shown, upstream 2 inactive"

    faults()
    n = creates()
    r = c.post(f"/hospitals/bulk/{d['batch_id']}/resume").json()
    assert r["status"] == "completed" and r["batch_activated"], r["status"]
    assert creates() - n == 1, f"resume made {creates() - n} creates, expected 1"
    up = upstream(d["batch_id"])
    assert len(up) == 3 and all(h["active"] for h in up)
    return first + "; resume -> 1 new create, completed, upstream 3 active"


def retry_transient():
    faults(flaky={"QA F1 Hospital": 2})
    n = creates()
    d = upload()
    assert d["status"] == "completed", d["status"]
    assert creates() - n == 5, f"{creates() - n} creates for 3 rows with 2 injected 503s"
    return "two 503s on row 1 retried automatically; completed (5 POSTs for 3 rows)"


def save_then_fail_no_duplicate():
    faults(save_then_fail=["QA F3 Hospital"])
    n = creates()
    d = upload()
    up = upstream(d["batch_id"])
    assert d["status"] == "completed", d["status"]
    assert creates() - n == 3, f"{creates() - n} POSTs for 3 rows (re-created after the 500?)"
    assert len(up) == 3 and all(h["active"] for h in up), f"{len(up)} upstream records (duplicate?)"
    assert d["hospitals"][2]["hospital_id"] in {h["id"] for h in up}
    return "row 3 saved upstream but answered 500; adopted, not re-posted: 3 POSTs, 3 records, completed"


def activation_failure():
    faults(fail_activate=True)
    d = upload()
    assert d["status"] == "activation_failed" and d["processed_hospitals"] == 3 and not d["batch_activated"]
    assert all(h["status"] == "created" for h in d["hospitals"])

    faults()
    n = creates()
    r = c.post(f"/hospitals/bulk/{d['batch_id']}/resume").json()
    assert r["status"] == "completed" and creates() == n
    assert all(h["active"] for h in upstream(d["batch_id"]))
    return "activate 502 -> activation_failed, rows 'created'; resume only re-activates"


def all_fail():
    faults(fail_names=ROWS)
    d = upload()
    assert d["status"] == "failed" and d["failed_hospitals"] == 3, d["status"]
    assert upstream(d["batch_id"]) == []
    return "every row 500 -> status failed, nothing upstream"


def rollback_partial():
    faults(fail_names=["QA F1 Hospital"])
    d = upload()
    assert len(upstream(d["batch_id"])) == 2
    faults()
    r = c.delete(f"/hospitals/bulk/{d['batch_id']}").json()
    assert r["status"] == "rolled_back" and upstream(d["batch_id"]) == []
    return "partial batch rolled back; upstream batch empty"


for name, fn in [
    ("Partial failure, then resume", partial_then_resume),
    ("Transient errors retried", retry_transient),
    ("Saved-but-errored create not duplicated", save_then_fail_no_duplicate),
    ("Activation failure, then resume", activation_failure),
    ("Every row fails", all_fail),
    ("Rollback a partial batch", rollback_partial),
]:
    check(name, fn)

faults()
for bid in batches:
    c.delete(f"/hospitals/bulk/{bid}")

for status, name, detail in results:
    print(f"{status:5}  {name:40}  {detail}")
fails = [r for r in results if r[0] != "PASS"]
print(f"\n{len(results) - len(fails)}/{len(results)} passed; cleaned up {len(batches)} batches")
sys.exit(1 if fails else 0)
