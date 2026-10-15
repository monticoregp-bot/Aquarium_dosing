"""Shared base for the per-pump entities.

Settings entities (number / time / select / switch) are *views* of the pump's
configuration held by the coordinator: writing one validates and saves the
whole configuration, and changes made anywhere else (the card, another entity)
are pushed back to all of them.
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import AquariumDosingCoordinator
from .dosing_engine import PumpConfig


class PumpEntity(Entity):
    """One entity of one pump."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self,
        coordinator: AquariumDosingCoordinator,
        entry: ConfigEntry,
        pump: str,
        key: str,
        name: str,
        icon: str | None = None,
    ) -> None:
        self._coordinator = coordinator
        self._pump = pump
        self._key = key
        self._attr_name = name
        self._attr_icon = icon
        self._attr_unique_id = f"{entry.entry_id}_{pump}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry.entry_id}_{pump}")},
            name=f"{coordinator.tank_name} {pump.replace('_', ' ').title()} pump",
            manufacturer="Aquarium Dosing",
            model="Dosing pump",
        )

    @property
    def _cfg(self) -> PumpConfig | None:
        return self._coordinator.pumps.get(self._pump)

    @property
    def _ledger(self) -> dict[str, Any]:
        return self._coordinator.ledgers.get(self._pump, {})

    @property
    def available(self) -> bool:
        return self._cfg is not None

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._coordinator.async_add_listener(self._handle_change))

    @callback
    def _handle_change(self) -> None:
        if self._cfg is not None:
            self.async_write_ha_state()

    async def _async_change(self, **changes: Any) -> None:
        """Change settings; an unsafe or invalid result is refused with a reason."""
        await self._async_act(self._coordinator.async_update_pump(self._pump, changes))

    @staticmethod
    async def _async_act(coro: Awaitable[Any]) -> None:
        try:
            await coro
        except ValueError as err:
            raise ServiceValidationError(str(err)) from err


@callback
def async_setup_pump_entities(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
    build: Callable[[AquariumDosingCoordinator, ConfigEntry, str], list[Entity]],
) -> None:
    """Create entities for every pump now, and for pumps added later."""
    coordinator: AquariumDosingCoordinator = hass.data[DOMAIN][entry.entry_id]
    known: set[str] = set()

    @callback
    def _sync() -> None:
        known.intersection_update(coordinator.pumps)  # forget deleted pumps
        new = [p for p in coordinator.pumps if p not in known]
        if new:
            known.update(new)
            async_add_entities([e for p in new for e in build(coordinator, entry, p)])

    _sync()
    entry.async_on_unload(coordinator.async_add_listener(_sync))
