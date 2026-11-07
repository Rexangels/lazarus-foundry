"""Shared pytest configuration.

Tests that need the database run against the real PostgreSQL configured by
``DATABASE_URL`` (pytest-django creates ``test_<NAME>`` on that server). When
that server is unreachable, those tests are skipped with the reason below;
tests that do not touch the database always run.
"""
from functools import lru_cache

import psycopg
import pytest
from django.conf import settings

# Fixtures whose presence (directly or transitively) means a test needs the DB.
DB_FIXTURES: frozenset[str] = frozenset({"db", "transactional_db", "django_db_setup"})
PROBE_TIMEOUT_SECONDS: int = 3


@lru_cache(maxsize=1)
def postgres_unavailable_reason() -> str | None:
    """Return why the configured PostgreSQL server can't be used, or None if it can."""
    db = settings.DATABASES["default"]
    engine = str(db.get("ENGINE", ""))
    if engine != "django.db.backends.postgresql":
        return f"DATABASE_URL must point at PostgreSQL, got engine {engine!r}"

    host = str(db.get("HOST") or "localhost")
    port = str(db.get("PORT") or "5432")
    try:
        conn = psycopg.connect(
            host=host,
            port=port,
            dbname=str(db.get("NAME") or ""),
            user=str(db.get("USER") or ""),
            password=str(db.get("PASSWORD") or ""),
            connect_timeout=PROBE_TIMEOUT_SECONDS,
        )
    except psycopg.Error as exc:
        first_line = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
        return (
            f"PostgreSQL at {host}:{port} is unreachable ({first_line}). "
            "Start it with `docker compose up -d` from the repository root."
        )
    conn.close()
    return None


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    db_items = [
        item
        for item in items
        if item.get_closest_marker("django_db") is not None
        or DB_FIXTURES.intersection(getattr(item, "fixturenames", ()))
    ]
    if not db_items:
        return
    reason = postgres_unavailable_reason()
    if reason is None:
        return
    skip = pytest.mark.skip(reason=reason)
    for item in db_items:
        item.add_marker(skip)
