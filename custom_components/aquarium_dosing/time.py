"""time entities: the daily operating window of each pump."""
from __future__ import annotations

from datetime import time

from homeassistant.components.time import TimeEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .dosing_engine import parse_hhmm
from .entity import PumpEntity, async_setup_pump_entities

_TIMES = (
    ("window_start", "Window start", "mdi:clock-start"),
    ("window_end", "Window end", "mdi:clock-end"),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_setup_pump_entities(
        hass, entry, async_add_entities,
        lambda coord, e, pump: [PumpTime(coord, e, pump, *spec) for spec in _TIMES],
    )


class PumpTime(PumpEntity, TimeEntity):
    _attr_entity_category = EntityCategory.CONFIG

    @property
    def native_value(self) -> time | None:
        cfg = self._cfg
        if cfg is None:
            return None
        minutes = parse_hhmm(getattr(cfg, self._key))
        return time(minutes // 60, minutes % 60)

    async def async_set_value(self, value: time) -> None:
        await self._async_change(**{self._key: f"{value.hour:02d}:{value.minute:02d}"})
