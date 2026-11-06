import asyncio
import hashlib
import logging
import uuid
from contextlib import asynccontextmanager
from typing import List, Optional

import httpx
from fastapi import (
    BackgroundTasks,
    FastAPI,
    File,
    Header,
    HTTPException,
    Query,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
    status,
)
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
    app.state.processor = BulkProcessor(
        client, settings.max_concurrency, settings.max_retries, settings.retry_backoff_seconds
    )
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


def _result_response(state: BatchState, replayed: bool = False) -> JSONResponse:
    """A batch result: 202 with a Location header while it runs, 200 once it has finished."""
    code = 202 if state.is_running else 200
    headers = {"Location": f"/hospitals/bulk/{state.batch_id}"}
    if replayed:
        headers["Idempotent-Replayed"] = "true"
    return JSONResponse(status_code=code, content=state.to_result().model_dump(mode="json"), headers=headers)


@app.get("/", tags=["health"])
async def health():
    """Liveness: the service is up."""
    return {"status": "ok"}


@app.get("/health", tags=["health"])
async def health_with_upstream(request: Request):
    """Readiness: the service is up and whether the Hospital Directory API answers."""
    try:
        latency = await request.app.state.client.ping(timeout=5.0)
        upstream = {"reachable": True, "latency_ms": round(latency * 1000)}
    except httpx.HTTPError as exc:
        upstream = {"reachable": False, "error": type(exc).__name__}
    return {"status": "ok" if upstream["reachable"] else "degraded", "upstream": upstream}


@app.post("/hospitals/bulk/validate", response_model=ValidationReport, tags=["bulk"])
async def validate_bulk_csv(request: Request, file: UploadFile = File(...)):
    """Check a CSV without creating anything. Returns every problem found."""
    content = await _read_upload(request, file)
    return validate_csv(content, request.app.state.settings.max_csv_rows)


@app.post(
    "/hospitals/bulk",
    response_model=BulkResult,
    tags=["bulk"],
    responses={
        202: {"model": BulkResult, "description": "Accepted; processing in the background"},
        409: {"description": "Idempotency-Key reused with a different file"},
        413: {"description": "File too large"},
        422: {"model": ValidationReport, "description": "Invalid CSV"},
    },
)
async def bulk_create_hospitals(
    request: Request,
    background: BackgroundTasks,
    file: UploadFile = File(..., description="CSV with columns name,address,phone (phone optional)"),
    async_mode: bool = Query(
        False,
        alias="async",
        description="Return 202 immediately and process in the background; follow progress at "
        "GET /hospitals/bulk/{batch_id} or WS /hospitals/bulk/{batch_id}/ws",
    ),
    idempotency_key: Optional[str] = Header(
        None,
        max_length=128,
        description="Repeat an upload safely: the same key and file return the original batch instead of a new one",
    ),
):
    """Create every hospital in the CSV under a new batch, then activate the batch."""
    content = await _read_upload(request, file)
    store: BatchStore = request.app.state.store
    content_hash = hashlib.sha256(content).hexdigest()

    if idempotency_key:
        existing = store.find_by_key(idempotency_key)
        if existing is not None:
            if existing.content_hash != content_hash:
                raise HTTPException(status.HTTP_409_CONFLICT, "Idempotency-Key was already used with a different file")
            return _result_response(existing, replayed=True)

    report = validate_csv(content, request.app.state.settings.max_csv_rows)
    if not report.valid:
        return JSONResponse(status_code=422, content=report.model_dump(mode="json"))

    state = BatchState.new(str(uuid.uuid4()), report.rows)
    state.idempotency_key = idempotency_key
    state.content_hash = content_hash
    store.add(state)
    processor: BulkProcessor = request.app.state.processor

    if async_mode:
        background.add_task(processor.run, state)
        return _result_response(state)

    await processor.run(state)
    return _result_response(state)


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


@app.websocket("/hospitals/bulk/{batch_id}/ws")
async def batch_progress_ws(websocket: WebSocket, batch_id: str):
    """Stream a batch's progress: a JSON snapshot on connect and after every change.

    Each message is a batch result plus `"final"`. The last message has
    `"final": true` (the batch has finished), after which the server closes the
    socket with code 1000. An unknown batch gets `{"error": ..., "final": true}`
    and code 4404. Clients should stop on `"final": true` rather than wait for
    the close frame: some proxies (Render's included) don't pass on a
    server-initiated close.
    """
    await websocket.accept()
    state = None
    try:
        state = websocket.app.state.store.get(str(uuid.UUID(batch_id)))
    except ValueError:
        pass
    if state is None:
        await websocket.send_json({"error": "Batch not found", "final": True})
        await websocket.close(code=4404)
        return

    last = None
    try:
        while True:
            final = not state.is_running
            snapshot = state.to_result().model_dump(mode="json")
            snapshot.pop("processing_time_seconds")  # changes every tick; not a progress event
            if snapshot != last or final:
                last = snapshot
                await websocket.send_json({**state.to_result().model_dump(mode="json"), "final": final})
            if final:
                break
            await asyncio.sleep(0.2)
        await websocket.close()
    except WebSocketDisconnect:
        pass
