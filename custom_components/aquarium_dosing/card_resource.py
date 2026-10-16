"""Serve the Lovelace card script and register it as a dashboard resource.

Two mechanisms are used because neither alone is reliable everywhere:

* ``add_extra_js_url`` injects a <script> tag on every dashboard (works in YAML
  mode too) but gives no ordering guarantee and can be missed by browsers with a
  cached app shell.
* A real Lovelace *resource* (what HACS dashboard cards use, and what you would add
  by hand under Settings > Dashboards > Resources) is loaded before cards render.
  It can only be managed from code in storage mode; in YAML mode the user must add
  the resource to their dashboard YAML.

The resource carries ``?v=<version>`` so an upgrade busts the browser cache. An
existing resource for the same file (for example one added by hand) is updated
in place, never duplicated, and removed again when the last config entry goes.
"""
from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.components.lovelace.const import LOVELACE_DATA
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import Event, HomeAssistant

from .const import DOMAIN, FRONTEND_URL, VERSION

_LOGGER = logging.getLogger(__name__)
_KEY = f"{DOMAIN}_card_registered"
_RESOURCE_TYPE = "module"


def _ours(item: dict) -> bool:
    return str(item.get("url", "")).split("?")[0] == FRONTEND_URL


async def async_setup(hass: HomeAssistant) -> None:
    """Serve the script and register it. Safe to call more than once."""
    if hass.data.get(_KEY):
        return
    hass.data[_KEY] = True
    script = Path(__file__).parent / "frontend" / FRONTEND_URL.rsplit("/", 1)[1]
    await hass.http.async_register_static_paths(
        [StaticPathConfig(FRONTEND_URL, str(script), cache_headers=False)]
    )
    add_extra_js_url(hass, f"{FRONTEND_URL}?v={VERSION}")
    if not await async_ensure_resource(hass):
        async def _retry(_event: Event) -> None:
            await async_ensure_resource(hass)

        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _retry)


async def async_ensure_resource(hass: HomeAssistant) -> bool:
    """Create or update the dashboard resource. Returns False if Lovelace is not ready."""
    lovelace = hass.data.get(LOVELACE_DATA)
    if lovelace is None:
        return False
    resources = lovelace.resources
    if lovelace.resource_mode != "storage" or not hasattr(resources, "async_create_item"):
        _LOGGER.info(
            "Dashboard resources are managed in YAML; add %s as a module resource "
            "to your Lovelace configuration for the cards to load reliably",
            FRONTEND_URL,
        )
        return True
    try:
        if not resources.loaded:
            await resources.async_load()
            resources.loaded = True
        url = f"{FRONTEND_URL}?v={VERSION}"
        ours = [i for i in resources.async_items() if _ours(i)]
        if not ours:
            await resources.async_create_item({"res_type": _RESOURCE_TYPE, "url": url})
            _LOGGER.debug("Registered dashboard resource %s", url)
            return True
        first = ours[0]
        if first.get("url") != url or first.get("type") != _RESOURCE_TYPE:
            await resources.async_update_item(first["id"], {"res_type": _RESOURCE_TYPE, "url": url})
        for duplicate in ours[1:]:
            await resources.async_delete_item(duplicate["id"])
    except Exception:  # noqa: BLE001 - never break integration setup over a card
        _LOGGER.exception("Could not register the dashboard resource for %s", DOMAIN)
    return True


async def async_remove_resource(hass: HomeAssistant) -> None:
    """Remove our resource (called when the last config entry is removed)."""
    lovelace = hass.data.get(LOVELACE_DATA)
    if lovelace is None or not hasattr(lovelace.resources, "async_delete_item"):
        return
    resources = lovelace.resources
    try:
        if not resources.loaded:
            await resources.async_load()
            resources.loaded = True
        for item in [i for i in resources.async_items() if _ours(i)]:
            await resources.async_delete_item(item["id"])
    except Exception:  # noqa: BLE001
        _LOGGER.exception("Could not remove the dashboard resource for %s", DOMAIN)
