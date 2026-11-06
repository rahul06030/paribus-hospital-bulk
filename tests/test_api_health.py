import httpx


def test_liveness(client):
    assert client.get("/").json() == {"status": "ok"}


def test_health_reports_reachable_upstream(client):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["upstream"]["reachable"] is True
    assert isinstance(body["upstream"]["latency_ms"], int)


def test_health_reports_unreachable_upstream(client, directory):
    directory.router["health"].mock(side_effect=httpx.ConnectError("refused"))
    body = client.get("/health").json()
    assert body == {"status": "degraded", "upstream": {"reachable": False, "error": "ConnectError"}}


def test_health_reports_upstream_error_status(client, directory):
    directory.router["health"].respond(503)
    body = client.get("/health").json()
    assert body["status"] == "degraded"
    assert body["upstream"]["error"] == "HTTPStatusError"


def test_openapi_documents_every_endpoint(client):
    paths = client.get("/openapi.json").json()["paths"]
    assert set(paths) >= {
        "/",
        "/health",
        "/hospitals/bulk",
        "/hospitals/bulk/validate",
        "/hospitals/bulk/{batch_id}",
        "/hospitals/bulk/{batch_id}/resume",
    }
    assert set(paths["/hospitals/bulk"]) == {"get", "post"}
    assert set(paths["/hospitals/bulk/{batch_id}"]) == {"get", "delete"}
    assert client.get("/docs").status_code == 200
