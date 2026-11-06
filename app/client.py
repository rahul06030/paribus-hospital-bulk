"""Async client for the Hospital Directory API.

Retries transient failures (network errors, timeouts, 429 and 5xx) with
exponential backoff. 4xx responses other than 429 are the request's fault,
so they fail immediately.
"""

import asyncio
import logging
from typing import Any, Dict, List, Optional

import httpx

from app.config import Settings
from app.models import HospitalRow

logger = logging.getLogger(__name__)

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class HospitalAPIError(Exception):
    def __init__(self, message: str, status_code: Optional[int] = None, retryable: bool = False):
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


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
        return await self._request("POST", "/hospitals/", json=payload)

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

    async def _request(self, method: str, url: str, **kwargs: Any) -> Any:
        attempts = self._settings.max_retries + 1
        last_error: Optional[HospitalAPIError] = None

        for attempt in range(1, attempts + 1):
            try:
                response = await self._http.request(method, url, **kwargs)
            except httpx.TimeoutException as exc:
                last_error = HospitalAPIError(f"Timed out calling {method} {url}", retryable=True)
                logger.info("%s %s timed out (attempt %d/%d): %s", method, url, attempt, attempts, exc)
            except httpx.TransportError as exc:
                last_error = HospitalAPIError(f"Network error calling {method} {url}: {exc}", retryable=True)
                logger.info("%s %s network error (attempt %d/%d): %s", method, url, attempt, attempts, exc)
            else:
                if response.is_success:
                    return response.json() if response.content else None
                retryable = response.status_code in RETRYABLE_STATUS
                last_error = HospitalAPIError(
                    f"{method} {url} returned {response.status_code}: {_detail(response)}",
                    status_code=response.status_code,
                    retryable=retryable,
                )
                if not retryable:
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
