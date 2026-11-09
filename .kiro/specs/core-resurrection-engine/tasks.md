# Implementation Plan — Lazarus Foundry

- [x] 1. Scaffold Monorepo, Local Windows Infrastructure & 7-App Django Core
  - [x] 1.1 Configure `docker-compose.yml` to run ONLY `db` (`pgvector/pgvector:pg16` on `localhost:5432`) and `redis` (`redis:7-alpine` on `localhost:6379`) for local Windows development. Create `.env.example`, update `.gitignore` (tracking `.kiro/` and ignoring `.lazarus_data/`), and document the Windows startup commands (`celery -A config worker -l info --pool=solo`) in `README.md`.
  - [x] 1.2 Initialize Django 5.1 project in `backend/` with `django-ninja`, `channels`, `channels-redis`, `celery`, `pgvector`, `GitPython`, `tree-sitter>=0.23.0`, `tree-sitter-language-pack>=1.8.0`, `tree-sitter-python>=0.23.0`, `openai`, and `docker`.
  - [x] 1.3 Create all 7 Django apps (`projects`, `ingestion`, `genome`, `revival`, `foundry`, `rebirth`, `telemetry`). Add `0001_enable_pgvector.py` with `VectorExtension()` in `apps.ingestion`, then implement all ORM models and run `makemigrations` + `migrate`.
  - [x] 1.4 Configure Django Channels WebSocket routing at `ws/projects/<uuid:project_id>/telemetry/` and `broadcast_telemetry(project_id, event_type, payload)`.

- [x] 2. Build the "Patient Zero" (`paypulse_dead_repo`) Git Fixture Generator
  - [x] 2.1 Write `backend/tests/fixtures/build_paypulse_fixture.py` that programmatically initializes a real Git repository at `/tmp/lazarus_fixtures/paypulse_dead_repo` (avoiding nested `.git` folders in our parent repo).
  - [x] 2.2 Have the script create realistic commits and forensic contradictions:
    - `main` branch: A 2021 Python micro-ledger with working core ledger math, a broken test in `test_webhooks.py`, and a `README.md` falsely claiming completed "Automated Exponential Backoff Webhook Retries" and "PDF Invoice Export".
    - `feat/webhook-retry-engine` (unmerged branch): Contains 80% of the retry engine implementation with a `# TODO: complete backoff jitter and wire into dispatcher` marker.
    - Add a `pytest` fixture and management command `python manage.py seed_paypulse_fixture` that builds this repo on demand.

- [x] 3. Implement System 1: Artifact Ingestion Engine (`apps.ingestion`)
  - [x] 3.1 Build `GitForensicService` (`GitPython`) to inspect branches, detect unmerged branches, compute ahead/behind counts, and extract diffs against `main`.
  - [x] 3.2 Build `ASTParserService` (`tree-sitter` + `tree-sitter-language-pack`) to extract classes, functions, imports, and `TODO`/`FIXME` comments with exact `line_start`, `line_end`, and `snippet`.
  - [x] 3.3 Wire up `ingest_repository_task(project_id)` to populate `BranchSnapshot` and `ArtifactRecord` and emit WebSocket telemetry.

- [x] 4. Implement System 2: Project Genome & Contradiction Reconciler (`apps.genome`)
  - [x] 4.1 Implement the parameterized sigmoid confidence formula $C(f)$ and epistemic status classifier in `apps/genome/scoring.py`.
  - [x] 4.2 Build `GenomeReconcilerAgent` in `apps/genome/agents.py` using the `openai` SDK with `settings.MODEL_BASE_URL`, `settings.MODEL_API_KEY`, and `settings.MODEL_ID` (with deterministic fallback synthesis if API key is absent during offline unit tests) to output `IntentNode`, `EvidenceCitation` (`line_start`, `line_end`, `snippet`), and `ConflictRecord` objects.
  - [x] 4.3 Expose `GET /api/projects/{project_id}/genome`.

- [x] 5. Implement System 3: Revival Intelligence & Spec Exporter (`apps.revival`)
  - [x] 5.1 Implement `score_revival_strategy()` in `apps/revival/scoring.py` using the parameterized $\text{Score}(S_k)$ formula for `RESTORE`, `MODERNIZE`, and `EXTRACT`.
  - [x] 5.2 Generate the `.kiro/specs/` recovery bundle (`requirements.md`, `design.md`, `tasks.md`) stored on `RevivalStrategy.generated_kiro_specs`.
  - [x] 5.3 Expose `GET /api/projects/{project_id}/strategies` and `POST /api/projects/{project_id}/strategies/{strategy_id}/approve`.

- [x] 6. Implement Systems 4 & 5: 4-Agent Foundry & Product Rebirth (`apps.foundry` & `apps.rebirth`)
  - [x] 6.1 Build `SandboxRunner` in `apps/foundry/sandbox.py` supporting `SANDBOX_MODE="subprocess"` (isolated venv + `pytest` runner) and `SANDBOX_MODE="docker"` (`python:3.11-slim`, port `8010`).
  - [x] 6.2 Implement all 4 sequential agents in `apps/foundry/agents.py`: `EnvironmentAgent` -> `ArchitectureAgent` -> `EngineeringAgent` -> `VerificationAgent` (with up to 3 self-healing retry loops).
  - [x] 6.3 Populate `RebirthArtifact` in `apps.rebirth` (unified Git diff, exported `.kiro/specs/` files written into the target repo, Continuity Memory JSON, and PR summary) and expose `GET /api/projects/{project_id}/rebirth`.

- [x] 7. Build the Lazarus Forensic MCP Server (`backend/mcp_server/`)
  - [x] 7.1 Build `backend/mcp_server/lazarus_mcp.py` exposing `inspect_dead_branches`, `query_project_genome`, `detect_doc_code_conflicts`, and `export_kiro_revival_spec`.

- [ ] 8. Build the Next.js 15 "Necromancer Telemetry" Frontend (`frontend/`)
  - [-] 8.1 Build the Landing Page + 1-Click "Resurrect Patient Zero (PayPulse)" trigger.
  - [~] 8.2 Build **The Autopsy** (React Flow Genome Graph + Evidence Citations drawer showing `line_start`–`line_end` snippets + Conflict Inspector).
  - [~] 8.3 Build **The Defibrillator** (3 Strategy cards with $\text{Score}(S_k)$ breakdown + `.kiro` spec preview + Human Approval Gate button).
  - [~] 8.4 Build **The Heartbeat** (4-Agent pipeline progress bar + live WebSocket terminal + Before/After `pytest` output + Unified Diff + Continuity Memory).
