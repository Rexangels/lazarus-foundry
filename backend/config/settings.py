"""Django settings for Lazarus Foundry.

All environment-specific values come from the repository-root ``.env`` file
(one level above ``backend/``) or from real environment variables, which take
precedence over ``.env``. See ``.env.example`` for the full list.
"""
from pathlib import Path

import environ
from django.core.exceptions import ImproperlyConfigured

from config.paths import BACKEND_DIR, REPO_ROOT, resolve_repo_path

BASE_DIR: Path = BACKEND_DIR

env = environ.Env()
# overwrite=False (the default): variables already set in the process win over .env.
environ.Env.read_env(REPO_ROOT / ".env")

# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------
SECRET_KEY: str = env.str("SECRET_KEY")
DEBUG: bool = env.bool("DEBUG", default=False)
ALLOWED_HOSTS: list[str] = env.list("ALLOWED_HOSTS", default=["localhost", "127.0.0.1", "[::1]"])

# ---------------------------------------------------------------------------
# Applications
# ---------------------------------------------------------------------------
DJANGO_APPS: list[str] = [
    # daphne must precede django.contrib.staticfiles so `runserver` serves ASGI.
    "daphne",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
]

THIRD_PARTY_APPS: list[str] = [
    "channels",
    "ninja",
]

# The seven Lazarus Foundry apps: five intelligence systems plus the two
# shared infrastructure apps (projects, telemetry).
LOCAL_APPS: list[str] = [
    "apps.projects",
    "apps.ingestion",
    "apps.genome",
    "apps.revival",
    "apps.foundry",
    "apps.rebirth",
    "apps.telemetry",
]

INSTALLED_APPS: list[str] = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

MIDDLEWARE: list[str] = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF: str = "config.urls"

TEMPLATES: list[dict[str, object]] = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION: str = "config.wsgi.application"
ASGI_APPLICATION: str = "config.asgi.application"

# ---------------------------------------------------------------------------
# Database: PostgreSQL 16 + pgvector (docker-compose `db` service).
# No SQLite fallback: DATABASE_URL is required.
# ---------------------------------------------------------------------------
DATABASES: dict[str, dict[str, object]] = {
    "default": env.db_url("DATABASE_URL"),
}
# Fail fast instead of hanging when PostgreSQL is down (on Windows, a closed
# local port can leave libpq waiting indefinitely). A `?connect_timeout=` in
# DATABASE_URL takes precedence.
DB_CONNECT_TIMEOUT_SECONDS: int = 5
DATABASES["default"].setdefault("OPTIONS", {})
DATABASES["default"]["OPTIONS"].setdefault("connect_timeout", DB_CONNECT_TIMEOUT_SECONDS)  # type: ignore[union-attr]

# ---------------------------------------------------------------------------
# Redis: Celery broker/result backend and Channels layer
# ---------------------------------------------------------------------------
REDIS_URL: str = env.str("REDIS_URL")

CELERY_BROKER_URL: str = REDIS_URL
CELERY_RESULT_BACKEND: str = REDIS_URL
CELERY_ACCEPT_CONTENT: list[str] = ["json"]
CELERY_TASK_SERIALIZER: str = "json"
CELERY_RESULT_SERIALIZER: str = "json"
CELERY_TASK_TRACK_STARTED: bool = True
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP: bool = True

CHANNEL_LAYERS: dict[str, dict[str, object]] = {
    "default": {
        "BACKEND": "channels_redis.core.RedisChannelLayer",
        "CONFIG": {"hosts": [REDIS_URL]},
    },
}

# ---------------------------------------------------------------------------
# Reasoning model (OpenAI-compatible endpoint; current provider: Meta Model API)
# An empty MODEL_API_KEY makes agents use their deterministic offline fallback.
# ---------------------------------------------------------------------------
MODEL_API_KEY: str = env.str("MODEL_API_KEY", default="")
MODEL_BASE_URL: str = env.str("MODEL_BASE_URL", default="https://api.meta.ai/v1")
MODEL_ID: str = env.str("MODEL_ID", default="muse-spark-1.3")

# ---------------------------------------------------------------------------
# Sandbox execution
# ---------------------------------------------------------------------------
SANDBOX_MODES: tuple[str, ...] = ("subprocess", "docker")
SANDBOX_MODE: str = env.str("SANDBOX_MODE", default="subprocess")
if SANDBOX_MODE not in SANDBOX_MODES:
    raise ImproperlyConfigured(
        f"SANDBOX_MODE must be one of {SANDBOX_MODES}, got {SANDBOX_MODE!r}."
    )
SANDBOX_PREVIEW_PORT: int = env.int("SANDBOX_PREVIEW_PORT", default=8010)

# Relative values are anchored at the repository root, never the process cwd.
WORKSPACE_ROOT: Path = resolve_repo_path(env.str("WORKSPACE_ROOT", default="./.lazarus_data/workspaces"))
FIXTURE_ROOT: Path = resolve_repo_path(env.str("FIXTURE_ROOT", default="./.lazarus_data/fixtures"))

# ---------------------------------------------------------------------------
# Auth, i18n, static files
# ---------------------------------------------------------------------------
AUTH_PASSWORD_VALIDATORS: list[dict[str, str]] = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE: str = "en-us"
TIME_ZONE: str = "UTC"
USE_I18N: bool = True
USE_TZ: bool = True

CELERY_TIMEZONE: str = TIME_ZONE

STATIC_URL: str = "static/"

DEFAULT_AUTO_FIELD: str = "django.db.models.BigAutoField"
