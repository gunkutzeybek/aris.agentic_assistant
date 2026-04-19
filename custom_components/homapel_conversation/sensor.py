"""Sensors: subscription tier + cloud health/latency."""
from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_UNIT_ID, DOMAIN
from .coordinator import HomapelCoordinator

TIER_OPTIONS = ["dormant", "basic", "pro"]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: HomapelCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            HomapelTierSensor(coordinator, entry),
            HomapelHealthSensor(coordinator, entry),
        ]
    )


class _HomapelSensorBase(CoordinatorEntity[HomapelCoordinator], SensorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator: HomapelCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        unit_id = entry.data[CONF_UNIT_ID]
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, unit_id)})


class HomapelTierSensor(_HomapelSensorBase):
    _attr_name = "Subscription tier"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = TIER_OPTIONS

    def __init__(self, coordinator: HomapelCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_tier"

    @property
    def native_value(self) -> str | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.tier or "dormant"


class HomapelHealthSensor(_HomapelSensorBase):
    _attr_name = "Last cloud latency"
    _attr_native_unit_of_measurement = UnitOfTime.MILLISECONDS
    _attr_device_class = SensorDeviceClass.DURATION

    def __init__(self, coordinator: HomapelCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_latency"

    @property
    def native_value(self) -> int | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.last_latency_ms

    @property
    def extra_state_attributes(self) -> dict[str, str | None] | None:
        if self.coordinator.data is None:
            return None
        last_at = self.coordinator.data.last_converse_at
        return {
            "last_error": self.coordinator.data.last_error,
            "last_converse_at": last_at.isoformat() if last_at else None,
        }
