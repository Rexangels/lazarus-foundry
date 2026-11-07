---
inclusion: always
---
# Technology Stack & Engineering Conventions

## 1. Backend Architecture (Agentic Django)
- **Framework:** Django 5.1+ with **Django Ninja** (`django-ninja`) for all REST API endpoints. Do NOT use Django REST Framework (DRF).
- **7 Core Django Apps:** `apps.projects`, `apps.ingestion`, `apps.genome`, `apps.revival`, `apps.foundry`, `apps.rebirth`, `apps.telemetry`.
- **Schemas & Validation:** Use `ninja.Schema` and `ninja.ModelSchema` (Pydantic v2) for all request payloads, response serialization, and structured LLM outputs.
- **Database:** PostgreSQL 16 with `pgvector` (`pgvector/pgvector:pg16`). Enable the extension via `pgvector.django.VectorExtension()` in the first migration.
- **Async & Real-Time Telemetry:**
  - Use **Celery + Redis** (the worker runs natively on Windows with `celery -A config worker -l info --pool=solo`; Docker runs only PostgreSQL and Redis) for repository ingestion, Project Genome synthesis, and Foundry execution tasks.
  - Use **Django Channels (`channels_redis`)** to stream real-time agent events (`AST_PARSED`, `GENOME_NODE_CREATED`, `SANDBOX_LOG`, `TEST_PASSED`) via WebSockets (`ws://.../ws/projects/{project_id}/telemetry/`).
- **Code & Git Forensics (Verified Packages):**
  - Use `GitPython>=3.1.43` (`git.Repo`) for branch topology, commit timeline traversal, and diff extraction.
  - Use `tree-sitter>=0.23.0` and `tree-sitter-language-pack>=1.8.0` (plus `tree-sitter-python>=0.23.0`) for deterministic AST extraction. Do NOT use the deprecated `tree-sitter-languages` package.
- **AI & LLM Orchestration:**
  - Use the `openai` Python SDK with a custom base URL: `settings.MODEL_BASE_URL` (`https://api.meta.ai/v1`) and key `settings.MODEL_API_KEY`. The current provider is Meta Model API.
  - Testing model: `muse-spark-1.3` (recommended). Preview alternative: `muse-wonder-1.0`.
  - Read the model ID exclusively from `django.conf.settings.MODEL_ID` (loaded from `.env`). Never hardcode model strings in agent files.
- **Sandbox Execution (`SANDBOX_MODE`):**
  - Controlled via `settings.SANDBOX_MODE` (`"subprocess"` default for native Windows compatibility, or `"docker"` using image `python:3.11-slim` with `--memory="1g" --cpus="1.0"` and preview port `8010`).

## 2. Frontend Architecture
- **Framework:** Next.js 15 (App Router), React 19, TypeScript (strict mode), Tailwind CSS.
- **Palette:** Obsidian (`#07090E`), Slate (`#0F141C`), Bioluminescent Emerald (`#10B981`), Amber Pulse (`#F59E0B`), Crimson (`#EF4444`), Electric Violet (`#8B5CF6`).
- **Graph Visualization:** `@xyflow/react` (React Flow).
