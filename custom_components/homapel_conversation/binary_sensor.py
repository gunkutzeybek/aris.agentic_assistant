"""Binary sensor exposing the unit's active/dormant state."""
from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_UNIT_ID, DOMAIN
from .coordinator import HomapelCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: HomapelCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([HomapelActiveBinarySensor(coordinator, entry)])


class HomapelActiveBinarySensor(
    CoordinatorEntity[HomapelCoordinator], BinarySensorEntity
):
    _attr_has_entity_name = True
    _attr_name = "Active"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, coordinator: HomapelCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_active"
        unit_id = entry.data[CONF_UNIT_ID]
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, unit_id)})

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
