"""Health endpoint and settings tests. None of these need PostgreSQL or Redis."""
import os
from datetime import datetime
from pathlib import Path

import pytest
from django.conf import settings
from django.test import Client

from config.paths import BACKEND_DIR, REPO_ROOT, resolve_repo_path


def test_health_endpoint_returns_ok_json(client: Client) -> None:
    response = client.get("/api/health")

    assert response.status_code == 200
    assert response["Content-Type"].startswith("application/json")
    body = response.json()
    assert body["status"] == "ok"
    assert body["service"] == "lazarus-foundry"
    assert body["version"] == "0.1.0"
    assert datetime.fromisoformat(body["timestamp"]).tzinfo is not None


def test_health_endpoint_rejects_post(client: Client) -> None:
    assert client.post("/api/health").status_code == 405


def test_repo_root_is_parent_of_backend() -> None:
    assert (BACKEND_DIR / "manage.py").is_file()
    assert REPO_ROOT == BACKEND_DIR.parent
    assert (REPO_ROOT / "docker-compose.yml").is_file()


@pytest.mark.parametrize(
    ("raw", "expected_relative"),
    [
        ("./.lazarus_data/workspaces", Path(".lazarus_data/workspaces")),
        (".lazarus_data/fixtures", Path(".lazarus_data/fixtures")),
        ("./a/../b", Path("b")),
    ],
)
def test_relative_paths_resolve_against_repo_root_not_cwd(
    raw: str, expected_relative: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    resolved = resolve_repo_path(raw)

    assert resolved == (REPO_ROOT / expected_relative).resolve()
    assert tmp_path not in resolved.parents


def test_absolute_paths_are_kept(tmp_path: Path) -> None:
    target = tmp_path / "workspaces"
    assert resolve_repo_path(str(target)) == target.resolve()


def test_settings_workspace_and_fixture_roots_are_repo_anchored() -> None:
    for name, default in (
        ("WORKSPACE_ROOT", "./.lazarus_data/workspaces"),
        ("FIXTURE_ROOT", "./.lazarus_data/fixtures"),
    ):
        value: Path = getattr(settings, name)
        assert value.is_absolute()
        assert value == resolve_repo_path(os.environ.get(name, default))


def test_database_is_postgresql_not_sqlite() -> None:
    assert settings.DATABASES["default"]["ENGINE"] == "django.db.backends.postgresql"


def test_redis_backs_celery_and_channels() -> None:
    assert settings.CELERY_BROKER_URL == settings.REDIS_URL
    assert settings.CELERY_RESULT_BACKEND == settings.REDIS_URL
    layer = settings.CHANNEL_LAYERS["default"]
    assert layer["BACKEND"] == "channels_redis.core.RedisChannelLayer"
    assert layer["CONFIG"]["hosts"] == [settings.REDIS_URL]


def test_asgi_and_channels_are_configured() -> None:
    apps = settings.INSTALLED_APPS
    assert settings.ASGI_APPLICATION == "config.asgi.application"
    assert "channels" in apps
    assert apps.index("daphne") < apps.index("django.contrib.staticfiles")


def test_model_and_sandbox_settings_are_loaded() -> None:
    assert settings.MODEL_ID
    assert settings.MODEL_BASE_URL.startswith("https://")
    assert isinstance(settings.MODEL_API_KEY, str)
    assert settings.SANDBOX_MODE in settings.SANDBOX_MODES
    assert isinstance(settings.SANDBOX_PREVIEW_PORT, int)
