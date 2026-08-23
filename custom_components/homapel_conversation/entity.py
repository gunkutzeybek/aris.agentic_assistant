"""The one device every Homapel entity hangs off."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceInfo

from .const import CONF_UNIT_ID, DOMAIN


def homapel_device_info(entry: ConfigEntry) -> DeviceInfo:
    """Full device info from every platform, so the device name (and therefore
    the entity ids) do not depend on which platform registers first."""
    return DeviceInfo(
        identifiers={(DOMAIN, entry.data[CONF_UNIT_ID])},
        name="Homapel Aris",
        manufacturer="Homapel",
        model="Aris",
    )
