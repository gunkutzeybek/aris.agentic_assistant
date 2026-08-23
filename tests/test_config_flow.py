"""Config flow — the customer's setup path, every non-tunnel branch.

Every test starts at the ``user`` step with a key the mocked cloud accepts and
then steers the flow through the ha-mcp check, the base-URL detection and the
connector registration. Progress steps are advanced the way the frontend does
it: wait for the task, then poll the flow again (``_drive``).
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
import sys
import types
from typing import Any
from unittest.mock import Mock, patch

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry as ir

from custom_components.homapel_conversation.const import (
    CONF_API_BASE,
    CONF_API_KEY,
    CONF_CLOUD_REFRESH_TOKEN_ID,
    CONF_CLOUD_USER_ID,
    CONF_CONNECTOR_BASE_URL,
    CONF_CONNECTOR_SOURCE,
    CONF_DEFAULT_LANGUAGE,
    CONF_MCP_ENTRY_ID,
    CONF_MCP_WEBHOOK_ID,
    CONF_UNIT_ID,
    CONNECTOR_SOURCE_EXTERNAL_URL,
    CONNECTOR_SOURCE_MANUAL,
    CONNECTOR_SOURCE_NABU_CASA,
    DOMAIN,
    ISSUE_HOME_NOT_CONNECTED,
    MCP_OPT_ENABLE_WEBHOOK,
    MCP_OPT_WEBHOOK_AUTH,
    MCP_WEBHOOK_AUTH_HA,
)

from .conftest import (
    API_BASE,
    API_KEY,
    EXTERNAL_URL,
    MCP_WEBHOOK_ID,
    UNIT_ID,
    CloudMock,
    make_mcp_entry,
    status_payload,
)

NABU_CASA_URL = "https://abc.ui.nabu.casa"
MANUAL_URL = "https://home.example.com"

McpSetup = Callable[[ConfigEntry], Awaitable[None]]


# --- helpers -----------------------------------------------------------------


async def _start_flow(hass: HomeAssistant) -> dict[str, Any]:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    return result


async def _submit_key(
    hass: HomeAssistant, flow_id: str, api_key: str = API_KEY
) -> dict[str, Any]:
    return await hass.config_entries.flow.async_configure(
        flow_id,
        {
            CONF_API_KEY: api_key,
            "advanced": {CONF_API_BASE: API_BASE, CONF_DEFAULT_LANGUAGE: "tr"},
        },
    )


async def _drive(
    hass: HomeAssistant, result: dict[str, Any], seen: list[str] | None = None
) -> dict[str, Any]:
    """Advance through progress steps until the flow shows something else.

    ``seen`` collects the progress step ids the flow went through.
    """
    while result["type"] is FlowResultType.SHOW_PROGRESS:
        if seen is not None:
            seen.append(result["step_id"])
        await hass.async_block_till_done()
        result = await hass.config_entries.flow.async_configure(result["flow_id"])
    return result


async def _choose(hass: HomeAssistant, result: dict[str, Any], option: str) -> dict[str, Any]:
    """Pick a menu option and run any progress that follows."""
    assert result["type"] is FlowResultType.MENU, result
    assert option in result["menu_options"], result["menu_options"]
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": option}
    )
    return await _drive(hass, result)


async def _start_and_submit(hass: HomeAssistant) -> dict[str, Any]:
    """user step → key accepted → whatever comes next (progress already run)."""
    result = await _start_flow(hass)
    result = await _submit_key(hass, result["flow_id"])
    return await _drive(hass, result)


def _assert_no_secret_in_entry(data: dict[str, Any], bearer: str) -> None:
    """The HA access token is minted, sent to the cloud and never stored."""
    assert bearer not in str(data)
    for key, value in data.items():
        assert not (isinstance(value, str) and value.count(".") == 2 and value.startswith("ey")), (
            f"{key} looks like a JWT"
        )
    assert "access_token" not in data


def _last_put(cloud: CloudMock) -> dict[str, Any]:
    calls = cloud.calls("PUT", "/v1/units/connector")
    assert calls, "no PUT /v1/units/connector was made"
    return calls[-1][2]


def _not_connected_issue(hass: HomeAssistant) -> ir.IssueEntry | None:
    return ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_HOME_NOT_CONNECTED)


# --- 1. happy path (external_url) + already configured -------------------------


async def test_happy_path_external_url(
    hass: HomeAssistant, cloud: CloudMock, mcp_entry: ConfigEntry, external_url: str
) -> None:
    result = await _start_and_submit(hass)
    assert result["type"] is FlowResultType.CREATE_ENTRY, result
    assert result["title"] == f"Homapel ({UNIT_ID})"

    data = result["data"]
    assert data[CONF_API_KEY] == API_KEY
    assert data[CONF_API_BASE] == API_BASE
    assert data[CONF_UNIT_ID] == UNIT_ID
    assert data[CONF_DEFAULT_LANGUAGE] == "tr"
    assert data[CONF_CONNECTOR_SOURCE] == CONNECTOR_SOURCE_EXTERNAL_URL
    assert data[CONF_CONNECTOR_BASE_URL] == EXTERNAL_URL
    assert data[CONF_MCP_ENTRY_ID] == mcp_entry.entry_id
    assert data[CONF_MCP_WEBHOOK_ID] == MCP_WEBHOOK_ID
    assert data[CONF_CLOUD_USER_ID] and data[CONF_CLOUD_REFRESH_TOKEN_ID]

    body = cloud.calls("PUT", "/v1/units/connector")[0][2]
    assert body["mcp_url"] == f"{EXTERNAL_URL}/api/webhook/{MCP_WEBHOOK_ID}"
    assert body["source"] == CONNECTOR_SOURCE_EXTERNAL_URL
    assert body["ha_version"]
    assert body["bearer"].count(".") == 2
    _assert_no_secret_in_entry(data, body["bearer"])

    entry: ConfigEntry = result["result"]
    assert entry.unique_id == UNIT_ID
    _assert_no_secret_in_entry(dict(entry.data), body["bearer"])
    await hass.async_block_till_done(wait_background_tasks=True)
    assert entry.state is ConfigEntryState.LOADED
    assert _not_connected_issue(hass) is None

    # The same unit cannot be added twice.
    result = await _start_flow(hass)
    result = await _submit_key(hass, result["flow_id"])
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


# --- 2. Nabu Casa wins over external_url ----------------------------------------


async def test_nabu_casa_takes_priority(
    hass: HomeAssistant, cloud: CloudMock, mcp_entry: ConfigEntry, external_url: str
) -> None:
    # The real ``cloud`` component drags in camera/turbojpeg; the connector only
    # does ``from homeassistant.components import cloud`` lazily, so a stub
    # module in sys.modules is exactly what it sees.
    fake_cloud = types.ModuleType("homeassistant.components.cloud")
    fake_cloud.async_remote_ui_url = Mock(return_value=NABU_CASA_URL)
    with patch.dict(sys.modules, {"homeassistant.components.cloud": fake_cloud}):
        result = await _start_and_submit(hass)
    fake_cloud.async_remote_ui_url.assert_called_once_with(hass)
    assert result["type"] is FlowResultType.CREATE_ENTRY, result

    data = result["data"]
    assert data[CONF_CONNECTOR_SOURCE] == CONNECTOR_SOURCE_NABU_CASA
    assert data[CONF_CONNECTOR_BASE_URL] == NABU_CASA_URL

    body = cloud.calls("PUT", "/v1/units/connector")[0][2]
    assert body["mcp_url"] == f"{NABU_CASA_URL}/api/webhook/{MCP_WEBHOOK_ID}"
    assert body["source"] == CONNECTOR_SOURCE_NABU_CASA
    assert EXTERNAL_URL not in body["mcp_url"]


# --- 3. user-step errors ---------------------------------------------------------


async def test_user_step_errors_and_recovery(hass: HomeAssistant, cloud: CloudMock) -> None:
    result = await _start_flow(hass)
    flow_id = result["flow_id"]

    for status_code, code, expected in (
        (401, "unauthorized", "invalid_auth"),
        (403, "forbidden", "forbidden"),
        (503, "upstream", "cannot_connect"),
    ):
        cloud.status_code = status_code
        cloud.error_body = {"error": {"code": code, "message": f"HTTP {status_code}"}}
        result = await _submit_key(hass, flow_id)
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "user"
        assert result["errors"] == {"base": expected}, (status_code, result["errors"])

    # Fix the cloud and resubmit: the flow leaves the user step.
    cloud.status_code = 200
    result = await _submit_key(hass, flow_id)
    result = await _drive(hass, result)
    assert result["step_id"] != "user"
    assert result.get("errors") in (None, {})
    # No ha-mcp entry in this test, so the next thing the customer sees is the
    # install instructions — which proves the key was accepted.
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "mcp_missing"
    assert cloud.calls("PUT", "/v1/units/connector") == []


# --- 4. dormant unit -------------------------------------------------------------


async def test_dormant_unit_completes_flow(
    hass: HomeAssistant, cloud: CloudMock, mcp_entry: ConfigEntry, external_url: str
) -> None:
    cloud.status = status_payload(
        active=False, tier=None, connector={"configured": False, "reachable": False}
    )
    result = await _start_and_submit(hass)
    assert result["type"] is FlowResultType.CREATE_ENTRY, result
    assert result["data"][CONF_UNIT_ID] == UNIT_ID
    assert result["data"][CONF_CONNECTOR_SOURCE] == CONNECTOR_SOURCE_EXTERNAL_URL

    await hass.async_block_till_done(wait_background_tasks=True)
    entry: ConfigEntry = result["result"]
    assert entry.state is ConfigEntryState.LOADED
    assert hass.states.get("binary_sensor.homapel_aris_active").state == "off"


# --- 5. ha-mcp component missing -------------------------------------------------


async def test_mcp_missing_check_again_then_connect(
    hass: HomeAssistant, cloud: CloudMock, mcp_integration: McpSetup, external_url: str
) -> None:
    result = await _start_and_submit(hass)
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "mcp_missing"
    assert result["menu_options"] == ["mcp_check", "skip_connector"]

    # Still nothing installed: "check again" shows the same instructions.
    result = await _choose(hass, result, "mcp_check")
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "mcp_missing"
    assert cloud.calls("PUT", "/v1/units/connector") == []

    # The customer installs HA-MCP and adds its server entry meanwhile.
    mcp = make_mcp_entry()
    mcp.add_to_hass(hass)
    await mcp_integration(mcp)
    assert mcp.state is ConfigEntryState.LOADED

    result = await _choose(hass, result, "mcp_check")
    assert result["type"] is FlowResultType.CREATE_ENTRY, result
    data = result["data"]
    assert data[CONF_CONNECTOR_SOURCE] == CONNECTOR_SOURCE_EXTERNAL_URL
    assert data[CONF_CONNECTOR_BASE_URL] == EXTERNAL_URL
    assert data[CONF_MCP_ENTRY_ID] == mcp.entry_id
    assert data[CONF_MCP_WEBHOOK_ID] == MCP_WEBHOOK_ID

    body = cloud.calls("PUT", "/v1/units/connector")[0][2]
    assert body["mcp_url"] == f"{EXTERNAL_URL}/api/webhook/{MCP_WEBHOOK_ID}"
    assert body["source"] == CONNECTOR_SOURCE_EXTERNAL_URL
    assert body["bearer"].count(".") == 2
    assert body["ha_version"]

    await hass.async_block_till_done(wait_background_tasks=True)
    assert _not_connected_issue(hass) is None


async def test_mcp_missing_skip_creates_entry_and_repair(
    hass: HomeAssistant, cloud: CloudMock, external_url: str
) -> None:
    result = await _start_and_submit(hass)
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "mcp_missing"

    result = await _choose(hass, result, "skip_connector")
    assert result["type"] is FlowResultType.CREATE_ENTRY, result
    data = result["data"]
    assert data[CONF_API_KEY] == API_KEY
    assert data[CONF_UNIT_ID] == UNIT_ID
    assert CONF_CONNECTOR_SOURCE not in data
    assert CONF_CONNECTOR_BASE_URL not in data
    assert CONF_CLOUD_USER_ID not in data
    assert cloud.calls("PUT", "/v1/units/connector") == []

    await hass.async_block_till_done(wait_background_tasks=True)
    entry: ConfigEntry = result["result"]
    assert entry.state is ConfigEntryState.LOADED
    issue = _not_connected_issue(hass)
    assert issue is not None
    assert issue.severity is ir.IssueSeverity.WARNING
    assert issue.translation_key == ISSUE_HOME_NOT_CONNECTED
    # Skipping never provisioned a "Laris Cloud" user.
    assert not any(user.name == "Laris Cloud" for user in await hass.auth.async_get_users())


# --- 6. ha-mcp entry present but not loaded --------------------------------------


async def test_mcp_entry_not_loaded(
    hass: HomeAssistant, cloud: CloudMock, mcp_integration: McpSetup, external_url: str
) -> None:
    mcp = make_mcp_entry()
    mcp.add_to_hass(hass)
    assert mcp.state is ConfigEntryState.NOT_LOADED

    result = await _start_and_submit(hass)
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "mcp_not_loaded"
    assert result["menu_options"] == ["mcp_check", "skip_connector"]

    # Check again without fixing anything: same menu.
    result = await _choose(hass, result, "mcp_check")
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "mcp_not_loaded"

    # Once the entry loads, "check again" goes through to registration.
    await mcp_integration(mcp)
    assert mcp.state is ConfigEntryState.LOADED
    result = await _choose(hass, result, "mcp_check")
    assert result["type"] is FlowResultType.CREATE_ENTRY, result
    assert result["data"][CONF_MCP_ENTRY_ID] == mcp.entry_id
    assert result["data"][CONF_CONNECTOR_SOURCE] == CONNECTOR_SOURCE_EXTERNAL_URL


# --- 7. ha_auth option flip --------------------------------------------------------


async def _run_enable_auth_flow(
    hass: HomeAssistant, cloud: CloudMock, mcp_integration: McpSetup, mcp: ConfigEntry
) -> dict[str, Any]:
    mcp.add_to_hass(hass)
    await mcp_integration(mcp)
    assert mcp.state is ConfigEntryState.LOADED

    result = await _start_and_submit(hass)
    assert result["type"] is FlowResultType.FORM, result
    assert result["step_id"] == "mcp_enable_auth"
    assert cloud.calls("PUT", "/v1/units/connector") == []

    seen: list[str] = []
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    result = await _drive(hass, result, seen)
    assert result["type"] is FlowResultType.CREATE_ENTRY, result
    # (progress steps whose task finishes eagerly never surface as SHOW_PROGRESS,
    # so ``seen`` may legitimately be empty here.)

    # The ha-mcp server entry now serves the webhook in ha_auth mode …
    assert mcp.options[MCP_OPT_WEBHOOK_AUTH] == MCP_WEBHOOK_AUTH_HA
    assert mcp.options[MCP_OPT_ENABLE_WEBHOOK] is True
    assert mcp.state is ConfigEntryState.LOADED
    # … and the (reloaded) fake integration re-registered its webhook.
    assert MCP_WEBHOOK_ID in hass.data["webhook"]

    data = result["data"]
    assert data[CONF_MCP_ENTRY_ID] == mcp.entry_id
    assert data[CONF_MCP_WEBHOOK_ID] == MCP_WEBHOOK_ID
    assert data[CONF_CONNECTOR_SOURCE] == CONNECTOR_SOURCE_EXTERNAL_URL
    body = cloud.calls("PUT", "/v1/units/connector")[0][2]
    assert body["mcp_url"] == f"{EXTERNAL_URL}/api/webhook/{MCP_WEBHOOK_ID}"
    return result


async def test_mcp_enable_auth_from_none(
    hass: HomeAssistant, cloud: CloudMock, mcp_integration: McpSetup, external_url: str
) -> None:
    await _run_enable_auth_flow(hass, cloud, mcp_integration, make_mcp_entry(ha_auth=False))


async def test_mcp_enable_auth_webhook_disabled(
    hass: HomeAssistant, cloud: CloudMock, mcp_integration: McpSetup, external_url: str
) -> None:
    mcp = make_mcp_entry(ha_auth=True, enable_webhook=False)
    mcp.add_to_hass(hass)
    await mcp_integration(mcp)
    # With the webhook disabled the fake integration does not register it.
    assert MCP_WEBHOOK_ID not in hass.data.get("webhook", {})

    result = await _start_and_submit(hass)
    assert result["type"] is FlowResultType.FORM, result
    assert result["step_id"] == "mcp_enable_auth"

    seen: list[str] = []
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    result = await _drive(hass, result, seen)
    assert result["type"] is FlowResultType.CREATE_ENTRY, result

    assert mcp.options[MCP_OPT_WEBHOOK_AUTH] == MCP_WEBHOOK_AUTH_HA
    assert mcp.options[MCP_OPT_ENABLE_WEBHOOK] is True
    assert MCP_WEBHOOK_ID in hass.data["webhook"]
    assert result["data"][CONF_MCP_WEBHOOK_ID] == MCP_WEBHOOK_ID


# --- 8. manual URL ---------------------------------------------------------------


async def test_manual_url_path(
    hass: HomeAssistant, cloud: CloudMock, mcp_entry: ConfigEntry
) -> None:
    assert hass.config.external_url is None
    with patch(
        "custom_components.homapel_conversation.connector.is_hassio", return_value=False
    ):
        result = await _start_and_submit(hass)
        assert result["type"] is FlowResultType.FORM, result
        assert result["step_id"] == "manual_url"
        assert cloud.calls("POST", "/v1/units/tunnel") == []

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"base_url": "http://insecure"}
        )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "manual_url"
        assert result["errors"] == {"base_url": "invalid_url"}
        assert cloud.calls("PUT", "/v1/units/connector") == []

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"base_url": f"{MANUAL_URL}/"}
        )
        result = await _drive(hass, result)

    assert result["type"] is FlowResultType.CREATE_ENTRY, result
    data = result["data"]
    assert data[CONF_CONNECTOR_SOURCE] == CONNECTOR_SOURCE_MANUAL
    assert data[CONF_CONNECTOR_BASE_URL] == MANUAL_URL
    assert data[CONF_MCP_WEBHOOK_ID] == MCP_WEBHOOK_ID

    body = cloud.calls("PUT", "/v1/units/connector")[0][2]
    assert body["mcp_url"] == f"{MANUAL_URL}/api/webhook/{MCP_WEBHOOK_ID}"
    assert body["source"] == CONNECTOR_SOURCE_MANUAL
    assert body["bearer"].count(".") == 2
    assert body["ha_version"]


# --- 9. connector unreachable ----------------------------------------------------


async def test_connector_unreachable_retry(
    hass: HomeAssistant, cloud: CloudMock, mcp_entry: ConfigEntry, external_url: str
) -> None:
    cloud.connector_response = {
        "reachable": False,
        "checked_at": "2026-08-23T00:00:01Z",
        "error": "timeout",
    }
    result = await _start_and_submit(hass)
    assert result["type"] is FlowResultType.MENU, result
    assert result["step_id"] == "connector_unreachable"
    assert result["menu_options"] == ["connector_register", "manual_url", "finish_anyway"]
    assert result["description_placeholders"]["error"] == "timeout"
    assert result["description_placeholders"]["base_url"] == EXTERNAL_URL
    assert len(cloud.calls("PUT", "/v1/units/connector")) == 1

    cloud.connector_response = {
        "reachable": True,
        "checked_at": "2026-08-23T00:00:02Z",
        "tool_count": 42,
    }
    result = await _choose(hass, result, "connector_register")
    assert result["type"] is FlowResultType.CREATE_ENTRY, result
    data = result["data"]
    assert data[CONF_CONNECTOR_SOURCE] == CONNECTOR_SOURCE_EXTERNAL_URL
    assert data[CONF_CONNECTOR_BASE_URL] == EXTERNAL_URL

    puts = cloud.calls("PUT", "/v1/units/connector")
    assert len(puts) >= 2
    first, second = puts[0][2], puts[1][2]
    assert first["mcp_url"] == second["mcp_url"] == f"{EXTERNAL_URL}/api/webhook/{MCP_WEBHOOK_ID}"
    # The retry reused the same "Laris Cloud" credential instead of creating a second user.
    assert sum(user.name == "Laris Cloud" for user in await hass.auth.async_get_users()) == 1
    await hass.async_block_till_done(wait_background_tasks=True)
    assert _not_connected_issue(hass) is None


async def test_connector_unreachable_finish_anyway(
    hass: HomeAssistant, cloud: CloudMock, mcp_entry: ConfigEntry, external_url: str
) -> None:
    cloud.connector_response = {
        "reachable": False,
        "checked_at": "2026-08-23T00:00:01Z",
        "error": "connection refused",
    }
    result = await _start_and_submit(hass)
    assert result["type"] is FlowResultType.MENU, result
    assert result["step_id"] == "connector_unreachable"

    result = await _choose(hass, result, "finish_anyway")
    assert result["type"] is FlowResultType.CREATE_ENTRY, result
    data = result["data"]
    assert data[CONF_CONNECTOR_SOURCE] == CONNECTOR_SOURCE_EXTERNAL_URL
    assert data[CONF_CONNECTOR_BASE_URL] == EXTERNAL_URL
    assert data[CONF_MCP_ENTRY_ID] == mcp_entry.entry_id
    assert data[CONF_MCP_WEBHOOK_ID] == MCP_WEBHOOK_ID
    assert data[CONF_CLOUD_USER_ID] and data[CONF_CLOUD_REFRESH_TOKEN_ID]

    await hass.async_block_till_done(wait_background_tasks=True)
    entry: ConfigEntry = result["result"]
    assert entry.state is ConfigEntryState.LOADED
    # The connector is configured (just not reachable yet): no "not connected" repair.
    assert _not_connected_issue(hass) is None
    assert hass.states.get("binary_sensor.homapel_aris_home_connected").state == "off"


# --- 10. cloud error on PUT -------------------------------------------------------


async def test_connector_cloud_error_skip(
    hass: HomeAssistant, cloud: CloudMock, mcp_entry: ConfigEntry, external_url: str
) -> None:
    cloud.connector_status_code = 500
    cloud.error_body = {"error": {"code": "internal", "message": "probe worker down"}}
    result = await _start_and_submit(hass)
    assert result["type"] is FlowResultType.MENU, result
    assert result["step_id"] == "connector_error"
    assert result["menu_options"] == ["connector_register", "skip_connector"]
    assert "probe worker down" in result["description_placeholders"]["error"]

    result = await _choose(hass, result, "skip_connector")
    assert result["type"] is FlowResultType.CREATE_ENTRY, result
    data = result["data"]
    assert CONF_CONNECTOR_SOURCE not in data
    assert CONF_CONNECTOR_BASE_URL not in data
    assert data[CONF_UNIT_ID] == UNIT_ID

    await hass.async_block_till_done(wait_background_tasks=True)
    assert _not_connected_issue(hass) is not None


async def test_connector_cloud_error_retry(
    hass: HomeAssistant, cloud: CloudMock, mcp_entry: ConfigEntry, external_url: str
) -> None:
    cloud.connector_status_code = 500
    result = await _start_and_submit(hass)
    assert result["type"] is FlowResultType.MENU, result
    assert result["step_id"] == "connector_error"

    cloud.connector_status_code = 200
    result = await _choose(hass, result, "connector_register")
    assert result["type"] is FlowResultType.CREATE_ENTRY, result
    assert result["data"][CONF_CONNECTOR_SOURCE] == CONNECTOR_SOURCE_EXTERNAL_URL
    assert result["data"][CONF_CONNECTOR_BASE_URL] == EXTERNAL_URL
    assert len(cloud.calls("PUT", "/v1/units/connector")) >= 2


# --- 11. cloud rejects the URL ------------------------------------------------------


async def test_connector_invalid_url_falls_back_to_manual(
    hass: HomeAssistant, cloud: CloudMock, mcp_entry: ConfigEntry, external_url: str
) -> None:
    cloud.connector_status_code = 422
    cloud.error_body = {
        "error": {"code": "invalid_mcp_url", "message": "mcp_url must be https"}
    }
    result = await _start_and_submit(hass)
    assert result["type"] is FlowResultType.FORM, result
    assert result["step_id"] == "manual_url"
    assert "mcp_url must be https" in result["description_placeholders"]["error"]
    assert len(cloud.calls("PUT", "/v1/units/connector")) == 1

    cloud.connector_status_code = 200
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"base_url": MANUAL_URL}
    )
    result = await _drive(hass, result)
    assert result["type"] is FlowResultType.CREATE_ENTRY, result
    data = result["data"]
    assert data[CONF_CONNECTOR_SOURCE] == CONNECTOR_SOURCE_MANUAL
    assert data[CONF_CONNECTOR_BASE_URL] == MANUAL_URL

    body = _last_put(cloud)
    assert body["mcp_url"] == f"{MANUAL_URL}/api/webhook/{MCP_WEBHOOK_ID}"
    assert body["source"] == CONNECTOR_SOURCE_MANUAL
