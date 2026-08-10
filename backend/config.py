"""Central configuration, loaded once from the environment."""
import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

SECRET_KEY = os.environ.get("SECRET_KEY", "dev-insecure-change-me")
BASE_URL = os.environ.get("BASE_URL", "http://localhost:8080").rstrip("/")
DATABASE_URL = os.environ.get("DATABASE_URL", f"sqlite:///{BASE_DIR / 'nomi.db'}")

# Empty string => no domain restriction.
ORG_EMAIL_DOMAIN = os.environ.get("ORG_EMAIL_DOMAIN", "").strip().lstrip("@")

_secret_file = os.environ.get("GOOGLE_CLIENT_SECRET_FILE", "client_secret.json")
CLIENT_SECRET_FILE = str(BASE_DIR / _secret_file) if not os.path.isabs(_secret_file) else _secret_file

REDIRECT_URI = f"{BASE_URL}/auth/callback"
ORG_SETTINGS_FILE = str(BASE_DIR / "org_settings.json")

GOOGLE_SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/directory.readonly",
]

DEFAULT_TIMEZONE = "America/New_York"
SESSION_COOKIE = "cw_session"
SESSION_DAYS = 7

# Allow http:// redirect + scope drift during local dev only.
if BASE_URL.startswith("http://"):
    os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"
os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"
