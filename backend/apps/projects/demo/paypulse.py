"""Build "Patient Zero": the ``paypulse_dead_repo`` demo Git repository.

The PayPulse content: an abandoned 2021 micro-ledger and invoicing project
whose ``main`` history is described by ``MAIN_BRANCH``, plus the unmerged
``feat/webhook-retry-engine`` branch (``RETRY_BRANCH``) that forked from main
in July 2021 and was abandoned with the retry engine ~80% done. The history
engine that writes it lives in ``apps.projects.demo.history``.

This is production code: ``python manage.py seed_paypulse_fixture`` and the
demo-ingest path build the repository from here. ``tests/fixtures/
build_paypulse_fixture.py`` re-exports it for the test suite.

The destination is always explicit. The CLI / management-command layer is
responsible for defaulting it to ``settings.FIXTURE_ROOT / PAYPULSE_FIXTURE_NAME``.
"""
from __future__ import annotations

import contextlib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType

from apps.projects.demo.history import (
    BranchSpec,
    CommitRecord,
    CommitSpec,
    Identity,
    build_branch,
    checkout,
    init_repository,
    prepare_destination,
    remove_tree,
    run_git,
)

PAYPULSE_FIXTURE_NAME: str = "paypulse_dead_repo"
DEFAULT_BRANCH: str = "main"
# The abandoned, never-merged feature branch holding ~80% of the retry engine.
RETRY_BRANCH_NAME: str = "feat/webhook-retry-engine"
# Planted exactly once on RETRY_BRANCH_NAME (in paypulse/retry.py), never on main.
RETRY_ENGINE_TODO_MARKER: str = "# TODO: complete backoff jitter and wire into dispatcher"


@dataclass(frozen=True)
class FixtureBuildResult:
    """What ``build_paypulse_fixture`` produced.

    ``branches`` maps every branch name to the commits the build added to it,
    oldest first. For the default branch that is its whole history; for a
    feature branch it is only the commits made after it forked from ``base``.
    ``head_sha`` is the checked-out commit (the default branch's tip).
    """

    repo_path: Path
    default_branch: str
    main_commits: tuple[CommitRecord, ...]
    head_sha: str
    branches: Mapping[str, tuple[CommitRecord, ...]]

    @property
    def branch_heads(self) -> dict[str, str]:
        """Branch name -> tip SHA."""
        return {name: commits[-1].sha for name, commits in self.branches.items()}

    @property
    def retry_branch_commits(self) -> tuple[CommitRecord, ...]:
        """The commits that exist only on the unmerged retry-engine branch."""
        return self.branches[RETRY_BRANCH_NAME]


def _replace_once(text: str, old: str, new: str) -> str:
    """Return ``text`` with ``old`` replaced exactly once (fails loudly if the anchor drifted)."""
    count = text.count(old)
    if count != 1:
        raise ValueError(f"expected exactly one occurrence of {old!r}, found {count}")
    return text.replace(old, new)


# ---------------------------------------------------------------------------
# PayPulse: people and time zones
# ---------------------------------------------------------------------------
PRIYA: Identity = Identity("Priya Nandakumar", "priya@paypulse.dev")
MARCUS: Identity = Identity("Marcus Feld", "marcus@paypulse.dev")

IST: timezone = timezone(timedelta(hours=5, minutes=30))  # Priya, Bengaluru
EST: timezone = timezone(timedelta(hours=-5))  # Marcus, Toronto (winter offset, fixed)


# ---------------------------------------------------------------------------
# PayPulse: file contents (each constant is one version of one file)
# ---------------------------------------------------------------------------
GITIGNORE: str = '''\
__pycache__/
*.py[cod]
.pytest_cache/
.venv/
venv/
build/
dist/
*.egg-info/
.env
'''

SETUP_CFG: str = '''\
[metadata]
name = paypulse
version = attr: paypulse.__version__
description = Micro-ledger and invoicing API for small merchants
license = MIT

[options]
packages = find:
python_requires = >=3.8

[options.packages.find]
exclude =
    tests

[tool:pytest]
testpaths = tests

[flake8]
max-line-length = 100
exclude = .git,__pycache__,.venv
'''

REQUIREMENTS_V1: str = '''\
pytest==6.2.2
'''

REQUIREMENTS_V2: str = REQUIREMENTS_V1 + '''\
requests==2.25.1
'''

REQUIREMENTS_V3: str = _replace_once(REQUIREMENTS_V2, "pytest==6.2.2", "pytest==6.2.4")

PACKAGE_INIT_V1: str = '''\
"""PayPulse: a tiny double-entry ledger and invoicing API."""

__version__ = "0.1.0"
'''

PACKAGE_INIT_V2: str = _replace_once(PACKAGE_INIT_V1, '"0.1.0"', '"0.9.0"')

README_V1: str = '''\
# PayPulse

PayPulse is a micro-ledger and invoicing API for small merchants.

Work in progress.

## Development

    pip install -r requirements.txt
    pytest
'''

README_V2: str = '''\
# PayPulse

PayPulse is a micro-ledger and invoicing API for small merchants: a
double-entry ledger, invoices with tax, and signed webhooks for invoice events.

## Features

- [x] Double-entry ledger with balanced, atomic postings
- [x] Invoices with line items, tax and a draft/issued/paid/void workflow
- [x] Multi-currency conversion (USD, EUR, GBP) with daily ECB rates
- [x] Automated Exponential Backoff Webhook Retries
- [x] PDF Invoice Export

All features above are complete and covered by tests as of v0.9.0.

## Quick start

```python
from decimal import Decimal

from paypulse.invoices import Invoice
from paypulse.ledger import AccountType, Ledger

ledger = Ledger()
ledger.open_account("1200", "Accounts Receivable", AccountType.ASSET)
ledger.open_account("4000", "Revenue", AccountType.REVENUE)

invoice = Invoice(number="INV-1001", customer="Acme Bakery")
invoice.add_line_item("Card terminal rental", 2, Decimal("45.00"))
invoice.issue()
invoice.post_to_ledger(ledger, "1200", "4000")
```

## Development

    pip install -r requirements.txt
    pytest

See `docs/architecture-notes.md` for the design of the webhook retry engine,
PDF export and FX conversion.

## License

MIT
'''

LEDGER_PY: str = '''\
"""Double-entry ledger primitives.

Every transaction is a set of entries whose debits and credits net to zero.
Amounts are Decimal values quantized to cents; floats are rejected because
binary rounding errors are unacceptable in a ledger.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_HALF_EVEN, Decimal
from enum import Enum
from typing import Dict, Iterable, List, Optional, Tuple, Union

CENT = Decimal("0.01")
ZERO = Decimal("0.00")

AmountLike = Union[Decimal, int, str]


class LedgerError(Exception):
    """Base class for all ledger failures."""


class UnknownAccountError(LedgerError):
    """Raised when a posting references an account that was never opened."""


class UnbalancedTransactionError(LedgerError):
    """Raised when the debits and credits of a transaction differ."""


class InsufficientFundsError(LedgerError):
    """Raised when a posting would overdraw an account that forbids it."""


class AccountType(Enum):
    ASSET = "asset"
    LIABILITY = "liability"
    EQUITY = "equity"
    REVENUE = "revenue"
    EXPENSE = "expense"


# Debit-normal accounts grow with debits; every other type grows with credits.
DEBIT_NORMAL = frozenset({AccountType.ASSET, AccountType.EXPENSE})


def to_amount(value: AmountLike) -> Decimal:
    """Convert value to a cent-quantized Decimal, rejecting floats."""
    if isinstance(value, float):
        raise TypeError("use Decimal or str for money, never float")
    amount = Decimal(value)
    if not amount.is_finite():
        raise ValueError("amount must be finite")
    return amount.quantize(CENT, rounding=ROUND_HALF_EVEN)


@dataclass
class Account:
    code: str
    name: str
    account_type: AccountType
    currency: str = "USD"
    allow_negative: bool = True
    balance: Decimal = ZERO

    @property
    def is_debit_normal(self) -> bool:
        return self.account_type in DEBIT_NORMAL

    def delta(self, debit: Decimal, credit: Decimal) -> Decimal:
        """Return how a debit/credit pair changes this account's balance."""
        if self.is_debit_normal:
            return debit - credit
        return credit - debit


@dataclass(frozen=True)
class Entry:
    account_code: str
    debit: Decimal = ZERO
    credit: Decimal = ZERO

    def __post_init__(self) -> None:
        debit = to_amount(self.debit)
        credit = to_amount(self.credit)
        if debit < 0 or credit < 0:
            raise ValueError("debit and credit must be non-negative")
        if (debit > 0) == (credit > 0):
            raise ValueError("an entry must have exactly one of debit or credit")
        object.__setattr__(self, "debit", debit)
        object.__setattr__(self, "credit", credit)

    @classmethod
    def debit_of(cls, account_code: str, amount: AmountLike) -> "Entry":
        return cls(account_code, debit=to_amount(amount))

    @classmethod
    def credit_of(cls, account_code: str, amount: AmountLike) -> "Entry":
        return cls(account_code, credit=to_amount(amount))


@dataclass(frozen=True)
class Transaction:
    id: int
    description: str
    entries: Tuple[Entry, ...]
    posted_at: datetime

    @property
    def amount(self) -> Decimal:
        return sum((entry.debit for entry in self.entries), ZERO)


class Ledger:
    """An in-memory book of accounts and the transactions posted to them."""

    def __init__(self, currency: str = "USD") -> None:
        self.currency = currency
        self._accounts: Dict[str, Account] = {}
        self._transactions: List[Transaction] = []

    def open_account(
        self,
        code: str,
        name: str,
        account_type: AccountType,
        allow_negative: bool = True,
    ) -> Account:
        if code in self._accounts:
            raise LedgerError(f"account {code} already exists")
        account = Account(
            code=code,
            name=name,
            account_type=account_type,
            currency=self.currency,
            allow_negative=allow_negative,
        )
        self._accounts[code] = account
        return account

    def account(self, code: str) -> Account:
        try:
            return self._accounts[code]
        except KeyError:
            raise UnknownAccountError(code) from None

    def balance(self, code: str) -> Decimal:
        return self.account(code).balance

    @property
    def transactions(self) -> List[Transaction]:
        return list(self._transactions)

    def post_transaction(
        self,
        description: str,
        entries: Iterable[Entry],
        posted_at: Optional[datetime] = None,
    ) -> Transaction:
        posted = tuple(entries)
        if len(posted) < 2:
            raise UnbalancedTransactionError("a transaction needs at least two entries")
        total_debit = sum((entry.debit for entry in posted), ZERO)
        total_credit = sum((entry.credit for entry in posted), ZERO)
        if total_debit != total_credit:
            raise UnbalancedTransactionError(f"debits {total_debit} != credits {total_credit}")

        # Validate every posting before touching any balance so a rejected
        # transaction leaves the ledger unchanged.
        projected: Dict[str, Decimal] = {}
        for entry in posted:
            account = self.account(entry.account_code)
            current = projected.get(account.code, account.balance)
            projected[account.code] = current + account.delta(entry.debit, entry.credit)
        for code, new_balance in projected.items():
            if new_balance < 0 and not self._accounts[code].allow_negative:
                raise InsufficientFundsError(f"{code} would drop to {new_balance}")

        for code, new_balance in projected.items():
            self._accounts[code].balance = new_balance

        transaction = Transaction(
            id=len(self._transactions) + 1,
            description=description,
            entries=posted,
            posted_at=posted_at or datetime.now(timezone.utc),
        )
        self._transactions.append(transaction)
        return transaction

    def trial_balance(self) -> Tuple[Decimal, Decimal]:
        """Return (total debits, total credits) across all posted transactions."""
        entries = [entry for transaction in self._transactions for entry in transaction.entries]
        debits = sum((entry.debit for entry in entries), ZERO)
        credits = sum((entry.credit for entry in entries), ZERO)
        return debits, credits
'''

TEST_LEDGER_PY: str = '''\
from decimal import Decimal

import pytest

from paypulse.ledger import (
    AccountType,
    Entry,
    InsufficientFundsError,
    Ledger,
    UnbalancedTransactionError,
    UnknownAccountError,
    to_amount,
)


@pytest.fixture
def ledger():
    book = Ledger()
    book.open_account("1000", "Cash", AccountType.ASSET, allow_negative=False)
    book.open_account("1200", "Accounts Receivable", AccountType.ASSET)
    book.open_account("4000", "Revenue", AccountType.REVENUE)
    return book


def test_balanced_transaction_updates_balances(ledger):
    ledger.post_transaction(
        "Invoice INV-1",
        [Entry.debit_of("1200", "150.00"), Entry.credit_of("4000", "150.00")],
    )
    assert ledger.balance("1200") == Decimal("150.00")
    assert ledger.balance("4000") == Decimal("150.00")


def test_unbalanced_transaction_is_rejected(ledger):
    with pytest.raises(UnbalancedTransactionError):
        ledger.post_transaction(
            "bad",
            [Entry.debit_of("1200", "10.00"), Entry.credit_of("4000", "9.99")],
        )
    assert ledger.transactions == []


def test_overdraft_is_rejected_atomically(ledger):
    with pytest.raises(InsufficientFundsError):
        ledger.post_transaction(
            "refund",
            [Entry.debit_of("4000", "25.00"), Entry.credit_of("1000", "25.00")],
        )
    assert ledger.balance("1000") == Decimal("0.00")
    assert ledger.balance("4000") == Decimal("0.00")


def test_unknown_account_is_rejected(ledger):
    with pytest.raises(UnknownAccountError):
        ledger.post_transaction(
            "typo",
            [Entry.debit_of("9999", "1.00"), Entry.credit_of("4000", "1.00")],
        )


def test_trial_balance_stays_even(ledger):
    ledger.post_transaction("sale", [Entry.debit_of("1200", "99.99"), Entry.credit_of("4000", "99.99")])
    ledger.post_transaction("collection", [Entry.debit_of("1000", "99.99"), Entry.credit_of("1200", "99.99")])
    debits, credits = ledger.trial_balance()
    assert debits == credits == Decimal("199.98")
    assert ledger.balance("1200") == Decimal("0.00")
    assert ledger.balance("1000") == Decimal("99.99")


def test_floats_are_rejected():
    with pytest.raises(TypeError):
        to_amount(0.1)


def test_entry_requires_exactly_one_side():
    with pytest.raises(ValueError):
        Entry("1000", debit=Decimal("1.00"), credit=Decimal("1.00"))
'''

CURRENCY_PY: str = '''\
"""Currency helpers.

PayPulse books every ledger in a single settlement currency today. The
conversion entry point exists so invoice code can call it, but only the
identity conversion is implemented.
"""
from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal

BASE_CURRENCY = "USD"
SUPPORTED_CURRENCIES = frozenset({"USD"})

# FIXME: only USD is listed; JPY (0 decimals) and BHD (3 decimals) will round wrongly.
MINOR_UNITS = {"USD": 2}


class CurrencyError(ValueError):
    """Raised for malformed or unsupported currency codes."""


def normalize_code(code: str) -> str:
    """Return the upper-case ISO 4217 form of code."""
    normalized = code.strip().upper()
    if len(normalized) != 3 or not normalized.isalpha():
        raise CurrencyError(f"invalid currency code: {code!r}")
    return normalized


def quantize(amount: Decimal, currency: str = BASE_CURRENCY) -> Decimal:
    """Round amount to the minor unit of currency."""
    places = MINOR_UNITS.get(normalize_code(currency), 2)
    return amount.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_EVEN)


def convert(amount: Decimal, from_currency: str, to_currency: str) -> Decimal:
    """Convert amount between currencies. Only same-currency conversion works."""
    source = normalize_code(from_currency)
    target = normalize_code(to_currency)
    if source == target:
        return quantize(amount, target)
    # TODO: fetch daily FX rates (ECB reference feed) and support EUR/GBP invoicing.
    raise NotImplementedError(f"currency conversion {source}->{target} is not implemented")
'''

INVOICES_PY_V1: str = '''\
"""Invoices with line items, tax and a small status workflow."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import ROUND_HALF_EVEN, Decimal
from enum import Enum
from typing import List, Optional

from paypulse.currency import BASE_CURRENCY, normalize_code, quantize
from paypulse.ledger import Entry, Ledger, Transaction, to_amount


class InvoiceError(Exception):
    """Raised for invalid invoice operations."""


class InvoiceStatus(Enum):
    DRAFT = "draft"
    ISSUED = "issued"
    PAID = "paid"
    VOID = "void"


@dataclass(frozen=True)
class LineItem:
    description: str
    quantity: int
    unit_price: Decimal

    @property
    def total(self) -> Decimal:
        return to_amount(self.unit_price * self.quantity)


@dataclass
class Invoice:
    number: str
    customer: str
    currency: str = BASE_CURRENCY
    tax_rate: Decimal = Decimal("0")
    line_items: List[LineItem] = field(default_factory=list)
    status: InvoiceStatus = InvoiceStatus.DRAFT
    issued_at: Optional[datetime] = None
    paid_at: Optional[datetime] = None

    def __post_init__(self) -> None:
        self.currency = normalize_code(self.currency)
        if not Decimal("0") <= self.tax_rate < Decimal("1"):
            raise InvoiceError("tax_rate must be between 0 and 1")

    def add_line_item(self, description: str, quantity: int, unit_price: Decimal) -> LineItem:
        if self.status is not InvoiceStatus.DRAFT:
            raise InvoiceError("line items can only be added to draft invoices")
        if quantity <= 0:
            raise InvoiceError("quantity must be positive")
        item = LineItem(description, quantity, to_amount(unit_price))
        self.line_items.append(item)
        return item

    @property
    def subtotal(self) -> Decimal:
        return quantize(sum((item.total for item in self.line_items), Decimal("0")), self.currency)

    @property
    def tax(self) -> Decimal:
        return (self.subtotal * self.tax_rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)

    @property
    def total(self) -> Decimal:
        return self.subtotal + self.tax

    def issue(self, at: Optional[datetime] = None) -> None:
        if self.status is not InvoiceStatus.DRAFT:
            raise InvoiceError(f"cannot issue a {self.status.value} invoice")
        if not self.line_items:
            raise InvoiceError("cannot issue an empty invoice")
        self.status = InvoiceStatus.ISSUED
        self.issued_at = at or datetime.now(timezone.utc)

    def mark_paid(self, at: Optional[datetime] = None) -> None:
        if self.status is not InvoiceStatus.ISSUED:
            raise InvoiceError(f"cannot pay a {self.status.value} invoice")
        self.status = InvoiceStatus.PAID
        self.paid_at = at or datetime.now(timezone.utc)

    def void(self) -> None:
        if self.status is InvoiceStatus.PAID:
            raise InvoiceError("paid invoices cannot be voided")
        self.status = InvoiceStatus.VOID

    def post_to_ledger(
        self,
        ledger: Ledger,
        receivable_code: str,
        revenue_code: str,
        tax_code: Optional[str] = None,
    ) -> Transaction:
        """Book the issued invoice: debit receivables, credit revenue and tax payable."""
        if self.status is not InvoiceStatus.ISSUED:
            raise InvoiceError("only issued invoices are posted to the ledger")
        entries = [
            Entry.debit_of(receivable_code, self.total),
            Entry.credit_of(revenue_code, self.subtotal),
        ]
        if self.tax > 0:
            if tax_code is None:
                raise InvoiceError("tax_code is required for taxed invoices")
            entries.append(Entry.credit_of(tax_code, self.tax))
        return ledger.post_transaction(f"Invoice {self.number}", entries, posted_at=self.issued_at)
'''

INVOICES_PY_V2: str = _replace_once(
    _replace_once(
        INVOICES_PY_V1,
        "from decimal import ROUND_HALF_EVEN, Decimal",
        "from decimal import ROUND_HALF_UP, Decimal",
    ),
    '        return (self.subtotal * self.tax_rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)',
    "        # Tax authorities round half-up; banker's rounding under-collected by a cent.\n"
    '        return (self.subtotal * self.tax_rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)',
)

TEST_INVOICES_PY_V1: str = '''\
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from paypulse.invoices import Invoice, InvoiceError, InvoiceStatus
from paypulse.ledger import AccountType, Ledger

ISSUED_AT = datetime(2021, 4, 6, 12, 0, tzinfo=timezone.utc)


def make_invoice(tax_rate="0.10"):
    invoice = Invoice(number="INV-1001", customer="Acme Bakery", tax_rate=Decimal(tax_rate))
    invoice.add_line_item("Card terminal rental", 2, Decimal("45.00"))
    invoice.add_line_item("Setup fee", 1, Decimal("30.00"))
    return invoice


def test_totals_include_tax():
    invoice = make_invoice()
    assert invoice.subtotal == Decimal("120.00")
    assert invoice.tax == Decimal("12.00")
    assert invoice.total == Decimal("132.00")


def test_status_workflow():
    invoice = make_invoice()
    invoice.issue(at=ISSUED_AT)
    assert invoice.status is InvoiceStatus.ISSUED
    with pytest.raises(InvoiceError):
        invoice.add_line_item("Late add", 1, Decimal("1.00"))
    invoice.mark_paid(at=ISSUED_AT)
    assert invoice.status is InvoiceStatus.PAID
    with pytest.raises(InvoiceError):
        invoice.void()


def test_empty_invoice_cannot_be_issued():
    with pytest.raises(InvoiceError):
        Invoice(number="INV-0", customer="Nobody").issue()


def test_posting_to_ledger_balances():
    ledger = Ledger()
    ledger.open_account("1200", "Accounts Receivable", AccountType.ASSET)
    ledger.open_account("4000", "Revenue", AccountType.REVENUE)
    ledger.open_account("2100", "Sales Tax Payable", AccountType.LIABILITY)
    invoice = make_invoice()
    invoice.issue(at=ISSUED_AT)
    invoice.post_to_ledger(ledger, "1200", "4000", tax_code="2100")
    assert ledger.balance("1200") == Decimal("132.00")
    assert ledger.balance("4000") == Decimal("120.00")
    assert ledger.balance("2100") == Decimal("12.00")
'''

TEST_INVOICES_PY_V2: str = TEST_INVOICES_PY_V1 + '''\


def test_tax_rounds_half_up():
    invoice = Invoice(number="INV-1002", customer="Corner Cafe", tax_rate=Decimal("0.05"))
    invoice.add_line_item("Espresso beans", 1, Decimal("10.50"))
    assert invoice.tax == Decimal("0.53")
'''

WEBHOOKS_PY_V1: str = '''\
"""Outbound webhooks for invoice lifecycle events.

Payloads are JSON, signed with HMAC-SHA256 so receivers can verify that a
request came from PayPulse.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional

# A transport POSTs body with headers to url and returns the HTTP status code.
Transport = Callable[[str, bytes, Mapping[str, str]], int]

SIGNATURE_HEADER = "X-PayPulse-Signature"


@dataclass(frozen=True)
class DeliveryResult:
    event: str
    url: str
    attempts: int
    delivered: bool
    status_code: Optional[int] = None
    error: Optional[str] = None


def sign_payload(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def http_post_transport(url: str, body: bytes, headers: Mapping[str, str]) -> int:
    """Default transport backed by requests (imported lazily)."""
    import requests

    response = requests.post(url, data=body, headers=dict(headers), timeout=10)
    return response.status_code


class WebhookDispatcher:
    """Delivers signed event payloads to every subscribed URL."""

    def __init__(self, secret: str, transport: Optional[Transport] = None) -> None:
        if not secret:
            raise ValueError("a signing secret is required")
        self._secret = secret
        self._transport = transport or http_post_transport
        self._subscriptions: Dict[str, List[str]] = {}

    def subscribe(self, event: str, url: str) -> None:
        urls = self._subscriptions.setdefault(event, [])
        if url not in urls:
            urls.append(url)

    def encode(self, event: str, payload: Mapping[str, Any]) -> bytes:
        document = {"event": event, "data": dict(payload)}
        return json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")

    def dispatch(self, event: str, payload: Mapping[str, Any]) -> List[DeliveryResult]:
        body = self.encode(event, payload)
        headers = {
            "Content-Type": "application/json",
            SIGNATURE_HEADER: sign_payload(self._secret, body),
        }
        return [self._deliver(event, url, body, headers) for url in self._subscriptions.get(event, [])]

    def _deliver(self, event: str, url: str, body: bytes, headers: Mapping[str, str]) -> DeliveryResult:
        try:
            status = self._transport(url, body, headers)
        except Exception as exc:  # transport failures are reported, not raised
            return DeliveryResult(event, url, attempts=1, delivered=False, error=str(exc))
        return DeliveryResult(event, url, attempts=1, delivered=200 <= status < 300, status_code=status)
'''

WEBHOOKS_PY_V2: str = _replace_once(
    WEBHOOKS_PY_V1,
    "        try:\n            status = self._transport(url, body, headers)\n",
    "        # FIXME: single attempt only. The exponential backoff retry engine lives on\n"
    "        # feat/webhook-retry-engine and still has to be wired into this dispatcher.\n"
    "        try:\n            status = self._transport(url, body, headers)\n",
)

TEST_WEBHOOKS_PY_V1: str = '''\
import json

from paypulse.webhooks import SIGNATURE_HEADER, WebhookDispatcher, sign_payload

URL = "https://hooks.example.com/paypulse"


class RecordingTransport:
    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.calls = []

    def __call__(self, url, body, headers):
        self.calls.append((url, body, dict(headers)))
        return self.statuses[min(len(self.calls), len(self.statuses)) - 1]


def test_dispatch_delivers_signed_payload():
    transport = RecordingTransport([200])
    dispatcher = WebhookDispatcher(secret="whsec_test", transport=transport)
    dispatcher.subscribe("invoice.paid", URL)
    [result] = dispatcher.dispatch("invoice.paid", {"number": "INV-1001"})
    assert result.delivered
    assert result.attempts == 1
    url, body, headers = transport.calls[0]
    assert url == URL
    assert json.loads(body) == {"event": "invoice.paid", "data": {"number": "INV-1001"}}
    assert headers[SIGNATURE_HEADER] == sign_payload("whsec_test", body)


def test_failed_delivery_is_reported():
    dispatcher = WebhookDispatcher(secret="whsec_test", transport=RecordingTransport([500]))
    dispatcher.subscribe("invoice.paid", URL)
    [result] = dispatcher.dispatch("invoice.paid", {"number": "INV-1001"})
    assert not result.delivered
    assert result.status_code == 500


def test_events_without_subscribers_are_dropped():
    dispatcher = WebhookDispatcher(secret="whsec_test", transport=RecordingTransport([200]))
    assert dispatcher.dispatch("invoice.void", {"number": "INV-1"}) == []
'''

TEST_WEBHOOKS_PY_V2: str = TEST_WEBHOOKS_PY_V1 + '''\


def test_dispatch_retries_with_exponential_backoff():
    # Spec from docs/architecture-notes.md: retry 5xx responses, doubling the delay.
    transport = RecordingTransport([503, 503, 200])
    sleeps = []
    dispatcher = WebhookDispatcher(
        secret="whsec_test",
        transport=transport,
        max_attempts=4,
        base_delay=0.5,
        sleep=sleeps.append,
    )
    dispatcher.subscribe("invoice.paid", URL)
    [result] = dispatcher.dispatch("invoice.paid", {"number": "INV-1001"})
    assert result.delivered
    assert result.attempts == 3
    assert sleeps == [0.5, 1.0]
'''

ARCHITECTURE_NOTES_MD: str = '''\
# PayPulse Architecture Notes

_Last updated: September 2021 (Priya)_

## Modules

| Module | Responsibility |
| --- | --- |
| `paypulse/ledger.py` | Double-entry ledger: accounts, balanced transactions, trial balance |
| `paypulse/invoices.py` | Invoice line items, tax, status workflow, ledger posting |
| `paypulse/currency.py` | Currency codes, rounding, FX conversion |
| `paypulse/webhooks.py` | Signed outbound webhooks for invoice events |
| `paypulse/export/pdf.py` | PDF invoice rendering |

## Webhook retry engine

Receivers go down. A delivery that hits a network error or a 5xx response is
retried with exponential backoff:

- the delay before attempt `n` is `base_delay * 2 ** (n - 2)`, so 0.5s, 1s, 2s
  and 4s with the default `base_delay=0.5`;
- optional full jitter (`jitter=True`) spreads retries from many invoices; it
  is off by default so delays stay predictable in tests;
- after `max_attempts` (default 5) the delivery moves to a dead-letter list for
  manual replay;
- 4xx responses are never retried.

`WebhookDispatcher` takes `max_attempts`, `base_delay` and an injectable
`sleep` callable so tests never wait.

## PDF invoice export

`Invoice.to_pdf()` renders an issued invoice to PDF through
`paypulse/export/pdf.py` (ReportLab). The layout has the merchant header, the
line item table, a tax summary and a payment QR code.

## Multi-currency

Invoices can be billed in USD, EUR or GBP. `currency.convert()` uses ECB daily
reference rates cached for 24 hours; ledger postings are always converted to
the merchant's settlement currency.
'''


# ---------------------------------------------------------------------------
# PayPulse: main branch history (oldest first)
# ---------------------------------------------------------------------------
MAIN_COMMITS: tuple[CommitSpec, ...] = (
    CommitSpec(
        message="Initial commit: PayPulse project skeleton",
        author=PRIYA,
        authored_at=datetime(2021, 2, 8, 10, 14, tzinfo=IST),
        writes={
            ".gitignore": GITIGNORE,
            "README.md": README_V1,
            "setup.cfg": SETUP_CFG,
            "requirements.txt": REQUIREMENTS_V1,
            "paypulse/__init__.py": PACKAGE_INIT_V1,
        },
    ),
    CommitSpec(
        message=(
            "Add double-entry ledger with balanced postings\n"
            "\n"
            "Transactions are validated in full before any balance changes, so a\n"
            "rejected posting never leaves the book half-applied."
        ),
        author=PRIYA,
        authored_at=datetime(2021, 2, 19, 18, 42, tzinfo=IST),
        writes={
            "paypulse/ledger.py": LEDGER_PY,
            "tests/__init__.py": "",
            "tests/test_ledger.py": TEST_LEDGER_PY,
        },
    ),
    CommitSpec(
        message="Add currency helpers (single settlement currency for now)",
        author=PRIYA,
        authored_at=datetime(2021, 3, 30, 11, 5, tzinfo=IST),
        writes={"paypulse/currency.py": CURRENCY_PY},
    ),
    CommitSpec(
        message="Add invoices with line items, tax and status workflow",
        author=MARCUS,
        authored_at=datetime(2021, 4, 6, 14, 27, tzinfo=EST),
        writes={
            "paypulse/invoices.py": INVOICES_PY_V1,
            "tests/test_invoices.py": TEST_INVOICES_PY_V1,
        },
    ),
    CommitSpec(
        message="Add signed webhook dispatcher for invoice events",
        author=MARCUS,
        authored_at=datetime(2021, 7, 22, 16, 3, tzinfo=EST),
        writes={
            "paypulse/webhooks.py": WEBHOOKS_PY_V1,
            "tests/test_webhooks.py": TEST_WEBHOOKS_PY_V1,
            "requirements.txt": REQUIREMENTS_V2,
        },
    ),
    CommitSpec(
        message="Document architecture: retry engine, PDF export and FX design",
        author=PRIYA,
        authored_at=datetime(2021, 9, 3, 9, 31, tzinfo=IST),
        writes={"docs/architecture-notes.md": ARCHITECTURE_NOTES_MD},
    ),
    CommitSpec(
        message=(
            "Add retry-with-backoff spec test for webhook dispatcher\n"
            "\n"
            "The retry engine itself is being built on feat/webhook-retry-engine.\n"
            "This test pins the expected behaviour until that branch lands."
        ),
        author=MARCUS,
        authored_at=datetime(2021, 11, 15, 13, 48, tzinfo=EST),
        writes={
            "paypulse/webhooks.py": WEBHOOKS_PY_V2,
            "tests/test_webhooks.py": TEST_WEBHOOKS_PY_V2,
        },
    ),
    CommitSpec(
        message="Update README for the 0.9 feature set",
        author=PRIYA,
        authored_at=datetime(2022, 3, 10, 20, 16, tzinfo=IST),
        writes={
            "README.md": README_V2,
            "paypulse/__init__.py": PACKAGE_INIT_V2,
        },
    ),
    CommitSpec(
        message=(
            "Bump pytest to 6.2.4\n"
            "\n"
            "Stay on 6.x until the CI runner image is rebuilt; 7.0 changed tmpdir handling."
        ),
        author=MARCUS,
        authored_at=datetime(2022, 8, 29, 10, 52, tzinfo=EST),
        writes={"requirements.txt": REQUIREMENTS_V3},
    ),
    CommitSpec(
        message="Fix rounding in invoice tax calculation",
        author=PRIYA,
        authored_at=datetime(2023, 1, 18, 17, 9, tzinfo=IST),
        writes={
            "paypulse/invoices.py": INVOICES_PY_V2,
            "tests/test_invoices.py": TEST_INVOICES_PY_V2,
        },
    ),
)

MAIN_BRANCH: BranchSpec = BranchSpec(name=DEFAULT_BRANCH, commits=MAIN_COMMITS)


# ---------------------------------------------------------------------------
# PayPulse: feat/webhook-retry-engine (unmerged, abandoned July 2021)
#
# Marcus forks main right after landing the webhook dispatcher and, with one
# commit from Priya, builds most of a generic retry engine in three days:
# policy, exponential delays, max_delay cap, a retry loop with injectable
# sleep, and retryable-failure classification. Jitter is a stub that raises
# NotImplementedError, the dispatcher in paypulse/webhooks.py is never wired to
# the engine, and the branch is never merged. main keeps pointing at it (the
# FIXME in webhooks.py, the spec test, the docs, the README "[x]") for another
# 18 months.
# ---------------------------------------------------------------------------
# Index into MAIN_COMMITS of the fork point: "Add signed webhook dispatcher".
RETRY_BRANCH_BASE_INDEX: int = 4

RETRY_PY_V1: str = (
    '''\
"""Exponential backoff retry engine for webhook deliveries.

A delivery that fails transiently is retried after a delay that grows by
multiplier after every attempt. The engine is transport-agnostic: callers pass
the operation to run and a predicate that decides whether a failure is worth
retrying.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List


@dataclass(frozen=True)
class RetryPolicy:
    """How many times to try a delivery, and how long to wait between attempts."""

    max_attempts: int = 5
    base_delay: float = 0.5
    multiplier: float = 2.0
    jitter: bool = False

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if self.base_delay < 0:
            raise ValueError("base_delay must be non-negative")
        if self.multiplier < 1:
            raise ValueError("multiplier must be at least 1")

    def delay_for(self, attempt: int) -> float:
        """Return the delay in seconds before attempt number attempt (1-based).

        The first attempt runs immediately; attempt n waits
        base_delay * multiplier ** (n - 2).
        """
        if attempt < 1:
            raise ValueError("attempts are numbered from 1")
        if attempt == 1:
            return 0.0
        delay = self.base_delay * self.multiplier ** (attempt - 2)
        if self.jitter:
            '''
    + RETRY_ENGINE_TODO_MARKER
    + '''
            raise NotImplementedError("backoff jitter is not implemented yet")
        return delay

    def delays(self) -> List[float]:
        """Return the waits before attempts 2..max_attempts."""
        return [self.delay_for(attempt) for attempt in range(2, self.max_attempts + 1)]
'''
)

RETRY_PY_V2: str = _replace_once(
    _replace_once(
        _replace_once(
            RETRY_PY_V1,
            "    multiplier: float = 2.0\n",
            "    multiplier: float = 2.0\n    max_delay: float = 30.0\n",
        ),
        '            raise ValueError("multiplier must be at least 1")\n',
        '            raise ValueError("multiplier must be at least 1")\n'
        "        if self.max_delay < self.base_delay:\n"
        '            raise ValueError("max_delay must be at least base_delay")\n',
    ),
    "        delay = self.base_delay * self.multiplier ** (attempt - 2)\n",
    "        delay = min(self.max_delay, self.base_delay * self.multiplier ** (attempt - 2))\n",
)

RETRY_PY_V3: str = _replace_once(
    _replace_once(
        RETRY_PY_V2,
        "from __future__ import annotations\n\nfrom dataclasses import dataclass\nfrom typing import List\n",
        "from __future__ import annotations\n\nimport time\n"
        "from dataclasses import dataclass\nfrom typing import Callable, Generic, List, TypeVar\n\n"
        'T = TypeVar("T")\n\nSleep = Callable[[float], None]\n',
    ),
    "@dataclass(frozen=True)\nclass RetryPolicy:\n",
    '''\
class RetryExhaustedError(Exception):
    """Raised when every attempt allowed by the policy has failed."""

    def __init__(self, attempts: int, last_error: Exception) -> None:
        super().__init__(f"gave up after {attempts} attempts: {last_error}")
        self.attempts = attempts
        self.last_error = last_error


@dataclass(frozen=True)
class RetryPolicy:
''',
) + '''

@dataclass(frozen=True)
class RetryOutcome(Generic[T]):
    """The value of a successful call and what it took to get it."""

    value: T
    attempts: int
    delays: List[float]


def call_with_retry(
    operation: Callable[[], T],
    policy: RetryPolicy,
    should_retry: Callable[[Exception], bool],
    sleep: Sleep = time.sleep,
) -> RetryOutcome[T]:
    """Run operation until it succeeds, fails non-retryably, or runs out of attempts.

    Non-retryable exceptions propagate unchanged. sleep is injectable so tests
    never actually wait.
    """
    delays: List[float] = []
    for attempt in range(1, policy.max_attempts + 1):
        if attempt > 1:
            delay = policy.delay_for(attempt)
            sleep(delay)
            delays.append(delay)
        try:
            value = operation()
        except Exception as exc:
            if not should_retry(exc):
                raise
            if attempt == policy.max_attempts:
                raise RetryExhaustedError(attempt, exc) from exc
            continue
        return RetryOutcome(value=value, attempts=attempt, delays=delays)
    raise AssertionError("unreachable: max_attempts is validated to be at least 1")
'''

RETRY_PY_V4: str = _replace_once(
    _replace_once(
        RETRY_PY_V3,
        "\n\n@dataclass(frozen=True)\nclass RetryOutcome(Generic[T]):\n",
        '''

# OSError covers ConnectionError, TimeoutError and requests' RequestException.
RETRYABLE_EXCEPTIONS = (OSError,)


class RetryableStatusError(Exception):
    """A webhook receiver answered with a non-2xx status code."""

    def __init__(self, status_code: int) -> None:
        super().__init__(f"receiver answered {status_code}")
        self.status_code = status_code


def is_retryable_status(status_code: int) -> bool:
    """5xx responses are retried; 4xx means the payload was rejected, so never retry."""
    return 500 <= status_code < 600


def is_retryable_exception(exc: Exception) -> bool:
    """Default should_retry: network failures and 5xx responses."""
    if isinstance(exc, RetryableStatusError):
        return is_retryable_status(exc.status_code)
    return isinstance(exc, RETRYABLE_EXCEPTIONS)


@dataclass(frozen=True)
class RetryOutcome(Generic[T]):
''',
    ),
    "    should_retry: Callable[[Exception], bool],\n",
    "    should_retry: Callable[[Exception], bool] = is_retryable_exception,\n",
)

TEST_RETRY_PY_V1: str = '''\
import pytest

from paypulse.retry import RetryPolicy


def test_first_attempt_runs_immediately():
    assert RetryPolicy().delay_for(1) == 0.0


def test_delays_double_from_base_delay():
    policy = RetryPolicy(max_attempts=5, base_delay=0.5)
    assert policy.delays() == [0.5, 1.0, 2.0, 4.0]


@pytest.mark.parametrize(
    "kwargs",
    [{"max_attempts": 0}, {"base_delay": -1}, {"multiplier": 0.5}],
)
def test_invalid_policies_are_rejected(kwargs):
    with pytest.raises(ValueError):
        RetryPolicy(**kwargs)
'''

TEST_RETRY_PY_V2: str = _replace_once(
    TEST_RETRY_PY_V1,
    '    [{"max_attempts": 0}, {"base_delay": -1}, {"multiplier": 0.5}],\n',
    '    [\n'
    '        {"max_attempts": 0},\n'
    '        {"base_delay": -1},\n'
    '        {"multiplier": 0.5},\n'
    '        {"base_delay": 2.0, "max_delay": 1.0},\n'
    '    ],\n',
) + '''

def test_delays_are_capped_at_max_delay():
    policy = RetryPolicy(max_attempts=8, base_delay=1.0, max_delay=10.0)
    assert policy.delays() == [1.0, 2.0, 4.0, 8.0, 10.0, 10.0, 10.0]
'''

TEST_RETRY_PY_V3: str = _replace_once(
    TEST_RETRY_PY_V2,
    "from paypulse.retry import RetryPolicy\n",
    "from paypulse.retry import RetryExhaustedError, RetryPolicy, call_with_retry\n",
) + '''

class Flaky:
    """Raises the given exceptions in order, then returns "ok"."""

    def __init__(self, *failures):
        self.failures = list(failures)
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        return "ok"


def always(exc):
    return True


def test_retries_until_success_and_records_delays():
    sleeps = []
    operation = Flaky(ConnectionError("down"), ConnectionError("still down"))
    outcome = call_with_retry(operation, RetryPolicy(base_delay=0.5), always, sleep=sleeps.append)
    assert outcome.value == "ok"
    assert outcome.attempts == 3
    assert outcome.delays == sleeps == [0.5, 1.0]


def test_gives_up_after_max_attempts():
    sleeps = []
    operation = Flaky(ConnectionError("a"), ConnectionError("b"), ConnectionError("c"))
    with pytest.raises(RetryExhaustedError) as excinfo:
        call_with_retry(operation, RetryPolicy(max_attempts=3), always, sleep=sleeps.append)
    assert excinfo.value.attempts == 3
    assert operation.calls == 3
    assert sleeps == [0.5, 1.0]


def test_non_retryable_errors_propagate_immediately():
    operation = Flaky(ValueError("bad payload"))
    with pytest.raises(ValueError):
        call_with_retry(operation, RetryPolicy(), lambda exc: False, sleep=lambda _: None)
    assert operation.calls == 1
'''

TEST_RETRY_PY_V4: str = _replace_once(
    TEST_RETRY_PY_V3,
    "from paypulse.retry import RetryExhaustedError, RetryPolicy, call_with_retry\n",
    "from paypulse.retry import (\n"
    "    RetryableStatusError,\n"
    "    RetryExhaustedError,\n"
    "    RetryPolicy,\n"
    "    call_with_retry,\n"
    "    is_retryable_exception,\n"
    "    is_retryable_status,\n"
    ")\n",
) + '''

@pytest.mark.parametrize(
    "status, expected",
    [(500, True), (503, True), (599, True), (404, False), (429, False), (200, False)],
)
def test_only_5xx_statuses_are_retryable(status, expected):
    assert is_retryable_status(status) is expected


def test_default_classifier_retries_network_errors_and_5xx():
    sleeps = []
    operation = Flaky(ConnectionError("reset"), RetryableStatusError(502))
    outcome = call_with_retry(operation, RetryPolicy(), sleep=sleeps.append)
    assert outcome.attempts == 3
    assert not is_retryable_exception(RetryableStatusError(404))
    assert not is_retryable_exception(ValueError("bad payload"))
'''

RETRY_BRANCH_COMMITS: tuple[CommitSpec, ...] = (
    CommitSpec(
        message=(
            "Start webhook retry engine: RetryPolicy with exponential delays\n"
            "\n"
            "First cut of the policy the dispatcher will consult between delivery\n"
            "attempts. Jitter is stubbed until we pick full vs. equal jitter."
        ),
        author=MARCUS,
        authored_at=datetime(2021, 7, 22, 19, 48, tzinfo=EST),
        writes={
            "paypulse/retry.py": RETRY_PY_V1,
            "tests/test_retry.py": TEST_RETRY_PY_V1,
        },
    ),
    CommitSpec(
        message="Cap retry delays at max_delay",
        author=MARCUS,
        authored_at=datetime(2021, 7, 23, 9, 40, tzinfo=EST),
        writes={
            "paypulse/retry.py": RETRY_PY_V2,
            "tests/test_retry.py": TEST_RETRY_PY_V2,
        },
    ),
    CommitSpec(
        message=(
            "Add retry loop with injectable sleep\n"
            "\n"
            "call_with_retry() runs an operation until it succeeds, fails with a\n"
            "non-retryable error, or exhausts the policy. Tests pass a fake sleep."
        ),
        author=MARCUS,
        authored_at=datetime(2021, 7, 23, 17, 55, tzinfo=EST),
        writes={
            "paypulse/retry.py": RETRY_PY_V3,
            "tests/test_retry.py": TEST_RETRY_PY_V3,
        },
    ),
    CommitSpec(
        message=(
            "Classify retryable failures: network errors and 5xx only\n"
            "\n"
            "A 4xx means the receiver rejected the payload; retrying cannot help."
        ),
        author=PRIYA,
        authored_at=datetime(2021, 7, 24, 11, 20, tzinfo=IST),
        writes={
            "paypulse/retry.py": RETRY_PY_V4,
            "tests/test_retry.py": TEST_RETRY_PY_V4,
        },
    ),
)

RETRY_BRANCH: BranchSpec = BranchSpec(
    name=RETRY_BRANCH_NAME,
    commits=RETRY_BRANCH_COMMITS,
    # Relative to main's tip so the spec stays a plain string revision.
    base=f"{DEFAULT_BRANCH}~{len(MAIN_COMMITS) - 1 - RETRY_BRANCH_BASE_INDEX}",
)

# Built in this order; every branch with a base must come after the branch it forks from.
FIXTURE_BRANCHES: tuple[BranchSpec, ...] = (MAIN_BRANCH, RETRY_BRANCH)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def build_paypulse_fixture(destination: Path, *, force: bool = False) -> FixtureBuildResult:
    """Build the PayPulse dead repository at ``destination``.

    Builds every branch in ``FIXTURE_BRANCHES`` and leaves the default branch
    checked out with a clean work tree. Raises ``FixtureExistsError`` if
    ``destination`` exists and ``force`` is False, and
    ``UnsafeDestinationError`` for destinations refused by
    ``assert_safe_destination``. A failed build removes the partial repository.
    """
    repo_path = prepare_destination(destination, force=force)
    try:
        init_repository(repo_path, default_branch=DEFAULT_BRANCH, identity=PRIYA)
        branches = {branch.name: build_branch(repo_path, branch) for branch in FIXTURE_BRANCHES}
        checkout(repo_path, DEFAULT_BRANCH)
        head_sha = run_git(repo_path, "rev-parse", "HEAD").strip()
    except BaseException:
        with contextlib.suppress(OSError):
            remove_tree(repo_path)
        raise
    return FixtureBuildResult(
        repo_path=repo_path,
        default_branch=DEFAULT_BRANCH,
        main_commits=branches[DEFAULT_BRANCH],
        head_sha=head_sha,
        branches=MappingProxyType(branches),
    )
