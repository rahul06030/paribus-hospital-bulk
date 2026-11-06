import asyncio
import time

import pytest

from app.client import HospitalAPIError
from app.models import BatchStatus, HospitalRow, RowStatus
from app.processor import BatchConflictError, BulkProcessor
from app.store import BatchState


class FakeClient:
    """Programmable upstream. `create_behaviour[name]` is a list of outcomes, consumed per call."""

    def __init__(self, delay=0.0):
        self.delay = delay
        self.in_flight = 0
        self.peak = 0
        self.next_id = 100
        self.saved = []
        self.create_behaviour = {}
        self.create_log = []
        self.get_batch_error = None
        self.activate_error = None
        self.delete_error = None

    async def warm_up(self):
        pass

    async def create_hospital(self, row, batch_id):
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        self.create_log.append((row.name, time.monotonic()))
        try:
            await asyncio.sleep(self.delay)
            outcomes = self.create_behaviour.get(row.name, [])
            outcome = outcomes.pop(0) if outcomes else "ok"
            if outcome == "ambiguous":
                raise HospitalAPIError("500", status_code=500, retryable=True, ambiguous=True)
            if outcome == "rejected":
                raise HospitalAPIError("422", status_code=422)
            if outcome == "crash":
                raise RuntimeError("bug")
            self.next_id += 1
            self.saved.append({"id": self.next_id, "name": row.name, "address": row.address, "phone": row.phone})
            return {"id": self.next_id}
        finally:
            self.in_flight -= 1

    async def get_batch(self, batch_id):
        if self.get_batch_error:
            raise self.get_batch_error
        return list(self.saved)

    async def activate_batch(self, batch_id):
        if self.activate_error:
            raise self.activate_error
        return {}

    async def delete_batch(self, batch_id):
        if self.delete_error:
            raise self.delete_error
        self.saved.clear()


def rows(n):
    return [HospitalRow(row=i, name=f"H{i}", address="A") for i in range(1, n + 1)]


async def test_creates_run_in_parallel_but_never_above_the_limit():
    client = FakeClient(delay=0.02)
    state = BatchState.new("batch", rows(20))

    await BulkProcessor(client, max_concurrency=5).run(state)

    assert state.status == BatchStatus.COMPLETED
    assert client.peak == 5
    # 20 rows x 20ms at 5 at a time is ~80ms; sequential would be 400ms.
    assert state.elapsed_seconds() < 0.3


async def test_a_row_backing_off_does_not_hold_a_concurrency_slot():
    client = FakeClient(delay=0.01)
    client.create_behaviour = {"H1": ["ambiguous"]}
    state = BatchState.new("batch", rows(2))

    await BulkProcessor(client, max_concurrency=1, max_retries=1, retry_backoff_seconds=0.3).run(state)

    assert state.status == BatchStatus.COMPLETED
    order = [name for name, _ in client.create_log]
    assert order == ["H1", "H2", "H1"]  # H2 ran while H1 was backing off


async def test_ambiguous_failure_is_retried_after_checking_upstream():
    client = FakeClient()
    client.create_behaviour = {"H1": ["ambiguous", "ambiguous"]}
    state = BatchState.new("batch", rows(1))

    await BulkProcessor(client, max_concurrency=5, max_retries=2, retry_backoff_seconds=0).run(state)

    assert state.status == BatchStatus.COMPLETED
    assert state.results[1].attempts == 3


async def test_rejected_row_is_not_retried():
    client = FakeClient()
    client.create_behaviour = {"H1": ["rejected"]}
    state = BatchState.new("batch", rows(2))

    await BulkProcessor(client, max_concurrency=5, retry_backoff_seconds=0).run(state)

    assert state.status == BatchStatus.PARTIALLY_FAILED
    assert state.results[1].attempts == 1 and state.results[1].error == "422"


async def test_unreadable_upstream_batch_falls_back_to_retrying():
    client = FakeClient()
    client.create_behaviour = {"H1": ["ambiguous"]}
    client.get_batch_error = HospitalAPIError("down", status_code=503)
    state = BatchState.new("batch", rows(1))

    await BulkProcessor(client, max_concurrency=5, max_retries=1, retry_backoff_seconds=0).run(state)

    assert state.status == BatchStatus.COMPLETED
    assert state.results[1].attempts == 2


async def test_unexpected_error_marks_the_batch_failed():
    client = FakeClient()
    client.create_behaviour = {"H1": ["crash"]}
    state = BatchState.new("batch", rows(2))

    await BulkProcessor(client, max_concurrency=1).run(state)

    assert state.status == BatchStatus.FAILED
    assert state.finished_at is not None
    assert any(r.error == "Internal error while processing the batch" for r in state.results.values())


async def test_resume_when_upstream_batch_cannot_be_read_still_retries():
    client = FakeClient()
    client.create_behaviour = {"H1": ["rejected"]}
    state = BatchState.new("batch", rows(1))
    processor = BulkProcessor(client, max_concurrency=5, retry_backoff_seconds=0)
    await processor.run(state)
    assert state.status == BatchStatus.FAILED

    client.get_batch_error = HospitalAPIError("down", status_code=503)
    await processor.resume(state)
    assert state.status == BatchStatus.COMPLETED


async def test_resume_refuses_completed_and_rolled_back_batches():
    processor = BulkProcessor(FakeClient(), max_concurrency=5)
    state = BatchState.new("batch", rows(1))
    await processor.run(state)
    with pytest.raises(BatchConflictError):
        await processor.resume(state)
    await processor.rollback(state)
    with pytest.raises(BatchConflictError):
        await processor.resume(state)


async def test_rollback_is_idempotent_and_propagates_upstream_errors():
    client = FakeClient()
    processor = BulkProcessor(client, max_concurrency=5)
    state = BatchState.new("batch", rows(2))
    await processor.run(state)

    client.delete_error = HospitalAPIError("down", status_code=500)
    with pytest.raises(HospitalAPIError):
        await processor.rollback(state)
    assert state.status == BatchStatus.COMPLETED  # unchanged when the delete failed

    client.delete_error = None
    await processor.rollback(state)
    await processor.rollback(state)
    assert state.status == BatchStatus.ROLLED_BACK
    assert all(r.status == RowStatus.ROLLED_BACK for r in state.results.values())
    assert not state.batch_activated
