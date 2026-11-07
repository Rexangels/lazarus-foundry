"""Filesystem path helpers shared by settings and tests."""
from pathlib import Path

# backend/ (the directory containing manage.py)
BACKEND_DIR: Path = Path(__file__).resolve().parent.parent

# Repository root (one level above backend/), where .env and .lazarus_data/ live.
REPO_ROOT: Path = BACKEND_DIR.parent


def resolve_repo_path(value: str | Path, base: Path = REPO_ROOT) -> Path:
    """Resolve a configured path against ``base`` instead of the process cwd.

    Absolute paths (after ``~`` expansion) are kept as-is; relative paths such
    as ``./.lazarus_data/workspaces`` are anchored at ``base``. The result is
    always absolute and normalized.
    """
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base / path
    return path.resolve()
