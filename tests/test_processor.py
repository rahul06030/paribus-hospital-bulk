import asyncio

import pytest

from app.models import BatchStatus, HospitalRow
from app.processor import BulkProcessor
from app.store import BatchState


class SlowClient:
    """Records how many creates run at once."""

    def __init__(self):
        self.in_flight = 0
        self.peak = 0
        self.next_id = 1

    async def warm_up(self):
        pass

    async def create_hospital(self, row, batch_id):
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        await asyncio.sleep(0.02)
        self.in_flight -= 1
        self.next_id += 1
        return {"id": self.next_id}

    async def activate_batch(self, batch_id):
        return {}


@pytest.mark.asyncio
async def test_creates_run_in_parallel_but_never_above_the_limit():
    client = SlowClient()
    rows = [HospitalRow(row=i, name=f"H{i}", address="A") for i in range(1, 21)]
    state = BatchState.new("batch", rows)

    await BulkProcessor(client, max_concurrency=5).run(state)

    assert state.status == BatchStatus.COMPLETED
    assert client.peak == 5
    # 20 rows x 20ms at 5 at a time is ~80ms; sequential would be 400ms.
    assert state.elapsed_seconds() < 0.3
