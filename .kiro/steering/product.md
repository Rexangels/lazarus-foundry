---
inclusion: always
---
# Lazarus Foundry — Product Vision & Behavioral Standard

## 1. Core Identity
Lazarus Foundry is an **AI Software Revival Foundry** built for the Kiroween Resurrection Track. It ingests dormant, broken, or obsolete software repositories, reconstructs their lost product intent ("Project Genome"), proposes strategic recovery paths ("Revival Intelligence"), and executes verified code modernization inside isolated sandboxes ("Autonomous Engineering Foundry").

## 2. Five Intelligence Systems Mapped to Seven Django Apps
Our product architecture consists of **Five Intelligence Systems** supported by **Two Shared Infrastructure Apps** (7 Django apps total in `backend/apps/`):

### The Five Core Intelligence Systems:
1. **Artifact Ingestion (`apps.ingestion`):** Clones target repositories into isolated workspaces, extracts Git commit histories, identifies unmerged/abandoned branches, parses language-agnostic ASTs via Tree-sitter, and indexes documentation (`README`, issues, `TODO` comments).
2. **Project Genome (`apps.genome`):** Reconciles conflicting evidence across artifacts using the configured reasoning model (`settings.MODEL_ID`, currently `muse-spark-1.3` on Meta Model API for testing). Classifies every discovered feature into one of three states:
   - `VERIFIED_WORKING` (Green): Implemented in AST and verified.
   - `ABANDONED_IN_PROGRESS` (Amber): Partially built on dead branches or broken in `main`.
   - `GHOST_FEATURE` (Crimson): Described in README/designs/issues but missing from code.
3. **Revival Intelligence (`apps.revival`):** Computes bounded feasibility scores $\text{Score}(S_k) \in [0.0, 100.0]$ and presents actionable revival strategies (`RESTORE`, `MODERNIZE`, `EXTRACT`) alongside a generated `.kiro/specs/` recovery plan.
4. **Autonomous Engineering Foundry (`apps.foundry`):** Sequential 4-agent execution pipeline (`EnvironmentAgent` -> `ArchitectureAgent` -> `EngineeringAgent` -> `VerificationAgent`) with strict human-in-the-loop approval gates.
5. **Product Rebirth & Continuity (`apps.rebirth`):** Verifies automated test suites (`pytest`), captures before/after execution logs, produces a unified Pull Request diff bundle, exports `.kiro` specs into the resurrected repo, and persists Continuity Memory.

### The Two Shared Infrastructure Apps:
6. **Project Workspace Manager (`apps.projects`):** Owns the root `Project` lifecycle state (`DORMANT` -> `REBORN`) and local filesystem paths (`WORKSPACE_ROOT`, `FIXTURE_ROOT`).
7. **Real-Time Telemetry (`apps.telemetry`):** Streams live WebSocket events over Django Channels to the frontend.

## 3. The Three Visual Moments (UI/UX Standard)
- **View 1: The Autopsy (Genome Graph):** Interactive React Flow node graph + forensic evidence ledger showing knowns, inferences, conflicts, and confidence scores $C(f) \in [0, 1]$.
- **View 2: The Defibrillator (Strategy Matrix):** Interactive decision deck comparing `Restore`, `Modernize`, and `Extract` strategies with an approval gate that locks in the spec.
- **View 3: The Heartbeat (Live Foundry & Rebirth Split-Screen):** Real-time WebSocket telemetry showing the sandbox fixing dependencies, implementing the missing feature, running tests, and rendering the unified diff and continuity roadmap.
