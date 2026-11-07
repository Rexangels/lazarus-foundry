"""Synthetic ingestion data for the fast genome tests (no Git, no database).

``toy_artifacts()`` mirrors the shape ``apps.ingestion.pipeline`` produces for
a small repository modeled on PayPulse:

* ``README.md`` (main) claims ``[x] Ledger postings``, ``[x] Webhook retries``,
  ``[x] PDF export``, ``[x] Currency conversion`` and ``[ ] Dark mode``.
* ``pkg/ledger.py`` implements the ledger; ``tests/test_ledger.py`` tests it.
* ``pkg/currency.py`` has ``convert`` raising ``NotImplementedError`` plus a TODO.
* ``pkg/webhooks.py`` has a working ``WebhookDispatcher`` and a FIXME about retries.
* ``docs/design.md`` has a "PDF export" section.
* ``feat/webhook-retries`` (unmerged, dormant 300 days) adds ``pkg/retry.py``.
"""
from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from apps.genome.catalog import ArtifactInput, BranchInfo, EvidenceCatalog, RepositoryContext, build_catalog
from apps.ingestion.models import ArtifactType
from apps.ingestion.pipeline import ArtifactRow, BranchRow

MAIN = "main"
BRANCH = "feat/webhook-retries"
MAIN_SHA = "a" * 40
BRANCH_SHA = "b" * 40
BRANCH_DORMANCY_DAYS = 300

README_TEXT = """\
# Toy

- [x] Ledger postings
- [x] Webhook retries
- [x] PDF export
- [x] Currency conversion
- [ ] Dark mode
"""

DESIGN_TEXT = """\
# Design

## PDF export

Invoices render to PDF through pkg/export/pdf.py.

## Unrelated notes
"""

RETRY_PATCH = """\
--- /dev/null
+++ b/pkg/retry.py
@@ -0,0 +1,3 @@
+class RetryPolicy:
+    # TODO: finish retry jitter
+    pass
"""


def fp(*parts: object) -> str:
    return ":".join(str(part) for part in parts)


def code(name: str, line: int, *, path: str, kind: str = "FUNCTION", end: int | None = None, **extra: Any) -> dict[str, Any]:
    qualified = extra.pop("qualified_name", name)
    return {
        "kind": kind,
        "name": name.rsplit(".", 1)[-1],
        "qualified_name": qualified,
        "parent": extra.pop("parent", qualified.rsplit(".", 1)[0] if "." in qualified else None),
        "file_path": path,
        "line_start": line,
        "line_end": end or line,
        "snippet": extra.pop("snippet", f"def {name}(): ..."),
        "snippet_truncated": False,
        "fingerprint": fp(kind, path, qualified),
        **extra,
    }


def ast_module(
    path: str,
    symbols: Sequence[dict[str, Any]],
    *,
    stubs: Sequence[dict[str, Any]] = (),
    branch: str = MAIN,
    commit: str = MAIN_SHA,
    pk: int | None = None,
) -> ArtifactInput:
    return ArtifactInput(
        artifact_type=ArtifactType.AST_MODULE,
        file_path=path,
        branch_name=branch,
        raw_content="",
        structured_metadata={"commit_sha": commit, "symbols": list(symbols), "stubs": list(stubs), "routes": []},
        pk=pk,
    )


def marker(path: str, line: int, text: str, *, tag: str = "TODO", branch: str = MAIN, commit: str = MAIN_SHA,
           enclosing: str | None = None) -> ArtifactInput:
    snippet = f"# {tag}: {text}"
    return ArtifactInput(
        artifact_type=ArtifactType.TODO_MARKER,
        file_path=path,
        branch_name=branch,
        raw_content=snippet,
        structured_metadata={
            "kind": "MARKER", "name": f"{tag}: {text}", "file_path": path, "line_start": line, "line_end": line,
            "snippet": snippet, "snippet_truncated": False, "fingerprint": fp("MARKER", path, tag, text),
            "tag": tag, "text": text, "owner": None, "enclosing_symbol": enclosing, "commit_sha": commit,
        },
    )


def readme(text: str = README_TEXT, *, path: str = "README.md") -> ArtifactInput:
    claims = []
    for number, line in enumerate(text.split("\n"), start=1):
        if line.startswith("- ["):
            claim = line[6:]
            claims.append(
                {
                    "kind": "DOC_CLAIM", "name": claim, "file_path": path, "line_start": number, "line_end": number,
                    "snippet": line, "snippet_truncated": False, "fingerprint": fp("DOC_CLAIM", path, claim),
                    "text": claim, "checked": line[3] == "x", "style": "CHECKLIST", "section": None,
                    "in_feature_section": False,
                }
            )
    return ArtifactInput(
        artifact_type=ArtifactType.README_DOC,
        file_path=path,
        branch_name=MAIN,
        raw_content=text,
        structured_metadata={"commit_sha": MAIN_SHA, "claims": claims, "headings": [
            {"kind": "HEADING", "name": "Toy", "file_path": path, "line_start": 1, "line_end": 1, "snippet": "# Toy",
             "snippet_truncated": False, "fingerprint": fp("HEADING", path, "toy"), "level": 1, "text": "Toy"},
        ]},
    )


def design_doc() -> ArtifactInput:
    path = "docs/design.md"
    headings = [
        (1, 1, "Design"),
        (3, 2, "PDF export"),
        (7, 2, "Unrelated notes"),
    ]
    return ArtifactInput(
        artifact_type=ArtifactType.README_DOC,
        file_path=path,
        branch_name=MAIN,
        raw_content=DESIGN_TEXT,
        structured_metadata={
            "commit_sha": MAIN_SHA,
            "claims": [],
            "headings": [
                {"kind": "HEADING", "name": text, "file_path": path, "line_start": line, "line_end": line,
                 "snippet": DESIGN_TEXT.split("\n")[line - 1], "snippet_truncated": False,
                 "fingerprint": fp("HEADING", path, text), "level": level, "text": text}
                for line, level, text in headings
            ],
        },
    )


def commit_log() -> ArtifactInput:
    return ArtifactInput(
        artifact_type=ArtifactType.COMMIT_LOG,
        file_path="",
        branch_name=BRANCH,
        raw_content=RETRY_PATCH,
        structured_metadata={
            "branch": BRANCH, "head_sha": BRANCH_SHA, "is_merged": False, "is_abandoned": True,
            "dormancy_days": BRANCH_DORMANCY_DAYS,
            "commits": [{"sha": "c" * 40, "subject": "Add retry policy"}],
            "diff": {"files": [{"path": "pkg/retry.py", "change_type": "ADDED", "additions": 3, "deletions": 0,
                                "patch_truncated": False}]},
        },
    )


def toy_artifacts(readme_text: str = README_TEXT) -> list[ArtifactInput]:
    ledger = "pkg/ledger.py"
    currency = "pkg/currency.py"
    webhooks = "pkg/webhooks.py"
    tests = "tests/test_ledger.py"
    retry = "pkg/retry.py"
    return [
        readme(readme_text),
        design_doc(),
        ast_module(ledger, [
            code("Ledger", 1, path=ledger, kind="CLASS", end=20, docstring="An in-memory ledger."),
            code("Ledger.post", 5, path=ledger, kind="METHOD", end=10),
        ]),
        ast_module(tests, [code("test_post", 1, path=tests, end=3, is_test=True)]),
        ast_module(currency, [
            code("normalize", 1, path=currency, end=3),
            code("convert", 5, path=currency, end=9, is_stub=True),
        ], stubs=[{
            "kind": "STUB", "name": "convert", "file_path": currency, "line_start": 9, "line_end": 9,
            "snippet": "    raise NotImplementedError", "snippet_truncated": False,
            "fingerprint": fp("STUB", currency, "convert"), "enclosing_symbol": "convert",
            "exception": "NotImplementedError", "message": None, "in_abstract_method": False,
        }]),
        marker(currency, 8, "support EUR and GBP", enclosing="convert"),
        ast_module(webhooks, [
            code("WebhookDispatcher", 1, path=webhooks, kind="CLASS", end=30),
            code("WebhookDispatcher.dispatch", 5, path=webhooks, kind="METHOD", end=12),
        ]),
        marker(webhooks, 10, "single attempt only; retries live on feat/webhook-retries", tag="FIXME"),
        ast_module(retry, [code("RetryPolicy", 1, path=retry, kind="CLASS", end=3)], branch=BRANCH, commit=BRANCH_SHA),
        marker(retry, 2, "finish retry jitter", branch=BRANCH, commit=BRANCH_SHA),
        commit_log(),
    ]


def toy_context() -> RepositoryContext:
    when = datetime(2023, 1, 1, tzinfo=UTC).isoformat()
    return RepositoryContext.build([
        BranchInfo(MAIN, MAIN_SHA, True, False, False, 0.0, 0, 0, when),
        BranchInfo(BRANCH, BRANCH_SHA, False, False, True, float(BRANCH_DORMANCY_DAYS), 1, 4, when),
    ])


def toy_catalog(readme_text: str = README_TEXT) -> EvidenceCatalog:
    return build_catalog(toy_artifacts(readme_text), toy_context())


def toy_ingestion_rows() -> tuple[list[BranchRow], list[ArtifactRow]]:
    """The toy data as pipeline rows, for ``persist_ingestion`` in database tests."""
    when = datetime(2023, 1, 1, tzinfo=UTC)
    branches = [
        BranchRow(MAIN, MAIN_SHA, when, False, 0, 0, {"is_default": True, "is_merged": False, "dormancy_days": 0}),
        BranchRow(BRANCH, BRANCH_SHA, when, True, 1, 4,
                  {"is_default": False, "is_merged": False, "dormancy_days": BRANCH_DORMANCY_DAYS}),
    ]
    artifacts = [
        ArtifactRow(item.artifact_type, item.file_path, item.branch_name, item.raw_content, dict(item.structured_metadata))
        for item in toy_artifacts()
    ]
    return branches, artifacts
