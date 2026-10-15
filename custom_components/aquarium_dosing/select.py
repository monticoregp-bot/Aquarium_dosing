"""select entity: schedule mode of each pump."""
from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .dosing_engine import MODES
from .entity import PumpEntity, async_setup_pump_entities


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_setup_pump_entities(
        hass, entry, async_add_entities,
        lambda coord, e, pump: [PumpMode(coord, e, pump, "mode", "Schedule mode", "mdi:calendar-clock")],
    )


class PumpMode(PumpEntity, SelectEntity):
    _attr_entity_category = EntityCategory.CONFIG
    _attr_options = list(MODES)

    @property
    def current_option(self) -> str | None:
        cfg = self._cfg
        return None if cfg is None else cfg.mode

    async def async_select_option(self, option: str) -> None:
        await self._async_change(mode=option)
