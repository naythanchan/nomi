"""Nomi — FastAPI app.

Serves the frontend, handles Google sign-in, and exposes the scheduling API.
Booking creates a real Google Calendar event and invites everyone immediately.
"""
import re
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request, Response, HTTPException, Depends
from fastapi.responses import RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import config
import auth
import google_cal as gcal
import scheduler
import intent as intents
import llm
from db import init_db, db_session, User, BookedMeeting
from schemas import ScheduleRequest, BookRequest, SmartScheduleRequest, AskRequest

app = FastAPI(title="Nomi")
FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
SETTINGS = scheduler.load_settings(config.ORG_SETTINGS_FILE)
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@app.middleware("http")
async def prevent_stale_frontend(request: Request, call_next):
    response = await call_next(request)
    if request.url.path in ("/", "/index.html", "/app.js", "/styles.css"):
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return response


@app.on_event("startup")
def _startup():
    init_db()


# ---------- auth dependency ----------

def current_user(request: Request):
    token = request.cookies.get(config.SESSION_COOKIE)
    user = auth.get_current_user(token)
    if not user:
        raise HTTPException(status_code=401, detail="not authenticated")
    return user


# ---------- auth routes ----------

@app.get("/auth/login")
def login():
    url, state = auth.authorization_url()
    resp = RedirectResponse(url)
    resp.set_cookie(
        "cw_oauth_state", state,
        httponly=True, samesite="lax",
        secure=config.BASE_URL.startswith("https://"),
        max_age=600, path="/auth",
    )
    return resp


@app.get("/auth/callback")
def callback(request: Request):
    expected_state = request.cookies.get("cw_oauth_state")
    returned_state = request.query_params.get("state")
    if not expected_state or not returned_state or not secrets.compare_digest(expected_state, returned_state):
        return JSONResponse({"error": "sign-in state mismatch; please try again"}, status_code=400)
    try:
        # Railway terminates TLS before forwarding to the app, so request.url can
        # appear to be http:// internally. OAuthlib requires the public HTTPS URL.
        authorization_response_url = f"{config.REDIRECT_URI}?{request.url.query}"
        token = auth.handle_callback(authorization_response_url, expected_state)
    except Exception as e:
        return JSONResponse({"error": f"sign-in failed: {e}"}, status_code=400)
    resp = RedirectResponse(config.BASE_URL + "/")
    resp.set_cookie(
        config.SESSION_COOKIE, token,
        httponly=True, samesite="lax",
        secure=config.BASE_URL.startswith("https://"),
        max_age=config.SESSION_DAYS * 86400, path="/",
    )
    resp.delete_cookie("cw_oauth_state", path="/auth")
    return resp


@app.get("/auth/logout")
@app.post("/auth/logout")
def do_logout(request: Request):
    auth.logout(request.cookies.get(config.SESSION_COOKIE))
    resp = RedirectResponse(config.BASE_URL + "/") if request.method == "GET" else JSONResponse({"ok": True})
    resp.delete_cookie(config.SESSION_COOKIE, path="/")
    return resp


# ---------- api ----------

@app.get("/api/me")
def api_me(request: Request):
    token = request.cookies.get(config.SESSION_COOKIE)
    user = auth.get_current_user(token)
    if not user:
        return JSONResponse({"authenticated": False, "ai": llm.ai_enabled()}, status_code=200)
    return {"authenticated": True, "user": user, "ai": llm.ai_enabled()}


@app.get("/api/contacts/search")
def api_contacts(q: str = "", user=Depends(current_user)):
    if len(q.strip()) < 2:
        return []
    return gcal.search_directory(user["id"], q.strip())


def _resolve_attendees(user, tokens):
    """Turn a mix of names/emails into (resolved_emails, unresolved_names, bad_emails)."""
    me = user["email"].lower()
    resolved, unresolved, bad = [], [], []
    for raw in tokens or []:
        tok = (raw or "").strip()
        if not tok:
            continue
        if "@" in tok:
            if EMAIL_RE.match(tok):
                e = tok.lower()
                if e != me and e not in resolved:
                    resolved.append(e)
            else:
                bad.append(tok)
        else:
            email = gcal.resolve_email(user["id"], tok)  # directory lookup
            if email and email.lower() != me and email.lower() not in resolved:
                resolved.append(email.lower())
            else:
                unresolved.append(tok)
    return resolved, unresolved, bad


def _require_valid_attendees(user, tokens):
    """Resolve attendee chips and reject anything that is not a real person/email."""
    resolved, unresolved, bad = _resolve_attendees(user, tokens)
    if bad:
        raise HTTPException(status_code=400, detail="Not a valid email: " + ", ".join(bad))
    if unresolved:
        raise HTTPException(status_code=400, detail="Couldn't identify: " + ", ".join(unresolved))
    if not resolved:
        raise HTTPException(status_code=400, detail="Add at least one valid person to invite.")
    return resolved


def _has_when(ctx):
    """Whether a scheduling request provides a usable date/time constraint."""
    if any(ctx.get(k) for k in ("weekday", "explicit_date", "span_days",
                                "time_start_local", "time_end_local")):
        return True
    if ctx.get("day_offset") is not None:
        return True
    if ctx.get("time_kind") and ctx.get("time_kind") != "any":
        return True
    purpose = (ctx.get("purpose") or "").lower()
    return any(name in purpose for name in intents.PURPOSE_DAYPARTS)


def _windows_followup_intent(text, prior):
    """Parse common availability-window follow-ups without another model call."""
    lowered = (text or "").lower()
    asks_windows = (
        any(word in lowered for word in ("window", "windows", "availability")) and
        any(word in lowered for word in ("free", "empty", "open", "available"))
    )
    if not asks_windows:
        return None

    parsed = {"action": "windows"}
    new_day = False
    if "tomorrow" in lowered or "tmrw" in lowered:
        parsed.update({"day_offset": 1, "weekday": None, "next_week": False})
        new_day = True
    elif "today" in lowered:
        parsed.update({"day_offset": 0, "weekday": None, "next_week": False})
        new_day = True
    else:
        weekdays = {
            "monday": "monday", "mon": "monday", "tuesday": "tuesday", "tue": "tuesday",
            "wednesday": "wednesday", "wed": "wednesday", "thursday": "thursday", "thu": "thursday",
            "friday": "friday", "fri": "friday", "saturday": "saturday", "sat": "saturday",
            "sunday": "sunday", "sun": "sunday",
        }
        for token, weekday in weekdays.items():
            if re.search(rf"\b{token}\b", lowered):
                parsed.update({"weekday": weekday, "day_offset": None,
                               "next_week": "next" in lowered})
                new_day = True
                break

    dayparts = {
        "morning": (9 * 60, 12 * 60),
        "afternoon": (12 * 60, 17 * 60),
        "evening": (17 * 60, 21 * 60),
        "night": (19 * 60, 22 * 60),
    }
    for word, (lo, hi) in dayparts.items():
        if word in lowered:
            parsed.update({"time_kind": "daypart",
                           "time_start_local": f"{lo // 60:02d}:{lo % 60:02d}",
                           "time_end_local": f"{hi // 60:02d}:{hi % 60:02d}"})
            break
    else:
        if new_day:
            parsed["_clear_time"] = True

    if not new_day and not any(word in lowered for word in dayparts) and not _has_when(prior):
        return None
    return parsed


def _wants_shared_availability(text):
    """Whether an availability question explicitly includes the organizer."""
    lowered = (text or "").lower()
    shared_phrases = (
        "shared", "both free", "we both", "we all", "all free", "all of us",
        "everyone", "everybody", "work for us", "our availability",
    )
    return any(phrase in lowered for phrase in shared_phrases)


def _purpose_followup_intent(text, prior):
    """Parse simple activity follow-ups while preserving the prior date."""
    lowered = (text or "").lower()
    if not _has_when(prior):
        return None
    purposes = (
        "breakfast", "brunch", "coffee", "lunch", "dinner", "drinks",
        "sync", "meeting", "call", "interview", "workout", "demo",
    )
    purpose = next((word for word in purposes if re.search(rf"\b{word}\b", lowered)), None)
    if not purpose:
        return None
    # Explicit timing still goes through the full parser. This handles concise
    # contextual turns such as "when's best for lunch?".
    timing_words = (
        "today", "tomorrow", "tmrw", "morning", "afternoon", "evening", "night",
        "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
        " am", " pm",
    )
    if any(word in lowered for word in timing_words) or re.search(r"\d", lowered):
        return None
    return {"action": "search", "purpose": purpose, "_clear_time": True,
            "_clear_duration": True, "_clear_title": True}


def _known_users(emails):
    """Map email -> {id, name, tz, has_token} for attendees who've signed into Nomi."""
    if not emails:
        return {}
    out = {}
    with db_session() as db:
        for u in db.query(User).filter(User.email.in_(emails)).all():
            out[u.email.lower()] = {
                "id": u.id, "name": u.name, "tz": u.timezone,
                "has_token": bool(u.google_access_token),
            }
    return out


def _gather_people(user, org_tz, resolved, win_start, win_end):
    """Build scheduler participants, reading each signed-in attendee's own calendar.

    Returns (people, unknown, statuses):
      - people    = scheduler.Participant list (organizer first)
      - unknown   = attendee emails whose free/busy we couldn't read — invited
                    anyway, but never treated as free
      - statuses  = {email: calendar_status} where status is
                    "full" (own calendar / own token),
                    "freebusy" (best-effort via organizer), or
                    "unavailable" (couldn't check)

    The organizer is always included in the availability search. Attendee
    timezones are only set when actually known (a signed-in Nomi user); unknown
    timezones stay None and are never guessed.
    """
    t0, t1 = win_start.isoformat(), win_end.isoformat()
    known = _known_users(resolved)

    org_email = user["email"].lower()
    org_busy, _ = gcal.freebusy(user["id"], [user["email"]], t0, t1)
    people = [scheduler.build_participant(
        user["email"], user.get("name") or user["email"], org_tz,
        org_busy.get(user["email"], []),
    )]
    statuses = {org_email: "full"}

    unknown = []
    for email in resolved:
        k = known.get(email.lower())
        if k and k["has_token"]:
            # read the attendee's own calendar with their own credentials
            bm, un = gcal.freebusy(k["id"], [email], t0, t1)
            status = "unavailable" if email in un else "full"
            tz = k["tz"]
        else:
            # best-effort via the organizer's credentials
            bm, un = gcal.freebusy(user["id"], [email], t0, t1)
            status = "unavailable" if email in un else "freebusy"
            tz = k["tz"] if k else None
        if status == "unavailable":
            unknown.append(email)
        statuses[email.lower()] = status
        people.append(scheduler.build_participant(
            email,
            (k["name"] if k and k["name"] else email),
            tz,
            bm.get(email, []),
        ))
    return people, unknown, statuses


def _slot_dict(s, *, title=None, location_type=None):
    d = {
        "start": s.start.isoformat(),
        "end": s.end.isoformat(),
        "score": s.score,
        "reasons": [{"type": t, "text": txt} for t, txt in s.reasons],
    }
    if title is not None:
        d["title"] = title
    if location_type is not None:
        d["location_type"] = location_type
    return d


def _window_description(plan, org_tz):
    tz = ZoneInfo(org_tz)
    start = plan.window_start.astimezone(tz)
    end = plan.window_end.astimezone(tz)
    if plan.day_lo_min is not None and plan.day_hi_min is not None:
        lo = (start.replace(hour=0, minute=0, second=0, microsecond=0) +
              timedelta(minutes=plan.day_lo_min))
        hi = (start.replace(hour=0, minute=0, second=0, microsecond=0) +
              timedelta(minutes=plan.day_hi_min))
        return f"{lo.strftime('%A, %b %-d from %-I:%M %p')} to {hi.strftime('%-I:%M %p')}"
    return f"{start.strftime('%A, %b %-d %-I:%M %p')} to {end.strftime('%A, %b %-d %-I:%M %p')}"


def _requested_blockers(people, start, end, location_type):
    blockers = []
    wake = SETTINGS["waking_hours"]
    buffer_min = SETTINGS["slot"]["travel_buffer_minutes"] if location_type == "in-person" else 0
    buf = timedelta(minutes=buffer_min)
    for person in people:
        if any(bs < end and be > start for bs, be in person.busy):
            blockers.append(f"{person.name} is busy then")
            continue
        if buffer_min and any(bs < end + buf and be > start - buf for bs, be in person.busy):
            blockers.append(f"{person.name} doesn't have the {buffer_min}-minute travel buffer")
            continue
        if person.tz:
            local_start, local_end = start.astimezone(ZoneInfo(person.tz)), end.astimezone(ZoneInfo(person.tz))
            sm = local_start.hour * 60 + local_start.minute
            em = local_end.hour * 60 + local_end.minute
            if local_end.date() != local_start.date() or sm < wake["start"] or em > wake["end"]:
                blockers.append(f"it falls outside {person.name}'s waking hours")
    return blockers


def _availability_windows(people, plan, org_tz):
    """Return contiguous shared-free ranges, using the scheduler's hard rules."""
    tz = ZoneInfo(org_tz)
    step = timedelta(minutes=SETTINGS["slot"]["interval_minutes"])
    minimum = timedelta(minutes=plan.duration_minutes)
    start = plan.window_start.astimezone(tz)
    end = plan.window_end.astimezone(tz)
    cursor = start.replace(second=0, microsecond=0)
    if cursor.minute % 15:
        cursor += timedelta(minutes=15 - cursor.minute % 15)

    windows = []
    open_start = None
    while cursor < end:
        segment_end = min(cursor + step, end)
        local_minute = cursor.hour * 60 + cursor.minute
        in_band = ((plan.day_lo_min is None or local_minute >= plan.day_lo_min) and
                   (plan.day_hi_min is None or
                    local_minute + int(step.total_seconds() / 60) <= plan.day_hi_min))
        free = (in_band and segment_end.date() == cursor.date() and
                scheduler.slot_free(
                    people, cursor, segment_end, plan.location_type, SETTINGS,
                    enforce_waking=not plan.relax_hours,
                ))
        if free and open_start is None:
            open_start = cursor
        if not free and open_start is not None:
            if cursor - open_start >= minimum:
                windows.append({"start": open_start.isoformat(), "end": cursor.isoformat()})
            open_start = None
        cursor = segment_end
        if len(windows) >= 12:
            break
    if open_start is not None and end - open_start >= minimum and len(windows) < 12:
        windows.append({"start": open_start.isoformat(), "end": end.isoformat()})
    return windows


def _run_search(user, org_tz, attendee_tokens, ctx, now):
    """Shared scheduling engine for both Schedule and Ask.

    Attendee chips are authoritative — the LLM never adds or removes people.
    Returns (result_dict, people, plan).
    """
    resolved = _require_valid_attendees(user, attendee_tokens)
    unresolved = []

    plan = intents.resolve_plan(ctx, org_tz, now)

    people, unknown, statuses = _gather_people(
        user, org_tz, resolved, plan.window_start, plan.window_end)

    tz = ZoneInfo(org_tz)
    pref_min = None
    if plan.preferred_start:
        ps = plan.preferred_start.astimezone(tz)
        pref_min = ps.hour * 60 + ps.minute

    slots = scheduler.find_slots(
        people, plan.window_start, plan.window_end,
        plan.duration_minutes, plan.location_type, SETTINGS, org_tz,
        day_lo_min=plan.day_lo_min, day_hi_min=plan.day_hi_min,
        preferred_start_min=pref_min, enforce_waking=not plan.relax_hours,
        top_k=6,
    )

    # was the *specific* requested time available? (only meaningful for exact/preferred)
    requested_available = None
    proposal = slots[0] if slots else None
    target = plan.exact_start or plan.preferred_start
    if target is not None:
        target = target.astimezone(tz)
        t_end = target + timedelta(minutes=plan.duration_minutes)
        free = scheduler.slot_free(
            people, target, t_end, plan.location_type, SETTINGS,
            enforce_waking=not plan.relax_hours,
        )
        requested_available = free
        requested_blockers = (_requested_blockers(
            people, target, t_end, plan.location_type) if not free else [])
        if free:
            proposal = scheduler.ScoredSlot(
                start=target, end=t_end, score=100,
                reasons=[("good", "the time you asked for")],
            )

    alternatives = [s for s in slots if not (proposal and s.start == proposal.start)]

    new_ctx = dict(ctx)
    new_ctx.pop("_relative_shift", None)
    new_ctx["title"] = plan.title
    new_ctx["duration_minutes"] = plan.duration_minutes
    new_ctx["location_type"] = plan.location_type
    new_ctx["current_proposal"] = (
        {"start": proposal.start.isoformat(), "end": proposal.end.isoformat(),
         "title": plan.title, "location_type": plan.location_type}
        if proposal else None
    )

    if target is None:
        requested_blockers = []
    window_description = _window_description(plan, org_tz)
    new_ctx["last_search"] = {
        "window": window_description,
        "requested_blockers": requested_blockers,
    }

    result = {
        "org_timezone": org_tz,
        "requested_time_available": requested_available,
        "proposal": _slot_dict(proposal, title=plan.title,
                               location_type=plan.location_type) if proposal else None,
        "alternatives": [_slot_dict(s) for s in alternatives[:5]],
        "participants": [
            {"name": p.name, "email": p.email, "timezone": p.tz,
             "calendar_status": statuses.get(p.email.lower(), "freebusy"),
             "organizer": i == 0}
            for i, p in enumerate(people)
        ],
        "unresolved": unresolved,
        "calendar_unknown": unknown,
        "requested_blockers": requested_blockers,
        "window_description": window_description,
        "intent": {"title": plan.title, "duration_minutes": plan.duration_minutes,
                   "location_type": plan.location_type, "purpose": plan.purpose},
        "context": new_ctx,
    }
    return result, people, plan


def _fmt(dt_iso, org_tz, fmt):
    return datetime.fromisoformat(dt_iso).astimezone(ZoneInfo(org_tz)).strftime(fmt)


def _availability_summary(result, people, plan, org_tz):
    """Ground-truth facts for the LLM to phrase — never leaves 'unknown' as 'free'."""
    lines = []
    tz = ZoneInfo(org_tz)

    # If the user asked about a SPECIFIC time, answer that time first, per person.
    target = plan.exact_start or plan.preferred_start
    if target is not None:
        t_start = target.astimezone(tz)
        t_end = t_start + timedelta(minutes=plan.duration_minutes)
        when = t_start.strftime("%A %b %-d, %-I:%M %p")
        request_label = "exact time" if plan.exact_start else "requested time"
        verdict = (f"that {request_label} works for everyone I can check"
                   if result.get("requested_time_available")
                   else f"that {request_label} does NOT work")
        lines.append(f"The user asked specifically about {when} — {verdict}.")
        for p in people:
            if p.email in result["calendar_unknown"]:
                lines.append(f"- {p.name}: calendar NOT visible at that time — can still invite")
            else:
                busy = any(bs < t_end and be > t_start for bs, be in p.busy)
                lines.append(f"- {p.name}: {'BUSY' if busy else 'free'} at {when}")
        lines.append("")  # blank line before the recommendation

    prop = result.get("proposal")
    if prop:
        start = datetime.fromisoformat(prop["start"])
        end = datetime.fromisoformat(prop["end"])
        when = _fmt(prop["start"], org_tz, "%A %b %-d, %-I:%M %p")
        label = ("Best alternative that works" if result.get("requested_time_available") is False
                 else "Recommended time")
        lines.append(f"{label}: {when} for {plan.duration_minutes} min "
                     f"({plan.location_type}).")
        for p in people:
            if p.email in result["calendar_unknown"]:
                lines.append(f"- {p.name}: calendar NOT visible — cannot confirm, "
                             "can still invite")
            else:
                busy = any(bs < end and be > start for bs, be in p.busy)
                lines.append(f"- {p.name}: {'busy' if busy else 'free'}")
    else:
        lines.append("No time within everyone's waking hours works for the requested "
                     "window.")
        for p in people:
            if p.email in result["calendar_unknown"]:
                lines.append(f"- {p.name}: calendar NOT visible")

    if result.get("requested_time_available") is False:
        lines.append("Note: the requested time is not free — the time above is the "
                     "best ranked alternative.")
    if result["alternatives"]:
        alts = ", ".join(_fmt(a["start"], org_tz, "%a %-I:%M %p")
                         for a in result["alternatives"][:3])
        lines.append(f"Other options: {alts}.")
    if result["unresolved"]:
        lines.append("Couldn't identify: " + ", ".join(result["unresolved"]))
    return "\n".join(lines)


def _availability_answer(result, plan, org_tz):
    """Phrase the computed result without letting generated prose contradict it."""
    proposal = result.get("proposal")
    unknown = result.get("calendar_unknown") or []
    unknown_note = ""
    if unknown:
        unknown_note = (" I couldn't check " + ", ".join(unknown) +
                        ", so their availability isn't confirmed.")

    target = plan.exact_start or plan.preferred_start
    if target is not None:
        requested = target.astimezone(ZoneInfo(org_tz)).strftime("%A at %-I:%M %p")
        if result.get("requested_time_available"):
            return f"{requested} works for everyone I could check.{unknown_note}"
        blockers = result.get("requested_blockers") or []
        reason = f" because {'; '.join(blockers)}" if blockers else ""
        if proposal:
            best = _fmt(proposal["start"], org_tz, "%A at %-I:%M %p")
            return (f"{requested} doesn't work{reason}. "
                    f"The best available option is {best}.{unknown_note}")
        return f"{requested} doesn't work{reason}.{unknown_note}"

    if proposal:
        best = _fmt(proposal["start"], org_tz, "%A at %-I:%M %p")
        return f"The best available option is {best}.{unknown_note}"
    window = result.get("window_description") or "that window"
    return (f"I couldn't find a {plan.duration_minutes}-minute time that works in "
            f"{window}.{unknown_note}")


def _do_book(user, org_tz, *, title, location_type, start_iso, end_iso, attendee_emails):
    location = "Virtual" if location_type == "virtual" else "In person"
    event = gcal.insert_event(
        user["id"], summary=title or "Meeting", description=None, location=location,
        start_iso=start_iso, end_iso=end_iso, timezone_name=org_tz,
        attendee_emails=attendee_emails,
    )
    with db_session() as db:
        db.add(BookedMeeting(
            organizer_id=user["id"], google_event_id=event.get("id"),
            title=title or "Meeting", description=None, location=location,
            location_type=location_type, attendees=",".join(attendee_emails),
            start_time=datetime.fromisoformat(start_iso),
            end_time=datetime.fromisoformat(end_iso),
        ))
    return event


@app.post("/api/schedule")
def api_schedule(req: ScheduleRequest, user=Depends(current_user)):
    """Non-AI fallback: title + chips + duration + location, next 7 days."""
    org_tz = user.get("timezone") or config.DEFAULT_TIMEZONE
    now = datetime.now(ZoneInfo(org_tz))
    if not any((t or "").strip() for t in (req.attendees or [])):
        raise HTTPException(status_code=400, detail="Add at least one person to invite.")
    ctx = {"title": req.title, "duration_minutes": req.duration_minutes,
           "location_type": req.location_type}
    result, _, _ = _run_search(user, org_tz, req.attendees, ctx, now)
    return result


@app.post("/api/smart-schedule")
def api_smart_schedule(req: SmartScheduleRequest, user=Depends(current_user)):
    """People-first scheduling. `attendees` (chips) are authoritative; the optional
    free-text `text` sets only the scheduling intent (never the attendee list)."""
    org_tz = user.get("timezone") or config.DEFAULT_TIMEZONE
    now = datetime.now(ZoneInfo(org_tz))
    if not any((t or "").strip() for t in (req.attendees or [])):
        raise HTTPException(status_code=400, detail="Add at least one person to invite.")

    ctx = {}
    text = (req.text or "").strip()
    if text and llm.ai_enabled():
        intent = llm.interpret_scheduling_intent(text, org_tz)
        ctx = intents.merge_intent({}, intent)

    if not _has_when(ctx):
        raise HTTPException(
            status_code=422,
            detail="Add when you'd like to meet, such as ‘tomorrow afternoon’ or ‘Friday at 2’."
        )

    # explicit controls fill in only what the text didn't specify
    if not ctx.get("duration_minutes") and req.duration_minutes:
        ctx["duration_minutes"] = req.duration_minutes
    if not ctx.get("location_type") and req.location_type:
        ctx["location_type"] = req.location_type

    result, _, _ = _run_search(user, org_tz, req.attendees, ctx, now)
    return result


@app.post("/api/ask")
def api_ask(req: AskRequest, user=Depends(current_user)):
    """Conversational scheduling. Chips are the attendees; the text carries intent
    and refinements ("anything earlier?", "make it 30 min", "schedule it")."""
    if not llm.ai_enabled():
        raise HTTPException(status_code=503, detail="AI isn't configured. Set OPENAI_API_KEY.")
    org_tz = user.get("timezone") or config.DEFAULT_TIMEZONE
    now = datetime.now(ZoneInfo(org_tz))

    prior = dict(req.context or {})
    has_prop = bool(prior.get("current_proposal"))
    deterministic_windows = _windows_followup_intent(req.text, prior)
    deterministic_purpose = _purpose_followup_intent(req.text, prior)
    if deterministic_windows:
        intent = deterministic_windows
    elif deterministic_purpose:
        intent = deterministic_purpose
    else:
        intent = llm.interpret_scheduling_intent(
            req.text, org_tz, has_proposal=has_prop, history=req.history)
    if not intent:
        raise HTTPException(status_code=422, detail="Couldn't understand that. Try rephrasing.")

    ctx = intents.merge_intent(prior, intent)
    if intent.get("_clear_time"):
        for field in ("time_kind", "time_start_local", "time_end_local"):
            ctx.pop(field, None)
    if intent.get("_clear_duration"):
        ctx.pop("duration_minutes", None)
    if intent.get("_clear_title"):
        ctx.pop("title", None)
    action = intent.get("action", "search")

    if action == "explain":
        last = prior.get("last_search") or {}
        if not last:
            return {"action": "explain", "answer": "I don't have a previous search to explain yet.",
                    "context": prior}
        answer = f"I searched {last.get('window', 'the requested window')}."
        blockers = last.get("requested_blockers") or []
        if blockers:
            answer += " The requested time was skipped because " + "; ".join(blockers) + "."
        else:
            answer += " The displayed proposal was the highest-ranked available time in that window."
        return {"action": "explain", "answer": answer, "context": prior}

    # --- confirm & book the current proposal ---
    if action == "book":
        prop = ctx.get("current_proposal")
        if not prop:
            return {"action": "search", "answer": "There's no time on the table yet — "
                    "tell me what to find first.", "context": ctx}
        resolved = _require_valid_attendees(user, req.attendees)
        try:
            event = _do_book(
                user, org_tz,
                title=prop.get("title") or ctx.get("title") or "Meeting",
                location_type=prop.get("location_type") or "virtual",
                start_iso=prop["start"], end_iso=prop["end"], attendee_emails=resolved,
            )
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"could not create calendar event: {e}")
        when = _fmt(prop["start"], org_tz, "%A %b %-d, %-I:%M %p")
        return {
            "action": "booked", "booked": True,
            "answer": f"Done — invite sent for {when}.",
            "event_id": event.get("id"), "html_link": event.get("htmlLink"),
            "context": ctx,
        }

    # --- search / refine / list shared availability windows ---
    if not _has_when(ctx):
        raise HTTPException(
            status_code=422,
            detail="Ask about a date or time, such as ‘Are they free tomorrow afternoon?’"
        )

    result, people, plan = _run_search(user, org_tz, req.attendees, ctx, now)
    if action == "windows":
        shared = _wants_shared_availability(req.text)
        if shared:
            window_people = [p for p in people if p.email not in result["calendar_unknown"]]
        else:
            # "their availability" means the attendee chips, not the organizer.
            window_people = [p for p in people[1:] if p.email not in result["calendar_unknown"]]
        windows = _availability_windows(window_people, plan, org_tz) if window_people else []
        result["action"] = "windows"
        result["windows"] = windows
        result["availability_scope"] = "shared" if shared else "attendees"
        result["proposal"] = None
        result["alternatives"] = []
        result["context"]["current_proposal"] = None
        if not window_people:
            result["answer"] = "I couldn't check the requested attendee calendars."
        elif windows and shared:
            result["answer"] = "Here are the shared free windows I found."
        elif windows:
            result["answer"] = "Here are the attendees' free windows I found."
        elif shared:
            result["answer"] = "I couldn't find a shared free window in that period."
        else:
            result["answer"] = "I couldn't find an attendee free window in that period."
        if result.get("calendar_unknown"):
            result["answer"] += " This only reflects the calendars I could check."
        return result
    result["action"] = "search"
    result["answer"] = _availability_answer(result, plan, org_tz)
    return result


@app.post("/api/book")
def api_book(req: BookRequest, user=Depends(current_user)):
    org_tz = user.get("timezone") or config.DEFAULT_TIMEZONE
    attendee_emails, unresolved, bad = _resolve_attendees(user, req.attendees)
    if bad:
        raise HTTPException(status_code=400, detail="Not a valid email: " + ", ".join(bad))
    if unresolved:
        raise HTTPException(status_code=400, detail="Couldn't identify: " + ", ".join(unresolved))
    if req.end <= req.start:
        raise HTTPException(status_code=400, detail="Meeting end must be after its start.")

    try:
        event = gcal.insert_event(
            user["id"],
            summary=req.title or "Meeting",
            description=req.description,
            location=req.location,
            start_iso=req.start.isoformat(),
            end_iso=req.end.isoformat(),
            timezone_name=org_tz,
            attendee_emails=attendee_emails,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"could not create calendar event: {e}")

    with db_session() as db:
        m = BookedMeeting(
            organizer_id=user["id"],
            google_event_id=event.get("id"),
            title=req.title or "Meeting",
            description=req.description,
            location=req.location,
            location_type=req.location_type,
            attendees=",".join(attendee_emails),
            start_time=req.start,
            end_time=req.end,
        )
        db.add(m)

    return {"ok": True, "event_id": event.get("id"), "html_link": event.get("htmlLink")}


@app.get("/api/meetings")
def api_meetings(user=Depends(current_user)):
    with db_session() as db:
        rows = (db.query(BookedMeeting)
                .filter(BookedMeeting.organizer_id == user["id"])
                .order_by(BookedMeeting.start_time.desc()).all())
        return [m.to_dict() for m in rows]


# ---------- static frontend (must be mounted last) ----------

if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
