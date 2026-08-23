"""Config flow for Homapel Conversation — the customer's setup path.

    user ─────────── paste the API key from laris.homapel.com
      │               (validated on GET /v1/units/status; dormant is fine)
      ▼
    mcp_check ────── find the ha-mcp server entry
      │  missing / not loaded → mcp_missing / mcp_not_loaded (check again | skip)
      │  wrong options        → mcp_enable_auth (confirm) → mcp_wait (progress)
      ▼
    connector_url ── Nabu Casa → external_url → tunnel_* (Supervisor) → manual_url
      ▼
    connector_register (progress) ── mint bearer, PUT /v1/units/connector
      │  reachable      → finish
      │  not reachable  → connector_unreachable (retry | change URL | finish anyway)
      │  cloud error    → connector_error (retry | skip)
      ▼
    finish ────────── create the entry (or update + reload when reconfiguring)

Reauth (``reauth`` → ``reauth_confirm``) swaps a rotated API key; reconfigure
re-runs everything from ``mcp_check`` for a new URL or a regenerated webhook
id. Skipping the connector never fails the flow — the entry is created and a
``home_not_connected`` repair issue points the customer back here.
"""
from __future__ import annotations

import asyncio
from collections.abc import Mapping
import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    SOURCE_RECONFIGURE,
    ConfigEntry,
    ConfigEntryState,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.data_entry_flow import section
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import (
    HomapelApiError,
    HomapelAuthError,
    HomapelCloudClient,
    HomapelForbiddenError,
    HomapelInvalidRequestError,
    HomapelNetworkError,
    HomapelTimeoutError,
    HomapelTunnelNotConfiguredError,
    StatusResult,
)
from .connector import (
    ConnectorRegistration,
    async_can_use_tunnel,
    async_detect_base_url,
    async_enable_mcp_ha_auth_and_wait,
    async_find_mcp_server_entry,
    async_is_webhook_live,
    async_mcp_needs_options_change,
    async_mcp_webhook_id,
    async_register_connector,
    async_wait_for_mcp_webhook,
    is_https_url,
    normalize_base_url,
)
from .const import (
    CONF_API_BASE,
    CONF_API_KEY,
    CONF_CLOUD_REFRESH_TOKEN_ID,
    CONF_CLOUD_USER_ID,
    CONF_CONNECTOR_BASE_URL,
    CONF_CONNECTOR_SOURCE,
    CONF_CONVERSE_SOCK_READ,
    CONF_DEFAULT_LANGUAGE,
    CONF_MCP_ENTRY_ID,
    CONF_MCP_WEBHOOK_ID,
    CONF_UNIFIED_PIPELINE,
    CONF_UNIT_ID,
    CONNECTOR_SOURCE_MANUAL,
    CONNECTOR_SOURCE_TUNNEL,
    DASHBOARD_URL,
    DEFAULT_API_BASE,
    DEFAULT_CONVERSE_SOCK_READ,
    DEFAULT_LANGUAGE,
    DEFAULT_UNIFIED_PIPELINE,
    DOMAIN,
    ISSUE_HOME_NOT_CONNECTED,
    MCP_HACS_URL,
    MCP_MIN_HA_VERSION,
    MCP_WEBHOOK_WAIT_TIMEOUT,
    SUPPORTED_LANGUAGES,
)
from .tunnel import TunnelError, async_existing_tunnel_token, async_install_tunnel

_LOGGER = logging.getLogger(__name__)

CONF_ADVANCED = "advanced"
CONF_BASE_URL = "base_url"

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_API_KEY): str,
        vol.Required(CONF_ADVANCED): section(
            vol.Schema(
                {
                    vol.Optional(CONF_API_BASE, default=DEFAULT_API_BASE): str,
                    vol.Optional(CONF_DEFAULT_LANGUAGE, default=DEFAULT_LANGUAGE): vol.In(
                        SUPPORTED_LANGUAGES
                    ),
                }
            ),
            {"collapsed": True},
        ),
    }
)

STEP_REAUTH_SCHEMA = vol.Schema({vol.Required(CONF_API_KEY): str})


class HomapelConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Homapel Conversation."""

    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._mcp_entry_id: str | None = None
        self._webhook_id: str | None = None
        self._base_url: str | None = None
        self._source: str | None = None
        self._registration: ConnectorRegistration | None = None
        self._last_error: str = ""
        self._task: asyncio.Task[Any] | None = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return HomapelOptionsFlow()

    # --- shared helpers -----------------------------------------------------

    def _client(self) -> HomapelCloudClient:
        return HomapelCloudClient(async_get_clientsession(self.hass), self._data[CONF_API_BASE])

    async def _async_validate_key(self, api_base: str, api_key: str) -> tuple[StatusResult | None, str | None]:
        """Validate a key against the cloud. Returns (status, error_key)."""
        client = HomapelCloudClient(async_get_clientsession(self.hass), api_base)
        try:
            return await client.get_status(api_key), None
        except HomapelAuthError:
            return None, "invalid_auth"
        except HomapelForbiddenError:
            return None, "forbidden"
        except HomapelTimeoutError:
            return None, "timeout"
        except HomapelNetworkError:
            return None, "cannot_connect"
        except Exception:
            _LOGGER.exception("Unexpected error validating api_key")
            return None, "unknown"

    @property
    def _placeholders(self) -> dict[str, str]:
        return {
            "dashboard_url": DASHBOARD_URL,
            "hacs_url": MCP_HACS_URL,
            "min_ha_version": MCP_MIN_HA_VERSION,
            "error": self._last_error,
            "base_url": self._base_url or "",
            "example_url": "https://home.example.com",
        }

    # --- step: user ---------------------------------------------------------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            advanced = user_input.get(CONF_ADVANCED) or {}
            api_key = user_input[CONF_API_KEY].strip()
            api_base = normalize_base_url(advanced.get(CONF_API_BASE) or DEFAULT_API_BASE)
            default_language = advanced.get(CONF_DEFAULT_LANGUAGE, DEFAULT_LANGUAGE)

            status, error = await self._async_validate_key(api_base, api_key)
            if error:
                errors["base"] = error
            else:
                assert status is not None
                await self.async_set_unique_id(status.unit_id)
                self._abort_if_unique_id_configured()
                self._data = {
                    CONF_API_BASE: api_base,
                    CONF_API_KEY: api_key,
                    CONF_UNIT_ID: status.unit_id,
                    CONF_DEFAULT_LANGUAGE: default_language,
                }
                return await self.async_step_mcp_check()

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_SCHEMA,
            errors=errors,
            description_placeholders=self._placeholders,
        )

    # --- step: ha-mcp server entry -----------------------------------------

    async def async_step_mcp_check(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Locate the ha-mcp server entry and make sure it serves ha_auth."""
        entry = async_find_mcp_server_entry(self.hass)
        if entry is None:
            return await self.async_step_mcp_missing()
        self._mcp_entry_id = entry.entry_id

        if entry.state is not ConfigEntryState.LOADED:
            return await self.async_step_mcp_not_loaded()

        if async_mcp_needs_options_change(entry):
            return await self.async_step_mcp_enable_auth()

        webhook_id = async_mcp_webhook_id(entry)
        if webhook_id and async_is_webhook_live(self.hass, webhook_id):
            self._webhook_id = webhook_id
            return await self.async_step_connector_url()
        return await self.async_step_mcp_wait()

    async def async_step_mcp_missing(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="mcp_missing",
            menu_options=["mcp_check", "skip_connector"],
            description_placeholders=self._placeholders,
        )

    async def async_step_mcp_not_loaded(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="mcp_not_loaded",
            menu_options=["mcp_check", "skip_connector"],
            description_placeholders=self._placeholders,
        )

    async def async_step_mcp_enable_auth(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask before switching the HA-MCP server to ha_auth (it reloads)."""
        if user_input is None:
            return self.async_show_form(
                step_id="mcp_enable_auth",
                data_schema=vol.Schema({}),
                description_placeholders=self._placeholders,
            )
        assert self._mcp_entry_id is not None
        entry = self.hass.config_entries.async_get_entry(self._mcp_entry_id)
        if entry is None:
            return await self.async_step_mcp_check()
        self._task = self.hass.async_create_task(
            async_enable_mcp_ha_auth_and_wait(self.hass, entry, MCP_WEBHOOK_WAIT_TIMEOUT)
        )
        return await self.async_step_mcp_wait()

    async def async_step_mcp_wait(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Wait for ha-mcp to (re)load and register its webhook.

        The task is either the enable-auth-and-wait one created by the previous
        step, or a plain wait when the options were already right.
        """
        assert self._mcp_entry_id is not None
        if self._task is None:
            self._task = self.hass.async_create_task(
                async_wait_for_mcp_webhook(
                    self.hass, self._mcp_entry_id, MCP_WEBHOOK_WAIT_TIMEOUT
                )
            )
        if not self._task.done():
            return self.async_show_progress(
                step_id="mcp_wait",
                progress_action="mcp_wait",
                progress_task=self._task,
                description_placeholders=self._placeholders,
            )

        task, self._task = self._task, None
        try:
            webhook_id = await task
        except Exception:
            _LOGGER.exception("Waiting for the HA-MCP webhook failed")
            webhook_id = None

        if webhook_id is None:
            return self.async_show_progress_done(next_step_id="mcp_not_loaded")
        self._webhook_id = webhook_id
        return self.async_show_progress_done(next_step_id="connector_url")

    # --- step: base URL -----------------------------------------------------

    async def async_step_connector_url(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick how the cloud reaches this HA: Nabu Casa → external_url → tunnel → manual."""
        detected = async_detect_base_url(self.hass)
        if detected is not None:
            self._base_url, self._source = detected
            return await self.async_step_connector_register()
        if async_can_use_tunnel(self.hass):
            return await self.async_step_tunnel_check()
        return await self.async_step_manual_url()

    async def async_step_tunnel_check(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """A Cloudflared add-on the customer configured themselves is not overwritten silently."""
        try:
            existing = await async_existing_tunnel_token(self.hass)
        except TunnelError as err:
            self._last_error = str(err)
            return await self.async_step_tunnel_failed()
        if existing:
            return await self.async_step_tunnel_replace()
        return await self.async_step_tunnel_install()

    async def async_step_tunnel_replace(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="tunnel_replace",
            menu_options=["tunnel_install", "manual_url", "skip_connector"],
            description_placeholders=self._placeholders,
        )

    async def async_step_tunnel_install(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Issue the cloud tunnel and install/start the Cloudflared add-on."""
        if self._task is None:
            self._task = self.hass.async_create_task(self._async_install_tunnel())
        if not self._task.done():
            return self.async_show_progress(
                step_id="tunnel_install",
                progress_action="tunnel_install",
                progress_task=self._task,
                description_placeholders=self._placeholders,
            )

        task, self._task = self._task, None
        try:
            hostname = await task
        except HomapelTunnelNotConfiguredError:
            self._last_error = ""
            return self.async_show_progress_done(next_step_id="manual_url")
        except (HomapelApiError, TunnelError) as err:
            self._last_error = str(err)
            return self.async_show_progress_done(next_step_id="tunnel_failed")
        except Exception as err:
            _LOGGER.exception("Tunnel setup failed")
            self._last_error = str(err)
            return self.async_show_progress_done(next_step_id="tunnel_failed")

        self._base_url = f"https://{hostname}"
        self._source = CONNECTOR_SOURCE_TUNNEL
        return self.async_show_progress_done(next_step_id="connector_register")

    async def _async_install_tunnel(self) -> str:
        tunnel = await self._client().create_tunnel(self._data[CONF_API_KEY])
        await async_install_tunnel(self.hass, tunnel.tunnel_token)
        return tunnel.hostname

    async def async_step_tunnel_failed(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="tunnel_failed",
            menu_options=["tunnel_install", "manual_url", "skip_connector"],
            description_placeholders=self._placeholders,
        )

    async def async_step_manual_url(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the public https base URL of this Home Assistant."""
        errors: dict[str, str] = {}
        # When a progress task finishes before the frontend polls, HA re-enters
        # the next step with the *previous* step's input — only a submission of
        # this form carries ``base_url``.
        if user_input is not None and CONF_BASE_URL in user_input:
            base_url = normalize_base_url(user_input[CONF_BASE_URL])
            if not is_https_url(base_url):
                errors[CONF_BASE_URL] = "invalid_url"
            else:
                self._base_url = base_url
                self._source = CONNECTOR_SOURCE_MANUAL
                return await self.async_step_connector_register()

        current = self._base_url if self._source == CONNECTOR_SOURCE_MANUAL else ""
        return self.async_show_form(
            step_id="manual_url",
            data_schema=vol.Schema({vol.Required(CONF_BASE_URL, default=current): str}),
            errors=errors,
            description_placeholders=self._placeholders,
        )

    # --- step: register with the cloud --------------------------------------

    async def async_step_connector_register(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Provision the bearer and PUT the connector; the cloud probes it."""
        if self._task is None:
            self._task = self.hass.async_create_task(self._async_register())
        if not self._task.done():
            return self.async_show_progress(
                step_id="connector_register",
                progress_action="connector_register",
                progress_task=self._task,
                description_placeholders=self._placeholders,
            )

        task, self._task = self._task, None
        try:
            registration = await task
        except HomapelInvalidRequestError as err:
            # The cloud refused the URL itself (not https, …) — let the user fix it.
            self._last_error = str(err)
            return self.async_show_progress_done(next_step_id="manual_url")
        except HomapelApiError as err:
            self._last_error = str(err)
            return self.async_show_progress_done(next_step_id="connector_error")
        except Exception as err:
            _LOGGER.exception("Connector registration failed")
            self._last_error = str(err)
            return self.async_show_progress_done(next_step_id="connector_error")

        self._registration = registration
        self._data[CONF_CLOUD_USER_ID] = registration.credential.user_id
        self._data[CONF_CLOUD_REFRESH_TOKEN_ID] = registration.credential.refresh_token_id
        if registration.result.reachable:
            return self.async_show_progress_done(next_step_id="finish")
        self._last_error = registration.result.error or ""
        return self.async_show_progress_done(next_step_id="connector_unreachable")

    async def _async_register(self) -> ConnectorRegistration:
        assert self._base_url and self._source and self._webhook_id
        return await async_register_connector(
            self.hass,
            self._client(),
            self._data[CONF_API_KEY],
            base_url=self._base_url,
            source=self._source,
            webhook_id=self._webhook_id,
            user_id=self._data.get(CONF_CLOUD_USER_ID),
            refresh_token_id=self._data.get(CONF_CLOUD_REFRESH_TOKEN_ID),
        )

    async def async_step_connector_unreachable(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="connector_unreachable",
            menu_options=["connector_register", "manual_url", "finish_anyway"],
            description_placeholders=self._placeholders,
        )

    async def async_step_connector_error(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="connector_error",
            menu_options=["connector_register", "skip_connector"],
            description_placeholders=self._placeholders,
        )

    # --- finishing ----------------------------------------------------------

    async def async_step_skip_connector(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Finish without a connector; the repair issue brings the customer back.

        When reconfiguring a home that *had* a connector, the cloud is told to
        forget it too — otherwise it would keep dialing a dead endpoint and
        keep reporting the home as connected.
        """
        self._registration = None
        self._base_url = None
        self._source = None
        if self.source == SOURCE_RECONFIGURE and self._data.get(CONF_CONNECTOR_SOURCE):
            try:
                await self._client().delete_connector(self._data[CONF_API_KEY])
            except HomapelApiError as err:
                _LOGGER.warning("Could not clear the connector on the cloud: %s", err)
        return await self.async_step_finish()

    async def async_step_finish_anyway(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Keep the (currently unreachable) connector; the cloud re-probes later."""
        return await self.async_step_finish()

    async def async_step_finish(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        data = dict(self._data)
        if self._registration is not None and self._base_url and self._source:
            data[CONF_CONNECTOR_SOURCE] = self._source
            data[CONF_CONNECTOR_BASE_URL] = self._base_url
            data[CONF_MCP_ENTRY_ID] = self._mcp_entry_id
            data[CONF_MCP_WEBHOOK_ID] = self._webhook_id
            ir.async_delete_issue(self.hass, DOMAIN, ISSUE_HOME_NOT_CONNECTED)
        else:
            data.pop(CONF_CONNECTOR_SOURCE, None)
            data.pop(CONF_CONNECTOR_BASE_URL, None)
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                ISSUE_HOME_NOT_CONNECTED,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=ISSUE_HOME_NOT_CONNECTED,
                translation_placeholders={"dashboard_url": DASHBOARD_URL},
            )

        if self.source == SOURCE_RECONFIGURE:
            return self.async_update_reload_and_abort(
                self._get_reconfigure_entry(), data=data
            )
        return self.async_create_entry(title=f"Homapel ({data[CONF_UNIT_ID]})", data=data)

    # --- reauth -------------------------------------------------------------

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """The cloud rejected the stored key (rotated from the dashboard)."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()

        if user_input is not None:
            api_key = user_input[CONF_API_KEY].strip()
            status, error = await self._async_validate_key(entry.data[CONF_API_BASE], api_key)
            if error:
                errors["base"] = error
            else:
                assert status is not None
                await self.async_set_unique_id(status.unit_id)
                self._abort_if_unique_id_mismatch(reason="unit_mismatch")
                # The reload re-registers the connector with a fresh bearer.
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_API_KEY: api_key}
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=STEP_REAUTH_SCHEMA,
            errors=errors,
            description_placeholders=self._placeholders,
        )

    # --- reconfigure --------------------------------------------------------

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Re-run only the connector part (new base URL, regenerated webhook id).

        Shown as a confirmation form first: the initial step of a flow must not
        run straight into a progress step (the frontend has no rendering for a
        ``progress_done`` result coming back from flow creation).
        """
        entry = self._get_reconfigure_entry()
        self._data = dict(entry.data)
        self._base_url = entry.data.get(CONF_CONNECTOR_BASE_URL)
        self._source = entry.data.get(CONF_CONNECTOR_SOURCE)
        if user_input is None:
            return self.async_show_form(
                step_id="reconfigure",
                data_schema=vol.Schema({}),
                description_placeholders=self._placeholders,
            )
        return await self.async_step_mcp_check()


class HomapelOptionsFlow(OptionsFlow):
    """Per-entry tunables. Picked up on the next utterance — no reload needed."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current_sock_read = self.config_entry.options.get(
            CONF_CONVERSE_SOCK_READ, DEFAULT_CONVERSE_SOCK_READ
        )
        current_unified = self.config_entry.options.get(
            CONF_UNIFIED_PIPELINE, DEFAULT_UNIFIED_PIPELINE
        )
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_CONVERSE_SOCK_READ, default=current_sock_read
                ): vol.All(vol.Coerce(int), vol.Range(min=10, max=600)),
                vol.Required(
                    CONF_UNIFIED_PIPELINE, default=current_unified
                ): bool,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
