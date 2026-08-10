# Nomi

A calm meeting scheduler. Describe a meeting, and Nomi — a little clock pet —
finds the time that works for everyone, then books it and sends real Google
Calendar invites.

- **Frontend:** a single-screen "clock pet" (vanilla HTML/CSS/JS in `frontend/`).
- **Backend:** FastAPI + SQLAlchemy (SQLite by default) in `backend/`.
- **Scheduling:** a scoring engine that respects work hours, lunch, focus time,
  **travel buffers** for in-person meetings, and **timezone kindness** for virtual ones.
- **Invites:** confirming books the event on your Google Calendar with
  `sendUpdates="all"`, so everyone gets a real invite. No sign-up wall.

## Setup

1. **Google OAuth client** — in Google Cloud Console, create an OAuth 2.0 **Web**
   client. Add `http://localhost:8080/auth/callback` as an authorized redirect URI.
   Enable the **Google Calendar API** and **People API**. Download the JSON and
   save it as `backend/client_secret.json`.

2. **Environment** — copy and fill in:
   ```bash
   cd backend
   cp .env.example .env
   # set SECRET_KEY (python -c "import secrets; print(secrets.token_urlsafe(32))")
   ```

3. **Install & run:**
   ```bash
   cd backend
   python3 -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   uvicorn main:app --reload --port 8080
   ```

4. Open <http://localhost:8080>, tap the clock, sign in with Google, and schedule.

## Notes

- SQLite lives at `backend/nomi.db` (git-ignored). Point `DATABASE_URL` at
  Postgres to switch — no code changes needed.
- Attendee timezones are known for people who've signed into Nomi; others
  are left unknown (never guessed) for the timezone-kindness scoring.
- `client_secret.json`, `.env`, and the DB are git-ignored — keep them out of commits.
