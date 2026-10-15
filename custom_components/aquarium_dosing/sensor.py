"""sensor entities: status, last dose, next dose, delivered in the last 24 h."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import STATUSES
from .entity import PumpEntity, async_setup_pump_entities


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_setup_pump_entities(
        hass, entry, async_add_entities,
        lambda coord, e, pump: [
            PumpStatus(coord, e, pump, "status", "Status", "mdi:information-outline"),
            PumpTimestamp(coord, e, pump, "last_dose", "Last dose", "mdi:history"),
            PumpTimestamp(coord, e, pump, "next_dose", "Next dose", "mdi:clock-outline"),
            PumpDelivered(coord, e, pump, "delivered_24h", "Delivered (24 h)", "mdi:beaker-outline"),
        ],
    )


class PumpStatus(PumpEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = STATUSES

    @property
    def native_value(self) -> str | None:
        return self._coordinator.status(self._pump) if self._cfg else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        cfg = self._cfg
        if cfg is None:
            return {}
        return {
            "switch": cfg.switch,
            "mode": cfg.mode,
            "window": "all hours" if cfg.all_hours else f"{cfg.window_start}-{cfg.window_end}",
            "master_on": self._coordinator.master_on,
            "fault": self._ledger.get("fault"),
        }


class PumpTimestamp(PumpEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    @property
    def native_value(self) -> datetime | None:
        if self._cfg is None:
            return None
        if self._key == "last_dose":
            return self._coordinator.last_dose(self._pump)
        return self._coordinator.next_dose(self._pump)


class PumpDelivered(PumpEntity, SensorEntity):
    _attr_native_unit_of_measurement = "mL"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 2

    @property
    def native_value(self) -> float | None:
        return self._coordinator.delivered_24h(self._pump) if self._cfg else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        cfg = self._cfg
        if cfg is None:
            return {}
        return {"daily_cap": cfg.effective_daily_cap(), "scheduled_daily": cfg.scheduled_daily_volume()}
