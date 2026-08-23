"""Unit tests for ``api.py`` — the cloud client's wire contract and error ladder."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp
import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from custom_components.homapel_conversation.api import (
    ConnectorProbeResult,
    ConnectorStatus,
    ConnectorSummary,
    HomapelApiError,
    HomapelAuthError,
    HomapelCloudClient,
    HomapelCostCeilingError,
    HomapelForbiddenError,
    HomapelInvalidRequestError,
    HomapelNetworkError,
    HomapelRateLimitedError,
    HomapelTimeoutError,
    HomapelTunnelNotConfiguredError,
    HomapelUnitNotActiveError,
    TunnelResult,
)

from .conftest import API_BASE, API_KEY, UNIT_ID, status_payload

STATUS_URL = f"{API_BASE}/v1/units/status"
CONNECTOR_URL = f"{API_BASE}/v1/units/connector"
TUNNEL_URL = f"{API_BASE}/v1/units/tunnel"
MCP_URL = "https://home.example.com/api/webhook/mcp_0123456789abcdef"


@pytest.fixture
def client(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> HomapelCloudClient:
    """A client bound to the mocked aiohttp session (no routes pre-registered)."""
    return HomapelCloudClient(async_get_clientsession(hass), API_BASE)


def _error(code: str, message: str = "boom") -> dict[str, Any]:
    return {"error": {"code": code, "message": message}}


# --- GET /v1/units/status ------------------------------------------------------


async def test_get_status_parses_connector_block(
    client: HomapelCloudClient, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(
        STATUS_URL, json=status_payload(connector={"configured": True, "reachable": False})
    )

    result = await client.get_status(API_KEY)

    assert result.unit_id == UNIT_ID
    assert result.active is True
    assert result.not_modified is False
    assert result.connector == ConnectorSummary(configured=True, reachable=False)
    assert result.stt is not None and result.stt.enabled
    assert result.tts is not None and result.tts.enabled
    headers = aioclient_mock.mock_calls[0][3]
    assert headers["Authorization"] == f"Bearer {API_KEY}"
    assert "If-None-Match" not in headers


async def test_get_status_connector_none_when_absent(
    client: HomapelCloudClient, aioclient_mock: AiohttpClientMocker
) -> None:
    """An older cloud without the connector block must not be mistaken for 'configured'."""
    aioclient_mock.get(STATUS_URL, json=status_payload(connector=None))

    result = await client.get_status(API_KEY)

    assert result.connector is None


async def test_get_status_etag_round_trip(
    client: HomapelCloudClient, aioclient_mock: AiohttpClientMocker
) -> None:
    """First poll captures the ETag; the next poll sends it and a 304 means not_modified."""
    responses = iter(
        [
            {
                "json": status_payload(connector={"configured": True, "reachable": True}),
                "headers": {"ETag": '"x"'},
            },
            {"status": 304, "headers": {"ETag": '"x"'}},
        ]
    )

    async def _status(method: str, url: Any, data: Any) -> AiohttpClientMockResponse:
        return AiohttpClientMockResponse(method, url, **next(responses))

    aioclient_mock.get(STATUS_URL, side_effect=_status)

    first = await client.get_status(API_KEY)
    assert first.etag == '"x"'
    assert first.not_modified is False
    assert first.connector == ConnectorSummary(configured=True, reachable=True)

    second = await client.get_status(API_KEY, etag=first.etag)
    assert second.not_modified is True
    assert second.etag == '"x"'
    assert second.connector is None  # unpopulated: caller must reuse cached state
    assert aioclient_mock.mock_calls[1][3]["If-None-Match"] == '"x"'


# --- PUT / GET / DELETE /v1/units/connector ------------------------------------


async def test_put_connector_sends_payload_and_parses_probe(
    client: HomapelCloudClient, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.put(
        CONNECTOR_URL,
        json={
            "reachable": True,
            "checked_at": "2026-08-23T00:00:01Z",
            "error": None,
            "tool_count": 42,
        },
    )

    result = await client.put_connector(
        API_KEY,
        mcp_url=MCP_URL,
        bearer="jwt.token.here",
        source="external_url",
        ha_version="2026.8.3",
        component_version="0.5.0",
    )

    assert result == ConnectorProbeResult(
        reachable=True, checked_at="2026-08-23T00:00:01Z", error=None, tool_count=42
    )
    method, url, body, headers = aioclient_mock.mock_calls[0]
    assert method == "PUT"
    assert str(url) == CONNECTOR_URL
    assert body == {
        "mcp_url": MCP_URL,
        "bearer": "jwt.token.here",
        "source": "external_url",
        "ha_version": "2026.8.3",
        "component_version": "0.5.0",
    }
    assert headers["Authorization"] == f"Bearer {API_KEY}"


async def test_put_connector_omits_none_versions_and_missing_tool_count(
    client: HomapelCloudClient, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.put(
        CONNECTOR_URL,
        json={
            "reachable": False,
            "checked_at": "2026-08-23T00:00:02Z",
            "error": "connect timeout",
        },
    )

    result = await client.put_connector(
        API_KEY, mcp_url=MCP_URL, bearer="jwt.token.here", source="manual"
    )

    assert result.reachable is False
    assert result.error == "connect timeout"
    assert result.tool_count is None
    body = aioclient_mock.mock_calls[0][2]
    assert body == {"mcp_url": MCP_URL, "bearer": "jwt.token.here", "source": "manual"}
    assert "ha_version" not in body and "component_version" not in body


async def test_get_connector_parses_status(
    client: HomapelCloudClient, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(
        CONNECTOR_URL,
        json={
            "configured": True,
            "reachable": False,
            "source": "tunnel",
            "last_ok_at": "2026-08-22T23:00:00Z",
            "last_error": "401 from home",
        },
    )

    result = await client.get_connector(API_KEY)

    assert result == ConnectorStatus(
        configured=True,
        reachable=False,
        source="tunnel",
        last_ok_at="2026-08-22T23:00:00Z",
        last_error="401 from home",
    )
    assert aioclient_mock.mock_calls[0][3]["Authorization"] == f"Bearer {API_KEY}"


async def test_delete_connector_tolerates_empty_204(
    client: HomapelCloudClient, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.delete(CONNECTOR_URL, status=204)

    await client.delete_connector(API_KEY)

    method, url, _, headers = aioclient_mock.mock_calls[0]
    assert method == "DELETE"
    assert str(url) == CONNECTOR_URL
    assert headers["Authorization"] == f"Bearer {API_KEY}"


# --- POST /v1/units/tunnel -------------------------------------------------------


async def test_create_tunnel_parses_result(
    client: HomapelCloudClient, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.post(
        TUNNEL_URL, json={"hostname": "unit-1234.tunnel.test", "tunnel_token": "cf-token"}
    )

    result = await client.create_tunnel(API_KEY)

    assert result == TunnelResult(hostname="unit-1234.tunnel.test", tunnel_token="cf-token")
    assert aioclient_mock.mock_calls[0][3]["Authorization"] == f"Bearer {API_KEY}"


@pytest.mark.parametrize(
    "body",
    [
        {"tunnel_token": "cf-token"},
        {"hostname": "unit-1234.tunnel.test"},
        {"hostname": "", "tunnel_token": "cf-token"},
        {},
    ],
    ids=["no_hostname", "no_token", "empty_hostname", "empty_body"],
)
async def test_create_tunnel_rejects_incomplete_response(
    client: HomapelCloudClient, aioclient_mock: AiohttpClientMocker, body: dict[str, Any]
) -> None:
    aioclient_mock.post(TUNNEL_URL, json=body)

    with pytest.raises(HomapelApiError) as excinfo:
        await client.create_tunnel(API_KEY)

    # A malformed 200 is a plain API error, not a typed one the flow would act on.
    assert type(excinfo.value) is HomapelApiError


# --- Error mapping -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "code", "expected"),
    [
        (401, "invalid_token", HomapelAuthError),
        (403, "unit_suspended", HomapelForbiddenError),
        (422, "unit_not_active", HomapelUnitNotActiveError),
        (422, "invalid_mcp_url", HomapelInvalidRequestError),
        (429, "cost_ceiling_exceeded", HomapelCostCeilingError),
        (429, "rate_limited", HomapelRateLimitedError),
        (501, "tunnel_not_configured", HomapelTunnelNotConfiguredError),
        (500, "internal_error", HomapelNetworkError),
        (502, "tunnel_provider_error", HomapelNetworkError),
    ],
)
async def test_error_mapping(
    client: HomapelCloudClient,
    aioclient_mock: AiohttpClientMocker,
    status: int,
    code: str,
    expected: type[HomapelApiError],
) -> None:
    aioclient_mock.put(
        CONNECTOR_URL,
        status=status,
        json=_error(code, "explained"),
        headers={"X-Request-Id": "req-abc"},
    )

    with pytest.raises(expected) as excinfo:
        await client.put_connector(API_KEY, mcp_url=MCP_URL, bearer="b", source="manual")

    err = excinfo.value
    assert type(err) is expected
    assert err.code == code
    assert err.status == status
    assert err.request_id == "req-abc"
    assert str(err) == "explained"


async def test_tunnel_not_configured_from_tunnel_endpoint(
    client: HomapelCloudClient, aioclient_mock: AiohttpClientMocker
) -> None:
    """The 501 the flow falls back on for a manual URL comes from POST /units/tunnel."""
    aioclient_mock.post(TUNNEL_URL, status=501, json=_error("tunnel_not_configured"))

    with pytest.raises(HomapelTunnelNotConfiguredError) as excinfo:
        await client.create_tunnel(API_KEY)

    assert excinfo.value.code == "tunnel_not_configured"


async def test_invalid_request_carries_code(
    client: HomapelCloudClient, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.put(
        CONNECTOR_URL, status=422, json=_error("invalid_mcp_url", "mcp_url must be https")
    )

    with pytest.raises(HomapelInvalidRequestError) as excinfo:
        await client.put_connector(API_KEY, mcp_url="http://plain", bearer="b", source="manual")

    assert excinfo.value.code == "invalid_mcp_url"
    assert not isinstance(excinfo.value, HomapelUnitNotActiveError)


async def test_rate_limited_reads_retry_after_header(
    client: HomapelCloudClient, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(
        STATUS_URL,
        status=429,
        json=_error("rate_limited"),
        headers={"Retry-After": "7", "X-Request-Id": "req-429"},
    )

    with pytest.raises(HomapelRateLimitedError) as excinfo:
        await client.get_status(API_KEY)

    assert excinfo.value.retry_after == 7
    assert excinfo.value.request_id == "req-429"


async def test_rate_limited_without_numeric_retry_after(
    client: HomapelCloudClient, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(
        STATUS_URL,
        status=429,
        json=_error("rate_limited"),
        headers={"Retry-After": "Sun, 23 Aug 2026 00:00:00 GMT"},
    )

    with pytest.raises(HomapelRateLimitedError) as excinfo:
        await client.get_status(API_KEY)

    assert excinfo.value.retry_after is None


async def test_non_json_5xx_maps_to_network_error(
    client: HomapelCloudClient, aioclient_mock: AiohttpClientMocker
) -> None:
    """A proxy's HTML 502 has no envelope; still a network error, with a fallback message."""
    aioclient_mock.get(
        STATUS_URL,
        status=502,
        text="<html>Bad Gateway</html>",
        headers={"Content-Type": "text/html", "X-Request-Id": "req-502"},
    )

    with pytest.raises(HomapelNetworkError) as excinfo:
        await client.get_status(API_KEY)

    err = excinfo.value
    assert err.code is None
    assert err.status == 502
    assert err.request_id == "req-502"
    assert str(err) == "HTTP 502"


async def test_request_id_absent_is_none(
    client: HomapelCloudClient, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(STATUS_URL, status=401, json=_error("invalid_token"))

    with pytest.raises(HomapelAuthError) as excinfo:
        await client.get_status(API_KEY)

    assert excinfo.value.request_id is None


# --- Transport failures ---------------------------------------------------------


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (TimeoutError(), HomapelTimeoutError),
        (aiohttp.ClientConnectionError("connection refused"), HomapelNetworkError),
        (aiohttp.ClientError("generic"), HomapelNetworkError),
    ],
    ids=["timeout", "connection_error", "client_error"],
)
@pytest.mark.parametrize(
    "call",
    [
        lambda c: c.get_status(API_KEY),
        lambda c: c.put_connector(API_KEY, mcp_url=MCP_URL, bearer="b", source="manual"),
        lambda c: c.get_connector(API_KEY),
        lambda c: c.delete_connector(API_KEY),
        lambda c: c.create_tunnel(API_KEY),
    ],
    ids=["get_status", "put_connector", "get_connector", "delete_connector", "create_tunnel"],
)
async def test_transport_failures_map_to_typed_errors(
    client: HomapelCloudClient,
    aioclient_mock: AiohttpClientMocker,
    exc: Exception,
    expected: type[HomapelApiError],
    call: Callable[[HomapelCloudClient], Awaitable[Any]],
) -> None:
    aioclient_mock.get(STATUS_URL, exc=exc)
    aioclient_mock.put(CONNECTOR_URL, exc=exc)
    aioclient_mock.get(CONNECTOR_URL, exc=exc)
    aioclient_mock.delete(CONNECTOR_URL, exc=exc)
    aioclient_mock.post(TUNNEL_URL, exc=exc)

    with pytest.raises(expected) as excinfo:
        await call(client)

    assert excinfo.value.__cause__ is exc
