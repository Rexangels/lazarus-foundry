"""``python manage.py seed_paypulse_fixture``: build the PayPulse dead repo on demand.

The builder lives in ``apps.projects.demo`` (``tests/fixtures/
build_paypulse_fixture.py`` re-exports it for the test suite). The command
touches only the filesystem and Git: no system checks, no migration checks and
no database access, so it works while PostgreSQL is down.
"""
from __future__ import annotations

from argparse import ArgumentParser
from pathlib import Path
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.projects.demo.history import FixtureBuildError, FixtureExistsError
from apps.projects.demo.paypulse import PAYPULSE_FIXTURE_NAME, FixtureBuildResult, build_paypulse_fixture


def default_destination() -> Path:
    return Path(settings.FIXTURE_ROOT) / PAYPULSE_FIXTURE_NAME


def format_summary(result: FixtureBuildResult) -> list[str]:
    """Human-readable lines describing a finished build."""
    lines = [f"Built PayPulse fixture at {result.repo_path}", f"  checked out: {result.default_branch}"]
    width = max(len(name) for name in result.branches)
    for name, commits in result.branches.items():
        if name == result.default_branch:
            count = f"{len(commits)} commits"
        else:
            count = f"{len(commits)} commits not on {result.default_branch} (unmerged)"
        lines.append(f"  {name:<{width}}  head {commits[-1].sha}  {count}")
    return lines


class Command(BaseCommand):
    help = (
        "Build the 'Patient Zero' PayPulse dead Git repository (main plus the unmerged "
        "feat/webhook-retry-engine branch). Defaults to FIXTURE_ROOT/paypulse_dead_repo."
    )
    # Filesystem + Git only: skip system checks so a down database can't block seeding.
    requires_system_checks: list[str] = []
    requires_migrations_checks: bool = False

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument(
            "--destination",
            default=None,
            help=f"Directory to build the repository in (default: FIXTURE_ROOT/{PAYPULSE_FIXTURE_NAME}).",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Delete and rebuild the destination if it holds a previous Lazarus-built fixture.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        raw_destination: str | Path | None = options["destination"]
        destination = Path(raw_destination).expanduser() if raw_destination else default_destination()
        try:
            result = build_paypulse_fixture(destination, force=bool(options["force"]))
        except FixtureExistsError as exc:
            raise CommandError(
                f"{destination.resolve()} already exists. Re-run with --force to rebuild it."
            ) from exc
        except FixtureBuildError as exc:  # UnsafeDestinationError, missing git, git failures
            raise CommandError(str(exc)) from exc
        for line in format_summary(result):
            self.stdout.write(line)
        self.stdout.write(self.style.SUCCESS("PayPulse fixture ready."))
