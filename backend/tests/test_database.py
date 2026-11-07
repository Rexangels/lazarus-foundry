"""Tests that require the real PostgreSQL + pgvector server.

Skipped automatically (see conftest.py) when PostgreSQL is not reachable.
"""
import pytest
from django.db import connection


@pytest.mark.django_db
def test_database_is_postgres_with_pgvector_available() -> None:
    assert connection.vendor == "postgresql"
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1 FROM pg_available_extensions WHERE name = %s", ["vector"])
        assert cursor.fetchone() is not None, "pgvector extension is not installed on the server"
