"""The Homapel Conversation integration."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import HomapelApiError, HomapelCloudClient
from .const import CONF_API_BASE, CONF_API_KEY, CONF_UNIT_ID, DOMAIN
from .coordinator import HomapelCoordinator
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

    coordinator = HomapelCoordinator(hass, client, api_key, unit_id)
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    webhook_url = await async_register_webhook(hass, entry.entry_id, coordinator)
    try:
        await client.register_webhook(api_key, webhook_url)
    except HomapelApiError as err:
        _LOGGER.warning("Could not register webhook URL with cloud: %s", err)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    async_unregister_webhook(hass, entry.entry_id)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok
