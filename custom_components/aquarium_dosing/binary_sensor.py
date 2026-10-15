"""binary_sensor: pump fault (latched until cleared)."""
from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .entity import PumpEntity, async_setup_pump_entities


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_setup_pump_entities(
        hass, entry, async_add_entities,
        lambda coord, e, pump: [PumpFault(coord, e, pump, "fault", "Fault", None)],
    )


class PumpFault(PumpEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    @property
    def is_on(self) -> bool | None:
        return bool(self._ledger.get("fault")) if self._cfg else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return dict(self._ledger.get("fault") or {})
