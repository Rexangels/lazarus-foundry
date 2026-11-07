"""Output capture shared by both sandbox modes, and local process control for subprocess mode.

* ``OutputSink`` keeps at most ``limit`` bytes of stdout and of stderr (each)
  and streams complete lines to ``on_output(stream, line)``. Calls are
  serialized, a failing callback is dropped (it never fails the command), and
  streaming stops once a stream is truncated.
* ``run_process`` runs an argv list (never a shell) in its own process group
  (Windows: ``CREATE_NEW_PROCESS_GROUP``; POSIX: a new session) and, on
  timeout, kills the whole tree (``taskkill /F /T /PID`` or ``killpg``).
"""
from __future__ import annotations

import contextlib
import logging
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

from apps.foundry.sandbox_env import SandboxError

logger = logging.getLogger(__name__)

OutputCallback = Callable[[str, str], None]
"""``on_output(stream, line)`` with ``stream`` ``"stdout"`` or ``"stderr"``; calls are serialized."""

KILL_GRACE_SECONDS: float = 10.0
READER_JOIN_SECONDS: float = 10.0
INTERPRETER_PROBE_TIMEOUT_SECONDS: float = 30.0
MAX_STREAMED_LINE_BYTES: int = 64 * 1024
VERSION_PROBE: str = "import platform, sys; print(sys.executable); print(platform.python_version())"


class SandboxUnavailableError(SandboxError):
    """The Docker daemon, the image or a Python interpreter is not available."""


class OutputSink:
    """Two capped captures plus the serialized ``on_output`` callback."""

    def __init__(self, limit: int, on_output: OutputCallback | None) -> None:
        self._lock = threading.Lock()
        self._callback = on_output
        self.stdout = Capture("stdout", limit, self)
        self.stderr = Capture("stderr", limit, self)

    @property
    def streaming(self) -> bool:
        return self._callback is not None

    def emit(self, stream: str, line: str) -> None:
        with self._lock:
            if self._callback is None:
                return
            try:
                self._callback(stream, line)
            except Exception:
                logger.exception("sandbox on_output callback failed; streaming stopped for this command")
                self._callback = None

    def finish(self) -> None:
        self.stdout.finish()
        self.stderr.finish()


class Capture:
    def __init__(self, name: str, limit: int, sink: OutputSink) -> None:
        self.name = name
        self._limit = limit
        self._sink = sink
        self._chunks: list[bytes] = []
        self._size = 0
        self._pending = b""
        self._streaming = True
        self.truncated = False

    def feed(self, data: bytes) -> None:
        if not data:
            return
        room = max(self._limit - self._size, 0)
        kept = data[:room]
        if kept:
            self._chunks.append(kept)
            self._size += len(kept)
        if len(data) > room:
            self.truncated = True
        self._stream(kept)

    def _stream(self, kept: bytes) -> None:
        """Stream only the captured bytes, so telemetry never shows more than the stored output."""
        if not (self._streaming and self._sink.streaming):
            return
        self._pending += kept
        *lines, self._pending = self._pending.split(b"\n")
        for line in lines:
            self._emit(line)
        if len(self._pending) > MAX_STREAMED_LINE_BYTES:
            self._emit(self._pending)
            self._pending = b""
        if self.truncated:
            if self._pending:
                self._emit(self._pending)
            self._streaming = False
            self._pending = b""
            self._sink.emit(self.name, f"[lazarus] {self.name} truncated at {self._limit} bytes")

    def _emit(self, line: bytes) -> None:
        self._sink.emit(self.name, line.rstrip(b"\r").decode("utf-8", errors="replace"))

    def finish(self) -> None:
        if self._pending and self._streaming:
            self._emit(self._pending)
        self._pending = b""

    def text(self) -> str:
        return b"".join(self._chunks).decode("utf-8", errors="replace")


def start_reader(chunks: Iterable[bytes], capture: Capture) -> threading.Thread:
    """Feed ``chunks`` into ``capture`` on a daemon thread."""

    def pump() -> None:
        try:
            for chunk in chunks:
                capture.feed(chunk)
        except Exception:  # a closed pipe or a dropped log stream ends the capture
            logger.debug("sandbox %s reader stopped", capture.name, exc_info=True)

    thread = threading.Thread(target=pump, name=f"sandbox-{capture.name}", daemon=True)
    thread.start()
    return thread


def pipe_chunks(pipe: IO[bytes]) -> Iterable[bytes]:
    reader = getattr(pipe, "read1", pipe.read)
    return iter(lambda: reader(65536), b"")


# ---------------------------------------------------------------------------
# Local processes
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ProcessOutcome:
    exit_code: int
    stdout: str
    stderr: str
    stdout_truncated: bool
    stderr_truncated: bool
    duration_seconds: float
    timed_out: bool


def process_group_kwargs() -> dict[str, Any]:
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def kill_process_tree(pid: int) -> None:
    """Kill ``pid`` and its descendants: ``taskkill /F /T`` on Windows, the process group elsewhere."""
    if os.name == "nt":
        # Absolute path: a sandbox-controlled PATH must not choose the kill command.
        taskkill = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32" / "taskkill.exe"
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run(  # noqa: S603 - fixed argv
                [str(taskkill), "/F", "/T", "/PID", str(pid)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=30, check=False,
            )
    else:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pid, signal.SIGKILL)


def spawn(argv: Sequence[str], *, cwd: Path, env: Mapping[str, str]) -> subprocess.Popen[bytes]:
    """Start ``argv`` (no shell) with piped output in its own process group."""
    try:
        return subprocess.Popen(  # noqa: S603 - argv list, never a shell
            list(argv), cwd=cwd, env=dict(env), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, close_fds=True, **process_group_kwargs(),
        )
    except OSError as exc:
        raise SandboxError(f"could not start {Path(argv[0]).name!r}: {exc.strerror or exc}") from exc


def attach_readers(process: subprocess.Popen[bytes], sink: OutputSink) -> list[threading.Thread]:
    assert process.stdout is not None and process.stderr is not None
    return [start_reader(pipe_chunks(process.stdout), sink.stdout),
            start_reader(pipe_chunks(process.stderr), sink.stderr)]


def stop_process(process: subprocess.Popen[bytes]) -> int:
    """Kill the process tree of ``process`` and return its exit code."""
    kill_process_tree(process.pid)
    try:
        return process.wait(timeout=KILL_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        return process.wait()


def run_process(
    argv: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    timeout: float,
    max_output_bytes: int,
    on_output: OutputCallback | None = None,
) -> ProcessOutcome:
    """Run ``argv`` and wait at most ``timeout`` seconds; on timeout kill the whole process tree."""
    sink = OutputSink(max_output_bytes, on_output)
    started = time.monotonic()
    process = spawn(argv, cwd=cwd, env=env)
    readers = attach_readers(process, sink)
    timed_out = False
    try:
        exit_code = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        exit_code = stop_process(process)
    except BaseException:
        stop_process(process)
        raise
    for reader in readers:
        reader.join(READER_JOIN_SECONDS)
    if any(reader.is_alive() for reader in readers):
        # A descendant outlived the command and still holds its output pipes open.
        kill_process_tree(process.pid)
        logger.warning("sandbox command %r left a process running that holds its output open", argv[0])
    sink.finish()
    return ProcessOutcome(
        exit_code=exit_code,
        stdout=sink.stdout.text(),
        stderr=sink.stderr.text(),
        stdout_truncated=sink.stdout.truncated,
        stderr_truncated=sink.stderr.truncated,
        duration_seconds=round(time.monotonic() - started, 3),
        timed_out=timed_out,
    )


@dataclass(frozen=True)
class InterpreterChoice:
    executable: Path
    version: str
    source: str  # "py-launcher", "path" or "configured"

    def matches(self, requested: str) -> bool:
        return self.version == requested or self.version.startswith(requested + ".")


def resolve_interpreter(version: str, *, env: Mapping[str, str]) -> InterpreterChoice:
    """Find Python ``version`` for a subprocess-mode venv.

    Windows tries the ``py -X.Y`` launcher, other systems ``pythonX.Y`` on PATH;
    the fallback is this process's interpreter. ``InterpreterChoice.version``
    is the version actually found, which may differ from the one requested.
    """
    candidates: list[tuple[list[str], str]] = []
    if os.name == "nt":
        launcher = shutil.which("py")
        if launcher:
            candidates.append(([launcher, f"-{version}", "-c", VERSION_PROBE], "py-launcher"))
    else:
        exact = shutil.which(f"python{version}")
        if exact:
            candidates.append(([exact, "-c", VERSION_PROBE], "path"))
    candidates.append(([sys.executable, "-c", VERSION_PROBE], "configured"))
    for argv, source in candidates:
        try:
            completed = subprocess.run(  # noqa: S603 - argv list, never a shell
                argv, env=dict(env), stdin=subprocess.DEVNULL, capture_output=True, text=True,
                timeout=INTERPRETER_PROBE_TIMEOUT_SECONDS, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        lines = completed.stdout.strip().splitlines()
        if completed.returncode == 0 and len(lines) >= 2:
            return InterpreterChoice(executable=Path(lines[-2].strip()), version=lines[-1].strip(), source=source)
    raise SandboxUnavailableError(f"no Python interpreter could be started for version {version}")
