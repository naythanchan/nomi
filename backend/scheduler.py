"""The scheduling engine.

Given each participant's busy blocks, a search window, a duration, and a
location type, it scores every candidate start time and returns the best few.

Hard constraints (never silently violated):
  1. Everyone with a visible calendar must be free across the slot.
  2. In-person meetings reserve `travel_buffer_minutes` of clear time on both
     sides (virtual meetings do not).
  3. Waking hours — a slot must fall within [waking.start, waking.end] in the
     LOCAL time of every participant whose timezone is known. Unknown timezones
     are never guessed. Can be relaxed when the user explicitly asks.

Soft preferences (only break ties between valid slots):
  - proximity to a requested "around this time" (preferred) time
  - gentle daytime shaping, avoiding the very edges of waking hours and lunch
  - timezone kindness for virtual meetings across zones
  - keeping long free blocks intact (anti-fragmentation)
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo


@dataclass
class Participant:
    email: str
    name: str
    tz: str | None                 # IANA timezone, or None if unknown
    busy: list[tuple[datetime, datetime]]  # aware datetimes


@dataclass
class ScoredSlot:
    start: datetime
    end: datetime
    score: int
    reasons: list[tuple[str, str]] = field(default_factory=list)  # (polarity, text)


def _parse(dt) -> datetime:
    if isinstance(dt, datetime):
        d = dt
    else:
        d = datetime.fromisoformat(str(dt).replace("Z", "+00:00"))
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d


def load_settings(path: str) -> dict:
    with open(path) as f:
        return json.load(f)


def build_participant(email, name, tz, busy_blocks) -> Participant:
    parsed = []
    for b in busy_blocks or []:
        try:
            parsed.append((_parse(b["start"]), _parse(b["end"])))
        except (KeyError, ValueError):
            continue
    return Participant(email=email, name=name or email, tz=tz or None, busy=parsed)


def _all_free(people: list[Participant], start: datetime, end: datetime) -> bool:
    for p in people:
        for bs, be in p.busy:
            if bs < end and be > start:
                return False
    return True


def _within_waking(people, start, end, waking) -> bool:
    """True if [start, end) sits inside waking hours for everyone with a known tz."""
    lo, hi = waking["start"], waking["end"]
    for p in people:
        if not p.tz:
            continue  # unknown timezone — cannot judge, so don't block
        try:
            z = ZoneInfo(p.tz)
        except Exception:
            continue
        ls = start.astimezone(z)
        le = end.astimezone(z)
        ls_min = ls.hour * 60 + ls.minute
        le_min = le.hour * 60 + le.minute
        if le.date() != ls.date():
            le_min = 24 * 60  # crossed midnight -> past waking end
        if ls_min < lo or le_min > hi:
            return False
    return True


def slot_free(people, start, end, location_type, settings, *,
              enforce_waking=True) -> bool:
    """Is one specific slot bookable? (availability + buffer + waking hours)"""
    buffer_min = settings["slot"]["travel_buffer_minutes"] if location_type == "in-person" else 0
    buf = timedelta(minutes=buffer_min)
    if not _all_free(people, start - buf, end + buf):
        return False
    if enforce_waking and not _within_waking(people, start, end, settings["waking_hours"]):
        return False
    return True


def _shared_gap_minutes(people, start, end, day_lo, day_hi, step) -> float:
    """Size (minutes) of the contiguous all-free span containing [start, end)."""
    s, e = start, end
    stepd = timedelta(minutes=step)
    while s - stepd >= day_lo and _all_free(people, s - stepd, s):
        s -= stepd
    while e + stepd <= day_hi and _all_free(people, e, e + stepd):
        e += stepd
    return (e - s).total_seconds() / 60.0


def find_slots(
    people: list[Participant],
    window_start: datetime,
    window_end: datetime,
    duration_minutes: int,
    location_type: str,
    settings: dict,
    org_tz: str,
    *,
    day_lo_min: int | None = None,
    day_hi_min: int | None = None,
    preferred_start_min: int | None = None,   # local minutes-of-day to reward (soft)
    enforce_waking: bool = True,
    top_k: int = 6,
) -> list[ScoredSlot]:
    tz = ZoneInfo(org_tz)
    window_start = _parse(window_start).astimezone(tz)
    window_end = _parse(window_end).astimezone(tz)

    waking = settings["waking_hours"]
    lunch = settings["lunch_window"]
    pen = settings["penalties"]
    bon = settings["bonuses"]
    step = settings["slot"]["interval_minutes"]
    buffer_min = settings["slot"]["travel_buffer_minutes"] if location_type == "in-person" else 0

    # within-day band: an explicit daypart, else the full day (waking is the hard filter)
    band_lo = day_lo_min if day_lo_min is not None else 0
    band_hi = day_hi_min if day_hi_min is not None else 24 * 60

    duration = timedelta(minutes=duration_minutes)
    buf = timedelta(minutes=buffer_min)
    multi_tz = len({p.tz for p in people if p.tz}) > 1

    candidates: list[ScoredSlot] = []
    day = window_start.replace(hour=0, minute=0, second=0, microsecond=0)
    last_day = window_end.replace(hour=0, minute=0, second=0, microsecond=0)

    while day <= last_day:
        day_lo = day + timedelta(minutes=band_lo)
        day_hi = day + timedelta(minutes=band_hi)
        m = band_lo
        while m + duration_minutes <= band_hi:
            start = day + timedelta(minutes=m)
            end = start + duration
            m += step
            if start < window_start or end > window_end:
                continue
            # hard: everyone free across the (buffered) span
            if not _all_free(people, start - buf, end + buf):
                continue
            # hard: within everyone's waking hours (known tz only)
            if enforce_waking and not _within_waking(people, start, end, waking):
                continue
            candidates.append(_score(
                people, start, end, day_lo, day_hi, step,
                waking, lunch, pen, bon, location_type, multi_tz, preferred_start_min,
            ))
        day += timedelta(days=1)

    candidates.sort(key=lambda s: (-s.score, s.start))

    # spread picks out so alternatives are genuinely different times
    picked: list[ScoredSlot] = []
    spacing = timedelta(minutes=max(duration_minutes, 45))
    for c in candidates:
        if any(abs((c.start - p.start).total_seconds()) < spacing.total_seconds() for p in picked):
            continue
        picked.append(c)
        if len(picked) >= top_k:
            break
    return picked


def _score(people, start, end, day_lo, day_hi, step, waking, lunch, pen, bon,
           location_type, multi_tz, preferred_start_min) -> ScoredSlot:
    s = 100.0
    reasons: list[tuple[str, str]] = []
    start_min = start.hour * 60 + start.minute
    end_min = end.hour * 60 + end.minute
    w_lo, w_hi = waking["start"], waking["end"]

    # soft proximity to a requested "around this time"
    if preferred_start_min is not None:
        diff = abs(start_min - preferred_start_min)
        if diff == 0:
            s += 40; reasons.append(("good", "right when you asked"))
        elif diff <= 60:
            s += 40 - diff * 0.5
            reasons.append(("good", "close to your preferred time"))
        else:
            s -= min(20, (diff - 60) * 0.1)

    # gentle time-of-day shaping, relative to waking hours
    if w_lo + 120 <= start_min < w_lo + 300:
        s += bon["mid_morning"]; reasons.append(("good", "a gentle mid-morning"))
    if 840 <= start_min < 930:
        s += bon["mid_afternoon"]; reasons.append(("good", "a quiet afternoon"))
    if start_min < lunch["end"] and end_min > lunch["start"]:
        s -= pen["overlaps_lunch"]; reasons.append(("warn", "brushes past lunch"))
    if start_min < w_lo + 90:
        s -= pen["early_morning"]; reasons.append(("warn", "an early start"))
    if end_min > w_hi - 90:
        s -= pen["late_evening"]; reasons.append(("warn", "late in the day"))

    # fragmentation
    gap = _shared_gap_minutes(people, start, end, day_lo, day_hi, step)
    dur = (end - start).total_seconds() / 60.0
    if gap >= 180:
        s += bon["protects_focus"]; reasons.append(("good", "preserving focus time"))
    elif gap <= dur + 30:
        s -= pen["splits_short_gap"]; reasons.append(("warn", "splitting a short gap"))

    # location
    if location_type == "in-person":
        if 600 <= start_min < 900:
            s += bon["in_person_midday"]
        reasons.append(("good", "with room to travel"))
    else:
        s += bon["virtual"]
        # timezone kindness (known tz only)
        pain = 0.0
        for p in people:
            if not p.tz:
                continue
            try:
                z = ZoneInfo(p.tz)
            except Exception:
                continue
            ls = start.astimezone(z)
            le = end.astimezone(z)
            lsh = ls.hour + ls.minute / 60.0
            leh = le.hour + le.minute / 60.0
            if lsh < 8.5:
                pain += 8.5 - lsh
            if leh > 17.5:
                pain += leh - 17.5
        if pain > 0:
            s -= min(pen["max_timezone_penalty"], pain * pen["per_timezone_hour_out"])
            reasons.append(("warn", "stretching a timezone"))
        elif multi_tz:
            s += bon["kind_across_timezones"]; reasons.append(("good", "kind to every timezone"))

    # day of week
    wd = start.weekday()  # Mon=0
    if wd == 0 and start_min < 660:
        s += bon["monday_morning"]
    if wd == 4 and start_min > 840:
        s -= pen["friday_afternoon"]; reasons.append(("warn", "a Friday wind-down"))

    score = max(0, min(100, round(s)))
    # keep the strongest, most relevant reasons
    return ScoredSlot(start=start, end=end, score=score, reasons=reasons[:3])
