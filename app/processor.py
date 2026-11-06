"""Runs a batch: create every hospital through the upstream API, then activate the batch.

Rows are created in parallel, bounded by a semaphore (MAX_CONCURRENCY) so the
load on the upstream stays predictable as limits grow. The batch is activated
only once every row exists; otherwise its hospitals stay inactive (invisible
as active records) until the failed rows are resumed or the batch is rolled back.
"""

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

from app.client import HospitalAPIError, HospitalDirectoryClient
from app.models import BatchStatus, HospitalRow, RowStatus
from app.store import BatchState

logger = logging.getLogger(__name__)

RETRYABLE_ROW_STATUSES = (RowStatus.PENDING, RowStatus.FAILED)


class BatchConflictError(Exception):
    """The batch is in a state that doesn't allow the requested operation."""


class BulkProcessor:
    def __init__(
        self,
        client: HospitalDirectoryClient,
        max_concurrency: int,
        max_retries: int = 3,
        retry_backoff_seconds: float = 0.5,
    ):
        self._client = client
        self._max_concurrency = max_concurrency
        self._max_retries = max_retries
        self._retry_backoff = retry_backoff_seconds

    async def run(self, state: BatchState) -> None:
        async with state.lock:
            await self._run_locked(state)

    async def resume(self, state: BatchState) -> None:
        """Retry failed rows (and a failed activation) for an existing batch."""
        async with state.lock:
            if state.status in (BatchStatus.COMPLETED, BatchStatus.ROLLED_BACK):
                raise BatchConflictError(f"Batch is {state.status.value}; nothing to resume")
            await self._reconcile(state)
            await self._run_locked(state)

    async def rollback(self, state: BatchState) -> None:
        """Delete every hospital in the batch upstream."""
        async with state.lock:
            if state.status == BatchStatus.ROLLED_BACK:
                return
            await self._client.delete_batch(state.batch_id)
            for result in state.results.values():
                result.status = RowStatus.ROLLED_BACK
            state.status = BatchStatus.ROLLED_BACK
            state.batch_activated = False

    async def _run_locked(self, state: BatchState) -> None:
        state.status = BatchStatus.PROCESSING
        state.started_at = time.monotonic()
        state.finished_at = None
        try:
            todo = [r for r in state.rows if state.results[r.row].status in RETRYABLE_ROW_STATUSES]
            if todo:
                await self._client.warm_up()
                semaphore = asyncio.Semaphore(self._max_concurrency)
                await asyncio.gather(*(self._create_row(state, row, semaphore) for row in todo))
            await self._finish(state)
        except Exception:
            logger.exception("Batch %s crashed", state.batch_id)
            for result in state.results.values():
                if result.status == RowStatus.PENDING:
                    result.status = RowStatus.FAILED
                    result.error = "Internal error while processing the batch"
            state.status = BatchStatus.FAILED
        finally:
            state.finished_at = time.monotonic()

    async def _create_row(self, state: BatchState, row: HospitalRow, semaphore: asyncio.Semaphore) -> None:
        result = state.results[row.row]
        for attempt in range(self._max_retries + 1):
            # Hold a slot only while talking to the upstream, not while backing
            # off, so one retrying row doesn't hold up the others.
            async with semaphore:
                result.attempts += 1
                try:
                    created = await self._client.create_hospital(row, state.batch_id)
                except HospitalAPIError as exc:
                    # After an ambiguous failure the upstream may have saved the
                    # record anyway; adopt it rather than create a duplicate.
                    if exc.ambiguous and await self._adopt_existing(state, [row]):
                        return
                    if not exc.ambiguous or attempt == self._max_retries:
                        result.status = RowStatus.FAILED
                        result.error = str(exc)
                        return
                else:
                    result.hospital_id = created.get("id")
                    result.status = RowStatus.CREATED
                    result.error = None
                    return
            await asyncio.sleep(self._retry_backoff * 2 ** attempt)

    async def _finish(self, state: BatchState) -> None:
        results = state.results.values()
        created = sum(1 for r in results if r.status == RowStatus.CREATED)

        if created < len(state.results):
            state.status = BatchStatus.PARTIALLY_FAILED if created else BatchStatus.FAILED
            return

        try:
            await self._client.activate_batch(state.batch_id)
        except HospitalAPIError as exc:
            logger.warning("Activating batch %s failed: %s", state.batch_id, exc)
            state.status = BatchStatus.ACTIVATION_FAILED
            return

        for result in results:
            result.status = RowStatus.CREATED_AND_ACTIVATED
        state.batch_activated = True
        state.status = BatchStatus.COMPLETED

    async def _reconcile(self, state: BatchState) -> None:
        """Before a resume, mark failed rows as created if the upstream already has them."""
        failed = [r for r in state.rows if state.results[r.row].status in RETRYABLE_ROW_STATUSES]
        if failed:
            await self._adopt_existing(state, failed)

    async def _adopt_existing(self, state: BatchState, rows: List[HospitalRow]) -> bool:
        """Match rows to upstream records in this batch that no row has claimed yet.

        A row matches a record with the same name, address and phone. Returns
        True if every given row was matched.
        """
        try:
            existing = await self._client.get_batch(state.batch_id)
        except HospitalAPIError as exc:
            logger.warning("Could not read batch %s from upstream: %s", state.batch_id, exc)
            return False

        # No await below, so concurrent rows can't claim the same record.
        claimed = {r.hospital_id for r in state.results.values() if r.hospital_id is not None}
        matched = 0
        for row in rows:
            record = _find_unclaimed(existing, row, claimed)
            if record is None:
                continue
            result = state.results[row.row]
            result.hospital_id = record["id"]
            result.status = RowStatus.CREATED
            result.error = None
            claimed.add(record["id"])
            matched += 1
        return matched == len(rows)


def _find_unclaimed(records: List[Dict[str, Any]], row: HospitalRow, claimed: set) -> Optional[Dict[str, Any]]:
    for record in records:
        if record.get("id") in claimed:
            continue
        if (record.get("name"), record.get("address"), record.get("phone")) == (row.name, row.address, row.phone):
            return record
    return None
