"""number entities: the numeric settings of each pump."""
from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .entity import PumpEntity, async_setup_pump_entities

# key, name, icon, min, max, step, unit
_NUMBERS = (
    ("volume", "Dose volume", "mdi:eyedropper", 0.01, 1000, 0.01, "mL"),
    ("multiplier", "Calibration multiplier", "mdi:timer-cog-outline", 0.1, 600, 0.01, "s/mL"),
    ("interval_minutes", "Interval", "mdi:timer-sand", 1, 1440, 1, UnitOfTime.MINUTES),
    ("offset_minutes", "Offset", "mdi:timer-plus-outline", 0, 1439, 1, UnitOfTime.MINUTES),
    ("daily_total", "Daily total", "mdi:beaker-outline", 0, 10000, 0.1, "mL"),
    ("doses_per_day", "Doses per day", "mdi:counter", 1, 288, 1, None),
    ("daily_cap", "Daily cap (0 = automatic)", "mdi:shield-alert-outline", 0, 10000, 0.1, "mL"),
    ("max_run_seconds", "Maximum run time", "mdi:timer-alert-outline", 1, 3600, 1, "s"),
    ("cooldown_minutes", "Cooldown", "mdi:timer-lock-outline", 0, 1440, 1, UnitOfTime.MINUTES),
)
_INTEGER_KEYS = {"interval_minutes", "offset_minutes", "doses_per_day", "max_run_seconds", "cooldown_minutes"}


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_setup_pump_entities(
        hass, entry, async_add_entities,
        lambda coord, e, pump: [PumpNumber(coord, e, pump, *spec) for spec in _NUMBERS],
    )


class PumpNumber(PumpEntity, NumberEntity):
    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator, entry, pump, key, name, icon, low, high, step, unit) -> None:
        super().__init__(coordinator, entry, pump, key, name, icon)
        self._attr_native_min_value = low
        self._attr_native_max_value = high
        self._attr_native_step = step
        self._attr_native_unit_of_measurement = unit

    @property
    def native_value(self) -> float | None:
        cfg = self._cfg
        return None if cfg is None else float(getattr(cfg, self._key))

    async def async_set_native_value(self, value: float) -> None:
        await self._async_change(**{self._key: int(value) if self._key in _INTEGER_KEYS else value})
