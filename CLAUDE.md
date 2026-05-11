# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Multi-Agent Studio is a visual AI multi-agent workflow orchestration platform. Users build DAG workflows by dragging node types (Coder, Plan, Explore, Shell, Review, Human) onto a React Flow canvas, then run them with real-time streaming output. It supports two workflow modes: **manual** (user designs the DAG on canvas) and **auto** (planner agent builds the DAG from a goal description). The monorepo has a Next.js 14 frontend, a Python FastAPI backend, and a standalone Python agent framework.

## Common Commands

### Frontend (apps/web)
```bash
cd apps/web
pnpm install              # install deps
pnpm dev                  # dev server on :3000
pnpm build                # production build
pnpm lint                 # ESLint
pnpm type-check           # tsc --noEmit
```

### Backend (apps/orchestrator)
```bash
cd apps/orchestrator
poetry install            # install deps
poetry run python -m app.main   # API server on :8000 (with hot reload)
poetry run pytest               # run all tests
poetry run pytest tests/test_specific.py -v   # run single test file
poetry run pytest tests/test_specific.py::test_name -v  # run single test
poetry run ruff check app/      # lint
poetry run mypy app/            # type check
```

### Agent framework (apps/agent)
```bash
cd apps/agent
pip install -e ".[dev]"         # install in dev mode
python -m pytest tests/ -v      # run tests
```

### Dev environment (both services)
```bash
# Windows
powershell scripts/dev.ps1
# Linux/macOS
bash scripts/dev.sh
```

## Architecture

### Monorepo Layout
- `apps/web/` — Next.js 14 frontend (App Router, TypeScript)
- `apps/orchestrator/` — Python FastAPI backend (Poetry, Python 3.11+)
- `apps/agent/` — Standalone Python agent framework (`mas_agent` package)
- `apps/gateway/` — Go API gateway (Phase 2, not yet active)
- `packages/shared-types/` — JSON schemas for workflow/events/node-config
- `scripts/` — Setup and dev shell scripts (setup.sh/ps1, dev.sh/ps1)
- `doc/` — Technical planning documents

### Frontend Stack
- **React Flow v12** (`@xyflow/react`) for the DAG canvas
- **Zustand v5** for state (`workflowStore.ts`, `runStore.ts`, `taskStore.ts`, `settingsStore.ts`, `localeStore.ts`)
- **Monaco Editor** for prompt editing
- **Xterm.js** for terminal output rendering
- **TailwindCSS** for styling
- **i18n** — Full Chinese/English support (`src/lib/i18n.ts`, 220+ keys, locale persisted in localStorage)
- Next.js rewrites proxy `/api/*` to the backend at `localhost:8000`
- Path alias: `@/*` maps to `./src/*`

Key frontend paths:
- `src/components/canvas/` — Flow canvas and custom node components
- `src/components/panels/` — Config, output, and tool call panels
- `src/stores/` — Zustand stores
- `src/hooks/useWebSocket.ts` — WebSocket connection to `/ws/runs/{runId}/stream`
- `src/lib/api.ts` — Typed API client with structured error handling
- `src/lib/i18n.ts` — Translation keys and locale management
- `src/lib/constants.ts` — Node connection rules (`VALID_CONNECTIONS` whitelist: shell↔review blocked, human is sink-only)

### Backend Stack (MVP — local-only, no Docker/Redis/Temporal)

The MVP replaces the production infrastructure with local equivalents:

| Production (README describes) | MVP (actual code) |
|---|---|
| PostgreSQL | SQLite via aiosqlite |
| Redis Pub/Sub | `core/local_bus.py` — InProcessEventBus |
| Temporal.io | `core/local_engine.py` — LocalDAGExecutor |
| Docker sandbox | `core/local_sandbox.py` — LocalSandbox (filesystem) |

Backend module responsibilities:
- `app/main.py` — FastAPI app with lifespan (creates tables, wires up event bus + sandbox + executor)
- `app/config.py` — Pydantic Settings, env vars prefixed with `MAS_`
- `app/api/` — REST routers: `workflows.py` (CRUD), `runs.py` (execute/cancel), `models.py` (available models), `planner_chat.py` (interactive workflow design with SSE), `settings.py` (global settings), `tasks.py` (task board CRUD)
- `app/ws/hub.py` — WebSocket hub, subscribes to event bus per run
- `app/core/local_bus.py` — In-process async pub/sub
- `app/core/local_engine.py` — DAG executor: compiles React Flow JSON → topological layers, runs nodes with concurrency control
- `app/core/local_sandbox.py` — Filesystem-based sandbox (workspace directories under `.sandboxes/`)
- `app/core/task_scheduler.py` — Task execution scheduler with doom-loop detection and planner escalation
- `app/workflows/compiler.py` — DAG compiler (React Flow JSON → topo-sorted execution layers)
- `app/workflows/plan_parser.py` — Plan node output → child node creation
- `app/sandbox/checkpoint.py` — Git-based checkpoint/rollback
- `app/sandbox/provision.py` — Sandbox workspace setup
- `app/models/db.py` — SQLAlchemy ORM models
- `app/models/schemas.py` — Pydantic request/response schemas

### Agent Framework (apps/agent)
Standalone `mas_agent` package providing the agentic loop:
- `mas_agent/loop.py` — Core agent loop with doom-loop detection and message compaction
- `mas_agent/tools/` — Tool system with permission checking (allow/deny/ask)
- `mas_agent/providers/` — Multi-provider LLM support (Anthropic, OpenAI-compatible)
- `mas_agent/tool_registry.py` — Tool registry with agent-type-specific validation

### Event Streaming Pipeline
Agent output → InProcessEventBus → WebSocketHub → WebSocket → Frontend renders in Monaco (llm_token), Xterm.js (shell_stdout), or tool call panels.

WebSocket behavior: 30-second heartbeat pings, max 500 buffered events per run for late-connecting clients, 3-second auto-reconnect with 10-second fallback polling.

### Key Configuration
- Backend env vars use `MAS_` prefix, loaded from `apps/orchestrator/.env`
  - `MAS_HOST` / `MAS_PORT` — Server binding
  - `MAS_DATABASE_URL` — SQLite path (default: `sqlite+aiosqlite:///data/multi_agent_studio.db`)
  - `MAS_ACCESS_PASSWORD` — Optional app access password for LAN deployments; when set, API and WebSocket requests require the password
- Frontend env vars:
  - `BACKEND_URL` — Backend URL for Next.js rewrites (default: `http://localhost:8000`)
  - `NEXT_PUBLIC_API_URL` — API base URL (default: `/api`)
  - `NEXT_PUBLIC_WS_URL` — WebSocket URL (default: `ws://localhost:8000`)
  - `MAS_STATIC_EXPORT=1` — Enable static export for EXE packaging
- Backend data stored in `apps/orchestrator/data/` (SQLite DB + settings JSON)
- Ports: Frontend 3000, Backend API 8000, Swagger docs at `/docs`

### CI/CD
GitHub Actions (`main` branch pushes and PRs):
- **Frontend** — lint, type-check, build (Node 20, pnpm)
- **Backend** — pytest, ruff (Python 3.12, Poetry)
- **Agent** — pytest (Python 3.12, pip)

## Development Notes

- The `apps/orchestrator/app/workflows/` directory has `compiler.py` and `plan_parser.py` but the deleted files (dag_workflow.py, activities.py, worker.py) were Temporal-specific and no longer exist — the local engine in `core/local_engine.py` handles execution.
- Similarly, `agents/`, `mcp_server/`, `memory/`, `streaming/`, and `sandbox/manager.py` are deleted (git status shows `D`) — their functionality is collapsed into the local engine/sandbox modules.
- Ruff config: target Python 3.11, line length 100, rules E/F/I/N/W
- pytest-asyncio mode is `auto`
- TypeScript strict mode enabled in the frontend
