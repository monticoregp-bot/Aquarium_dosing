"""Integration tests that run in a real Home Assistant (test harness).

    pip install pytest-homeassistant-custom-component     # Python 3.13+
    pytest tests/
"""
import asyncio
from datetime import timedelta

import pytest
from homeassistant.exceptions import ServiceValidationError
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

DOMAIN = "aquarium_dosing"
MASTER = "input_boolean.aquarium_dosing"
SWITCH = "switch.alk_pump"


@pytest.fixture(autouse=True)
def _enable(enable_custom_integrations):
    yield


class Switches:
    """Fake switch services that record when each pump turns on/off."""

    def __init__(self, hass):
        self.hass, self.events, self.stuck_on = hass, [], False
        hass.services.async_register("switch", "turn_on", self._on)
        hass.services.async_register("switch", "turn_off", self._off)

    async def _on(self, call):
        for entity_id in call.data["entity_id"] if isinstance(call.data["entity_id"], list) else [call.data["entity_id"]]:
            self.events.append(("on", entity_id))
            self.hass.states.async_set(entity_id, "on")

    async def _off(self, call):
        for entity_id in call.data["entity_id"] if isinstance(call.data["entity_id"], list) else [call.data["entity_id"]]:
            self.events.append(("off", entity_id))
            if not self.stuck_on:
                self.hass.states.async_set(entity_id, "off")


async def _setup(hass, *, master_on=True, fake_switches=True):
    hass.states.async_set(SWITCH, "off")
    assert await async_setup_component(hass, "input_boolean", {"input_boolean": {"aquarium_dosing": {}}})
    if master_on:
        await hass.services.async_call("input_boolean", "turn_on", {"entity_id": MASTER}, blocking=True)
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=MASTER, title="Aquarium Dosing (Reef)",
        data={"name": "Reef", "master_entity": MASTER, "one_at_a_time": True, "late_tolerance_minutes": 5,
              "initial_pump": "alk", "initial_switch": SWITCH},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    # Registered AFTER setup: setting up our switch platform loads HA's real
    # `switch` component, which would replace fake services registered earlier.
    switches = Switches(hass) if fake_switches else None
    coordinator = hass.data[DOMAIN][entry.entry_id]
    coordinator._OFF_POLL_INTERVAL = 0.01
    return entry, coordinator, switches


# Short runs: 0.1 mL x 0.1 s/mL = 10 ms per dose.
FAST = {"switch": SWITCH, "volume": 0.1, "multiplier": 0.1, "enabled": True, "cooldown_minutes": 0}


async def _idle(coordinator, timeout=3.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if not coordinator._runs and not coordinator._tasks:
            return True
        await asyncio.sleep(0.02)
    return False


def _tomorrow(h, m=0, s=0):
    return (dt_util.now() + timedelta(days=1)).replace(hour=h, minute=m, second=s, microsecond=0)


def _since(coordinator, when, pump="alk"):
    coordinator.ledgers[pump]["last_slot"] = when.isoformat()


def _results(coordinator, pump="alk"):
    return [(e["result"], e.get("reason", "")) for e in coordinator.ledgers[pump]["history"]]


# ------------------------------------------------------------------ entities
async def test_entities_are_created_for_a_pump(hass):
    await _setup(hass)
    ids = sorted(e for e in hass.states.async_entity_ids() if "reef_alk_pump" in e)
    print("ENTITIES:", ids)
    assert len(ids) == 23
    assert hass.states.get("switch.reef_alk_pump_schedule_enabled").state == "off"  # safe default
    assert hass.states.get("switch.reef_alk_pump_operate_at_all_hours").state == "on"
    assert hass.states.get("select.reef_alk_pump_schedule_mode").state == "interval"
    assert hass.states.get("time.reef_alk_pump_window_start").state == "08:00:00"
    assert hass.states.get("sensor.reef_alk_pump_status").state == "disabled"


async def test_entity_edits_are_validated_and_synced(hass):
    # real `switch` services here (we toggle one of our own switch entities)
    entry, coordinator, _ = await _setup(hass, fake_switches=False)
    await hass.services.async_call("time", "set_value", {"entity_id": "time.reef_alk_pump_window_start", "time": "09:00:00"}, blocking=True)
    assert coordinator.pumps["alk"].window_start == "09:00"
    await hass.services.async_call("switch", "turn_on", {"entity_id": "switch.reef_alk_pump_schedule_enabled"}, blocking=True)
    assert coordinator.pumps["alk"].enabled and hass.states.get("sensor.reef_alk_pump_status").state == "idle"
    await hass.services.async_call("number", "set_value", {"entity_id": "number.reef_alk_pump_dose_volume", "value": 2.5}, blocking=True)
    assert coordinator.pumps["alk"].volume == 2.5
    # unsafe: 2.5 mL x 100 s/mL = 250 s, above the 120 s limit -> refused with a reason
    with pytest.raises(ServiceValidationError, match="maximum run time"):
        await hass.services.async_call("number", "set_value", {"entity_id": "number.reef_alk_pump_calibration_multiplier", "value": 100}, blocking=True)
    assert coordinator.pumps["alk"].multiplier == 1.0
    # times mode with no times is refused
    with pytest.raises(ServiceValidationError, match="at least one"):
        await hass.services.async_call("select", "select_option", {"entity_id": "select.reef_alk_pump_schedule_mode", "option": "times"}, blocking=True)


# ----------------------------------------------------------------- scheduling
async def test_scheduled_dose_runs_exactly_once(hass):
    entry, coordinator, switches = await _setup(hass)
    await coordinator.async_set_pump("alk", {**FAST, "mode": "times", "times": ["10:00"]})
    _since(coordinator, _tomorrow(9, 0))
    await coordinator.async_tick(now=_tomorrow(10, 0, 30))
    assert await _idle(coordinator)
    assert _results(coordinator) == [("done", "")]
    assert switches.events == [("on", SWITCH), ("off", SWITCH)]
    await coordinator.async_tick(now=_tomorrow(10, 0, 45))  # same slot again
    await coordinator.async_tick(now=_tomorrow(10, 1, 30))
    assert await _idle(coordinator)
    assert len(coordinator.ledgers["alk"]["history"]) == 1
    assert coordinator.delivered_24h("alk", _tomorrow(10, 2)) == pytest.approx(0.1)


async def test_late_and_paused_slots_are_skipped_not_caught_up(hass):
    entry, coordinator, switches = await _setup(hass)
    await coordinator.async_set_pump("alk", {**FAST, "mode": "times", "times": ["10:00"]})
    _since(coordinator, _tomorrow(9, 0))
    await coordinator.async_tick(now=_tomorrow(10, 30))  # 30 min late (tolerance 5)
    assert await _idle(coordinator)
    assert switches.events == [] and _results(coordinator)[0][0] == "skipped" and "too late" in _results(coordinator)[0][1]

    # master toggle off: the next day's slot is skipped as paused
    await hass.services.async_call("input_boolean", "turn_off", {"entity_id": MASTER}, blocking=True)
    await hass.async_block_till_done()
    day2 = _tomorrow(10, 0, 30) + timedelta(days=1)
    _since(coordinator, _tomorrow(10, 45))
    await coordinator.async_tick(now=day2)
    assert switches.events == [] and "paused" in _results(coordinator)[-1][1]


async def test_disabled_pump_does_not_run(hass):
    entry, coordinator, switches = await _setup(hass)
    await coordinator.async_set_pump("alk", {**FAST, "enabled": False, "mode": "times", "times": ["10:00"]})
    _since(coordinator, _tomorrow(9, 0))
    await coordinator.async_tick(now=_tomorrow(10, 0, 30))
    assert switches.events == [] and "paused" in _results(coordinator)[0][1]


# ------------------------------------------------------------ operating window
def _window_excluding_now():
    h = dt_util.now().hour
    return {"all_hours": False, "window_start": f"{(h + 2) % 24:02d}:00", "window_end": f"{(h + 4) % 24:02d}:00"}


def _window_including_now():
    h = dt_util.now().hour
    return {"all_hours": False, "window_start": f"{(h - 1) % 24:02d}:00", "window_end": f"{(h + 1) % 24:02d}:59"}


async def test_manual_dose_respects_the_operating_window(hass):
    entry, coordinator, switches = await _setup(hass)
    await coordinator.async_set_pump("alk", {**FAST, "mode": "interval", "interval_minutes": 60, **_window_excluding_now()})
    with pytest.raises(ValueError, match="outside the operating window"):
        await coordinator.async_dose("alk")
    assert switches.events == []
    await coordinator.async_dose("alk", ignore_window=True)  # explicit override
    assert await _idle(coordinator)
    assert _results(coordinator) == [("done", "")]

    await coordinator.async_set_pump("alk", {**FAST, "mode": "interval", "interval_minutes": 60, **_window_including_now()})
    await coordinator.async_dose("alk")
    assert await _idle(coordinator)
    assert [r for r, _ in _results(coordinator)] == ["done", "done"]


async def test_scheduled_slots_only_exist_inside_the_window(hass):
    entry, coordinator, _ = await _setup(hass)
    await coordinator.async_set_pump("alk", {**FAST, "mode": "interval", "interval_minutes": 60, "all_hours": False, "window_start": "20:00", "window_end": "23:00"})
    snap = coordinator.snapshot(_tomorrow(12))
    minutes = [s["minute"] for s in snap["pumps"]["alk"]["slots"]]
    assert minutes == [20 * 60, 21 * 60, 22 * 60]


# --------------------------------------------------------------------- safety
async def test_daily_cap_and_cooldown_block_manual_doses(hass):
    entry, coordinator, _ = await _setup(hass)
    await coordinator.async_set_pump("alk", {**FAST, "mode": "interval", "interval_minutes": 1440, "daily_cap": 0.15, "cooldown_minutes": 0})
    await coordinator.async_dose("alk")  # 0.1 mL, within the 0.15 cap
    assert await _idle(coordinator)
    with pytest.raises(ValueError, match="daily cap"):
        await coordinator.async_dose("alk")
    await coordinator.async_update_pump("alk", {"daily_cap": 5, "cooldown_minutes": 10})
    with pytest.raises(ValueError, match="cooldown"):
        await coordinator.async_dose("alk")


async def test_pump_that_will_not_switch_off_latches_a_fault(hass):
    entry, coordinator, switches = await _setup(hass)
    coordinator._OFF_ATTEMPTS, coordinator._OFF_POLLS = 2, 3
    await coordinator.async_set_pump("alk", {**FAST})
    switches.stuck_on = True
    await coordinator.async_dose("alk")
    assert await _idle(coordinator)
    await hass.async_block_till_done()
    assert coordinator.status("alk") == "fault"
    assert hass.states.get("binary_sensor.reef_alk_pump_fault").state == "on"
    assert switches.events.count(("off", SWITCH)) == 2  # retried
    with pytest.raises(ValueError, match="unresolved fault"):
        await coordinator.async_dose("alk")
    switches.stuck_on = False
    await coordinator.async_clear_fault("alk")
    assert coordinator.status("alk") != "fault"
    await coordinator.async_dose("alk", ignore_window=True)
    assert await _idle(coordinator)


async def test_restart_while_pump_was_running_switches_it_off(hass):
    entry, coordinator, switches = await _setup(hass)
    await coordinator.async_set_pump("alk", {**FAST})
    coordinator.ledgers["alk"]["in_flight"] = {"kind": "dose", "started": dt_util.now().isoformat(), "volume": 2.0, "slot": None, "source": "schedule"}
    hass.states.async_set(SWITCH, "on")
    await coordinator._async_recover()
    assert ("off", SWITCH) in switches.events
    assert coordinator.ledgers["alk"]["in_flight"] is None
    last = coordinator.ledgers["alk"]["history"][-1]
    assert last["result"] == "interrupted" and last["volume"] == 2.0  # counts in full towards the cap
    assert coordinator.delivered_24h("alk") == pytest.approx(2.0)


async def test_unavailable_switch_blocks_the_dose(hass):
    entry, coordinator, switches = await _setup(hass)
    await coordinator.async_set_pump("alk", {**FAST})
    hass.states.async_set(SWITCH, "unavailable")
    with pytest.raises(ValueError, match="unavailable"):
        await coordinator.async_dose("alk")


async def test_only_one_pump_runs_at_a_time(hass):
    entry, coordinator, switches = await _setup(hass)
    hass.states.async_set("switch.ca_pump", "off")
    slow = {"volume": 1, "multiplier": 0.1, "enabled": True, "cooldown_minutes": 0}  # 0.1 s each
    await coordinator.async_set_pump("alk", {**slow, "switch": SWITCH})
    await coordinator.async_set_pump("ca", {**slow, "switch": "switch.ca_pump"})
    await coordinator.async_dose("alk")
    await coordinator.async_dose("ca")
    assert await _idle(coordinator, 5)
    kinds = [(k, e) for k, e in switches.events if k in ("on", "off")]
    # strictly sequential: on/off of one pump completes before the other starts
    assert kinds[0][1] == kinds[1][1] and kinds[2][1] == kinds[3][1] and kinds[0][1] != kinds[2][1]


# ---------------------------------------------------------------- calibration
async def test_calibration_uses_the_multiplier_from_the_test_dose(hass, monkeypatch):
    from custom_components.aquarium_dosing import coordinator as coord_module

    monkeypatch.setattr(coord_module, "CAL_VOLUME", 0.5)
    entry, coordinator, _ = await _setup(hass)
    await coordinator.async_set_pump("alk", {**FAST})  # multiplier 0.1
    with pytest.raises(ValueError, match="test dose first"):
        await coordinator.async_calibrate("alk", 0.4)
    await coordinator.async_calibration_dose("alk")
    assert await _idle(coordinator)
    assert coordinator.ledgers["alk"]["calibration"]["multiplier"] == 0.1
    await coordinator.async_update_pump("alk", {"multiplier": 0.2})  # changed meanwhile
    new = await coordinator.async_calibrate("alk", 0.4)  # 0.5 mL expected, 0.4 measured
    assert new == pytest.approx(0.125)  # from the multiplier USED in the test, not the changed one
    assert coordinator.pumps["alk"].multiplier == pytest.approx(0.125)
    assert coordinator.ledgers["alk"]["calibration"] is None
    assert coordinator.delivered_24h("alk") == 0  # test dose is not tank dosing


# ------------------------------------------------------- services & websocket
async def test_dose_service_targets_a_pump_entity(hass):
    entry, coordinator, switches = await _setup(hass)
    await coordinator.async_set_pump("alk", {**FAST})
    await hass.services.async_call(DOMAIN, "dose", {"entity_id": "sensor.reef_alk_pump_status", "volume": 0.2}, blocking=True)
    assert await _idle(coordinator)
    assert coordinator.ledgers["alk"]["history"][-1]["volume"] == 0.2
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, "dose", {"entity_id": "light.nope"}, blocking=True)


async def test_pumps_added_and_removed_over_websocket(hass, hass_ws_client):
    entry, coordinator, _ = await _setup(hass)
    client = await hass_ws_client(hass)
    cfg = {"switch": "switch.ca_pump", "mode": "times", "times": ["09:00", "21:00"], "all_hours": False, "window_start": "08:00", "window_end": "22:00", "volume": 1.0, "multiplier": 1.0}

    await client.send_json({"id": 1, "type": "aquarium_dosing/set_pump", "name": "ca", "config": cfg})
    msg = await client.receive_json()
    assert msg["success"], msg
    await hass.async_block_till_done()
    assert hass.states.get("select.reef_ca_pump_schedule_mode").state == "times"
    assert msg["result"]["pumps"]["ca"]["scheduled_daily"] == 2.0

    await client.send_json({"id": 2, "type": "aquarium_dosing/preview", "config": cfg})
    preview = (await client.receive_json())["result"]
    assert [s["minute"] for s in preview["slots"]] == [540, 1260] and preview["daily_cap"] == 3.0

    await client.send_json({"id": 3, "type": "aquarium_dosing/preview", "config": {**cfg, "times": ["07:00"]}})
    bad = await client.receive_json()
    assert not bad["success"] and "outside the operating window" in bad["error"]["message"]

    await client.send_json({"id": 4, "type": "aquarium_dosing/delete_pump", "name": "ca"})
    assert (await client.receive_json())["success"]
    await hass.async_block_till_done()
    assert hass.states.get("select.reef_ca_pump_schedule_mode") is None
    assert hass.states.get("select.reef_alk_pump_schedule_mode") is not None

    hass.states.async_set(SWITCH, "unavailable")
    await client.send_json({"id": 5, "type": "aquarium_dosing/dose", "name": "alk"})
    refused = await client.receive_json()
    assert not refused["success"] and refused["error"]["code"] == "refused"
    assert "unavailable" in refused["error"]["message"]
