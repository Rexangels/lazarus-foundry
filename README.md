# Lazarus Foundry

Lazarus Foundry is an AI software revival foundry built for the Kiroween Resurrection Track. It ingests a dormant Git repository, reconstructs what the project was meant to do (the "Project Genome"), proposes scored revival strategies (`RESTORE`, `MODERNIZE`, `EXTRACT`), and, after human approval, runs a four-agent pipeline that repairs the code and verifies it with `pytest` in an isolated sandbox. The backend is Django 5.1 (Django Ninja, Channels, Celery) and the frontend is Next.js 15.

## Prerequisites

- Python 3.11+
- Node.js 20+
- Docker Desktop (PostgreSQL and Redis, and the Foundry's Docker sandbox)
- Git

## Local setup (Windows hybrid)

Docker runs only the two infrastructure services defined in `docker-compose.yml`:

| Service | Image | Address |
|---------|-------|---------|
| `db` | `pgvector/pgvector:pg16` | `127.0.0.1:5432` |
| `redis` | `redis:7-alpine` | `127.0.0.1:6379` |

Both ports are bound to `127.0.0.1`, so they are not reachable from other machines on your network. Django (served by Daphne), the Celery worker, and the Next.js dev server all run natively on Windows.

### Configure environment variables

From the repository root:

```powershell
Copy-Item .env.example .env
```

Open `.env` and set `MODEL_API_KEY` to your Meta Model API key. The defaults for `MODEL_BASE_URL` (`https://api.meta.ai/v1`) and `MODEL_ID` (`muse-spark-1.3`) work as-is. `.env` is gitignored; never commit it.

Runtime data (cloned workspaces and generated fixtures) goes under `.lazarus_data/`, which is also gitignored.

## Running the stack

Open four PowerShell terminals at the repository root.

**Terminal 1: PostgreSQL and Redis**

```powershell
docker compose up -d
```

**Terminal 2: Django API and WebSockets (Daphne on port 8000)**

```powershell
cd backend; python -m venv .venv; .\.venv\Scripts\Activate.ps1; pip install -r requirements.txt; python manage.py migrate; daphne -b 127.0.0.1 -p 8000 config.asgi:application
```

After the first run, you only need `cd backend; .\.venv\Scripts\Activate.ps1; daphne -b 127.0.0.1 -p 8000 config.asgi:application`.

**Terminal 3: Celery worker**

```powershell
cd backend; .\.venv\Scripts\Activate.ps1; celery -A config worker -l info --pool=solo
```

`--pool=solo` is required on Windows. Celery's default `prefork` pool depends on `fork()`, which Windows does not provide, so without this flag tasks hang or fail silently.

**Terminal 4: Next.js frontend**

```powershell
cd frontend; npm install; npm run dev
```

The UI is served at `http://localhost:3000`. It reaches the API at `http://localhost:8000/api` and telemetry WebSockets at `ws://localhost:8000/ws`.

## Seeding the demo fixture

The "Patient Zero" demo repository (`paypulse_dead_repo`) is generated on demand rather than committed:

```powershell
cd backend; .\.venv\Scripts\Activate.ps1; python manage.py seed_paypulse_fixture
```

It builds `main` plus the unmerged `feat/webhook-retry-engine` branch at `FIXTURE_ROOT/paypulse_dead_repo` (default `.lazarus_data/fixtures/paypulse_dead_repo`). Pass `--force` to rebuild an existing fixture or `--destination <path>` to build elsewhere. The command does not need PostgreSQL.

## Running tests

```powershell
cd backend; .\.venv\Scripts\Activate.ps1; pip install -r requirements-dev.txt; python -m pytest
```

Tests that need the database run against the real PostgreSQL from `DATABASE_URL` (pytest-django creates a `test_lazarus` database on that server). If PostgreSQL is not running, those tests are skipped with the reason shown in the summary; all other tests still run.

Test markers: `slow` (builds Git repositories or runs real sandboxes; the save hook runs `-m "not slow"`), `network` (installs from PyPI; skipped when offline) and `docker` (needs the Docker daemon; skipped when it is not reachable). The offline sandbox test needs a modern pytest wheel in `.lazarus_data/wheelhouse`; it is skipped until you fill it once:

```powershell
cd backend; .\.venv\Scripts\python.exe -m pip download pytest --only-binary=:all: -d ..\.lazarus_data\wheelhouse
```

## Kiro MCP server (Lazarus Forensics)

`backend/mcp_server/lazarus_mcp.py` exposes `inspect_dead_branches`, `query_project_genome`, `detect_doc_code_conflicts` and `export_kiro_revival_spec` to Kiro over stdio (official `mcp` SDK, installed by `requirements.txt`). The tools take a local `repo_path` (no database needed) or the `project_id` of an ingested project. Only `export_kiro_revival_spec` writes, and only `.kiro/specs/<spec_name>/` inside `repo_path`.

It is registered in `.kiro/settings/mcp.json` as `lazarus-forensics`. The command must be the backend's virtualenv Python, because a system Python lacks the dependencies:

```json
{
  "mcpServers": {
    "lazarus-forensics": {
      "command": "backend/.venv/Scripts/python.exe",
      "args": ["backend/mcp_server/lazarus_mcp.py"],
      "env": { "LAZARUS_MCP_ALLOWED_ROOTS": "C:/path/to/lazarus-foundry;C:/path/to/dead-repos" }
    }
  }
}
```

Use absolute paths if Kiro does not start the server from the workspace root. `repo_path` must be inside `LAZARUS_MCP_ALLOWED_ROOTS` (separated by `;` on Windows). By default the allowed roots are this repository, `FIXTURE_ROOT` and `WORKSPACE_ROOT`. The genome is deterministic. Set `LAZARUS_MCP_USE_MODEL=1` to let the reasoning model refine it: that sends repository text to `MODEL_BASE_URL`, and one request took about a minute on PayPulse. Server logs go to stderr; stdout carries only MCP messages.

## Docker sandbox (Foundry)

The Foundry runs a repository's dependency installs and tests in a sandbox (`apps/foundry/sandbox.py`; the security model is in `.kiro/specs/core-resurrection-engine/design.md`, "Foundry: sandbox").

- `SANDBOX_MODE=docker` is the only mode for repositories you did not write. Each command runs in a fresh `python:<version>-slim` container as uid 1000, with all capabilities dropped, a read-only root filesystem, memory, CPU and process limits, and no network while repository code runs.
- `SANDBOX_MODE=subprocess` (the default) runs commands directly on your machine with **no isolation**. It is allowed only for the PayPulse demo. `SANDBOX_ALLOW_SUBPROCESS_FOR_UNTRUSTED=True` lifts that restriction: leave it `False` unless the machine is disposable, because the repository's code would run with your user's access to your files and network.

### Setting up Docker Desktop

1. Install Docker Desktop with the WSL 2 backend and start it. `docker version` in a new PowerShell window must show both a Client and a Server section.
2. File sharing: the sandbox bind-mounts the workspace (`WORKSPACE_ROOT/<project>`) and the run's results directory (`SANDBOX_RUNS_ROOT/<run>`). With the WSL 2 backend, Windows drives are shared automatically. With the Hyper-V backend, add the drive or folder under Settings > Resources > File sharing. A mount that is not shared fails with an error that names this setting.
3. Pull the images once to save time on the first run: `docker pull python:3.11-slim` (and any other version in `SANDBOX_PYTHON_VERSIONS` you expect to use).
4. Set `SANDBOX_MODE=docker` in `.env` and restart Daphne and the Celery worker. On start, the worker removes sandbox containers, volumes and run directories left over from a crash (everything labelled `lazarus.sandbox=true`).

### Keep workspaces out of OneDrive

The default `WORKSPACE_ROOT` and `SANDBOX_RUNS_ROOT` are under the repository's `.lazarus_data/`. If the repository is inside OneDrive (for example under `Desktop` or `Documents` with folder backup turned on), sync can lock or rewrite files while tests run, and Docker Desktop bind mounts of synced folders are slow and can fail. The sandbox logs a warning when it detects this. Point both at a non-synced folder in `.env`:

```dotenv
WORKSPACE_ROOT=C:/lazarus-data/workspaces
SANDBOX_RUNS_ROOT=C:/lazarus-data/sandbox_runs
```

### Running the Docker tests

```powershell
cd backend; .\.venv\Scripts\Activate.ps1; python -m pytest -m docker -rA
```

They need the daemon and internet access (Docker Hub and PyPI). They run PayPulse in `python:3.9-slim` and `python:3.11-slim` and check that the execute phase has no network, runs as uid 1000, has a read-only root filesystem, sees none of the backend's secrets, and leaves no labelled containers or volumes behind. Without a reachable daemon they are skipped, and the skip reason says why.

## Stopping

```powershell
docker compose down
```

Add `-v` to also delete the PostgreSQL data volume (`pgdata`). This erases all local data.
