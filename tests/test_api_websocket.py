import time

import pytest
from starlette.websockets import WebSocketDisconnect

from app.models import BatchStatus, HospitalRow, RowStatus
from app.store import BatchState
from tests.conftest import make_csv, upload

ROWS = [("Hospital A", "1 Main St", "555"), ("Hospital B", "2 Main St", "")]


def test_finished_batch_sends_one_snapshot_then_closes(client, directory):
    batch_id = upload(client, make_csv(ROWS)).json()["batch_id"]

    with client.websocket_connect(f"/hospitals/bulk/{batch_id}/ws") as ws:
        snapshot = ws.receive_json()
        assert snapshot["batch_id"] == batch_id
        assert snapshot["status"] == "completed"
        assert snapshot["processed_hospitals"] == 2
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()
        assert closed.value.code == 1000


def test_running_batch_streams_each_change_until_done(client):
    rows = [HospitalRow(row=1, name="A", address="1 St"), HospitalRow(row=2, name="B", address="2 St")]
    state = BatchState.new("11111111-1111-1111-1111-111111111111", rows)
    state.status = BatchStatus.PROCESSING
    client.app.state.store.add(state)

    with client.websocket_connect(f"/hospitals/bulk/{state.batch_id}/ws") as ws:
        first = ws.receive_json()
        assert (first["status"], first["processed_hospitals"]) == ("processing", 0)

        state.results[1].status = RowStatus.CREATED
        second = ws.receive_json()
        assert second["processed_hospitals"] == 1

        state.results[2].status = RowStatus.CREATED
        for r in state.results.values():
            r.status = RowStatus.CREATED_AND_ACTIVATED
        state.batch_activated = True
        state.status = BatchStatus.COMPLETED
        events = [second]
        deadline = time.time() + 3
        while events[-1]["status"] != "completed" and time.time() < deadline:
            events.append(ws.receive_json())
        assert events[-1]["status"] == "completed" and events[-1]["batch_activated"] is True
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()


def test_unknown_batch_closes_with_4404(client):
    with client.websocket_connect("/hospitals/bulk/00000000-0000-0000-0000-000000000000/ws") as ws:
        assert ws.receive_json() == {"error": "Batch not found"}
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()
        assert closed.value.code == 4404


def test_malformed_batch_id_closes_with_4404(client):
    with client.websocket_connect("/hospitals/bulk/not-a-uuid/ws") as ws:
        assert ws.receive_json()["error"] == "Batch not found"
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()
        assert closed.value.code == 4404


def test_links_point_at_progress_endpoints(client, directory):
    response = upload(client, make_csv(ROWS), **{"async": "true"})
    body = response.json()
    batch_id = body["batch_id"]
    assert response.headers["Location"] == f"/hospitals/bulk/{batch_id}"
    assert body["links"] == {
        "self": f"/hospitals/bulk/{batch_id}",
        "progress_ws": f"/hospitals/bulk/{batch_id}/ws",
        "resume": f"/hospitals/bulk/{batch_id}/resume",
    }


def test_client_disconnecting_mid_stream_is_handled(client):
    rows = [HospitalRow(row=1, name="A", address="1 St")]
    state = BatchState.new("33333333-3333-3333-3333-333333333333", rows)
    state.status = BatchStatus.PROCESSING
    client.app.state.store.add(state)

    with client.websocket_connect(f"/hospitals/bulk/{state.batch_id}/ws") as ws:
        assert ws.receive_json()["status"] == "processing"
    # The client has gone; the next change must not crash the server.
    state.results[1].status = RowStatus.CREATED
    time.sleep(0.5)
    state.status = BatchStatus.PARTIALLY_FAILED
    assert client.get("/").status_code == 200
