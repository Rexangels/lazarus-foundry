"""Tests for the Lazarus Forensic MCP server (``backend/mcp_server/lazarus_mcp.py``).

Fast tests use the synthetic toy ingestion of ``tests/genome_helpers.py`` (no Git) and an
in-memory MCP client. ``test_stdio_end_to_end_on_paypulse`` (slow) starts the server as a stdio
subprocess through the MCP client SDK and calls every tool on a private PayPulse build; run it
with ``-s`` to see the evidence. The ``project_id`` test needs PostgreSQL and is skipped by
conftest.py when it is unreachable.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

import anyio
import pytest
from mcp import Client, StdioServerParameters, stdio_client
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult

from apps.genome.agents import GenomeReconcilerAgent
from apps.genome.graph import build_genome_graph_from_inputs
from apps.genome.services import load_catalog, persist_genome
from apps.ingestion.persistence import persist_ingestion
from apps.ingestion.pipeline import IngestionResult
from apps.projects.git_runner import run_git
from apps.projects.models import Project, ProjectStatus
from apps.rebirth.builder import spec_documents
from apps.revival.models import RevivalStrategy, StrategyType
from apps.revival.services import generate_strategies
from config.paths import REPO_ROOT
from mcp_server import lazarus_mcp as server
from tests.fixtures.build_paypulse_fixture import (
    PAYPULSE_FIXTURE_NAME,
    RETRY_BRANCH_NAME,
    RETRY_ENGINE_TODO_MARKER,
    build_paypulse_fixture,
)
from tests.genome_helpers import BRANCH, MAIN, commit_log, toy_ingestion_rows

TOOL_NAMES = {"inspect_dead_branches", "query_project_genome", "detect_doc_code_conflicts", "export_kiro_revival_spec"}
SPEC_DOCS = ("requirements.md", "design.md", "tasks.md")
TOY_SPEC_DIR = ".kiro/specs/lazarus-restore-toy"
E2E_TIMEOUT_SECONDS = 300
E2E_REQUEST_TIMEOUT_SECONDS = 240


@pytest.fixture
def roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "allowed"
    root.mkdir()
    monkeypatch.setenv(server.ALLOWED_ROOTS_ENV, str(root))
    return root


def fake_repo(parent: Path, name: str = "repo") -> Path:
    """A directory that passes the work-tree check (the fast tests never run Git in it)."""
    repo = parent / name
    (repo / ".git").mkdir(parents=True)
    return repo


@pytest.fixture(scope="module")
def toy_analysis(tmp_path_factory: pytest.TempPathFactory) -> server.RepositoryAnalysis:
    branches, artifacts = toy_ingestion_rows()
    ingestion = IngestionResult(MAIN, tuple(branches), tuple(artifacts), {})
    return server.build_analysis(tmp_path_factory.mktemp("toy") / "toy", ingestion)


@pytest.fixture(scope="module")
def toy_graph(toy_analysis: server.RepositoryAnalysis) -> Any:
    return build_genome_graph_from_inputs(toy_analysis.genome)


# ---------------------------------------------------------------------------
# Paths and sources
# ---------------------------------------------------------------------------
def test_repo_path_inside_an_allowed_root_resolves(roots: Path) -> None:
    repo = fake_repo(roots)
    assert server.resolve_repo_path(str(repo)) == repo.resolve()
    assert server.resolve_repo_path("repo") == repo.resolve()  # relative to the first root


def test_repo_path_outside_the_allowed_roots_is_refused(roots: Path, tmp_path: Path) -> None:
    outside = fake_repo(tmp_path, "outside")
    for value in (str(outside), str(roots / ".." / "outside"), "../outside"):
        with pytest.raises(ToolError, match="outside the allowed roots"):
            server.resolve_repo_path(value)


def test_a_link_out_of_an_allowed_root_is_refused(roots: Path, tmp_path: Path) -> None:
    outside = fake_repo(tmp_path, "outside")
    link = roots / "link"
    if sys.platform == "win32":
        import _winapi

        _winapi.CreateJunction(str(outside), str(link))  # no admin rights needed, unlike symlinks
    else:
        link.symlink_to(outside, target_is_directory=True)
    try:
        with pytest.raises(ToolError, match="outside the allowed roots"):
            server.resolve_repo_path(str(link))
    finally:
        os.rmdir(link) if sys.platform == "win32" else link.unlink()


@pytest.mark.parametrize(
    ("value", "message"),
    [("", "non-empty"), ("repo\x00", "control characters"), ("missing", "not an existing directory")],
)
def test_malformed_or_missing_repo_paths_are_refused(roots: Path, value: str, message: str) -> None:
    fake_repo(roots)
    with pytest.raises(ToolError, match=message):
        server.resolve_repo_path(value)


def test_a_directory_without_git_is_refused(roots: Path) -> None:
    (roots / "plain").mkdir()
    with pytest.raises(ToolError, match="not the top of a Git work tree"):
        server.resolve_repo_path("plain")


def test_read_tools_need_exactly_one_source() -> None:
    for kwargs in ({}, {"repo_path": "repo", "project_id": str(uuid4())}):
        with pytest.raises(ToolError, match="exactly one"):
            server.query_project_genome(**kwargs)
    with pytest.raises(ToolError, match="not a UUID"):
        server.parse_project_id("not-a-uuid")


def test_export_refuses_an_unknown_strategy(roots: Path) -> None:
    repo = fake_repo(roots)
    with pytest.raises(ToolError, match="strategy must be one of"):
        server.export_kiro_revival_spec(repo_path=str(repo), strategy="RESURRECT")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Results built from the toy ingestion
# ---------------------------------------------------------------------------
def test_dead_branch_report_from_a_commit_log(monkeypatch: pytest.MonkeyPatch) -> None:
    log = commit_log()
    default = server.default_branch_view(MAIN, "a" * 40, None, {"total_commit_count": 7})
    logs = {BRANCH: (log.structured_metadata, log.raw_content)}
    source = server.repository_source(Path("toy"))

    report = server.dead_branch_report(source, default, logs, branch=None, include_merged=False,
                                       include_patches=True)

    [branch] = report["branches"]
    assert branch["name"] == BRANCH and branch["is_abandoned"] and branch["dormancy_days"] == 300
    [changed] = branch["diff"]["files"]
    assert changed["path"] == "pkg/retry.py" and changed["change_type"] == "ADDED"
    assert "+class RetryPolicy:" in changed["patch"]
    assert report["unmerged_count"] == report["abandoned_count"] == 1 and report["patches_omitted"] == 0
    assert report["default_branch"]["commit_count"] == 7

    monkeypatch.setattr(server, "MAX_PATCH_CHARS_TOTAL", 10)
    capped = server.dead_branch_report(source, default, logs, branch=BRANCH, include_merged=False,
                                       include_patches=True)
    assert capped["branches"][0]["diff"]["files"][0]["patch"] is None and capped["patches_omitted"] == 1
    with pytest.raises(ToolError, match="is the default branch"):
        server.dead_branch_report(source, default, logs, branch=MAIN, include_merged=False, include_patches=False)
    with pytest.raises(ToolError, match="No non-default branch"):
        server.dead_branch_report(source, default, logs, branch="feat/none", include_merged=False,
                                  include_patches=False)


def test_genome_report_lists_features_with_line_cited_evidence(toy_analysis: server.RepositoryAnalysis,
                                                               toy_graph: Any) -> None:
    source = server.repository_source(toy_analysis.repo_path)

    report = server.genome_report(source, toy_graph, feature=None, include_snippets=True, include_graph=False)

    features = {feature["title"]: feature for feature in report["features"]}
    assert set(features) == {"Ledger postings", "Webhook retries", "PDF export", "Currency conversion", "Dark mode",
                             "pkg.webhooks module"}
    assert report["synthesis"]["mode"] == "DETERMINISTIC" and "graph" not in report
    ledger = features["Ledger postings"]
    assert (ledger["vital_status"], ledger["epistemic_status"]) == ("VERIFIED_WORKING", "KNOWN")
    cited = {(citation["source_type"], citation["location"]) for citation in ledger["citations"]}
    assert {("AST_IMPLEMENTATION", "pkg/ledger.py:1-20"), ("README_CLAIM", "README.md:3")} <= cited
    assert all(feature["citations"] for feature in report["features"])  # forensic-invariants section 1
    assert features["Webhook retries"]["conflict_ids"] and not ledger["conflict_ids"]
    assert report["conflict_count"] == 3


def test_genome_report_filters_features_and_can_drop_snippets(toy_analysis: server.RepositoryAnalysis,
                                                              toy_graph: Any) -> None:
    source = server.repository_source(toy_analysis.repo_path)

    report = server.genome_report(source, toy_graph, feature="RETRIES", include_snippets=False, include_graph=True)

    assert [feature["title"] for feature in report["features"]] == ["Webhook retries"]
    assert all("snippet" not in citation for citation in report["features"][0]["citations"])
    assert report["graph"]["nodes"] and report["graph"]["conflicts"]
    with pytest.raises(ToolError, match="No feature matches"):
        server.genome_report(source, toy_graph, feature="quantum ledger", include_snippets=True, include_graph=False)


def test_conflict_report_names_the_contradicted_readme_claims(toy_analysis: server.RepositoryAnalysis,
                                                              toy_graph: Any) -> None:
    report = server.conflict_report(server.repository_source(toy_analysis.repo_path), toy_graph)

    conflicts = {conflict["feature_title"]: conflict for conflict in report["conflicts"]}
    assert set(conflicts) == {"Webhook retries", "PDF export", "Currency conversion"}
    assert report["documents"] == {"README.md": 3}
    pdf = conflicts["PDF export"]
    assert pdf["vital_status"] == "GHOST_FEATURE" and pdf["epistemic_status"] == "CONFLICTED"
    assert (pdf["claim"]["file_path"], pdf["claim"]["line_start"], pdf["claim"]["snippet"]) == (
        "README.md", 5, "- [x] PDF export")
    retries = conflicts["Webhook retries"]
    assert any(item["is_branch_only"] and item["branch_name"] == BRANCH for item in retries["reality_evidence"])


def test_export_writes_the_bundle_and_never_clobbers_a_changed_file(toy_analysis: server.RepositoryAnalysis,
                                                                     roots: Path) -> None:
    repo = fake_repo(roots)
    specs, info = server.generated_specs(toy_analysis, StrategyType.RESTORE)
    _, documents = spec_documents(specs)
    assert info["strategy_type"] == "RESTORE" and 0.0 <= info["feasibility_score"] <= 100.0

    first = server.write_spec_bundle(repo, specs, overwrite=False)

    assert first["spec_directory"] == TOY_SPEC_DIR and first["written"]
    assert [(f["path"], f["status"]) for f in first["files"]] == [(f"{TOY_SPEC_DIR}/{doc}", "created") for doc in SPEC_DOCS]
    for path, text in documents.items():
        assert (repo / path).read_bytes() == text.encode("utf-8")

    again = server.write_spec_bundle(repo, specs, overwrite=False)
    assert not again["written"] and {f["status"] for f in again["files"]} == {"unchanged"}

    edited = repo / TOY_SPEC_DIR / "tasks.md"
    edited.write_bytes(b"edited by hand\n")
    with pytest.raises(ToolError, match="overwrite=true"):
        server.write_spec_bundle(repo, specs, overwrite=False)
    assert edited.read_bytes() == b"edited by hand\n"

    replaced = server.write_spec_bundle(repo, specs, overwrite=True)
    assert [f["status"] for f in replaced["files"]] == ["unchanged", "unchanged", "overwritten"]
    assert edited.read_bytes() == documents[f"{TOY_SPEC_DIR}/tasks.md"].encode("utf-8")

    with pytest.raises(ToolError, match="cannot be exported"):
        server.write_spec_bundle(repo, {**specs, "spec_name": "../escape"}, overwrite=False)


# ---------------------------------------------------------------------------
# MCP protocol
# ---------------------------------------------------------------------------
def payload(result: CallToolResult) -> dict[str, Any]:
    text = " ".join(getattr(block, "text", "") for block in result.content)
    assert not result.is_error, text
    if result.structured_content is not None:
        return dict(result.structured_content)
    return json.loads(text)


async def test_in_memory_client_lists_the_tools_and_reports_tool_errors() -> None:
    async with Client(server.build_server()) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        result = await client.call_tool("detect_doc_code_conflicts", {"repo_path": "x", "project_id": str(uuid4())})

    assert set(tools) == TOOL_NAMES
    for name in TOOL_NAMES - {"export_kiro_revival_spec"}:
        assert tools[name].annotations.read_only_hint is True
    export = tools["export_kiro_revival_spec"]
    assert export.annotations.read_only_hint is False and export.annotations.destructive_hint is False
    assert export.input_schema["required"] == ["repo_path"]
    assert result.is_error and "exactly one of repo_path" in result.content[0].text


@pytest.mark.slow
async def test_stdio_end_to_end_on_paypulse(tmp_path: Path) -> None:
    root = tmp_path / "repos"
    repo = build_paypulse_fixture(root / PAYPULSE_FIXTURE_NAME).repo_path
    env = {
        **os.environ,
        server.ALLOWED_ROOTS_ENV: str(root),
        "DJANGO_SETTINGS_MODULE": "config.settings",
        "PYTHONPATH": str(server.BACKEND_DIR),
    }
    env.pop(server.USE_MODEL_ENV, None)
    params = StdioServerParameters(command=sys.executable, args=[str(Path(server.__file__))], env=env,
                                   cwd=str(REPO_ROOT))
    stderr_log = tmp_path / "server-stderr.log"
    call = {"repo_path": str(repo)}
    with stderr_log.open("w", encoding="utf-8") as errlog, anyio.fail_after(E2E_TIMEOUT_SECONDS):
        # mode="legacy": the initialize handshake that 2025-era clients such as Kiro use.
        async with Client(stdio_client(params, errlog=errlog), mode="legacy",
                          read_timeout_seconds=E2E_REQUEST_TIMEOUT_SECONDS) as client:
            protocol = client.protocol_version
            listed = await client.list_tools()
            branches = payload(await client.call_tool("inspect_dead_branches", call))
            genome = payload(await client.call_tool("query_project_genome", call))
            conflicts = payload(await client.call_tool("detect_doc_code_conflicts", call))
            exported = payload(await client.call_tool("export_kiro_revival_spec", {**call, "strategy": "RESTORE"}))
            again = payload(await client.call_tool("export_kiro_revival_spec", {**call, "strategy": "RESTORE"}))
            refused = await client.call_tool("inspect_dead_branches", {"repo_path": str(tmp_path)})

    assert {tool.name for tool in listed.tools} == TOOL_NAMES

    retry = next(branch for branch in branches["branches"] if branch["name"] == RETRY_BRANCH_NAME)
    assert (retry["is_merged"], retry["is_abandoned"], retry["commits_ahead"], retry["commits_behind"]) == (
        False, True, 4, 5)
    assert {f["path"] for f in retry["diff"]["files"]} == {"paypulse/retry.py", "tests/test_retry.py"}
    assert any(RETRY_ENGINE_TODO_MARKER in (f["patch"] or "") for f in retry["diff"]["files"])

    table = {f["title"]: (f["vital_status"], f["epistemic_status"], f["confidence_score"]) for f in genome["features"]}
    assert table == {
        "Double-entry ledger with balanced, atomic postings": ("VERIFIED_WORKING", "KNOWN", 0.8594),
        "Invoices with line items, tax and a draft/issued/paid/void workflow": ("VERIFIED_WORKING", "KNOWN", 0.8594),
        "Outbound webhooks for invoice lifecycle events": ("VERIFIED_WORKING", "KNOWN", 0.8665),
        "Automated Exponential Backoff Webhook Retries": ("ABANDONED_IN_PROGRESS", "CONFLICTED", 0.7951),
        "Multi-currency conversion (USD, EUR, GBP) with daily ECB rates": ("ABANDONED_IN_PROGRESS", "CONFLICTED", 0.898),
        "PDF Invoice Export": ("GHOST_FEATURE", "CONFLICTED", 0.4182),
    }
    assert genome["synthesis"]["mode"] == "DETERMINISTIC"
    for feature in genome["features"]:
        lined = [c for c in feature["citations"] if isinstance(c["line_start"], int) and c["snippet"]]
        assert lined and all(c["line_end"] >= c["line_start"] for c in lined), feature["title"]

    claims = {c["feature_title"]: c["claim"] for c in conflicts["conflicts"]}
    for title, line in (("Automated Exponential Backoff Webhook Retries", 11), ("PDF Invoice Export", 12)):
        assert claims[title]["file_path"] == "README.md" and claims[title]["line_start"] == line
        assert title in claims[title]["snippet"]
    assert conflicts["conflict_count"] == 3

    spec_dir = ".kiro/specs/lazarus-restore-paypulse-dead-repo"
    assert exported["spec_directory"] == spec_dir and exported["source"]["kind"] == "repository"
    assert [(f["path"], f["status"]) for f in exported["files"]] == [(f"{spec_dir}/{doc}", "created") for doc in SPEC_DOCS]
    texts = {doc: (repo / spec_dir / doc).read_text(encoding="utf-8") for doc in SPEC_DOCS}
    assert texts["requirements.md"].startswith("# Requirements")
    assert texts["design.md"].startswith("# Design")
    assert texts["tasks.md"].startswith("# Implementation Plan")
    assert {f["status"] for f in again["files"]} == {"unchanged"} and not again["written"]
    assert run_git(repo, "status", "--porcelain").strip() == "?? .kiro/"  # nothing tracked was touched

    refused_text = " ".join(getattr(block, "text", "") for block in refused.content)
    assert refused.is_error and "outside the allowed roots" in refused_text

    print(  # noqa: T201 - evidence, shown with -s
        f"\nprotocol {protocol}; tools: {sorted(tool.name for tool in listed.tools)}"
        f"\ndead branch: {retry['name']} ahead={retry['commits_ahead']} behind={retry['commits_behind']} "
        f"abandoned={retry['is_abandoned']} dormancy={retry['dormancy_days']}d merge_base={retry['merge_base_sha'][:12]} "
        f"files={[(f['path'], f['change_type'], f['additions'], f['deletions']) for f in retry['diff']['files']]}"
        f"\ngenome ({genome['synthesis']['mode']}):"
        + "".join(
            f"\n  {f['title']}: {f['vital_status']}/{f['epistemic_status']} C={f['confidence_score']} "
            f"citations={len(f['citations'])} e.g. "
            f"{next(c['location'] for c in f['citations'] if isinstance(c['line_start'], int))}"
            for f in genome["features"]
        )
        + "\nconflicts:"
        + "".join(f"\n  {c['claim']['source']} -> {c['feature_title']} ({c['vital_status']})"
                  for c in conflicts["conflicts"])
        + f"\nexported: {[(f['path'], f['status'], f['bytes']) for f in exported['files']]}"
        f"\nre-export: {[f['status'] for f in again['files']]}; git status: {run_git(repo, 'status', '--porcelain').strip()}"
        f"\nrefused: {refused_text[:160]}"
        f"\nserver stderr: {stderr_log.read_text(encoding='utf-8').strip()[-600:]}"
    )


# ---------------------------------------------------------------------------
# project_id (PostgreSQL)
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_project_id_tools_read_the_persisted_rows(roots: Path) -> None:
    project = Project.objects.create(name="toy", status=ProjectStatus.AUTOPSY_COMPLETE)
    branches, artifacts = toy_ingestion_rows()
    persist_ingestion(project, IngestionResult(MAIN, tuple(branches), tuple(artifacts), {"branch_count": 2}))
    project.refresh_from_db()
    persist_genome(project, GenomeReconcilerAgent(api_key="").synthesize(load_catalog(project)))
    generate_strategies(project)
    project_id = str(project.pk)

    dead = server.inspect_dead_branches(project_id=project_id)
    genome = server.query_project_genome(project_id=project_id, include_snippets=False)
    conflicts = server.detect_doc_code_conflicts(project_id=project_id)
    repo = fake_repo(roots)
    exported = server.export_kiro_revival_spec(repo_path=str(repo), project_id=project_id)

    assert dead["source"]["kind"] == "project" and [b["name"] for b in dead["branches"]] == [BRANCH]
    assert dead["branches"][0]["diff"]["files"][0]["path"] == "pkg/retry.py"
    assert {f["title"] for f in genome["features"]} >= {"Ledger postings", "Webhook retries"}
    assert {c["feature_title"] for c in conflicts["conflicts"]} == {"Webhook retries", "PDF export", "Currency conversion"}
    stored = RevivalStrategy.objects.get(project=project, strategy_type="RESTORE").generated_kiro_specs
    _, documents = spec_documents(stored)
    assert exported["strategy"]["origin"] == "RevivalStrategy.generated_kiro_specs"
    for path, text in documents.items():
        assert (repo / path).read_bytes() == text.encode("utf-8")
    with pytest.raises(ToolError, match="No Lazarus project"):
        server.query_project_genome(project_id=str(uuid4()))
