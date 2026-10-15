"""Scheduler, dose runner and ledger for Aquarium Dosing.

How a dose is kept safe and exactly-once:

* The schedule (``dosing_engine``) yields time *slots*. Each tick processes the
  slots that became due since ``last_slot`` and saves ``last_slot`` BEFORE
  running anything, so a restart can never repeat a slot.
* A slot more than ``late_tolerance`` minutes late (HA was down, queue backed
  up) is skipped and logged, never caught up. Slots that pass while dosing is
  paused (master toggle off, or the pump's schedule disabled) are skipped too.
* Before every dose: unresolved fault, switch availability, operating window,
  run-time cap, cooldown and the rolling 24 h cap are checked.
* An ``in_flight`` record is saved before the pump starts. After a crash or
  restart any pump that was running is switched off and the dose counts in full
  towards the daily cap (it is not retried).
* After the run the integration switches the pump off and *confirms* the switch
  state. If it cannot, the pump is latched in a fault and nothing runs until the
  user presses "Clear fault".
* By default only one pump runs at a time.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime, timedelta
from typing import Any, Callable

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_NAME,
    EVENT_HOMEASSISTANT_STARTED,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import CoreState, Event, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    CONF_INITIAL_PUMP,
    CONF_INITIAL_SWITCH,
    CONF_LATE_TOLERANCE,
    CONF_MASTER_ENTITY,
    CONF_ONE_AT_A_TIME,
    DEFAULT_LATE_TOLERANCE,
    DEFAULT_MASTER_ENTITY,
    DOMAIN,
    EVENT_DOSING,
    STORAGE_VERSION,
)
from .dosing_engine import (
    CAL_VOLUME,
    PumpConfig,
    Slot,
    calibrated_multiplier,
    check_dose_allowed,
    delivered_last_24h,
    due_slots,
    last_dose_time,
    minute_of_day,
    next_slot,
    prune_history,
    slots_on_date,
    validate_pump_name,
)

_LOGGER = logging.getLogger(__name__)
MAX_SKIPPED_SLOTS_PER_ENTRY = 100

STATUSES = ["idle", "disabled", "paused", "queued", "dosing", "priming", "calibrating", "fault"]


def _new_ledger(now: datetime) -> dict[str, Any]:
    return {
        "last_slot": now.isoformat(),
        "history": [],
        "in_flight": None,
        "fault": None,
        "calibration": None,
    }


class AquariumDosingCoordinator:
    """Owns pump configs, the persistent ledger and all dosing activity."""

    # Pump-off confirmation (class attributes so tests can shorten them).
    _OFF_ATTEMPTS = 3
    _OFF_POLLS = 15
    _OFF_POLL_INTERVAL = 0.2

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.pumps: dict[str, PumpConfig] = {}
        self.ledgers: dict[str, dict[str, Any]] = {}
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}")
        self._global_lock = asyncio.Lock()
        self._pump_locks: dict[str, asyncio.Lock] = {}
        self._runs: dict[str, str] = {}  # pump -> queued|dosing|priming|calibrating
        self._prime_tasks: dict[str, asyncio.Task] = {}
        self._tasks: set[asyncio.Task] = set()
        self._master_on = False
        self._kick = asyncio.Event()
        self._loop_task: asyncio.Task | None = None
        self._unsub_master: Callable[[], None] | None = None
        self._listeners: list[Callable[[], None]] = []

    # ------------------------------------------------------------------ #
    # options
    # ------------------------------------------------------------------ #
    @property
    def _options(self) -> dict[str, Any]:
        return {**self.entry.data, **self.entry.options}

    @property
    def tank_name(self) -> str:
        return self._options.get(CONF_NAME) or "Aquarium"

    @property
    def master_entity(self) -> str:
        return self._options.get(CONF_MASTER_ENTITY) or DEFAULT_MASTER_ENTITY

    @property
    def one_at_a_time(self) -> bool:
        return bool(self._options.get(CONF_ONE_AT_A_TIME, True))

    @property
    def late_tolerance(self) -> timedelta:
        return timedelta(minutes=int(self._options.get(CONF_LATE_TOLERANCE, DEFAULT_LATE_TOLERANCE)))

    @property
    def master_on(self) -> bool:
        return self._master_on

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    async def async_load(self) -> None:
        now = dt_util.now()
        data = await self._store.async_load()
        if not data:
            data = {"pumps": {}, "ledger": {}}
            name = self._options.get(CONF_INITIAL_PUMP)
            if name:
                data["pumps"][name] = PumpConfig(
                    switch=self._options.get(CONF_INITIAL_SWITCH, "")
                ).to_dict()
        for name, raw in data.get("pumps", {}).items():
            try:
                self.pumps[name] = PumpConfig.from_dict(raw)
            except ValueError as err:
                _LOGGER.error("Ignoring invalid configuration for pump %s: %s", name, err)
        for name in self.pumps:
            ledger = {**_new_ledger(now), **data.get("ledger", {}).get(name, {})}
            ledger["last_slot"] = ledger.get("last_slot") or now.isoformat()
            self.ledgers[name] = ledger
        await self._async_save()

    async def _async_save(self) -> None:
        await self._store.async_save(
            {
                "pumps": {n: c.to_dict() for n, c in self.pumps.items()},
                "ledger": self.ledgers,
            }
        )

    @callback
    def async_start(self) -> None:
        self._unsub_master = async_track_state_change_event(
            self.hass, [self.master_entity], self._handle_master
        )
        self._sync_master()
        self._loop_task = self.entry.async_create_background_task(
            self.hass, self._run(), f"{DOMAIN}_scheduler"
        )

    async def async_stop(self) -> None:
        """Cancel everything; running doses switch their pump off on the way out."""
        if self._unsub_master:
            self._unsub_master()
            self._unsub_master = None
        tasks = [t for t in (self._loop_task, *self._tasks, *self._prime_tasks.values()) if t]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._loop_task = None

    # ------------------------------------------------------------------ #
    # listeners / events
    # ------------------------------------------------------------------ #
    @callback
    def async_add_listener(self, cb: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(cb)

        def _remove() -> None:
            with contextlib.suppress(ValueError):
                self._listeners.remove(cb)

        return _remove

    @callback
    def _notify(self) -> None:
        for cb in list(self._listeners):
            cb()

    @callback
    def _fire(self, pump: str, event: str, **data: Any) -> None:
        self.hass.bus.async_fire(
            EVENT_DOSING, {"aquarium": self.tank_name, "pump": pump, "type": event, **data}
        )

    @callback
    def _handle_master(self, event: Event) -> None:
        new = event.data.get("new_state")
        self._master_on = new is not None and new.state == STATE_ON
        self._notify()

    @callback
    def _sync_master(self) -> None:
        state = self.hass.states.get(self.master_entity)
        self._master_on = state is not None and state.state == STATE_ON

    # ------------------------------------------------------------------ #
    # configuration changes
    # ------------------------------------------------------------------ #
    def _pump(self, name: str) -> PumpConfig:
        cfg = self.pumps.get(name)
        if cfg is None:
            raise ValueError(f"Unknown pump {name!r}")
        return cfg

    async def async_set_pump(self, name: str, data: dict[str, Any]) -> None:
        validate_pump_name(name)
        new = PumpConfig.from_dict(data)  # may raise ValueError
        old = self.pumps.get(name)
        self.pumps[name] = new
        if name not in self.ledgers:
            self.ledgers[name] = _new_ledger(dt_util.now())
        elif (
            old is None
            or (new.enabled and not old.enabled)
            or (old.anchor_minute, old.slot_offsets()) != (new.anchor_minute, new.slot_offsets())
        ):
            # Enabling or reshaping the schedule starts "from now": no surprise
            # catch-up dose for a slot that passed a moment ago.
            self.ledgers[name]["last_slot"] = dt_util.now().isoformat()
        await self._async_save()
        self._kick.set()
        self._notify()

    async def async_update_pump(self, name: str, changes: dict[str, Any]) -> None:
        cfg = self._pump(name)
        await self.async_set_pump(name, {**cfg.to_dict(), **changes})

    async def async_delete_pump(self, name: str) -> None:
        self._pump(name)
        if self._runs.get(name):
            raise ValueError("The pump is running; wait for it to finish first")
        del self.pumps[name]
        self.ledgers.pop(name, None)
        await self._async_save()
        dev_reg = dr.async_get(self.hass)
        device = dev_reg.async_get_device(identifiers={(DOMAIN, f"{self.entry.entry_id}_{name}")})
        if device:
            dev_reg.async_remove_device(device.id)  # also removes its entities
        self._notify()

    # ------------------------------------------------------------------ #
    # derived state
    # ------------------------------------------------------------------ #
    def status(self, name: str) -> str:
        cfg, ledger = self.pumps[name], self.ledgers[name]
        if ledger["fault"]:
            return "fault"
        if run := self._runs.get(name):
            return run
        if not cfg.enabled:
            return "disabled"
        if not self._master_on:
            return "paused"
        return "idle"

    def delivered_24h(self, name: str, now: datetime | None = None) -> float:
        return delivered_last_24h(self.ledgers[name]["history"], now or dt_util.now())

    def last_dose(self, name: str) -> datetime | None:
        return last_dose_time(self.ledgers[name]["history"])

    def next_dose(self, name: str) -> datetime | None:
        cfg = self.pumps[name]
        if not (cfg.enabled and self._master_on):
            return None
        slot = next_slot(cfg, dt_util.now())
        return slot.at if slot else None

    def _slot_states(self, name: str) -> dict[str, str]:
        states: dict[str, str] = {}
        for entry in self.ledgers[name]["history"]:
            if entry.get("slot"):
                states[entry["slot"]] = entry["result"]
            for iso in entry.get("slots", []):
                states[iso] = entry["result"]
        return states

    def snapshot(self, now: datetime | None = None) -> dict[str, Any]:
        """JSON-safe view for the Lovelace cards; everything is derived."""
        now = now or dt_util.now()
        pumps: dict[str, Any] = {}
        for name, cfg in self.pumps.items():
            ledger = self.ledgers[name]
            states = self._slot_states(name)
            slots = []
            for slot in slots_on_date(cfg, now.date(), now.tzinfo):
                result = states.get(slot.at.isoformat())
                state = result or ("pending" if slot.at > now else "missed")
                slots.append(
                    {
                        "at": slot.at.isoformat(),
                        "minute": slot.at.hour * 60 + slot.at.minute,
                        "volume": slot.volume,
                        "state": state,
                    }
                )
            last = self.last_dose(name)
            nxt = self.next_dose(name)
            pumps[name] = {
                "config": cfg.to_dict(),
                "status": self.status(name),
                "fault": ledger["fault"],
                "calibration": ledger["calibration"],
                "delivered_24h": self.delivered_24h(name, now),
                "daily_cap": cfg.effective_daily_cap(),
                "scheduled_daily": cfg.scheduled_daily_volume(),
                "last_dose": last.isoformat() if last else None,
                "next_dose": nxt.isoformat() if nxt else None,
                "slots": slots,
                "history": ledger["history"][-15:][::-1],
            }
        return {
            "name": self.tank_name,
            "master_entity": self.master_entity,
            "master_on": self._master_on,
            "one_at_a_time": self.one_at_a_time,
            "late_tolerance_minutes": int(self.late_tolerance.total_seconds() // 60),
            "now": now.isoformat(),
            "now_minute": minute_of_day(now),
            "cal_volume": CAL_VOLUME,
            "pumps": pumps,
        }

    # ------------------------------------------------------------------ #
    # scheduler loop
    # ------------------------------------------------------------------ #
    async def _wait_for_start(self) -> None:
        if self.hass.state is CoreState.running:
            return
        started = asyncio.Event()
        unsub = self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, lambda _e: started.set())
        try:
            await started.wait()
        finally:
            unsub()

    async def _run(self) -> None:
        await self._wait_for_start()
        await self._async_recover()
        self._sync_master()
        if self.hass.states.get(self.master_entity) is None:
            _LOGGER.warning(
                "Dosing master toggle %s does not exist. Create a Toggle helper with "
                "that entity id; scheduled dosing stays paused until it exists and is on",
                self.master_entity,
            )
        while True:
            try:
                await self.async_tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the scheduler must never die
                _LOGGER.exception("Unexpected error in the dosing scheduler")
            now = dt_util.utcnow()
            delay = 60.0 - now.second - now.microsecond / 1e6 + 0.25  # next :00
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._kick.wait(), timeout=delay)
            self._kick.clear()

    async def async_tick(self, now: datetime | None = None) -> None:
        """Process slots that became due. ``now`` is injectable for tests."""
        now = now or dt_util.now()
        to_run: list[tuple[str, Slot]] = []
        changed = False
        for name, cfg in list(self.pumps.items()):
            ledger = self.ledgers[name]
            due = due_slots(cfg, now, datetime.fromisoformat(ledger["last_slot"]))
            if not due:
                continue
            changed = True
            ledger["last_slot"] = due[-1].at.isoformat()  # saved below, BEFORE any dose runs
            paused = not (cfg.enabled and self._master_on)
            skipped: dict[str, list[Slot]] = {}
            for slot in due:
                if paused:
                    skipped.setdefault("dosing paused", []).append(slot)
                elif now - slot.at > self.late_tolerance:
                    skipped.setdefault("too late (missed)", []).append(slot)
                else:
                    to_run.append((name, slot))
            for reason, slots in skipped.items():
                self._record_skip(name, slots, reason, now)
        if changed:
            await self._async_save()
        for name, slot in to_run:
            self._runs.setdefault(name, "queued")
            self._spawn(self._run_job(name, "dose", volume=slot.volume, source="schedule", slot=slot))
        self._notify()

    def _spawn(self, coro) -> asyncio.Task:
        task = self.entry.async_create_background_task(self.hass, coro, f"{DOMAIN}_job")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    # ------------------------------------------------------------------ #
    # ledger
    # ------------------------------------------------------------------ #
    def _append(self, name: str, entry: dict[str, Any]) -> None:
        ledger = self.ledgers[name]
        ledger["history"].append(entry)
        ledger["history"] = prune_history(ledger["history"], dt_util.now())

    def _record_skip(self, name: str, slots: list[Slot], reason: str, now: datetime, source: str = "schedule") -> None:
        self._append(
            name,
            {
                "ts": now.isoformat(),
                "slots": [s.at.isoformat() for s in slots][:MAX_SKIPPED_SLOTS_PER_ENTRY],
                "volume": round(sum(s.volume for s in slots), 6),
                "result": "skipped",
                "source": source,
                "reason": reason if len(slots) == 1 else f"{reason} ({len(slots)} doses)",
            },
        )
        self._fire(name, "skipped", reason=reason, doses=len(slots))
        _LOGGER.info("Pump %s: skipped %d scheduled dose(s): %s", name, len(slots), reason)

    # ------------------------------------------------------------------ #
    # running jobs (dose / calibration / prime)
    # ------------------------------------------------------------------ #
    def _lock_for(self, name: str) -> asyncio.Lock:
        if self.one_at_a_time:
            return self._global_lock
        return self._pump_locks.setdefault(name, asyncio.Lock())

    def _blocked_reason(
        self, name: str, kind: str, volume: float, now: datetime, ignore_window: bool = False
    ) -> str | None:
        cfg, ledger = self.pumps[name], self.ledgers[name]
        if ledger["fault"]:
            return "the pump has an unresolved fault (check it, then press Clear fault)"
        if not cfg.switch:
            return "no pump switch is configured"
        state = self.hass.states.get(cfg.switch)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return f"{cfg.switch} is unavailable"
        if kind == "prime":
            return None
        if kind == "calibration":
            seconds = cfg.run_seconds(volume)
            if seconds > cfg.max_run_seconds:
                return (
                    f"the {volume:g} mL test dose would run {seconds:.0f} s, above the "
                    f"{cfg.max_run_seconds} s limit; raise the maximum run time first"
                )
            return None
        history = ledger["history"]
        return check_dose_allowed(
            cfg, now, volume, delivered_last_24h(history, now), last_dose_time(history),
            ignore_window=ignore_window,
        )

    async def _run_job(
        self, name: str, kind: str, *, volume: float = 0.0, source: str,
        slot: Slot | None = None, ignore_window: bool = False,
    ) -> None:
        if name not in self.pumps:
            return
        self._runs.setdefault(name, "queued")
        try:
            async with self._lock_for(name):
                if name not in self.pumps:
                    return
                now = dt_util.now()
                if slot is not None and now - slot.at > self.late_tolerance:
                    self._record_skip(name, [slot], "too late (waited for another pump)", now)
                    await self._async_save()
                    return
                reason = self._blocked_reason(name, kind, volume, now, ignore_window)
                if reason:
                    if slot is not None:
                        self._record_skip(name, [slot], f"blocked: {reason}", now)
                        await self._async_save()
                    else:
                        _LOGGER.warning("Pump %s: %s not started: %s", name, kind, reason)
                    return
                await self._execute(name, kind, volume, now, source, slot)
        finally:
            self._runs.pop(name, None)
            if kind == "prime":
                self._prime_tasks.pop(name, None)
            self._notify()

    async def _execute(
        self, name: str, kind: str, volume: float, now: datetime, source: str, slot: Slot | None
    ) -> None:
        cfg, ledger = self.pumps[name], self.ledgers[name]
        seconds = cfg.max_run_seconds if kind == "prime" else cfg.run_seconds(volume)
        multiplier = cfg.multiplier
        self._runs[name] = {"dose": "dosing", "calibration": "calibrating", "prime": "priming"}[kind]
        ledger["in_flight"] = {
            "kind": kind, "started": now.isoformat(), "volume": volume,
            "slot": slot.at.isoformat() if slot else None, "source": source,
        }
        await self._async_save()  # recorded BEFORE the pump starts
        self._notify()
        self._fire(name, "started", kind=kind, volume=volume, seconds=round(seconds, 2))

        started = cancelled = False
        error: Exception | None = None
        try:
            await self._switch(cfg.switch, True)
            started = True
            await asyncio.sleep(seconds)
        except asyncio.CancelledError:
            cancelled = True
        except Exception as err:  # noqa: BLE001
            error = err
            _LOGGER.error("Pump %s: could not run: %s", name, err)

        off_ok = await self._ensure_off(cfg.switch)

        if cancelled or (error is not None and started):
            result = "interrupted"
        elif error is not None:
            result = "failed"
        elif kind == "calibration":
            result = "calibration"
        elif kind == "prime":
            result = "prime"
        else:
            result = "done"
        entry = {
            "ts": dt_util.now().isoformat(), "slot": slot.at.isoformat() if slot else None,
            "volume": volume if kind != "prime" else 0.0, "result": result, "source": source,
            "reason": str(error) if error else ("stopped early" if cancelled else ""),
            "seconds": seconds,
        }
        self._append(name, entry)
        ledger["in_flight"] = None
        if result == "calibration" and off_ok:
            ledger["calibration"] = {
                "multiplier": multiplier, "volume": volume, "at": dt_util.now().isoformat()
            }
        if not off_ok:
            self._latch_fault(name, "Could not confirm the pump switched off")
        await self._async_save()
        self._fire(name, result, kind=kind, volume=volume)
        if cancelled:
            raise asyncio.CancelledError

    async def _switch(self, entity_id: str, on: bool) -> None:
        await self.hass.services.async_call(
            "switch", "turn_on" if on else "turn_off", {"entity_id": entity_id}, blocking=True
        )

    async def _ensure_off(self, entity_id: str) -> bool:
        """Switch off and confirm the state; retry a few times."""
        for _ in range(self._OFF_ATTEMPTS):
            try:
                await self._switch(entity_id, False)
            except Exception as err:  # noqa: BLE001
                _LOGGER.error("Turning %s off failed: %s", entity_id, err)
                await asyncio.sleep(self._OFF_POLL_INTERVAL)
                continue
            for _ in range(self._OFF_POLLS):
                state = self.hass.states.get(entity_id)
                if state is not None and state.state == STATE_OFF:
                    return True
                await asyncio.sleep(self._OFF_POLL_INTERVAL)
        return False

    @callback
    def _latch_fault(self, name: str, reason: str) -> None:
        self.ledgers[name]["fault"] = {"reason": reason, "since": dt_util.now().isoformat()}
        _LOGGER.error("Pump %s fault: %s", name, reason)
        persistent_notification.async_create(
            self.hass,
            f"{self.tank_name} pump '{name}' is stopped: {reason}. Check the pump and "
            "its switch, then press 'Clear fault' to allow dosing again.",
            title="Aquarium dosing fault",
            notification_id=f"{DOMAIN}_{self.entry.entry_id}_{name}_fault",
        )
        self._fire(name, "fault", reason=reason)

    async def _async_recover(self) -> None:
        """After a restart, switch off any pump that was running."""
        recovered = False
        for name, ledger in self.ledgers.items():
            flight = ledger.get("in_flight")
            if not flight:
                continue
            recovered = True
            cfg = self.pumps.get(name)
            ok = await self._ensure_off(cfg.switch) if cfg and cfg.switch else False
            kind = flight.get("kind", "dose")
            self._append(
                name,
                {
                    "ts": dt_util.now().isoformat(), "slot": flight.get("slot"),
                    "volume": flight.get("volume", 0.0) if kind == "dose" else 0.0,
                    "result": "interrupted" if kind == "dose" else kind,
                    "source": flight.get("source", "schedule"),
                    "reason": "Home Assistant restarted while the pump was running",
                },
            )
            ledger["in_flight"] = None
            if not ok:
                self._latch_fault(name, "Could not confirm the pump is off after a restart")
        if recovered:
            await self._async_save()
            self._notify()

    # ------------------------------------------------------------------ #
    # user actions
    # ------------------------------------------------------------------ #
    async def async_dose(self, name: str, volume: float | None = None, *, ignore_window: bool = False) -> None:
        cfg = self._pump(name)
        vol = float(volume) if volume else cfg.manual_volume()
        if vol <= 0:
            raise ValueError("The dose volume must be above 0 mL")
        if self._runs.get(name):
            raise ValueError("This pump is already dosing or queued")
        reason = self._blocked_reason(name, "dose", vol, dt_util.now(), ignore_window)
        if reason:
            raise ValueError(f"Dose refused: {reason}")
        self._runs[name] = "queued"
        self._notify()
        self._spawn(self._run_job(name, "dose", volume=vol, source="manual", ignore_window=ignore_window))

    async def async_calibration_dose(self, name: str) -> None:
        self._pump(name)
        if self._runs.get(name):
            raise ValueError("This pump is already running")
        reason = self._blocked_reason(name, "calibration", CAL_VOLUME, dt_util.now())
        if reason:
            raise ValueError(f"Calibration refused: {reason}")
        self._runs[name] = "queued"
        self._notify()
        self._spawn(self._run_job(name, "calibration", volume=CAL_VOLUME, source="calibration"))

    async def async_calibrate(self, name: str, measured_ml: float) -> float:
        self._pump(name)
        cal = self.ledgers[name]["calibration"]
        if not cal:
            raise ValueError("Run the calibration test dose first")
        new = calibrated_multiplier(cal["multiplier"], cal["volume"], float(measured_ml))
        old = self.pumps[name].multiplier
        await self.async_update_pump(name, {"multiplier": new})  # validated, incl. run-time cap
        self.ledgers[name]["calibration"] = None
        await self._async_save()
        self._fire(name, "calibrated", old=old, new=new)
        self._notify()
        return new

    async def async_set_prime(self, name: str, on: bool) -> None:
        self._pump(name)
        if on:
            if name in self._prime_tasks:
                return
            if self._runs.get(name):
                raise ValueError("This pump is already running")
            reason = self._blocked_reason(name, "prime", 0.0, dt_util.now())
            if reason:
                raise ValueError(f"Priming refused: {reason}")
            self._runs[name] = "queued"
            self._prime_tasks[name] = self._spawn(self._run_job(name, "prime", source="prime"))
        else:
            task = self._prime_tasks.get(name)
            if task:
                task.cancel()

    async def async_clear_fault(self, name: str) -> None:
        self._pump(name)
        if not self.ledgers[name]["fault"]:
            return
        self.ledgers[name]["fault"] = None
        persistent_notification.async_dismiss(
            self.hass, f"{DOMAIN}_{self.entry.entry_id}_{name}_fault"
        )
        await self._async_save()
        self._fire(name, "fault_cleared")
        self._notify()
