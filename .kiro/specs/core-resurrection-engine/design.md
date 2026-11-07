# Technical Design — Lazarus Foundry

## 1. Seven Django Apps & Core ORM Models (`backend/apps/`)

### 1. `apps.projects`
- `Project`: `id (UUID)`, `name`, `repo_url`, `is_golden_demo (bool)`, `workspace_path`, `status` (`DORMANT`, `INGESTING`, `AUTOPSY_COMPLETE`, `STRATEGY_APPROVED`, `FOUNDRY_RUNNING`, `REBORN`, `FAILED`), `default_branch`, `created_at`, `updated_at`.

### 2. `apps.ingestion`
- Migration `0001_enable_pgvector.py`: Runs `VectorExtension()`.
- `BranchSnapshot`: `project (FK)`, `name`, `head_sha`, `last_commit_date`, `is_abandoned (bool)`, `commits_ahead (int)`, `commits_behind (int)`, `diff_summary (JSONField)`.
- `ArtifactRecord`: `project (FK)`, `artifact_type` (`AST_MODULE`, `README_DOC`, `DEPENDENCY_MANIFEST`, `TODO_MARKER`, `COMMIT_LOG`), `file_path`, `branch_name`, `raw_content`, `structured_metadata (JSONField)`, `embedding (VectorField(dimensions=1536, null=True))`.

### 3. `apps.genome`
- `IntentNode`: `id (UUID)`, `project (FK)`, `title`, `category` (`CORE_FEATURE`, `ARCHITECTURE`, `INTEGRATION`, `DATA_MODEL`), `vital_status` (`VERIFIED_WORKING`, `ABANDONED_IN_PROGRESS`, `GHOST_FEATURE`), `epistemic_status` (`KNOWN`, `INFERRED`, `CONFLICTED`, `MISSING_CONTEXT`), `confidence_score (float)`, `reconstructed_intent`, `actual_implementation_state`, `graph_position (JSONField)`.
- `EvidenceCitation`: `intent_node (FK, related_name="citations")`, `artifact (FK, null=True)`, `source_type`, `file_path`, `commit_sha (blank=True)`, `line_start (int, null=True)`, `line_end (int, null=True)`, `snippet (TextField)`, `weight (float)`.
- `ConflictRecord`: `project (FK)`, `intent_node (FK, related_name="conflicts")`, `claim_source`, `reality_source`, `conflict_description`, `resolution_recommendation`.

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
