"""Reauth (rotated API key) and reconfigure (re-run the connector part) flows."""
from __future__ import annotations

from typing import Any
from unittest.mock import patch

from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from homeassistant.config_entries import SOURCE_REAUTH, SOURCE_RECONFIGURE, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util

from custom_components.homapel_conversation.connector import async_provision_cloud_credential
from custom_components.homapel_conversation.const import (
    CONF_API_KEY,
    CONF_CLOUD_REFRESH_TOKEN_ID,
    CONF_CLOUD_USER_ID,
    CONF_CONNECTOR_BASE_URL,
    CONF_CONNECTOR_SOURCE,
    CONF_MCP_ENTRY_ID,
    CONF_MCP_WEBHOOK_ID,
    CONNECTOR_SOURCE_EXTERNAL_URL,
    DOMAIN,
    ISSUE_HOME_NOT_CONNECTED,
    MCP_DATA_WEBHOOK_ID,
    POLL_INTERVAL,
)

from .conftest import API_KEY, EXTERNAL_URL, MCP_WEBHOOK_ID, UNIT_ID, CloudMock, status_payload

NEW_API_KEY = "hmpk_new"
NEW_WEBHOOK_ID = "mcp_fedcba9876543210"

CONNECTOR_PATH = "/v1/units/connector"
STATUS_PATH = "/v1/units/status"


# --- helpers ------------------------------------------------------------------


async def _start_reconfigure(hass: HomeAssistant, entry: MockConfigEntry) -> dict[str, Any]:
    """Reconfigure opens with a confirmation form; submitting it runs the connector steps."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id}
    )
    assert result["type"] is FlowResultType.FORM, result
    assert result["step_id"] == "reconfigure"
    assert result["description_placeholders"]["base_url"] == entry.data.get(
        CONF_CONNECTOR_BASE_URL, ""
    )
    return await hass.config_entries.flow.async_configure(result["flow_id"], {})


async def _setup_connected_entry(
    hass: HomeAssistant,
    cloud: CloudMock,
    entry: MockConfigEntry,
    mcp_entry: MockConfigEntry | None,
) -> MockConfigEntry:
    """Load ``entry`` the way the config flow leaves it after a successful connector step."""
    credential = await async_provision_cloud_credential(hass)
    hass.config_entries.async_update_entry(
        entry,
        data={
            **entry.data,
            CONF_CONNECTOR_SOURCE: CONNECTOR_SOURCE_EXTERNAL_URL,
            CONF_CONNECTOR_BASE_URL: EXTERNAL_URL,
            CONF_MCP_ENTRY_ID: mcp_entry.entry_id if mcp_entry else "gone",
            CONF_MCP_WEBHOOK_ID: MCP_WEBHOOK_ID,
            CONF_CLOUD_USER_ID: credential.user_id,
            CONF_CLOUD_REFRESH_TOKEN_ID: credential.refresh_token_id,
        },
    )
    # The cloud agrees with the entry: a connector is registered and reachable.
    cloud.status = status_payload(connector={"configured": True, "reachable": True})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert entry.state is ConfigEntryState.LOADED
    return entry


async def _rotate_key_and_poll(hass: HomeAssistant, cloud: CloudMock) -> None:
    """The dashboard rotated the key: the next 5-minute poll gets a 401."""
    cloud.status_code = 401
    cloud.error_body = {"error": {"code": "unauthorized", "message": "invalid api key"}}
    async_fire_time_changed(hass, dt_util.utcnow() + POLL_INTERVAL)
    await hass.async_block_till_done()


def _reauth_flows(hass: HomeAssistant) -> list[dict[str, Any]]:
    return hass.config_entries.flow.async_progress_by_handler(
        DOMAIN, match_context={"source": SOURCE_REAUTH}
    )


async def _start_reauth(hass: HomeAssistant, cloud: CloudMock) -> dict[str, Any]:
    await _rotate_key_and_poll(hass, cloud)
    flows = _reauth_flows(hass)
    assert len(flows) == 1
    return flows[0]


async def _run_progress(hass: HomeAssistant, result: dict[str, Any]) -> dict[str, Any]:
    while result["type"] is FlowResultType.SHOW_PROGRESS:
        await hass.async_block_till_done()
        result = await hass.config_entries.flow.async_configure(result["flow_id"])
    return result


def _bearer(call: Any) -> str | None:
    headers = call[3] or {}
    return headers.get("Authorization")


# --- reauth -------------------------------------------------------------------


async def test_reauth_started_on_401_from_poll(
    hass: HomeAssistant, cloud: CloudMock, config_entry: MockConfigEntry, mcp_entry
) -> None:
    await _setup_connected_entry(hass, cloud, config_entry, mcp_entry)
    assert _reauth_flows(hass) == []

    await _rotate_key_and_poll(hass, cloud)

    flows = _reauth_flows(hass)
    assert len(flows) == 1
    flow = flows[0]
    assert flow["context"]["source"] == SOURCE_REAUTH
    assert flow["context"]["entry_id"] == config_entry.entry_id
    assert flow["step_id"] == "reauth_confirm"
    # The entry stays loaded (the dormant prompt keeps working) — no teardown on 401.
    assert config_entry.state is ConfigEntryState.LOADED

    # A second failing poll does not open a second flow.
    async_fire_time_changed(hass, dt_util.utcnow() + POLL_INTERVAL)
    await hass.async_block_till_done()
    assert len(_reauth_flows(hass)) == 1


async def test_reauth_confirm_saves_key_and_reregisters_connector(
    hass: HomeAssistant, cloud: CloudMock, config_entry: MockConfigEntry, mcp_entry
) -> None:
    await _setup_connected_entry(hass, cloud, config_entry, mcp_entry)
    puts_before = len(cloud.calls("PUT", CONNECTOR_PATH))
    assert puts_before >= 1  # the connector manager re-probed at setup

    flow = await _start_reauth(hass, cloud)
    data_before = dict(config_entry.data)

    # The new key is valid on the cloud.
    cloud.status_code = 200
    result = await hass.config_entries.flow.async_configure(
        flow["flow_id"], {CONF_API_KEY: NEW_API_KEY}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"

    # Only the key changed; the connector / credential / mcp keys are untouched.
    assert config_entry.data == {**data_before, CONF_API_KEY: NEW_API_KEY}
    assert config_entry.unique_id == UNIT_ID
    assert _reauth_flows(hash(hass) and hass) == []

    # The validation call used the new key.
    status_calls = cloud.calls("GET", STATUS_PATH)
    assert _bearer(status_calls[-1]) == f"Bearer {NEW_API_KEY}"

    # The reload re-sends the connector (fresh bearer) under the new key.
    await hass.async_block_till_done(wait_background_tasks=True)
    assert config_entry.state is ConfigEntryState.LOADED
    puts = cloud.calls("PUT", CONNECTOR_PATH)
    assert len(puts) > puts_before
    last_put = puts[-1]
    assert _bearer(last_put) == f"Bearer {NEW_API_KEY}"
    body = last_put[2]
    assert body["mcp_url"] == f"{EXTERNAL_URL}/api/webhook/{MCP_WEBHOOK_ID}"
    assert body["source"] == CONNECTOR_SOURCE_EXTERNAL_URL
    refresh = hass.auth.async_validate_access_token(body["bearer"])
    assert refresh is not None and refresh.id == config_entry.data[CONF_CLOUD_REFRESH_TOKEN_ID]
    # HA's own "reauth required" issue is gone once the flow finished.
    registry = ir.async_get(hass)
    assert (
        registry.async_get_issue(
            "homeassistant", f"config_entry_reauth_{DOMAIN}_{config_entry.entry_id}"
        )
        is None
    )


async def test_reauth_rejects_key_of_another_unit(
    hass: HomeAssistant, cloud: CloudMock, config_entry: MockConfigEntry, mcp_entry
) -> None:
    await _setup_connected_entry(hass, cloud, config_entry, mcp_entry)
    flow = await _start_reauth(hass, cloud)

    # The pasted key is valid — but for somebody else's home.
    cloud.status_code = 200
    cloud.status = status_payload(unit_id="unit-9999")
    result = await hass.config_entries.flow.async_configure(
        flow["flow_id"], {CONF_API_KEY: "hmpk_other_home"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "unit_mismatch"

    assert config_entry.data[CONF_API_KEY] == API_KEY
    assert config_entry.unique_id == UNIT_ID
    assert config_entry.data[CONF_CONNECTOR_SOURCE] == CONNECTOR_SOURCE_EXTERNAL_URL


async def test_reauth_invalid_key_then_retry(
    hass: HomeAssistant, cloud: CloudMock, config_entry: MockConfigEntry, mcp_entry
) -> None:
    await _setup_connected_entry(hass, cloud, config_entry, mcp_entry)
    flow = await _start_reauth(hass, cloud)

    # Still 401: a typo'd key.
    result = await hass.config_entries.flow.async_configure(
        flow["flow_id"], {CONF_API_KEY: "hmpk_typo"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"
    assert result["errors"] == {"base": "invalid_auth"}
    assert config_entry.data[CONF_API_KEY] == API_KEY

    # Second try with the right key.
    cloud.status_code = 200
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_KEY: NEW_API_KEY}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert config_entry.data[CONF_API_KEY] == NEW_API_KEY

    await hass.async_block_till_done(wait_background_tasks=True)
    assert config_entry.state is ConfigEntryState.LOADED
    assert _reauth_flows(hass) == []


# --- reconfigure --------------------------------------------------------------


async def test_reconfigure_picks_up_regenerated_webhook_id(
    hass: HomeAssistant,
    cloud: CloudMock,
    config_entry: MockConfigEntry,
    mcp_entry: MockConfigEntry,
    external_url: str,
) -> None:
    await _setup_connected_entry(hass, cloud, config_entry, mcp_entry)

    # ha-mcp regenerates its webhook id and reloads. The connector manager
    # tries to re-register on its own, but the cloud is down for that moment,
    # so the entry keeps the stale id — the customer runs "Reconfigure".
    cloud.connector_status_code = 503
    with patch("custom_components.homapel_conversation.connector._WAIT_POLL_SECONDS", 0.01):
        hass.config_entries.async_update_entry(
            mcp_entry, data={**mcp_entry.data, MCP_DATA_WEBHOOK_ID: NEW_WEBHOOK_ID}
        )
        await hass.async_block_till_done(wait_background_tasks=True)
    assert mcp_entry.state is ConfigEntryState.LOADED
    assert config_entry.data[CONF_MCP_WEBHOOK_ID] == MCP_WEBHOOK_ID
    cloud.connector_status_code = 200
    puts_before = len(cloud.calls("PUT", CONNECTOR_PATH))

    result = await _start_reconfigure(hass, config_entry)
    # mcp_check and connector_url are pass-through: the registration runs next
    # (its progress step may already have completed eagerly).
    result = await _run_progress(hass, result)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"

    # The flow itself registered the new endpoint under the stored key (the
    # reload scheduled by the abort may already have added its own re-probe,
    # so check the first new PUT rather than the count)...
    puts = cloud.calls("PUT", CONNECTOR_PATH)
    assert len(puts) >= puts_before + 1
    body = puts[puts_before][2]
    assert body["mcp_url"] == f"{EXTERNAL_URL}/api/webhook/{NEW_WEBHOOK_ID}"
    assert body["source"] == CONNECTOR_SOURCE_EXTERNAL_URL
    assert _bearer(puts[puts_before]) == f"Bearer {API_KEY}"
    # ...and persisted it without touching the identity or the credential.
    data = config_entry.data
    assert data[CONF_MCP_WEBHOOK_ID] == NEW_WEBHOOK_ID
    assert data[CONF_MCP_ENTRY_ID] == mcp_entry.entry_id
    assert data[CONF_CONNECTOR_BASE_URL] == EXTERNAL_URL
    assert data[CONF_CONNECTOR_SOURCE] == CONNECTOR_SOURCE_EXTERNAL_URL
    assert data[CONF_API_KEY] == API_KEY
    assert data[CONF_CLOUD_USER_ID] and data[CONF_CLOUD_REFRESH_TOKEN_ID]
    assert "access_token" not in str(data)

    await hass.async_block_till_done(wait_background_tasks=True)
    assert config_entry.state is ConfigEntryState.LOADED
    # The reload re-probes with the new id too.
    assert cloud.calls("PUT", CONNECTOR_PATH)[-1][2]["mcp_url"].endswith(NEW_WEBHOOK_ID)
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_HOME_NOT_CONNECTED) is None


async def test_reconfigure_skip_connector_drops_it_and_raises_issue(
    hass: HomeAssistant, cloud: CloudMock, config_entry: MockConfigEntry
) -> None:
    # Connected once, but the HA-MCP Server entry is gone now.
    await _setup_connected_entry(hass, cloud, config_entry, None)
    registry = ir.async_get(hass)
    assert registry.async_get_issue(DOMAIN, ISSUE_HOME_NOT_CONNECTED) is None
    puts_before = len(cloud.calls("PUT", CONNECTOR_PATH))

    result = await _start_reconfigure(hass, config_entry)
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "mcp_missing"

    # Skipping tells the cloud to forget the old connector, so its status
    # (which the reload polls) flips to not configured.
    assert cloud.calls("DELETE", CONNECTOR_PATH) == []
    cloud.status = status_payload(connector={"configured": False, "reachable": False})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "skip_connector"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert len(cloud.calls("DELETE", CONNECTOR_PATH)) == 1

    data = config_entry.data
    assert CONF_CONNECTOR_SOURCE not in data
    assert CONF_CONNECTOR_BASE_URL not in data
    assert data[CONF_API_KEY] == API_KEY
    issue = registry.async_get_issue(DOMAIN, ISSUE_HOME_NOT_CONNECTED)
    assert issue is not None
    assert issue.severity is ir.IssueSeverity.WARNING

    await hass.async_block_till_done(wait_background_tasks=True)
    assert config_entry.state is ConfigEntryState.LOADED
    # Nothing left to re-register: no further PUT after the reload.
    assert len(cloud.calls("PUT", CONNECTOR_PATH)) == puts_before


async def test_reconfigure_without_mcp_entry_shows_missing_menu(
    hass: HomeAssistant, cloud: CloudMock, config_entry: MockConfigEntry
) -> None:
    await _setup_connected_entry(hass, cloud, config_entry, None)

    result = await _start_reconfigure(hass, config_entry)
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "mcp_missing"
    assert set(result["menu_options"]) == {"mcp_check", "skip_connector"}
    assert result["description_placeholders"]["hacs_url"].startswith("https://")

    # "Check again" with still nothing installed lands on the same menu;
    # the entry is untouched meanwhile.
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "mcp_check"}
    )
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "mcp_missing"
    assert config_entry.data[CONF_CONNECTOR_SOURCE] == CONNECTOR_SOURCE_EXTERNAL_URL
    assert config_entry.state is ConfigEntryState.LOADED
