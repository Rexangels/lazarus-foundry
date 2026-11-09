"""Deterministic code changes: the EngineeringAgent's offline path (no ``MODEL_API_KEY``).

Without a reasoning model the Foundry cannot write new code for an arbitrary
repository. It can still apply two kinds of changes exactly:

* **Anchored patches** (``Recipe``) for the golden demo, PayPulse. Each one is
  a list of ``(old, new)`` text replacements on one file, or the full text of a
  new file. A replacement applies only when ``old`` occurs exactly once (or
  ``new`` is already there), so a recipe never edits a file that drifted from
  the content it was written for; it then reports itself as not applicable and
  the item is ``UNSUPPORTED``.
* **Generic structure** for ``EXTRACT``: the boundary test that imports every
  extracted module (``boundary_test_source``) and the extract's minimal
  ``pyproject.toml`` (``extract_pyproject_source``, used by the ArchitectureAgent).

``Recipe.satisfied`` is what the self-healing loop checks: a recipe whose
result is missing from the work tree (for example because a later edit
reverted it) is applied again; when every recipe is satisfied there is nothing
deterministic left to repair.
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from apps.foundry.workspace import read_text


@dataclass(frozen=True)
class FileEdit:
    path: str
    content: str
    created: bool


@dataclass(frozen=True)
class Replacement:
    old: str
    new: str


@dataclass(frozen=True)
class Recipe:
    name: str
    kind: str  # the PlanItem kind it implements
    path: str
    summary: str
    replacements: tuple[Replacement, ...] = ()
    create: str | None = None  # full text of a file the recipe creates
    requires: tuple[str, ...] = ()  # files that must exist first (e.g. a merged module)

    def edit(self, workspace: Path) -> FileEdit | None:
        """The file after this recipe, or None when it does not apply to the current content."""
        if any(read_text(workspace, path) is None for path in self.requires):
            return None
        current = read_text(workspace, self.path)
        if self.create is not None:
            if current is None or current == self.create:
                return FileEdit(self.path, self.create, created=current is None)
            return None
        if current is None:
            return None
        text = current
        for step in self.replacements:
            if step.new in text:
                continue
            if text.count(step.old) != 1:
                return None
            text = text.replace(step.old, step.new)
        return FileEdit(self.path, text, created=False)

    def satisfied(self, workspace: Path) -> bool:
        current = read_text(workspace, self.path)
        if current is None:
            return False
        if self.create is not None:
            return current == self.create
        return all(step.new in current for step in self.replacements)


def recipes_for(kind: str, paths: Iterable[str]) -> tuple[Recipe, ...]:
    wanted = set(paths)
    return tuple(recipe for recipe in RECIPES if recipe.kind == kind and recipe.path in wanted)


# ---------------------------------------------------------------------------
# PayPulse: complete the retry engine's jitter (restore-02)
# ---------------------------------------------------------------------------
RETRY_JITTER = Recipe(
    name="paypulse.retry.full_jitter",
    kind="COMPLETE_STUB",
    path="paypulse/retry.py",
    summary="Implement full jitter in RetryPolicy.delay_for (a uniform wait in [0, delay]); signature unchanged.",
    replacements=(
        Replacement(
            "import time\nfrom dataclasses import dataclass\n",
            "import random\nimport time\nfrom dataclasses import dataclass\n",
        ),
        Replacement(
            "        if self.jitter:\n"
            "            # TODO: complete backoff jitter and wire into dispatcher\n"
            '            raise NotImplementedError("backoff jitter is not implemented yet")\n'
            "        return delay\n",
            "        if self.jitter:\n"
            "            # Full jitter: a uniform wait in [0, delay] spreads the retries of many\n"
            "            # deliveries that failed at the same moment.\n"
            "            return random.uniform(0.0, delay)\n"
            "        return delay\n",
        ),
    ),
)

# ---------------------------------------------------------------------------
# PayPulse: wire the retry engine into the webhook dispatcher (restore-03)
# ---------------------------------------------------------------------------
WEBHOOKS_RETRY_WIRING = Recipe(
    name="paypulse.webhooks.retry_wiring",
    kind="FIX_FAILING_TEST",
    path="paypulse/webhooks.py",
    summary=(
        "WebhookDispatcher takes max_attempts, base_delay and an injectable sleep and delivers through "
        "paypulse.retry.call_with_retry: 5xx and network errors are retried with exponential backoff, "
        "4xx are not. The default of one attempt keeps the old behavior for existing callers."
    ),
    requires=("paypulse/retry.py",),
    replacements=(
        Replacement(
            "import json\n"
            "from dataclasses import dataclass\n"
            "from typing import Any, Callable, Dict, List, Mapping, Optional\n",
            "import json\n"
            "import time\n"
            "from dataclasses import dataclass\n"
            "from typing import Any, Callable, Dict, List, Mapping, Optional\n"
            "\n"
            "from paypulse.retry import (\n"
            "    RetryableStatusError,\n"
            "    RetryExhaustedError,\n"
            "    RetryPolicy,\n"
            "    call_with_retry,\n"
            ")\n",
        ),
        Replacement(
            "    def __init__(self, secret: str, transport: Optional[Transport] = None) -> None:\n"
            "        if not secret:\n"
            '            raise ValueError("a signing secret is required")\n'
            "        self._secret = secret\n"
            "        self._transport = transport or http_post_transport\n"
            "        self._subscriptions: Dict[str, List[str]] = {}\n",
            "    def __init__(\n"
            "        self,\n"
            "        secret: str,\n"
            "        transport: Optional[Transport] = None,\n"
            "        max_attempts: int = 1,\n"
            "        base_delay: float = 0.5,\n"
            "        sleep: Callable[[float], None] = time.sleep,\n"
            "    ) -> None:\n"
            "        if not secret:\n"
            '            raise ValueError("a signing secret is required")\n'
            "        self._secret = secret\n"
            "        self._transport = transport or http_post_transport\n"
            "        self._subscriptions: Dict[str, List[str]] = {}\n"
            "        # Deliveries retry through the exponential backoff engine (paypulse/retry.py).\n"
            "        # One attempt by default keeps the original behavior for callers that do not opt in.\n"
            "        self._policy = RetryPolicy(max_attempts=max_attempts, base_delay=base_delay)\n"
            "        self._sleep = sleep\n",
        ),
        Replacement(
            "    def _deliver(self, event: str, url: str, body: bytes, headers: Mapping[str, str]) -> DeliveryResult:\n"
            "        # FIXME: single attempt only. The exponential backoff retry engine lives on\n"
            "        # feat/webhook-retry-engine and still has to be wired into this dispatcher.\n"
            "        try:\n"
            "            status = self._transport(url, body, headers)\n"
            "        except Exception as exc:  # transport failures are reported, not raised\n"
            "            return DeliveryResult(event, url, attempts=1, delivered=False, error=str(exc))\n"
            "        return DeliveryResult(event, url, attempts=1, delivered=200 <= status < 300, status_code=status)\n",
            "    def _deliver(self, event: str, url: str, body: bytes, headers: Mapping[str, str]) -> DeliveryResult:\n"
            "        attempts = 0\n"
            "\n"
            "        def attempt() -> int:\n"
            "            nonlocal attempts\n"
            "            attempts += 1\n"
            "            status = self._transport(url, body, headers)\n"
            "            if not 200 <= status < 300:\n"
            "                raise RetryableStatusError(status)\n"
            "            return status\n"
            "\n"
            "        try:\n"
            "            outcome = call_with_retry(attempt, self._policy, sleep=self._sleep)\n"
            "        except RetryExhaustedError as exc:\n"
            "            return self._failure(event, url, attempts, exc.last_error)\n"
            "        except Exception as exc:  # non-retryable failures (4xx, bad payloads) are reported, not raised\n"
            "            return self._failure(event, url, attempts, exc)\n"
            "        return DeliveryResult(\n"
            "            event, url, attempts=outcome.attempts, delivered=True, status_code=outcome.value\n"
            "        )\n"
            "\n"
            "    @staticmethod\n"
            "    def _failure(event: str, url: str, attempts: int, error: Exception) -> DeliveryResult:\n"
            "        if isinstance(error, RetryableStatusError):\n"
            "            return DeliveryResult(\n"
            "                event, url, attempts=attempts, delivered=False, status_code=error.status_code\n"
            "            )\n"
            "        return DeliveryResult(event, url, attempts=attempts, delivered=False, error=str(error))\n",
        ),
    ),
)

# ---------------------------------------------------------------------------
# PayPulse: complete currency.convert against an injectable rate source (restore-04)
# ---------------------------------------------------------------------------
CURRENCY_RATE_PROVIDER = Recipe(
    name="paypulse.currency.rate_provider",
    kind="COMPLETE_STUB",
    path="paypulse/currency.py",
    summary=(
        "convert() converts USD/EUR/GBP with rates from an injectable RateProvider (set_rate_provider); "
        "StaticRateProvider is the offline implementation. The live ECB feed stays deferred, so without a "
        "provider a cross-currency conversion raises CurrencyError. Signature unchanged."
    ),
    replacements=(
        Replacement(
            '"""Currency helpers.\n'
            "\n"
            "PayPulse books every ledger in a single settlement currency today. The\n"
            "conversion entry point exists so invoice code can call it, but only the\n"
            "identity conversion is implemented.\n"
            '"""\n',
            '"""Currency helpers.\n'
            "\n"
            "PayPulse books every ledger in a single settlement currency. convert()\n"
            "converts between the supported currencies with rates from an injectable\n"
            "RateProvider (set_rate_provider). The live ECB daily reference feed is not\n"
            "wired in yet, so without a provider a cross-currency conversion raises\n"
            "CurrencyError.\n"
            '"""\n',
        ),
        Replacement(
            "from decimal import ROUND_HALF_EVEN, Decimal\n",
            "from decimal import ROUND_HALF_EVEN, Decimal\nfrom typing import Mapping, Optional, Protocol\n",
        ),
        Replacement(
            'SUPPORTED_CURRENCIES = frozenset({"USD"})\n',
            'SUPPORTED_CURRENCIES = frozenset({"USD", "EUR", "GBP"})\n',
        ),
        Replacement(
            "def convert(amount: Decimal, from_currency: str, to_currency: str) -> Decimal:\n"
            '    """Convert amount between currencies. Only same-currency conversion works."""\n'
            "    source = normalize_code(from_currency)\n"
            "    target = normalize_code(to_currency)\n"
            "    if source == target:\n"
            "        return quantize(amount, target)\n"
            "    # TODO: fetch daily FX rates (ECB reference feed) and support EUR/GBP invoicing.\n"
            '    raise NotImplementedError(f"currency conversion {source}->{target} is not implemented")\n',
            "class RateProvider(Protocol):\n"
            '    """A source of FX rates: how many units of target one unit of source buys."""\n'
            "\n"
            "    def rate(self, source: str, target: str) -> Decimal:\n"
            "        ...\n"
            "\n"
            "\n"
            "class StaticRateProvider:\n"
            '    """Fixed rates, quoted as units of each currency per one BASE_CURRENCY.\n'
            "\n"
            "    Offline and deterministic: for tests, and for callers that load the ECB\n"
            "    reference rates themselves.\n"
            '    """\n'
            "\n"
            "    def __init__(self, per_base: Mapping[str, Decimal]) -> None:\n"
            "        rates = {normalize_code(code): Decimal(value) for code, value in per_base.items()}\n"
            "        rates.setdefault(BASE_CURRENCY, Decimal(1))\n"
            "        for code, value in rates.items():\n"
            "            if not value.is_finite() or value <= 0:\n"
            '                raise CurrencyError(f"invalid rate for {code}: {value}")\n'
            "        self._per_base = rates\n"
            "\n"
            "    def rate(self, source: str, target: str) -> Decimal:\n"
            "        try:\n"
            "            return self._per_base[normalize_code(target)] / self._per_base[normalize_code(source)]\n"
            "        except KeyError as exc:\n"
            '            raise CurrencyError(f"no rate for {exc.args[0]}") from None\n'
            "\n"
            "\n"
            "_rate_provider: Optional[RateProvider] = None\n"
            "\n"
            "\n"
            "def set_rate_provider(provider: Optional[RateProvider]) -> None:\n"
            '    """Install the FX rate source convert() uses; None removes it."""\n'
            "    global _rate_provider\n"
            "    _rate_provider = provider\n"
            "\n"
            "\n"
            "def get_rate_provider() -> Optional[RateProvider]:\n"
            "    return _rate_provider\n"
            "\n"
            "\n"
            "def convert(amount: Decimal, from_currency: str, to_currency: str) -> Decimal:\n"
            '    """Convert amount between supported currencies, rounded to the target\'s minor unit."""\n'
            "    source = normalize_code(from_currency)\n"
            "    target = normalize_code(to_currency)\n"
            "    if source == target:\n"
            "        return quantize(amount, target)\n"
            "    for code in (source, target):\n"
            "        if code not in SUPPORTED_CURRENCIES:\n"
            '            raise CurrencyError(f"unsupported currency: {code}")\n'
            "    provider = _rate_provider\n"
            "    if provider is None:\n"
            "        raise CurrencyError(\n"
            '            f"no FX rate provider is configured for {source}->{target}; call set_rate_provider() first"\n'
            "        )\n"
            "    rate = Decimal(provider.rate(source, target))\n"
            "    if not rate.is_finite() or rate <= 0:\n"
            '        raise CurrencyError(f"invalid FX rate {rate} for {source}->{target}")\n'
            "    return quantize(Decimal(amount) * rate, target)\n",
        ),
    ),
)

# ---------------------------------------------------------------------------
# PayPulse: offline tests for convert (restore-05)
# ---------------------------------------------------------------------------
CURRENCY_TESTS = Recipe(
    name="paypulse.tests.test_currency",
    kind="ADD_TESTS",
    path="tests/test_currency.py",
    summary="Offline tests for convert(): identity rounding, base and cross rates, no provider, bad input.",
    requires=("paypulse/currency.py",),
    create='''\
from decimal import Decimal

import pytest

from paypulse.currency import (
    CurrencyError,
    StaticRateProvider,
    convert,
    get_rate_provider,
    set_rate_provider,
)

RATES = StaticRateProvider({"EUR": Decimal("0.9000"), "GBP": Decimal("0.8000")})


@pytest.fixture
def rates():
    previous = get_rate_provider()
    set_rate_provider(RATES)
    yield RATES
    set_rate_provider(previous)


@pytest.fixture
def no_provider():
    previous = get_rate_provider()
    set_rate_provider(None)
    yield
    set_rate_provider(previous)


def test_same_currency_is_quantized_without_a_provider(no_provider):
    assert convert(Decimal("10.005"), "usd", "USD") == Decimal("10.00")


def test_base_to_foreign_uses_the_provider(rates):
    assert convert(Decimal("100.00"), "USD", "EUR") == Decimal("90.00")


def test_cross_rates_go_through_the_base_currency(rates):
    assert convert(Decimal("90.00"), "EUR", "GBP") == Decimal("80.00")


def test_conversion_without_a_provider_fails_clearly(no_provider):
    with pytest.raises(CurrencyError, match="no FX rate provider"):
        convert(Decimal("1.00"), "USD", "EUR")


def test_unsupported_currencies_are_rejected(rates):
    with pytest.raises(CurrencyError):
        convert(Decimal("1.00"), "USD", "JPY")


def test_non_positive_rates_are_rejected():
    with pytest.raises(CurrencyError):
        StaticRateProvider({"EUR": Decimal("0")})
''',
)

RECIPES: tuple[Recipe, ...] = (RETRY_JITTER, WEBHOOKS_RETRY_WIRING, CURRENCY_RATE_PROVIDER, CURRENCY_TESTS)


# ---------------------------------------------------------------------------
# EXTRACT: generic structure
# ---------------------------------------------------------------------------
def module_name(relative: str) -> str | None:
    """``paypulse/ledger.py`` -> ``paypulse.ledger``; ``paypulse/__init__.py`` -> ``paypulse``."""
    path = PurePosixPath(relative)
    if path.suffix != ".py" or not all(part.isidentifier() for part in (*path.parts[:-1], path.stem)):
        return None
    parts = list(path.parts[:-1]) if path.stem == "__init__" else [*path.parts[:-1], path.stem]
    return ".".join(parts) or None


def extracted_modules(extract_root: str, destinations: Sequence[str]) -> tuple[str, ...]:
    """Importable non-test modules copied under ``extract_root``, sorted."""
    prefix = extract_root.rstrip("/") + "/"
    names: set[str] = set()
    for destination in destinations:
        if not destination.startswith(prefix):
            continue
        relative = destination[len(prefix):]
        if relative.split("/", 1)[0] in ("tests", "test"):
            continue
        name = module_name(relative)
        if name:
            names.add(name)
    return tuple(sorted(names))


def boundary_test_source(modules: Sequence[str]) -> str:
    listed = "".join(f"    {name!r},\n" for name in modules)
    return (
        '"""Boundary test generated by Lazarus Foundry: every extracted module imports on its own."""\n'
        "import importlib\n"
        "\n"
        "import pytest\n"
        "\n"
        f"MODULES = [\n{listed}]\n"
        "\n"
        "\n"
        '@pytest.mark.parametrize("name", MODULES)\n'
        "def test_module_imports(name):\n"
        "    assert importlib.import_module(name).__name__ == name\n"
    )


def extract_pyproject_source(extract_root: str, modules: Sequence[str]) -> str:
    slug = PurePosixPath(extract_root).name or "extracted"
    packages = sorted({name.split(".", 1)[0] for name in modules})
    listed = ", ".join(f'"{name}"' for name in packages)
    return (
        "[build-system]\n"
        'requires = ["setuptools>=61"]\n'
        'build-backend = "setuptools.build_meta"\n'
        "\n"
        "[project]\n"
        f'name = "{slug}"\n'
        'version = "0.1.0"\n'
        'description = "Extracted by Lazarus Foundry"\n'
        'requires-python = ">=3.8"\n'
        "\n"
        "[project.optional-dependencies]\n"
        'test = ["pytest"]\n'
        "\n"
        "[tool.setuptools]\n"
        f"packages = [{listed}]\n"
        "\n"
        "[tool.pytest.ini_options]\n"
        'testpaths = ["tests"]\n'
    )
