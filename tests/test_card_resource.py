"""The card script is registered as a dashboard resource automatically."""
import pytest
from homeassistant.components.lovelace.const import LOVELACE_DATA
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.aquarium_dosing.const import DOMAIN, FRONTEND_URL, VERSION


@pytest.fixture(autouse=True)
def _enable(enable_custom_integrations):
    yield


async def _resources(hass):
    assert await async_setup_component(hass, "lovelace", {})
    res = hass.data[LOVELACE_DATA].resources
    await res.async_load()
    res.loaded = True
    return res


def _entry(hass, uid="one"):
    entry = MockConfigEntry(domain=DOMAIN, unique_id=uid, title="t", data={"name": "Reef", "master_entity": "input_boolean.x", "initial_pump": "alk", "initial_switch": ""})
    entry.add_to_hass(hass)
    return entry


def _ours(res):
    return [i for i in res.async_items() if i["url"].split("?")[0] == FRONTEND_URL]


async def test_resource_is_created_on_setup(hass):
    res = await _resources(hass)
    entry = _entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    items = _ours(res)
    assert len(items) == 1
    assert items[0]["url"] == f"{FRONTEND_URL}?v={VERSION}" and items[0]["type"] == "module"


async def test_hand_added_resource_is_updated_not_duplicated(hass):
    res = await _resources(hass)
    await res.async_create_item({"res_type": "module", "url": FRONTEND_URL})  # as added by hand
    await res.async_create_item({"res_type": "module", "url": f"{FRONTEND_URL}?v=0.0.1"})  # stale duplicate
    other = await res.async_create_item({"res_type": "module", "url": "/hacsfiles/some-other-card.js"})
    entry = _entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    items = _ours(res)
    assert len(items) == 1 and items[0]["url"] == f"{FRONTEND_URL}?v={VERSION}"
    assert any(i["id"] == other["id"] for i in res.async_items())  # other cards untouched


async def test_resource_removed_with_last_entry_only(hass):
    res = await _resources(hass)
    first = _entry(hass, "one")
    assert await hass.config_entries.async_setup(first.entry_id)
    await hass.async_block_till_done()
    second = _entry(hass, "two")
    assert await hass.config_entries.async_setup(second.entry_id)
    await hass.async_block_till_done()
    assert len(_ours(res)) == 1  # one resource however many entries

    await hass.config_entries.async_remove(first.entry_id)
    await hass.async_block_till_done()
    assert len(_ours(res)) == 1  # another entry still needs it
    await hass.config_entries.async_remove(second.entry_id)
    await hass.async_block_till_done()
    assert _ours(res) == []


async def test_yaml_resource_mode_is_left_alone(hass):
    res = await _resources(hass)
    hass.data[LOVELACE_DATA].resource_mode = "yaml"
    entry = _entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert _ours(res) == []  # cannot be managed from code in YAML mode; setup still succeeds
