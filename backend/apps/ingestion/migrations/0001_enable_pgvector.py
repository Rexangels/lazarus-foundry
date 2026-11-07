"""Enable the pgvector extension before any model uses ``VectorField``.

Every migration that adds a ``VectorField`` must depend (directly or
transitively) on this one.
"""
from django.db import migrations
from pgvector.django import VectorExtension


class Migration(migrations.Migration):
    initial = True

    dependencies: list[tuple[str, str]] = []

    operations = [VectorExtension()]
