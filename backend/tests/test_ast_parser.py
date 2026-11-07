"""Tests for apps.ingestion.ast_parser / doc_extractor / evidence (no database, no network)."""
from __future__ import annotations

import logging
import re
import textwrap
from collections.abc import Iterator

import pytest
from tree_sitter import Language

from apps.ingestion import ast_parser
from apps.ingestion.ast_parser import (
    ASTParserService,
    FileInventory,
    InventoryScope,
    LanguageSpec,
    ParseStatus,
    RevisionInventory,
    extract_with_spec,
    parse_branch_changes,
    parse_revision,
    python_language,
)
from apps.ingestion.doc_extractor import ClaimStyle, Ecosystem, ManifestKind
from apps.ingestion.evidence import EvidenceKind, MarkerDetection, MarkerTag
from apps.ingestion.git_forensics import GitForensicService
from tests.fixtures.build_paypulse_fixture import (
    MAIN_COMMITS,
    RETRY_BRANCH_COMMITS,
    RETRY_BRANCH_NAME,
    RETRY_ENGINE_TODO_MARKER,
    CommitSpec,
    FixtureBuildResult,
)


@pytest.fixture
def parser() -> ASTParserService:
    return ASTParserService(max_parse_bytes=64 * 1024, allow_grammar_download=False)


def parse(parser: ASTParserService, path: str, source: str | bytes) -> FileInventory:
    data = source.encode("utf-8") if isinstance(source, str) else source
    return parser.parse_file(path, data)


def src(text: str) -> str:
    return textwrap.dedent(text).lstrip("\n")


def lines_of(source: str, start: int, end: int) -> str:
    return "\n".join(source.split("\n")[start - 1 : end])


# ---------------------------------------------------------------------------
# Python symbols
# ---------------------------------------------------------------------------
SERVICE_PY = src(
    '''
    """Billing service."""
    import functools


    class Billing:
        """Bills customers."""

        @functools.cache
        @staticmethod
        def total(amount: int,
                  tax: int = 0) -> int:
            return amount + tax

        class Config:
            retries = 3

            def describe(self):
                return "config"


    async def fetch(url: str) -> bytes:
        """Fetch a URL."""
        return b""
    '''
)


def test_decorated_method_async_function_and_nested_class_lines(parser: ASTParserService) -> None:
    inventory = parse(parser, "billing/service.py", SERVICE_PY)
    assert inventory.outcome.status is ParseStatus.PARSED

    total = inventory.symbol("Billing.total")
    assert total is not None and total.kind is EvidenceKind.METHOD
    # Decorated definitions start at the first decorator.
    assert (total.line_start, total.line_end) == (8, 12)
    assert total.snippet == lines_of(SERVICE_PY, 8, 12)
    assert total.snippet.startswith("    @functools.cache")
    assert total.decorators == ("functools.cache", "staticmethod")
    assert total.signature == "def total(amount: int, tax: int = 0) -> int"
    assert total.parent == "Billing"

    fetch = inventory.symbol("fetch")
    assert fetch is not None and fetch.kind is EvidenceKind.FUNCTION and fetch.is_async
    assert (fetch.line_start, fetch.line_end) == (21, 23)
    assert fetch.snippet == lines_of(SERVICE_PY, 21, 23)
    assert fetch.docstring == "Fetch a URL."

    config = inventory.symbol("Billing.Config")
    describe = inventory.symbol("Billing.Config.describe")
    assert config is not None and config.kind is EvidenceKind.CLASS
    assert (config.line_start, config.line_end) == (14, 18)
    assert describe is not None and describe.kind is EvidenceKind.METHOD
    assert (describe.line_start, describe.line_end) == (17, 18)

    billing = inventory.symbol("Billing")
    assert billing is not None and billing.docstring == "Bills customers."
    module = inventory.symbol("billing.service")
    assert module is not None and module.kind is EvidenceKind.MODULE_DOCSTRING and module.docstring == "Billing service."


def test_class_bases_and_module_constants(parser: ASTParserService) -> None:
    source = src(
        """
        MAX_RETRIES: int = 5
        _PRIVATE_LIMIT = 10
        not_a_constant = 1

        class Error(ValueError, metaclass=Meta):
            LOCAL = 1
        """
    )
    inventory = parse(parser, "pkg/errors.py", source)
    constants = {item.name: item for item in inventory.symbols if item.kind is EvidenceKind.CONSTANT}
    assert set(constants) == {"MAX_RETRIES", "_PRIVATE_LIMIT"}
    assert constants["MAX_RETRIES"].snippet == "MAX_RETRIES: int = 5"
    error = inventory.symbol("Error")
    assert error is not None and error.bases == ("ValueError", "metaclass=Meta")


def test_relative_aliased_and_local_imports(parser: ASTParserService) -> None:
    source = src(
        """
        from __future__ import annotations
        import os.path as osp, sys
        from ..core.models import Ledger as BaseLedger, Entry
        from . import utils
        from .helpers import *

        def send():
            import requests
            return requests
        """
    )
    imports = parse(parser, "pkg/sub/mod.py", source).imports
    by_name = {item.name: item for item in imports}
    assert set(by_name) == {"__future__", "os.path", "sys", "..core.models", ".", ".helpers", "requests"}

    assert by_name["os.path"].alias == "osp" and by_name["os.path"].line_start == 2
    assert by_name["sys"].alias is None and by_name["sys"].snippet == "import os.path as osp, sys"

    models = by_name["..core.models"]
    assert models.is_relative and models.level == 2 and models.module == "core.models"
    assert [(name.name, name.alias) for name in models.names] == [("Ledger", "BaseLedger"), ("Entry", None)]

    assert by_name["."].level == 1 and by_name["."].module == "" and by_name["."].names[0].name == "utils"
    assert by_name[".helpers"].names[0].name == "*"
    assert by_name["requests"].enclosing_symbol == "send" and by_name["requests"].line_start == 8


def test_route_decorators(parser: ASTParserService) -> None:
    source = src(
        """
        @router.post("/invoices/{invoice_id}/pay")
        def pay_invoice(request, invoice_id: int):
            return {}

        @app.route("/health", methods=["GET", "HEAD"])
        def health():
            return "ok"

        @functools.lru_cache(maxsize=None)
        def not_a_route():
            return 1
        """
    )
    routes = {route.handler: route for route in parse(parser, "api/views.py", source).routes}
    assert set(routes) == {"pay_invoice", "health"}
    pay = routes["pay_invoice"]
    assert pay.methods == ("POST",) and pay.path == "/invoices/{invoice_id}/pay" and pay.router == "router"
    assert (pay.line_start, pay.line_end) == (1, 2)
    assert pay.snippet == lines_of(source, 1, 2)
    assert routes["health"].methods == ("GET", "HEAD")


def test_not_implemented_stub_is_attributed_to_its_function(parser: ASTParserService) -> None:
    source = src(
        """
        class Exporter:
            def to_pdf(self):
                if self.ready:
                    return b""
                raise NotImplementedError("PDF export is not implemented")

            @abc.abstractmethod
            def render(self):
                raise NotImplementedError

            def ok(self):
                return NotImplemented
        """
    )
    inventory = parse(parser, "app/export.py", source)
    stubs = {stub.enclosing_symbol: stub for stub in inventory.stubs}
    assert set(stubs) == {"Exporter.to_pdf", "Exporter.render"}

    pdf = stubs["Exporter.to_pdf"]
    assert pdf.kind is EvidenceKind.STUB and pdf.line_start == pdf.line_end == 5
    assert pdf.snippet == '        raise NotImplementedError("PDF export is not implemented")'
    assert pdf.message == "PDF export is not implemented" and not pdf.in_abstract_method
    assert stubs["Exporter.render"].in_abstract_method and stubs["Exporter.render"].message is None

    to_pdf, render, ok = (inventory.symbol(name) for name in ("Exporter.to_pdf", "Exporter.render", "Exporter.ok"))
    assert to_pdf is not None and to_pdf.is_stub and not to_pdf.is_abstract
    assert render is not None and render.is_stub and render.is_abstract
    assert ok is not None and not ok.is_stub
    exporter = inventory.symbol("Exporter")
    assert exporter is not None and not exporter.is_stub


def test_test_functions_are_flagged_only_in_test_files(parser: ASTParserService) -> None:
    source = src(
        """
        def helper():
            pass

        def test_retries():
            def test_inner():
                pass

        class TestDispatcher:
            def test_signs(self):
                pass

        class Helper:
            def test_not_collected(self):
                pass
        """
    )
    flagged = {item.qualified_name for item in parse(parser, "tests/test_dispatch.py", source).symbols if item.is_test}
    assert flagged == {"test_retries", "TestDispatcher", "TestDispatcher.test_signs"}
    assert not any(item.is_test for item in parse(parser, "app/dispatch.py", source).symbols)
    assert any(item.is_test for item in parse(parser, "pkg/dispatch_test.py", source).symbols)


# ---------------------------------------------------------------------------
# Comment markers
# ---------------------------------------------------------------------------
def test_markers_come_from_comments_not_strings(parser: ASTParserService) -> None:
    source = src(
        '''
        """TODO: docstrings are strings, not comments."""
        LABEL = "TODO: not a comment either"


        def retry():
            # TODO(marcus): add jitter
            # and wire into the dispatcher
            value = "# FIXME inside a string"  # HACK: tolerate 4xx
            return value
        # XXX: module-level note
        '''
    )
    markers = parse(parser, "app/retry.py", source).markers
    assert [(m.tag, m.line_start, m.line_end) for m in markers] == [
        (MarkerTag.TODO, 6, 7),
        (MarkerTag.HACK, 8, 8),
        (MarkerTag.XXX, 10, 10),
    ]
    todo, hack, xxx = markers
    assert todo.text == "add jitter and wire into the dispatcher" and todo.owner == "marcus"
    assert todo.snippet == lines_of(source, 6, 7)
    assert todo.enclosing_symbol == "retry" and todo.detection is MarkerDetection.COMMENT_NODE
    assert hack.text == "tolerate 4xx" and hack.enclosing_symbol == "retry"
    # Written at column 0 after the function: tree-sitter may attach it to the body, but it is module-level.
    assert xxx.enclosing_symbol is None
    retry = parse(parser, "app/retry.py", source).symbol("retry")
    assert retry is not None and retry.line_end == 9


def test_crlf_and_non_ascii_line_numbers(parser: ASTParserService) -> None:
    source = src(
        """
        # Ünïcödé header: café ✓ 日本語
        PRICE = "€5"


        def convert(amount):  # 変換
            # TODO: support ¥ and ₹
            return amount
        """
    )
    lf = parse(parser, "app/fx.py", source)
    crlf = parse(parser, "app/fx.py", source.replace("\n", "\r\n").encode("utf-8"))
    for inventory in (lf, crlf):
        convert = inventory.symbol("convert")
        assert convert is not None and (convert.line_start, convert.line_end) == (5, 7)
        assert convert.snippet == lines_of(source, 5, 7)
        [marker] = inventory.markers
        assert (marker.line_start, marker.text) == (6, "support ¥ and ₹")
        assert marker.snippet == "    # TODO: support ¥ and ₹"
    assert lf.symbol("convert").fingerprint == crlf.symbol("convert").fingerprint  # type: ignore[union-attr]


def test_syntax_errors_still_yield_symbols(parser: ASTParserService) -> None:
    source = src(
        """
        def broken(:
            pass


        class Survivor:
            def works(self):
                return 1
        """
    )
    inventory = parse(parser, "app/broken.py", source)
    assert inventory.outcome.status is ParseStatus.PARSED_WITH_ERRORS
    assert inventory.outcome.has_syntax_errors and 1 in inventory.outcome.error_lines
    works = inventory.symbol("Survivor.works")
    assert works is not None and (works.line_start, works.line_end) == (6, 7)


# ---------------------------------------------------------------------------
# Skip rules and grammar fallback
# ---------------------------------------------------------------------------
def test_binary_oversized_and_vendored_files_are_skipped() -> None:
    parser = ASTParserService(max_parse_bytes=100, allow_grammar_download=False)
    cases = {
        "assets/logo.py": (b"\x89PNG\r\n\x1a\n\0\0\0", ParseStatus.SKIPPED_BINARY),
        "app/huge.py": (b"x = 1\n" * 50, ParseStatus.SKIPPED_TOO_LARGE),
        "node_modules/left-pad/index.js": (b"module.exports = 1;\n", ParseStatus.SKIPPED_VENDORED),
        ".venv/lib/site.py": (b"x = 1\n", ParseStatus.SKIPPED_VENDORED),
        "pkg/__pycache__/mod.py": (b"x = 1\n", ParseStatus.SKIPPED_VENDORED),
        "static/app.min.js": (b"var a=1;\n", ParseStatus.SKIPPED_VENDORED),
        "poetry.lock": (b"[[package]]\n", ParseStatus.SKIPPED_VENDORED),
    }
    for path, (data, status) in cases.items():
        inventory = parser.parse_file(path, data)
        assert inventory.outcome.status is status, path
        assert inventory.evidence == ()


def test_unknown_extension_is_unsupported_but_line_scanned(parser: ASTParserService) -> None:
    inventory = parse(parser, "deploy/run.sh", "#!/bin/sh\n# TODO: add healthcheck\necho TODO\n")
    assert inventory.outcome.status is ParseStatus.UNSUPPORTED_LANGUAGE and inventory.outcome.language is None
    [marker] = inventory.markers
    assert (marker.line_start, marker.text, marker.detection) == (2, "add healthcheck", MarkerDetection.LINE_SCAN)


def test_grammar_load_failure_degrades_to_grammar_unavailable(caplog: pytest.LogCaptureFixture) -> None:
    calls: list[str] = []

    def failing_loader(name: str) -> Language:
        calls.append(name)
        raise RuntimeError(f"simulated download failure for {name}")

    parser = ASTParserService(grammar_loader=failing_loader)
    source = "class Cart {}\n// FIXME: totals ignore tax\n"
    with caplog.at_level(logging.WARNING, logger="apps.ingestion.ast_parser"):
        first = parse(parser, "web/cart.ts", source)
        second = parse(parser, "web/other.ts", source)
    for inventory in (first, second):
        assert inventory.outcome.status is ParseStatus.GRAMMAR_UNAVAILABLE
        assert inventory.outcome.language == "typescript" and inventory.symbols == ()
        assert [(m.line_start, m.text) for m in inventory.markers] == [(2, "totals ignore tax")]
    assert calls == ["typescript"]  # the failure is remembered...
    assert len([r for r in caplog.records if "Grammar unavailable" in r.getMessage()]) == 1  # ...and logged once
    # Python never depends on the loader.
    assert parse(parser, "app/x.py", "def f():\n    pass\n").outcome.status is ParseStatus.PARSED


def test_uncached_grammar_is_unavailable_without_downloading(monkeypatch: pytest.MonkeyPatch) -> None:
    import tree_sitter_language_pack as pack

    def no_network(name: str) -> Language:
        raise AssertionError("get_language must not be called when downloads are disabled")

    monkeypatch.setattr(ast_parser, "cached_pack_languages", lambda: frozenset())
    monkeypatch.setattr(pack, "get_language", no_network)
    inventory = parse(ASTParserService(allow_grammar_download=False), "src/main.go", "package main\n")
    assert inventory.outcome.status is ParseStatus.GRAMMAR_UNAVAILABLE
    assert "INGEST_ALLOW_GRAMMAR_DOWNLOAD" in inventory.outcome.reason


def test_spec_driven_extraction_for_other_languages() -> None:
    # Exercised with the bundled Python grammar so no download is needed.
    spec = LanguageSpec(
        name="spec-test",
        class_nodes=frozenset({"class_definition"}),
        function_nodes=frozenset({"function_definition"}),
        import_nodes=frozenset({"import_statement"}),
    )
    source = "import os\nclass Shop:\n    def buy(self):\n        pass  # TODO: pay\n"
    inventory = extract_with_spec("shop.py", source.encode(), python_language(), spec)
    assert [(s.kind, s.qualified_name, s.line_start, s.line_end) for s in inventory.symbols] == [
        (EvidenceKind.CLASS, "Shop", 2, 4),
        (EvidenceKind.METHOD, "Shop.buy", 3, 4),
    ]
    assert [item.module for item in inventory.imports] == ["os"]
    [marker] = inventory.markers
    assert marker.enclosing_symbol == "Shop.buy" and marker.line_start == 4


# ---------------------------------------------------------------------------
# Markdown and manifests
# ---------------------------------------------------------------------------
README = src(
    """
    # Shop

    ## Features

    - [x] **PDF** Invoice Export
    - [ ] Multi-currency conversion
      with daily rates
    - Webhook retries

    ### Payments

    * Card payments

    ```bash
    # not a heading
    - [x] not a claim
    ```

    ## Install

    - pip install shop
    - [ ] Publish to PyPI
    """
)


def test_markdown_headings_and_feature_claims(parser: ASTParserService) -> None:
    inventory = parse(parser, "README.md", README)
    assert inventory.outcome.status is ParseStatus.PARSED and inventory.outcome.language == "markdown"
    assert [(h.level, h.text, h.line_start) for h in inventory.headings] == [
        (1, "Shop", 1), (2, "Features", 3), (3, "Payments", 10), (2, "Install", 19),
    ]
    claims = [(c.text, c.checked, c.style, c.line_start, c.line_end, c.in_feature_section) for c in inventory.claims]
    assert claims == [
        ("**PDF** Invoice Export", True, ClaimStyle.CHECKLIST, 5, 5, True),
        ("Multi-currency conversion with daily rates", False, ClaimStyle.CHECKLIST, 6, 7, True),
        ("Webhook retries", None, ClaimStyle.BULLET, 8, 8, True),
        ("Card payments", None, ClaimStyle.BULLET, 12, 12, True),
        ("Publish to PyPI", False, ClaimStyle.CHECKLIST, 22, 22, False),
    ]
    first = inventory.claims[0]
    assert first.snippet == "- [x] **PDF** Invoice Export" and first.section == "Features"
    assert inventory.claims[1].snippet == lines_of(README, 6, 7)
    # Ticking a box does not change the claim's identity.
    ticked = parse(parser, "README.md", README.replace("- [ ] Multi-currency", "- [x] Multi-currency"))
    assert ticked.claims[1].checked is True
    assert ticked.claims[1].fingerprint == inventory.claims[1].fingerprint


def test_requirements_txt(parser: ASTParserService) -> None:
    source = src(
        """
        # runtime
        -r base.txt
        requests==2.25.1  # pinned for TLS
        Django>=4.2,<5 ; python_version >= "3.10"
        celery[redis] == 5.3.6 \\
            --hash=sha256:abc
        numpy==1.*
        """
    )
    deps = parse(parser, "requirements-dev.txt", source).dependencies
    assert [(d.name, d.specifier, d.pinned_version, d.line_start, d.line_end) for d in deps] == [
        ("requests", "==2.25.1", "2.25.1", 3, 3),
        ("Django", ">=4.2,<5", None, 4, 4),
        ("celery", "== 5.3.6", "5.3.6", 5, 6),
        ("numpy", "==1.*", None, 7, 7),
    ]
    assert deps[0].snippet == "requests==2.25.1  # pinned for TLS"
    assert deps[0].manifest is ManifestKind.REQUIREMENTS_TXT and deps[0].line == 3
    assert deps[1].normalized_name == "django"


def test_pyproject_pep621_and_poetry(parser: ASTParserService) -> None:
    source = src(
        """
        [project]
        name = "shop"
        dependencies = [
            "requests==2.31.0",
            "pydantic>=2",
        ]

        [project.optional-dependencies]
        pdf = ["reportlab~=4.0"]

        [tool.poetry.dependencies]
        python = "^3.11"
        httpx = "0.27.0"
        rich = { version = "^13.0", optional = true }

        [tool.poetry.group.dev.dependencies]
        pytest = "^8"
        """
    )
    deps = parse(parser, "pyproject.toml", source).dependencies
    assert [(d.name, d.specifier, d.pinned_version, d.group, d.line_start) for d in deps] == [
        ("requests", "==2.31.0", "2.31.0", "main", 4),
        ("pydantic", ">=2", None, "main", 5),
        ("reportlab", "~=4.0", None, "pdf", 9),
        ("httpx", "0.27.0", "0.27.0", "main", 13),
        ("rich", "^13.0", None, "main", 14),
        ("pytest", "^8", None, "dev", 17),
    ]
    assert all(d.manifest is ManifestKind.PYPROJECT_TOML for d in deps)


def test_package_json(parser: ASTParserService) -> None:
    source = src(
        """
        {
          "name": "web",
          "dependencies": {
            "react": "19.0.0",
            "next": "^15.1.0"
          },
          "devDependencies": {
            "typescript": "~5.6.2"
          }
        }
        """
    )
    deps = parse(parser, "frontend/package.json", source).dependencies
    assert [(d.name, d.specifier, d.pinned_version, d.group, d.line_start) for d in deps] == [
        ("react", "19.0.0", "19.0.0", "dependencies", 4),
        ("next", "^15.1.0", None, "dependencies", 5),
        ("typescript", "~5.6.2", None, "devDependencies", 8),
    ]
    assert deps[0].ecosystem is Ecosystem.NPM and deps[0].snippet == '    "react": "19.0.0",'
    broken = parse(parser, "package.json", "{ not json")
    assert broken.outcome.status is ParseStatus.PARSED_WITH_ERRORS and broken.dependencies == ()


def test_setup_cfg(parser: ASTParserService) -> None:
    source = src(
        """
        [metadata]
        name = shop

        [options]
        install_requires =
            requests==2.25.1
            click>=8

        [options.extras_require]
        pdf =
            reportlab
        """
    )
    deps = parse(parser, "setup.cfg", source).dependencies
    assert [(d.name, d.specifier, d.pinned_version, d.group, d.line_start) for d in deps] == [
        ("requests", "==2.25.1", "2.25.1", "main", 6),
        ("click", ">=8", None, "main", 7),
        ("reportlab", "", None, "pdf", 11),
    ]


# ---------------------------------------------------------------------------
# Fingerprints
# ---------------------------------------------------------------------------
def test_fingerprints_survive_inserted_lines(parser: ASTParserService) -> None:
    source = src(
        """
        import os

        class Ledger:
            def post(self):
                # TODO: validate currency
                raise NotImplementedError
        """
    )
    shifted = "# header\n\n\nVERSION = 2\n" + source
    before, after = parse(parser, "pkg/ledger.py", source), parse(parser, "pkg/ledger.py", shifted)
    assert len(before.evidence) == 5  # import, class, method, marker, stub
    before_by_fp = {item.fingerprint: item for item in before.evidence}
    after_by_fp = {item.fingerprint: item for item in after.evidence}
    assert set(before_by_fp) <= set(after_by_fp)
    for fingerprint, old in before_by_fp.items():
        new = after_by_fp[fingerprint]
        assert (new.kind, new.name, new.line_start, new.snippet) == (old.kind, old.name, old.line_start + 4, old.snippet)
    # Same name in another file, or a different kind of thing, is different evidence.
    other = parse(parser, "pkg/other.py", source)
    assert {item.fingerprint for item in other.evidence}.isdisjoint(before_by_fp)
    assert re.fullmatch(r"[0-9a-f]{32}", next(iter(before_by_fp)))


def test_snippets_are_capped_with_a_truncation_flag(parser: ASTParserService) -> None:
    body = "".join(f"    x{i} = {i}\n" for i in range(200))
    inventory = parse(parser, "app/long.py", "def long():\n" + body)
    long = inventory.symbol("long")
    assert long is not None and (long.line_start, long.line_end) == (1, 201)
    assert long.snippet_truncated and long.snippet.startswith("def long():\n    x0 = 0")
    assert len(long.snippet.split("\n")) < 201


# ---------------------------------------------------------------------------
# PayPulse (Patient Zero)
# ---------------------------------------------------------------------------
def last_version(commits: tuple[CommitSpec, ...], path: str) -> str:
    return next(commit.writes[path] for commit in reversed(commits) if path in commit.writes)


def line_of(text: str, needle: str) -> int:
    return next(number for number, line in enumerate(text.split("\n"), start=1) if needle in line)


@pytest.fixture(scope="module")
def paypulse_service(paypulse_repo: FixtureBuildResult) -> Iterator[GitForensicService]:
    with GitForensicService(paypulse_repo.repo_path) as service:
        yield service


@pytest.fixture(scope="module")
def main_inventory(paypulse_service: GitForensicService) -> RevisionInventory:
    return parse_revision(paypulse_service, "main", parser=ASTParserService(allow_grammar_download=False))


@pytest.fixture(scope="module")
def retry_inventory(paypulse_service: GitForensicService) -> RevisionInventory:
    branch = paypulse_service.inspect_branch(RETRY_BRANCH_NAME)
    return parse_branch_changes(paypulse_service, branch, parser=ASTParserService(allow_grammar_download=False))


@pytest.mark.slow
def test_paypulse_main_code_evidence(main_inventory: RevisionInventory, paypulse_repo: FixtureBuildResult) -> None:
    assert main_inventory.commit_sha == paypulse_repo.head_sha and main_inventory.scope is InventoryScope.FULL_TREE
    assert not any(item.outcome.has_syntax_errors for item in main_inventory.files)

    post = main_inventory.symbol("Ledger.post_transaction", "paypulse/ledger.py")
    ledger_py = last_version(MAIN_COMMITS, "paypulse/ledger.py")
    assert post is not None and post.kind is EvidenceKind.METHOD
    assert post.line_start == line_of(ledger_py, "def post_transaction(")
    assert post.snippet == lines_of(ledger_py, post.line_start, post.line_end)

    currency = main_inventory.file("paypulse/currency.py")
    currency_py = last_version(MAIN_COMMITS, "paypulse/currency.py")
    assert currency is not None
    [stub] = currency.stubs
    assert stub.enclosing_symbol == "convert" and stub.line_start == line_of(currency_py, "raise NotImplementedError")
    assert currency.symbol("convert").is_stub  # type: ignore[union-attr]
    markers = {marker.tag: marker for marker in currency.markers}
    assert markers[MarkerTag.TODO].enclosing_symbol == "convert"
    assert markers[MarkerTag.TODO].line_start == line_of(currency_py, "# TODO: fetch daily FX rates")
    assert markers[MarkerTag.FIXME].line_start == line_of(currency_py, "# FIXME: only USD is listed")

    webhooks_py = last_version(MAIN_COMMITS, "paypulse/webhooks.py")
    [fixme] = main_inventory.file("paypulse/webhooks.py").markers  # type: ignore[union-attr]
    assert fixme.tag is MarkerTag.FIXME and fixme.enclosing_symbol == "WebhookDispatcher._deliver"
    assert fixme.line_start == line_of(webhooks_py, "# FIXME: single attempt only")
    assert fixme.line_end == fixme.line_start + 1 and "feat/webhook-retry-engine" in fixme.text

    backoff = main_inventory.symbol("test_dispatch_retries_with_exponential_backoff", "tests/test_webhooks.py")
    assert backoff is not None and backoff.is_test


@pytest.mark.slow
def test_paypulse_main_docs_and_dependencies(main_inventory: RevisionInventory) -> None:
    readme = main_inventory.file("README.md")
    assert readme is not None
    claims = {claim.text: claim for claim in readme.claims}
    for wanted in ("Multi-currency conversion", "Automated Exponential Backoff Webhook Retries", "PDF Invoice Export"):
        claim = next(claim for text, claim in claims.items() if text.startswith(wanted))
        assert claim.checked is True and claim.section == "Features" and claim.snippet == f"- [x] {claim.text}"
    assert {heading.text for heading in main_inventory.file("docs/architecture-notes.md").headings} >= {  # type: ignore[union-attr]
        "Webhook retry engine", "PDF invoice export",
    }

    pins = {dep.name: dep for dep in main_inventory.dependencies if dep.file_path == "requirements.txt"}
    assert pins["pytest"].pinned_version == "6.2.4" and pins["pytest"].snippet == "pytest==6.2.4"
    assert pins["requests"].pinned_version == "2.25.1" and pins["requests"].line_start == 2


@pytest.mark.slow
def test_paypulse_main_has_no_pdf_export_code(main_inventory: RevisionInventory) -> None:
    """Evidence of absence for the README's "PDF Invoice Export" ghost feature."""
    ghost = re.compile(r"pdf|export", re.IGNORECASE)
    code_files = [item for item in main_inventory.files if item.outcome.language == "python"]
    assert code_files
    assert not [item.file_path for item in main_inventory.files if ghost.search(item.file_path)]
    for item in code_files:
        names = [*(s.qualified_name for s in item.symbols), *(i.name for i in item.imports), *(r.path for r in item.routes)]
        assert not [name for name in names if ghost.search(name)], item.file_path


@pytest.mark.slow
def test_paypulse_retry_branch_diff(retry_inventory: RevisionInventory, paypulse_repo: FixtureBuildResult) -> None:
    assert retry_inventory.scope is InventoryScope.BRANCH_DIFF
    assert retry_inventory.commit_sha == paypulse_repo.branch_heads[RETRY_BRANCH_NAME]
    assert {item.file_path for item in retry_inventory.files} == {"paypulse/retry.py", "tests/test_retry.py"}

    retry = retry_inventory.file("paypulse/retry.py")
    retry_py = last_version(RETRY_BRANCH_COMMITS, "paypulse/retry.py")
    assert retry is not None
    policy, call = retry.symbol("RetryPolicy"), retry.symbol("call_with_retry")
    assert policy is not None and policy.kind is EvidenceKind.CLASS
    assert call is not None and call.kind is EvidenceKind.FUNCTION
    assert call.signature.startswith("def call_with_retry(")

    [todo] = retry.markers
    assert todo.line_start == todo.line_end == line_of(retry_py, RETRY_ENGINE_TODO_MARKER)
    assert todo.snippet.strip() == RETRY_ENGINE_TODO_MARKER
    assert todo.text == "complete backoff jitter and wire into dispatcher"
    assert todo.enclosing_symbol == "RetryPolicy.delay_for"

    [stub] = retry.stubs
    assert stub.enclosing_symbol == "RetryPolicy.delay_for" and stub.line_start == todo.line_start + 1
    assert stub.message == "backoff jitter is not implemented yet"
    assert retry.symbol("RetryPolicy.delay_for").is_stub  # type: ignore[union-attr]
