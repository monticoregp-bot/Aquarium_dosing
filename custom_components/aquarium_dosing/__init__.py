"""Aquarium Dosing - scheduled, safety-checked dosing pumps for Home Assistant."""
from __future__ import annotations

import logging
from pathlib import Path

import voluptuous as vol
from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_ENTITY_ID, Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er

from . import api
from .const import DOMAIN, FRONTEND_URL, VERSION
from .coordinator import AquariumDosingCoordinator

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.TIME,
]
_GLOBAL_KEY = f"{DOMAIN}_global_setup_done"


def resolve_pump(hass: HomeAssistant, entity_id: str) -> tuple[AquariumDosingCoordinator, str]:
    """Find the coordinator and pump an entity of this integration belongs to."""
    entity = er.async_get(hass).async_get(entity_id)
    if entity is None or entity.platform != DOMAIN or entity.config_entry_id not in hass.data.get(DOMAIN, {}):
        raise ServiceValidationError(f"{entity_id} is not an Aquarium Dosing entity")
    coordinator: AquariumDosingCoordinator = hass.data[DOMAIN][entity.config_entry_id]
    rest = (entity.unique_id or "").removeprefix(f"{entity.config_entry_id}_")
    matches = [p for p in coordinator.pumps if rest.startswith(f"{p}_")]
    if not matches:
        raise ServiceValidationError(f"Could not work out which pump {entity_id} belongs to")
    return coordinator, max(matches, key=len)


async def _async_setup_global(hass: HomeAssistant) -> None:
    """Register the websocket API, services and Lovelace cards once per HA run."""
    if hass.data.get(_GLOBAL_KEY):
        return
    api.async_register(hass)
    card = Path(__file__).parent / "frontend" / "aquarium-dosing-card.js"
    await hass.http.async_register_static_paths(
        [StaticPathConfig(FRONTEND_URL, str(card), cache_headers=False)]
    )
    # If the cards are missing on your HA version, add FRONTEND_URL as a dashboard
    # resource (see the README).
    add_extra_js_url(hass, f"{FRONTEND_URL}?v={VERSION}")

    async def _dose(call: ServiceCall) -> None:
        for entity_id in call.data[ATTR_ENTITY_ID]:
            coordinator, pump = resolve_pump(hass, entity_id)
            try:
                await coordinator.async_dose(
                    pump, call.data.get("volume"), ignore_window=call.data.get("ignore_window", False)
                )
            except ValueError as err:
                raise ServiceValidationError(str(err)) from err

    async def _calibrate(call: ServiceCall) -> None:
        for entity_id in call.data[ATTR_ENTITY_ID]:
            coordinator, pump = resolve_pump(hass, entity_id)
            try:
                await coordinator.async_calibrate(pump, call.data["measured_ml"])
            except ValueError as err:
                raise ServiceValidationError(str(err)) from err

    hass.services.async_register(
        DOMAIN, "dose", _dose,
        vol.Schema({
            vol.Required(ATTR_ENTITY_ID): cv.entity_ids,
            vol.Optional("volume"): vol.All(vol.Coerce(float), vol.Range(min=0.01, max=1000)),
            vol.Optional("ignore_window", default=False): cv.boolean,
        }),
    )
    hass.services.async_register(
        DOMAIN, "calibrate", _calibrate,
        vol.Schema({
            vol.Required(ATTR_ENTITY_ID): cv.entity_ids,
            vol.Required("measured_ml"): vol.All(vol.Coerce(float), vol.Range(min=0.1, max=1000)),
        }),
    )
    hass.data[_GLOBAL_KEY] = True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    await _async_setup_global(hass)
    coordinator = AquariumDosingCoordinator(hass, entry)
    await coordinator.async_load()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    coordinator.async_start()
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator: AquariumDosingCoordinator = hass.data[DOMAIN][entry.entry_id]
    await coordinator.async_stop()  # running pumps are switched off on the way out
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unloaded


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
