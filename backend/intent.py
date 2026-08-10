"""Scheduling intent: the bridge between what the LLM understands and what
Python decides.

The LLM extracts *semantic* constraints (a purpose, a calendar date, a local
clock time, and what kind of time constraint it is). It never invents timezone
offsets, DST, ISO datetimes, or availability. This module takes that semantic
intent, merges it into a conversational `SchedulingContext`, and resolves it
into a concrete `Plan` (absolute search window + hard/soft constraints) that the
scheduler can execute deterministically.

A SchedulingContext is a plain dict so it round-trips as JSON to the frontend
between Ask turns. Fields:

    purpose            str            e.g. "dinner" ("" if none)
    date               "YYYY-MM-DD"   the day to search (None = today/next 7d)
    date_end           "YYYY-MM-DD"   end of a multi-day range (None = single day)
    time_kind          str            exact|preferred|after|before|between|daypart|any
    time_start_local   "HH:MM"        lower clock bound (meaning depends on kind)
    time_end_local     "HH:MM"        upper clock bound
    duration_minutes   int            explicit duration (None = infer from purpose)
    location_type      str            virtual|in-person (None = default)
    relax_hours        bool           user explicitly wants outside waking hours
    current_proposal   dict|None      {start, end, title, location_type} last shown
    title              str            derived meeting title
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, date as date_cls
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, model_validator


# Deterministic duration by purpose (minutes). Unknown purpose -> 30.
DEFAULT_DURATIONS = {
    "coffee": 30, "catch up": 30, "catchup": 30, "sync": 30, "standup": 15,
    "meeting": 30, "call": 30, "chat": 30, "1:1": 30, "one on one": 30,
    "lunch": 60, "dinner": 90, "breakfast": 60, "brunch": 90, "drinks": 90,
    "interview": 45, "workout": 60, "gym": 60, "review": 45, "demo": 30,
}

# Purpose -> a sensible daypart window (local minutes) when the user names the
# activity but not a time ("dinner Thursday" implies the evening).
PURPOSE_DAYPARTS = {
    "breakfast": (7 * 60, 10 * 60),
    "brunch": (10 * 60, 13 * 60),
    "coffee": (8 * 60, 17 * 60),
    "lunch": (11 * 60 + 30, 14 * 60),
    "dinner": (17 * 60 + 30, 21 * 60),
    "drinks": (17 * 60, 22 * 60),
}

# Named dayparts -> (start_min, end_min) local.
DAYPARTS = {
    "morning": (9 * 60, 12 * 60),
    "afternoon": (12 * 60, 17 * 60),
    "evening": (17 * 60, 21 * 60),
    "night": (19 * 60, 22 * 60),
}


class SchedulingIntent(BaseModel):
    """A validated semantic update extracted from the user's current message."""

    model_config = ConfigDict(extra="forbid")

    action: Literal["search", "windows", "book", "cancel", "explain"] = "search"
    purpose: str | None = Field(default=None, max_length=100)
    weekday: Literal[
        "monday", "tuesday", "wednesday", "thursday",
        "friday", "saturday", "sunday",
    ] | None = None
    day_offset: int | None = Field(default=None, ge=0, le=365)
    next_week: bool | None = None
    explicit_date: str | None = None
    span_days: int | None = Field(default=None, ge=1, le=31)
    time_kind: Literal[
        "exact", "preferred", "after", "before", "between", "daypart", "any",
    ] | None = None
    time_start_local: str | None = Field(default=None, pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    time_end_local: str | None = Field(default=None, pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    duration_minutes: int | None = Field(default=None, ge=5, le=480)
    location_type: Literal["virtual", "in-person"] | None = None
    relative_shift: Literal["earlier", "later"] | None = None
    relax_hours: bool | None = None

    @model_validator(mode="after")
    def coherent_constraints(self):
        day_refs = sum(value is not None for value in
                       (self.weekday, self.day_offset, self.explicit_date))
        if day_refs > 1:
            raise ValueError("use only one day reference")
        if self.explicit_date and _parse_date(self.explicit_date) is None:
            raise ValueError("explicit_date must be an ISO calendar date")
        needs_start = self.time_kind in ("exact", "preferred", "after", "between", "daypart")
        needs_end = self.time_kind in ("before", "between", "daypart")
        if needs_start and not self.time_start_local:
            raise ValueError(f"{self.time_kind} requires time_start_local")
        if needs_end and not self.time_end_local:
            raise ValueError(f"{self.time_kind} requires time_end_local")
        if self.time_kind == "any" and (self.time_start_local or self.time_end_local):
            raise ValueError("time_kind any cannot include clock times")
        return self


def infer_duration(purpose: str, explicit: int | None = None) -> int:
    if explicit:
        return int(explicit)
    p = (purpose or "").strip().lower()
    if p in DEFAULT_DURATIONS:
        return DEFAULT_DURATIONS[p]
    for key, mins in DEFAULT_DURATIONS.items():
        if key in p:
            return mins
    return 30


def title_from_purpose(purpose: str) -> str:
    p = (purpose or "").strip()
    return p[:1].upper() + p[1:] if p else "Meeting"


def _hhmm_to_min(s: str | None) -> int | None:
    if not s:
        return None
    try:
        h, m = str(s).split(":")
        return int(h) * 60 + int(m)
    except (ValueError, AttributeError):
        return None


def _parse_date(s: str | None) -> date_cls | None:
    if not s:
        return None
    try:
        return date_cls.fromisoformat(str(s)[:10])
    except ValueError:
        return None


# ---------- context merge ----------

_TIME_FIELDS = ("time_kind", "time_start_local", "time_end_local")
_DATE_FIELDS = ("weekday", "day_offset", "next_week", "explicit_date", "span_days")
_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def merge_intent(prev: dict | None, intent: dict | SchedulingIntent | None) -> dict:
    """Apply one validated semantic update to the running context.

    Clearing follows general meaning rather than phrase lists: a changed
    purpose drops an inherited clock/duration unless the message supplied new
    ones, and a broad windows request drops an inherited clock constraint.
    """
    ctx = dict(prev or {})
    if isinstance(intent, SchedulingIntent):
        update = intent.model_dump(exclude_none=True)
        action = update.pop("action", "search")
        new_purpose = update.get("purpose")
        supplies_time = (update.get("time_kind") not in (None, "any") or
                         update.get("time_start_local") is not None or
                         update.get("time_end_local") is not None)
        if new_purpose and new_purpose != ctx.get("purpose"):
            ctx.pop("title", None)
            if not supplies_time:
                for field in _TIME_FIELDS:
                    ctx.pop(field, None)
            if update.get("duration_minutes") is None:
                ctx.pop("duration_minutes", None)
        has_new_day = (update.get("weekday") is not None or
                       update.get("day_offset") is not None or
                       update.get("explicit_date") is not None)
        if action == "windows" and has_new_day and not supplies_time:
            for field in _TIME_FIELDS:
                ctx.pop(field, None)
        intent = update

    # Compatibility for internal callers that already provide a partial dict.
    intent = intent or {}

    if intent.get("purpose"):
        ctx["purpose"] = intent["purpose"]
        # Titles are derived from purpose; never retain a stale prior title.
        ctx.pop("title", None)

    # a new day reference replaces the whole prior day reference (all-or-nothing)
    has_day = (intent.get("weekday") or intent.get("day_offset") is not None
               or intent.get("explicit_date"))
    if has_day:
        for f in _DATE_FIELDS:
            ctx[f] = intent.get(f)

    kind = intent.get("time_kind")
    if kind and kind != "any":
        for f in _TIME_FIELDS:
            ctx[f] = intent.get(f)
    if intent.get("duration_minutes"):
        ctx["duration_minutes"] = int(intent["duration_minutes"])
    if intent.get("location_type") in ("virtual", "in-person"):
        ctx["location_type"] = intent["location_type"]
    if intent.get("relax_hours") is not None:
        ctx["relax_hours"] = bool(intent["relax_hours"])

    ctx["_relative_shift"] = intent.get("relative_shift")  # transient, not persisted long
    return ctx


def _resolve_date(ctx: dict, today: date_cls) -> tuple[date_cls | None, date_cls | None]:
    """Turn classified day tokens into a concrete (start_date, end_date).

    Python owns this math — the LLM never computes dates. Weekdays resolve to the
    soonest upcoming occurrence (today counts); "next" pushes a further week.
    """
    exp = _parse_date(ctx.get("explicit_date"))
    off = ctx.get("day_offset")
    wd = (ctx.get("weekday") or "").strip().lower()
    base = None
    if exp:
        base = exp
    elif off is not None:
        try:
            base = today + timedelta(days=int(off))
        except (TypeError, ValueError):
            base = None
    elif wd in _WEEKDAYS:
        delta = (_WEEKDAYS.index(wd) - today.weekday()) % 7
        base = today + timedelta(days=delta)
        if ctx.get("next_week"):
            base += timedelta(days=7)

    if base is None:
        return None, None
    span = ctx.get("span_days")
    try:
        end = base + timedelta(days=int(span) - 1) if span and int(span) > 1 else base
    except (TypeError, ValueError):
        end = base
    return base, end


# ---------- plan resolution ----------

@dataclass
class Plan:
    window_start: datetime          # absolute hard lower bound
    window_end: datetime            # absolute hard upper bound
    day_lo_min: int | None          # within-day band (local minutes), None = waking
    day_hi_min: int | None
    duration_minutes: int
    location_type: str
    title: str
    purpose: str
    exact_start: datetime | None    # a hard exact-time request
    preferred_start: datetime | None  # a soft "around this time" request
    relax_hours: bool


def resolve_plan(ctx: dict, org_tz: str, now: datetime,
                 default_location: str = "virtual",
                 default_days: int = 7,
                 waking_start: int = 7 * 60, waking_end: int = 22 * 60) -> Plan:
    """Turn a merged context into concrete, deterministic search parameters.

    Python owns every relative-date / weekday / timezone / DST / window
    decision here — the LLM only labeled what the user meant.
    """
    tz = ZoneInfo(org_tz)
    now = now.astimezone(tz)

    purpose = ctx.get("purpose") or ""
    duration = infer_duration(purpose, ctx.get("duration_minutes"))
    location = ctx.get("location_type") or default_location
    title = ctx.get("title") or title_from_purpose(purpose)
    relax = bool(ctx.get("relax_hours"))

    the_date, date_end = _resolve_date(ctx, now.date())

    kind = ctx.get("time_kind") or "any"
    lo = _hhmm_to_min(ctx.get("time_start_local"))
    hi = _hhmm_to_min(ctx.get("time_end_local"))

    # Naming an explicit clock time outside waking hours *is* the override —
    # "schedule 6am" or "dinner at 11pm" is an explicit request, so honor it.
    for t in (lo, hi):
        if t is not None and (t < waking_start or t > waking_end):
            relax = True

    # if a purpose implies a daypart and the user gave no concrete clock time,
    # use it — regardless of how the model labelled time_kind (dinner is evening
    # whether it called it "any", "preferred", or "daypart")
    if lo is None and hi is None and purpose:
        pk = purpose.strip().lower()
        for key, (a, b) in PURPOSE_DAYPARTS.items():
            if key in pk:
                kind, lo, hi = "daypart", a, b
                break

    def at(d: date_cls, minutes: int) -> datetime:
        return datetime(d.year, d.month, d.day, tzinfo=tz) + timedelta(minutes=minutes)

    day_lo_min = day_hi_min = None
    exact_start = preferred_start = None

    if the_date is None:
        # no date at all -> roll a multi-day search starting now
        win_start = now
        win_end = now + timedelta(days=default_days)
    else:
        base = the_date
        last = date_end or the_date
        win_start = at(base, 0)
        win_end = at(last, 24 * 60)  # end of the last day

        if kind == "exact" and lo is not None:
            # search the whole day too, so alternatives exist if the exact time is taken
            exact_start = at(base, lo)
        elif kind == "preferred" and lo is not None:
            preferred_start = at(base, lo)
            # search the whole day but reward the preferred time
        elif kind == "after" and lo is not None:
            win_start = at(base, lo)
        elif kind == "before" and hi is not None:
            win_end = at(base, hi)
        elif kind == "between" and lo is not None and hi is not None:
            win_start, win_end = at(base, lo), at(base, hi)
        elif kind == "daypart" and lo is not None and hi is not None:
            day_lo_min, day_hi_min = lo, hi

    # "anything earlier / later" — pivot around the last proposal, but stay in
    # the same part of the day (an earlier *dinner* shouldn't become breakfast)
    shift = ctx.get("_relative_shift")
    prop = ctx.get("current_proposal") or {}
    if shift and prop.get("start"):
        try:
            p_start = datetime.fromisoformat(prop["start"]).astimezone(tz)
            p_end = datetime.fromisoformat(prop["end"]).astimezone(tz)
            day = p_start.replace(hour=0, minute=0, second=0, microsecond=0)
            if day_lo_min is None and day_hi_min is None:
                ph = p_start.hour + p_start.minute / 60.0
                if ph < 12:
                    day_lo_min, day_hi_min = 7 * 60, 12 * 60      # morning
                elif ph < 17:
                    day_lo_min, day_hi_min = 12 * 60, 17 * 60     # afternoon
                else:
                    day_lo_min, day_hi_min = 17 * 60, 22 * 60     # evening
            if shift == "earlier":
                win_start = max(day, now)
                win_end = p_start
            elif shift == "later":
                win_start = p_end
                win_end = day + timedelta(days=1)
            exact_start = preferred_start = None
        except (ValueError, KeyError):
            pass

    # never search the past
    if win_start < now:
        win_start = now
    if win_end <= win_start:
        win_end = win_start + timedelta(minutes=max(duration, 30))

    return Plan(
        window_start=win_start, window_end=win_end,
        day_lo_min=day_lo_min, day_hi_min=day_hi_min,
        duration_minutes=duration, location_type=location,
        title=title, purpose=purpose,
        exact_start=exact_start, preferred_start=preferred_start,
        relax_hours=relax,
    )
