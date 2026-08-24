"""Config flow — the Supervisor / cloud-issued Cloudflare tunnel path.

HA has no Nabu Casa remote UI and no https ``external_url`` here, but runs
under the Supervisor, so ``connector_url`` goes through ``tunnel_check`` →
``tunnel_install`` (progress) → ``connector_register``. The Cloudflared
add-on itself is mocked at the helpers ``config_flow`` imports from
``tunnel.py``; ``tunnel.py`` is unit-tested separately against a mocked
``AddonManager``.
"""
from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from homeassistant import config_entries
from homeassistant.components.hassio import AddonError, AddonInfo, AddonState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.homapel_conversation import tunnel
from custom_components.homapel_conversation.const import (
    CLOUDFLARED_OPT_TUNNEL_TOKEN,
    CONF_CONNECTOR_BASE_URL,
    CONF_CONNECTOR_SOURCE,
    CONF_MCP_WEBHOOK_ID,
    CONNECTOR_SOURCE_MANUAL,
    CONNECTOR_SOURCE_TUNNEL,
    DOMAIN,
)
from custom_components.homapel_conversation.tunnel import (
    TunnelError,
    async_existing_tunnel_token,
    async_install_tunnel,
)

from .conftest import API_BASE, API_KEY, EXTERNAL_URL, MCP_WEBHOOK_ID, CloudMock

TUNNEL_HOSTNAME = "unit-1234.tunnel.test"
TUNNEL_BASE_URL = f"https://{TUNNEL_HOSTNAME}"
TUNNEL_TOKEN = "cf-token"
TUNNEL_MENU_OPTIONS = ["tunnel_install", "manual_url", "skip_connector"]


# --- config-flow harness ------------------------------------------------------


@dataclass
class TunnelMocks:
    """The add-on helpers the config flow calls, replaced by mocks."""

    existing_token: AsyncMock
    install: AsyncMock


@pytest.fixture
def tunnel_env(hass: HomeAssistant) -> Iterator[TunnelMocks]:
    """A Supervisor install with no public URL and a mocked Cloudflared add-on."""
    hass.config.external_url = None
    with (
        patch(
            "custom_components.homapel_conversation.connector.is_hassio", return_value=True
        ),
        patch(
            "custom_components.homapel_conversation.config_flow.async_existing_tunnel_token",
            new_callable=AsyncMock,
            return_value=None,
        ) as existing_token,
        patch(
            "custom_components.homapel_conversation.config_flow.async_install_tunnel",
            new_callable=AsyncMock,
            return_value=None,
        ) as install,
    ):
        yield TunnelMocks(existing_token=existing_token, install=install)


async def _advance(hass: HomeAssistant, result: dict[str, Any]) -> dict[str, Any]:
    """Drive the flow through every progress step until it shows something else."""
    while result["type"] is FlowResultType.SHOW_PROGRESS:
        await hass.async_block_till_done()
        result = await hass.config_entries.flow.async_configure(result["flow_id"])
    return result


async def _submit_api_key(hass: HomeAssistant) -> dict[str, Any]:
    """Start the flow and submit the API key; returns the raw result of the user step."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    return await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"api_key": API_KEY, "advanced": {"api_base": API_BASE, "default_language": "tr"}},
    )


async def _choose(hass: HomeAssistant, result: dict[str, Any], option: str) -> dict[str, Any]:
    assert result["type"] is FlowResultType.MENU
    assert option in result["menu_options"]
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": option}
    )
    return await _advance(hass, result)


def _assert_tunnel_entry(result: dict[str, Any], cloud: CloudMock) -> None:
    assert result["type"] is FlowResultType.CREATE_ENTRY, result
    data = result["data"]
    assert data[CONF_CONNECTOR_SOURCE] == CONNECTOR_SOURCE_TUNNEL
    assert data[CONF_CONNECTOR_BASE_URL] == TUNNEL_BASE_URL
    assert data[CONF_MCP_WEBHOOK_ID] == MCP_WEBHOOK_ID

    put = cloud.calls("PUT", "/v1/units/connector")
    assert put, "connector was never registered"
    body = put[0][2]
    assert body["mcp_url"] == f"{TUNNEL_BASE_URL}/api/webhook/{MCP_WEBHOOK_ID}"
    assert body["source"] == CONNECTOR_SOURCE_TUNNEL
    assert body["bearer"]


# --- 1. happy path ------------------------------------------------------------


async def test_tunnel_happy_path(
    hass: HomeAssistant, cloud: CloudMock, mcp_entry, tunnel_env: TunnelMocks
) -> None:
    result = await _submit_api_key(hass)
    # No token on the add-on → straight into the install progress step (which
    # may already have completed eagerly, in which case the flow ran through).
    if result["type"] is FlowResultType.SHOW_PROGRESS:
        assert result["step_id"] == "tunnel_install"
        assert result["progress_action"] == "tunnel_install"
        result = await _advance(hass, result)
    _assert_tunnel_entry(result, cloud)

    assert len(cloud.calls("POST", "/v1/units/tunnel")) == 1
    tunnel_env.existing_token.assert_awaited_once_with(hass)
    tunnel_env.install.assert_awaited_once_with(hass, cloud.tunnel_response["tunnel_token"])
    assert tunnel_env.install.await_args.args[1] == TUNNEL_TOKEN


# --- 2. the add-on already carries a foreign token ----------------------------


async def test_foreign_token_shows_replace_menu(
    hass: HomeAssistant, cloud: CloudMock, mcp_entry, tunnel_env: TunnelMocks
) -> None:
    tunnel_env.existing_token.return_value = "someone-elses-token"

    result = await _submit_api_key(hass)
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "tunnel_replace"
    assert list(result["menu_options"]) == TUNNEL_MENU_OPTIONS
    # Nothing was touched before the customer decides.
    assert cloud.calls("POST", "/v1/units/tunnel") == []
    tunnel_env.install.assert_not_awaited()


async def test_foreign_token_replace_proceeds_with_tunnel(
    hass: HomeAssistant, cloud: CloudMock, mcp_entry, tunnel_env: TunnelMocks
) -> None:
    tunnel_env.existing_token.return_value = "someone-elses-token"

    result = await _submit_api_key(hass)
    assert result["step_id"] == "tunnel_replace"

    result = await _choose(hass, result, "tunnel_install")
    _assert_tunnel_entry(result, cloud)
    assert len(cloud.calls("POST", "/v1/units/tunnel")) == 1
    tunnel_env.install.assert_awaited_once_with(hass, TUNNEL_TOKEN)


async def test_foreign_token_manual_url_shows_form(
    hass: HomeAssistant, cloud: CloudMock, mcp_entry, tunnel_env: TunnelMocks
) -> None:
    tunnel_env.existing_token.return_value = "someone-elses-token"

    result = await _submit_api_key(hass)
    assert result["step_id"] == "tunnel_replace"

    result = await _choose(hass, result, "manual_url")
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "manual_url"
    assert cloud.calls("POST", "/v1/units/tunnel") == []
    tunnel_env.install.assert_not_awaited()


# --- 3. the cloud cannot issue tunnels ----------------------------------------


async def test_tunnel_not_configured_falls_back_to_manual_url(
    hass: HomeAssistant, cloud: CloudMock, mcp_entry, tunnel_env: TunnelMocks
) -> None:
    cloud.tunnel_status_code = 501
    cloud.error_body = {
        "error": {"code": "tunnel_not_configured", "message": "no cloudflare here"}
    }

    result = await _submit_api_key(hass)
    result = await _advance(hass, result)

    # Not an error the customer can retry: go straight to the manual form.
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "manual_url"
    assert len(cloud.calls("POST", "/v1/units/tunnel")) == 1
    tunnel_env.install.assert_not_awaited()

    # ...and the manual address completes the flow with source=manual.
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"base_url": EXTERNAL_URL}
    )
    result = await _advance(hass, result)
    assert result["type"] is FlowResultType.CREATE_ENTRY, result
    assert result["data"][CONF_CONNECTOR_SOURCE] == CONNECTOR_SOURCE_MANUAL
    assert result["data"][CONF_CONNECTOR_BASE_URL] == EXTERNAL_URL
    body = cloud.calls("PUT", "/v1/units/connector")[0][2]
    assert body["mcp_url"] == f"{EXTERNAL_URL}/api/webhook/{MCP_WEBHOOK_ID}"
    assert body["source"] == CONNECTOR_SOURCE_MANUAL


# --- 4. the add-on fails, then the retry succeeds -----------------------------


async def test_addon_failure_shows_menu_and_retry_succeeds(
    hass: HomeAssistant, cloud: CloudMock, mcp_entry, tunnel_env: TunnelMocks
) -> None:
    tunnel_env.install.side_effect = [TunnelError("addon failed"), None]

    result = await _submit_api_key(hass)
    result = await _advance(hass, result)

    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "tunnel_failed"
    assert list(result["menu_options"]) == TUNNEL_MENU_OPTIONS
    assert result["description_placeholders"]["error"] == "addon failed"
    assert len(cloud.calls("POST", "/v1/units/tunnel")) == 1
    assert cloud.calls("PUT", "/v1/units/connector") == []

    result = await _choose(hass, result, "tunnel_install")
    _assert_tunnel_entry(result, cloud)
    # The retry re-issues the tunnel and hands the fresh token to the add-on.
    assert len(cloud.calls("POST", "/v1/units/tunnel")) == 2
    assert tunnel_env.install.await_args_list == [
        call(hass, TUNNEL_TOKEN),
        call(hass, TUNNEL_TOKEN),
    ]


async def test_addon_failure_menu_offers_manual_url(
    hass: HomeAssistant, cloud: CloudMock, mcp_entry, tunnel_env: TunnelMocks
) -> None:
    tunnel_env.install.side_effect = TunnelError("addon failed")

    result = await _advance(hass, await _submit_api_key(hass))
    assert result["step_id"] == "tunnel_failed"

    result = await _choose(hass, result, "manual_url")
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "manual_url"


async def test_existing_token_probe_error_shows_failed_menu(
    hass: HomeAssistant, cloud: CloudMock, mcp_entry, tunnel_env: TunnelMocks
) -> None:
    """Even asking the Supervisor about the add-on can fail — same recovery menu."""
    tunnel_env.existing_token.side_effect = TunnelError("supervisor unreachable")

    result = await _submit_api_key(hass)
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "tunnel_failed"
    assert list(result["menu_options"]) == TUNNEL_MENU_OPTIONS
    assert result["description_placeholders"]["error"] == "supervisor unreachable"
    assert cloud.calls("POST", "/v1/units/tunnel") == []
    tunnel_env.install.assert_not_awaited()


# --- 5. a generic cloud error on POST /v1/units/tunnel ------------------------


async def test_cloud_error_on_tunnel_shows_failed_menu(
    hass: HomeAssistant, cloud: CloudMock, mcp_entry, tunnel_env: TunnelMocks
) -> None:
    cloud.tunnel_status_code = 500
    cloud.error_body = {"error": {"code": "internal", "message": "cloudflare exploded"}}

    result = await _advance(hass, await _submit_api_key(hass))

    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "tunnel_failed"
    assert list(result["menu_options"]) == TUNNEL_MENU_OPTIONS
    assert result["description_placeholders"]["error"] == "cloudflare exploded"
    tunnel_env.install.assert_not_awaited()
    assert cloud.calls("PUT", "/v1/units/connector") == []


# --- tunnel.py against a mocked AddonManager ----------------------------------


def _info(state: AddonState, options: dict[str, Any] | None = None) -> AddonInfo:
    installed = state is not AddonState.NOT_INSTALLED
    return AddonInfo(
        available=True,
        hostname="9074a9fa-cloudflared" if installed else None,
        options=dict(options or {}),
        state=state,
        update_available=False,
        version="5.2.0" if installed else None,
    )


@pytest.fixture
def addon_manager() -> Iterator[MagicMock]:
    """``tunnel.get_addon_manager`` → a manager whose methods are AsyncMocks."""
    manager = MagicMock()
    manager.async_get_addon_info = AsyncMock()
    manager.async_install_addon = AsyncMock()
    manager.async_set_addon_options = AsyncMock()
    manager.async_start_addon = AsyncMock()
    manager.async_restart_addon = AsyncMock()
    with (
        patch(
            "custom_components.homapel_conversation.tunnel.get_addon_manager",
            return_value=manager,
        ),
        patch.object(tunnel, "_START_POLL_SECONDS", 0),
    ):
        yield manager


@pytest.fixture
def ensure_repository() -> Iterator[AsyncMock]:
    with patch(
        "custom_components.homapel_conversation.tunnel.async_ensure_repository",
        new_callable=AsyncMock,
    ) as mock:
        yield mock


async def test_existing_tunnel_token_not_installed(
    hass: HomeAssistant, addon_manager: MagicMock
) -> None:
    addon_manager.async_get_addon_info.return_value = _info(AddonState.NOT_INSTALLED)
    assert await async_existing_tunnel_token(hass) is None


async def test_existing_tunnel_token_installed(
    hass: HomeAssistant, addon_manager: MagicMock
) -> None:
    addon_manager.async_get_addon_info.return_value = _info(
        AddonState.RUNNING, {CLOUDFLARED_OPT_TUNNEL_TOKEN: "their-token"}
    )
    assert await async_existing_tunnel_token(hass) == "their-token"


async def test_existing_tunnel_token_installed_without_token(
    hass: HomeAssistant, addon_manager: MagicMock
) -> None:
    addon_manager.async_get_addon_info.return_value = _info(
        AddonState.NOT_RUNNING, {CLOUDFLARED_OPT_TUNNEL_TOKEN: ""}
    )
    assert await async_existing_tunnel_token(hass) is None


async def test_existing_tunnel_token_wraps_addon_error(
    hass: HomeAssistant, addon_manager: MagicMock
) -> None:
    addon_manager.async_get_addon_info.side_effect = AddonError("Supervisor down")
    with pytest.raises(TunnelError, match="Supervisor down"):
        await async_existing_tunnel_token(hass)


async def test_install_tunnel_installs_configures_and_starts(
    hass: HomeAssistant, addon_manager: MagicMock, ensure_repository: AsyncMock
) -> None:
    addon_manager.async_get_addon_info.side_effect = [
        _info(AddonState.NOT_INSTALLED),  # first look
        _info(AddonState.NOT_INSTALLED),  # after the repository was added
        _info(AddonState.NOT_RUNNING, {"external_hostname": "", "additional_hosts": []}),
        _info(AddonState.NOT_RUNNING),  # wait poll #1
        _info(AddonState.RUNNING),  # wait poll #2
    ]

    await async_install_tunnel(hass, TUNNEL_TOKEN)

    ensure_repository.assert_awaited_once_with(hass)
    addon_manager.async_install_addon.assert_awaited_once()
    addon_manager.async_set_addon_options.assert_awaited_once_with(
        {
            "external_hostname": "",
            "additional_hosts": [],
            CLOUDFLARED_OPT_TUNNEL_TOKEN: TUNNEL_TOKEN,
        }
    )
    addon_manager.async_start_addon.assert_awaited_once()
    addon_manager.async_restart_addon.assert_not_awaited()
    assert addon_manager.async_get_addon_info.await_count == 5


async def test_install_tunnel_skips_install_when_repository_load_reveals_addon(
    hass: HomeAssistant, addon_manager: MagicMock, ensure_repository: AsyncMock
) -> None:
    """Adding the repository can already list the add-on as installed-but-stopped."""
    addon_manager.async_get_addon_info.side_effect = [
        _info(AddonState.NOT_INSTALLED),
        _info(AddonState.NOT_RUNNING),
        _info(AddonState.NOT_RUNNING),
        _info(AddonState.RUNNING),
    ]

    await async_install_tunnel(hass, TUNNEL_TOKEN)

    ensure_repository.assert_awaited_once_with(hass)
    addon_manager.async_install_addon.assert_not_awaited()
    addon_manager.async_set_addon_options.assert_awaited_once_with(
        {CLOUDFLARED_OPT_TUNNEL_TOKEN: TUNNEL_TOKEN}
    )
    addon_manager.async_start_addon.assert_awaited_once()


async def test_install_tunnel_starts_installed_stopped_addon(
    hass: HomeAssistant, addon_manager: MagicMock, ensure_repository: AsyncMock
) -> None:
    addon_manager.async_get_addon_info.side_effect = [
        _info(AddonState.NOT_RUNNING, {CLOUDFLARED_OPT_TUNNEL_TOKEN: "stale"}),
        _info(AddonState.RUNNING),
    ]

    await async_install_tunnel(hass, TUNNEL_TOKEN)

    ensure_repository.assert_not_awaited()
    addon_manager.async_install_addon.assert_not_awaited()
    addon_manager.async_set_addon_options.assert_awaited_once_with(
        {CLOUDFLARED_OPT_TUNNEL_TOKEN: TUNNEL_TOKEN}
    )
    addon_manager.async_start_addon.assert_awaited_once()
    addon_manager.async_restart_addon.assert_not_awaited()


async def test_install_tunnel_restarts_running_addon_with_new_token(
    hass: HomeAssistant, addon_manager: MagicMock, ensure_repository: AsyncMock
) -> None:
    addon_manager.async_get_addon_info.side_effect = [
        _info(AddonState.RUNNING, {CLOUDFLARED_OPT_TUNNEL_TOKEN: "old-token"}),
        _info(AddonState.RUNNING, {CLOUDFLARED_OPT_TUNNEL_TOKEN: TUNNEL_TOKEN}),
    ]

    await async_install_tunnel(hass, TUNNEL_TOKEN)

    ensure_repository.assert_not_awaited()
    addon_manager.async_install_addon.assert_not_awaited()
    addon_manager.async_set_addon_options.assert_awaited_once_with(
        {CLOUDFLARED_OPT_TUNNEL_TOKEN: TUNNEL_TOKEN}
    )
    addon_manager.async_restart_addon.assert_awaited_once()
    addon_manager.async_start_addon.assert_not_awaited()


async def test_install_tunnel_is_a_noop_when_already_running_with_token(
    hass: HomeAssistant, addon_manager: MagicMock, ensure_repository: AsyncMock
) -> None:
    addon_manager.async_get_addon_info.return_value = _info(
        AddonState.RUNNING, {CLOUDFLARED_OPT_TUNNEL_TOKEN: TUNNEL_TOKEN}
    )

    await async_install_tunnel(hass, TUNNEL_TOKEN)

    ensure_repository.assert_not_awaited()
    addon_manager.async_install_addon.assert_not_awaited()
    addon_manager.async_set_addon_options.assert_not_awaited()
    addon_manager.async_restart_addon.assert_not_awaited()
    addon_manager.async_start_addon.assert_not_awaited()


async def test_install_tunnel_wraps_addon_error(
    hass: HomeAssistant, addon_manager: MagicMock, ensure_repository: AsyncMock
) -> None:
    addon_manager.async_get_addon_info.return_value = _info(AddonState.NOT_INSTALLED)
    addon_manager.async_install_addon.side_effect = AddonError(
        "Failed to install the Cloudflared add-on: no space left"
    )

    with pytest.raises(TunnelError, match="no space left"):
        await async_install_tunnel(hass, TUNNEL_TOKEN)

    addon_manager.async_start_addon.assert_not_awaited()


async def test_install_tunnel_wraps_repository_error(
    hass: HomeAssistant, addon_manager: MagicMock, ensure_repository: AsyncMock
) -> None:
    addon_manager.async_get_addon_info.return_value = _info(AddonState.NOT_INSTALLED)
    ensure_repository.side_effect = TunnelError("Could not add the Cloudflared repository")

    with pytest.raises(TunnelError, match="repository"):
        await async_install_tunnel(hass, TUNNEL_TOKEN)

    addon_manager.async_install_addon.assert_not_awaited()


async def test_install_tunnel_times_out_when_addon_never_runs(
    hass: HomeAssistant, addon_manager: MagicMock, ensure_repository: AsyncMock
) -> None:
    addon_manager.async_get_addon_info.return_value = _info(AddonState.NOT_RUNNING)

    with (
        patch.object(tunnel, "CLOUDFLARED_START_TIMEOUT", 0),
        pytest.raises(TunnelError, match="did not start in time"),
    ):
        await async_install_tunnel(hass, TUNNEL_TOKEN)

    addon_manager.async_start_addon.assert_awaited_once()
