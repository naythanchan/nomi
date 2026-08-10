"""Natural-language understanding for Nomi, via the OpenAI API.

The division of labour is deliberate: **the LLM understands what the user wants;
Python decides whether and when it can actually happen.**

  1. interpret_scheduling_intent() — turn "grab lunch in person this wed at 5pm"
     into a *semantic* intent (purpose, calendar date, local clock time, what
     kind of time constraint). It never manufactures timezone offsets, ISO
     datetimes, or availability, and it never touches the attendee list — chips
     are authoritative. Python (intent.py) resolves the rest deterministically.

  2. answer_question() — a warm, concise chat reply, grounded strictly in the
     real availability facts we compute and hand to the model.

Uses the OpenAI SDK. Model is configurable via OPENAI_MODEL (default: a low-cost
small model). If the SDK isn't installed or OPENAI_API_KEY isn't set,
ai_enabled() is False and callers fall back to the plain (non-AI) flow.
"""
from __future__ import annotations

import os
import json
from datetime import datetime
from zoneinfo import ZoneInfo

try:
    from openai import OpenAI
except ImportError:  # SDK optional until the AI features are used
    OpenAI = None

# Low-cost nano-tier default; override with OPENAI_MODEL in .env.
MODEL = os.environ.get("OPENAI_MODEL", "gpt-4.1-nano")
_client = None


def ai_enabled() -> bool:
    return OpenAI is not None and bool(os.environ.get("OPENAI_API_KEY"))


def _get_client():
    global _client
    if _client is None:
        _client = OpenAI()  # reads OPENAI_API_KEY from env
    return _client


def _chat_json(system: str, user: str) -> dict | None:
    """Call the model in JSON mode and return the parsed object, or None."""
    try:
        resp = _get_client().chat.completions.create(
            model=MODEL,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}],
            response_format={"type": "json_object"},
        )
        return json.loads(resp.choices[0].message.content)
    except Exception as e:
        print(f"[llm] json call failed: {e}")
        return None


def _chat_text(system: str, user: str, max_tokens: int = 300) -> str | None:
    try:
        resp = _get_client().chat.completions.create(
            model=MODEL,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}],
            max_tokens=max_tokens,
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception as e:
        print(f"[llm] text call failed: {e}")
        return None


# ---------- 1. interpret scheduling intent (Schedule + Ask share this) ----------

def interpret_scheduling_intent(text: str, org_tz: str,
                                has_proposal: bool = False) -> dict | None:
    """Extract semantic scheduling intent from one natural-language utterance.

    Returns a dict (never attendees). It CLASSIFIES the day reference — it does
    NOT compute calendar dates; Python does that deterministically. Keys:
      action            "search" | "book" | "cancel"
      purpose           str ("" if none)
      weekday           "monday".."sunday" | null   (a named weekday)
      day_offset        int | null                  (0=today, 1=tomorrow, 2=...)
      next_week         bool                         ("next tuesday" -> true)
      explicit_date     "YYYY-MM-DD" | null          (only a literally stated date)
      span_days         int | null                   ("this week" -> 7)
      time_kind         "exact"|"preferred"|"after"|"before"|"between"|"daypart"|"any"
      time_start_local  "HH:MM" | null
      time_end_local    "HH:MM" | null
      duration_minutes  int | null
      location_type     "virtual" | "in-person" | null
      relative_shift    "earlier" | "later" | null
      relax_hours       bool
    """
    if not ai_enabled() or not (text or "").strip():
        return None
    now = datetime.now(ZoneInfo(org_tz))
    today = now.strftime("%A")
    prop_line = ("There is a proposed time already on the table. If the user is simply "
                 "confirming it (\"that works\", \"schedule it\", \"book it\", \"do it\", "
                 "\"yes go ahead\") with no NEW time, set action to \"book\".\n"
                 if has_proposal else "")
    system = (
        "You extract the scheduling INTENT from a message. You do NOT decide "
        "availability, you do NOT list attendees (the app already knows who's "
        "invited), and you do NOT compute calendar dates — you only CLASSIFY the "
        "day the user referred to; code turns that into a real date. Return ONLY JSON.\n"
        f"Today is {today}. The user's timezone is {org_tz}.\n"
        + prop_line +
        "Keys (use null when not mentioned):\n"
        '  "action": "search" | "book" | "cancel"  (default "search")\n'
        '  "purpose": short noun like "lunch", "dinner", "sync" ("" if none)\n'
        '  "weekday": the named day lowercased ("this wed"->"wednesday", '
        '"friday"->"friday"), else null\n'
        '  "day_offset": 0 for "today", 1 for "tomorrow", 2 for "day after tomorrow"; '
        'else null. Use EITHER weekday OR day_offset, not both.\n'
        '  "next_week": true only if the user said "next" (e.g. "next tuesday"), else false\n'
        '  "explicit_date": "YYYY-MM-DD" only if the user stated an actual date '
        '("Aug 20", "the 20th"), else null\n'
        '  "span_days": integer for a range ("this week"->7, "next few days"->3), else null\n'
        '  "time_kind": one of:\n'
        '      "exact"     a strict time ("exactly 5", "at 5 sharp")\n'
        '      "preferred" a specific but flexible time ("at 5", "around 5", "5ish")\n'
        '      "after"     "after 7" -> start no earlier than 19:00\n'
        '      "before"    "before noon" -> end no later than 12:00\n'
        '      "between"   "between 2 and 4" -> hard window\n'
        '      "daypart"   "morning/afternoon/evening/lunchtime" (no clock time)\n'
        '      "any"       no time mentioned\n'
        '  "time_start_local": "HH:MM" 24h (the time, or lower bound), else null\n'
        '  "time_end_local": "HH:MM" 24h (upper bound for before/between/daypart), else null\n'
        '  "duration_minutes": integer if stated ("45 min", "an hour"), else null\n'
        '  "location_type": "in-person" only if clearly implied (grab/meet/lunch out), '
        'else "virtual" if implied (call/zoom/virtual), else null\n'
        '  "relative_shift": "earlier" or "later" for "anything earlier?"/"later?", else null\n'
        '  "relax_hours": true only if the user explicitly wants an odd hour '
        '(e.g. "6am", "late night is fine"), else false\n'
        "For dayparts use morning 09:00-12:00, afternoon 12:00-17:00, evening 17:00-21:00."
    )
    data = _chat_json(system, text.strip())
    if not isinstance(data, dict):
        return None
    data.setdefault("action", "search")
    return data


# ---------- 2. answer an availability question ----------

def answer_question(question: str, availability_summary: str) -> str | None:
    """Compose a short, friendly reply grounded in the availability we computed."""
    if not ai_enabled():
        return None
    system = (
        "You are Nomi, a calm, concise scheduling assistant. "
        "Answer using ONLY the availability facts provided — never invent free/busy "
        "status. Crucial: if a calendar could NOT be checked, do NOT say that person "
        "is free; say you couldn't check them and can still invite them. Prefer "
        "phrasing like \"everyone I could check is free\" over \"everyone is free\" "
        "whenever any calendar is unknown. No invitation has been sent yet: never say "
        "someone is invited, only that the organizer can still invite them. Do not "
        "address an unknown attendee as \"you\" or say \"I can invite\"; tell the organizer "
        "\"you can still invite them\". Use complete sentences and standard capitalization. "
        "Be brief and warm: say plainly whether it "
        "works, name who's busy or unknown, and if it doesn't work suggest an "
        "alternative. Two or three sentences at most."
    )
    return _chat_text(
        system,
        f"Question: {question}\n\nAvailability facts:\n{availability_summary}",
    )
