"""Home connector — how the Laris cloud reaches this Home Assistant.

The cloud calls back into the home through the ha-mcp in-process server
(custom component ``ha_mcp_tools``) at ``<HA base URL>/api/webhook/<id>``,
authenticating with ``Authorization: Bearer <HA long-lived access token>``
(ha-mcp's ``ha_auth`` mode). This module owns everything around that:

* finding the ha-mcp server entry and flipping it to ``ha_auth``;
* provisioning the "Laris Cloud" admin user + long-lived refresh token whose
  access token is the bearer (only the *ids* are persisted, never the token);
* deciding which public base URL the cloud should dial (Nabu Casa →
  ``external_url`` → cloud-issued Cloudflare tunnel → manual);
* registering ``{mcp_url, bearer, source}`` with ``PUT /v1/units/connector``;
* at runtime, re-registering after HA start and whenever ha-mcp's webhook id
  changes, so the cloud always holds a working URL + a valid bearer.

The config flow and ``__init__`` both build on these helpers; nothing here
renders UI.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
import logging
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from homeassistant.auth.const import GROUP_ID_ADMIN
from homeassistant.auth.models import TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN
from homeassistant.config_entries import (
    SIGNAL_CONFIG_ENTRY_CHANGED,
    ConfigEntry,
    ConfigEntryChange,
    ConfigEntryState,
)
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, __version__ as HA_VERSION
from homeassistant.core import CoreState, Event, HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.hassio import is_hassio
from homeassistant.loader import async_get_custom_components

from .api import ConnectorProbeResult, HomapelApiError, HomapelCloudClient
from .const import (
    CLOUD_ACCESS_TOKEN_DAYS,
    CLOUD_TOKEN_CLIENT_NAME,
    CLOUD_USER_NAME,
    CONF_API_KEY,
    CONF_CLOUD_REFRESH_TOKEN_ID,
    CONF_CLOUD_USER_ID,
    CONF_CONNECTOR_BASE_URL,
    CONF_CONNECTOR_SOURCE,
    CONF_MCP_ENTRY_ID,
    CONF_MCP_WEBHOOK_ID,
    CONNECTOR_SOURCE_EXTERNAL_URL,
    CONNECTOR_SOURCE_NABU_CASA,
    MCP_DATA_WEBHOOK_ID,
    MCP_DOMAIN,
    MCP_ENTRY_TYPE_KEY,
    MCP_ENTRY_TYPE_SERVER,
    MCP_OPT_ENABLE_WEBHOOK,
    MCP_OPT_WEBHOOK_AUTH,
    MCP_RELOAD_GRACE,
    MCP_WEBHOOK_AUTH_HA,
    MCP_WEBHOOK_STARTUP_WAIT_TIMEOUT,
)

if TYPE_CHECKING:
    from .coordinator import HomapelCoordinator

_LOGGER = logging.getLogger(__name__)

# hass.data key the ``webhook`` component keeps its handler table under.
_WEBHOOK_DATA_KEY = "webhook"
_WAIT_POLL_SECONDS = 1.0


# --- ha-mcp server entry ------------------------------------------------------


@callback
def async_find_mcp_server_entry(hass: HomeAssistant) -> ConfigEntry | None:
    """The ``ha_mcp_tools`` *server* entry (the component also has a tools entry)."""
    for entry in hass.config_entries.async_entries(MCP_DOMAIN):
        if entry.data.get(MCP_ENTRY_TYPE_KEY) == MCP_ENTRY_TYPE_SERVER:
            return entry
    return None


@callback
def async_mcp_webhook_id(entry: ConfigEntry | None) -> str | None:
    """The webhook id ha-mcp minted for its endpoint (``mcp_<hex>``)."""
    if entry is None:
        return None
    webhook_id = entry.data.get(MCP_DATA_WEBHOOK_ID)
    return str(webhook_id) if webhook_id else None


@callback
def async_mcp_needs_options_change(entry: ConfigEntry) -> bool:
    """True unless the entry already serves the webhook in ``ha_auth`` mode."""
    options = entry.options
    return (
        options.get(MCP_OPT_WEBHOOK_AUTH) != MCP_WEBHOOK_AUTH_HA
        or not options.get(MCP_OPT_ENABLE_WEBHOOK, True)
    )


@callback
def async_is_webhook_live(hass: HomeAssistant, webhook_id: str | None) -> bool:
    """Whether the ``webhook`` component currently routes this id to a handler.

    ha-mcp registers its webhook only at the end of its background bring-up
    (which pip-installs the server the first time), so "entry loaded" is not
    the same as "endpoint answers".
    """
    if not webhook_id:
        return False
    handlers = hass.data.get(_WEBHOOK_DATA_KEY)
    return isinstance(handlers, dict) and webhook_id in handlers


@callback
def async_enable_mcp_ha_auth(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Flip the server entry to ``webhook_auth=ha_auth`` + ``enable_webhook``.

    ha-mcp reloads itself from its options update listener. Returns whether
    anything changed.
    """
    new_options = {
        **entry.options,
        MCP_OPT_WEBHOOK_AUTH: MCP_WEBHOOK_AUTH_HA,
        MCP_OPT_ENABLE_WEBHOOK: True,
    }
    return hass.config_entries.async_update_entry(entry, options=new_options)


class McpReloadWatcher:
    """Notices the ha-mcp entry going through a reload.

    HA dispatches ``SIGNAL_CONFIG_ENTRY_CHANGED`` on every state transition, so
    "left LOADED, then came back to LOADED" is observable — and with eager task
    start the whole reload may even run inside ``async_update_entry`` itself,
    before the caller gets control back. Subscribe *before* changing options.
    """

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._entry_id = entry_id
        self._left_loaded = False
        self._reloaded = asyncio.Event()
        self._unsub = async_dispatcher_connect(
            hass, SIGNAL_CONFIG_ENTRY_CHANGED, self._async_changed
        )

    @callback
    def _async_changed(self, change: ConfigEntryChange, entry: ConfigEntry) -> None:
        if entry.entry_id != self._entry_id or change is not ConfigEntryChange.UPDATED:
            return
        if entry.state is not ConfigEntryState.LOADED:
            self._left_loaded = True
        elif self._left_loaded:
            self._reloaded.set()

    async def async_wait_for_reload(self, grace: float) -> bool:
        """Wait until the entry has reloaded; False if it never started within ``grace``."""
        try:
            async with asyncio.timeout(grace):
                await self._reloaded.wait()
        except TimeoutError:
            return False
        return True

    @callback
    def async_unsubscribe(self) -> None:
        self._unsub()


async def async_enable_mcp_ha_auth_and_wait(
    hass: HomeAssistant, entry: ConfigEntry, timeout: float
) -> str | None:
    """Switch ha-mcp to ha_auth, let it reload, and wait for its webhook.

    Without the reload watch the old (pre-reload) LOADED state and the old
    webhook registration would satisfy the wait immediately and the cloud
    would probe a server that is mid-restart. Returns the live webhook id
    or ``None`` on timeout.
    """
    watcher = McpReloadWatcher(hass, entry.entry_id)
    try:
        if async_enable_mcp_ha_auth(hass, entry) and not await watcher.async_wait_for_reload(
            MCP_RELOAD_GRACE
        ):
            _LOGGER.debug("ha-mcp did not reload after the options change; continuing")
    finally:
        watcher.async_unsubscribe()
    return await async_wait_for_mcp_webhook(hass, entry.entry_id, timeout)


async def async_wait_for_mcp_webhook(
    hass: HomeAssistant, entry_id: str, timeout: float
) -> str | None:
    """Wait until the ha-mcp entry is loaded *and* its webhook is registered.

    Returns the live webhook id, or ``None`` on timeout / entry gone. The id
    is re-read every poll because a reload may regenerate it.
    """
    loop = hass.loop
    deadline = loop.time() + timeout
    while True:
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is None:
            return None
        webhook_id = async_mcp_webhook_id(entry)
        if entry.state is ConfigEntryState.LOADED and async_is_webhook_live(
            hass, webhook_id
        ):
            return webhook_id
        if loop.time() >= deadline:
            return None
        await asyncio.sleep(_WAIT_POLL_SECONDS)


async def async_mcp_component_version(hass: HomeAssistant) -> str | None:
    """Version of the installed ``ha_mcp_tools`` component, if any."""
    try:
        components = await async_get_custom_components(hass)
    except Exception:
        return None
    integration = components.get(MCP_DOMAIN)
    return str(integration.version) if integration and integration.version else None


# --- "Laris Cloud" credential -------------------------------------------------


@dataclass(slots=True)
class CloudCredential:
    """An admin long-lived token the cloud presents to ha-mcp."""

    user_id: str
    refresh_token_id: str
    access_token: str


async def async_provision_cloud_credential(
    hass: HomeAssistant,
    *,
    user_id: str | None = None,
    refresh_token_id: str | None = None,
) -> CloudCredential:
    """Create (or reuse) the "Laris Cloud" user + long-lived refresh token and
    mint a fresh access token.

    Mirrors what ha-mcp does for its own loopback credential: an active,
    non-system-generated admin user is exactly what its ``ha_auth`` gate
    accepts. ``local_only`` is False because the cloud dials in from the
    internet. The access token is returned only — callers persist the two ids.
    """
    auth = hass.auth

    user = await auth.async_get_user(user_id) if user_id else None
    if user is not None and (not user.is_active or not user.is_admin):
        # Someone edited the user in Settings → People; a token minted for it
        # would be rejected by ha-mcp, so start over.
        _LOGGER.warning("%s user was changed; re-provisioning", CLOUD_USER_NAME)
        user = None
    if user is None:
        user = await auth.async_create_user(
            CLOUD_USER_NAME, group_ids=[GROUP_ID_ADMIN], local_only=False
        )
        refresh_token_id = None

    refresh_token = auth.async_get_refresh_token(refresh_token_id) if refresh_token_id else None
    if refresh_token is not None and refresh_token.user.id != user.id:
        refresh_token = None

    if refresh_token is None:
        # client_name is unique per user for long-lived tokens: clear any
        # leftover from a partial earlier provision before creating ours.
        for token in list(user.refresh_tokens.values()):
            if (
                token.client_name == CLOUD_TOKEN_CLIENT_NAME
                and token.token_type == TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN
            ):
                auth.async_remove_refresh_token(token)
        refresh_token = await auth.async_create_refresh_token(
            user,
            client_name=CLOUD_TOKEN_CLIENT_NAME,
            token_type=TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN,
            access_token_expiration=timedelta(days=CLOUD_ACCESS_TOKEN_DAYS),
        )

    access_token = auth.async_create_access_token(refresh_token)
    return CloudCredential(
        user_id=user.id, refresh_token_id=refresh_token.id, access_token=access_token
    )


async def async_revoke_cloud_credential(
    hass: HomeAssistant, *, user_id: str | None, refresh_token_id: str | None
) -> None:
    """Revoke the refresh token (invalidating every access token minted from
    it) and delete the "Laris Cloud" user. Idempotent."""
    auth = hass.auth
    if refresh_token_id:
        refresh_token = auth.async_get_refresh_token(refresh_token_id)
        if refresh_token is not None:
            auth.async_remove_refresh_token(refresh_token)
    if user_id:
        user = await auth.async_get_user(user_id)
        if user is not None:
            await auth.async_remove_user(user)


# --- Base URL -----------------------------------------------------------------


def is_https_url(url: str | None) -> bool:
    if not url:
        return False
    parts = urlsplit(url.strip())
    return parts.scheme == "https" and bool(parts.netloc)


def normalize_base_url(url: str) -> str:
    return url.strip().rstrip("/")


def build_mcp_url(base_url: str, webhook_id: str) -> str:
    return f"{normalize_base_url(base_url)}/api/webhook/{webhook_id}"


@callback
def async_detect_base_url(hass: HomeAssistant) -> tuple[str, str] | None:
    """Public base URL the cloud can dial without any extra setup.

    Nabu Casa remote UI first (it proxies ``/api/webhook/*`` and is what most
    Turkish customers with a subscription already have), then a configured
    https ``external_url``. Returns ``(base_url, source)`` or ``None``.
    """
    try:
        from homeassistant.components import cloud

        remote_url = cloud.async_remote_ui_url(hass)
    except Exception:
        remote_url = None
    if is_https_url(remote_url):
        return normalize_base_url(remote_url), CONNECTOR_SOURCE_NABU_CASA

    external_url = hass.config.external_url
    if is_https_url(external_url):
        return normalize_base_url(external_url), CONNECTOR_SOURCE_EXTERNAL_URL

    return None


@callback
def async_can_use_tunnel(hass: HomeAssistant) -> bool:
    """The cloud-issued Cloudflare tunnel needs the Supervisor (add-on host)."""
    return is_hassio(hass)


# --- Registration with the cloud ---------------------------------------------


@dataclass(slots=True)
class ConnectorRegistration:
    """Outcome of one ``PUT /v1/units/connector``."""

    mcp_url: str
    source: str
    credential: CloudCredential
    result: ConnectorProbeResult


async def async_register_connector(
    hass: HomeAssistant,
    client: HomapelCloudClient,
    api_key: str,
    *,
    base_url: str,
    source: str,
    webhook_id: str,
    user_id: str | None,
    refresh_token_id: str | None,
) -> ConnectorRegistration:
    """Mint a fresh bearer and register the endpoint with the cloud.

    Raises the usual ``HomapelApiError`` family on cloud failures.
    """
    credential = await async_provision_cloud_credential(
        hass, user_id=user_id, refresh_token_id=refresh_token_id
    )
    mcp_url = build_mcp_url(base_url, webhook_id)
    result = await client.put_connector(
        api_key,
        mcp_url=mcp_url,
        bearer=credential.access_token,
        source=source,
        ha_version=HA_VERSION,
        component_version=await async_mcp_component_version(hass),
    )
    _LOGGER.info(
        "Connector registered (%s): reachable=%s tools=%s%s",
        source,
        result.reachable,
        result.tool_count,
        f" error={result.error}" if result.error else "",
    )
    return ConnectorRegistration(
        mcp_url=mcp_url, source=source, credential=credential, result=result
    )


# --- Runtime manager ----------------------------------------------------------


class ConnectorManager:
    """Keeps the cloud's connector record fresh while the entry is loaded.

    Re-registers (same payload, fresh access token) after HA has started and
    whenever the ha-mcp server entry's webhook id changes. Does nothing for an
    entry that finished setup without a connector ("skip for now") — the
    repair issue covers that case.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: HomapelCloudClient,
        coordinator: HomapelCoordinator,
    ) -> None:
        self._hass = hass
        self._entry = entry
        self._client = client
        self._coordinator = coordinator
        self._unsubs: list[Callable[[], None]] = []
        self._task: asyncio.Task[None] | None = None

    @property
    def configured(self) -> bool:
        data = self._entry.data
        return bool(data.get(CONF_CONNECTOR_SOURCE) and data.get(CONF_CONNECTOR_BASE_URL))

    @callback
    def async_setup(self) -> None:
        if not self.configured:
            return

        self._unsubs.append(
            async_dispatcher_connect(
                self._hass, SIGNAL_CONFIG_ENTRY_CHANGED, self._async_entry_changed
            )
        )

        if self._hass.state is CoreState.running:
            self.async_schedule_reprobe()
        else:
            self._unsubs.append(
                self._hass.bus.async_listen_once(
                    EVENT_HOMEASSISTANT_STARTED, self._async_on_started
                )
            )

    @callback
    def async_unload(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = None

    @callback
    def _async_on_started(self, _event: Event) -> None:
        self.async_schedule_reprobe()

    @callback
    def _async_entry_changed(self, change: ConfigEntryChange, entry: ConfigEntry) -> None:
        if change is not ConfigEntryChange.UPDATED or entry.domain != MCP_DOMAIN:
            return
        if entry.data.get(MCP_ENTRY_TYPE_KEY) != MCP_ENTRY_TYPE_SERVER:
            return
        new_id = async_mcp_webhook_id(entry)
        if new_id and new_id != self._entry.data.get(CONF_MCP_WEBHOOK_ID):
            _LOGGER.info("ha-mcp webhook id changed; re-registering connector")
            self.async_schedule_reprobe()

    @callback
    def async_schedule_reprobe(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._task = self._entry.async_create_background_task(
            self._hass, self.async_reprobe(), "homapel_connector_reprobe"
        )

    async def async_reprobe(
        self, *, wait_timeout: float = MCP_WEBHOOK_STARTUP_WAIT_TIMEOUT
    ) -> ConnectorRegistration | None:
        """Re-send the connector with a fresh bearer. Best-effort; never raises."""
        data = self._entry.data
        mcp_entry = self._resolve_mcp_entry()
        if mcp_entry is None:
            _LOGGER.warning("ha-mcp server entry not found; connector not re-registered")
            return None

        webhook_id = await async_wait_for_mcp_webhook(
            self._hass, mcp_entry.entry_id, wait_timeout
        )
        if webhook_id is None:
            _LOGGER.warning(
                "ha-mcp webhook did not come up within %ss; connector not re-registered",
                wait_timeout,
            )
            return None

        try:
            registration = await async_register_connector(
                self._hass,
                self._client,
                data[CONF_API_KEY],
                base_url=data[CONF_CONNECTOR_BASE_URL],
                source=data[CONF_CONNECTOR_SOURCE],
                webhook_id=webhook_id,
                user_id=data.get(CONF_CLOUD_USER_ID),
                refresh_token_id=data.get(CONF_CLOUD_REFRESH_TOKEN_ID),
            )
        except HomapelApiError as err:
            _LOGGER.warning("Could not re-register connector with cloud: %s", err)
            return None

        updates: dict[str, Any] = {}
        if registration.credential.user_id != data.get(CONF_CLOUD_USER_ID):
            updates[CONF_CLOUD_USER_ID] = registration.credential.user_id
        if registration.credential.refresh_token_id != data.get(CONF_CLOUD_REFRESH_TOKEN_ID):
            updates[CONF_CLOUD_REFRESH_TOKEN_ID] = registration.credential.refresh_token_id
        if webhook_id != data.get(CONF_MCP_WEBHOOK_ID):
            updates[CONF_MCP_WEBHOOK_ID] = webhook_id
        if mcp_entry.entry_id != data.get(CONF_MCP_ENTRY_ID):
            updates[CONF_MCP_ENTRY_ID] = mcp_entry.entry_id
        if updates:
            self._hass.config_entries.async_update_entry(
                self._entry, data={**data, **updates}
            )

        self._coordinator.record_connector(
            configured=True, reachable=registration.result.reachable
        )
        return registration

    @callback
    def _resolve_mcp_entry(self) -> ConfigEntry | None:
        entry_id = self._entry.data.get(CONF_MCP_ENTRY_ID)
        if entry_id:
            entry = self._hass.config_entries.async_get_entry(entry_id)
            if entry is not None and entry.domain == MCP_DOMAIN:
                return entry
        return async_find_mcp_server_entry(self._hass)
