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

# Telemetry WebSockets for project IDs not in the database are closed with 4404.
# Set to False to let clients subscribe without the lookup (see apps/telemetry/consumers.py).
TELEMETRY_REQUIRE_EXISTING_PROJECT: bool = env.bool("TELEMETRY_REQUIRE_EXISTING_PROJECT", default=True)

# ---------------------------------------------------------------------------
# Reasoning model (OpenAI-compatible endpoint; current provider: Meta Model API)
# An empty MODEL_API_KEY makes agents use their deterministic offline fallback.
# ---------------------------------------------------------------------------
MODEL_API_KEY: str = env.str("MODEL_API_KEY", default="")
MODEL_BASE_URL: str = env.str("MODEL_BASE_URL", default="https://api.meta.ai/v1")
MODEL_ID: str = env.str("MODEL_ID", default="muse-spark-1.3")
# Passed as `reasoning_effort` when set (e.g. "low", "medium", "high"); empty omits it.
MODEL_REASONING_EFFORT: str = env.str("MODEL_REASONING_EFFORT", default="").strip()
# Per-request timeout and SDK retries for reasoning-model calls (apps/genome/agents.py).
MODEL_REQUEST_TIMEOUT_SECONDS: float = env.float("MODEL_REQUEST_TIMEOUT_SECONDS", default=120.0)
MODEL_MAX_RETRIES: int = env.int("MODEL_MAX_RETRIES", default=1)
if MODEL_REQUEST_TIMEOUT_SECONDS <= 0 or MODEL_MAX_RETRIES < 0:
    raise ImproperlyConfigured("MODEL_REQUEST_TIMEOUT_SECONDS must be positive and MODEL_MAX_RETRIES >= 0.")

# ---------------------------------------------------------------------------
# Genome synthesis (apps/genome/agents.py, tasks.py)
# ---------------------------------------------------------------------------
# Most evidence-catalog items sent to the reasoning model (draft citations first).
GENOME_MAX_CATALOG_ITEMS: int = env.int("GENOME_MAX_CATALOG_ITEMS", default=400)
GENOME_TASK_SOFT_TIME_LIMIT_SECONDS: int = env.int("GENOME_TASK_SOFT_TIME_LIMIT_SECONDS", default=900)
GENOME_TASK_TIME_LIMIT_SECONDS: int = env.int("GENOME_TASK_TIME_LIMIT_SECONDS", default=960)
if GENOME_MAX_CATALOG_ITEMS <= 0:
    raise ImproperlyConfigured(f"GENOME_MAX_CATALOG_ITEMS must be positive, got {GENOME_MAX_CATALOG_ITEMS}.")
if not 0 < GENOME_TASK_SOFT_TIME_LIMIT_SECONDS < GENOME_TASK_TIME_LIMIT_SECONDS:
    raise ImproperlyConfigured(
        "GENOME_TASK_SOFT_TIME_LIMIT_SECONDS must be positive and below GENOME_TASK_TIME_LIMIT_SECONDS."
    )

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
# Ingestion: Git forensics (apps/ingestion/git_forensics.py)
# ---------------------------------------------------------------------------
# Hosts users may submit repository URLs for (https:// only, exact host match).
INGEST_ALLOWED_HOSTS: list[str] = [
    host.strip().lower()
    for host in env.list("INGEST_ALLOWED_HOSTS", default=["github.com", "gitlab.com", "bitbucket.org"])
    if host.strip()
]
# An unmerged branch is abandoned when its last commit is at least this many
# days older than the repository's newest commit (the repo's own timeline).
ABANDONED_BRANCH_MIN_AGE_DAYS: int = env.int("ABANDONED_BRANCH_MIN_AGE_DAYS", default=90)
# Per-file cap on the unified patch text kept for a branch diff.
INGEST_MAX_PATCH_BYTES: int = env.int("INGEST_MAX_PATCH_BYTES", default=65536)
INGEST_CLONE_TIMEOUT_SECONDS: int = env.int("INGEST_CLONE_TIMEOUT_SECONDS", default=600)
if ABANDONED_BRANCH_MIN_AGE_DAYS < 0:
    raise ImproperlyConfigured(
        f"ABANDONED_BRANCH_MIN_AGE_DAYS must be >= 0, got {ABANDONED_BRANCH_MIN_AGE_DAYS}."
    )
if INGEST_MAX_PATCH_BYTES <= 0 or INGEST_CLONE_TIMEOUT_SECONDS <= 0:
    raise ImproperlyConfigured("INGEST_MAX_PATCH_BYTES and INGEST_CLONE_TIMEOUT_SECONDS must be positive.")

# ---------------------------------------------------------------------------
# Ingestion: AST and document extraction (apps/ingestion/ast_parser.py)
# ---------------------------------------------------------------------------
# Files larger than this are recorded as SKIPPED_TOO_LARGE and not parsed.
INGEST_MAX_PARSE_BYTES: int = env.int("INGEST_MAX_PARSE_BYTES", default=512 * 1024)
if INGEST_MAX_PARSE_BYTES <= 0:
    raise ImproperlyConfigured(f"INGEST_MAX_PARSE_BYTES must be positive, got {INGEST_MAX_PARSE_BYTES}.")
# tree-sitter-language-pack fetches non-Python grammars from the internet on first
# use. Off: only grammars already in its local cache are used; the rest are
# GRAMMAR_UNAVAILABLE. Python always uses the bundled tree-sitter-python wheel.
INGEST_ALLOW_GRAMMAR_DOWNLOAD: bool = env.bool("INGEST_ALLOW_GRAMMAR_DOWNLOAD", default=False)

# ---------------------------------------------------------------------------
# Ingestion: pipeline, persistence and task (apps/ingestion/pipeline.py, tasks.py)
# ---------------------------------------------------------------------------
# Cap on ArtifactRecord.raw_content (file text, or a branch's combined diff).
INGEST_MAX_STORED_CONTENT_BYTES: int = env.int("INGEST_MAX_STORED_CONTENT_BYTES", default=256 * 1024)
# Celery limits for ingest_repository_task. Not enforced by the Windows `--pool=solo`
# worker (Celery needs a prefork child to interrupt); the clone timeout still applies.
INGEST_TASK_SOFT_TIME_LIMIT_SECONDS: int = env.int("INGEST_TASK_SOFT_TIME_LIMIT_SECONDS", default=1800)
INGEST_TASK_TIME_LIMIT_SECONDS: int = env.int("INGEST_TASK_TIME_LIMIT_SECONDS", default=1860)
if INGEST_MAX_STORED_CONTENT_BYTES <= 0:
    raise ImproperlyConfigured(
        f"INGEST_MAX_STORED_CONTENT_BYTES must be positive, got {INGEST_MAX_STORED_CONTENT_BYTES}."
    )
if not 0 < INGEST_TASK_SOFT_TIME_LIMIT_SECONDS < INGEST_TASK_TIME_LIMIT_SECONDS:
    raise ImproperlyConfigured(
        "INGEST_TASK_SOFT_TIME_LIMIT_SECONDS must be positive and below INGEST_TASK_TIME_LIMIT_SECONDS "
        f"(got {INGEST_TASK_SOFT_TIME_LIMIT_SECONDS} and {INGEST_TASK_TIME_LIMIT_SECONDS})."
    )

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
