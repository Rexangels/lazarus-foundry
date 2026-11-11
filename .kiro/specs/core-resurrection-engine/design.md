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
  4. `on_genome_complete(project_id, emit)` is the **only** place that decides what follows a genome. It runs revival strategy generation inline (see "Revival: strategy generation pipeline"), which emits `STRATEGY_GENERATED` per strategy. The task's return value adds `strategies` (`GENERATED`, `FAILED`, `SUPERSEDED`, `REFUSED`).
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
- `RevivalStrategy`: `id (UUID)`, `project (FK)`, `synthesis (FK StrategySynthesis, null=True, SET_NULL)`, `strategy_type` (`RESTORE`, `MODERNIZE`, `EXTRACT`), `title`, `summary`, `feasibility_score (float)`, `value_score (float)`, `debt_cost_score (float)`, `risk_score (float)`, `score_breakdown (JSONField)`, `action_plan (JSONField)`, `is_actionable (bool)`, `generated_kiro_specs (JSONField)`, `approved_by_human (bool)`, `approved_at (DateTime, null=True)`, `approved_token (char 64)`, `approved_by (char 255)`. Migration `revival/0003` adds the last two and the partial unique constraint `revival_revivalstrategy_one_approved_per_project` (`project` where `approved_by_human`), so a project has at most one approved strategy.
- `StrategySynthesis` (provenance of one generation run): `id (UUID)`, `project (FK)`, `genome_synthesis (FK GenomeSynthesis, null=True, SET_NULL)`, `mode` (`DETERMINISTIC`, `MODEL`, `MODEL_FALLBACK`; how the spec prose was written), `model_id`, `started_at`, `finished_at`, `input_digest`, `validation_report (JSONField)`, `raw_model_response`, `preserved_strategy_types (JSONField)`. Migration `revival/0002` adds it with `RevivalStrategy.synthesis`, `score_breakdown` and `is_actionable`, and changes the `action_plan` default to `{}`.

#### Revival: planning and scoring (`apps/revival/planner.py`, `scoring.py`)
The three strategies differ because the genome's evidence differs, not because of fixed constants. The planner turns the genome into one work order per strategy; the scorer turns each work order into V, D and R and then $\text{Score}(S_k)$ (forensic-invariants §4).

- **Pure planner.** `plan_strategies(genome: GenomeInputs, branches=None, *, dependencies=(), imports=None) -> (RESTORE, MODERNIZE, EXTRACT)`.
  - The genome is the same `GenomeInputs` the graph builder uses (`FeatureInput.from_model` for a persisted genome, `inputs_from_features` for an in-memory one), so the planner runs without a database.
  - `dependencies_from_artifacts` reads the default branch's dependency records (with `pinned_version`), and `import_graph_from_artifacts` resolves the default branch's Python imports (absolute, relative, `from pkg import mod`) to repository files plus third-party top-level modules. Both are optional.
  - A citation's role comes from ingestion data only: source type, file (test files by `is_test_path`, manifests by name), branch, lines and the reconciler's match kind (`score_breakdown["citations"]`, joined on `signal_index`). Branch diff files are `UNMERGED_BRANCH_COMMIT` citations with a file and no lines; their change type and size are read from the cited patch.
- **Strategy semantics.**
  - `RESTORE` finishes `ABANDONED_IN_PROGRESS` features in the current stack:
    - merge each abandoned branch that carries the feature's code (`MERGE_ABANDONED_BRANCH`; added files are `files_to_create`, others `files_to_modify`);
    - complete each non-abstract stub (`COMPLETE_STUB`; branch stubs depend on the merge);
    - when the feature has no code of its own at HEAD, make its HEAD tests pass (`FIX_FAILING_TEST`). Those tests are presumed failing and are never edited. The files to change are the modules under test (`test_x.py` → `x.py`, preferring what the test imports) and the HEAD files holding the feature's markers;
    - add tests (`ADD_TESTS`) when no item of the feature has a target test.
    - A `GHOST_FEATURE` is in scope only with a design document (an `ISSUE_SPEC` citation that is not a manifest) and no unmet external requirement. It then gets `IMPLEMENT_FROM_SPEC` (the paths the spec names: existing ones are modified, the rest created) plus `ADD_TESTS`.
    - Everything else goes to `deferred` with a `DeferralReason`: `NO_DETAILED_SPEC`, `NEEDS_NEW_DEPENDENCY`, `NEEDS_EXTERNAL_SERVICE`, `BRANCH_STILL_ACTIVE` (unmerged but not abandoned: left to its owner) or `NO_ACTIONABLE_EVIDENCE`.
  - `MODERNIZE` is `RESTORE` plus one `UPGRADE_DEPENDENCY` per exact pin on the default branch, cited by its manifest line. No target version is chosen (`requires_resolution=True`, `target_version: null`); the EnvironmentAgent resolves it in the sandbox.
  - `EXTRACT` copies the highest-value cluster of `VERIFIED_WORKING` features into `extracted/<slug>/` (`EXTRACT_MODULE` per feature, then one `ADD_TESTS` boundary test, `tests/test_public_api.py`).
    - Clusters are the connected components of verified features under the import graph (one feature per cluster without it), ranked by the sum of C(f), then citation count, then fewer outside files.
    - Files are copied, not moved, with import paths unchanged, so the copied tests run as they are. Import-closure files outside the cluster are copied too, and each one lowers the item's feasibility and raises R.
    - Other features are listed in `deferred` as `OUTSIDE_SELECTED_CLUSTER` or `NOT_VERIFIED_WORKING`.
- **External requirements.** `EXTERNAL_CAPABILITIES` is an explicit table (PDF rendering, QR codes, live FX rates, email, payment gateways, object storage) of trigger words and the packages that provide each capability. It is matched against the feature title and the claim, spec and marker snippets the feature cites.
  - A third-party dependency is unmet when no providing package is declared. An external service is always unmet, because the sandbox has no network.
  - A ghost with an unmet requirement is deferred. An abandoned feature stays in scope: its items carry the requirement as a constraint and a feasibility factor, and a service's live part is a partial deferral (`partial: true`).
- **PlanItem (Task 6's work order; stored in `RevivalStrategy.action_plan`).**

  ```text
  PlanItem {
    id: "<strategy>-NN"                    // in execution order
    kind: MERGE_ABANDONED_BRANCH | COMPLETE_STUB | FIX_FAILING_TEST | IMPLEMENT_FROM_SPEC
        | UPGRADE_DEPENDENCY | EXTRACT_MODULE | ADD_TESTS
    stage: ENVIRONMENT | ARCHITECTURE | ENGINEERING   // the FoundryRun.agent_stage that executes it
    feature_id, feature_key: str | null    // null for project-wide upgrades
    title, rationale
    files_to_modify, files_to_create: str[]
    target_tests: str[]                    // "path" or "path::test"; must pass afterwards, never edited
    branch: str | null                     // the branch to merge, or the branch a stub lives on
    evidence: { ref, kind: CITATION | DEPENDENCY, source_type, file_path, line_start, line_end,
                branch_name, citation_id, artifact_id }[]          // at least one
    effort: S | M | L                      // from file count and changed lines
    item_feasibility: float in [0, 1]
    feasibility_factors: { name, value, detail }[]                // their product is item_feasibility
    depends_on: str[]                      // earlier item ids only
    requires_resolution: bool              // true for upgrades
    dependency: null | { name, ecosystem, manifest, file_path, line_start, current_specifier,
                         current_version, target_version: null }
    external_requirements: { capability, kind, label, matched_terms, evidence, candidates }[]
    open_markers: str[]                    // "path:line: TODO ..." left in the item's files
    constraints: str[]                     // e.g. keep the signature, no network, copy don't move
    copies: { source, destination }[]      // EXTRACT: files copied verbatim before editing
    changed_lines: int
  }
  ```

  - Stages map to the Foundry: ENVIRONMENT (upgrades) → ARCHITECTURE (merges, extracts) → ENGINEERING (stubs, specs, failing tests, new tests). VERIFICATION runs the union of `target_tests`.
  - Ordering is stage, then feature (title, id; cluster rank for an extract), then kind, then file. Plans also carry `in_scope` (feature, feasibility, item ids, `partial`) and `deferred` (feature, reason, detail, evidence, external requirements, `would_touch`).
- **Scoring.** `score_revival_strategy(v, d, r, params) -> StrategyScore` validates its inputs (finite, in [0, 1]), rounds them to 4 decimals and applies the formula. `score_plan(plan) -> PlanScore` derives V (`value_ratio`), D (`debt_cost`) and R (`strategy_risk`) from the plan.
  - `PlanScore.model_fields()` gives the four `RevivalStrategy` score columns, which satisfy the existing check constraints.
  - `PlanScore.as_dict()` is the Defibrillator's JSON breakdown: score, weights, each term in points, the V derivation (verified ids, targets with feasibility), the D file list and the R base with every factor's count, rate, cap and adjustment.
- **Deliberate deviations and judgment calls.**
  - R is no longer a pure constant per type. It keeps the spec's bases and only rises through the three named factors above, so neutral evidence gives the spec's values.
  - `ADD_TESTS` is part of `RESTORE` for targets without any test, so the Foundry can verify what it changes.
  - Multi-currency is restored (`COMPLETE_STUB` of `convert` against an injectable rate source) rather than deferred whole: the stub's interface and the architecture note define the rate-provider boundary, and only the live ECB fetch needs the network. That live part is a partial deferral and costs the item the external-service factor.
  - `MODERNIZE` targets the same features as `RESTORE`, so its V is the same. Its extra cost (one more file, higher R) is real, and the value of current dependencies is not part of V as §4 defines it, so it scores lower unless the formula changes.
  - An `EXTRACT` with nothing verified has no items and V = 0, but the formula still gives it $100 \cdot (0.3 + 0.2 \cdot 0.65) = 43$. `StrategyPlan.is_actionable` is false so Task 5.3 can mark it.
- **PayPulse (deterministic; asserted in `tests/test_revival_planner.py`).**

  | Strategy | V | D | R | Score | In scope | Deferred |
  | --- | --- | --- | --- | --- | --- | --- |
  | RESTORE | 0.6810 | 0.3333 (5 files) | 0.30 (base + 5 commits behind) | 68.1 | Retries: merge `feat/webhook-retry-engine` (`paypulse/retry.py`, `tests/test_retry.py`), complete `delay_for` jitter, make `test_dispatch_retries_with_exponential_backoff` pass by wiring `paypulse/webhooks.py`. Multi-currency: complete `convert`, add `tests/test_currency.py` | PDF export (needs a PDF and a QR code library); multi-currency's live ECB feed (partial) |
  | MODERNIZE | 0.6810 | 0.4000 (6 files) | 0.70 (base + 0.05 + 2 pins) | 58.1 | RESTORE plus upgrades of `pytest==6.2.4` and `requests==2.25.1` | as RESTORE |
  | EXTRACT | 0.6667 | 0.6000 (9 files) | 0.40 (base + `currency.py`) | 57.3 | Ledger and invoices into `extracted/paypulse-ledger-invoices/` with their tests and a boundary test | webhooks (cluster worth 0.8665 vs 1.7188), the three non-verified features |

  Feature feasibilities: retries 0.68 (the merge: 0.85 × 0.8), multi-currency 0.4061 (0.75 × 0.95² × 0.6).

#### Revival: strategy generation pipeline (`apps/revival/services.py`)
- **Chaining.** Generation runs inline at the end of the genome task, through `apps.genome.tasks.on_genome_complete`, not as a chained Celery task. Deterministic generation takes milliseconds and reads the genome just committed. Running it in the same worker needs no second enqueue and no broker-down fallback, and the task returns only once the strategies are ready. With a model key, the prose refinement adds one bounded model request.
- **`generate_strategies(project, *, agent=None)`.**
  1. Load the latest genome with `load_genome_inputs`, plus the default branch's `DEPENDENCY_MANIFEST` and `AST_MODULE` rows (`dependencies_from_artifacts`, `import_graph_from_artifacts`).
  2. Plan with `plan_strategies` and score with `score_plans`.
  3. Build each skeleton (`build_spec_skeleton`) and validate its deterministic bundle. A `SpecValidationError` is raised before anything is written.
  4. `SpecRefinementAgent.refine` rewrites the prose. Model failures fall back inside the agent.
  5. Persist in one transaction under `select_for_update` on the project. The run first re-checks that the project is still `AUTOPSY_COMPLETE` and that the genome it planned from is still the latest `GenomeSynthesis`; otherwise it raises `StaleGenomeError` and writes nothing. It then writes one `StrategySynthesis` and the strategy rows.
- **Stored fields.**
  - The four score columns come from `PlanScore.model_fields()`, and `score_breakdown` is `PlanScore.as_dict()`.
  - `action_plan` = `{items: PlanItem[], in_scope, deferred, files, extract_root, risk_evidence}`.
  - `is_actionable` = `StrategyPlan.is_actionable`.
  - `generated_kiro_specs` = `{spec_name, requirements_md, design_md, tasks_md, evidence_index, mode, generated_at}`.
  - Nothing is written to disk here. Task 6.3 writes the files into the target workspace on the revival branch.
- **Regeneration and approval.** Unapproved strategies are always deleted and re-created, so they match the latest genome. An approved strategy is never modified or deleted, because it is the human-signed work order. Its type is skipped: the row keeps its scores, plan and specs, `StrategySynthesis.preserved_strategy_types` lists it, and `STRATEGY_GENERATED` reports it with `regenerated: false`. Its plan's feature ids may then refer to a previous genome; that plan is a frozen snapshot by design. Older `StrategySynthesis` rows are kept as history.
- **Telemetry.** `STRATEGY_GENERATED` per strategy, in `RESTORE`, `MODERNIZE`, `EXTRACT` order: `strategy_id`, `strategy_type`, `title`, `feasibility_score`, `value_score`, `debt_cost_score`, `risk_score`, `in_scope_count`, `deferred_count`, `item_count`, `is_actionable`, `approved_by_human`, `regenerated`, `spec_name`, `spec_mode`, `synthesis_id`. The events follow `GENOME_COMPLETE`.
- **Failure handling (`run_strategy_generation`, never raises).** The project stays `AUTOPSY_COMPLETE`. The genome is intact and viewable, and re-synthesis from `AUTOPSY_COMPLETE` retries generation; `FAILED` would leave only a full re-ingestion. The run records a user-safe `status_detail`, deletes unapproved strategies (they belong to the replaced genome), logs the traceback and emits `ERROR` with `stage: "REVIVAL"`. A stale run returns `SUPERSEDED` without an error. A project that is not `AUTOPSY_COMPLETE` is refused (`REFUSED`).
- **Frozen after approval.** `generate_strategies` raises `GenerationRefusedError` before planning unless the project is `AUTOPSY_COMPLETE`, and the persist step raises it under the lock if an approval committed meanwhile; nothing is written. `run_strategy_generation` reports `REFUSED` and records no failure. The failure path (`status_detail`, deleting unapproved rows) also runs only while the project is still `AUTOPSY_COMPLETE`. Genome re-synthesis (`GENOME_CLAIMABLE_STATUSES`) and re-ingestion (`CLAIMABLE_STATUSES`) already refuse a `STRATEGY_APPROVED` project, so no path regenerates an approved project's strategies.

#### Revival: the `.kiro` spec bundle (`apps/revival/specs.py`)
The bundle is what the resurrected repository inherits and what the Foundry executes, so it follows the format of this spec's own files. `render_spec_bundle(plan, plan_score, genome_inputs) -> SpecBundle(spec_name, requirements_md, design_md, tasks_md, evidence_index)` is pure and deterministic.
- **Spec name.** `lazarus-<strategy>-<project slug>`, at most 64 characters (`lazarus-restore-paypulse`).
- **`requirements.md`.** `## Introduction`, then `### Requirement N: <title>` with a `**User Story:**` line and numbered EARS criteria (`WHEN`/`IF … THEN`/`WHILE`/`WHERE`/`THE` … `SHALL`).
  - There is one requirement per in-scope feature, then "Current dependencies" when the plan upgrades pins, then a final "Verification" requirement.
  - Feature criteria come from the evidence:
    - the README claim (`THE <project> system SHALL provide …, as README.md:N claims`);
    - one criterion per plan item: the merge contains its diff files and nothing else, a stub returns instead of raising and keeps its signature (and resolves its marker), a failing test passes without being edited, a spec section is implemented, new tests run offline;
    - `IF <service> is unreachable, THEN …` for external services;
    - every target test (`THE test <id> SHALL pass`);
    - up to two quoted design notes.
  - Verification: `python -m pytest` exits 0, tests that passed before still pass, the target tests pass, and failures are repaired in the implementation within 3 self-healing iterations.
- **`design.md`.** The sections are Overview, Change Set, Merge Plan, Verification Strategy, Risks and Out of Scope, in that order.
  - Overview: the narrative plus the Score(S_k) breakdown (formula with numbers, V targets, D files, R base plus factors).
  - Change Set: one section per plan item with kind, stage, effort, the feasibility factors, rationale, files, copies, target tests, dependencies, the dependency pin, external requirements, open markers and constraints.
  - Merge Plan: per merged branch, its head, ahead/behind counts, dormancy, files, the task order and the conflict policy.
  - Out of Scope: every deferred feature with its `DeferralReason`, detail, `would_touch` and partial flag.
- **`tasks.md`.** `- [ ] N. <title>` per plan item in `depends_on` order, then a final "Verify the revival" task.
  - Each task carries a description and detail bullets (plan item, kind, stage, effort, feasibility, requirement, branch, `Depends on: task …`, files, target tests, evidence).
  - Each task then has `  - [ ] N.M` sub-tasks: the primary action, one per constraint, one per open marker, and a final `Verify: run python -m pytest <target tests> …` step.
- **Evidence footnotes.** Every criterion, task, change-set entry, merge and deferral cites `[^E<n>]` footnotes. Labels are assigned in order of first use and are shared by the three files. Each file defines the labels it uses (`` [^E3]: `paypulse/retry.py:28-66` · UNMERGED_BRANCH_COMMIT · on `feat/…` · citation `…` ``). `evidence_index[label]` holds `ref`, `kind`, `source_type`, `file_path`, the lines, `location`, `branch_name`, `citation_id`, `artifact_id` and the cited snippet (300 characters).
- **Untrusted text.** Repository-derived text (titles, rationales, constraints, markers, quotes) goes through `inline_text`: one line, leading list/heading/checkbox markers stripped, backticks dropped, and `\ [ ] < > |` escaped. It cannot open a code span, footnote, checkbox, list item or HTML. Only the renderer writes code spans and footnote references.

#### Revival: spec prose refinement contract (`apps/revival/agents.py`, `schemas.py`)
- Follows the `GenomeReconcilerAgent` pattern: it runs only with `MODEL_API_KEY`, and an empty key gives `DETERMINISTIC` with no client built. It makes one `chat.completions.create` with strict `json_schema` (`revival_spec_prose`), uses `reasoning_effort` when set, and has a system prompt that marks repository content as untrusted data.
- **The model rewrites prose only:** each bundle's `introduction` and `design_overview`, each requirement's `user_story` (by `requirement_id`) and each task's `description` (by `task_id`). The schema has no field for requirements, criteria, tasks, files, tests or evidence. Its objects are response → bundle → story or description (4 objects, depth 3), each with `additionalProperties: false`, every key required, and no `oneOf`/`allOf`. An empty string keeps the draft.
- **Server-side validation:**
  - unknown or repeated bundles are dropped, and so are unknown or duplicate ids;
  - prose is sanitized with `inline_text` and capped (introduction 1,500, overview 2,000, story 600, description 800 characters);
  - prose that names a file the plan does not know is dropped (`UNKNOWN_FILE`), and a story must read "As …, I want …";
  - the Markdown is re-rendered from the skeleton plus the accepted prose and must validate.
- **Fallback.** `MODEL_FALLBACK` keeps the deterministic skeletons and records the reason. It applies on a timeout, an HTTP or connection error, a refusal, truncation, an empty reply, invalid JSON, a schema mismatch, a refined bundle that does not validate (`MODEL_BUNDLE_INVALID`), or no usable prose (`MODEL_NO_USABLE_PROSE`). The digest and the capped raw reply are stored on `StrategySynthesis`, and the key never is.

#### Revival: spec format validator (`specs.validate_spec_bundle`, pure)
`spec_bundle_problems(bundle, known_files=None)` lists every problem, and `validate_spec_bundle` raises `SpecValidationError` with all of them. It checks:
- the spec name is a kebab-case slug;
- `requirements.md`: the `# Requirements` heading, Introduction and Requirements sections, `Requirement N` numbered 1…n, exactly one `**User Story:** As …, I want …` per requirement, `#### Acceptance Criteria` with criteria numbered 1…k that are EARS (keyword first, `SHALL`, `IF` needs `THEN`), at least one footnote per requirement, and a "Verification" requirement;
- `tasks.md`: the `# Implementation Plan` heading; every checkbox line is `- [ ] N. ` or `  - [ ] N.M ` (anything else, such as deeper nesting, is malformed); tasks are numbered 1…n and sub-tasks N.1…N.m under their parent; every task has sub-tasks, its last sub-task starts with `Verify:`, and it cites at least one footnote;
- `design.md`: the `# Design` heading and the six sections in order;
- all files: every footnote reference resolves in `evidence_index` and is defined in the same file, every definition is in the index, and every file path in a code span is in `known_files`. `known_files` covers the plan's files, test files, copies, evidence paths, `would_touch` and the in-scope features' citation paths.

#### Revival: read and approval API (`apps/revival/api.py`, `approval.py`, `services.py`)
The Defibrillator's backend and the human approval gate. The Foundry may only change code for a strategy with `approved_by_human == True`, and the approval covers exactly the content the human reviewed.
- **Routes.** The router lives in `apps.revival` and is mounted on `/api/projects/` next to the projects and genome routers.
  - `GET /api/projects/{project_id}/strategies`: 200 `StrategiesOut`, 404 for an unknown project, 409 `STRATEGIES_PENDING` while the project is `DORMANT`, `INGESTING` or `SYNTHESIZING_GENOME`, and 409 `STRATEGIES_UNAVAILABLE` when no strategy exists in any other status (generation failed; the body carries `status_detail`). A project past `AUTOPSY_COMPLETE` still gets 200 with its frozen strategies. The endpoint runs 3 queries (project, strategies, latest `StrategySynthesis` without `raw_model_response`); the DB test asserts at most 5.
  - `POST /api/projects/{project_id}/strategies/{strategy_id}/approve`, body `{"approval_token": "<64 lowercase hex>"}` (extra fields, a missing, blank or malformed token: 422).
  - `GET /api/projects/{project_id}/strategies/{strategy_id}/specs/{doc}`: one document as `text/markdown; charset=utf-8` with `Content-Disposition: inline; filename="<spec_name>-<doc>"` (`?download=true` gives `attachment`). `doc` is checked against the allowlist `requirements.md`, `design.md`, `tasks.md` before any query. Any other name is 404, and a path with `/` never matches the route. The file name stem is the spec name reduced to `[a-z0-9-]`.
- **Approval token (`approval.approval_token`, pure).** The SHA-256 hex of canonical JSON (sorted keys, no whitespace, UTF-8, `allow_nan=False`) of `{version: "lazarus-approval-v1", strategy_id, strategy_type, scores: {feasibility_score, value_score, debt_cost_score, risk_score}, action_plan, specs: {requirements.md, design.md, tasks.md}}`. Sorted keys make it stable across the JSONB round trip. `evidence_index`, `mode` and `generated_at` are not hashed, because every footnote a document uses is defined in its own Markdown. GET returns the token of each strategy's current content; approve must send it back.
- **Recommendation (`approval.recommended_strategy_id`, pure).** The highest `feasibility_score` among actionable strategies. Ties go to `RESTORE`, `MODERNIZE`, `EXTRACT` order, then to the smaller id. It is null when no strategy is actionable. PayPulse recommends `RESTORE` (68.1).
- **Approval (`services.approve_strategy`).** One transaction, `select_for_update` on the project, then the strategy. The checks run in this order:
  1. 403 `APPROVER_REQUIRED` (in the view, before any query) when `REVIVAL_APPROVAL_REQUIRES_HEADER` is on and `X-Lazarus-Approver` is missing, blank, longer than 255 characters or not printable.
  2. 404 when the project is unknown or the strategy belongs to another project.
  3. An already approved strategy: the same token returns 200 with `already_approved: true`, the original `FoundryRun` and no writes or events. A different token is 409 `STRATEGY_STALE`. If the stored `approved_token` no longer matches the content (the row was edited after approval), the response is 409 `APPROVED_CONTENT_CHANGED`.
  4. 409 `ANOTHER_STRATEGY_APPROVED` (with `approved_strategy_id`) when another strategy of the project is approved.
  5. 409 `PROJECT_NOT_READY` unless the project is `AUTOPSY_COMPLETE`.
  6. 409 `STRATEGY_STALE` with `current_approval_token` when the token does not match the current content.
  7. 409 `STRATEGY_NOT_ACTIONABLE`.
  - Success sets `approved_by_human`, `approved_at`, `approved_token` (the verified token) and `approved_by` (the header value, or blank), moves the project to `STRATEGY_APPROVED`, and creates a `FoundryRun` (`agent_stage=ENVIRONMENT`, `sandbox_mode=settings.SANDBOX_MODE`). The response is `ApprovalOut` with the approved strategy, `foundry_run_id`, `project_status` and `telemetry_ws_path`.
  - The partial unique constraint backs rule 4 in the database. An `IntegrityError` from it is reported as `ANOTHER_STRATEGY_APPROVED`.
  - `approval.approval_is_intact(strategy, strategy.approved_token)` is the check Task 6 runs before each Foundry stage. GET reports it as `approval.approval_intact` for approved strategies.
- **Foundry hand-off (`services.on_strategy_approved(strategy, foundry_run)`).** It runs after the approval commits (`transaction.on_commit`) and never raises. It is the single place that decides what follows an approval, as `on_ingestion_complete` is for ingestion and `on_genome_complete` for the genome. Today it logs and emits `STRATEGY_APPROVED` (`strategy_id`, `strategy_type`, `title`, `feasibility_score`, `approved_at`, `approved_by`, `foundry_run_id`, `agent_stage`, `sandbox_mode`, `status`). The `FoundryRun` row in `ENVIRONMENT` is the queryable "approved, Foundry queued" state. Task 6.2 makes this function enqueue the Foundry pipeline for that run; nothing is enqueued before that task exists.
- **Security.** The API has no authentication yet. In local development anyone who can reach the server can approve a strategy. `REVIVAL_APPROVAL_REQUIRES_HEADER` (default `False`, in `.env.example`) makes approval require `X-Lazarus-Approver: <name>`, which is stored as `approved_by`. This gives a hosted demo a minimal gate and an audit name. It is not authentication, because any client can send the header. Real authentication replaces it when the API gets users.
- **Decisions.**
  - *Regenerated rows get new ids.* Strategy regeneration deletes and re-creates unapproved rows, so a client that holds an old strategy id gets 404 on approve, not `STRATEGY_STALE`. `STRATEGY_STALE` covers in-place changes such as an admin edit. Clients should refetch on 404 and on every `STRATEGY_GENERATED` event.
  - *`APPROVED_CONTENT_CHANGED`* is an extra 409 code beyond the five planned. Without it, a tampered approved row would loop the client between `STRATEGY_STALE` and refresh.
  - *Plan entries are passed through as JSON objects.* `action_plan.items`, `in_scope` and `deferred` are typed `dict[str, Any]`; their shape is the PlanItem block above and the planner is its source of truth.

##### Revival API contract (source of truth for `frontend/src/lib/types.ts`)
The schemas are in `backend/apps/revival/schemas.py` (`StrategiesOut` and below).

```text
StrategiesOut {
  project:   { id: uuid, name, status, status_detail, default_branch }
  synthesis: null | { id: uuid, mode: DETERMINISTIC|MODEL|MODEL_FALLBACK, model_id, started_at, finished_at,
                      preserved_strategy_types: str[],
                      validation: { fallback_reason: str|null, refined_bundle_count, applied_prose_count,
                                    dropped_prose_count } }
  recommended_strategy_id: uuid|null
  approved_strategy_id:    uuid|null
  telemetry_ws_path
  strategies: StrategyOut[]                                   // RESTORE, MODERNIZE, EXTRACT
}
StrategyOut { id: uuid, strategy_type: RESTORE|MODERNIZE|EXTRACT, title, summary,
              feasibility_score, value_score, debt_cost_score, risk_score,
              score_breakdown: {…},                           // PlanScore.as_dict()
              is_actionable, is_recommended,
              action_plan: { items: PlanItem[], in_scope: {…}[], deferred: {…}[], files: str[],
                             extract_root: str|null, risk_evidence: {…} },
              spec: { spec_name, requirements_md, design_md, tasks_md, evidence_index: {[label]: {…}},
                      mode, generated_at, document_urls: { requirements_md, design_md, tasks_md } },
              approval: { approved_by_human, approved_at: datetime|null, approved_by, approval_token,
                          approval_intact: bool|null },
              synthesis_id: uuid|null, created_at }
StrategiesNotReadyOut (409) { detail, code: STRATEGIES_PENDING|STRATEGIES_UNAVAILABLE, project_id, status,
                              status_detail, telemetry_ws_path }
ApproveRequest   { approval_token: 64 hex }
ApprovalOut      { strategy: StrategyOut, foundry_run_id: uuid|null, project_status, already_approved,
                   telemetry_ws_path }
ApprovalErrorOut (409) { detail, code: PROJECT_NOT_READY|STRATEGY_STALE|STRATEGY_NOT_ACTIONABLE
                                       |ANOTHER_STRATEGY_APPROVED|APPROVED_CONTENT_CHANGED,
                         project_id, strategy_id, current_approval_token: str|null,
                         approved_strategy_id: uuid|null }
ApproverRequiredOut (403) { detail, code: "APPROVER_REQUIRED" }
```

### 5. `apps.foundry`
- **Four Sequential Agents (`apps/foundry/agents.py`):**
  1. `EnvironmentAgent`: Audits `requirements.txt`/`package.json` in workspace and resolves broken versions.
  2. `ArchitectureAgent`: Produces the file-level modification and branch-merge blueprint (`feat/webhook-retry-engine` -> `main`).
  3. `EngineeringAgent`: Applies the code synthesis and test updates inside the isolated workspace.
  4. `VerificationAgent`: Executes `pytest` inside the sandbox runtime (`SANDBOX_MODE=subprocess` or `docker` image `python:3.11-slim`, preview port `8010`) and captures exit code and logs.
- `FoundryRun`: `id (UUID)`, `project (FK)`, `strategy (FK)`, `agent_stage` (`ENVIRONMENT`, `ARCHITECTURE`, `ENGINEERING`, `VERIFICATION`, `COMPLETE`, `FAILED`), `sandbox_mode`, `before_test_output`, `after_test_output`, `tests_passed (bool)`, `iterations_used (int)`.

#### Foundry: sandbox (`apps/foundry/sandbox.py`, `sandbox_env.py`, `sandbox_process.py`, `junit.py`)
The sandbox runs installs and tests for code we don't control. Its security matters more than its convenience, so it is safe by default and states its isolation honestly in every result.

- **Factory and policy.** `get_sandbox(project, run_id, *, python_version=None, mode=None) -> Sandbox`.
  - `mode` defaults to `SANDBOX_MODE`. `python_version` defaults to `SANDBOX_DEFAULT_PYTHON` (3.11) and must be in `SANDBOX_PYTHON_VERSIONS` (3.8–3.13); the EnvironmentAgent picks it.
  - `run_id` (the `FoundryRun` id) must match `[A-Za-z0-9][A-Za-z0-9_.-]{0,63}`; it names the run directory, the venv volume and the labels.
  - Subprocess mode for a project that is not `is_golden_demo` raises `UntrustedSandboxError`, unless `SANDBOX_ALLOW_SUBPROCESS_FOR_UNTRUSTED=True` (default False; logged as a warning on every use).
  - `project.workspace_path` must resolve inside `WORKSPACE_ROOT` (`git_forensics.resolve_workspace_dir(..., require_empty=False)`). `SANDBOX_RUNS_ROOT` must not overlap `WORKSPACE_ROOT` (checked in settings and again at use).
  - A workspace or runs root inside a OneDrive folder (the `OneDrive*` variables or a `OneDrive`/`OneDrive - <org>` path component) logs a warning recommending a non-synced path.
- **Docker mode: the only mode for untrusted repositories.** Python `docker` SDK; image `SANDBOX_DOCKER_IMAGE_TEMPLATE` (`python:{version}-slim`), pulled when missing. Every command is a fresh container, removed afterwards.
  - Hardening on every container: `user="1000:1000"`, `cap_drop=["ALL"]`, `security_opt=["no-new-privileges:true"]`, `read_only=True`, `tmpfs={"/tmp": "rw,nosuid,nodev,size=512m"}`, `mem_limit` = `memswap_limit` = `SANDBOX_MEMORY_LIMIT` (1g, no swap), `nano_cpus` from `SANDBOX_CPUS` (1.0), `pids_limit` = `SANDBOX_PIDS_LIMIT` (256), `privileged=False`, `init=True`, `ipc_mode="private"`, json-file logs capped at 20 MB, and the labels `lazarus.sandbox=true`, `lazarus.foundry_run`, `lazarus.project`, `lazarus.role`.
  - Mounts are only the workspace (`/workspace`), the run's results directory (`/lazarus-results`) and the per-run named volume `lazarus-venv-<run_id>` (`/opt/lazarus-venv`). The Docker socket is never mounted.

    | Phase | Network | Workspace | Results | Venv volume |
    | --- | --- | --- | --- | --- |
    | setup (`chown`, then `venv.create`) | `none` | not mounted | not mounted | rw |
    | install | `bridge` (`none` with `network=False`) | read-only | not mounted | rw |
    | execute (`run`, `run_pytest`) | `none` | rw | rw | read-only |
    | service (`start_service`) | per-run bridge network | rw | rw | read-only |

  - The venv lives in the volume, not the workspace, so it never shows up in the Git diff. A read-only venv during execute stops tests from tampering with the installed packages between runs.
  - *Root exception:* a new named volume is root-owned and the slim images have no uid 1000. One init container runs `chown 1000:1000 /opt/lazarus-venv` as root with only `CAP_CHOWN`, no network and no workspace mount. Nothing from the repository runs as root.
  - *Install-phase exposure:* the default bridge can reach the package index but also the host (`host.docker.internal`) and the LAN. Installs are therefore wheels-only (`--only-binary=:all:`) unless the caller passes `allow_source_builds=True`, because a source build runs the package's own code with that network. Requirements files go through `validate_requirements_file`: `-r`/`-c` includes must stay inside the workspace, and index, find-links, trusted-host, editable, `--no-binary`, URL and local-path requirements are refused.
  - *Preview exception:* Docker cannot publish a port from a `network_mode="none"` container. `start_service` (only on explicit request) uses a per-run bridge network `lazarus-net-<run_id>`, isolated from other containers but with outbound access, and publishes `SANDBOX_PREVIEW_PORT` (8010) on `127.0.0.1` only.
  - Timeouts: a timer kills the container at the command timeout (`timed_out=True`, exit 137). `container.wait` has a 30 s grace as a backstop.
  - Cleanup: `close()` removes every container, volume and network labelled with the run id, then the run directory. `sweep_orphaned_sandboxes()` removes everything labelled `lazarus.sandbox=true` plus stale run directories; it runs on the Celery `worker_ready` signal (`apps/foundry/apps.py`), when no run can be active under `--pool=solo`.
  - Linux hosts: uid 1000 must be able to write the workspace bind mount (Docker Desktop on Windows and macOS maps permissions, so this only matters on native Linux). The results directory is made world-writable inside the private run directory.
- **Subprocess mode: trusted fixtures only.** A venv in `SANDBOX_RUNS_ROOT/<run_id>/venv`, outside the workspace.
  - The interpreter is `py -X.Y` on Windows or `pythonX.Y` on PATH elsewhere, else this process's Python. The version actually used is reported (`python_version`) and a mismatch is logged.
  - Commands run in their own process group (`CREATE_NEW_PROCESS_GROUP` on Windows, a new session on POSIX). On timeout the whole tree is killed (`%SYSTEMROOT%\System32\taskkill.exe /F /T /PID`, or `killpg`).
  - There is no network, filesystem or process isolation, and every result says `network_isolated=False`. `network=False` only adds pip's `--no-index`. A process that leaves the tree (or a grandchild that outlives a normal exit on Windows) is not tracked.
- **Secrets never enter the sandbox.** Both modes build the child environment from an allowlist (`sandbox_env`).
  - Subprocess: only `PATH` (venv first) and, on Windows, `SYSTEMROOT`, `SYSTEMDRIVE`, `WINDIR`, `COMSPEC`, `PATHEXT`, `NUMBER_OF_PROCESSORS`, `PROCESSOR_ARCHITECTURE` and `OS` are inherited. `HOME`/`USERPROFILE`/`APPDATA`/`LOCALAPPDATA`/`TEMP`/`TMP`/`TMPDIR` point into the run directory.
  - Docker: nothing is inherited. Image-defined variables (`PYTHON_VERSION`, and `GPG_KEY`, the public ID of the CPython release key) remain.
  - Fixed values: `LANG=C.UTF-8`, `PYTHONHASHSEED=0`, `PYTHONDONTWRITEBYTECODE=1`, `PYTHONNOUSERSITE=1`, `PYTHONIOENCODING=utf-8`, `PIP_DISABLE_PIP_VERSION_CHECK=1`, `PIP_NO_INPUT=1`, `VIRTUAL_ENV`.
  - `env_overrides` refuses names that look like secrets (`DATABASE_URL`, `REDIS_URL`, `SECRET_KEY`, `MODEL_API_KEY`, and any `KEY`/`TOKEN`/`SECRET`/`PASSWORD`/`CREDENTIAL`/`AUTH`/`DSN` name part) and the sandbox-managed names (`PATH`, `HOME`, `VIRTUAL_ENV`, ...).
- **API.** `Sandbox` is a context manager and not thread-safe. Every argv is a list; there is never a shell. `python` and `pip` as argv[0] mean the sandbox venv's.
  - `create_environment() -> ExecResult` creates the venv and records the actual Python version.
  - `install(requirements_files=(), *, packages=(), commands=(), network=True, allow_source_builds=False, find_links=None, timeout=None) -> tuple[ExecResult, ...]` stops at the first failure. `find_links` (a local wheelhouse) is subprocess-only, because Docker mounts nothing but the workspace and results.
  - `run(argv, *, timeout=None, env_overrides=None, on_output=None) -> ExecResult`. `ExecResult` has `argv`, `phase`, `exit_code`, `stdout`/`stderr` (each capped at `SANDBOX_MAX_OUTPUT_BYTES`, 1 MiB, with `*_truncated`), `duration_seconds`, `timed_out`, `mode`, `network_isolated`, `image` and `python_version`. `on_output(stream, line)` gets lines as they arrive, serialized, for SANDBOX_LOG telemetry (the caller batches). It stops at the cap with one `[lazarus] stdout truncated` line, and a failing callback is dropped without failing the command.
  - `run_pytest(test_ids=None, *, timeout=None, extra_args=(), env_overrides=None, on_output=None) -> TestRunResult` runs `python -m pytest -p no:cacheprovider --junitxml=<results>/junit.xml -q [ids]`. Ids that start with `-` are refused. The result carries the `ExecResult`, the parsed `JUnitReport` (per case: `nodeid`, `outcome` passed/failed/error/skipped, `duration_seconds`, `message` capped at 4,000 characters, `is_collection_error`; totals), `junit_error` when the report is missing or rejected, `collection_error` (a `collection failure` case, or exit 2 with "error during collection"), and `succeeded`.
  - `start_service(argv, *, host_port=None) -> ServiceHandle` (`url`, `stop() -> ExecResult`); `close()` stops services too.
  - `info -> SandboxInfo` gives mode, requested and actual Python, image, `network_isolated` (execute phase) and plain-language isolation notes for the FoundryRun log.
- **JUnit report (`junit.py`).** It is written by sandboxed code, so it is untrusted. It is read only as a regular, non-symlink file directly in the results directory, at most 10 MiB, as UTF-8. Any `<!DOCTYPE`/`<!ENTITY` is rejected before parsing (no entity expansion, no external entities). `defusedxml` is used when installed (it is not a dependency); otherwise `xml.etree` parses the checked bytes. Both pytest families parse: xunit2 (pytest 5.1+ through 9, `<testsuites>`) and xunit1/pre-5.1 (`<testsuite>` root, `file` attribute). Node ids are rebuilt from `classname` using the workspace's files (`tests.test_x.TestA` → `tests/test_x.py::TestA::name`). Totals are counted from the cases, and a test that fails and then errors in teardown counts as failed.
- **Observed PayPulse results** (`tests/test_foundry_sandbox_paypulse.py`; real runs on Windows):

  | Mode | Python | Dependencies | Result |
  | --- | --- | --- | --- |
  | subprocess, offline | 3.13.5 | pytest 9.2.0 from a local wheelhouse | 1 failed (`test_dispatch_retries_with_exponential_backoff`), 15 passed |
  | subprocess, PyPI | 3.13.5 | `requirements.txt` (pytest 6.2.4, requests 2.25.1) | same; pytest 6.2.4 imports but emits ~393 `ast.Str`/`ast.NameConstant` DeprecationWarnings (removed in Python 3.14) |
  | subprocess, PyPI | 3.10.0 | `requirements.txt` | same, no warnings |
  | docker | 3.9, 3.11 | `requirements.txt` | written, not yet run (no Docker daemon was reachable) |

  The EnvironmentAgent can keep PayPulse's pins on Python ≤ 3.13. They will break on 3.14.
- **Deliberate deviations and judgment calls.**
  - `python_version` is optional and defaults to `SANDBOX_DEFAULT_PYTHON`. `get_sandbox` also takes `docker_client_factory` so tests can pass an SDK test double.
  - Wheels-only installs by default, and the requirements-file allowlist, are additions to the task text, made because the install phase has network.
  - `find_links` is not available in Docker mode, so the "workspace and results only" mount rule holds.
  - Subprocess mode cannot enforce that a preview binds 127.0.0.1. It only passes `HOST=127.0.0.1`, which is one more reason the mode is trusted-only.
  - Image tags are mutable, and one `{version}` template cannot hold a digest per version. Reproducible pulls would need images mirrored to a registry you control; this is not done.

### 6. `apps.rebirth`
- `RebirthArtifact`: `id (UUID)`, `project (OneToOneField)`, `foundry_run (FK)`, `unified_diff_patch (TextField)`, `pr_title`, `pr_body_markdown`, `exported_kiro_specs_path`, `continuity_memory (JSONField)`, `preview_url (CharField, default="http://localhost:8010")`, `created_at`.

#### Rebirth: building the artifact (`apps/rebirth/builder.py`, pure; `services.py`, ORM)
- **Trigger.** `apps.foundry.services.on_foundry_complete` calls `run_rebirth(run, emit=...)` for a run that is `COMPLETE` with `tests_passed`, while the project is `FOUNDRY_RUNNING`. It never raises. The strategy's approval is re-checked first (`approval_is_intact`), so the exported specs are exactly the approved bundle.
- **Workspace state it starts from.** The revival branch `lazarus/<strategy>-<run>` holds the ArchitectureAgent's merge commits; the EngineeringAgent's edits and repairs are uncommitted. The base is `report.environment.base_sha` (the default branch's HEAD when the run started), and it must be an ancestor of HEAD.
- **Steps (`build_rebirth`).**
  1. Write `requirements.md`, `design.md` and `tasks.md` from `RevivalStrategy.generated_kiro_specs` to `.kiro/specs/<spec_name>/` with `workspace.write_text`. The spec name must be a kebab-case slug of at most 64 characters (`SPEC_INVALID` otherwise, nothing written).
  2. Commit the work tree on the revival branch (Lazarus identity, `--no-verify`). The specs are added with `-f`, so a `.gitignore` cannot drop them. The commit may contain only `report.changed_files` plus the spec files; any other change is unstaged and the build fails with `UNEXPECTED_CHANGES`. A second build commits nothing and gives the same diff.
  3. `unified_diff_patch` = `git diff --no-renames <base_sha> <rebirth commit>`, capped at 2,000,000 characters at a file boundary (with a note). It applies with `git apply` on the base commit.
  4. Continuity Memory (`lazarus-continuity-v1`): `project`, `revival` (strategy, approval, run, branch, base and rebirth commits, spec path and files), `verification` (before/after totals, tests fixed, target outcomes, iterations used, Python, sandbox), `completed` plan items, `merged_branches`, `features_revived`, `roadmap` (the deferred features in order, `status: DEFERRED`), `open_markers`, `changed_files` (path, status, additions, deletions) and `uncommitted_files`. It is checked with `json.dumps(allow_nan=False)`.
  5. PR title `Lazarus <TYPE>: <strategy title>` (255 characters). The body has Summary, Verification (a before/after table and "Before: X of N tests passed, F failing. After: …"), Changes, Files changed, Kiro specs, Deferred and Preview sections. Repository text goes through `inline_text`; paths are code spans.
- **Persistence.** One transaction under `select_for_update` on the project re-checks `FOUNDRY_RUNNING`, writes the artifact (`update_or_create` on the project) and sets the project `REBORN`. Then `REBIRTH_COMPLETE` is emitted (`rebirth_artifact_id`, `foundry_run_id`, `pr_title`, `revival_branch`, `base_sha`, `rebirth_commit_sha`, `files_changed`, `additions`, `deletions`, `diff_truncated`, `exported_kiro_specs_path`, `spec_files`, `before`, `after`, `preview_url`).
- **Failure.** The project becomes `FAILED` with `Rebirth failed (<code>): <detail>`, and `ERROR` is emitted with `stage: "REBIRTH"`. The `FoundryRun` stays `COMPLETE`, because its verification passed. No server path is stored: `exported_kiro_specs_path` is repository-relative.
- **Decision.** Committing the verified tree on the revival branch (rather than diffing the work tree) makes the stored patch equal to what the branch contains, so it can be pushed or applied as is. The commit stays on the Lazarus-owned branch; the default branch is never changed.
- **Observed PayPulse result** (`tests/test_rebirth_paypulse.py`, subprocess mode, offline wheelhouse, deterministic engineering): before 16 tests, 15 passed, 1 failed; after 39 of 39 passed with 0 self-healing iterations. The diff has 8 files (+657 −14): `paypulse/currency.py`, `paypulse/retry.py`, `paypulse/webhooks.py`, `tests/test_currency.py`, `tests/test_retry.py` and the three files of `.kiro/specs/lazarus-restore-paypulse/`.

### 7. `apps.telemetry`
- Django Channels WebSocket consumer at `ws/projects/<uuid:project_id>/telemetry/` broadcasting structured JSON frames.

### 8. Lazarus Forensic MCP server (`backend/mcp_server/lazarus_mcp.py`)
- **SDK and transport.** The official `mcp` Python SDK v2 (`mcp>=2.3,<3`, `mcp.server.MCPServer`; v2 renamed v1's `FastMCP`), stdio only. A v2 server answers both the 2025-era `initialize` handshake (Kiro) and 2026-07-28 clients. `.kiro/settings/mcp.json` registers it as `lazarus-forensics`. The script sets up Django itself, so no `PYTHONPATH` is needed. Sync tools run on SDK worker threads, so the ORM can be used.
- **Sources.** The read tools take exactly one of `repo_path` (a local Git work tree; no database) or `project_id` (an ingested project; PostgreSQL). `export_kiro_revival_spec` always writes into `repo_path`. With `project_id` it exports the stored `RevivalStrategy.generated_kiro_specs`; otherwise it generates the bundle from the repository.
- **Reuse, no new forensics.**
  - `inspect_dead_branches`: `GitForensicService.analyze` → `branch_row`/`commit_log_row`, or the `BranchSnapshot` and `COMMIT_LOG` rows. Per-file patches come from `catalog.split_patches`, within a 200,000-character budget per call.
  - `query_project_genome`: `run_ingestion` → `catalog_from_rows` → `GenomeReconcilerAgent` → `inputs_from_features`, or `load_genome_inputs`. Both are serialized by `build_genome_graph` (the `GET …/genome` builder) as features with their `CitationOut` evidence. The full graph is included only on request (`include_graph`).
  - `detect_doc_code_conflicts`: the graph's `ConflictOut` records, with the claim citation's snippet and the reality-side citation locations.
  - `export_kiro_revival_spec`: `plan_strategies` → `score_plan` → `render_spec_bundle` → `validate_spec_bundle` → `as_storage`, then `rebirth.builder.spec_documents`/`export_specs`.
- **Read-only analysis in place.** A repository is read only through Git objects (the `GitForensicService` environment: `GIT_OPTIONAL_LOCKS=0`, system and user config off). It is not cloned: committed history is analyzed, and the work tree and index are never touched. Results are cached per (path, default branch, branch heads), for the last 4 repositories.
- **Deterministic by default (deviation).** For a repository the genome agent runs with an empty key unless `LAZARUS_MCP_USE_MODEL=1`. One model-refined synthesis of PayPulse took about 61 s, against 6 s deterministic, and MCP clients often time out sooner. The model path also sends repository text to `MODEL_BASE_URL`, which a tool call should not do implicitly. `synthesis.mode` in the result says which ran.
- **Path policy.** `repo_path` is resolved (`..`, symlinks and junctions) and must lie inside an allowed root: `LAZARUS_MCP_ALLOWED_ROOTS` (`os.pathsep`-separated), by default the repository root, `FIXTURE_ROOT` and `WORKSPACE_ROOT`. Relative paths are taken relative to the first root. Control characters are refused, and the path must be the top of a Git work tree. Export targets are fixed (`.kiro/specs/<kebab-case name>/<doc>`) and pass `safe_relative_path` (no symlinks on the way).
- **Writes.** Only the export writes, and never commits. Identical files are left alone (`unchanged`). A differing file is replaced only with `overwrite=true`; otherwise nothing is written. Tool annotations: the three read tools are `readOnlyHint`; the export is `readOnlyHint: false`, `destructiveHint: false`, `idempotentHint: true`.
- **stdout.** stdout carries only JSON-RPC. Logging goes to stderr, and while serving the SDK points file descriptor 1 at stderr, so stray output from libraries or Git children cannot corrupt the stream. A `ToolError` message reaches the caller; other exceptions become a generic tool error, with the traceback in the server log.
- **Observed PayPulse result** (`tests/test_mcp_server.py::test_stdio_end_to_end_on_paypulse`: stdio subprocess, legacy handshake, protocol `2025-11-25`). `feat/webhook-retry-engine` is unmerged and abandoned, ahead 4, behind 5, dormant 543 days. The genome is the deterministic table above. The three README conflicts are at `README.md:10`, `:11` and `:12`. The export writes `.kiro/specs/lazarus-restore-paypulse-dead-repo/` (three files), re-export reports `unchanged`, and `git status` shows only `?? .kiro/`.

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
- **`GET /api/projects/{project_id}/strategies`** (`apps/revival/api.py`, Task 5.3): `200 StrategiesOut` (the three strategies with scores, breakdowns, plans, spec bundles, approval state and `approval_token`, plus `recommended_strategy_id` and the latest `StrategySynthesis` summary), `404`, and `409 StrategiesNotReadyOut` (`STRATEGIES_PENDING`, `STRATEGIES_UNAVAILABLE`).
- **`POST /api/projects/{project_id}/strategies/{strategy_id}/approve`**: body `{"approval_token"}`. `200 ApprovalOut` (idempotent for the same token), `403 APPROVER_REQUIRED` (only with `REVIVAL_APPROVAL_REQUIRES_HEADER`), `404`, `409 ApprovalErrorOut`, `422` for a malformed body. Success sets the project to `STRATEGY_APPROVED`, creates a `FoundryRun` in `ENVIRONMENT` and emits `STRATEGY_APPROVED` after commit. Unauthenticated in local development.
- **`GET /api/projects/{project_id}/strategies/{strategy_id}/specs/{doc}`**: `doc` ∈ `requirements.md`, `design.md`, `tasks.md`; `text/markdown` with a `Content-Disposition` file name; 404 otherwise. The section "Revival: read and approval API" gives the details.
- **`GET /api/projects/{project_id}/rebirth`** (`apps/rebirth/api.py`, Task 6.3): `200 RebirthOut` (`id`, `project`, `foundry_run_id`, `strategy_id`, `strategy_type`, `revival_branch`, `base_sha`, `rebirth_commit_sha`, `pr_title`, `pr_body_markdown`, `unified_diff_patch`, `files[] {path, status, additions, deletions}`, `exported_kiro_specs_path`, `spec_files`, `continuity_memory`, `preview_url`, `before_test_output`, `after_test_output`, `tests_passed`, `iterations_used`, `created_at`, `telemetry_ws_path`), `404` for an unknown project, and `409 RebirthNotReadyOut {detail, code, project_id, status, status_detail, telemetry_ws_path}` with `REBIRTH_PENDING` before the project is `REBORN` or `FAILED`, else `REBIRTH_UNAVAILABLE`. Two queries.
- Telemetry WebSocket: `ws/projects/<uuid:project_id>/telemetry/` (see `apps.telemetry`).

## 3. Frontend (Next.js 15 App Router, `frontend/`)

### Routes, data and telemetry (Task 8.1)
- **Routes.** `/` is the landing page with the 1-click trigger. `/foundry/[projectId]` redirects to `/foundry/[projectId]/autopsy`; `autopsy`, `defibrillator` and `heartbeat` are sub-routes of one layout (deviation from structure.md's single tabbed `page.tsx`: each view is linkable and code-split, and the layout keeps one session across them). A non-UUID `projectId` is a 404.
- **Trigger.** "Resurrect Patient Zero (PayPulse)" calls the existing `POST /api/projects/ingest` with `{"demo": "paypulse"}` and navigates to the Autopsy. A 503 (Redis down) shows the server's `detail`; an unreachable backend offers the offline demo.
- **Session (`components/telemetry/ProjectSession.tsx`).** The project layout owns one `ProjectSessionProvider`: `GET /api/projects/{id}` polled every 3 s while the status is in flight (`DORMANT`, `INGESTING`, `SYNTHESIZING_GENOME`, `STRATEGY_APPROVED`, `FOUNDRY_RUNNING`) and refetched on stage-ending events, one telemetry socket, and the derived five-step progress. Views read it with `useProjectSession()` and may `subscribe()` to frames (8.4's terminal).
- **Progress (`lib/progress.ts`, pure).** The furthest point shown by the polled status or the frames wins, because frames sent before the socket opened are missed. `AUTOPSY_COMPLETE` finishes Genome and leaves Strategy active until a human approves. An `ERROR` marks the step its `stage` names; only `FAILED` stops the run (a `REVIVAL` error leaves the project `AUTOPSY_COMPLETE`).
- **Client (`lib/api.ts`).** Typed methods for every route in section 2. Errors are `ApiError` with `kind` `offline` (no response; a CORS refusal looks the same), `timeout`, `http` (status and parsed body, `code` for 409/403 bodies) or `invalid_response`. The 409 "not ready" reads resolve as `{ready: false, notReady}`. Ids are checked as UUIDs before they reach a URL. `lib/types.ts` mirrors the Ninja schemas.
- **Telemetry (`lib/ws.ts`).** `useTelemetry` de-duplicates by `event_id`, orders by `timestamp`, keeps the newest 2,000 frames, pings every 25 s and reconnects after 1 s doubling to 15 s (+30 % jitter). Close code 4404 and a normal close are final; a page that becomes visible reconnects at once.
- **Configuration.** `NEXT_PUBLIC_API_BASE_URL` (default `http://localhost:8000/api`), `NEXT_PUBLIC_WS_BASE_URL` (default `ws://localhost:8000/ws`; the backend's `telemetry_ws_path` already starts with `/ws`, and the overlap is removed), `NEXT_PUBLIC_DEMO_MOCK`.
- **CORS gap.** The backend has no CORS middleware, so a browser on `localhost:3000` cannot read direct responses from `localhost:8000`, and the JSON `POST` fails its preflight. Until the backend allows the dev origin, set `NEXT_PUBLIC_API_BASE_URL=/api`: `next.config.ts` proxies `/api/*` to `LAZARUS_BACKEND_ORIGIN` (default `http://localhost:8000`). WebSockets connect directly; Channels' `AllowedHostsOriginValidator` accepts the `localhost` origin because `ALLOWED_HOSTS` contains `localhost`.
- **Offline demo (`lib/mock/`).** The project id `0b5e7d1a-6c2f-4e8a-9f3d-2a1c0de0a001`, or `NEXT_PUBLIC_DEMO_MOCK=1` for every project, serves the recorded PayPulse run: the telemetry is replayed on a clock started by the trigger (kept in `sessionStorage`), the project status follows it, and `getGenome` returns the deterministic genome (six features with their exact C(f), the three README conflicts at `README.md:10`, `:11` and `:12`, the abandoned branch). Citation lists are an abridged sample with real files, lines and snippets; `citation_count` keeps the real totals. Strategies and rebirth answer their 409 bodies until 8.3 and 8.4 record them.

### Frontend design system (follow it in 8.2–8.4)
A dark "resurrection lab": a reanimation lab crossed with an ICU monitor. Confident and a little eerie, never cartoonish.
- **Tokens (`src/app/globals.css`, Tailwind v4 `@theme`).** Palette: `obsidian` #07090E (page), `slate-core` #0F141C (surfaces), `slate-raised` #151C27, `line` #222C3A (hairlines), `emerald-bio` #10B981, `amber-pulse` #F59E0B, `crimson` #EF4444, `violet-electric` #8B5CF6. Backend token aliases: `alive`, `dormant`, `ghost`, `conflict`, `neutral-token` (#64748B). Text: `ink` #E6EDF3, `ink-muted` #94A3B8, and the AA text tints `alive-text` #34D399, `dormant-text` #FBBF24, `ghost-text` #F87171, `conflict-text` #A78BFA. Glows: `shadow-glow-{alive,dormant,ghost,conflict}`. Motion: `animate-ecg-trace`, `vital-pulse`, `flatline-blink`, `charge`, `scan`, `sheen`, `feed-in`.
- **Color tokens (`src/lib/palette.ts`).** `COLOR_TOKEN_HEX` (SVG and React Flow strokes) and `COLOR_TOKEN_CLASSES` (`text`, `bg`, `border`, `fill`, `glow`, written as literal Tailwind classes) follow the "Color tokens" table: `conflict` is Electric Violet, as design.md specifies (the brief also listed conflicts under Crimson; Crimson is `ghost`). Unknown tokens fall back to `neutral`. Never build a class name from a token string.
- **Accessibility.** The brand hues stay the fills, lines and glows. Small text uses the `*-text` tints, because Emerald and Crimson drop below 4.5:1 on a 10 % tinted chip and Violet is 4.4:1 on Slate; `palette.test.ts` checks every text color. Status is never color alone: vital badges carry ● ◐ ○, epistemic badges ✓ ≈ ⚠ ? and outlined or dashed borders. Every control is a real `button` or link with a visible emerald focus ring; feeds are `role="log"`; `prefers-reduced-motion` stops every animation, and the dim ECG waveform still shows the state.
- **Type (`next/font`).** Display: Chakra Petch (`font-display`, uppercase, wide tracking) for titles and buttons. Body: Inter (`font-sans`). Forensic readouts, ids, scores, lines and logs: JetBrains Mono (`font-mono`, `tabular-nums`).
- **Components (`src/components/ui/`).** `Panel` (Slate surface, hairline border, corner brackets tinted by `tone`, optional `glow`, eyebrow and titled region). `Button`/`ButtonLink` with `primary`, `secondary`, `ghost`, `danger` and `charge` (the one big defibrillator call to action per screen: emerald gradient, glow, sheen, `busy` pulses the charge). `TokenBadge`, `VitalBadge`, `EpistemicBadge`, `ProjectStatusBadge`. `HeartbeatLine` (`flat` or `pulse` ECG SVG, stretches to its container). `Readout`/`ReadoutGrid` (mono label and value).
- **Shell (`src/components/shell/`).** `AppShell` (sticky brand bar, optional `nav` and `status`, footer), `ProjectNav` (01 Autopsy, 02 Defibrillator, 03 Heartbeat with `aria-current`), `ViewHeader`. Telemetry pieces in `src/components/telemetry/`: `PipelineTrack`, `TelemetryFeed`, `ConnectionIndicator`, `ResurrectionMonitor` (`compact` for a status strip), `SessionErrorBanner`.
- **Motifs.** The ECG is flat until something is alive and pulses once telemetry flows; the background is a faint 48 px lab grid with scanlines; emerald glow marks life, violet marks AI work and conflicts, amber marks dormancy, crimson marks ghosts and failure.

### Autopsy (Task 8.2)
- **Files.** `components/autopsy/` (`AutopsyView`, `GenomeGraph`, `GenomeNodes`, `GenomeEdges`, `GraphControls`, `EvidenceDrawer`, `Citation`, `ScoreBreakdown`, `ConflictInspector`, `FeatureLedger`, `PendingGenome`, `useGenome`). The logic is pure and tested in `lib/autopsy.ts`: contract → React Flow mapping, filters, selection and conflict focus, claim-citation lookup, snippet line numbering, the telemetry "materializing" list and the Defibrillator call to action.
- **Graph.** Nodes sit at the backend `position`s and are not draggable. Feature nodes show the vital badge and glyph, a C(f) ring with the exact value, the epistemic badge and the citation and conflict counts. Branch nodes show abandoned or merged state, dormancy, ahead/behind and the head sha. Document nodes show the conflict count. Conflict edges leave a feature's left handle toward its document: violet, dashed, with dashes flowing from the claim (`motion-safe:animate-dash-flow`, so they are static under reduced motion). Evidence edges leave the right handle toward branches, in the branch's token color. The graph has a lab-grid `Background`, `MiniMap`, `Controls`, a legend, and vital and epistemic filters. A branch or document node stays visible while any of its features is visible, and an edge is dropped when either end is hidden. A selection dims unrelated elements.
- **Evidence drawer.** A non-modal `role="dialog"`. Focus moves to its heading when it opens or its subject changes, Esc closes it, and focus then returns to the opener. For a feature it shows the reconstructed intent, the actual implementation state, the C(f) terms with `status_reason` and `vital_status_reason` explained, the conflicts, and every citation. Each citation shows its source type, `file:line_start–line_end`, commit sha, branch and branch-only flag, weight and the verbatim snippet, with a gutter numbered from `line_start` (left blank when there is no line range). Branch and document nodes open their own forensics. If a response carries no citations for a feature, the drawer says its claims cannot be checked instead of showing them unsupported.
- **Conflict Inspector.** Each ConflictRecord is shown as claim vs reality: the claim citation's snippet and lines (resolved by `claim_citation_id`, then by file and line; otherwise `claim_source`, labelled as such), the reality source, the description and the recommendation. Inspect selects the feature, opens the drawer and highlights the edge, widening the filters if the feature was hidden.
- **States.** Loading. Pending (409 `GENOME_PENDING`, which the backend returns for every in-flight status) shows the monitor and the features announced by `GENOME_NODE_CREATED` frames. Unavailable shows `GENOME_UNAVAILABLE` with its `status_detail`. Error offers retry, plus the offline demo when the backend is unreachable. Ready refetches on a status change or a `GENOME_COMPLETE` frame. A banner marks the offline demo. A `FAILED` project with a stored genome is labelled. `AUTOPSY_COMPLETE` shows the charge call to action to the Defibrillator, and later statuses show a secondary link.

### Defibrillator (Task 8.3)
- **Files.** `components/defibrillator/` (`DefibrillatorView`, `useStrategies`, `StrategyCard`, `ScoreGauge`, `ScoreFormula`, `ComparisonMatrix`, `WorkOrder`, `SpecPreview`, `Markdown`, `ApprovalGate`). The logic is pure and tested in `lib/defibrillator.ts` (breakdown, plan and deferral readers, comparison rows, recommendation mirror, gate availability, the gate reducer, approval error mapping, loading-state classification) and `lib/markdown.ts`. `ApiError` moved to `lib/apiError.ts` (re-exported by `lib/api.ts`) so the mock client can throw it without an import cycle.
- **Deck.** One card per strategy in `RESTORE`, `MODERNIZE`, `EXTRACT` order: title, summary, a 0–100 Score(S_k) dial (`role="meter"`, the value as the backend rounded it), V/D/R and a stacked bar of the three terms in points. Badges: ★ Recommended (the backend's `is_recommended`), ▲ Highest score (computed), ✕ Not actionable, 🔒 Approved or Locked. A comparison table puts every term, the item, file and deferral counts side by side and marks the best value of each row. Selecting a card (or a column) shows its full breakdown, work order and spec. The approved strategy is selected first, then the recommended one.
- **Breakdown.** `PlanScore.as_dict()` as plain math markup (`<var>`, `<sub>`): the formula, the substituted formula in the spec bundle's own notation (a test checks that each `design.md` contains it), each term's weight, input and points with their sum, and the V (verified features by title, targets × feasibility), D (file list against the budget) and R (base + each factor's count × rate, cap and adjustment) derivations. Missing parts fall back to the score columns.
- **Spec preview.** WAI-ARIA tabs for `requirements.md`, `design.md` and `tasks.md` with the spec name, `.kiro/specs/<spec_name>/`, the generation mode and `generated_at`. `lib/markdown.ts` parses the subset `specs.py` writes (headings, paragraphs, nested `-`/`1.` lists, checkboxes, `**strong**`, code spans, backslash escapes, footnote references and definitions) into a tree that React renders as text, so no raw HTML is possible and no Markdown dependency is added. Footnote references link to the document's evidence list and carry the `evidence_index` location, source type and snippet as a tooltip. With the HTTP client the three documents can be downloaded from `document_urls`.
- **Human Approval Gate.** Two steps: "Charge the paddles" arms the gate, then "Shock: approve <TYPE>" sends `POST .../approve` with body `{"approval_token"}` (exactly the token GET returned; the schema forbids extra fields) and `X-Lazarus-Approver` only when a name is entered. A stray confirm cannot fire from the idle state (`gateReducer`, property-tested), Esc stands down, and focus moves to the "Clear!" heading, back to the charge button, or to the error. The gate lists what approval locks in (plan, scores, spec bundle, the token prefix), that it starts the Foundry (`STRATEGY_APPROVED`, a `FoundryRun` at `ENVIRONMENT`) and that it cannot be undone. Answers: 200 (also `already_approved`) shows the locked state (`approved_by`, `approved_at`, `approval_intact`, `foundry_run_id`) and the charge call to action to the Heartbeat; 403 asks for the approver name; 404 and `STRATEGY_STALE`, `PROJECT_NOT_READY` and 422 offer a reload; `ANOTHER_STRATEGY_APPROVED` locks; `STRATEGY_NOT_ACTIONABLE` and `APPROVED_CONTENT_CHANGED` explain; an unreachable backend asks for a reload before retrying, because the request may have landed. The gate is closed in the backend's order: approved, another approved (locked), status not `AUTOPSY_COMPLETE`, not actionable, malformed token.
- **States.** Loading. Pending: `STRATEGIES_PENDING`, and also `STRATEGIES_UNAVAILABLE` while the project is `AUTOPSY_COMPLETE` with no `status_detail` and no `REVIVAL` error (the moment before the rows are written); it polls every 2.5 s and shows the `STRATEGY_GENERATED` frames as they arrive. Unavailable shows the detail and `status_detail`. Error offers retry, plus the offline demo when the backend is unreachable. Ready refetches on a status change and on every `STRATEGY_GENERATED` and `STRATEGY_APPROVED` frame, and keeps the deck on a failed background refetch.
- **Offline demo.** `lib/mock/paypulse-strategies.json` is real backend output from the no-database chain of `test_rebirth_paypulse.py` (plans, `PlanScore.as_dict()`, validated spec bundles), with feature ids mapped to the mock genome's and each token computed by `approval.approval_token` over that content; scores are 68.1, 58.1 and 57.3. It is loaded on demand. `mockApprove` applies the backend's checks in order. An approval is kept in `sessionStorage` for the current run (a new run forgets it), moves the mock project to `STRATEGY_APPROVED` and emits the `STRATEGY_APPROVED` frame through the replay (`subscribeMockFrames`); `mockApproval().approvedAt` is the clock the Heartbeat's replay can start from.

### Heartbeat (Task 8.4), data layer
- **Files.** `lib/heartbeat.ts` (pure, no React or I/O, tested in `lib/heartbeat.test.ts`); `lib/mock/paypulse-foundry.json` (the 18 telemetry frames the backend emitted for the RESTORE run, replayed from the approval in about 30 s by `mock/telemetry.ts`) and `lib/mock/paypulse-rebirth.json` (the recorded `RebirthOut`, served by `mock/rebirth.ts` `buildMockRebirth(approval)` once the replay reaches `REBIRTH_COMPLETE`). `lib/markdown.ts` also reads GFM tables and links for the PR body; only absolute http(s) URLs become links.
- **Pipeline.** `pipelineState({frames, status, statusDetail?, foundryRunId?, fallbackMaxIterations?}): PipelineState` folds `AGENT_STAGE_CHANGED`, `TEST_*`, `ERROR` and `REBIRTH_COMPLETE` frames of one run (default `latestFoundryRunId(frames)`) into four `StageState`s (`pending`, `running`, `healing`, `done`, `failed`), an `outcome` (`idle`, `queued`, `running`, `verified`, `reborn`, `failed`), `progress` 0..1, `healing` (iteration, max, files), `failure` (`FOUNDRY`, `REBIRTH` or `STATUS` scope with stage, code, detail), the baseline and last verification `TestRunEvent`s and the `REBIRTH_COMPLETE` summary. A started stage marks earlier ones done; the project status fills in missed frames (`fromStatusFallback`). A `VERIFICATION` test frame carries its suite totals under `full`. `healingLabel(state)` gives "Self-healing iteration n of max". Also exported: `PIPELINE_STAGES`, `STAGE_AGENTS`, `isPipelineStage`, `readTotals`, `readRebirthComplete`.
- **Terminal.** `terminalLines(frames, limit = MAX_TERMINAL_LINES /* 5000 */): {lines, dropped}` turns `SANDBOX_LOG` lines (stdout/stderr, a `$ command` line per new command) and stage, test, error and rebirth events into `TerminalLine`s with stable ids, keeping the newest `limit`. `filterTerminal(lines, {stage?, stdout?, stderr?})` filters by stage (lines without a stage stay) and stream.
- **Before/after pytest.** `pytestComparison(rebirth, pipeline?)` returns before (default branch) and after (revival branch) `PytestSummary`s with totals, failing ids, raw output and `allPassing`, plus `fixed` and `targetOutcomes`: from the Continuity Memory, else the test frames, else the output. `parsePytestTotals` (bordered or bare `-q` summary line), `parsePytestFailures` and `classifyPytestLine` read raw output.
- **Diff.** `parseUnifiedDiff(patch): ParsedDiff` (never throws) gives files (path, old/new path, `A`/`M`/`D`/`T`, binary, `isSpec` under `.kiro/specs/`, meta lines, hunks, counts) → hunks (header, ranges, section) → lines (`add`, `del`, `context`, `hunk`, `meta`) with old and new line numbers, and the `[lazarus] diff truncated` note. Git's quoted paths are decoded by `unquoteGitPath`. `withFileStats(parsed, files)` applies the backend's per-file status; `rebirthFileTotals(files)` sums them. On the recorded patch it agrees with `files` (8 files, +657 −14).
- **Continuity Memory.** `readContinuityMemory(memory): ContinuityMemory` and the section readers `readMemoryProject`, `readRevival`, `readVerification`, `readCompleted`, `readMergedBranches`, `readFeaturesRevived`, `readRoadmap`, `readOpenMarkers`, `readChangedFiles` accept a missing or mistyped memory and return nulls and empty lists; `continuityMemoryJson` pretty-prints it.

### Heartbeat (Task 8.4), UI
- **Files.** `components/heartbeat/` (`HeartbeatView`, `useRebirth`, `PipelineProgress`, `LiveTerminal`, `VitalSplit`, `DiffViewer`, `ContinuityMemoryPanel`, `PrSummary`, `useThrottledValue`). Pure helpers in `lib/heartbeatView.ts` (tested in `heartbeatView.test.ts`): `heartbeatPhase`, `shouldPollRebirth`, `tailSlice`/`headSlice`, `isNearBottom`, `vitalTrace`, `totalsSentence`, `memoryFileName`, `diffStatusLabel`, `gutterDigits`. The Markdown renderer moved to `components/ui/Markdown.tsx` and renders both the spec documents and the PR body. `HeartbeatLine` gained an `erratic` state (an irregular rhythm for a failing suite).
- **Data.** `useRebirth` calls `getRebirth`. It refetches when the project status changes and on every `REBIRTH_COMPLETE` frame, and polls every 2.5 s while `REBIRTH_PENDING` and the project is `STRATEGY_APPROVED`, `FOUNDRY_RUNNING` or (briefly) `REBORN`. An artifact on screen is never replaced by a later 409 or a failed refetch. The pipeline is `pipelineState` over the session frames, scoped to the artifact's `foundry_run_id` once it is known.
- **States (`heartbeatPhase`).** A stored artifact shows REBORN. Otherwise: loading; a Foundry failure (frames or `FAILED` after the run started) shows the failed stages, code, detail and `status_detail`; a read error offers retry and the offline demo; `REBIRTH_UNAVAILABLE` without a Foundry failure shows its detail; `REBIRTH_PENDING` before any approval links back to the Defibrillator; otherwise the run is live.
- **Live.** `PipelineProgress` shows the four agents plus Complete, with a `role="progressbar"`, `aria-current="step"`, a glyph and a word for every status (so color is never the only signal), the amber `healingLabel` marker and the failure scope, code and detail. `LiveTerminal` is a focusable `role="log"`, `aria-live="polite"` while live. Frames are committed at most every 750 ms (`useThrottledValue`), so the DOM and the screen reader get batches. stdout is white and stderr amber, with an `out`/`err` tag on each row. Stage and stream filter chips use `aria-pressed`. The terminal follows the tail until the user scrolls up, then a "Jump to live · n new" button appears. At most 2,000 rows are rendered (`tailSlice`), and the buffer cap (5,000) and the hidden rows are counted on screen.
- **REBORN.** A hero with the word REBORN, a steady emerald ECG, before → after totals, diff totals, self-healing iterations and tests fixed. The reveal and one emerald shock flash play, and focus moves to the heading, only if this page saw the run live. `VitalSplit` compares the baseline (erratic crimson trace) with the verified run (steady emerald pulse): totals, failing ids, fixed tests, target outcomes and the raw output colored by `classifyPytestLine` in a lazily rendered `<details>`. `DiffViewer` has a file table (status letter plus word, ◆ exported spec badge, +/−, files missing from a truncated patch) and per-file collapsible text-only diffs with old/new gutters, hunk headers and a cap of 1,500 rows per file with "show all". Code files start open and spec files collapsed. `ContinuityMemoryPanel` shows every memory section, a raw JSON toggle and a Download JSON button (a Blob URL revoked after the click; the file name is `[a-z0-9-]` only). `PrSummary` renders `pr_title` and `pr_body_markdown` with the safe renderer.
- **Motion.** New keyframes `reborn-in` and `shock-flash` are used only behind `motion-safe:`. Under `prefers-reduced-motion` the global rule stops every animation, and the static traces still show the state.
- **Offline demo (deviation from Part 1).** `lib/ws.ts` `replayMock` now replays `mockFoundryTimeline(mockApproval())` from the approval time, and schedules it again when the `STRATEGY_APPROVED` frame is emitted. Before this change the recorded Foundry frames never reached the session, so the mock terminal and stages stayed empty.
