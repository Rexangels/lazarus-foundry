"""Fast backend check for the Kiro save hook: ``manage.py check`` then the non-slow tests.

Launch it with any Python (``python backend/scripts/dev_check.py`` from the
repository root); it re-runs Django and pytest with the backend virtualenv's
interpreter, so the launcher needs nothing but the standard library.

Exit status: 0 when both steps pass or when there is nothing to check yet
(no ``backend/manage.py`` or no ``backend/.venv``); otherwise the first
non-zero exit code. ``python -m pytest`` from ``backend/`` still runs
everything, including tests marked ``slow``.
"""
from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

BACKEND_DIR: Path = Path(__file__).resolve().parent.parent
FAST_PYTEST_ARGS: tuple[str, ...] = ("-m", "pytest", "-q", "--tb=short", "-m", "not slow")


def venv_python(backend_dir: Path = BACKEND_DIR) -> Path:
    """Path of the backend virtualenv's interpreter for this platform (it may not exist)."""
    if os.name == "nt":
        return backend_dir / ".venv" / "Scripts" / "python.exe"
    return backend_dir / ".venv" / "bin" / "python"


def run_step(python: Path, args: Sequence[str], backend_dir: Path) -> int:
    shown = " ".join(f'"{arg}"' if " " in arg else arg for arg in args)
    print(f"$ {python} {shown}", flush=True)
    # The hook passes a JSON payload on stdin; the children never need it.
    return subprocess.call([str(python), *args], cwd=backend_dir, stdin=subprocess.DEVNULL)


def main(backend_dir: Path = BACKEND_DIR) -> int:
    if not (backend_dir / "manage.py").is_file():
        print(f"dev_check: {backend_dir / 'manage.py'} not found; Django skeleton not initialized yet, skipping.")
        return 0
    python = venv_python(backend_dir)
    if not python.is_file():
        print(f"dev_check: virtualenv interpreter {python} not found; create backend/.venv first, skipping.")
        return 0
    for args in (("manage.py", "check"), FAST_PYTEST_ARGS):
        code = run_step(python, args, backend_dir)
        if code != 0:
            return code
    return 0


if __name__ == "__main__":
    sys.exit(main())
