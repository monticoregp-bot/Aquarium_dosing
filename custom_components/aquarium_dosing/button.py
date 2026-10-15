"""button entities: dose now, calibration dose, clear fault."""
from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .entity import PumpEntity, async_setup_pump_entities


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_setup_pump_entities(
        hass, entry, async_add_entities,
        lambda coord, e, pump: [
            PumpButton(coord, e, pump, "dose_now", "Dose now", "mdi:eyedropper-variant"),
            PumpButton(coord, e, pump, "calibration_dose", "Calibration dose (10 mL)", "mdi:beaker-check-outline"),
            PumpButton(coord, e, pump, "clear_fault", "Clear fault", "mdi:alert-remove-outline"),
        ],
    )


class PumpButton(PumpEntity, ButtonEntity):
    async def async_press(self) -> None:
        if self._key == "dose_now":
            await self._async_act(self._coordinator.async_dose(self._pump))
        elif self._key == "calibration_dose":
            await self._async_act(self._coordinator.async_calibration_dose(self._pump))
        else:
            await self._async_act(self._coordinator.async_clear_fault(self._pump))
