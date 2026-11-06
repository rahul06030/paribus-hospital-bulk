from tests.conftest import make_csv

ROWS = [("Hospital A", "1 Main St", "555"), ("Hospital B", "2 Main St", "")]


def post(client, content, key=None, **params):
    headers = {"Idempotency-Key": key} if key else {}
    return client.post(
        "/hospitals/bulk", files={"file": ("h.csv", content, "text/csv")}, headers=headers, params=params
    )


def test_same_key_and_file_returns_the_original_batch(client, directory):
    first = post(client, make_csv(ROWS), key="upload-1")
    second = post(client, make_csv(ROWS), key="upload-1")

    assert first.status_code == second.status_code == 200
    assert second.json()["batch_id"] == first.json()["batch_id"]
    assert second.headers["Idempotent-Replayed"] == "true"
    assert "Idempotent-Replayed" not in first.headers
    assert directory.create_calls == 2  # nothing created the second time
    assert len(directory.hospitals) == 2


def test_same_key_with_a_different_file_is_rejected(client, directory):
    post(client, make_csv(ROWS), key="upload-1")
    response = post(client, make_csv(ROWS[:1]), key="upload-1")

    assert response.status_code == 409
    assert "different file" in response.json()["detail"]
    assert directory.create_calls == 2


def test_without_a_key_each_upload_is_a_new_batch(client, directory):
    first = post(client, make_csv(ROWS)).json()["batch_id"]
    second = post(client, make_csv(ROWS)).json()["batch_id"]
    assert first != second
    assert directory.create_calls == 4


def test_different_keys_are_independent(client, directory):
    a = post(client, make_csv(ROWS), key="a").json()["batch_id"]
    b = post(client, make_csv(ROWS), key="b").json()["batch_id"]
    assert a != b


def test_replay_works_for_async_uploads(client, directory):
    first = post(client, make_csv(ROWS), key="k", **{"async": "true"})
    assert first.status_code == 202
    replay = post(client, make_csv(ROWS), key="k", **{"async": "true"})
    assert replay.json()["batch_id"] == first.json()["batch_id"]
    assert replay.json()["status"] == "completed"


def test_invalid_file_is_not_remembered_under_the_key(client, directory):
    assert post(client, make_csv([("A", "")]), key="k").status_code == 422
    assert post(client, make_csv(ROWS), key="k").status_code == 200


def test_overlong_key_is_rejected(client):
    assert post(client, make_csv(ROWS), key="x" * 129).status_code == 422
