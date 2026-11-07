"""Tests for apps.genome.catalog on synthetic ingestion rows (no Git, no database)."""
from __future__ import annotations

from dataclasses import replace

from apps.genome.catalog import CatalogKind, build_catalog, is_test_path, split_patches
from apps.genome.models import EvidenceSourceType
from tests.genome_helpers import (
    BRANCH,
    BRANCH_DORMANCY_DAYS,
    BRANCH_SHA,
    DESIGN_TEXT,
    MAIN_SHA,
    ast_module,
    code,
    toy_artifacts,
    toy_catalog,
    toy_context,
)


def test_ids_are_sequential_and_independent_of_row_order() -> None:
    forward = build_catalog(toy_artifacts(), toy_context())
    backward = build_catalog(list(reversed(toy_artifacts())), toy_context())

    assert [item.id for item in forward.items] == [f"E{n}" for n in range(1, len(forward) + 1)]
    assert [(item.id, item.fingerprint) for item in forward.items] == [
        (item.id, item.fingerprint) for item in backward.items
    ]
    # Default-branch items come first, then branch-only items.
    flags = [item.observed_on_default_branch for item in forward.items]
    assert flags == sorted(flags, reverse=True)


def test_source_type_mapping() -> None:
    catalog = toy_catalog()

    def one(**criteria: object):
        [item] = catalog.find(**criteria)
        return item

    assert one(kind=CatalogKind.DOC_CLAIM, name="PDF export").source_type is EvidenceSourceType.README_CLAIM
    assert one(kind=CatalogKind.DOC_SECTION, name="PDF export").source_type is EvidenceSourceType.ISSUE_SPEC
    ledger = one(name="Ledger")
    assert ledger.source_type is EvidenceSourceType.AST_IMPLEMENTATION and ledger.age_days == 0.0
    assert ledger.commit_sha == MAIN_SHA and ledger.is_head_implementation
    assert one(name="test_post").is_test
    stub = one(kind=CatalogKind.STUB)
    assert stub.is_head_stub and stub.source_type is EvidenceSourceType.AST_IMPLEMENTATION
    assert one(kind=CatalogKind.MARKER, file_path="pkg/currency.py").source_type is EvidenceSourceType.TODO_COMMENT

    branch_code = one(name="RetryPolicy")
    assert branch_code.source_type is EvidenceSourceType.UNMERGED_BRANCH_COMMIT
    assert (branch_code.observed_on_default_branch, branch_code.age_days) == (False, BRANCH_DORMANCY_DAYS)
    branch_marker = one(kind=CatalogKind.MARKER, branch_name=BRANCH)
    assert branch_marker.source_type is EvidenceSourceType.TODO_COMMENT  # markers stay TODO_COMMENT on branches
    assert branch_marker.age_days == BRANCH_DORMANCY_DAYS
    diff = one(kind=CatalogKind.BRANCH_FILE)
    assert (diff.file_path, diff.commit_sha, diff.line_start) == ("pkg/retry.py", BRANCH_SHA, None)
    assert diff.snippet.startswith("--- /dev/null\n+++ b/pkg/retry.py")
    commit = one(kind=CatalogKind.BRANCH_COMMIT)
    assert (commit.snippet, commit.file_path) == ("Add retry policy", "")
    # README headings are page structure, not evidence.
    assert not catalog.find(kind=CatalogKind.DOC_SECTION, file_path="README.md")


def test_doc_sections_copy_the_exact_section_text() -> None:
    [section] = toy_catalog().find(kind=CatalogKind.DOC_SECTION, name="PDF export")
    lines = DESIGN_TEXT.split("\n")
    assert (section.line_start, section.line_end) == (3, 5)  # trailing blank line trimmed
    assert section.snippet == "\n".join(lines[2:5])


def test_branch_records_that_exist_at_head_are_not_branch_only() -> None:
    shared = code("Ledger", 1, path="pkg/ledger.py", kind="CLASS", end=20)
    artifacts = toy_artifacts() + [ast_module("pkg/ledger.py", [shared], branch=BRANCH, commit=BRANCH_SHA)]

    catalog = build_catalog(artifacts, toy_context())

    assert [item.branch_name for item in catalog.find(name="Ledger")] == ["main"]


def test_merged_branches_contribute_nothing() -> None:
    context = toy_context()
    merged = replace(context, branches=tuple(replace(b, is_merged=True) if b.name == BRANCH else b for b in context.branches))

    catalog = build_catalog(toy_artifacts(), merged)

    assert all(item.observed_on_default_branch for item in catalog.items)


def test_split_patches_and_test_paths() -> None:
    combined = (
        "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n"
        "--- a/gone.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-x\n"
    )
    patches = split_patches(combined)
    assert set(patches) == {"x.py", "gone.py"}
    assert patches["x.py"].endswith("+b") and patches["gone.py"].startswith("--- a/gone.py")
    assert is_test_path("tests/test_x.py") and is_test_path("pkg/x_test.py") and is_test_path("conftest.py")
    assert not is_test_path("pkg/testing_utils.py")
