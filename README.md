# Lazarus Foundry

Lazarus Foundry is an AI software revival foundry built for the Kiroween Resurrection Track. It ingests a dormant Git repository, reconstructs what the project was meant to do (the "Project Genome"), proposes scored revival strategies (`RESTORE`, `MODERNIZE`, `EXTRACT`), and, after human approval, runs a four-agent pipeline that repairs the code and verifies it with `pytest` in an isolated sandbox. The backend is Django 5.1 (Django Ninja, Channels, Celery) and the frontend is Next.js 15.

## Prerequisites

- Python 3.11+
- Node.js 20+
- Docker Desktop (only used for PostgreSQL and Redis)
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

## Stopping

```powershell
docker compose down
```

Add `-v` to also delete the PostgreSQL data volume (`pgdata`). This erases all local data.
