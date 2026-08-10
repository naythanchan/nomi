"""Google OAuth, session management, and credential handling with auto-refresh."""
import json
import secrets
from datetime import datetime, timezone, timedelta

from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleRequest
from googleapiclient.discovery import build

import config
from db import db_session, User, Session


# ---------- client secret helpers ----------

def _client_conf():
    if config.GOOGLE_CLIENT_SECRET_JSON:
        return json.loads(config.GOOGLE_CLIENT_SECRET_JSON)
    with open(config.CLIENT_SECRET_FILE) as f:
        return json.load(f)


def _client_id_secret():
    data = _client_conf()
    node = data.get("web") or data.get("installed") or {}
    return node.get("client_id"), node.get("client_secret")


def _flow(state=None):
    return Flow.from_client_config(
        _client_conf(),
        scopes=config.GOOGLE_SCOPES,
        redirect_uri=config.REDIRECT_URI,
        state=state,
    )


# ---------- OAuth flow ----------

def authorization_url():
    flow = _flow()
    url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )
    return url, state


def handle_callback(authorization_response_url: str, expected_state: str):
    """Exchange the code, upsert the user + tokens, and return a new session token."""
    flow = _flow(state=expected_state)
    flow.fetch_token(authorization_response=authorization_response_url)
    creds = flow.credentials

    info = build("oauth2", "v2", credentials=creds).userinfo().get().execute()
    email = info["email"]
    name = info.get("name", "")

    tz = _fetch_primary_timezone(creds)

    with db_session() as db:
        user = db.query(User).filter(User.email == email).first()
        if not user:
            user = User(email=email, name=name)
            db.add(user)
        user.name = name or user.name
        if tz:
            user.timezone = tz
        user.google_access_token = creds.token
        # Google only returns a refresh token on first consent; keep the old one otherwise.
        if creds.refresh_token:
            user.google_refresh_token = creds.refresh_token
        user.token_expiry = creds.expiry
        db.flush()

        token = secrets.token_urlsafe(32)
        db.add(Session(
            token=token,
            user_id=user.id,
            expires_at=datetime.now(timezone.utc) + timedelta(days=config.SESSION_DAYS),
        ))
        return token


def _fetch_primary_timezone(creds):
    try:
        cal = build("calendar", "v3", credentials=creds)
        setting = cal.settings().get(setting="timezone").execute()
        return setting.get("value")
    except Exception:
        return None


# ---------- sessions ----------

def get_current_user(token: str | None):
    """Return a lightweight dict for the logged-in user, or None."""
    if not token:
        return None
    with db_session() as db:
        sess = db.query(Session).filter(Session.token == token).first()
        if not sess:
            return None
        expires = sess.expires_at
        if expires is not None:
            # SQLite stores datetimes without tz; treat stored value as UTC.
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
            if expires < datetime.now(timezone.utc):
                db.delete(sess)
                return None
        user = sess.user
        if not user:
            return None
        return {"id": user.id, "email": user.email, "name": user.name, "timezone": user.timezone}


def logout(token: str | None):
    if not token:
        return
    with db_session() as db:
        sess = db.query(Session).filter(Session.token == token).first()
        if sess:
            db.delete(sess)


# ---------- credentials with auto-refresh ----------

def credentials_for_user(user_id: int):
    """Build Google Credentials for a user, refreshing + persisting if expired."""
    client_id, client_secret = _client_id_secret()
    with db_session() as db:
        user = db.query(User).filter(User.id == user_id).first()
        if not user or not user.google_access_token:
            return None
        creds = Credentials(
            token=user.google_access_token,
            refresh_token=user.google_refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=client_id,
            client_secret=client_secret,
            scopes=config.GOOGLE_SCOPES,
        )
        if not creds.valid and creds.refresh_token:
            try:
                creds.refresh(GoogleRequest())
                user.google_access_token = creds.token
                user.token_expiry = creds.expiry
            except Exception:
                return None
        return creds


def calendar_service(user_id: int):
    creds = credentials_for_user(user_id)
    return build("calendar", "v3", credentials=creds) if creds else None


def people_service(user_id: int):
    creds = credentials_for_user(user_id)
    return build("people", "v1", credentials=creds) if creds else None
