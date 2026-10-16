"""Tests for the pure dosing engine (no Home Assistant needed)."""
import importlib.util
import pathlib
import sys
from datetime import date, datetime, timedelta, timezone

import pytest

_PATH = (
    pathlib.Path(__file__).parents[1]
    / "custom_components"
    / "aquarium_dosing"
    / "dosing_engine.py"
)
_spec = importlib.util.spec_from_file_location("dosing_engine", _PATH)
de = importlib.util.module_from_spec(_spec)
sys.modules["dosing_engine"] = de  # needed by dataclasses
_spec.loader.exec_module(de)

TZ = timezone.utc


def cfg(**over):
    return de.PumpConfig.from_dict(over)


def at(hhmm, day=14, sec=0):
    h, m = map(int, hhmm.split(":"))
    return datetime(2026, 10, day, h, m, sec, tzinfo=TZ)


def hhmm(slots):
    return [s.at.strftime("%H:%M") for s in slots]


# ----------------------------------------------------------------- interval
def test_interval_all_hours_anchors_at_midnight_plus_offset():
    c = cfg(interval_minutes=360, offset_minutes=15)
    assert hhmm(de.slots_on_date(c, date(2026, 10, 14), TZ)) == ["00:15", "06:15", "12:15", "18:15"]


def test_two_pumps_can_be_staggered_with_offsets():
    ca = cfg(interval_minutes=60, offset_minutes=15)
    alk = cfg(interval_minutes=60, offset_minutes=45)
    a = hhmm(de.slots_on_date(ca, date(2026, 10, 14), TZ))
    b = hhmm(de.slots_on_date(alk, date(2026, 10, 14), TZ))
    assert a[:2] == ["00:15", "01:15"] and b[:2] == ["00:45", "01:45"]


def test_interval_inside_window_is_anchored_to_window_start():
    c = cfg(all_hours=False, window_start="08:00", window_end="22:00", interval_minutes=60, offset_minutes=15)
    slots = hhmm(de.slots_on_date(c, date(2026, 10, 14), TZ))
    assert slots[0] == "08:15" and slots[-1] == "21:15" and len(slots) == 14


def test_window_end_is_exclusive_and_start_inclusive():
    c = cfg(all_hours=False, window_start="08:00", window_end="10:00", interval_minutes=60)
    assert hhmm(de.slots_on_date(c, date(2026, 10, 14), TZ)) == ["08:00", "09:00"]  # not 10:00


def test_non_dividing_window_interval_is_fine_and_repeats_daily():
    c = cfg(all_hours=False, window_start="08:00", window_end="12:00", interval_minutes=90)
    assert hhmm(de.slots_on_date(c, date(2026, 10, 14), TZ)) == ["08:00", "09:30", "11:00"]
    assert hhmm(de.slots_on_date(c, date(2026, 10, 15), TZ)) == ["08:00", "09:30", "11:00"]


def test_all_hours_interval_must_divide_a_day():
    with pytest.raises(ValueError, match="divide 24 h"):
        cfg(interval_minutes=50)
    assert cfg(interval_minutes=120)  # fine


# ------------------------------------------------------------------- window
def test_window_crossing_midnight():
    c = cfg(all_hours=False, window_start="20:00", window_end="06:00", interval_minutes=120)
    assert c.window_minutes == 600
    assert c.in_window(23 * 60) and c.in_window(0) and c.in_window(5 * 60 + 59)
    assert not c.in_window(6 * 60) and not c.in_window(12 * 60) and not c.in_window(19 * 60 + 59)
    # slots on the calendar day include the tail of yesterday's window
    assert hhmm(de.slots_on_date(c, date(2026, 10, 14), TZ)) == ["00:00", "02:00", "04:00", "20:00", "22:00"]


def test_in_window_boundaries_normal_window():
    c = cfg(all_hours=False, window_start="08:00", window_end="22:00")
    assert c.in_window(8 * 60) and c.in_window(21 * 60 + 59)
    assert not c.in_window(22 * 60) and not c.in_window(7 * 60 + 59)


def test_all_hours_is_always_in_window():
    assert cfg().in_window(0) and cfg().in_window(1439)


def test_window_needs_distinct_start_and_end():
    with pytest.raises(ValueError, match="different start and end"):
        cfg(all_hours=False, window_start="08:00", window_end="08:00")


# --------------------------------------------------------------------- times
def test_times_mode_sorted_deduplicated_and_inside_window():
    c = cfg(mode="times", times=["20:15", "08:15", "08:15", "14:15"])
    assert c.times == ["08:15", "14:15", "20:15"]
    w = cfg(mode="times", all_hours=False, window_start="08:00", window_end="22:00", times=["09:00", "21:30"])
    assert hhmm(de.slots_on_date(w, date(2026, 10, 14), TZ)) == ["09:00", "21:30"]


def test_times_outside_the_window_are_rejected():
    with pytest.raises(ValueError, match="outside the operating window"):
        cfg(mode="times", all_hours=False, window_start="08:00", window_end="22:00", times=["07:00"])
    with pytest.raises(ValueError, match="at least one"):
        cfg(mode="times", times=[])


def test_times_in_a_midnight_crossing_window_are_ordered_from_the_window_start():
    c = cfg(mode="times", all_hours=False, window_start="22:00", window_end="04:00", times=["01:00", "23:00"])
    # window starts 22:00 on the anchor day, so 23:00 comes before 01:00 (next day)
    assert hhmm(de.slots_for_anchor_day(c, date(2026, 10, 14), TZ)) == ["23:00", "01:00"]


# --------------------------------------------------------------- daily total
def test_daily_total_splits_evenly_across_the_window():
    c = cfg(mode="daily_total", daily_total=12, doses_per_day=4, all_hours=False, window_start="08:00", window_end="20:00")
    slots = de.slots_on_date(c, date(2026, 10, 14), TZ)
    assert hhmm(slots) == ["08:00", "11:00", "14:00", "17:00"]
    assert all(s.volume == 3 for s in slots)
    assert c.scheduled_daily_volume() == 12
    assert c.manual_volume() == 3


def test_daily_total_all_hours_and_offset():
    c = cfg(mode="daily_total", daily_total=6, doses_per_day=6, offset_minutes=30)
    assert hhmm(de.slots_on_date(c, date(2026, 10, 14), TZ)) == ["00:30", "04:30", "08:30", "12:30", "16:30", "20:30"]
    with pytest.raises(ValueError, match="Offset"):
        cfg(mode="daily_total", daily_total=6, doses_per_day=6, offset_minutes=240)
    with pytest.raises(ValueError, match="above 0"):
        cfg(mode="daily_total", daily_total=0)


# ---------------------------------------------------------- due / next slots
def test_due_slots_are_exactly_once():
    c = cfg(interval_minutes=60)
    last = at("09:00")
    assert hhmm(de.due_slots(c, at("10:00", sec=1), last)) == ["10:00"]
    assert de.due_slots(c, at("10:30"), at("10:00")) == []  # already processed
    assert hhmm(de.due_slots(c, at("12:00"), at("09:00"))) == ["10:00", "11:00", "12:00"]  # backlog


def test_due_slots_cover_a_window_that_started_yesterday():
    c = cfg(all_hours=False, window_start="22:00", window_end="04:00", interval_minutes=60)
    assert hhmm(de.due_slots(c, at("01:00", day=15), at("00:00", day=15))) == ["01:00"]


def test_next_slot_finds_tomorrow_when_today_is_done():
    c = cfg(all_hours=False, window_start="08:00", window_end="10:00", interval_minutes=60)
    nxt = de.next_slot(c, at("23:00"))
    assert nxt.at == at("08:00", day=15)
    assert de.next_slot(c, at("08:30")).at == at("09:00")


# ------------------------------------------------------------------- safety
def test_manual_or_scheduled_dose_outside_window_is_refused_unless_overridden():
    c = cfg(all_hours=False, window_start="08:00", window_end="22:00")
    assert "outside the operating window" in de.check_dose_allowed(c, at("23:00"), 1, 0, None)
    assert de.check_dose_allowed(c, at("23:00"), 1, 0, None, ignore_window=True) is None
    assert de.check_dose_allowed(c, at("12:00"), 1, 0, None) is None


def test_run_time_cap():
    c = cfg(multiplier=10, max_run_seconds=60)
    assert de.check_dose_allowed(c, at("12:00"), 5, 0, None) is None
    assert "above the 60 s limit" in de.check_dose_allowed(c, at("12:00"), 7, 0, None)
    with pytest.raises(ValueError, match="maximum run time"):
        cfg(volume=10, multiplier=10, max_run_seconds=60)  # scheduled dose itself too long


def test_cooldown():
    c = cfg(cooldown_minutes=5)
    assert "cooldown" in de.check_dose_allowed(c, at("12:03"), 1, 0, at("12:00"))
    assert de.check_dose_allowed(c, at("12:06"), 1, 0, at("12:00")) is None
    assert de.check_dose_allowed(cfg(cooldown_minutes=0), at("12:00", sec=1), 1, 0, at("12:00")) is None


def test_daily_cap_automatic_and_explicit():
    c = cfg(interval_minutes=60, volume=1)  # 24 mL/day scheduled -> auto cap 36
    assert c.scheduled_daily_volume() == 24 and c.effective_daily_cap() == 36
    assert de.check_dose_allowed(c, at("12:00"), 1, 35, None) is None
    assert "daily cap of 36 mL" in de.check_dose_allowed(c, at("12:00"), 1, 35.5, None)
    explicit = cfg(interval_minutes=60, volume=1, daily_cap=30)
    assert explicit.effective_daily_cap() == 30
    with pytest.raises(ValueError, match="below the scheduled"):
        cfg(interval_minutes=60, volume=1, daily_cap=10)


def test_rolling_24h_delivered_counts_done_and_interrupted_only():
    now = at("12:00")
    hist = [
        {"ts": (now - timedelta(hours=25)).isoformat(), "volume": 5, "result": "done"},  # too old
        {"ts": (now - timedelta(hours=23)).isoformat(), "volume": 2, "result": "done"},
        {"ts": (now - timedelta(hours=1)).isoformat(), "volume": 1.5, "result": "interrupted"},
        {"ts": (now - timedelta(hours=1)).isoformat(), "volume": 9, "result": "failed"},
        {"ts": (now - timedelta(hours=1)).isoformat(), "volume": 10, "result": "calibration"},
        {"ts": (now - timedelta(hours=1)).isoformat(), "volume": 9, "result": "skipped"},
    ]
    assert de.delivered_last_24h(hist, now) == 3.5
    assert de.last_dose_time(hist) == now - timedelta(hours=1)


def test_prune_history():
    now = at("12:00")
    hist = [{"ts": (now - timedelta(days=d)).isoformat(), "volume": 1, "result": "done"} for d in (9, 6, 1, 0)]
    assert len(de.prune_history(hist, now)) == 3
    assert len(de.prune_history(hist, now, max_entries=2)) == 2


# -------------------------------------------------------------- calibration
def test_calibration_uses_the_multiplier_that_was_used_for_the_test():
    # ran 10 mL at 2.0 s/mL (20 s) but only 8 mL came out -> actual 2.5 s/mL
    assert de.calibrated_multiplier(2.0, 10, 8) == 2.5
    # measured more than expected -> faster pump -> smaller multiplier
    assert de.calibrated_multiplier(2.0, 10, 12.5) == 1.6
    with pytest.raises(ValueError):
        de.calibrated_multiplier(2.0, 10, 0)
    with pytest.raises(ValueError, match="outside the allowed"):
        de.calibrated_multiplier(2.0, 10, 0.001)  # absurd measurement


# --------------------------------------------------------------- validation
@pytest.mark.parametrize(
    "bad",
    [
        {"volume": 0}, {"volume": -1}, {"volume": 5000},
        {"multiplier": 0.01}, {"multiplier": 1000},
        {"mode": "random"}, {"max_run_seconds": 0}, {"max_run_seconds": 99999},
        {"cooldown_minutes": -1}, {"interval_minutes": 0}, {"offset_minutes": -5},
        {"interval_minutes": 60, "offset_minutes": 60},
        {"window_start": "25:00"}, {"doses_per_day": 0},
        {"all_hours": False, "window_start": "08:00", "window_end": "08:30", "interval_minutes": 60, "offset_minutes": 59},
    ],
)
def test_validation_rejects(bad):
    with pytest.raises(ValueError):
        cfg(**bad)


def test_roundtrip_and_defaults_are_safe():
    c = cfg(switch=" switch.alk ")
    d = c.to_dict()
    assert d["switch"] == "switch.alk" and d["enabled"] is False  # schedules start disabled
    assert de.PumpConfig.from_dict(d).to_dict() == d


def test_pump_names():
    assert de.validate_pump_name("alk_2") == "alk_2"
    for bad in ("", "Alk", "a b", "x" * 33):
        with pytest.raises(ValueError):
            de.validate_pump_name(bad)
