"""Async HTTP client for the Homapel cloud API.

Implements Boundary A as defined in ARCHITECTURE.md §7.3. Parses the
standard error envelope from §7.2 and maps error codes to typed exceptions.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
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


class ConverseStream:
    """Async-iterable of HA assistant content deltas.

    Adapts both transports the cloud may use:
      * ``text/event-stream`` — yields a delta per SSE ``delta`` event,
        plus a final ``meta`` event populating the result fields.
      * ``application/json`` — yields exactly one delta containing the full
        speech, then ends. Result fields come from the JSON body.

    The shape of yielded dicts matches HA's ``AssistantContentDeltaDict``
    (role/content/thinking_content), so the entity can hand this iterator
    straight to ``chat_log.async_add_delta_content_stream``.

    After iteration completes, the metadata fields below are populated.
    Any error mid-stream (or before first delta) raises a typed exception
    from this module — same ladder as the non-streaming path.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        url: str,
        body: dict[str, Any],
        headers: dict[str, str],
        timeout: float,
    ) -> None:
        self._session = session
        self._url = url
        self._body = body
        self._headers = headers
        self._timeout = timeout
        # Populated during/after iteration.
        self.conversation_id: str | None = None
        self.continue_conversation: bool = False
        self.dormant: bool = False
        self.llm_used: str | None = None
        self.tokens_in: int = 0
        self.tokens_out: int = 0
        self.tier: str | None = None

    async def __aiter__(self) -> AsyncIterator[dict[str, Any]]:
        try:
            async with self._session.request(
                "POST",
                self._url,
                json=self._body,
                headers=self._headers,
                timeout=aiohttp.ClientTimeout(total=self._timeout),
            ) as resp:
                request_id = resp.headers.get("X-Request-Id")
                if not (200 <= resp.status < 300):
                    await _raise_for_error(resp, request_id)

                content_type = resp.headers.get("Content-Type", "").split(";", 1)[0].strip()
                if content_type == "text/event-stream":
                    async for delta in self._iter_sse(resp):
                        yield delta
                else:
                    payload = await resp.json()
                    self._populate_meta(payload)
                    yield {"role": "assistant", "content": payload["speech"]}
        except asyncio.TimeoutError as err:
            raise HomapelTimeoutError(str(err)) from err
        except aiohttp.ClientError as err:
            raise HomapelNetworkError(str(err)) from err

    async def _iter_sse(
        self, resp: aiohttp.ClientResponse
    ) -> AsyncIterator[dict[str, Any]]:
        first_delta = True
        event_name: str | None = None
        data_lines: list[str] = []

        async for raw in resp.content:
            line = raw.decode("utf-8").rstrip("\r\n")
            if line == "":
                if not data_lines:
                    event_name = None
                    continue
                data = "\n".join(data_lines)
                event = event_name or "message"
                data_lines = []
                event_name = None
                try:
                    payload = json.loads(data)
                except ValueError:
                    _LOGGER.warning("Skipping non-JSON SSE event %s: %r", event, data)
                    continue

                if event == "meta":
                    self._populate_meta(payload)
                elif event == "error":
                    raise _map_error_payload(payload)
                else:
                    delta: dict[str, Any] = {}
                    if first_delta:
                        delta["role"] = "assistant"
                        first_delta = False
                    if "content" in payload:
                        delta["content"] = payload["content"]
                    if "thinking_content" in payload:
                        delta["thinking_content"] = payload["thinking_content"]
                    if delta:
                        yield delta
                continue

            if line.startswith(":"):
                continue
            if line.startswith("event:"):
                event_name = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].lstrip(" "))

    def _populate_meta(self, data: dict[str, Any]) -> None:
        if "conversation_id" in data:
            self.conversation_id = data["conversation_id"]
        if "continue_conversation" in data:
            self.continue_conversation = bool(data["continue_conversation"])
        if "dormant" in data:
            self.dormant = bool(data["dormant"])
        if "llm_used" in data:
            self.llm_used = data["llm_used"]
        tokens = data.get("tokens") or {}
        if tokens:
            self.tokens_in = int(tokens.get("in", 0))
            self.tokens_out = int(tokens.get("out", 0))
        if "tier" in data:
            self.tier = data["tier"]


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

    def converse_stream(
        self,
        api_key: str,
        *,
        text: str,
        conversation_id: str,
        language: str,
        device_id: str | None = None,
        area_id: str | None = None,
        speaker_id: str | None = None,
    ) -> ConverseStream:
        """§7.3.2 — open a converse request that yields assistant deltas.

        Negotiates SSE via ``Accept`` but falls back to JSON transparently.
        See ``ConverseStream`` for iteration semantics and the metadata
        fields populated as the stream is consumed.
        """
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

        headers = {
            "Accept": "text/event-stream, application/json;q=0.9",
            "Authorization": f"Bearer {api_key}",
        }
        return ConverseStream(
            self._session,
            f"{self._api_base}/v1/converse",
            body,
            headers,
            CONVERSE_TIMEOUT,
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

                await _raise_for_error(resp, request_id)
                # _raise_for_error always raises
                raise HomapelApiError("unreachable")
        except asyncio.TimeoutError as err:
            raise HomapelTimeoutError(str(err)) from err
        except aiohttp.ClientError as err:
            raise HomapelNetworkError(str(err)) from err


async def _raise_for_error(
    resp: aiohttp.ClientResponse, request_id: str | None
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


def _map_error_payload(payload: dict[str, Any]) -> HomapelApiError:
    """Map an SSE ``error`` event payload to a typed exception.

    Mirrors the §7.2 envelope shape (``{"error": {"code", "message"}}``) so
    a mid-stream error looks the same to the entity as a pre-stream HTTP
    failure. Status is unknown at this point — we map purely on ``code``.
    """
    err = payload.get("error") if isinstance(payload.get("error"), dict) else payload
    code = err.get("code") if isinstance(err, dict) else None
    message = (err.get("message") if isinstance(err, dict) else None) or "stream error"
    ctx = {"code": code, "status": None, "request_id": None}

    if code == "unit_not_active":
        return HomapelUnitNotActiveError(message, **ctx)
    if code == "cost_ceiling_exceeded":
        return HomapelCostCeilingError(message, **ctx)
    if code == "rate_limited":
        return HomapelRateLimitedError(message, retry_after=None, **ctx)
    if code in ("unauthorized", "invalid_token"):
        return HomapelAuthError(message, **ctx)
    return HomapelApiError(message, **ctx)
