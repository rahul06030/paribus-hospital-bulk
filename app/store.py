"""In-memory batch state.

Each batch keeps its rows and per-row results, so progress can be polled while
it runs and failed rows can be resumed later. State is lost on restart; the
upstream API stays the source of truth for which hospitals exist, and resume
reconciles against it.
"""

import asyncio
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

from app.models import BatchStatus, BulkResult, HospitalResult, HospitalRow, RowStatus


@dataclass
class BatchState:
    batch_id: str
    rows: List[HospitalRow]
    results: Dict[int, HospitalResult]
    status: BatchStatus = BatchStatus.QUEUED
    batch_activated: bool = False
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @classmethod
    def new(cls, batch_id: str, rows: List[HospitalRow]) -> "BatchState":
        results = {r.row: HospitalResult(row=r.row, name=r.name, status=RowStatus.PENDING) for r in rows}
        return cls(batch_id=batch_id, rows=rows, results=results)

    @property
    def is_running(self) -> bool:
        return self.status in (BatchStatus.QUEUED, BatchStatus.PROCESSING)

    def elapsed_seconds(self) -> float:
        if self.started_at is None:
            return 0.0
        end = self.finished_at if self.finished_at is not None else time.monotonic()
        return round(end - self.started_at, 3)

    def to_result(self) -> BulkResult:
        results = [self.results[r.row] for r in self.rows]
        done = (RowStatus.CREATED, RowStatus.CREATED_AND_ACTIVATED)
        return BulkResult(
            batch_id=self.batch_id,
            status=self.status,
            total_hospitals=len(results),
            processed_hospitals=sum(1 for r in results if r.status in done),
            failed_hospitals=sum(1 for r in results if r.status == RowStatus.FAILED),
            pending_hospitals=sum(1 for r in results if r.status == RowStatus.PENDING),
            processing_time_seconds=self.elapsed_seconds(),
            batch_activated=self.batch_activated,
            created_at=self.created_at,
            hospitals=results,
        )


class BatchStore:
    def __init__(self, max_batches: int):
        self._max = max_batches
        self._batches: "OrderedDict[str, BatchState]" = OrderedDict()

    def add(self, state: BatchState) -> None:
        self._batches[state.batch_id] = state
        while len(self._batches) > self._max:
            # Evict the oldest batch that isn't running.
            for batch_id, old in self._batches.items():
                if not old.is_running:
                    del self._batches[batch_id]
                    break
            else:
                break

    def get(self, batch_id: str) -> Optional[BatchState]:
        return self._batches.get(batch_id)

    def list(self) -> List[BatchState]:
        return list(reversed(self._batches.values()))
