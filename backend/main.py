"""Nomi — FastAPI app.

Serves the frontend, handles Google sign-in, and exposes the scheduling API.
Booking creates a real Google Calendar event and invites everyone immediately.
"""
import re
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
    url, _state = auth.authorization_url()
    return RedirectResponse(url)


@app.get("/auth/callback")
def callback(request: Request):
    try:
        token = auth.handle_callback(str(request.url))
    except Exception as e:
        return JSONResponse({"error": f"sign-in failed: {e}"}, status_code=400)
    resp = RedirectResponse(config.BASE_URL + "/")
    resp.set_cookie(
        config.SESSION_COOKIE, token,
        httponly=True, samesite="lax",
        secure=config.BASE_URL.startswith("https://"),
        max_age=config.SESSION_DAYS * 86400, path="/",
    )
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


def _run_search(user, org_tz, attendee_tokens, ctx, now):
    """Shared scheduling engine for both Schedule and Ask.

    Attendee chips are authoritative — the LLM never adds or removes people.
    Returns (result_dict, people, plan).
    """
    resolved, unresolved, bad = _resolve_attendees(user, attendee_tokens)
    if bad:
        raise HTTPException(status_code=400, detail="Not a valid email: " + ", ".join(bad))

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

    result = {
        "org_timezone": org_tz,
        "requested_time_available": requested_available,
        "proposal": _slot_dict(proposal, title=plan.title,
                               location_type=plan.location_type) if proposal else None,
        "alternatives": [_slot_dict(s) for s in alternatives[:5]],
        "participants": [
            {"name": p.name, "email": p.email, "timezone": p.tz,
             "calendar_status": statuses.get(p.email.lower(), "freebusy")}
            for p in people
        ],
        "unresolved": unresolved,
        "calendar_unknown": unknown,
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
        verdict = ("that exact time works for everyone I can check"
                   if result.get("requested_time_available")
                   else "that exact time does NOT work")
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
        label = ("Closest alternative that works" if result.get("requested_time_available") is False
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
        lines.append("Note: the EXACT time requested is not free — the time above is the "
                     "closest alternative.")
    if result["alternatives"]:
        alts = ", ".join(_fmt(a["start"], org_tz, "%a %-I:%M %p")
                         for a in result["alternatives"][:3])
        lines.append(f"Other options: {alts}.")
    if result["unresolved"]:
        lines.append("Couldn't identify: " + ", ".join(result["unresolved"]))
    return "\n".join(lines)


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
    intent = llm.interpret_scheduling_intent(req.text, org_tz, has_proposal=has_prop)
    if not intent:
        raise HTTPException(status_code=422, detail="Couldn't understand that. Try rephrasing.")

    ctx = intents.merge_intent(prior, intent)
    action = intent.get("action", "search")

    # --- confirm & book the current proposal ---
    if action == "book":
        prop = ctx.get("current_proposal")
        if not prop:
            return {"action": "search", "answer": "There's no time on the table yet — "
                    "tell me what to find first.", "context": ctx}
        resolved, unresolved, bad = _resolve_attendees(user, req.attendees)
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

    # --- search / refine ---
    result, people, plan = _run_search(user, org_tz, req.attendees, ctx, now)
    summary = _availability_summary(result, people, plan, org_tz)
    reply = llm.answer_question(req.text, summary)
    result["action"] = "search"
    result["answer"] = reply or "Here's the best I found."
    return result


@app.post("/api/book")
def api_book(req: BookRequest, user=Depends(current_user)):
    org_tz = user.get("timezone") or config.DEFAULT_TIMEZONE
    attendee_emails = [e for e in req.attendees if e and e.lower() != user["email"].lower()]

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
