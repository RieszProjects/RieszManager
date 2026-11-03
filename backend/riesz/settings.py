"""Django settings for the RieszManager backend.

All deployment-specific values come from the environment (normally the
project's `.env` file, which is mounted into the container and read here).
"""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlparse

from dotenv import dotenv_values

BASE_DIR = Path(__file__).resolve().parent.parent

_env_file = os.environ.get("RIESZ_ENV_FILE", "/config/.env")
ENV: dict[str, str] = {k: v for k, v in dotenv_values(_env_file, interpolate=False).items() if v is not None}
ENV.update(os.environ)


def env(name: str, default: str = "") -> str:
    return ENV.get(name, default)


def env_bool(name: str, default: bool = False) -> bool:
    value = ENV.get(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


SECRET_KEY = env("RIESZ_SECRET_KEY")
if len(SECRET_KEY) < 32:
    raise RuntimeError("RIESZ_SECRET_KEY must be set to a random string of at least 32 characters")

DEBUG = env_bool("RIESZ_DEBUG", False)

PUBLIC_ORIGIN = env("RIESZ_PUBLIC_ORIGIN").rstrip("/")
_public_host = urlparse(PUBLIC_ORIGIN).hostname if PUBLIC_ORIGIN else None
ALLOWED_HOSTS = [h.strip() for h in env("RIESZ_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h.strip()]
if _public_host:
    ALLOWED_HOSTS.append(_public_host)
CSRF_TRUSTED_ORIGINS = [PUBLIC_ORIGIN] if PUBLIC_ORIGIN else []
CSRF_TRUSTED_ORIGINS += [o.strip() for o in env("RIESZ_EXTRA_ORIGINS").split(",") if o.strip()]

HTTPS = PUBLIC_ORIGIN.startswith("https://")
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SESSION_COOKIE_SECURE = HTTPS
CSRF_COOKIE_SECURE = HTTPS
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_NAME = "riesz_session"
CSRF_COOKIE_NAME = "riesz_csrf"
SESSION_COOKIE_AGE = int(env("RIESZ_SESSION_DAYS", "30")) * 86400
SESSION_SAVE_EVERY_REQUEST = False
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"
SECURE_REFERRER_POLICY = "same-origin"

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "channels",
    "accounts",
    "bridge",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "riesz.urls"
ASGI_APPLICATION = "riesz.asgi.application"
TEMPLATES: list[dict] = []

DATA_DIR = Path(env("RIESZ_DATA_DIR", str(BASE_DIR / "data")))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": DATA_DIR / "db.sqlite3",
        "OPTIONS": {"timeout": 20, "transaction_mode": "IMMEDIATE",
                    "init_command": "PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;"},
    }
}

PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
    "django.contrib.auth.hashers.ScryptPasswordHasher",
]

CHANNEL_LAYERS = {"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
USE_TZ = True
TIME_ZONE = "UTC"
LANGUAGE_CODE = "zh-hans"

DATA_UPLOAD_MAX_MEMORY_SIZE = 8 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 8 * 1024 * 1024
MAX_UPLOAD_BYTES = int(env("RIESZ_MAX_UPLOAD_MB", "100")) * 1024 * 1024

# ---- application settings ----------------------------------------------------
RIESZ_USERNAME = env("RIESZ_USERNAME", "admin")
RIESZ_PASSWORD = env("RIESZ_PASSWORD")
RIESZ_PASSWORD_HASH = env("RIESZ_PASSWORD_HASH")
if not RIESZ_PASSWORD and not RIESZ_PASSWORD_HASH:
    raise RuntimeError("Set RIESZ_PASSWORD or RIESZ_PASSWORD_HASH")
RIESZ_REQUIRE_TOTP = env_bool("RIESZ_REQUIRE_TOTP", False)
RIESZ_TOTP_ISSUER = env("RIESZ_TOTP_ISSUER", "RieszManager")
RIESZ_TRUST_CF_HEADER = env_bool("RIESZ_TRUST_CF_HEADER", False)
LOGIN_MAX_FAILURES_PER_IP = int(env("RIESZ_LOGIN_MAX_FAILURES", "5"))
LOGIN_MAX_FAILURES_GLOBAL = int(env("RIESZ_LOGIN_MAX_FAILURES_GLOBAL", "30"))
LOGIN_LOCKOUT_MINUTES = int(env("RIESZ_LOGIN_LOCKOUT_MINUTES", "15"))
PENDING_LOGIN_SECONDS = 300

RUNNER_SOCKET = env("RUNNER_CONTAINER_SOCKET", "/run/riesz/runner.sock")
RUNNER_TOKEN = env("RUNNER_TOKEN")

OPENAI_API_KEY = env("OPENAI_API_KEY")
OPENAI_BASE_URL = env("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
TRANSCRIBE_MODEL = env("RIESZ_TRANSCRIBE_MODEL", "gpt-4o-transcribe")
TRANSCRIBE_LANGUAGE = env("RIESZ_TRANSCRIBE_LANGUAGE")

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {"django.request": {"level": "WARNING"}},
}
