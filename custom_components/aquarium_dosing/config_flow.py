"""Config flow: aquarium name, master toggle and safety defaults, then the first pump.

More pumps and all schedule editing are done from the Lovelace card or entities.
"""
from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_NAME
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
)

from .const import (
    CONF_INITIAL_PUMP,
    CONF_INITIAL_SWITCH,
    CONF_LATE_TOLERANCE,
    CONF_MASTER_ENTITY,
    CONF_ONE_AT_A_TIME,
    DEFAULT_LATE_TOLERANCE,
    DEFAULT_MASTER_ENTITY,
    DOMAIN,
)
from .dosing_engine import validate_pump_name


def _safety_fields(defaults: dict[str, Any]) -> dict[Any, Any]:
    return {
        vol.Required(CONF_ONE_AT_A_TIME, default=defaults.get(CONF_ONE_AT_A_TIME, True)): BooleanSelector(),
        vol.Required(
            CONF_LATE_TOLERANCE, default=defaults.get(CONF_LATE_TOLERANCE, DEFAULT_LATE_TOLERANCE)
        ): NumberSelector(
            NumberSelectorConfig(min=1, max=60, step=1, unit_of_measurement="min", mode=NumberSelectorMode.BOX)
        ),
    }


class AquariumDosingConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._settings: dict[str, Any] = {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            # one aquarium per master toggle; add the integration again for another tank
            await self.async_set_unique_id(user_input[CONF_MASTER_ENTITY])
            self._abort_if_unique_id_configured()
            self._settings = user_input
            return await self.async_step_pump()
        schema = vol.Schema(
            {
                vol.Required(CONF_NAME, default="Aquarium"): TextSelector(),
                vol.Required(CONF_MASTER_ENTITY): EntitySelector(EntitySelectorConfig(domain="input_boolean")),
                **_safety_fields({}),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema)

    async def async_step_pump(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        if user_input is not None:
            name = user_input["name"].strip().lower().replace(" ", "_")
            try:
                validate_pump_name(name)
            except ValueError:
                errors["name"] = "invalid_name"
            else:
                return self.async_create_entry(
                    title=f"Aquarium Dosing ({self._settings.get(CONF_NAME, 'Aquarium')})",
                    data={
                        **self._settings,
                        CONF_INITIAL_PUMP: name,
                        CONF_INITIAL_SWITCH: user_input.get("switch", ""),
                    },
                )
        schema = vol.Schema(
            {
                vol.Required("name", default="alk"): TextSelector(),
                vol.Optional("switch"): EntitySelector(EntitySelectorConfig(domain="switch")),
            }
        )
        return self.async_show_form(step_id="pump", data_schema=schema, errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry):
        return AquariumDosingOptionsFlow()


class AquariumDosingOptionsFlow(config_entries.OptionsFlow):
    """Change the master toggle or safety defaults. Reloads the entry on save."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        current = {**self.config_entry.data, **self.config_entry.options}
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_MASTER_ENTITY, default=current.get(CONF_MASTER_ENTITY) or DEFAULT_MASTER_ENTITY
                ): EntitySelector(EntitySelectorConfig(domain="input_boolean")),
                **_safety_fields(current),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
