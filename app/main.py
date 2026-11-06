import logging
import uuid
from contextlib import asynccontextmanager
from typing import List

from fastapi import BackgroundTasks, FastAPI, File, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import JSONResponse

from app.client import HospitalAPIError, HospitalDirectoryClient
from app.config import get_settings
from app.csv_parser import validate_csv
from app.models import BulkResult, ValidationReport
from app.processor import BatchConflictError, BulkProcessor
from app.store import BatchState, BatchStore

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    client = HospitalDirectoryClient(settings)
    app.state.settings = settings
    app.state.client = client
    app.state.store = BatchStore(settings.max_stored_batches)
    app.state.processor = BulkProcessor(client, settings.max_concurrency)
    yield
    await client.aclose()


app = FastAPI(
    title="Hospital Bulk Processing API",
    description=(
        "Bulk-creates hospitals from a CSV file through the Hospital Directory API. "
        "Each upload becomes a batch: hospitals are created inactive, then the whole "
        "batch is activated once every row succeeds."
    ),
    version="1.0.0",
    lifespan=lifespan,
)


async def _read_upload(request: Request, file: UploadFile) -> bytes:
    limit = request.app.state.settings.max_csv_bytes
    content = await file.read(limit + 1)
    if len(content) > limit:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, f"CSV file exceeds {limit // 1024} KB"
        )
    return content


def _get_batch(request: Request, batch_id: uuid.UUID) -> BatchState:
    state = request.app.state.store.get(str(batch_id))
    if state is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Batch not found")
    return state


@app.get("/", tags=["health"])
async def health():
    return {"status": "ok"}


@app.post("/hospitals/bulk/validate", response_model=ValidationReport, tags=["bulk"])
async def validate_bulk_csv(request: Request, file: UploadFile = File(...)):
    """Check a CSV without creating anything. Returns every problem found."""
    content = await _read_upload(request, file)
    return validate_csv(content, request.app.state.settings.max_csv_rows)


@app.post(
    "/hospitals/bulk",
    response_model=BulkResult,
    tags=["bulk"],
    responses={202: {"model": BulkResult}, 422: {"model": ValidationReport}},
)
async def bulk_create_hospitals(
    request: Request,
    background: BackgroundTasks,
    file: UploadFile = File(..., description="CSV with columns name,address,phone (phone optional)"),
    async_mode: bool = Query(
        False,
        alias="async",
        description="Return 202 immediately and process in the background; poll GET /hospitals/bulk/{batch_id}",
    ),
):
    """Create every hospital in the CSV under a new batch, then activate the batch."""
    content = await _read_upload(request, file)
    report = validate_csv(content, request.app.state.settings.max_csv_rows)
    if not report.valid:
        return JSONResponse(status_code=422, content=report.model_dump(mode="json"))

    state = BatchState.new(str(uuid.uuid4()), report.rows)
    request.app.state.store.add(state)
    processor: BulkProcessor = request.app.state.processor

    if async_mode:
        background.add_task(processor.run, state)
        return JSONResponse(status_code=202, content=state.to_result().model_dump(mode="json"))

    await processor.run(state)
    return state.to_result()


@app.get("/hospitals/bulk", response_model=List[BulkResult], tags=["bulk"])
async def list_batches(request: Request, limit: int = Query(20, ge=1, le=100)):
    """Recent batches, newest first."""
    return [s.to_result() for s in request.app.state.store.list()[:limit]]


@app.get("/hospitals/bulk/{batch_id}", response_model=BulkResult, tags=["bulk"])
async def get_batch_progress(request: Request, batch_id: uuid.UUID):
    """Progress and results of a batch. Poll this while an async batch runs."""
    return _get_batch(request, batch_id).to_result()


@app.post("/hospitals/bulk/{batch_id}/resume", response_model=BulkResult, tags=["bulk"])
async def resume_batch(request: Request, batch_id: uuid.UUID):
    """Retry the rows that failed (and activation, if that failed), then activate the batch."""
    state = _get_batch(request, batch_id)
    if state.is_running:
        raise HTTPException(status.HTTP_409_CONFLICT, "Batch is still processing")
    try:
        await request.app.state.processor.resume(state)
    except BatchConflictError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc))
    return state.to_result()


@app.delete("/hospitals/bulk/{batch_id}", response_model=BulkResult, tags=["bulk"])
async def rollback_batch(request: Request, batch_id: uuid.UUID):
    """Delete every hospital this batch created in the Hospital Directory."""
    state = _get_batch(request, batch_id)
    if state.is_running:
        raise HTTPException(status.HTTP_409_CONFLICT, "Batch is still processing")
    try:
        await request.app.state.processor.rollback(state)
    except HospitalAPIError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Hospital Directory API error: {exc}")
    return state.to_result()
