"""Shared fixtures: a mocked Laris cloud and a stand-in ha_mcp_tools server entry."""
from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from aiohttp.web import Request, Response
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    MockModule,
    mock_config_flow,
    mock_integration,
    mock_platform,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)

from homeassistant.components import webhook
from homeassistant.config_entries import ConfigEntry, ConfigFlow
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from custom_components.homapel_conversation.const import (
    CONF_API_BASE,
    CONF_API_KEY,
    CONF_DEFAULT_LANGUAGE,
    CONF_UNIT_ID,
    DOMAIN,
    MCP_DATA_WEBHOOK_ID,
    MCP_DOMAIN,
    MCP_ENTRY_TYPE_KEY,
    MCP_ENTRY_TYPE_SERVER,
    MCP_OPT_ENABLE_WEBHOOK,
    MCP_OPT_WEBHOOK_AUTH,
    MCP_WEBHOOK_AUTH_HA,
)

API_BASE = "https://api.test"
API_KEY = "hmpk_test_key"
UNIT_ID = "unit-1234"
MCP_WEBHOOK_ID = "mcp_0123456789abcdef"
EXTERNAL_URL = "https://home.example.com"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Let the harness load custom_components/homapel_conversation."""


@pytest.fixture(autouse=True)
async def core_components(hass: HomeAssistant) -> None:
    """``conversation`` needs the ``homeassistant`` component's exposed-entity store."""
    assert await async_setup_component(hass, "homeassistant", {})


def status_payload(
    *,
    active: bool = True,
    tier: str | None = "basic",
    voice: bool = True,
    connector: dict[str, bool] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """A /v1/units/status body. ``connector=None`` keeps the block out."""
    payload: dict[str, Any] = {
        "unit_id": UNIT_ID,
        "active": active,
        "tier": tier,
        "webhook_token": "whtok",
        "cost_ceiling_reached": False,
        "updated_at": "2026-08-23T00:00:00Z",
        "stt": {
            "enabled": voice,
            "languages": ["tr-TR", "en-US"],
            "max_audio_seconds": 30,
        },
        "tts": {
            "enabled": voice,
            "default_language": "tr-TR",
            "default_voice": "tr-voice",
            "languages": ["tr-TR", "en-US"],
            "voices": {
                "tr-TR": [{"id": "tr-voice", "name": "Türkçe"}],
                "en-US": [{"id": "en-voice", "name": "English"}],
            },
            "max_characters": 5000,
        },
    }
    if connector is not None:
        payload["connector"] = connector
    payload.update(extra)
    return payload


class CloudMock:
    """Wraps aioclient_mock with the cloud's routes, so tests tweak one object."""

    def __init__(self, mocker: AiohttpClientMocker) -> None:
        self.mocker = mocker
        self.status = status_payload(connector={"configured": False, "reachable": False})
        self.connector_response: dict[str, Any] = {
            "reachable": True,
            "checked_at": "2026-08-23T00:00:01Z",
            "tool_count": 42,
        }
        self.connector_status_code = 200
        self.status_code = 200
        self.tunnel_status_code = 200
        self.tunnel_response: dict[str, Any] = {
            "hostname": "unit-1234.tunnel.test",
            "tunnel_token": "cf-token",
        }
        self.error_body: dict[str, Any] = {"error": {"code": "unknown", "message": "boom"}}
        self._register()

    def _register(self) -> None:
        m = self.mocker

        async def _status(method: str, url: Any, data: Any) -> Any:
            if self.status_code != 200:
                return AiohttpClientMockResponse(
                    method, url, status=self.status_code, json=self.error_body
                )
            return AiohttpClientMockResponse(method, url, json=self.status)

        async def _connector(method: str, url: Any, data: Any) -> Any:
            if self.connector_status_code != 200:
                return AiohttpClientMockResponse(
                    method, url, status=self.connector_status_code, json=self.error_body
                )
            return AiohttpClientMockResponse(method, url, json=self.connector_response)

        async def _tunnel(method: str, url: Any, data: Any) -> Any:
            if self.tunnel_status_code != 200:
                return AiohttpClientMockResponse(
                    method, url, status=self.tunnel_status_code, json=self.error_body
                )
            return AiohttpClientMockResponse(method, url, json=self.tunnel_response)

        m.get(f"{API_BASE}/v1/units/status", side_effect=_status)
        m.post(f"{API_BASE}/v1/units/webhook", json={"ok": True})
        m.put(f"{API_BASE}/v1/units/connector", side_effect=_connector)
        m.get(
            f"{API_BASE}/v1/units/connector",
            json={"configured": True, "reachable": True, "source": "external_url"},
        )
        m.delete(f"{API_BASE}/v1/units/connector", status=204)
        m.post(f"{API_BASE}/v1/units/tunnel", side_effect=_tunnel)

    def calls(self, method: str, path: str) -> list[Any]:
        return [
            call
            for call in self.mocker.mock_calls
            if str(call[0]).upper() == method.upper() and str(call[1]) == f"{API_BASE}{path}"
        ]


@pytest.fixture
def cloud(aioclient_mock: AiohttpClientMocker) -> CloudMock:
    return CloudMock(aioclient_mock)


@pytest.fixture
def config_entry(hass: HomeAssistant) -> MockConfigEntry:
    """An entry as the config flow creates it (connector keys added per test)."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=UNIT_ID,
        title=f"Homapel ({UNIT_ID})",
        data={
            CONF_API_BASE: API_BASE,
            CONF_API_KEY: API_KEY,
            CONF_UNIT_ID: UNIT_ID,
            CONF_DEFAULT_LANGUAGE: "tr",
        },
    )
    entry.add_to_hass(hass)
    return entry


# --- ha_mcp_tools stand-in ----------------------------------------------------


async def _mcp_webhook_handler(hass: HomeAssistant, webhook_id: str, request: Request) -> Response:
    return Response(status=200)


@pytest.fixture
async def mcp_integration(
    hass: HomeAssistant,
) -> AsyncIterator[Callable[[ConfigEntry], Awaitable[None]]]:
    """Register a fake ``ha_mcp_tools`` that behaves like the real server entry:

    * registers its webhook on setup when ``enable_webhook`` is on;
    * reloads itself when its options change (what the real one does).
    """
    assert await async_setup_component(hass, "webhook", {})

    async def _reload(hass: HomeAssistant, entry: ConfigEntry) -> None:
        await hass.config_entries.async_reload(entry.entry_id)

    async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
        webhook_id = entry.data.get(MCP_DATA_WEBHOOK_ID)
        if webhook_id and entry.options.get(MCP_OPT_ENABLE_WEBHOOK, True):
            webhook.async_register(
                hass, MCP_DOMAIN, "HA-MCP", webhook_id, _mcp_webhook_handler
            )
        entry.async_on_unload(entry.add_update_listener(_reload))
        return True

    async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
        webhook_id = entry.data.get(MCP_DATA_WEBHOOK_ID)
        if webhook_id:
            webhook.async_unregister(hass, webhook_id)
        return True

    mock_integration(
        hass,
        MockModule(
            MCP_DOMAIN,
            async_setup_entry=async_setup_entry,
            async_unload_entry=async_unload_entry,
        ),
    )
    # Entry setup imports the config_flow platform to check the schema version.
    mock_platform(hass, f"{MCP_DOMAIN}.config_flow", None)

    async def _setup(entry: ConfigEntry) -> None:
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    with mock_config_flow(MCP_DOMAIN, _McpFlow):
        yield _setup


class _McpFlow(ConfigFlow):
    """Version stub so HA does not try to migrate the fake entry."""

    VERSION = 1
    MINOR_VERSION = 1


def make_mcp_entry(
    *, ha_auth: bool = True, enable_webhook: bool = True, webhook_id: str = MCP_WEBHOOK_ID
) -> MockConfigEntry:
    return MockConfigEntry(
        domain=MCP_DOMAIN,
        title="HA-MCP Server",
        data={MCP_ENTRY_TYPE_KEY: MCP_ENTRY_TYPE_SERVER, MCP_DATA_WEBHOOK_ID: webhook_id},
        options={
            MCP_OPT_WEBHOOK_AUTH: MCP_WEBHOOK_AUTH_HA if ha_auth else "none",
            MCP_OPT_ENABLE_WEBHOOK: enable_webhook,
        },
    )


@pytest.fixture
async def mcp_entry(
    hass: HomeAssistant, mcp_integration: Callable[[ConfigEntry], Awaitable[None]]
) -> MockConfigEntry:
    """A loaded ha_mcp_tools server entry already in ha_auth mode."""
    entry = make_mcp_entry()
    entry.add_to_hass(hass)
    await mcp_integration(entry)
    return entry


@pytest.fixture
def external_url(hass: HomeAssistant) -> str:
    """Give HA an https external URL so the flow picks source=external_url."""
    hass.config.external_url = EXTERNAL_URL
    return EXTERNAL_URL
