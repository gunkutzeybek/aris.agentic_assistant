"""Binary sensors: subscription active/dormant and home connector reachable."""
from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_CONNECTOR_BASE_URL, CONF_CONNECTOR_SOURCE, DOMAIN
from .coordinator import HomapelCoordinator
from .entity import homapel_device_info


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: HomapelCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            HomapelActiveBinarySensor(coordinator, entry),
            HomapelHomeConnectedBinarySensor(coordinator, entry),
        ]
    )


class _HomapelBinarySensorBase(CoordinatorEntity[HomapelCoordinator], BinarySensorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator: HomapelCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_device_info = homapel_device_info(entry)


class HomapelActiveBinarySensor(_HomapelBinarySensorBase):
    """``on`` while the subscription is active (cost ceiling folded in)."""

    _attr_translation_key = "active"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, coordinator: HomapelCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_active"

    @property
    def is_on(self) -> bool | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.active

    @property
    def extra_state_attributes(self) -> dict[str, bool] | None:
        if self.coordinator.data is None:
            return None
        return {"cost_ceiling_reached": self.coordinator.data.cost_ceiling_reached}


class HomapelHomeConnectedBinarySensor(_HomapelBinarySensorBase):
    """``on`` when the cloud reports it can reach this home's ha-mcp endpoint.

    ``unknown`` when the cloud has not reported a connector block at all.
    """

    _attr_translation_key = "home_connected"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, coordinator: HomapelCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_home_connected"

    @property
    def is_on(self) -> bool | None:
        data = self.coordinator.data
        if data is None or data.connector_configured is None:
            return None
        return bool(data.connector_configured and data.connector_reachable)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        data = self.coordinator.data
        if data is None:
            return None
        since = data.connector_unreachable_since
        return {
            "configured": data.connector_configured,
            "source": self._entry.data.get(CONF_CONNECTOR_SOURCE),
            "base_url": self._entry.data.get(CONF_CONNECTOR_BASE_URL),
            "unreachable_since": since.isoformat() if since else None,
        }
