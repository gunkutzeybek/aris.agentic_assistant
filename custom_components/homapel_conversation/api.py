"""Async HTTP client for the Laris cloud API (``agentic_service``).

Every call authenticates with the unit API key as a Bearer token. Error bodies
are ``{"error": {"code", "message"}}`` and are mapped to the typed exceptions
below; ``X-Request-Id`` is captured into each exception for support.
"""
from __future__ import annotations

from collections.abc import AsyncIterable, AsyncIterator, Mapping
from dataclasses import dataclass, field
import json
import logging
from typing import Any

import aiohttp

from .const import (
    CONNECTOR_TIMEOUT,
    PCM_BYTES_PER_SAMPLE,
    STATUS_TIMEOUT,
    STT_TIMEOUT,
    TTS_AUDIO_FORMAT,
    TTS_TIMEOUT,
    TUNNEL_TIMEOUT,
)

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


class HomapelTurnNotFoundError(HomapelApiError):
    """410 turn_not_found — expired, foreign, or already-claimed turn.

    Only raised by Mode B TTS. The caller must fall back to Mode A with the
    full text; this is a designed path, not an exceptional one.
    """


class HomapelPayloadTooLargeError(HomapelApiError):
    """413 audio_too_long / text_too_long."""


class HomapelInvalidRequestError(HomapelApiError):
    """422 other than unit_not_active — e.g. ``invalid_mcp_url``."""


class HomapelTunnelNotConfiguredError(HomapelApiError):
    """501 tunnel_not_configured — this cloud cannot issue tunnels."""


@dataclass(slots=True)
class ConnectorSummary:
    """The ``connector`` block of /v1/units/status."""

    configured: bool
    reachable: bool


@dataclass(slots=True)
class ConnectorProbeResult:
    """Response of PUT /v1/units/connector — the cloud's synchronous probe."""

    reachable: bool
    checked_at: str | None
    error: str | None = None
    tool_count: int | None = None


@dataclass(slots=True)
class ConnectorStatus:
    """Response of GET /v1/units/connector — stored state, no probe."""

    configured: bool
    reachable: bool
    source: str | None = None
    last_ok_at: str | None = None
    last_error: str | None = None


@dataclass(slots=True)
class TunnelResult:
    """Response of POST /v1/units/tunnel."""

    hostname: str
    tunnel_token: str


@dataclass(slots=True)
class SttCapability:
    """The ``stt`` block of /v1/units/status."""

    enabled: bool
    languages: list[str]
    max_audio_seconds: int


@dataclass(slots=True)
class TtsVoice:
    id: str
    name: str


@dataclass(slots=True)
class TtsCapability:
    """The ``tts`` block of /v1/units/status."""

    enabled: bool
    default_language: str
    default_voice: str
    languages: list[str]
    voices: dict[str, list[TtsVoice]] = field(default_factory=dict)
    max_characters: int = 5000


@dataclass(slots=True)
class SttResult:
    text: str
    language: str
    turn_id: str | None
    duration_ms: int
    audio_seconds: float
    provider_ms: int | None


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
    stt: SttCapability | None = None
    tts: TtsCapability | None = None
    # None on a cloud that predates the connector block.
    connector: ConnectorSummary | None = None


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
        sock_read: float,
    ) -> None:
        self._session = session
        self._url = url
        self._body = body
        self._headers = headers
        self._sock_read = sock_read
        # Populated during/after iteration.
        self.conversation_id: str | None = None
        self.continue_conversation: bool = False
        self.dormant: bool = False
        self.llm_used: str | None = None
        self.tokens_in: int = 0
        self.tokens_out: int = 0
        self.tier: str | None = None
        # Present only when the cloud actually ran the TTS feed for this request
        # (§6). Absence means "go straight to Mode A" — attaching without the
        # advertisement is a guaranteed 410 round trip.
        self.turn_id: str | None = None

    async def __aiter__(self) -> AsyncIterator[dict[str, Any]]:
        try:
            async with self._session.request(
                "POST",
                self._url,
                json=self._body,
                headers=self._headers,
                timeout=aiohttp.ClientTimeout(sock_read=self._sock_read),
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
        except TimeoutError as err:
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
        if data.get("turn_id"):
            self.turn_id = data["turn_id"]


class TtsStream:
    """Async-iterable of MP3 bytes from /v1/tts (Mode A or Mode B).

    Errors are guaranteed to arrive before the first audio byte as a JSON
    envelope, so status is checked up-front. After the first byte the only
    failure signal is a closed connection, which surfaces as a network error
    and is treated by callers as truncated audio.
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
        self.characters: int = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        try:
            async with self._session.post(
                self._url,
                json=self._body,
                headers=self._headers,
                timeout=aiohttp.ClientTimeout(total=self._timeout),
            ) as resp:
                request_id = resp.headers.get("X-Request-Id")
                if not (200 <= resp.status < 300):
                    await _raise_for_error(resp, request_id)

                raw = resp.headers.get("X-Homapel-Characters")
                if raw and raw.isdigit():
                    self.characters = int(raw)

                async for chunk in resp.content.iter_chunked(4096):
                    yield chunk
        except TimeoutError as err:
            raise HomapelTimeoutError(str(err)) from err
        except aiohttp.ClientError as err:
            raise HomapelNetworkError(str(err)) from err

    async def collect(self) -> bytes:
        buffer = bytearray()
        async for chunk in self:
            buffer.extend(chunk)
        return bytes(buffer)


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
            stt=_parse_stt_capability(data.get("stt")),
            tts=_parse_tts_capability(data.get("tts")),
            connector=_parse_connector_summary(data.get("connector")),
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

    # --- Home connector -----------------------------------------------------

    async def put_connector(
        self,
        api_key: str,
        *,
        mcp_url: str,
        bearer: str,
        source: str,
        ha_version: str | None = None,
        component_version: str | None = None,
    ) -> ConnectorProbeResult:
        """Register how the cloud reaches this home's ha-mcp endpoint.

        The cloud stores the URL + bearer and probes ``tools/list`` before it
        answers, so the result already says whether the home is reachable.
        An unreachable home is *stored* all the same — the caller decides
        whether to retry or finish.
        """
        body: dict[str, Any] = {
            "mcp_url": mcp_url,
            "bearer": bearer,
            "source": source,
        }
        if ha_version:
            body["ha_version"] = ha_version
        if component_version:
            body["component_version"] = component_version
        _, data, _ = await self._request(
            "PUT",
            "/v1/units/connector",
            json=body,
            timeout=CONNECTOR_TIMEOUT,
            auth_key=api_key,
        )
        tool_count = data.get("tool_count")
        return ConnectorProbeResult(
            reachable=bool(data.get("reachable", False)),
            checked_at=data.get("checked_at"),
            error=data.get("error"),
            tool_count=int(tool_count) if isinstance(tool_count, int) else None,
        )

    async def get_connector(self, api_key: str) -> ConnectorStatus:
        """Stored connector status — no probe, no HA round trip."""
        _, data, _ = await self._request(
            "GET",
            "/v1/units/connector",
            timeout=STATUS_TIMEOUT,
            auth_key=api_key,
        )
        return ConnectorStatus(
            configured=bool(data.get("configured", False)),
            reachable=bool(data.get("reachable", False)),
            source=data.get("source"),
            last_ok_at=data.get("last_ok_at"),
            last_error=data.get("last_error"),
        )

    async def delete_connector(self, api_key: str) -> None:
        """Forget the connector (integration removed)."""
        await self._request(
            "DELETE",
            "/v1/units/connector",
            timeout=STATUS_TIMEOUT,
            auth_key=api_key,
        )

    async def create_tunnel(self, api_key: str) -> TunnelResult:
        """Issue (or re-issue) this home's cloud-managed Cloudflare tunnel.

        Raises ``HomapelTunnelNotConfiguredError`` when the cloud has no
        Cloudflare credentials — the caller falls back to a manual URL.
        """
        _, data, _ = await self._request(
            "POST",
            "/v1/units/tunnel",
            timeout=TUNNEL_TIMEOUT,
            auth_key=api_key,
        )
        hostname = data.get("hostname")
        token = data.get("tunnel_token")
        if not hostname or not token:
            raise HomapelApiError("tunnel response missing hostname/tunnel_token")
        return TunnelResult(hostname=str(hostname), tunnel_token=str(token))

    def converse_stream(
        self,
        api_key: str,
        *,
        text: str,
        conversation_id: str,
        language: str,
        sock_read: float,
        device_id: str | None = None,
        area_id: str | None = None,
        speaker_id: str | None = None,
        turn_id: str | None = None,
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
        if turn_id is not None:
            # Unknown/expired/foreign/already-used ids are ignored silently by
            # the cloud and the request runs normally from `text` — never an
            # error, so this is always safe to send.
            body["turn_id"] = turn_id
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
            sock_read,
        )

    async def transcribe(
        self,
        api_key: str,
        *,
        stream: AsyncIterable[bytes],
        language: str,
        sample_rate: int,
        channels: int,
        max_audio_seconds: int = 0,
        device_id: str | None = None,
        area_id: str | None = None,
        eager: bool = False,
        timeout: float = STT_TIMEOUT,
    ) -> SttResult:
        """§3 — stream raw PCM to /v1/stt and return the transcript.

        The body is streamed with chunked transfer-encoding as HA produces it,
        so the cloud can forward to the provider while the user is still
        speaking. Nothing is buffered locally.

        ``max_audio_seconds`` bounds the upload client-side. The cloud enforces
        the same limit and may answer 413 mid-body, but stopping locally keeps
        us off that path in the common case — aiohttp's handling of a response
        that arrives while the request body is still being written is not
        something to rely on for a routine limit.
        """
        params: dict[str, str] = {"language": language}
        if device_id:
            params["device_id"] = device_id
        if area_id:
            params["area_id"] = area_id
        if eager:
            params["eager"] = "true"

        max_bytes = (
            int(max_audio_seconds * sample_rate * channels * PCM_BYTES_PER_SAMPLE)
            if max_audio_seconds > 0
            else 0
        )

        async def _chunks() -> AsyncIterator[bytes]:
            sent = 0
            async for chunk in stream:
                if not chunk:
                    continue
                if max_bytes and sent + len(chunk) >= max_bytes:
                    remainder = max_bytes - sent
                    if remainder > 0:
                        yield bytes(chunk[:remainder])
                    _LOGGER.warning(
                        "Utterance exceeded %s s; truncating upload", max_audio_seconds
                    )
                    return
                sent += len(chunk)
                yield chunk

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "Content-Type": f"audio/L16; rate={sample_rate}; channels={channels}",
        }

        try:
            async with self._session.post(
                f"{self._api_base}/v1/stt",
                params=params,
                data=_chunks(),
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                request_id = resp.headers.get("X-Request-Id")
                if not (200 <= resp.status < 300):
                    await _raise_for_error(resp, request_id)
                payload = await resp.json()
        except TimeoutError as err:
            raise HomapelTimeoutError(str(err)) from err
        except aiohttp.ClientError as err:
            raise HomapelNetworkError(str(err)) from err

        timings = payload.get("timings") or {}
        provider_ms = timings.get("provider_ms")
        return SttResult(
            # Silence and Whisper hallucinations both come back as "" with a
            # 200; the caller maps that to HA's "didn't understand".
            text=payload.get("text") or "",
            language=payload.get("language") or language,
            turn_id=payload.get("turn_id"),
            duration_ms=int(payload.get("duration_ms") or 0),
            audio_seconds=float(payload.get("audio_seconds") or 0.0),
            provider_ms=int(provider_ms) if isinstance(provider_ms, (int, float)) else None,
        )

    def synthesize(
        self,
        api_key: str,
        *,
        text: str | None = None,
        language: str | None = None,
        voice: str | None = None,
        turn_id: str | None = None,
        timeout: float = TTS_TIMEOUT,
    ) -> TtsStream:
        """§4/§5 — Mode A (``text``) or Mode B (``turn_id``) synthesis.

        Mode B attaches to audio the cloud pre-synthesized during the converse
        stream. Pass ``voice`` in Mode B only when it differs from the tenant
        default: the feed cannot know the pipeline's voice choice at converse
        time, so naming a different one makes the cloud discard its buffer and
        re-synthesize (correct answer, one synthesis slower).
        """
        body: dict[str, Any] = {}
        if turn_id is not None:
            body["turn_id"] = turn_id
        else:
            body["text"] = text or ""
            body["format"] = TTS_AUDIO_FORMAT
            if language is not None:
                body["language"] = language
        if voice is not None:
            body["voice"] = voice

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Accept": "audio/mpeg, application/json;q=0.9",
        }
        return TtsStream(
            self._session,
            f"{self._api_base}/v1/tts",
            body,
            headers,
            timeout,
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
    ) -> tuple[int, dict[str, Any], Mapping[str, str]]:
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
                    # Returned as aiohttp's case-insensitive mapping, not a plain
                    # dict: response header names are title-cased by the parser,
                    # so `dict(...)["ETag"]` silently misses.
                    return 304, {}, resp.headers
                if 200 <= resp.status < 300:
                    # 204 / empty bodies (DELETE) decode to None or raise —
                    # both mean "no payload".
                    try:
                        payload = await resp.json()
                    except (aiohttp.ContentTypeError, ValueError):
                        payload = None
                    if not isinstance(payload, dict):
                        payload = {}
                    return resp.status, payload, resp.headers

                await _raise_for_error(resp, request_id)
                # _raise_for_error always raises
                raise HomapelApiError("unreachable")
        except TimeoutError as err:
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
    if status == 410:
        raise HomapelTurnNotFoundError(message, **ctx)
    if status == 413:
        raise HomapelPayloadTooLargeError(message, **ctx)
    if status == 422:
        if code == "unit_not_active":
            raise HomapelUnitNotActiveError(message, **ctx)
        raise HomapelInvalidRequestError(message, **ctx)
    if status == 429:
        if code == "cost_ceiling_exceeded":
            raise HomapelCostCeilingError(message, **ctx)
        raise HomapelRateLimitedError(message, retry_after=retry_after, **ctx)
    if status == 501:
        raise HomapelTunnelNotConfiguredError(message, **ctx)
    if 500 <= status < 600:
        raise HomapelNetworkError(message, **ctx)
    raise HomapelApiError(message, **ctx)


def _parse_stt_capability(raw: Any) -> SttCapability | None:
    """Parse the ``stt`` status block. Absent/malformed → treated as disabled."""
    if not isinstance(raw, dict):
        return None
    languages = [str(x) for x in raw.get("languages") or [] if x]
    return SttCapability(
        enabled=bool(raw.get("enabled", False)),
        languages=languages,
        max_audio_seconds=int(raw.get("max_audio_seconds") or 0),
    )


def _parse_connector_summary(raw: Any) -> ConnectorSummary | None:
    """Parse the ``connector`` status block. Absent → None (older cloud)."""
    if not isinstance(raw, dict):
        return None
    return ConnectorSummary(
        configured=bool(raw.get("configured", False)),
        reachable=bool(raw.get("reachable", False)),
    )


def _parse_tts_capability(raw: Any) -> TtsCapability | None:
    """Parse the ``tts`` status block. Absent/malformed → treated as disabled."""
    if not isinstance(raw, dict):
        return None

    voices: dict[str, list[TtsVoice]] = {}
    for language, entries in (raw.get("voices") or {}).items():
        if not isinstance(entries, list):
            continue
        parsed = [
            TtsVoice(id=str(v["id"]), name=str(v.get("name") or v["id"]))
            for v in entries
            if isinstance(v, dict) and v.get("id")
        ]
        if parsed:
            voices[str(language)] = parsed

    # STT and TTS language sets can legitimately differ (e.g. en-GB is STT-only),
    # so each entity is built strictly from its own block.
    languages = [str(x) for x in raw.get("languages") or [] if x]
    return TtsCapability(
        enabled=bool(raw.get("enabled", False)),
        default_language=str(raw.get("default_language") or ""),
        default_voice=str(raw.get("default_voice") or ""),
        languages=languages,
        voices=voices,
        max_characters=int(raw.get("max_characters") or 5000),
    )


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
