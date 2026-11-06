"""Async client for the Hospital Directory API.

Retries transient failures with exponential backoff. 4xx responses other than
429 are the request's fault, so they fail immediately.

Creating a hospital is not idempotent, so a create is retried here only when
the upstream certainly did not process it (connection never made, 429, 503).
After an ambiguous failure (500/502/504, read timeout, dropped connection) the
record may already exist; the error is raised with `ambiguous=True` and the
processor checks the batch before trying again.
"""

import asyncio
import logging
from typing import Any, Dict, List, Optional

import httpx

from app.config import Settings
from app.models import HospitalRow

logger = logging.getLogger(__name__)

RETRYABLE_STATUS = {429, 500, 502, 503, 504}
# Statuses that mean the request was not processed.
NOT_PROCESSED_STATUS = {429, 503}
# Errors raised before the request reached the upstream.
NOT_SENT_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)


class HospitalAPIError(Exception):
    def __init__(
        self,
        message: str,
        status_code: Optional[int] = None,
        retryable: bool = False,
        ambiguous: bool = False,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable
        # True when the upstream may have applied the request despite the error.
        self.ambiguous = ambiguous


class HospitalDirectoryClient:
    def __init__(self, settings: Settings, http: Optional[httpx.AsyncClient] = None):
        self._settings = settings
        self._http = http or httpx.AsyncClient(
            base_url=settings.hospital_api_base_url.rstrip("/"),
            timeout=settings.request_timeout_seconds,
            limits=httpx.Limits(max_connections=settings.max_concurrency * 2),
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def warm_up(self) -> None:
        """Wake the upstream if it is asleep, so the batch's parallel calls don't all time out."""
        try:
            await self._http.get("/", timeout=self._settings.warmup_timeout_seconds)
        except httpx.HTTPError as exc:
            logger.warning("Upstream warm-up failed: %s", exc)

    async def create_hospital(self, row: HospitalRow, batch_id: str) -> Dict[str, Any]:
        payload = {
            "name": row.name,
            "address": row.address,
            "phone": row.phone,
            "creation_batch_id": batch_id,
        }
        return await self._request("POST", "/hospitals/", idempotent=False, json=payload)

    async def activate_batch(self, batch_id: str) -> Any:
        return await self._request("PATCH", f"/hospitals/batch/{batch_id}/activate")

    async def get_batch(self, batch_id: str) -> List[Dict[str, Any]]:
        try:
            return await self._request("GET", f"/hospitals/batch/{batch_id}")
        except HospitalAPIError as exc:
            if exc.status_code == 404:  # upstream answers 404 for an empty batch
                return []
            raise

    async def delete_batch(self, batch_id: str) -> Any:
        try:
            return await self._request("DELETE", f"/hospitals/batch/{batch_id}")
        except HospitalAPIError as exc:
            if exc.status_code == 404:
                return None
            raise

    async def _request(self, method: str, url: str, idempotent: bool = True, **kwargs: Any) -> Any:
        """Send a request, retrying transient failures.

        For a non-idempotent request, only failures where the upstream certainly
        did nothing are retried; an ambiguous failure is raised at once.
        """
        attempts = self._settings.max_retries + 1
        last_error: Optional[HospitalAPIError] = None

        for attempt in range(1, attempts + 1):
            try:
                response = await self._http.request(method, url, **kwargs)
            except httpx.TransportError as exc:
                not_sent = isinstance(exc, NOT_SENT_ERRORS)
                kind = "Timed out" if isinstance(exc, httpx.TimeoutException) else "Network error"
                last_error = HospitalAPIError(
                    f"{kind} calling {method} {url}: {exc!r}", retryable=True, ambiguous=not not_sent
                )
                logger.info("%s %s failed (attempt %d/%d): %r", method, url, attempt, attempts, exc)
            else:
                if response.is_success:
                    return response.json() if response.content else None
                status_code = response.status_code
                last_error = HospitalAPIError(
                    f"{method} {url} returned {status_code}: {_detail(response)}",
                    status_code=status_code,
                    retryable=status_code in RETRYABLE_STATUS,
                    ambiguous=status_code >= 500 and status_code not in NOT_PROCESSED_STATUS,
                )
                if not last_error.retryable:
                    raise last_error

            if not idempotent and last_error.ambiguous:
                raise last_error
            if attempt < attempts:
                await asyncio.sleep(self._settings.retry_backoff_seconds * 2 ** (attempt - 1))

        assert last_error is not None
        raise last_error


def _detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:200]
    if isinstance(body, dict) and "detail" in body:
        return str(body["detail"])[:200]
    return str(body)[:200]
