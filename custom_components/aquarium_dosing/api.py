"""Websocket API for the Aquarium Dosing Lovelace cards.

Every command takes an optional ``master_entity`` (the aquarium's master toggle)
to say which aquarium you mean; with a single aquarium it can be omitted.
"""
from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import AquariumDosingCoordinator
from .dosing_engine import PumpConfig, minute_of_day, slots_on_date

_AQ = {vol.Optional("master_entity"): str}
_Conn = websocket_api.ActiveConnection


def _resolve(hass: HomeAssistant, connection: _Conn, msg: dict[str, Any]) -> AquariumDosingCoordinator | None:
    coordinators: list[AquariumDosingCoordinator] = list(hass.data.get(DOMAIN, {}).values())
    wanted = msg.get("master_entity")
    if wanted:
        for coordinator in coordinators:
            if coordinator.master_entity == wanted:
                return coordinator
        connection.send_error(
            msg["id"], "not_found",
            f"No Aquarium Dosing entry uses {wanted}. Check master_entity in the card config.",
        )
        return None
    if len(coordinators) == 1:
        return coordinators[0]
    if not coordinators:
        connection.send_error(msg["id"], "not_configured", "Aquarium Dosing is not set up")
    else:
        connection.send_error(
            msg["id"], "ambiguous", "Several aquariums are configured; set master_entity in the card config."
        )
    return None


async def _action(connection: _Conn, msg: dict[str, Any], coordinator: AquariumDosingCoordinator, coro) -> None:
    """Run a coordinator action; refusals come back as readable errors."""
    try:
        await coro
    except ValueError as err:
        connection.send_error(msg["id"], "refused", str(err))
        return
    connection.send_result(msg["id"], coordinator.snapshot())


@callback
def async_register(hass: HomeAssistant) -> None:
    for command in (ws_get, ws_preview, ws_set_pump, ws_delete_pump, ws_dose,
                    ws_calibration_dose, ws_calibrate, ws_clear_fault):
        websocket_api.async_register_command(hass, command)


@websocket_api.websocket_command({vol.Required("type"): "aquarium_dosing/get", **_AQ})
@callback
def ws_get(hass: HomeAssistant, connection: _Conn, msg: dict[str, Any]) -> None:
    if (coordinator := _resolve(hass, connection, msg)) is not None:
        connection.send_result(msg["id"], coordinator.snapshot())


@websocket_api.websocket_command(
    {vol.Required("type"): "aquarium_dosing/preview", vol.Required("config"): dict}
)
@callback
def ws_preview(hass: HomeAssistant, connection: _Conn, msg: dict[str, Any]) -> None:
    """Evaluate an UNSAVED draft with the same engine that runs the pumps."""
    try:
        cfg = PumpConfig.from_dict(msg["config"])
    except ValueError as err:
        connection.send_error(msg["id"], "invalid_config", str(err))
        return
    now = dt_util.now()
    connection.send_result(
        msg["id"],
        {
            "slots": [
                {"at": s.at.isoformat(), "minute": s.at.hour * 60 + s.at.minute, "volume": s.volume, "state": "pending"}
                for s in slots_on_date(cfg, now.date(), now.tzinfo)
            ],
            "scheduled_daily": cfg.scheduled_daily_volume(),
            "daily_cap": cfg.effective_daily_cap(),
            "manual_volume": cfg.manual_volume(),
            "now_minute": minute_of_day(now),
        },
    )


@websocket_api.require_admin
@websocket_api.websocket_command(
    {vol.Required("type"): "aquarium_dosing/set_pump", vol.Required("name"): str, vol.Required("config"): dict, **_AQ}
)
@websocket_api.async_response
async def ws_set_pump(hass: HomeAssistant, connection: _Conn, msg: dict[str, Any]) -> None:
    if (c := _resolve(hass, connection, msg)) is not None:
        await _action(connection, msg, c, c.async_set_pump(msg["name"], msg["config"]))


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): "aquarium_dosing/delete_pump", vol.Required("name"): str, **_AQ})
@websocket_api.async_response
async def ws_delete_pump(hass: HomeAssistant, connection: _Conn, msg: dict[str, Any]) -> None:
    if (c := _resolve(hass, connection, msg)) is not None:
        await _action(connection, msg, c, c.async_delete_pump(msg["name"]))


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): "aquarium_dosing/dose", vol.Required("name"): str,
        vol.Optional("volume"): vol.Coerce(float), vol.Optional("ignore_window", default=False): bool, **_AQ,
    }
)
@websocket_api.async_response
async def ws_dose(hass: HomeAssistant, connection: _Conn, msg: dict[str, Any]) -> None:
    if (c := _resolve(hass, connection, msg)) is not None:
        await _action(connection, msg, c, c.async_dose(msg["name"], msg.get("volume"), ignore_window=msg["ignore_window"]))


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): "aquarium_dosing/calibration_dose", vol.Required("name"): str, **_AQ})
@websocket_api.async_response
async def ws_calibration_dose(hass: HomeAssistant, connection: _Conn, msg: dict[str, Any]) -> None:
    if (c := _resolve(hass, connection, msg)) is not None:
        await _action(connection, msg, c, c.async_calibration_dose(msg["name"]))


@websocket_api.require_admin
@websocket_api.websocket_command(
    {vol.Required("type"): "aquarium_dosing/calibrate", vol.Required("name"): str,
     vol.Required("measured_ml"): vol.Coerce(float), **_AQ}
)
@websocket_api.async_response
async def ws_calibrate(hass: HomeAssistant, connection: _Conn, msg: dict[str, Any]) -> None:
    if (c := _resolve(hass, connection, msg)) is not None:
        await _action(connection, msg, c, c.async_calibrate(msg["name"], msg["measured_ml"]))


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): "aquarium_dosing/clear_fault", vol.Required("name"): str, **_AQ})
@websocket_api.async_response
async def ws_clear_fault(hass: HomeAssistant, connection: _Conn, msg: dict[str, Any]) -> None:
    if (c := _resolve(hass, connection, msg)) is not None:
        await _action(connection, msg, c, c.async_clear_fault(msg["name"]))
