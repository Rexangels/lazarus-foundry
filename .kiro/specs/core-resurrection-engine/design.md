# Technical Design — Lazarus Foundry

## 1. Seven Django Apps & Core ORM Models (`backend/apps/`)

### 1. `apps.projects`
- `Project`: `id (UUID)`, `name`, `repo_url`, `is_golden_demo (bool)`, `workspace_path`, `status` (`DORMANT`, `INGESTING`, `SYNTHESIZING_GENOME`, `AUTOPSY_COMPLETE`, `STRATEGY_APPROVED`, `FOUNDRY_RUNNING`, `REBORN`, `FAILED`), `default_branch`, `status_detail (TextField, blank)`, `ingested_at (DateTime, null=True)`, `ingestion_summary (JSONField)`, `created_at`, `updated_at`.
  - `SYNTHESIZING_GENOME` (migration `projects/0003`) means ingestion is committed and `synthesize_genome_task` owns the project.
  - `status_detail` holds a user-safe explanation of the status (why ingestion `FAILED`); never tracebacks, Git output or server paths. `ingested_at` and `ingestion_summary` record the last successful ingestion (migration `projects/0002`).

### 2. `apps.ingestion`
- Migration `0001_enable_pgvector.py`: Runs `VectorExtension()`.
- `BranchSnapshot`: `project (FK)`, `name`, `head_sha`, `last_commit_date`, `is_abandoned (bool)`, `commits_ahead (int)`, `commits_behind (int)`, `diff_summary (JSONField)`.
- `ArtifactRecord`: `project (FK)`, `artifact_type` (`AST_MODULE`, `README_DOC`, `DEPENDENCY_MANIFEST`, `TODO_MARKER`, `COMMIT_LOG`), `file_path`, `branch_name`, `raw_content`, `structured_metadata (JSONField)`, `embedding (VectorField(dimensions=1536, null=True))`.

#### Ingestion: Git forensics (`apps/ingestion/git_forensics.py`)
- **Pure service.** `GitForensicService` returns frozen dataclasses (`RepositoryReport`, `BranchReport`, `CommitInfo`, `BranchDiff`, `FileDiff`, `DefaultBranchFacts`) and never touches the ORM; `ingest_repository_task` (Task 3.3) persists them as `BranchSnapshot`/`ArtifactRecord`. Reads use GitPython with `GIT_DIR`/work tree pinned and system/user Git config disabled.
- **Default branch.** Detected from `origin/HEAD`, else `main`, else `master`, else the alphabetically first branch. "Diffs against `main`" means against this detected default branch. Branches are the remote-tracking `refs/remotes/origin/*` (minus `origin/HEAD`) plus local branches, de-duplicated by short name (local wins).
- **Per branch.** Head SHA, last commit date (committer date), `commits_ahead`/`commits_behind` (`rev-list --left-right --count`), merge-base SHA and date, `is_merged` (tip is an ancestor of the default branch), the commits unique to the branch, and a three-dot diff (merge-base → branch tip) with change type, additions/deletions and a per-file unified patch capped at `INGEST_MAX_PATCH_BYTES` (truncation recorded). Binary files are listed without patch or line counts.
- **Abandonment rule.** Judged on the repository's own timeline, not the wall clock: `is_abandoned = not is_merged and (repo_latest_commit_date − branch_last_commit_date) ≥ ABANDONED_BRANCH_MIN_AGE_DAYS` (default 90). `repo_latest_commit_date` is the newest branch tip across all branches. The report also carries the default branch's head, commit count and first/last commit dates for Δt in $C(f)$. PayPulse: `feat/webhook-retry-engine` is unmerged and abandoned, ahead 4, behind 5, merge-base `main~5`.
- **Clone security.** User URLs go through `clone_into_workspace`: `https://` only, host must be in `INGEST_ALLOWED_HOSTS` (default `github.com, gitlab.com, bitbucket.org`), no credentials, ports, queries, fragments, `.`/`..` segments, whitespace/control characters, `::` transports or leading `-`. Clone runs via `apps.projects.git_runner` (scrubbed `GIT_*` env, `GIT_CONFIG_NOSYSTEM`, `GIT_CONFIG_GLOBAL=<devnull>`, no prompts, `INGEST_CLONE_TIMEOUT_SECONDS`) with `protocol.allow=never`, `protocol.https.allow=always`, `protocol.file.allow=never`, `--no-recurse-submodules`, `core.symlinks=false`, `core.autocrlf=false`, full history (no `--depth`, never `--mirror`). Local repositories (demo fixture, tests) use the separate `clone_local_repository_into_workspace` with `--no-local` (no hardlinked objects). Workspaces must be new or empty directories strictly inside `WORKSPACE_ROOT` (symlinks resolved first) and must not overlap the source; a failed clone deletes the workspace.
- **Files without checkout.** `read_file_at(revision, path)` and `list_files_at(revision)` read Git objects (`cat-file`/`ls-tree`, regular files only), so Task 3.2 can parse branch versions while the work tree stays on the default branch.
- **Deliberate deviations from the original task text:** "against `main`" is generalized to the detected default branch; abandonment uses repo-relative age rather than wall-clock age; the PayPulse builder moved from `tests/fixtures/` into `apps/projects/demo/` (the test path remains as a re-export) so production code never imports `tests`.

#### Ingestion: AST and document extraction (`apps/ingestion/ast_parser.py`, `doc_extractor.py`, `evidence.py`)
- **Pure service.** `ASTParserService.parse_file(path, source_bytes) -> FileInventory` (frozen dataclasses, no ORM, no Git, no network). `parse_revision(service, revision)` walks `GitForensicService.list_files_at` and parses each blob read with `read_file_at`; Task 3.3 persists the records as `ArtifactRecord` rows.
- **One evidence shape.** Every record (`CodeSymbol`, `ImportRecord`, `RouteRecord`, `StubRecord`, `CommentMarker`, `DocHeading`, `DocClaim`, `DependencyRecord`) carries `kind`, `name`, `file_path` (repo-relative POSIX), 1-based inclusive `line_start`/`line_end`, `snippet` (the exact text of those lines, line terminators normalized to `\n`, capped at 80 lines / 4000 chars at a line boundary, with `snippet_truncated`) and `fingerprint`. Lines are split on `\n` only, matching tree-sitter rows, so CRLF and non-ASCII files number correctly. Task 4 copies these fields into `EvidenceCitation`.
- **Fingerprint rule.** `sha256(kind, file_path, identity)[:32]`. The identity is the qualified name for code (`Ledger.post_transaction`), the module for imports, method plus path for routes, the enclosing function for stubs, and normalized text for markers, headings and doc claims (the checked state is excluded, so `[ ]` and `[x]` match). Dependencies use ecosystem, group and normalized name (the version is excluded). Line numbers are never part of it, and repeats in one file get `#2`, `#3`. The same symbol on `main` and on a branch therefore has the same fingerprint.
- **Python (bundled `tree-sitter-python`):** module docstring, classes with bases, functions/methods (async, decorators, signature, docstring; decorated definitions start at the first decorator), all imports (relative, aliased, wildcard, function-local), module-level `UPPER_CASE` constants, `raise NotImplementedError` stubs on the enclosing function (`is_stub`, with `in_abstract_method` for `@abstractmethod`), pytest-collectable tests (`is_test`), and routes from `@app.get("/x")` / `@router.post(...)` / `@app.route(..., methods=[...])`.
- **Grammar fallback.** `tree-sitter-language-pack` 1.24.1 ships no grammars; it downloads them on first `get_language()` into a per-user cache. Other languages (JS/TS/TSX, Go, Java, Ruby, Rust, C#, PHP; node-type maps in `LANGUAGE_SPECS`) use only cached grammars unless `INGEST_ALLOW_GRAMMAR_DOWNLOAD=True`. A missing or failing grammar is logged once and gives `GRAMMAR_UNAVAILABLE`; files without an extractor give `UNSUPPORTED_LANGUAGE`. Both still get line-scanned `TODO`/`FIXME`/`HACK`/`XXX` markers. Parsed files take markers from comment nodes only, so strings never count.
- **Docs and manifests.** Markdown headings plus checklist claims (anywhere) and plain bullets under a "Features"/"Roadmap"-style heading become `DocClaim` records. `requirements*.txt`, `pyproject.toml` (PEP 621, PEP 735, Poetry), `package.json` and `setup.cfg` become `DependencyRecord(name, specifier, pinned_version, line)` with no package-index calls.
- **Skips.** Vendored or generated paths (`node_modules/`, `.venv/`, `venv/`, `vendor/`, `dist/`, `build/`, `__pycache__/`, `*.egg-info/`, `*.min.js`, lockfiles), files over `INGEST_MAX_PARSE_BYTES` (512 KiB) and binaries (NUL in the first 8 KiB) are skipped. Syntax errors give `PARSED_WITH_ERRORS` with the error lines, and extraction continues.
- **Branch-diff-only parsing (deliberate improvement).** For an abandoned branch, `parse_branch_changes(service, branch_report)` parses only the non-deleted files in `BranchReport.diff.files`, at the branch tip. Untouched files are identical to their merge-base versions, so storing a full tree per branch would duplicate evidence.
- **Progress hook.** `parse_revision` and `parse_branch_changes` take an optional `on_file(FileInventory)` callback, called as each file is parsed, so telemetry streams while a scope is still being parsed.

#### Ingestion: pipeline, persistence and task (`apps/ingestion/pipeline.py`, `persistence.py`, `tasks.py`)
- **Pure pipeline.** `run_ingestion(workspace, *, emit) -> IngestionResult` runs `GitForensicService.analyze()`, parses the default branch's full tree, then parses the diff files of every unmerged branch (abandoned or not; the row keeps the flag). It returns frozen `BranchRow`/`ArtifactRow` dataclasses plus a summary and never touches the ORM. Every JSON payload goes through one serializer, `apps/ingestion/serialization.py::to_json`: dataclasses become dicts, enums become values, datetimes become ISO strings, paths become POSIX strings and timedeltas become seconds. NUL characters become U+FFFD because PostgreSQL rejects `\u0000` in `text` and `jsonb`.
- **Persistence.** `persist_ingestion(project, result)` runs in one `transaction.atomic()`. It deletes the project's previous `BranchSnapshot`/`ArtifactRecord` rows, `bulk_create`s the new ones and sets `project.default_branch`, `ingestion_summary` and `ingested_at`. Re-ingesting never duplicates rows, and a failure keeps the previous ingestion. Citations keep their copied snippet through `EvidenceCitation.artifact` `SET_NULL`.
- **`BranchSnapshot`.** One row per branch, the default branch included. `diff_summary` carries `ref`, `is_default`, `is_merged`, `merge_base_sha`/`merge_base_date`, `dormancy_days`, `files_changed`, `additions`, `deletions`, `unique_commit_count` and up to 50 commits (`sha`, `subject`, `author_name`, `committed_at`; `commits_truncated` marks the cut). The default branch adds `total_commit_count` and `first_commit_date`. The Autopsy's BranchNode renders from this without a second query.
- **`ArtifactRecord` granularity (for Task 4 citations).** A *scope* is the default branch (full tree) or one unmerged branch (its diff files at the branch tip); `branch_name` names it.
  - `AST_MODULE`: one per code file per scope. Metadata holds the file's symbols, imports, routes and stubs as evidence records (fingerprints, lines, snippets), the `ParseOutcome`, the commit SHA, the scope and `marker_fingerprints`. Files whose grammar is unavailable (`GRAMMAR_UNAVAILABLE`) also get a row, with no symbols, so their text is still available to the reasoning model.
  - `README_DOC`: one per Markdown file (headings and claims). `DEPENDENCY_MANIFEST`: one per manifest (dependency records and `manifest` kind).
  - `TODO_MARKER`: one per marker in any file type. Metadata is the marker record (`line_start`, `line_end`, `snippet`, `tag`, `text`, `owner`, `enclosing_symbol`, `fingerprint`) plus `commit_sha` and `scope`. `raw_content` is the snippet.
  - `COMMIT_LOG`: one per non-default branch (merged ones too), with `file_path=""`. Metadata holds the unique commits, merge-base, ahead/behind, dates, `dormancy_days` and per-file diff stats with patch-truncation flags. `raw_content` is the branch's combined unified diff.
  - File rows store the file text in `raw_content`, capped at `INGEST_MAX_STORED_CONTENT_BYTES` (default 256 KiB, cut at a line break and never inside a UTF-8 character; `raw_content_truncated` in metadata).
  - Skipped (vendored, too large, binary, unreadable) files and files without an extractor get no file row. Markers in files without an extractor still get rows. Per-status counts go to `Project.ingestion_summary["file_status_counts"][<branch>]`.
  - `embedding` stays NULL: there is no embedding provider yet, and $C(f)$ uses deterministic relevance.
- **Lifecycle (`ingest_repository_task(project_id)`, Celery `shared_task`, `acks_late=True`, `soft_time_limit`/`time_limit` from `INGEST_TASK_SOFT_TIME_LIMIT_SECONDS`/`INGEST_TASK_TIME_LIMIT_SECONDS`, no automatic retries).**
  1. Claim the project under `select_for_update`. It is allowed from `DORMANT`, `FAILED` or `AUTOPSY_COMPLETE` (re-ingest) and refused (no change) from `INGESTING` or later stages. The claim sets `INGESTING` and clears `status_detail`.
  2. Prepare `WORKSPACE_ROOT/<project_id>`. An old workspace is removed only if the path resolves to exactly itself inside `WORKSPACE_ROOT`; a symlink or junction there is refused.
  3. Clone. The demo builds PayPulse under `FIXTURE_ROOT` if it is missing (never `force`) and then runs `clone_local_repository_into_workspace`. Every other project uses `clone_into_workspace(repo_url)`, which re-validates the URL.
  4. Run `run_ingestion`, then `persist_ingestion`, then `on_ingestion_complete(project_id)`.
  - Any exception sets `FAILED` with a user-safe `status_detail` (`user_safe_error`), emits `ERROR` and logs the traceback. A bad URL or a failed clone is the user's to fix, so nothing retries.
  - `on_ingestion_complete` is the **only** place that decides what follows ingestion. It sets `SYNTHESIZING_GENOME`, emits `INGESTION_COMPLETE` with that status (through an `announce` callback, so it precedes every genome event) and enqueues `synthesize_genome_task` with `transaction.on_commit`. The ingestion task returns that hand-off status. The claim refuses `SYNTHESIZING_GENOME`, because the genome task is reading the artifacts.
  - The Windows `--pool=solo` worker does not enforce Celery time limits. `INGEST_CLONE_TIMEOUT_SECONDS` still bounds the clone.
- **Telemetry cadence.**
  - `INGESTION_STARTED` (task, after the claim; `name`, `source`, `repo_url`).
  - `BRANCH_DISCOVERED` per branch, default first (`name`, `head_sha`, `last_commit_date`, `is_default`, `is_merged`, `is_abandoned`, `commits_ahead`, `commits_behind`).
  - `AST_PARSED` while parsing, in batches of 25 non-skipped files per scope (`branch`, `scope`, `batch`, `files[{path, status, language, symbols, markers}]`, `skipped`, `files_processed`). Batching bounds the frame count on large repositories; skipped files are only counted.
  - `INGESTION_COMPLETE` (task, after rows and status are committed, so a client that refetches sees the data). It carries branch counts, artifact counts per type, file-status totals and the new `status` (`SYNTHESIZING_GENOME`).
  - `ERROR` (`stage`, `detail`) on failure.
  - Payloads never carry file content. Broadcast failures are logged and never fail the job.

### 3. `apps.genome`
- `IntentNode`: `id (UUID)`, `project (FK)`, `synthesis (FK GenomeSynthesis, null=True, SET_NULL)`, `title`, `category` (`CORE_FEATURE`, `ARCHITECTURE`, `INTEGRATION`, `DATA_MODEL`), `vital_status` (`VERIFIED_WORKING`, `ABANDONED_IN_PROGRESS`, `GHOST_FEATURE`), `epistemic_status` (`KNOWN`, `INFERRED`, `CONFLICTED`, `MISSING_CONTEXT`), `confidence_score (float)`, `reconstructed_intent`, `actual_implementation_state`, `graph_position (JSONField)`, `score_breakdown (JSONField)`.
- `GenomeSynthesis` (provenance of one run): `id (UUID)`, `project (FK)`, `mode` (`DETERMINISTIC`, `MODEL`, `MODEL_FALLBACK`), `model_id (blank when deterministic)`, `started_at`, `finished_at`, `catalog_size`, `input_digest` (SHA-256 of the request payload), `validation_report (JSONField)`, `raw_model_response` (capped at 200,000 characters), `node_count`, `conflict_count`. Migration `genome/0002` adds it with `IntentNode.synthesis` and `IntentNode.score_breakdown`.
- `EvidenceCitation`: `intent_node (FK, related_name="citations")`, `artifact (FK, null=True)`, `source_type`, `file_path`, `commit_sha (blank=True)`, `line_start (int, null=True)`, `line_end (int, null=True)`, `snippet (TextField)`, `weight (float)`.
- `ConflictRecord`: `project (FK)`, `intent_node (FK, related_name="conflicts")`, `claim_source`, `reality_source`, `conflict_description`, `resolution_recommendation`.

#### Genome: confidence scoring (`apps/genome/scoring.py`)
- **Pure service.** `score_feature(signals, *, conflict) -> FeatureScore` (breakdown, `EpistemicStatus`, `StatusReason`). Task 4.2 matches evidence to a feature and passes one frozen `EvidenceSignal(source_type, match_strength, age_days, observed_on_default_branch, is_stub, is_test)` per citation; scoring never guesses relevance. `score_intent_node` is the variant for nodes about to be persisted and refuses an empty list (forensic-invariants §1). `ConfidenceBreakdown.as_dict()` is plain JSON (per-type contributions, per-signal terms, bias, conflict penalty, parameters) for the Evidence Drawer in Task 4.3. All constants live in `ScoringParameters` (`DEFAULT_PARAMETERS`); the formula and rules are in forensic-invariants §2–3.
- **Deviations from the original §2–3 formula, and why:**
  - *Prior $\beta_0 = 0.5$ added to $z$.* Every original term is $\ge 0$, so without a conflict $C \ge \sigma(0) = 0.5$: zero evidence scored 0.5 (`INFERRED`), a lone README claim 0.552, and `MISSING_CONTEXT` (< 0.45) was unreachable. With the prior, zero evidence is 0.3775.
  - *Per-type diminishing returns ($\rho = 0.5$) and cap ($1.75 \cdot w_t$) instead of a plain sum.* The plain sum let ten TODOs reach `KNOWN` and made the score grow with a module's symbol count.
  - *$\Delta t$ defined as the age of the revision where the evidence was observed.* The original "commit age" would decay current code just for being old. HEAD evidence has $\Delta t = 0$; branch-only evidence uses the branch tip's dormancy. A signal on the default branch with $\Delta t \ne 0$, or an `UNMERGED_BRANCH_COMMIT` on the default branch, is rejected.
  - *`MATCH_STUB = 0.3` added* to the original 1.0 / 0.8 / 0.6 strengths: a `NotImplementedError` stub names the interface but proves no behavior.
  - *Status gates.* `KNOWN` also needs real (non-stub, non-test) AST code on the default branch; `INFERRED` also needs implementation-bearing evidence (AST, stub, unmerged-branch commit, or a spec with $s = 1.0$). TODO/README-only features are `MISSING_CONTEXT` regardless of count. `CONFLICTED` still overrides everything, and its $C(f)$ is still computed.
  - *Rounding.* $C(f)$ is rounded to 4 decimals and classified on the rounded value, so the stored score and status agree. The 0.75 / 0.45 thresholds are unchanged.
- **Calibration (tests in `tests/test_genome_scoring.py`).** PayPulse ledger (class, method, 3 tests, README) 0.8594 `KNOWN`; one AST symbol 0.6682 `INFERRED`; branch-only retry engine (543 days) 0.4846 `INFERRED`; one TODO 0.4428, ten TODOs 0.5099 (gated), README only 0.4280 and no evidence 0.3775, all `MISSING_CONTEXT`; webhook retries 0.4897, PDF export 0.4182 and multi-currency 0.5187, all `CONFLICTED`.

#### Genome: synthesis (`apps/genome/catalog.py`, `tokens.py`, `reconciler.py`, `schemas.py`, `agents.py`, `services.py`, `tasks.py`)
Every claim in the genome must cite real evidence, and repository text is untrusted input that can carry prompt injection. So the model never writes a citation, a line, a snippet, a score or a status. It only refers to catalog ids, and the server recomputes everything evidence-derived.

- **Evidence catalog (`catalog.py`, pure).** `build_catalog(ArtifactInput[], RepositoryContext)` turns the persisted `ArtifactRecord`/`BranchSnapshot` rows (or the identical pipeline `ArtifactRow`/`BranchRow`) into `CatalogItem`s with stable ids `E1…En`. Ids are assigned after sorting by default branch first, then branch, path, line, kind and fingerprint, so the same ingestion always gives the same ids. Each item copies `file_path`, `commit_sha`, `line_start`/`line_end`, the exact `snippet` and the `ArtifactRecord` pk from ingestion data.
  - Source types: AST symbols, routes and stubs at HEAD → `AST_IMPLEMENTATION`. Branch-only records → `UNMERGED_BRANCH_COMMIT`, except markers, which stay `TODO_COMMENT` with the branch age. Markers → `TODO_COMMENT`. README claims → `README_CLAIM`. Claims and sections of other docs (`docs/*.md`) → `ISSUE_SPEC`. Dependencies → `ISSUE_SPEC`, matched only as text mentions.
  - Branch evidence adds one `BRANCH_FILE` item per diff file (snippet = that file's patch, cut out of the `COMMIT_LOG` diff) and one `BRANCH_COMMIT` per unique commit (snippet = subject, no file or lines).
  - Not items: imports and module docstrings (they prove no behavior; docstrings stay on `ModuleInfo` for matching and titles) and README headings (page structure). A doc section's snippet is the exact text from its heading to the line before the next heading.
  - Symbols in test files are `is_test`. A branch record whose fingerprint also exists at HEAD is skipped as not branch-only. Merged branches contribute nothing.
  - `age_days` is 0 at HEAD and the branch tip's `dormancy_days` otherwise (§2).
- **Matching (`tokens.py`).** Words are split at non-alphanumerics and case changes, lowercased, stopword-filtered and stemmed with a small explicit stemmer (an irregular table for `conversion→convert` and `dispatcher→dispatch`, then plural, verb-suffix and silent-e rules). A text match needs `min(2, |feature stems|)` shared stems. The rule is deliberately asymmetric: a one-word item such as `Invoice` cannot match "PDF Invoice Export".
- **Deterministic reconciler (`reconciler.py`, pure).** It is both the offline genome and the draft the model refines.
  - Features: one per README feature claim (checked or not); one per unmerged branch that no claim's stems explain (titled from the branch name); one per default-branch code module that no claim covers. A claim covers a module when at least one stem is shared with the module's identifiers (path and symbol names) and `min(2, |claim|)` are shared overall, docstrings included. Identifier overlap counts double, and each module goes to its best claim only.
  - Attachments: module symbols → `OWN_CODE`; stubs → `STUB`; markers in owned files → `SAME_FILE`; `test_x.py` → the owner of `x.py` as `OWN_TEST`; test names, default-branch markers, doc sections, non-README claims and dependencies → `TEXT_MENTION` to every feature they match; the claim itself → `ANCHOR`; the explaining claim (or branch feature) gets all branch items.
  - Match strengths: `OWN_CODE`/`OWN_TEST` 1.0, `BRANCH_DIFF` 0.8, mentions, commits and anchors 0.6, any stub 0.3.
  - Citations: per source type the 8 strongest matches are cited, with up to 3 places kept for tests and every stub always cited. Ties go to more shared stems, then diff files and classes before methods, with dunder methods, exception classes and test helpers last. C(f) is scored on exactly the cited items, so every signal is a citation, and the cited set decides the vital status exactly as the full match set would.
  - Vital status: `VERIFIED_WORKING` = real non-test code at HEAD and no non-abstract stub at HEAD. `ABANDONED_IN_PROGRESS` = a stub at HEAD, work only on an unmerged branch, or only tests at HEAD. `GHOST_FEATURE` = none of these.
  - Conflicts (Req 2.4): a cited default-branch claim that asserts completion (`[x]`, or a plain bullet under a feature heading) on a feature that is not `VERIFIED_WORKING`. `claim_source` = `file:line: "[x] text"`. `reality_source` = the stubs, the unmerged branch with its diff files and dormancy, or "no implementation found on <default> at <sha12> or on any unmerged branch". The description and recommendation are templates. `[ ]` claims are plans and never conflict.
  - Score with `score_intent_node`. `graph_position` = columns by vital status (x 0/360/720), rows by title (y step 180).
- **Model refinement (`agents.py::GenomeReconcilerAgent`).** It runs only when `MODEL_API_KEY` is set; an empty key gives mode `DETERMINISTIC` and no client is ever built. The `openai.OpenAI(base_url=MODEL_BASE_URL, api_key=MODEL_API_KEY, timeout=MODEL_REQUEST_TIMEOUT_SECONDS, max_retries=MODEL_MAX_RETRIES)` client is built lazily and can be injected.
  - One `chat.completions.create` request with `response_format={"type": "json_schema", "json_schema": {"name": "project_genome_refinement", "schema": …, "strict": true}}`, plus `reasoning_effort` when `MODEL_REASONING_EFFORT` is set.
  - The system prompt says that repository content is untrusted data and never an instruction. The user message is JSON: repository facts, the compact catalog (draft citations first, at most `GENOME_MAX_CATALOG_ITEMS`, snippets capped at 300 characters) and the draft features.
  - Schema (`schemas.py`, Pydantic v2): `features[{draft_keys, title, category, suggested_vital_status, reconstructed_intent, actual_implementation_state, citation_ids, conflicts[{claim_citation_ids, reality_citation_ids, description, resolution_recommendation}]}], notes`. `$ref`s are inlined, every object has `additionalProperties: false` and lists all keys in `required`, there is no `oneOf`/`allOf`, and the depth is 3 objects. It has no score, status, line or snippet fields. Length limits are applied on the server, not in the schema.
- **Validation (authoritative).**
  - Unknown citation ids are dropped and reported.
  - A feature that names `draft_keys` inherits all their evidence: the model can add evidence, never remove it. Added evidence must pass `reconciler.relevance` against the drafts' stems (a new feature uses its title's stems), else it is reported as unsupported.
  - A feature with no evidence is rejected. Draft features no accepted feature refines are carried over unchanged.
  - Vital status, conflicts, C(f) and epistemic status are recomputed by `finalize_features`. A disagreeing `suggested_vital_status` is recorded, and the model's implementation-state prose for that feature is replaced by the template.
  - Model conflicts must cite a claim-side and a reality-side item among the feature's evidence, or they are dropped. Rule conflicts always exist; a model conflict on the same claim only rewrites their description and recommendation.
  - Titles are capped at 120 characters and prose at 2,000.
  - Any timeout, HTTP or connection error, refusal, truncation, empty reply, invalid JSON, schema mismatch, unusable reply, or a reply with no accepted feature (or more rejected than accepted) gives mode `MODEL_FALLBACK` with the deterministic genome and the reason in `validation_report`. Error text is redacted of the API key.
- **Provenance and persistence (`services.py`).** `persist_genome` refuses a node without citations before writing anything. Then, in one `transaction.atomic()`, it replaces the project's `IntentNode`/`EvidenceCitation`/`ConflictRecord` rows and writes a `GenomeSynthesis` row; older provenance rows are kept as history.
  - `EvidenceCitation` copies the catalog item (`artifact` = the `ArtifactRecord`, if it still exists).
  - Its `weight` is the item's term in C(f) (`SignalTerm.term` = authority weight × match strength × decay × rank factor), i.e. what that citation contributed before the per-type cap.
  - `IntentNode.score_breakdown` = `FeatureScore.as_dict()` plus the feature key, the vital-status rule and each citation's catalog id, match kind, strength and weight.
  - The API key and request headers are never stored.
- **Lifecycle (`tasks.py`).** `synthesize_genome_task(project_id)` (`acks_late`, `GENOME_TASK_SOFT_TIME_LIMIT_SECONDS`/`GENOME_TASK_TIME_LIMIT_SECONDS`, no retries) runs `run_genome_synthesis`:
  1. Claim the project from `SYNTHESIZING_GENOME` or `AUTOPSY_COMPLETE` (re-synthesis).
  2. `load_catalog`, then the agent, then `persist_genome` and the status `AUTOPSY_COMPLETE`, in one transaction.
  3. Emit `GENOME_NODE_CREATED` per node (`node_id`, `title`, `category`, `vital_status`, `epistemic_status`, `confidence`, `citation_count`, `graph_position`), `CONFLICT_DETECTED` per conflict, and `GENOME_COMPLETE` (counts per status, `mode`, `fallback_reason`).
  - Errors set `FAILED` with a user-safe detail and emit `ERROR` (`stage: "GENOME"`).
  - If `synthesize_genome_task.delay` raises (broker down), `start_genome_synthesis` runs the synthesis inline in the ingestion worker instead of leaving the project in `SYNTHESIZING_GENOME`.
- **PayPulse genome (deterministic; asserted in `tests/test_genome_reconciler.py`).**

  | Feature | Vital | Epistemic | C(f) | Citations | Conflict |
  | --- | --- | --- | --- | --- | --- |
  | Double-entry ledger with balanced, atomic postings | VERIFIED_WORKING | KNOWN | 0.8594 | 9 | no |
  | Invoices with line items, tax and a draft/issued/paid/void workflow | VERIFIED_WORKING | KNOWN | 0.8594 | 9 | no |
  | Outbound webhooks for invoice lifecycle events (module `paypulse/webhooks.py`) | VERIFIED_WORKING | KNOWN | 0.8665 | 9 | no |
  | Automated Exponential Backoff Webhook Retries | ABANDONED_IN_PROGRESS | CONFLICTED | 0.7951 | 15 | yes |
  | Multi-currency conversion (USD, EUR, GBP) with daily ECB rates | ABANDONED_IN_PROGRESS | CONFLICTED | 0.8980 | 12 | yes |
  | PDF Invoice Export | GHOST_FEATURE | CONFLICTED | 0.4182 | 2 | yes |

  The two conflicted `ABANDONED_IN_PROGRESS` features score higher than the 4.1 calibration (0.4897, 0.5187) because the real evidence is richer than those hand-picked signals. The retry feature also cites the architecture section, the HEAD spec test `test_dispatch_retries_with_exponential_backoff` and the branch code. Multi-currency cites the working `quantize`/`normalize_code` helpers next to the `convert` stub. C(f) measures how well the evidence is understood, not how finished the feature is; the conflict still forces `CONFLICTED`.

#### Genome: read API (`apps/genome/api.py`, `graph.py`, `services.py::load_genome_inputs`)
- **Route.** `GET /api/projects/{project_id}/genome`. The router lives in `apps.genome` and is mounted on the same `/api/projects/` prefix as `apps.projects.api` in `config/api.py`, which keeps the genome app from importing into the projects router.
- **Pure builder.** `graph.build_genome_graph(project, synthesis, features, branches) -> GenomeGraphOut` takes frozen dataclasses (`ProjectInput`, `SynthesisInput`, `FeatureInput` with `CitationInput`/`ConflictInput`, `BranchInput`). `FeatureInput.from_model` reads an `IntentNode` whose relations are prefetched. `inputs_from_features` wraps an in-memory reconciler or agent genome (feature ids `feature:<key>`), so the tests and Task 7's MCP server can build the graph without a database.
- **Loading (`load_genome_inputs`).** It loads the latest `GenomeSynthesis` (by `created_at`, without `raw_model_response`) and that synthesis's nodes only. It prefetches citations, annotated with the source `ArtifactRecord.branch_name` through `F()`, so `raw_content` and the embedding are never loaded. It also prefetches conflicts and reads the branches. The endpoint runs 6 queries for any genome size; the DB test asserts at most 8.
- **Statuses.**
  - 404: unknown project.
  - 409 `GENOME_PENDING`: the project is `DORMANT`, `INGESTING` or `SYNTHESIZING_GENOME`, even if an older genome is still stored, because it is about to be replaced.
  - 409 `GENOME_UNAVAILABLE`: any other status with no genome, `FAILED` included. The body carries `status_detail`.
  - Every 409 body also has `detail`, `project_id`, `status` and `telemetry_ws_path`, so the client can wait for `GENOME_COMPLETE`.
  - A `FAILED` project that still has a genome (a failed re-synthesis) gets 200, and `project.status` says `FAILED`.
- **Decisions.**
  - *No generic "artifact" nodes.* In PayPulse, README.md is cited by every claim-derived feature and the test modules by several. Shared-file hubs would mostly add edges that the Evidence Drawer already lists.
  - *Conflicts are edges to a `document` node.* The node is the documentation file that holds the contradicted claim, and the edge data names the claim (file, lines, citation id). The full record is in `conflicts`. Claims sit left of the features and branches right, so the graph reads claim → feature → reality.
  - *Claim resolution.* `claim_source` starts with the claim's `path:line[-end]` followed by `: `. The claim is the feature's README/ISSUE_SPEC citation at that location. Failing that, the location is parsed from the text and the edge has `claim_citation_id: null`. If no location can be found, the conflict is listed with `edge_id: null` and no edge is drawn.
  - *Citation branch.* The citation's branch is the artifact's `branch_name`. If the artifact row is gone (`SET_NULL`), it is the branch whose head is the citation's `commit_sha`, with the default branch winning ties. Branch-only means a non-default branch.
  - *Branch nodes.* Every non-default branch that a citation draws branch-only evidence from gets a node, and so does every abandoned branch, even uncited. Other branches appear only in `branches`.
  - *`signal_index`.* `persist_genome` writes citations in signal order, so pk order is signal order. `signal_index` is set when the source types line up with `score_breakdown.citations` and is null otherwise. It joins a citation to its `ScoreTermOut`.

##### Genome API contract (source of truth for `frontend/src/lib/types.ts`)
The schemas are in `backend/apps/genome/schemas.py` (`GenomeGraphOut` and below). The `forensic-schema-guard` hook (`PostFileSave` on `backend/apps/*/schemas.py`) will keep `types.ts` in sync with them once the frontend exists. Task 8 generates `types.ts` from this list.

```text
GenomeGraphOut {
  project:   { id: uuid, name, status, status_detail, default_branch }
  synthesis: null | { id: uuid|null, mode: DETERMINISTIC|MODEL|MODEL_FALLBACK, model_id, started_at, finished_at,
                      catalog_size, node_count, conflict_count, input_digest_prefix (12 chars),
                      validation: { dropped_citation_count, unsupported_citation_count, rejected_feature_count,
                                    dropped_conflict_count, vital_status_override_count, fallback_reason: str|null } }
  nodes: (FeatureNode | BranchNode | DocumentNode)[]          // discriminated by "type"
  edges: (EvidenceEdge | ConflictEdge)[]                      // discriminated by "type"
  citations:        { [feature_id]: CitationOut[] }           // Evidence Drawer
  conflicts:        ConflictOut[]                             // Conflict Inspector
  score_breakdowns: { [feature_id]: ScoreBreakdownOut }       // explainable C(f)
  branches:         BranchSummaryOut[]                        // every BranchSnapshot, default first
}
FeatureNode  { id: <IntentNode uuid>, type: "feature", position: {x, y},
               data: { feature_id, feature_key: str|null, title, category, vital_status, epistemic_status,
                       confidence_score, reconstructed_intent, actual_implementation_state,
                       citation_count, conflict_count, color_token } }
BranchNode   { id: "branch:<name>", type: "branch", position,
               data: { name, head_sha, last_commit_date, is_abandoned, is_merged, commits_ahead, commits_behind,
                       dormancy_days, citation_count, feature_ids: str[], color_token } }
DocumentNode { id: "document:<file_path>", type: "document", position,
               data: { file_path, conflict_count, feature_ids: str[], color_token: "conflict" } }
EvidenceEdge { id: "evidence:<feature_id>-><branch node id>", source: feature_id, target: branch node id, type: "evidence",
               data: { citation_count, source_types: EvidenceSourceType[], color_token } }
ConflictEdge { id: "conflict:<conflict id>", source: feature_id, target: document node id, type: "conflict",
               data: { conflict_id, claim_source, file_path, line_start, line_end, claim_citation_id: str|null,
                       color_token: "conflict" } }
CitationOut  { id, feature_id, source_type, file_path, commit_sha, line_start, line_end, snippet, weight,
               branch_name: str|null, is_branch_only, artifact_id: int|null, signal_index: int|null }
ConflictOut  { id, feature_id, claim_source, reality_source, conflict_description, resolution_recommendation,
               created_at, claim_citation_id, claim_file_path, claim_line_start, claim_line_end,
               document_node_id, edge_id }                    // the last six are null when unresolved
ScoreBreakdownOut { confidence, z, evidence_total, prior_bias, conflict, conflict_penalty,
                    contributions: { source_type, authority_weight, signal_count, uncapped, cap, contribution, capped,
                                     terms: { signal_index, rank, match_strength, age_days, decay, base_term,
                                              rank_factor, term }[] }[],
                    epistemic_status, status_reason, vital_status, vital_status_reason, feature_key, origin,
                    matched_evidence_count, citations: { signal_index, catalog_id, source_type, match_kind,
                                                         match_strength, weight }[], parameters: {…} }
BranchSummaryOut  { name, head_sha, last_commit_date, is_default, is_merged, is_abandoned, commits_ahead,
                    commits_behind, dormancy_days, node_id: str|null }
GenomeNotReadyOut (409) { detail, code: GENOME_PENDING|GENOME_UNAVAILABLE, project_id, status, status_detail,
                          telemetry_ws_path }
```
- **Color tokens.** The backend sends tokens and never hex values. The frontend maps them to the palette:

  | Token | Meaning | Color |
  | --- | --- | --- |
  | `alive` | `VERIFIED_WORKING` | Emerald `#10B981` |
  | `dormant` | `ABANDONED_IN_PROGRESS`, or an abandoned branch | Amber `#F59E0B` |
  | `ghost` | `GHOST_FEATURE` | Crimson `#EF4444` |
  | `conflict` | conflict edges and document nodes | Electric Violet `#8B5CF6` |
  | `neutral` | other branches, and evidence edges to non-abandoned branches | a muted gray (Slate `#0F141C` is the background, so it would be invisible) |

- **Layout.**
  - Feature nodes use the stored `graph_position`: x 0/360/720 by vital status, y step 180 by title.
  - Branch nodes sit at x = rightmost feature column + 360. Document nodes sit at x = leftmost − 360.
  - Each satellite node sits at the mean y of its features. Nodes are pushed down so they never come closer than 180, and uncited abandoned branches go below the last row.
  - The frontend registers custom node types `feature`, `branch` and `document`, and custom edge types `evidence` and `conflict`.
- **Ordering.**
  - Feature nodes by (x, y, title). Branch and document nodes by (y, name).
  - Evidence edges, then conflict edges, each in feature order.
  - Citations in `EvidenceSourceType` order, then by file and line. Branches with the default first, then by name.
- **Never sent:** `raw_content`, `raw_model_response`, the full `input_digest`, `fallback_detail` and embeddings. Snippets are the capped ingestion snippets. The PayPulse body is about 84 KB.

### 4. `apps.revival`
- `RevivalStrategy`: `id (UUID)`, `project (FK)`, `strategy_type` (`RESTORE`, `MODERNIZE`, `EXTRACT`), `title`, `summary`, `feasibility_score (float)`, `value_score (float)`, `debt_cost_score (float)`, `risk_score (float)`, `action_plan (JSONField)`, `generated_kiro_specs (JSONField)`, `approved_by_human (bool)`, `approved_at (DateTime, null=True)`.

### 5. `apps.foundry`
- **Four Sequential Agents (`apps/foundry/agents.py`):**
  1. `EnvironmentAgent`: Audits `requirements.txt`/`package.json` in workspace and resolves broken versions.
  2. `ArchitectureAgent`: Produces the file-level modification and branch-merge blueprint (`feat/webhook-retry-engine` -> `main`).
  3. `EngineeringAgent`: Applies the code synthesis and test updates inside the isolated workspace.
  4. `VerificationAgent`: Executes `pytest` inside the sandbox runtime (`SANDBOX_MODE=subprocess` or `docker` image `python:3.11-slim`, preview port `8010`) and captures exit code and logs.
- `FoundryRun`: `id (UUID)`, `project (FK)`, `strategy (FK)`, `agent_stage` (`ENVIRONMENT`, `ARCHITECTURE`, `ENGINEERING`, `VERIFICATION`, `COMPLETE`, `FAILED`), `sandbox_mode`, `before_test_output`, `after_test_output`, `tests_passed (bool)`, `iterations_used (int)`.

### 6. `apps.rebirth`
- `RebirthArtifact`: `id (UUID)`, `project (OneToOneField)`, `foundry_run (FK)`, `unified_diff_patch (TextField)`, `pr_title`, `pr_body_markdown`, `exported_kiro_specs_path`, `continuity_memory (JSONField)`, `preview_url (CharField, default="http://localhost:8010")`, `created_at`.

### 7. `apps.telemetry`
- Django Channels WebSocket consumer at `ws/projects/<uuid:project_id>/telemetry/` broadcasting structured JSON frames.

## 2. REST API (Django Ninja, mounted at `/api/` in `config/api.py`)
All request and response bodies are `ninja.Schema` (Pydantic v2) models; no DRF.

- `GET /api/health`: liveness check (no PostgreSQL or Redis access).
- **`POST /api/projects/ingest`** (`apps/projects/api.py`): the entry point for Requirement 1.1. No task text covered it, so Task 3.3 added it.
  - Body: `{"repo_url": "https://...", "name"?: str}` or `{"demo": "paypulse", "name"?: str}`. Exactly one source is required, and unknown fields are rejected.
  - `repo_url` is stripped and checked with `validate_remote_url` at this trust boundary. Local paths, `file://`, scp-style, credentialed or non-allowlisted URLs get a 422 that states the reason, before anything is written.
  - The endpoint creates a `DORMANT` `Project` (`is_golden_demo=True` for the demo, with the name derived from the URL's last path segment when omitted). It enqueues `ingest_repository_task.delay(project_id)` with `transaction.on_commit`, so the worker always finds the row.
  - Responses: `202 {project_id, name, status, is_golden_demo, telemetry_ws_path}`. If enqueueing raises (Redis down), the project is set to `FAILED` with a clear `status_detail` and the response is `503 {detail, project_id, status}`.
- **`GET /api/projects/{project_id}`**: `id`, `name`, `repo_url`, `is_golden_demo`, `status`, `status_detail`, `default_branch`, `created_at`, `updated_at`, `ingested_at`, `telemetry_ws_path` and `ingestion` (`branch_count`, `abandoned_branch_count` and `artifact_counts` per type, all counted live from the database, plus `file_status_counts` from the stored summary). Unknown ids return 404.
- **`GET /api/projects/{project_id}/branches`**: `BranchSnapshot` rows for the Autopsy's BranchNodes, default branch first: `name`, `head_sha`, `last_commit_date`, `is_default`, `is_abandoned`, `commits_ahead`, `commits_behind`, `diff_summary`.
- **`GET /api/projects/{project_id}/genome`** (`apps/genome/api.py`, Task 4.3): `200 GenomeGraphOut` (the React Flow graph, citations, conflicts, score breakdowns and branches of the latest synthesis), `404` for an unknown project, and `409 GenomeNotReadyOut` while no genome can be shown. The section "Genome: read API" and its "Genome API contract" give the details.
- Telemetry WebSocket: `ws/projects/<uuid:project_id>/telemetry/` (see `apps.telemetry`).
