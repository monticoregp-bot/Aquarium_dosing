"""Pure dosing engine for Aquarium Dosing (no Home Assistant imports).

Everything that decides *when* a pump may run and *whether* a dose is allowed
lives here as plain functions, so it can be unit-tested stand-alone:

* ``PumpConfig``: one pump's settings, validated on construction.
* Slot generation for the three schedule modes (interval, fixed times, daily
  total), all clipped to an optional daily **operating window**.
* ``check_dose_allowed``: the pre-dose safety checks (window, run-time cap,
  cooldown, rolling 24 h cap).
* Ledger helpers (rolling delivered volume, pruning) and calibration maths.

Time handling: every function takes the caller's ``now`` (timezone-aware in
Home Assistant) and builds slot datetimes with the same tzinfo.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field, fields
from datetime import date, datetime, time, timedelta, tzinfo
from typing import Any

MINUTES_PER_DAY = 1440
MODES = ("interval", "times", "daily_total")
MIN_MULTIPLIER = 0.1  # sec/mL
MAX_MULTIPLIER = 600.0
HARD_MAX_RUN_SECONDS = 3600
CAL_VOLUME = 10.0  # mL pumped by the calibration test dose
AUTO_CAP_FACTOR = 1.5  # automatic daily cap = this x the scheduled daily total
MAX_DOSES_PER_DAY = 288
NAME_RE = re.compile(r"^[a-z0-9_]{1,32}$")
COUNTED_RESULTS = ("done", "interrupted")  # these count towards the daily cap


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def parse_hhmm(value: Any) -> int:
    """Parse "HH:MM" (or "HH:MM:SS") into minutes since midnight."""
    try:
        hours_s, minutes_s = str(value).strip().split(":")[:2]
        hours, minutes = int(hours_s), int(minutes_s)
    except (ValueError, TypeError):
        raise ValueError(f"Invalid time {value!r}; expected HH:MM") from None
    if not (0 <= hours < 24 and 0 <= minutes < 60):
        raise ValueError(f"Invalid time {value!r}; expected HH:MM (00:00-23:59)")
    return hours * 60 + minutes


def format_hhmm(minutes: int) -> str:
    minutes %= MINUTES_PER_DAY
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def validate_pump_name(name: str) -> str:
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise ValueError(
            "Pump names may only contain lowercase letters, digits and "
            "underscores (max 32 characters)"
        )
    return name


def minute_of_day(when: datetime) -> float:
    return when.hour * 60 + when.minute + when.second / 60 + when.microsecond / 6e7


@dataclass(frozen=True)
class Slot:
    """One scheduled dose."""

    at: datetime
    volume: float


# --------------------------------------------------------------------------- #
# pump configuration
# --------------------------------------------------------------------------- #
@dataclass
class PumpConfig:
    """Settings for one dosing pump."""

    switch: str = ""  # switch entity that powers the pump
    volume: float = 1.0  # mL per dose (interval / times modes)
    multiplier: float = 1.0  # calibration, seconds per mL
    mode: str = "interval"
    enabled: bool = False  # schedule on/off (safe default: off)
    # Operating window: scheduled AND manual doses only run inside it.
    # Half-open [start, end); may cross midnight (e.g. 20:00 -> 06:00).
    all_hours: bool = True
    window_start: str = "08:00"
    window_end: str = "22:00"
    # interval mode: first dose at window start + offset, then every N minutes
    interval_minutes: int = 60
    offset_minutes: int = 0
    # times mode: explicit clock times, each inside the window
    times: list[str] = field(default_factory=list)
    # daily_total mode: split evenly across the window (offset shifts the start)
    daily_total: float = 0.0
    doses_per_day: int = 1
    # safety
    max_run_seconds: int = 120
    daily_cap: float = 0.0  # mL per rolling 24 h; 0 = automatic (1.5 x scheduled)
    cooldown_minutes: int = 1

    def __post_init__(self) -> None:
        self.validate()

    # -- validation --------------------------------------------------------- #
    def validate(self) -> None:
        """Normalise fields in place; raise ValueError if the config is unsafe."""
        self.switch = (self.switch or "").strip()
        try:
            self.volume = float(self.volume)
            self.multiplier = float(self.multiplier)
            self.daily_total = float(self.daily_total)
            self.daily_cap = float(self.daily_cap)
            self.interval_minutes = int(self.interval_minutes)
            self.offset_minutes = int(self.offset_minutes)
            self.doses_per_day = int(self.doses_per_day)
            self.max_run_seconds = int(self.max_run_seconds)
            self.cooldown_minutes = int(self.cooldown_minutes)
        except (TypeError, ValueError):
            raise ValueError("Numeric settings must be numbers") from None
        self.enabled = bool(self.enabled)
        self.all_hours = bool(self.all_hours)

        if self.mode not in MODES:
            raise ValueError(f"Schedule mode must be one of {', '.join(MODES)}")
        if not 0 < self.volume <= 1000:
            raise ValueError("Dose volume must be above 0 and at most 1000 mL")
        if not MIN_MULTIPLIER <= self.multiplier <= MAX_MULTIPLIER:
            raise ValueError(
                f"Calibration multiplier must be between {MIN_MULTIPLIER:g} and "
                f"{MAX_MULTIPLIER:g} sec/mL"
            )
        if not 1 <= self.max_run_seconds <= HARD_MAX_RUN_SECONDS:
            raise ValueError(f"Maximum run time must be 1-{HARD_MAX_RUN_SECONDS} s")
        if not 0 <= self.cooldown_minutes <= MINUTES_PER_DAY:
            raise ValueError("Cooldown must be 0-1440 minutes")
        if self.daily_total < 0 or self.daily_cap < 0:
            raise ValueError("Daily total and daily cap cannot be negative")
        if not 1 <= self.interval_minutes <= MINUTES_PER_DAY:
            raise ValueError("Interval must be 1-1440 minutes")
        if self.offset_minutes < 0:
            raise ValueError("Offset cannot be negative")
        if not 1 <= self.doses_per_day <= MAX_DOSES_PER_DAY:
            raise ValueError(f"Doses per day must be 1-{MAX_DOSES_PER_DAY}")

        # operating window
        start, end = parse_hhmm(self.window_start), parse_hhmm(self.window_end)
        self.window_start, self.window_end = format_hhmm(start), format_hhmm(end)
        if not self.all_hours and start == end:
            raise ValueError(
                "The operating window needs different start and end times "
                "(or switch on all hours)"
            )
        self._wstart = start
        self._wlen = MINUTES_PER_DAY if self.all_hours else (end - start) % MINUTES_PER_DAY
        self._anchor = 0 if self.all_hours else start

        parsed = sorted({parse_hhmm(t) for t in (self.times or [])})
        self.times = [format_hhmm(m) for m in parsed]

        self._offsets = self._build_offsets(parsed)
        if not self._offsets:
            raise ValueError("No doses fit inside the operating window")

        for _, vol in self._offsets:
            seconds = vol * self.multiplier
            if seconds > self.max_run_seconds:
                raise ValueError(
                    f"A {vol:g} mL dose takes {seconds:.0f} s, above the "
                    f"{self.max_run_seconds} s maximum run time"
                )
        if self.daily_cap and self.daily_cap + 1e-9 < self.scheduled_daily_volume():
            raise ValueError("The daily cap is below the scheduled daily total")

    def _build_offsets(self, parsed_times: list[int]) -> list[tuple[int, float]]:
        """(minutes after the window anchor, volume) for each dose of the day."""
        wlen = self._wlen
        if self.mode == "interval":
            if self.all_hours and MINUTES_PER_DAY % self.interval_minutes:
                raise ValueError(
                    "With all hours on, the interval must divide 24 h evenly "
                    "(for example 30, 60, 120, 180, 240, 360, 480, 720 or 1440 minutes) "
                    "so the pattern is the same every day; or set an operating window"
                )
            if self.offset_minutes >= self.interval_minutes:
                raise ValueError("Offset must be smaller than the interval")
            offsets, at = [], self.offset_minutes
            while at < wlen:
                offsets.append((at, self.volume))
                at += self.interval_minutes
            return offsets

        if self.mode == "times":
            if not parsed_times:
                raise ValueError("Add at least one dose time")
            offsets = []
            for minute in parsed_times:
                rel = (minute - self._anchor) % MINUTES_PER_DAY
                if rel >= wlen:
                    raise ValueError(
                        f"Dose time {format_hhmm(minute)} is outside the operating "
                        f"window ({self.window_start}-{self.window_end})"
                    )
                offsets.append((rel, self.volume))
            return sorted(offsets)

        # daily_total
        if self.daily_total <= 0:
            raise ValueError("Daily total must be above 0 mL in daily-total mode")
        step = wlen / self.doses_per_day
        if step < 1:
            raise ValueError("Too many doses per day for the operating window")
        if self.offset_minutes >= step:
            raise ValueError("Offset must be smaller than the gap between doses")
        each = self.daily_total / self.doses_per_day
        return [(self.offset_minutes + round(k * step), each) for k in range(self.doses_per_day)]

    # -- (de)serialisation -------------------------------------------------- #
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PumpConfig":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in dict(data).items() if k in known})

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    # -- derived values ----------------------------------------------------- #
    @property
    def anchor_minute(self) -> int:
        return self._anchor

    @property
    def window_minutes(self) -> int:
        return self._wlen

    def in_window(self, minute: float) -> bool:
        """True if ``minute`` (since midnight) is inside the operating window."""
        if self.all_hours:
            return True
        return (minute - self._wstart) % MINUTES_PER_DAY < self._wlen

    def slot_offsets(self) -> list[tuple[int, float]]:
        return list(self._offsets)

    def scheduled_daily_volume(self) -> float:
        return round(sum(v for _, v in self._offsets), 6)

    def effective_daily_cap(self) -> float:
        if self.daily_cap > 0:
            return self.daily_cap
        return round(AUTO_CAP_FACTOR * self.scheduled_daily_volume(), 6)

    def manual_volume(self) -> float:
        """Default volume for a manual "dose now"."""
        if self.mode == "daily_total":
            return round(self.daily_total / self.doses_per_day, 6)
        return self.volume

    def run_seconds(self, volume: float) -> float:
        return volume * self.multiplier


# --------------------------------------------------------------------------- #
# slots
# --------------------------------------------------------------------------- #
def slots_for_anchor_day(cfg: PumpConfig, day: date, tz: tzinfo | None) -> list[Slot]:
    """Slots of the schedule instance whose window starts on ``day``.

    A midnight-crossing window starting on ``day`` yields slots that fall on
    ``day + 1``.
    """
    base = datetime.combine(day, time(0, 0), tzinfo=tz) + timedelta(minutes=cfg.anchor_minute)
    return [Slot(base + timedelta(minutes=off), vol) for off, vol in cfg.slot_offsets()]


def due_slots(cfg: PumpConfig, now: datetime, after: datetime) -> list[Slot]:
    """Slots with ``after < slot.at <= now`` (oldest first)."""
    found: list[Slot] = []
    for day in (now.date() - timedelta(days=1), now.date()):
        found += [s for s in slots_for_anchor_day(cfg, day, now.tzinfo) if after < s.at <= now]
    return sorted(found, key=lambda s: s.at)


def next_slot(cfg: PumpConfig, now: datetime) -> Slot | None:
    future: list[Slot] = []
    for delta in (-1, 0, 1):
        day = now.date() + timedelta(days=delta)
        future += [s for s in slots_for_anchor_day(cfg, day, now.tzinfo) if s.at > now]
    return min(future, key=lambda s: s.at, default=None)


def slots_on_date(cfg: PumpConfig, day: date, tz: tzinfo | None) -> list[Slot]:
    """Slots that fall on calendar ``day`` (includes the tail of yesterday's window)."""
    found: list[Slot] = []
    for anchor in (day - timedelta(days=1), day):
        found += [s for s in slots_for_anchor_day(cfg, anchor, tz) if s.at.date() == day]
    return sorted(found, key=lambda s: s.at)


# --------------------------------------------------------------------------- #
# ledger helpers (history entries are plain dicts with an ISO "ts")
# --------------------------------------------------------------------------- #
def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


def delivered_last_24h(history: list[dict[str, Any]], now: datetime) -> float:
    cutoff = now - timedelta(hours=24)
    total = 0.0
    for entry in history:
        if entry.get("result") in COUNTED_RESULTS and _ts(entry["ts"]) > cutoff:
            total += float(entry.get("volume", 0.0))
    return round(total, 6)


def last_dose_time(history: list[dict[str, Any]]) -> datetime | None:
    times = [_ts(e["ts"]) for e in history if e.get("result") in COUNTED_RESULTS]
    return max(times, default=None)


def prune_history(
    history: list[dict[str, Any]], now: datetime, keep_days: int = 7, max_entries: int = 500
) -> list[dict[str, Any]]:
    cutoff = now - timedelta(days=keep_days)
    kept = [e for e in history if _ts(e["ts"]) >= cutoff]
    return kept[-max_entries:]


# --------------------------------------------------------------------------- #
# safety
# --------------------------------------------------------------------------- #
def check_dose_allowed(
    cfg: PumpConfig,
    now: datetime,
    volume: float,
    delivered_24h: float,
    last_dose_at: datetime | None,
    *,
    ignore_window: bool = False,
) -> str | None:
    """Return why a dose must not run now, or None if it may."""
    if not ignore_window and not cfg.in_window(minute_of_day(now)):
        return f"outside the operating window ({cfg.window_start}-{cfg.window_end})"
    seconds = cfg.run_seconds(volume)
    if seconds > cfg.max_run_seconds:
        return f"the dose would run {seconds:.0f} s, above the {cfg.max_run_seconds} s limit"
    if cfg.cooldown_minutes and last_dose_at is not None:
        if now - last_dose_at < timedelta(minutes=cfg.cooldown_minutes):
            return f"within the {cfg.cooldown_minutes} min cooldown after the last dose"
    cap = cfg.effective_daily_cap()
    if delivered_24h + volume > cap + 1e-9:
        return (
            f"it would exceed the daily cap of {cap:g} mL "
            f"({delivered_24h:g} mL delivered in the last 24 h)"
        )
    return None


def calibrated_multiplier(used_multiplier: float, test_volume: float, measured_ml: float) -> float:
    """New sec/mL from a test dose.

    The pump ran ``test_volume * used_multiplier`` seconds and delivered
    ``measured_ml``, so the true rate is ``used_multiplier * test_volume / measured``.
    """
    if measured_ml <= 0:
        raise ValueError("The measured output must be above 0 mL")
    new = used_multiplier * test_volume / measured_ml
    if not MIN_MULTIPLIER <= new <= MAX_MULTIPLIER:
        raise ValueError(
            f"That measurement gives {new:.3f} sec/mL, outside the allowed "
            f"{MIN_MULTIPLIER:g}-{MAX_MULTIPLIER:g}; check the measurement"
        )
    return round(new, 4)
