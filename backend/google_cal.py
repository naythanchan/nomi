"""Thin wrappers over the Google Calendar + People APIs."""
import config
from auth import calendar_service, people_service


def search_directory(user_id: int, query: str, limit: int = 10):
    """Search the org directory for people by name/email."""
    svc = people_service(user_id)
    if not svc or not query:
        return []
    try:
        result = svc.people().searchDirectoryPeople(
            query=query,
            readMask="names,emailAddresses",
            sources=["DIRECTORY_SOURCE_TYPE_DOMAIN_PROFILE"],
            pageSize=limit,
        ).execute()
    except Exception as e:
        print(f"[people] directory search failed: {e}")
        return []

    out = []
    for person in result.get("people", []):
        emails = person.get("emailAddresses", [])
        names = person.get("names", [])
        if not emails:
            continue
        email = emails[0]["value"]
        if config.ORG_EMAIL_DOMAIN and not email.endswith("@" + config.ORG_EMAIL_DOMAIN):
            continue
        out.append({"name": names[0]["displayName"] if names else email, "email": email})
    return out


def resolve_email(user_id: int, token: str) -> str | None:
    """Turn a free-text token (an email, or a name to look up) into an email."""
    token = token.strip()
    if not token:
        return None
    if "@" in token:
        return token.lower()
    matches = search_directory(user_id, token, limit=1)
    return matches[0]["email"] if matches else None


def freebusy(user_id: int, emails: list[str], time_min_iso: str, time_max_iso: str):
    """Query busy blocks for a set of calendars via the organizer's credentials.

    Returns (busy_map, unseen) where:
      - busy_map = {email: [{"start": iso, "end": iso}, ...]}
      - unseen   = [emails whose calendar could not be read] (bad address,
                   not shared, or otherwise inaccessible).
    """
    svc = calendar_service(user_id)
    if not svc or not emails:
        return {}, list(emails)
    try:
        body = {
            "timeMin": time_min_iso,
            "timeMax": time_max_iso,
            "items": [{"id": e} for e in emails],
        }
        result = svc.freebusy().query(body=body).execute()
    except Exception as e:
        print(f"[freebusy] query failed: {e}")
        return {e: [] for e in emails}, list(emails)

    calendars = result.get("calendars", {})
    busy, unseen = {}, []
    for email in emails:
        cal = calendars.get(email)
        if cal is None or cal.get("errors"):
            unseen.append(email)
            busy[email] = []
        else:
            busy[email] = [{"start": b["start"], "end": b["end"]} for b in cal.get("busy", [])]
    return busy, unseen


def insert_event(user_id: int, *, summary, description, location,
                 start_iso, end_iso, timezone_name, attendee_emails):
    """Create an event on the organizer's calendar and email invites to everyone."""
    svc = calendar_service(user_id)
    if not svc:
        raise RuntimeError("no calendar access for organizer")

    body = {
        "summary": summary,
        "description": description or "",
        "location": location or "",
        "start": {"dateTime": start_iso, "timeZone": timezone_name},
        "end": {"dateTime": end_iso, "timeZone": timezone_name},
        "attendees": [{"email": e} for e in attendee_emails],
        "reminders": {"useDefault": True},
    }
    event = svc.events().insert(
        calendarId="primary",
        body=body,
        sendUpdates="all",   # <-- real Google invites go out to every attendee
    ).execute()
    return event
