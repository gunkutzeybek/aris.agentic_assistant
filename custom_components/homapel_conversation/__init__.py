"""The Homapel Conversation integration — makes a home a Laris home.

Setup order: cloud client → coordinator (first status poll; a 401 starts the
reauth flow) → inbound status webhook → entity platforms → connector manager
(re-registers the ha-mcp endpoint with the cloud after HA start) → the
"Laris" Assist pipeline, created once.
"""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import HomapelApiError, HomapelCloudClient
from .connector import ConnectorManager, async_revoke_cloud_credential
from .const import (
    CONF_API_BASE,
    CONF_API_KEY,
    CONF_CLOUD_REFRESH_TOKEN_ID,
    CONF_CLOUD_USER_ID,
    CONF_UNIT_ID,
    DOMAIN,
)
from .coordinator import HomapelCoordinator
from .pipeline import async_ensure_laris_pipeline
from .webhook import async_register_webhook, async_unregister_webhook

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.CONVERSATION,
    Platform.STT,
    Platform.TTS,
    Platform.BINARY_SENSOR,
    Platform.SENSOR,
]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Homapel Conversation from a config entry."""
    session = async_get_clientsession(hass)
    client = HomapelCloudClient(session, entry.data[CONF_API_BASE])
    api_key = entry.data[CONF_API_KEY]
    unit_id = entry.data[CONF_UNIT_ID]

    coordinator = HomapelCoordinator(hass, client, api_key, unit_id, config_entry=entry)
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    webhook_url = await async_register_webhook(hass, entry.entry_id, coordinator)
    try:
        await client.register_webhook(api_key, webhook_url)
    except HomapelApiError as err:
        _LOGGER.warning("Could not register webhook URL with cloud: %s", err)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    manager = ConnectorManager(hass, entry, client, coordinator)
    manager.async_setup()
    coordinator.connector_manager = manager
    entry.async_on_unload(manager.async_unload)

    # The voice pipeline needs the stt/tts entities, which exist only when the
    # cloud advertises voice; the helper is a no-op until they do.
    await async_ensure_laris_pipeline(hass, entry)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    async_unregister_webhook(hass, entry.entry_id)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Forget the home: revoke the cloud's HA credential and clear the connector.

    Order matters — the cloud is told first (while the bearer still works for
    nothing, the DELETE is authenticated by the API key), then the token and
    the "Laris Cloud" user go away so nothing can dial in anymore.
    """
    client = HomapelCloudClient(async_get_clientsession(hass), entry.data[CONF_API_BASE])
    try:
        await client.delete_connector(entry.data[CONF_API_KEY])
    except HomapelApiError as err:
        _LOGGER.warning("Could not clear the connector on the cloud: %s", err)

    await async_revoke_cloud_credential(
        hass,
        user_id=entry.data.get(CONF_CLOUD_USER_ID),
        refresh_token_id=entry.data.get(CONF_CLOUD_REFRESH_TOKEN_ID),
    )
