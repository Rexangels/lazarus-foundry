# Requirements — Lazarus Foundry Core Resurrection Engine

## Introduction
Lazarus Foundry is an end-to-end AI Software Revival Foundry built around Five Core Intelligence Systems (`apps.ingestion`, `apps.genome`, `apps.revival`, `apps.foundry`, `apps.rebirth`) and Two Shared Infrastructure Apps (`apps.projects`, `apps.telemetry`).

## Requirements

### Requirement 1: Project Workspace & Multi-Artifact Ingestion (`apps.projects` & `apps.ingestion`)
**User Story:** As a developer, I want to submit a dormant Git repository URL or trigger the built-in golden dead repository (`paypulse_dead_repo`) so that the system extracts all code, branch histories, and documentation artifacts locally.

#### Acceptance Criteria
1. WHEN a user submits a Git URL or selects the demo fixture, THE `projects` and `ingestion` services SHALL initialize the workspace under `settings.WORKSPACE_ROOT` and record all local and remote branches.
2. WHEN analyzing Git history, THE `ingestion` service SHALL identify the default branch, all unmerged/abandoned branches, commit timestamps, and branch divergence points.
3. WHEN parsing source files, THE `ingestion` service SHALL use Tree-sitter (`tree-sitter-language-pack`) to extract an AST inventory of modules, classes, functions, dependencies, and inline `TODO`/`FIXME` comments with `line_start`, `line_end`, and `snippet`.
4. WHILE ingestion is running, THE `telemetry` service SHALL broadcast real-time progress events over WebSockets.

### Requirement 2: Project Genome & Contradiction Reconciliation (`apps.genome`)
**User Story:** As a developer inspecting an abandoned project, I want to see a structured map of what the software was meant to become, what actually works, what was abandoned mid-flight, and where the README contradicts the code.

#### Acceptance Criteria
1. WHEN artifact ingestion completes, THE `genome` service SHALL invoke the configured reasoning model (`settings.MODEL_ID` via `settings.MODEL_BASE_URL`) to synthesize the Project Genome.
2. THE `genome` service SHALL classify every identified feature into `VERIFIED_WORKING`, `ABANDONED_IN_PROGRESS`, or `GHOST_FEATURE` and compute its confidence score $C(f) \in [0.0, 1.0]$ and epistemic status (`KNOWN`, `INFERRED`, `CONFLICTED`, `MISSING_CONTEXT`).
3. EVERY `IntentNode` SHALL persist at least one `EvidenceCitation` (`file_path`, `commit_sha`, `line_start`, `line_end`, `snippet`, `weight`).
4. WHEN documentation claims a feature exists but the AST lacks implementation, THE `genome` service SHALL create a `ConflictRecord`.

### Requirement 3: Revival Intelligence & Strategy Selection (`apps.revival`)
**User Story:** As a decision-maker, I want the system to evaluate technical debt and propose concrete revival strategies on a 0–100 scale so I can approve the scope of autonomous engineering work.

#### Acceptance Criteria
1. WHEN the Project Genome is finalized, THE `revival` service SHALL generate three `RevivalStrategy` options: `RESTORE`, `MODERNIZE`, and `EXTRACT`.
2. THE `revival` service SHALL calculate a bounded feasibility score $\text{Score}(S_k) \in [0.0, 100.0]$ using the weighted convex formula in `forensic-invariants.md`.
3. WHEN a user approves a `RevivalStrategy`, THE `revival` service SHALL generate a `.kiro/specs/` recovery bundle (`requirements.md`, `design.md`, `tasks.md`) and unlock the Foundry.

### Requirement 4: 4-Agent Autonomous Foundry (`apps.foundry`)
**User Story:** As a developer, I want the Foundry agents to restore the environment, merge or implement the approved feature, and verify that tests pass in a local sandbox.

#### Acceptance Criteria
1. WHEN a `RevivalStrategy` is approved, THE `foundry` service SHALL run the 4 sequential agents: `EnvironmentAgent`, `ArchitectureAgent`, `EngineeringAgent`, and `VerificationAgent`.
2. THE `VerificationAgent` SHALL execute `pytest` in the workspace sandbox (`SANDBOX_MODE="subprocess"` on Windows local dev) and capture stdout/stderr and exit codes, iterating up to 3 self-healing cycles if tests fail.

### Requirement 5: Product Rebirth & Continuity Memory (`apps.rebirth`)
**User Story:** As a developer, I want a verified unified diff, exported `.kiro` specs inside my resurrected repo, and persistent continuity memory once tests pass.

#### Acceptance Criteria
1. WHEN verification exits with code `0`, THE `rebirth` service SHALL create a `RebirthArtifact` containing the unified Git diff patch, PR summary Markdown, exported `.kiro/specs/` files in the target repo, and the Continuity Memory JSON roadmap.
