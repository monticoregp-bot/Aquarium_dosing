"""switch entities: schedule enabled, all hours, and priming."""
from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .entity import PumpEntity, async_setup_pump_entities

_FLAGS = (
    ("enabled", "Schedule enabled", "mdi:calendar-check"),
    ("all_hours", "Operate at all hours", "mdi:clock-time-twelve-outline"),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_setup_pump_entities(
        hass, entry, async_add_entities,
        lambda coord, e, pump: [
            *[PumpFlag(coord, e, pump, *spec) for spec in _FLAGS],
            PumpPrime(coord, e, pump, "prime", "Prime", "mdi:water-pump"),
        ],
    )


class PumpFlag(PumpEntity, SwitchEntity):
    """A boolean setting of the pump."""

    _attr_entity_category = EntityCategory.CONFIG

    @property
    def is_on(self) -> bool | None:
        cfg = self._cfg
        return None if cfg is None else bool(getattr(cfg, self._key))

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_change(**{self._key: True})

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_change(**{self._key: False})


class PumpPrime(PumpEntity, SwitchEntity):
    """Runs the pump for priming (up to its maximum run time) or until switched off."""

    @property
    def is_on(self) -> bool:
        return self._coordinator.status(self._pump) == "priming" if self._cfg else False

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_act(self._coordinator.async_set_prime(self._pump, True))

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_act(self._coordinator.async_set_prime(self._pump, False))
