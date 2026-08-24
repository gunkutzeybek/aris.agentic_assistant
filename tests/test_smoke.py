"""Setup + config flow smoke tests (kept while the real suites are written)."""
from __future__ import annotations

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.setup import async_setup_component

from custom_components.homapel_conversation.const import DOMAIN

from .conftest import API_BASE, API_KEY, EXTERNAL_URL, MCP_WEBHOOK_ID


async def test_setup_entry(hass: HomeAssistant, cloud, config_entry) -> None:
    assert await async_setup_component(hass, "assist_pipeline", {})
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.LOADED
    assert hass.states.get("binary_sensor.homapel_aris_active").state == "on"
    assert hass.states.get("binary_sensor.homapel_aris_home_connected").state == "off"
    assert hass.states.get("conversation.homapel_aris_homapel") is not None
    assert hass.states.get("stt.homapel_aris_homapel") is not None
    assert hass.states.get("tts.homapel_aris_homapel") is not None
    from homeassistant.components import assist_pipeline

    names = [p.name for p in assist_pipeline.async_get_pipelines(hass)]
    assert "Laris" in names
    assert config_entry.data.get("pipeline_created") is True


async def test_flow_happy_path(hass: HomeAssistant, cloud, mcp_entry, external_url) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"api_key": API_KEY, "advanced": {"api_base": API_BASE, "default_language": "tr"}},
    )
    while result["type"] is FlowResultType.SHOW_PROGRESS:
        await hass.async_block_till_done()
        result = await hass.config_entries.flow.async_configure(result["flow_id"])
    assert result["type"] is FlowResultType.CREATE_ENTRY, result
    data = result["data"]
    assert data["connector_source"] == "external_url"
    assert data["connector_base_url"] == EXTERNAL_URL
    assert data["mcp_webhook_id"] == MCP_WEBHOOK_ID
    assert data["cloud_user_id"] and data["cloud_refresh_token_id"]
    assert "access_token" not in str(data)

    # One PUT from the flow, one from the connector manager when the new entry
    # sets up (HA is already running in tests, so it re-probes immediately).
    put = cloud.calls("PUT", "/v1/units/connector")
    assert len(put) == 2
    body = put[0][2]
    assert body["mcp_url"] == f"{EXTERNAL_URL}/api/webhook/{MCP_WEBHOOK_ID}"
    assert body["source"] == "external_url"
    assert body["bearer"].count(".") == 2  # a JWT
    # the bearer is an admin token ha-mcp's ha_auth gate would accept
    refresh = hass.auth.async_validate_access_token(body["bearer"])
    assert refresh is not None and refresh.user.is_admin and refresh.user.name == "Laris Cloud"
