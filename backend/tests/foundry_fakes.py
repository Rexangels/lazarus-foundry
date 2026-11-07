"""A recording test double of the parts of the Docker SDK that ``apps.foundry.sandbox`` uses.

``FakeDockerClient`` records every ``containers.create`` call (image and kwargs)
and runs a ``handler(container) -> FakeOutcome`` instead of a real container.
A ``hang=True`` outcome blocks ``wait()`` until ``kill()`` is called, like a
command that never finishes.
"""
from __future__ import annotations

import itertools
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

import requests
from docker.errors import APIError, ImageNotFound


@dataclass
class FakeOutcome:
    exit_code: int = 0
    stdout: bytes = b""
    stderr: bytes = b""
    hang: bool = False


def default_handler(container: FakeContainer) -> FakeOutcome:
    command = container.command
    if command[:2] == ["python", "-c"] and "venv.create" in command[2]:
        return FakeOutcome(stdout=b"3.11.9\n")
    return FakeOutcome()


def _matches(labels: dict[str, str], filters: dict[str, list[str]] | None) -> bool:
    for item in (filters or {}).get("label", []):
        key, _, value = item.partition("=")
        if labels.get(key) != value:
            return False
    return True


class FakeContainer:
    def __init__(self, client: FakeDockerClient, image: str, kwargs: dict[str, Any], number: int) -> None:
        self.client = client
        self.id = f"fake-container-{number}"
        self.image = image
        self.kwargs = kwargs
        self.command: list[str] = list(kwargs.get("command") or [])
        self.labels: dict[str, str] = dict(kwargs.get("labels") or {})
        self.started = self.killed = self.removed = False
        self.outcome: FakeOutcome | None = None
        self._done = threading.Event()

    @property
    def role(self) -> str:
        return self.labels.get("lazarus.role", "")

    def start(self) -> None:
        self.started = True
        self.outcome = self.client.handler(self)
        if not self.outcome.hang:
            self._done.set()

    def wait(self, timeout: float | None = None) -> dict[str, int]:
        if not self._done.wait(timeout):
            raise requests.exceptions.ReadTimeout("fake wait timed out")
        assert self.outcome is not None
        return {"StatusCode": self.outcome.exit_code}

    def kill(self) -> None:
        if self._done.is_set():
            raise APIError("container is not running")
        self.killed = True
        assert self.outcome is not None
        self.outcome.exit_code = 137
        self._done.set()

    def remove(self, force: bool = False) -> None:
        self.removed = True

    def logs_stream(self, stdout: bool) -> Iterator[bytes]:
        self._done.wait(30)
        assert self.outcome is not None
        data = self.outcome.stdout if stdout else self.outcome.stderr
        if data:
            yield data


class FakeLabelled:
    """A volume or network."""

    def __init__(self, name: str, labels: dict[str, str], kwargs: dict[str, Any]) -> None:
        self.name = name
        self.labels = labels
        self.kwargs = kwargs
        self.removed = False

    def remove(self, force: bool = False) -> None:
        self.removed = True


class FakeCollection:
    def __init__(self) -> None:
        self.items: list[Any] = []

    def list(self, all: bool = False, filters: dict[str, list[str]] | None = None) -> list[Any]:  # noqa: A002
        return [item for item in self.items if not item.removed and _matches(item.labels, filters)]


class FakeContainers(FakeCollection):
    def __init__(self, client: FakeDockerClient) -> None:
        super().__init__()
        self._client = client
        self._numbers = itertools.count(1)

    def create(self, image: str, **kwargs: Any) -> FakeContainer:
        container = FakeContainer(self._client, image, kwargs, next(self._numbers))
        self.items.append(container)
        return container


class FakeVolumes(FakeCollection):
    def create(self, name: str, driver: str = "local", labels: dict[str, str] | None = None) -> FakeLabelled:
        volume = FakeLabelled(name, dict(labels or {}), {"driver": driver})
        self.items.append(volume)
        return volume


class FakeNetworks(FakeCollection):
    def create(self, name: str, driver: str = "bridge", labels: dict[str, str] | None = None,
               **kwargs: Any) -> FakeLabelled:
        network = FakeLabelled(name, dict(labels or {}), {"driver": driver, **kwargs})
        self.items.append(network)
        return network


class FakeImages:
    def __init__(self, present: bool) -> None:
        self.present = present
        self.pulled: list[str] = []

    def get(self, name: str) -> object:
        if not self.present and name not in self.pulled:
            raise ImageNotFound(f"no such image: {name}")
        return object()

    def pull(self, repository: str, tag: str | None = None, **kwargs: Any) -> object:
        self.pulled.append(repository if tag is None else f"{repository}:{tag}")
        return object()


class FakeAPI:
    def __init__(self, client: FakeDockerClient) -> None:
        self._client = client

    def logs(self, container: str, stdout: bool = True, stderr: bool = True, stream: bool = False,
             follow: bool | None = None, **kwargs: Any) -> Iterator[bytes]:
        assert stream and follow and stdout != stderr
        [match] = [item for item in self._client.containers.items if item.id == container]
        return match.logs_stream(stdout=stdout)


class FakeDockerClient:
    def __init__(self, handler: Callable[[FakeContainer], FakeOutcome] = default_handler, *,
                 image_present: bool = True) -> None:
        self.handler = handler
        self.containers = FakeContainers(self)
        self.volumes = FakeVolumes()
        self.networks = FakeNetworks()
        self.images = FakeImages(image_present)
        self.api = FakeAPI(self)

    def ping(self) -> bool:
        return True

    def close(self) -> None:
        pass

    def created(self, role: str) -> list[FakeContainer]:
        return [item for item in self.containers.items if item.role == role]
