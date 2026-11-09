"""Product Rebirth (System 5), the part without the ORM: build a verified run's rebirth bundle.

``build_rebirth(workspace, inputs)`` runs after a Foundry run passed verification.
The workspace is then on the revival branch ``lazarus/<strategy>-<run>``: the
ArchitectureAgent's merge commits are on it, and the EngineeringAgent's edits
(and any self-healing repairs) are still uncommitted in the work tree.

1. Checks: the run has no failure and its last verification attempt passed; the
   workspace is on the run's revival branch; the run's base commit
   (``report["environment"]["base_sha"]``, the default branch's HEAD when the run
   started) is an ancestor of HEAD.
2. Exports the approved ``.kiro/specs/<spec_name>/`` bundle
   (``RevivalStrategy.generated_kiro_specs``) into the work tree:
   ``requirements.md``, ``design.md`` and ``tasks.md``.
3. Commits the verified work tree on the revival branch (fixed Lazarus identity,
   ``--no-verify``). The commit may only contain the files the run reported as
   changed (``report["changed_files"]``) plus the exported specs; anything else
   (a file that changed after verification) is unstaged and the build fails with
   ``UNEXPECTED_CHANGES``. The specs are added with ``-f``, so a ``.gitignore``
   in the target repository cannot drop them.
4. The unified diff is ``git diff <base_sha> <rebirth commit>`` without rename
   detection, so it equals the revival branch's change against the default
   branch and applies with ``git apply`` on the base commit.
5. Builds the Continuity Memory JSON and the PR title and Markdown body.

Repository-derived text (titles, rationales, deferral details) goes through
``apps.revival.specs.inline_text`` before it is embedded in Markdown. File paths
are written as code spans. No server path is stored: the exported spec path is
repository-relative.
"""
from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from apps.foundry.workspace import REVIVAL_BRANCH_PREFIX, WorkspaceError, current_branch, git, head_sha, write_text
from apps.projects.git_runner import GitCommandError, GitError
from apps.revival.approval import SPEC_DOCUMENTS
from apps.revival.specs import SPEC_NAME_MAX_CHARS, code, inline_text

CONTINUITY_VERSION: str = "lazarus-continuity-v1"
SPEC_ROOT: str = ".kiro/specs"
MAX_DIFF_CHARS: int = 2_000_000
MAX_PR_TITLE_CHARS: int = 255
MAX_LISTED_TESTS: int = 20
DIFF_TRUNCATED_NOTE: str = "\n[lazarus] diff truncated at {limit} characters; run git diff {base} {head} for the rest\n"
_SPEC_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_SHA = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")
STATUS_LABELS: dict[str, str] = {"A": "added", "M": "modified", "D": "deleted", "T": "type changed"}


class RebirthError(RuntimeError):
    """The rebirth bundle could not be built; ``detail`` is safe to show (no server paths)."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class RebirthInputs:
    """What ``build_rebirth`` needs from the project, the approved strategy and the finished run."""

    project_id: str
    project_name: str
    default_branch: str
    foundry_run_id: str
    report: Mapping[str, Any]  # FoundryRun.report / FoundryResult.report
    iterations_used: int
    strategy_id: str
    strategy_type: str
    strategy_title: str
    action_plan: Mapping[str, Any]
    kiro_specs: Mapping[str, Any]  # RevivalStrategy.generated_kiro_specs
    strategy_summary: str = ""
    feasibility_score: float | None = None
    repo_url: str = ""
    approved_by: str = ""
    approved_at: str | None = None
    preview_url: str = ""


@dataclass(frozen=True)
class DiffFile:
    path: str
    status: str  # A, M, D or T (git diff --name-status, no rename detection)
    additions: int | None  # None for a binary file
    deletions: int | None

    def as_dict(self) -> dict[str, Any]:
        return {"path": self.path, "status": self.status, "additions": self.additions, "deletions": self.deletions}


@dataclass(frozen=True)
class RebirthBundle:
    pr_title: str
    pr_body_markdown: str
    unified_diff_patch: str
    diff_truncated: bool
    exported_kiro_specs_path: str  # repository-relative, e.g. .kiro/specs/lazarus-restore-paypulse
    spec_files: tuple[str, ...]
    continuity_memory: dict[str, Any]
    base_sha: str
    rebirth_commit_sha: str
    revival_branch: str
    committed_files: tuple[str, ...]  # the files of the rebirth commit itself
    files: tuple[DiffFile, ...] = field(default_factory=tuple)  # base..rebirth commit

    @property
    def additions(self) -> int:
        return sum(f.additions or 0 for f in self.files)

    @property
    def deletions(self) -> int:
        return sum(f.deletions or 0 for f in self.files)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _dict(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


def plain_line(text: Any, limit: int | None = None) -> str:
    """One line of plain text (no control characters), for the PR title and commit message."""
    flat = " ".join(_CONTROL.sub(" ", str(text or "")).split())
    return flat[:limit].rstrip() if limit is not None else flat


def cell_code(path: str) -> str:
    """A code span for a Markdown table cell (``|`` would end the cell)."""
    return code(path).replace("|", "\\|")


def _z_fields(output: str) -> list[str]:
    return [part for part in output.split("\0") if part]


def totals_of(summary: Mapping[str, Any] | None) -> dict[str, int]:
    """``run_summary(...)["totals"]`` as five ints (zeros when the summary is missing)."""
    totals = _dict(_dict(summary).get("totals"))
    return {key: int(totals.get(key) or 0) for key in ("tests", "passed", "failed", "errors", "skipped")}


# ---------------------------------------------------------------------------
# Spec export
# ---------------------------------------------------------------------------
def spec_documents(specs: Mapping[str, Any]) -> tuple[str, dict[str, str]]:
    """``(".kiro/specs/<spec_name>", {repo-relative path: Markdown})`` of a stored spec bundle."""
    specs = _dict(specs)
    name = str(specs.get("spec_name") or "")
    if not _SPEC_NAME.match(name) or len(name) > SPEC_NAME_MAX_CHARS:
        raise RebirthError("SPEC_INVALID", "the approved strategy has no valid spec name")
    directory = f"{SPEC_ROOT}/{name}"
    documents: dict[str, str] = {}
    for doc, key in SPEC_DOCUMENTS.items():
        text = specs.get(key)
        if not isinstance(text, str) or not text.strip():
            raise RebirthError("SPEC_INVALID", f"the approved spec bundle has no {doc}")
        documents[f"{directory}/{doc}"] = text if text.endswith("\n") else text + "\n"
    return directory, documents


def export_specs(workspace: Path, specs: Mapping[str, Any]) -> tuple[str, tuple[str, ...]]:
    """Write the spec bundle into the work tree; returns the directory and the written paths."""
    directory, documents = spec_documents(specs)
    try:
        for path, text in documents.items():
            write_text(workspace, path, text)
    except WorkspaceError as exc:
        raise RebirthError("SPEC_EXPORT_FAILED", str(exc)) from exc
    return directory, tuple(documents)


# ---------------------------------------------------------------------------
# Git
# ---------------------------------------------------------------------------
def pending_files(workspace: Path) -> tuple[str, ...]:
    """Uncommitted changes against HEAD: tracked edits and deletions, and untracked files not ignored."""
    tracked = _z_fields(git(workspace, "diff", "--name-only", "--no-renames", "-z", "HEAD"))
    untracked = _z_fields(git(workspace, "ls-files", "--others", "--exclude-standard", "-z"))
    return tuple(sorted({*tracked, *untracked}))


def commit_rebirth(workspace: Path, *, allowed: Iterable[str], forced: Sequence[str],
                   message: str) -> tuple[str, tuple[str, ...]]:
    """Stage the pending work tree plus ``forced`` and commit it; returns ``(HEAD, committed files)``.

    Fails with ``UNEXPECTED_CHANGES`` (and unstages everything) when a staged
    file is not in ``allowed``. Nothing to commit leaves HEAD unchanged.
    """
    forced_set = set(forced)
    pending = [path for path in pending_files(workspace) if path not in forced_set]
    if pending:
        git(workspace, "add", "-A", "--", *pending)
    if forced:
        git(workspace, "add", "-f", "--", *forced)
    staged = tuple(sorted(_z_fields(git(workspace, "diff", "--cached", "--name-only", "--no-renames", "-z", "HEAD"))))
    unexpected = sorted(set(staged) - set(allowed))
    if unexpected:
        git(workspace, "reset", "-q")
        raise RebirthError(
            "UNEXPECTED_CHANGES",
            f"{len(unexpected)} file(s) changed outside the verified run: {', '.join(unexpected[:10])}",
        )
    if staged:
        git(workspace, "commit", "-q", "--no-verify", "-m", message)
    return head_sha(workspace), staged


def diff_files(workspace: Path, base: str, head: str) -> tuple[DiffFile, ...]:
    statuses = _z_fields(git(workspace, "diff", "--no-renames", "--name-status", "-z", base, head))
    status_of = {statuses[i + 1]: statuses[i][:1] for i in range(0, len(statuses) - 1, 2)}
    counts: dict[str, tuple[int | None, int | None]] = {}
    for record in _z_fields(git(workspace, "diff", "--no-renames", "--numstat", "-z", base, head)):
        added, deleted, path = record.split("\t", 2)
        counts[path] = (int(added) if added.isdigit() else None, int(deleted) if deleted.isdigit() else None)
    return tuple(
        DiffFile(path=path, status=status_of[path], additions=counts.get(path, (None, None))[0],
                 deletions=counts.get(path, (None, None))[1])
        for path in sorted(status_of)
    )


def unified_diff(workspace: Path, base: str, head: str, *, limit: int = MAX_DIFF_CHARS) -> tuple[str, bool]:
    """``git diff base head`` as a unified patch, and whether it was cut at ``limit`` characters."""
    patch = git(workspace, "diff", "--no-color", "--no-ext-diff", "--no-textconv", "--no-renames", base, head)
    if len(patch) <= limit:
        return patch, False
    cut = patch[:limit]
    cut = cut[: cut.rfind("\ndiff --git ") + 1] if "\ndiff --git " in cut else cut
    return cut + DIFF_TRUNCATED_NOTE.format(limit=limit, base=base[:12], head=head[:12]), True


# ---------------------------------------------------------------------------
# Verification facts from the run report
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class VerificationFacts:
    before: dict[str, int]
    after: dict[str, int]
    before_failed: tuple[str, ...]
    fixed: tuple[str, ...]
    target_outcomes: dict[str, str]
    targets_run_separately: dict[str, int] | None
    attempts: int
    python: str
    sandbox_mode: str
    network_isolated: bool | None
    install_resolution: str
    install_note: str

    def as_dict(self, *, iterations_used: int, max_healing_iterations: int) -> dict[str, Any]:
        return {
            "before": self.before, "after": self.after, "tests_fixed": list(self.fixed),
            "failing_before": list(self.before_failed), "target_outcomes": self.target_outcomes,
            "targets_run_separately": self.targets_run_separately, "verification_attempts": self.attempts,
            "iterations_used": iterations_used, "max_healing_iterations": max_healing_iterations,
            "python": self.python, "sandbox_mode": self.sandbox_mode, "network_isolated": self.network_isolated,
            "install_resolution": self.install_resolution,
        }


def verification_facts(report: Mapping[str, Any]) -> VerificationFacts:
    """The before/after facts of a passed run; ``RebirthError`` when the run did not pass."""
    if report.get("failure"):
        raise RebirthError("RUN_NOT_VERIFIED", "the Foundry run failed, so there is nothing to rebirth")
    attempts = [attempt for attempt in _list(_dict(report.get("verification")).get("attempts")) if isinstance(attempt, dict)]
    if not attempts or attempts[-1].get("passed") is not True:
        raise RebirthError("RUN_NOT_VERIFIED", "the Foundry run has no passing verification")
    last = attempts[-1]
    environment = _dict(report.get("environment"))
    baseline = _dict(environment.get("baseline"))
    full = _dict(last.get("full"))
    before_failed = tuple(str(nodeid) for nodeid in _list(baseline.get("failed")))
    still_failing = {str(nodeid) for nodeid in _list(full.get("failed"))}
    separate = last.get("targets_run_separately")
    sandbox = _dict(environment.get("sandbox"))
    install = _dict(environment.get("install"))
    outcomes = _dict(last.get("target_outcomes"))
    return VerificationFacts(
        before=totals_of(baseline), after=totals_of(full), before_failed=before_failed,
        fixed=tuple(nodeid for nodeid in before_failed if nodeid not in still_failing),
        target_outcomes={str(target): str(outcome) for target, outcome in outcomes.items()},
        targets_run_separately=totals_of(separate) if isinstance(separate, dict) else None,
        attempts=len(attempts), python=str(_dict(environment.get("python")).get("actual") or ""),
        sandbox_mode=str(sandbox.get("mode") or report.get("sandbox_mode") or ""),
        network_isolated=sandbox.get("network_isolated") if isinstance(sandbox.get("network_isolated"), bool) else None,
        install_resolution=str(install.get("resolution") or ""), install_note=str(install.get("note") or ""),
    )


# ---------------------------------------------------------------------------
# Continuity Memory
# ---------------------------------------------------------------------------
def continuity_memory(inputs: RebirthInputs, facts: VerificationFacts, *, base_sha: str, head: str,
                      revival_branch: str, spec_directory: str, spec_files: Sequence[str],
                      files: Sequence[DiffFile], uncommitted: Sequence[str], generated_at: str) -> dict[str, Any]:
    """The JSON roadmap the resurrected project carries forward: what was revived, verified and deferred."""
    plan = _dict(inputs.action_plan)
    items = [item for item in _list(plan.get("items")) if isinstance(item, dict)]
    merges = [merge for merge in _list(_dict(inputs.report.get("architecture")).get("merges")) if isinstance(merge, dict)]
    deferred = [entry for entry in _list(plan.get("deferred")) if isinstance(entry, dict)]
    return {
        "version": CONTINUITY_VERSION,
        "generated_at": generated_at,
        "project": {"id": inputs.project_id, "name": inputs.project_name, "repo_url": inputs.repo_url,
                    "default_branch": inputs.default_branch},
        "revival": {
            "strategy_id": inputs.strategy_id, "strategy_type": inputs.strategy_type, "title": inputs.strategy_title,
            "feasibility_score": inputs.feasibility_score, "approved_by": inputs.approved_by,
            "approved_at": inputs.approved_at, "foundry_run_id": inputs.foundry_run_id,
            "revival_branch": revival_branch, "base_sha": base_sha, "rebirth_commit_sha": head,
            "spec_name": spec_directory.rsplit("/", 1)[-1], "spec_path": spec_directory, "spec_files": list(spec_files),
        },
        "verification": facts.as_dict(
            iterations_used=inputs.iterations_used,
            max_healing_iterations=int(inputs.report.get("max_healing_iterations") or 0),
        ),
        "completed": [
            {"item_id": str(item.get("id") or ""), "kind": str(item.get("kind") or ""),
             "stage": str(item.get("stage") or ""), "title": str(item.get("title") or ""),
             "feature_id": item.get("feature_id"),
             "files": sorted({*map(str, _list(item.get("files_to_modify"))), *map(str, _list(item.get("files_to_create")))}),
             "target_tests": [str(test) for test in _list(item.get("target_tests"))]}
            for item in items
        ],
        "merged_branches": [
            {"branch": str(merge.get("branch") or ""), "branch_head": str(merge.get("branch_head") or ""),
             "merge_commit": str(merge.get("merge_commit") or ""), "files": [str(p) for p in _list(merge.get("files"))]}
            for merge in merges
        ],
        "features_revived": [
            {"feature_id": entry.get("feature_id"), "title": str(entry.get("title") or ""),
             "vital_status": str(entry.get("vital_status") or ""), "partial": bool(entry.get("partial"))}
            for entry in _list(plan.get("in_scope")) if isinstance(entry, dict)
        ],
        "roadmap": [
            {"step": number, "feature_id": entry.get("feature_id"), "title": str(entry.get("title") or ""),
             "vital_status": str(entry.get("vital_status") or ""), "reason": str(entry.get("reason") or ""),
             "detail": str(entry.get("detail") or ""), "would_touch": [str(p) for p in _list(entry.get("would_touch"))],
             "external_requirements": [_dict(r).get("label") or str(r) for r in _list(entry.get("external_requirements"))],
             "partial": bool(entry.get("partial")), "status": "DEFERRED"}
            for number, entry in enumerate(deferred, start=1)
        ],
        "open_markers": sorted({str(marker) for item in items for marker in _list(item.get("open_markers"))}),
        "changed_files": [f.as_dict() for f in files],
        "uncommitted_files": list(uncommitted),
    }


# ---------------------------------------------------------------------------
# PR summary
# ---------------------------------------------------------------------------
def pr_title(inputs: RebirthInputs) -> str:
    title = plain_line(inputs.strategy_title) or f"Revive {plain_line(inputs.project_name)}"
    return plain_line(f"Lazarus {inputs.strategy_type.upper()}: {title}", MAX_PR_TITLE_CHARS)


def _totals_row(label: str, totals: Mapping[str, int]) -> str:
    return (f"| {label} | {totals['tests']} | {totals['passed']} | {totals['failed']} | {totals['errors']} "
            f"| {totals['skipped']} |")


def _tests_list(nodeids: Sequence[str]) -> list[str]:
    lines = [f"- {code(nodeid)}" for nodeid in nodeids[:MAX_LISTED_TESTS]]
    if len(nodeids) > MAX_LISTED_TESTS:
        lines.append(f"- … and {len(nodeids) - MAX_LISTED_TESTS} more")
    return lines


def pr_body_markdown(inputs: RebirthInputs, facts: VerificationFacts, *, base_sha: str, head: str,
                     revival_branch: str, spec_files: Sequence[str], files: Sequence[DiffFile],
                     diff_truncated: bool) -> str:
    plan = _dict(inputs.action_plan)
    project = inline_text(inputs.project_name) or "the project"
    lines: list[str] = [
        "## Summary",
        "",
        f"Lazarus Foundry revived **{project}** with the human-approved **{inline_text(inputs.strategy_type)}** "
        f"strategy: {inline_text(inputs.strategy_title)}.",
    ]
    if inputs.strategy_summary:
        lines += ["", inline_text(inputs.strategy_summary, 1500)]
    lines += [
        "",
        f"- Branch: {code(revival_branch)}, based on {code(inputs.default_branch)} at {code(base_sha[:12])}",
        f"- Rebirth commit: {code(head[:12])}",
        f"- Foundry run: {code(inputs.foundry_run_id)}",
    ]
    if inputs.feasibility_score is not None:
        lines.append(f"- Feasibility score: {inputs.feasibility_score:.1f} / 100")
    if inputs.approved_by or inputs.approved_at:
        who = inline_text(inputs.approved_by, 255) or "a reviewer"
        lines.append(f"- Approved by {who}" + (f" at {inline_text(inputs.approved_at)}" if inputs.approved_at else ""))

    lines += [
        "",
        "## Verification",
        "",
        "| Run | Tests | Passed | Failed | Errors | Skipped |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
        _totals_row(f"Before ({inline_text(inputs.default_branch)})", facts.before),
        _totals_row(f"After ({inline_text(revival_branch)})", facts.after),
    ]
    if facts.targets_run_separately is not None:
        lines.append(_totals_row("After (target tests run separately)", facts.targets_run_separately))
    after = facts.after
    all_passing = after["failed"] == 0 and after["errors"] == 0
    lines += [
        "",
        f"Before: {facts.before['passed']} of {facts.before['tests']} tests passed, "
        f"{facts.before['failed'] + facts.before['errors']} failing. "
        f"After: {after['passed']} of {after['tests']} passed"
        + (", all passing." if all_passing else f", {after['failed'] + after['errors']} failing."),
        f"Self-healing iterations used: {inputs.iterations_used} of "
        f"{int(inputs.report.get('max_healing_iterations') or 0)}.",
    ]
    sandbox = facts.sandbox_mode or "unknown"
    isolation = {True: "network isolated", False: "not network isolated"}.get(facts.network_isolated, "isolation unknown")
    lines.append(f"Sandbox: {inline_text(sandbox)} ({isolation})"
                 + (f", Python {inline_text(facts.python)}" if facts.python else "")
                 + (f", dependencies {inline_text(facts.install_resolution)}" if facts.install_resolution else "") + ".")
    if facts.fixed:
        lines += ["", "Tests that failed before and pass now:", ""] + _tests_list(facts.fixed)
    if facts.target_outcomes:
        lines += ["", "Target tests:", ""] + [
            f"- {code(target)}: {inline_text(outcome)}" for target, outcome in list(facts.target_outcomes.items())[:MAX_LISTED_TESTS]
        ]

    items = [item for item in _list(plan.get("items")) if isinstance(item, dict)]
    lines += ["", "## Changes", ""]
    for item in items:
        paths = sorted({*map(str, _list(item.get("files_to_modify"))), *map(str, _list(item.get("files_to_create")))})
        entry = f"- **{inline_text(item.get('title'), 200)}** ({inline_text(item.get('kind'))})"
        if item.get("branch") and item.get("kind") == "MERGE_ABANDONED_BRANCH":
            entry += f", merges {code(str(item['branch']))}"
        if paths:
            entry += f": {', '.join(code(path) for path in paths)}"
        lines.append(entry)
    if not items:
        lines.append("- No plan items.")

    additions = sum(f.additions or 0 for f in files)
    deletions = sum(f.deletions or 0 for f in files)
    lines += [
        "",
        f"## Files changed ({len(files)} files, +{additions} -{deletions})",
        "",
        "| File | Status | + | - |",
        "| --- | --- | ---: | ---: |",
    ]
    for f in files:
        plus = "binary" if f.additions is None else str(f.additions)
        minus = "binary" if f.deletions is None else str(f.deletions)
        lines.append(f"| {cell_code(f.path)} | {STATUS_LABELS.get(f.status, f.status)} | {plus} | {minus} |")
    if diff_truncated:
        lines += ["", f"The stored diff is truncated at {MAX_DIFF_CHARS} characters."]

    lines += ["", "## Kiro specs", "", "The approved recovery spec ships with this change:", ""]
    lines += [f"- {code(path)}" for path in spec_files]

    deferred = [entry for entry in _list(plan.get("deferred")) if isinstance(entry, dict)]
    if deferred:
        lines += ["", "## Deferred (not in this change)", ""]
        for entry in deferred:
            detail = inline_text(entry.get("detail"), 300)
            lines.append(f"- **{inline_text(entry.get('title'), 200)}** ({inline_text(entry.get('reason'))})"
                         + (f": {detail}" if detail else ""))
    if inputs.preview_url:
        lines += ["", "## Preview", "", f"Sandbox preview (when started): <{inline_text(inputs.preview_url)}>"]
    lines += ["", "---", "", "Generated by Lazarus Foundry. The Continuity Memory JSON records the roadmap."]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def build_rebirth(workspace: Path, inputs: RebirthInputs, *, generated_at: str | None = None) -> RebirthBundle:
    """Export the specs, commit the verified tree and build the bundle (see the module docstring)."""
    report = inputs.report
    facts = verification_facts(report)
    revival_branch = str(report.get("revival_branch") or "")
    base_sha = str(_dict(report.get("environment")).get("base_sha") or "")
    if not revival_branch.startswith(REVIVAL_BRANCH_PREFIX):
        raise RebirthError("RUN_INCOMPLETE", "the Foundry run recorded no revival branch")
    if not _SHA.match(base_sha):
        raise RebirthError("RUN_INCOMPLETE", "the Foundry run recorded no base commit")
    generated_at = generated_at or datetime.now(timezone.utc).isoformat()
    try:
        branch = current_branch(workspace)
        if branch != revival_branch:
            raise RebirthError("WORKSPACE_NOT_ON_REVIVAL_BRANCH",
                               f"the workspace is on {branch}, not the revival branch {revival_branch}")
        try:
            git(workspace, "merge-base", "--is-ancestor", base_sha, "HEAD")
        except GitCommandError as exc:
            raise RebirthError("BASE_NOT_ANCESTOR",
                               f"the base commit {base_sha[:12]} is not an ancestor of {revival_branch}") from exc
        spec_directory, spec_files = export_specs(workspace, inputs.kiro_specs)
        allowed = {str(path) for path in _list(report.get("changed_files"))} | set(spec_files)
        message = (f"Lazarus Foundry: rebirth of {plain_line(inputs.project_name, 120)} "
                   f"({plain_line(inputs.strategy_type, 16)})\n\n"
                   f"Foundry run {plain_line(inputs.foundry_run_id, 64)} verified "
                   f"{facts.after['passed']}/{facts.after['tests']} tests passing.\n"
                   f"Specs: {spec_directory}\n")
        head, committed = commit_rebirth(workspace, allowed=allowed, forced=spec_files, message=message)
        files = diff_files(workspace, base_sha, head)
        patch, truncated = unified_diff(workspace, base_sha, head)
        uncommitted = pending_files(workspace)
    except GitError as exc:
        raise RebirthError("GIT_FAILED", f"a Git command failed while building the rebirth ({type(exc).__name__})") from exc
    memory = continuity_memory(inputs, facts, base_sha=base_sha, head=head, revival_branch=revival_branch,
                               spec_directory=spec_directory, spec_files=spec_files, files=files,
                               uncommitted=uncommitted, generated_at=generated_at)
    try:
        memory = json.loads(json.dumps(memory, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise RebirthError("MEMORY_INVALID", "the Continuity Memory is not valid JSON") from exc
    return RebirthBundle(
        pr_title=pr_title(inputs),
        pr_body_markdown=pr_body_markdown(inputs, facts, base_sha=base_sha, head=head, revival_branch=revival_branch,
                                          spec_files=spec_files, files=files, diff_truncated=truncated),
        unified_diff_patch=patch, diff_truncated=truncated, exported_kiro_specs_path=spec_directory,
        spec_files=spec_files, continuity_memory=memory, base_sha=base_sha, rebirth_commit_sha=head,
        revival_branch=revival_branch, committed_files=committed, files=files,
    )
