from app.models import BatchStatus, HospitalRow, RowStatus
from app.store import BatchState, BatchStore


def batch(batch_id, status=BatchStatus.COMPLETED, key=None, rows=1):
    state = BatchState.new(batch_id, [HospitalRow(row=i, name=f"H{i}", address="A") for i in range(1, rows + 1)])
    state.status = status
    state.idempotency_key = key
    return state


def test_evicts_oldest_finished_batch_when_full():
    store = BatchStore(max_batches=2)
    for i in range(3):
        store.add(batch(f"b{i}"))
    assert [s.batch_id for s in store.list()] == ["b2", "b1"]
    assert store.get("b0") is None


def test_never_evicts_a_running_batch():
    store = BatchStore(max_batches=2)
    store.add(batch("running", status=BatchStatus.PROCESSING))
    store.add(batch("done"))
    store.add(batch("new"))
    assert store.get("running") is not None
    assert store.get("done") is None


def test_keeps_all_when_every_batch_is_running():
    store = BatchStore(max_batches=1)
    store.add(batch("a", status=BatchStatus.PROCESSING))
    store.add(batch("b", status=BatchStatus.QUEUED))
    assert len(store.list()) == 2


def test_idempotency_key_lookup_and_eviction():
    store = BatchStore(max_batches=1)
    store.add(batch("a", key="k1"))
    assert store.find_by_key("k1").batch_id == "a"
    store.add(batch("b", key="k2"))
    assert store.find_by_key("k1") is None  # evicted with its batch
    assert store.find_by_key("k2").batch_id == "b"
    assert store.find_by_key("missing") is None


def test_result_counts_and_order():
    state = batch("x", status=BatchStatus.PARTIALLY_FAILED, rows=4)
    state.results[1].status = RowStatus.CREATED
    state.results[2].status = RowStatus.FAILED
    state.results[3].status = RowStatus.CREATED_AND_ACTIVATED
    result = state.to_result()
    assert (result.total_hospitals, result.processed_hospitals, result.failed_hospitals, result.pending_hospitals) == (
        4, 2, 1, 1,
    )
    assert [h.row for h in result.hospitals] == [1, 2, 3, 4]


def test_elapsed_is_zero_before_start_and_frozen_after_finish():
    state = batch("x")
    assert state.elapsed_seconds() == 0.0
    state.started_at, state.finished_at = 10.0, 12.5
    assert state.elapsed_seconds() == 2.5
