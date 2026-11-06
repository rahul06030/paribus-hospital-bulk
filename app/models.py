from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional

from pydantic import BaseModel, Field


class HospitalRow(BaseModel):
    """One validated CSV row. `row` is 1-based, counting data rows only."""

    row: int
    name: str
    address: str
    phone: Optional[str] = None


class RowError(BaseModel):
    row: int
    field: Optional[str] = None
    message: str


class RowStatus(str, Enum):
    PENDING = "pending"
    CREATED = "created"  # exists upstream, still inactive
    CREATED_AND_ACTIVATED = "created_and_activated"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"


class BatchStatus(str, Enum):
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    PARTIALLY_FAILED = "partially_failed"  # some rows failed; batch left inactive
    ACTIVATION_FAILED = "activation_failed"  # all rows created, activation call failed
    FAILED = "failed"  # nothing could be created
    ROLLED_BACK = "rolled_back"


class HospitalResult(BaseModel):
    row: int
    hospital_id: Optional[int] = None
    name: str
    status: RowStatus
    error: Optional[str] = None
    attempts: int = 0


class BulkResult(BaseModel):
    batch_id: str
    status: BatchStatus
    total_hospitals: int
    processed_hospitals: int
    failed_hospitals: int
    pending_hospitals: int
    processing_time_seconds: float
    batch_activated: bool
    created_at: datetime
    hospitals: List[HospitalResult]
    links: Dict[str, str] = Field(default_factory=dict)


class ValidationReport(BaseModel):
    valid: bool
    total_rows: int
    max_rows: int
    errors: List[RowError] = Field(default_factory=list)
    rows: List[HospitalRow] = Field(default_factory=list)
