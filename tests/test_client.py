"""HospitalDirectoryClient: which failures are retried, and which are flagged ambiguous."""

import httpx
import pytest
import respx

from app.client import HospitalAPIError, HospitalDirectoryClient
from app.config import Settings
from app.models import HospitalRow

BASE = "https://upstream.test"
ROW = HospitalRow(row=1, name="A", address="1 St", phone=None)
BATCH = "11111111-1111-1111-1111-111111111111"


@pytest.fixture
async def client():
    c = HospitalDirectoryClient(Settings(hospital_api_base_url=BASE, max_retries=2, retry_backoff_seconds=0))
    yield c
    await c.aclose()


@pytest.fixture
def api():
    with respx.mock(base_url=BASE) as mock:
        yield mock


async def test_create_sends_the_batch_id(client, api):
    route = api.post("/hospitals/").respond(200, json={"id": 7})
    assert await client.create_hospital(ROW, BATCH) == {"id": 7}
    sent = route.calls.last.request
    assert b'"creation_batch_id":"11111111-1111-1111-1111-111111111111"' in sent.content.replace(b" ", b"")


@pytest.mark.parametrize("status", [429, 503])
async def test_create_retries_statuses_that_mean_not_processed(client, api, status):
    route = api.post("/hospitals/").mock(
        side_effect=[httpx.Response(status), httpx.Response(status), httpx.Response(200, json={"id": 1})]
    )
    assert await client.create_hospital(ROW, BATCH) == {"id": 1}
    assert route.call_count == 3


async def test_create_retries_connection_errors(client, api):
    route = api.post("/hospitals/").mock(
        side_effect=[httpx.ConnectError("refused"), httpx.Response(200, json={"id": 1})]
    )
    assert await client.create_hospital(ROW, BATCH) == {"id": 1}
    assert route.call_count == 2


@pytest.mark.parametrize("status", [500, 502, 504])
async def test_create_does_not_retry_ambiguous_statuses(client, api, status):
    route = api.post("/hospitals/").respond(status, json={"detail": "boom"})
    with pytest.raises(HospitalAPIError) as err:
        await client.create_hospital(ROW, BATCH)
    assert route.call_count == 1
    assert err.value.ambiguous and err.value.status_code == status
    assert "boom" in str(err.value)


async def test_create_does_not_retry_a_read_timeout(client, api):
    route = api.post("/hospitals/").mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(HospitalAPIError) as err:
        await client.create_hospital(ROW, BATCH)
    assert route.call_count == 1
    assert err.value.ambiguous
    assert "Timed out" in str(err.value)


async def test_client_errors_fail_at_once_and_are_not_ambiguous(client, api):
    route = api.post("/hospitals/").respond(422, json={"detail": [{"msg": "field required"}]})
    with pytest.raises(HospitalAPIError) as err:
        await client.create_hospital(ROW, BATCH)
    assert route.call_count == 1
    assert not err.value.ambiguous and not err.value.retryable


async def test_retries_give_up_after_max_retries(client, api):
    route = api.post("/hospitals/").respond(503)
    with pytest.raises(HospitalAPIError) as err:
        await client.create_hospital(ROW, BATCH)
    assert route.call_count == 3  # 1 + max_retries
    assert not err.value.ambiguous


async def test_idempotent_calls_retry_any_transient_error(client, api):
    route = api.patch(f"/hospitals/batch/{BATCH}/activate").mock(
        side_effect=[httpx.Response(500), httpx.ReadTimeout("slow"), httpx.Response(200, json={"ok": True})]
    )
    assert await client.activate_batch(BATCH) == {"ok": True}
    assert route.call_count == 3


async def test_get_batch_treats_404_as_empty(client, api):
    api.get(f"/hospitals/batch/{BATCH}").respond(404, json={"detail": "No hospitals found"})
    assert await client.get_batch(BATCH) == []


async def test_get_batch_raises_other_errors(client, api):
    api.get(f"/hospitals/batch/{BATCH}").respond(400, json={"detail": "bad"})
    with pytest.raises(HospitalAPIError):
        await client.get_batch(BATCH)


async def test_delete_batch_treats_404_as_already_gone(client, api):
    api.delete(f"/hospitals/batch/{BATCH}").respond(404)
    assert await client.delete_batch(BATCH) is None


async def test_delete_batch_raises_other_errors(client, api):
    api.delete(f"/hospitals/batch/{BATCH}").respond(400, text="nope")
    with pytest.raises(HospitalAPIError) as err:
        await client.delete_batch(BATCH)
    assert "nope" in str(err.value)  # non-JSON error bodies are reported as text


async def test_empty_success_body_returns_none(client, api):
    api.delete(f"/hospitals/batch/{BATCH}").respond(204)
    assert await client.delete_batch(BATCH) is None


async def test_warm_up_never_raises(client, api):
    api.get("/").mock(side_effect=httpx.ConnectError("asleep"))
    await client.warm_up()


async def test_ping_returns_latency(client, api):
    api.get("/").respond(200, json={"status": "ok"})
    assert await client.ping(timeout=1) >= 0


async def test_error_detail_without_detail_key(client, api):
    api.post("/hospitals/").respond(400, json={"message": "odd"})
    with pytest.raises(HospitalAPIError) as err:
        await client.create_hospital(ROW, BATCH)
    assert "odd" in str(err.value)
