"""Async HTTP client for the Homapel cloud API.

Implements Boundary A as defined in ARCHITECTURE.md §7.3. Parses the
standard error envelope from §7.2 and maps error codes to typed exceptions.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import aiohttp

from .const import CONVERSE_TIMEOUT, STATUS_TIMEOUT

_LOGGER = logging.getLogger(__name__)


class HomapelApiError(Exception):
    """Base error. Carries the standard error envelope fields when available."""

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        status: int | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.request_id = request_id


class HomapelAuthError(HomapelApiError):
    """401: invalid or missing api_key."""


class HomapelForbiddenError(HomapelApiError):
    """403: unit_suspended / tier_not_allowed / cross_tenant_access."""


class HomapelUnitNotActiveError(HomapelApiError):
    """422 unit_not_active — dormant unit hit a non-converse endpoint."""


class HomapelRateLimitedError(HomapelApiError):
    """429 rate_limited."""

    def __init__(self, message: str, *, retry_after: int | None = None, **kwargs: Any) -> None:
        super().__init__(message, **kwargs)
        self.retry_after = retry_after


class HomapelCostCeilingError(HomapelApiError):
    """429 cost_ceiling_exceeded."""


class HomapelNetworkError(HomapelApiError):
    """Transport/5xx error reaching the cloud."""


class HomapelTimeoutError(HomapelApiError):
    """Request timed out."""


@dataclass(slots=True)
class StatusResult:
    unit_id: str
    active: bool
    tier: str | None
    webhook_token: str | None
    cost_ceiling_reached: bool
    updated_at: str | None
    etag: str | None  # For If-None-Match on subsequent polls
    not_modified: bool = False  # True when server returned 304


@dataclass(slots=True)
class ConverseResult:
    speech: str
    conversation_id: str
    continue_conversation: bool
    llm_used: str | None
    tokens_in: int
    tokens_out: int
    tier: str | None
    dormant: bool


class HomapelCloudClient:
    """Thin wrapper around the Homapel cloud REST API (Boundary A)."""

    def __init__(self, session: aiohttp.ClientSession, api_base: str) -> None:
        self._session = session
        self._api_base = api_base.rstrip("/")

    async def get_status(
        self, api_key: str, *, etag: str | None = None
    ) -> StatusResult:
        """Fetch current activation status.

        Supports §7.3.3 ETag / If-None-Match. When the server returns 304,
        ``not_modified=True`` and the other fields are unpopulated — the caller
        must reuse the previously cached state.
        """
        headers: dict[str, str] = {}
        if etag is not None:
            headers["If-None-Match"] = etag

        status_code, data, resp_headers = await self._request(
            "GET",
            "/v1/units/status",
            timeout=STATUS_TIMEOUT,
            auth_key=api_key,
            extra_headers=headers,
            allow_304=True,
        )
        if status_code == 304:
            return StatusResult(
                unit_id="",
                active=False,
                tier=None,
                webhook_token=None,
                cost_ceiling_reached=False,
                updated_at=None,
                etag=etag,
                not_modified=True,
            )
        return StatusResult(
            unit_id=data["unit_id"],
            active=bool(data.get("active", False)),
            tier=data.get("tier"),
            webhook_token=data.get("webhook_token"),
            cost_ceiling_reached=bool(data.get("cost_ceiling_reached", False)),
            updated_at=data.get("updated_at"),
            etag=resp_headers.get("ETag"),
        )

    async def register_webhook(self, api_key: str, webhook_url: str) -> None:
        """§7.3.4 — register our inbound webhook URL with the cloud."""
        await self._request(
            "POST",
            "/v1/units/webhook",
            json={"webhook_url": webhook_url},
            timeout=STATUS_TIMEOUT,
            auth_key=api_key,
        )

    async def converse(
        self,
        api_key: str,
        *,
        text: str,
        conversation_id: str,
        language: str,
        device_id: str | None = None,
        area_id: str | None = None,
        speaker_id: str | None = None,
    ) -> ConverseResult:
        """§7.3.2 — send utterance, receive agent reply."""
        body: dict[str, Any] = {
            "text": text,
            "conversation_id": conversation_id,
            "language": language,
        }
        if device_id is not None:
            body["device_id"] = device_id
        if area_id is not None or speaker_id is not None:
            body["context"] = {}
            if area_id is not None:
                body["context"]["area_id"] = area_id
            if speaker_id is not None:
                body["context"]["speaker_id"] = speaker_id

        _, data, _ = await self._request(
            "POST",
            "/v1/converse",
            json=body,
            timeout=CONVERSE_TIMEOUT,
            auth_key=api_key,
        )
        tokens = data.get("tokens", {}) or {}
        return ConverseResult(
            speech=data["speech"],
            conversation_id=data["conversation_id"],
            continue_conversation=bool(data.get("continue_conversation", False)),
            llm_used=data.get("llm_used"),
            tokens_in=int(tokens.get("in", 0)),
            tokens_out=int(tokens.get("out", 0)),
            tier=data.get("tier"),
            dormant=bool(data.get("dormant", False)),
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        timeout: float,
        auth_key: str | None,
        json: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
        allow_304: bool = False,
    ) -> tuple[int, dict[str, Any], dict[str, str]]:
        url = f"{self._api_base}{path}"
        headers = {"Accept": "application/json"}
        if auth_key is not None:
            headers["Authorization"] = f"Bearer {auth_key}"
        if extra_headers:
            headers.update(extra_headers)

        try:
            async with self._session.request(
                method,
                url,
                json=json,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                request_id = resp.headers.get("X-Request-Id")
                if resp.status == 304 and allow_304:
                    return 304, {}, dict(resp.headers)
                if 200 <= resp.status < 300:
                    payload: dict[str, Any] = {}
                    if resp.content_length != 0:
                        try:
                            payload = await resp.json()
                        except (aiohttp.ContentTypeError, ValueError):
                            payload = {}
                    return resp.status, payload, dict(resp.headers)

                await self._raise_for_error(resp, request_id)
                # _raise_for_error always raises
                raise HomapelApiError("unreachable")
        except asyncio.TimeoutError as err:
            raise HomapelTimeoutError(str(err)) from err
        except aiohttp.ClientError as err:
            raise HomapelNetworkError(str(err)) from err

    async def _raise_for_error(
        self, resp: aiohttp.ClientResponse, request_id: str | None
    ) -> None:
        """Parse the §7.2 error envelope and raise a typed exception."""
        code: str | None = None
        message = f"HTTP {resp.status}"
        try:
            envelope = await resp.json()
            if isinstance(envelope, dict) and isinstance(envelope.get("error"), dict):
                err = envelope["error"]
                code = err.get("code")
                message = err.get("message") or message
        except (aiohttp.ContentTypeError, ValueError):
            pass

        status = resp.status
        retry_after_raw = resp.headers.get("Retry-After")
        retry_after = int(retry_after_raw) if retry_after_raw and retry_after_raw.isdigit() else None
        ctx = {"code": code, "status": status, "request_id": request_id}

        if status == 401:
            raise HomapelAuthError(message, **ctx)
        if status == 403:
            raise HomapelForbiddenError(message, **ctx)
        if status == 422 and code == "unit_not_active":
            raise HomapelUnitNotActiveError(message, **ctx)
        if status == 429:
            if code == "cost_ceiling_exceeded":
                raise HomapelCostCeilingError(message, **ctx)
            raise HomapelRateLimitedError(message, retry_after=retry_after, **ctx)
        if 500 <= status < 600:
            raise HomapelNetworkError(message, **ctx)
        raise HomapelApiError(message, **ctx)
