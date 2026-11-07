"""Fast backend check for the Kiro save hook: ``manage.py check`` and the non-slow tests.

Launch it with any Python (``python backend/scripts/dev_check.py`` from the
repository root); it runs Django and pytest with the backend virtualenv's
interpreter, so the launcher needs nothing but the standard library.

The two steps run concurrently. Each child spends most of its time importing
Django and the apps (about 10 s per process when the virtualenv sits on a
synced or scanned drive), so running them side by side keeps the hook's wall
time near the pytest run alone. ``manage.py check`` output is buffered and
printed after the tests, so the two never interleave.

Exit status: 0 when both steps pass or when there is nothing to check yet
(no ``backend/manage.py`` or no ``backend/.venv``); otherwise the check's exit
code if it failed, else pytest's. ``python -m pytest`` from ``backend/`` still
runs everything, including tests marked ``slow``.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path

BACKEND_DIR: Path = Path(__file__).resolve().parent.parent
CHECK_ARGS: tuple[str, ...] = ("manage.py", "check")
FAST_PYTEST_ARGS: tuple[str, ...] = ("-m", "pytest", "-q", "--tb=short", "-m", "not slow")


def venv_python(backend_dir: Path = BACKEND_DIR) -> Path:
    """Path of the backend virtualenv's interpreter for this platform (it may not exist)."""
    if os.name == "nt":
        return backend_dir / ".venv" / "Scripts" / "python.exe"
    return backend_dir / ".venv" / "bin" / "python"


def describe(python: Path, args: Sequence[str]) -> str:
    shown = " ".join(f'"{arg}"' if " " in arg else arg for arg in args)
    return f"$ {python} {shown}"


def main(backend_dir: Path = BACKEND_DIR) -> int:
    if not (backend_dir / "manage.py").is_file():
        print(f"dev_check: {backend_dir / 'manage.py'} not found; Django skeleton not initialized yet, skipping.")
        return 0
    python = venv_python(backend_dir)
    if not python.is_file():
        print(f"dev_check: virtualenv interpreter {python} not found; create backend/.venv first, skipping.")
        return 0

    started = time.monotonic()
    # The hook passes a JSON payload on stdin; the children never need it.
    with tempfile.TemporaryFile(mode="w+b") as check_log:
        check = subprocess.Popen(
            [str(python), *CHECK_ARGS], cwd=backend_dir, stdin=subprocess.DEVNULL,
            stdout=check_log, stderr=subprocess.STDOUT,
        )
        print(describe(python, FAST_PYTEST_ARGS), flush=True)
        try:
            tests_code = subprocess.call([str(python), *FAST_PYTEST_ARGS], cwd=backend_dir, stdin=subprocess.DEVNULL)
        finally:
            check_code = check.wait()
        check_log.seek(0)
        print(describe(python, CHECK_ARGS), flush=True)
        sys.stdout.write(check_log.read().decode("utf-8", errors="replace"))
    print(f"dev_check: finished in {time.monotonic() - started:.1f} s", flush=True)
    return check_code or tests_code


if __name__ == "__main__":
    sys.exit(main())
