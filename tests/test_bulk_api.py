from tests.conftest import make_csv, upload

ROWS = [(f"Hospital {i}", f"{i} Main St", f"555-000{i}") for i in range(1, 6)]


def test_bulk_create_creates_and_activates_every_hospital(client, directory):
    response = upload(client, make_csv(ROWS))

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "completed"
    assert body["total_hospitals"] == 5
    assert body["processed_hospitals"] == 5
    assert body["failed_hospitals"] == 0
    assert body["batch_activated"] is True
    assert [h["row"] for h in body["hospitals"]] == [1, 2, 3, 4, 5]
    assert all(h["status"] == "created_and_activated" for h in body["hospitals"])
    assert {h["hospital_id"] for h in body["hospitals"]} == set(directory.hospitals)
    assert all(h["active"] and h["creation_batch_id"] == body["batch_id"] for h in directory.hospitals.values())


def test_invalid_csv_is_rejected_before_any_upstream_call(client, directory):
    response = upload(client, make_csv([("A", "")]))

    assert response.status_code == 422
    assert response.json()["errors"][0]["field"] == "address"
    assert directory.create_calls == 0


def test_too_many_rows_is_rejected(client, directory):
    rows = [(f"H{i}", f"A{i}", "") for i in range(21)]
    response = upload(client, make_csv(rows))

    assert response.status_code == 422
    assert directory.create_calls == 0


def test_oversized_file_is_rejected(client):
    response = upload(client, b"name,address\n" + b"x" * 300_000)
    assert response.status_code == 413


def test_transient_upstream_errors_are_retried(client, directory):
    directory.flaky_names = {"Hospital 2": 2}  # two 503s, then success (MAX_RETRIES=2)
    body = upload(client, make_csv(ROWS)).json()

    assert body["status"] == "completed"
    row2 = body["hospitals"][1]
    assert row2["status"] == "created_and_activated"
    assert directory.create_calls == 7


def test_ambiguous_create_failure_does_not_create_a_duplicate(client, directory):
    # The upstream saves the hospital but answers 500 (twice); retrying the
    # POST blindly would create it again.
    directory.save_then_fail_names = {"Hospital 2": 2}
    body = upload(client, make_csv(ROWS)).json()

    assert body["status"] == "completed"
    assert directory.create_calls == 5  # the saved record was adopted, not re-created
    assert len(directory.hospitals) == 5
    assert body["hospitals"][1]["hospital_id"] in directory.hospitals


def test_ambiguous_failure_without_a_saved_record_is_retried(client, directory):
    directory.fail_names = {"Hospital 2"}  # 500 every time, nothing saved
    body = upload(client, make_csv(ROWS)).json()

    assert body["hospitals"][1]["status"] == "failed"
    assert body["hospitals"][1]["attempts"] == 3  # MAX_RETRIES=2 in tests
    assert directory.create_calls == 4 + 3


def test_identical_rows_are_rejected(client, directory):
    rows = [("Twin Hospital", "1 Same St", "555"), ("twin hospital", "1 same st", "555")]
    response = upload(client, make_csv(rows))

    assert response.status_code == 422
    assert response.json()["errors"][0]["message"] == "Duplicate of row 1"
    assert directory.create_calls == 0


def test_client_errors_are_not_retried(client, directory):
    directory.reject_names = {"Hospital 3"}
    body = upload(client, make_csv(ROWS)).json()

    assert directory.create_calls == 5
    assert body["hospitals"][2]["status"] == "failed"
    assert "422" in body["hospitals"][2]["error"]


def test_partial_failure_leaves_batch_inactive(client, directory):
    directory.fail_names = {"Hospital 4"}
    body = upload(client, make_csv(ROWS)).json()

    assert body["status"] == "partially_failed"
    assert body["processed_hospitals"] == 4
    assert body["failed_hospitals"] == 1
    assert body["batch_activated"] is False
    assert body["hospitals"][3]["status"] == "failed"
    assert body["hospitals"][0]["status"] == "created"
    assert not any(h["active"] for h in directory.hospitals.values())


def test_resume_retries_only_failed_rows_then_activates(client, directory):
    directory.fail_names = {"Hospital 4"}
    batch_id = upload(client, make_csv(ROWS)).json()["batch_id"]
    calls_before = directory.create_calls

    directory.fail_names = set()
    response = client.post(f"/hospitals/bulk/{batch_id}/resume")

    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "completed"
    assert body["batch_activated"] is True
    assert directory.create_calls == calls_before + 1
    assert len(directory.hospitals) == 5
    assert all(h["active"] for h in directory.hospitals.values())


def test_resume_adopts_rows_the_upstream_saved_despite_an_error(client, directory):
    directory.fail_names = {"Hospital 2"}
    batch_id = upload(client, make_csv(ROWS)).json()["batch_id"]

    # Simulate a create that succeeded upstream although our call failed.
    directory.fail_names = set()
    directory.hospitals[999] = {
        "id": 999, "name": "Hospital 2", "address": "2 Main St", "phone": "555-0002",
        "creation_batch_id": batch_id, "active": False,
    }
    calls_before = directory.create_calls
    body = client.post(f"/hospitals/bulk/{batch_id}/resume").json()

    assert body["status"] == "completed"
    assert body["hospitals"][1]["hospital_id"] == 999
    assert directory.create_calls == calls_before  # no duplicate created


def test_activation_failure_can_be_resumed(client, directory):
    directory.activate_status = 500
    body = upload(client, make_csv(ROWS)).json()
    assert body["status"] == "activation_failed"
    assert body["processed_hospitals"] == 5
    assert body["batch_activated"] is False

    directory.activate_status = 200
    calls_before = directory.create_calls
    body = client.post(f"/hospitals/bulk/{body['batch_id']}/resume").json()
    assert body["status"] == "completed"
    assert directory.create_calls == calls_before


def test_resume_of_completed_batch_conflicts(client):
    batch_id = upload(client, make_csv(ROWS)).json()["batch_id"]
    assert client.post(f"/hospitals/bulk/{batch_id}/resume").status_code == 409


def test_rollback_deletes_batch_upstream(client, directory):
    directory.fail_names = {"Hospital 1"}
    batch_id = upload(client, make_csv(ROWS)).json()["batch_id"]
    assert len(directory.hospitals) == 4

    body = client.delete(f"/hospitals/bulk/{batch_id}").json()
    assert body["status"] == "rolled_back"
    assert directory.hospitals == {}


def test_async_mode_returns_202_and_progress_is_pollable(client, directory):
    response = upload(client, make_csv(ROWS), **{"async": "true"})
    assert response.status_code == 202
    batch_id = response.json()["batch_id"]
    assert response.json()["status"] == "queued"

    # TestClient runs background tasks before returning, so the batch is done here.
    body = client.get(f"/hospitals/bulk/{batch_id}").json()
    assert body["status"] == "completed"
    assert body["processed_hospitals"] == 5


def test_unknown_batch_is_404(client):
    assert client.get("/hospitals/bulk/00000000-0000-0000-0000-000000000000").status_code == 404
    assert client.get("/hospitals/bulk/not-a-uuid").status_code == 422


def test_validate_endpoint_creates_nothing(client, directory):
    body = upload(client, make_csv(ROWS), path="/hospitals/bulk/validate").json()
    assert body["valid"] is True
    assert body["total_rows"] == 5
    assert directory.create_calls == 0


def test_list_batches_newest_first(client):
    first = upload(client, make_csv(ROWS[:1])).json()["batch_id"]
    second = upload(client, make_csv(ROWS[1:2])).json()["batch_id"]
    ids = [b["batch_id"] for b in client.get("/hospitals/bulk").json()]
    assert ids[:2] == [second, first]


def test_rollback_reports_upstream_failure_as_502(client, directory):
    batch_id = upload(client, make_csv(ROWS)).json()["batch_id"]
    directory.delete_status = 500
    response = client.delete(f"/hospitals/bulk/{batch_id}")
    assert response.status_code == 502
    assert "Hospital Directory API error" in response.json()["detail"]
    assert client.get(f"/hospitals/bulk/{batch_id}").json()["status"] == "completed"


def test_resume_and_rollback_refuse_a_running_batch(client):
    from app.models import BatchStatus, HospitalRow
    from app.store import BatchState

    state = BatchState.new("22222222-2222-2222-2222-222222222222", [HospitalRow(row=1, name="A", address="B")])
    state.status = BatchStatus.PROCESSING
    client.app.state.store.add(state)

    assert client.post(f"/hospitals/bulk/{state.batch_id}/resume").status_code == 409
    assert client.delete(f"/hospitals/bulk/{state.batch_id}").status_code == 409
    assert client.get(f"/hospitals/bulk/{state.batch_id}").json()["status"] == "processing"
