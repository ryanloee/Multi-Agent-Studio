# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Multi-Agent Studio is a visual AI multi-agent workflow orchestration platform. Users drag node types (Coder, Planner, Designer, Explorer, Merger, Shell, Reviewer, Human) onto a React Flow canvas to build DAG workflows, then run them with real-time streaming output. Two workflow modes: **manual** (user designs DAG on canvas) and **auto** (Planner Agent generates DAG from goal description). The repo uses Next.js 14 frontend, Python FastAPI backend, and an in-process Python AgentRunner for node execution. Monorepo managed with pnpm workspaces + Turborepo.

## Common Commands

### Frontend (apps/web)
```bash
cd apps/web
pnpm install              # Install dependencies
pnpm dev                  # Dev server :3000
pnpm build                # Production build
pnpm lint                 # ESLint
pnpm type-check           # tsc --noEmit
pnpm test                 # Vitest unit tests
```

### Backend (apps/orchestrator)
```bash
cd apps/orchestrator
poetry install                            # Install dependencies
poetry run python -m app.main             # API server :8000 (hot reload)
poetry run pytest                         # Run all tests
poetry run pytest tests/test_specific.py -v   # Run single test file
poetry run pytest tests/test_specific.py::test_name -v  # Run single test
poetry run ruff check app/                # Lint
poetry run mypy app/                      # Type check
```

### End-to-End Test (root)
```bash
# Requires a running backend with valid API key in apps/orchestrator/.env
python tests/e2e_test.py
```

### Dev Environment (start both frontend and backend)
```bash
# Windows
powershell scripts/start.ps1
# Linux/macOS
bash scripts/start.sh
# Or from repo root via Turborepo
pnpm dev
```

## Architecture

### Repo Structure
- `apps/web/` — Next.js 14 frontend (App Router, TypeScript)
- `apps/orchestrator/` — Python FastAPI backend (Poetry, Python 3.11+)
- `packages/shared-types/` — JSON Schema definitions for workflows, events, node config
- `scripts/` — Setup and dev launch scripts (setup.sh/ps1, start.sh/ps1, build.ps1)
- `tests/` — Root-level e2e test

### Frontend Stack
- **React Flow v12** (`@xyflow/react`) DAG canvas
- **Zustand v5** state management (6 stores: `workflowStore`, `runStore`, `taskStore`, `settingsStore`, `localeStore`, `plannerChatStore`)
- **Monaco Editor** for prompt editing and LLM output rendering
- **Xterm.js** for terminal output rendering
- **TailwindCSS** for styling
- **i18n** — Full Chinese/English support (`src/lib/i18n.ts`, 270+ translation keys, language preference persisted to localStorage)
- **Vitest** + Testing Library for unit tests
- Next.js rewrites proxy `/api/*` to backend `localhost:8000`
- Path alias: `@/*` maps to `./src/*`

Frontend key paths:
- `src/components/canvas/` — Flow canvas and custom node components
- `src/components/panels/` — Config, output, tool call panels
- `src/stores/` — Zustand state stores
- `src/hooks/useWebSocket.ts` — WebSocket connection to `/ws/runs/{runId}/stream`
- `src/lib/api.ts` — Typed API client with structured error handling
- `src/lib/i18n.ts` — Translation keys and language management
- `src/lib/constants.ts` — Node connection rules (`VALID_CONNECTIONS` whitelist: shell↔review cannot connect, human can only be a sink)

### Backend Stack (MVP Local Mode — no Docker/Redis/Temporal)

MVP replaces production infra with local components:

| Production | MVP Local Replacement |
|---|---|
| PostgreSQL | SQLite (aiosqlite) |
| Redis Pub/Sub | `core/local_bus.py` — InProcessEventBus |
| Temporal.io | `core/director_loop.py` — DirectorLoop (agentic dispatch loop) |
| Docker Sandbox | `core/local_sandbox.py` — LocalSandbox (filesystem isolation) |

#### Director Loop Architecture

The execution engine uses an agentic Director Loop pattern, not a static DAG executor:

1. **DirectorLoop** (`core/director_loop.py`) — Scheduler. Director Agent reads compressed World Model, uses tool-use to call a strong LLM to decide next action, dispatches sub-agents, parses results, loops until done or max iterations.
2. **NodeRunner** (`core/node_runner.py`) — Executes a single Agent node. Creates/reuses sandbox, initializes workspace, calls Python AgentRunner for the LLM loop.
3. **AgentRunner** (`core/agent_runner.py`) — Python agent loop. Directly calls LLM API via httpx, executes tools, manages conversation history. Replaces the former Bun/TypeScript opencode CLI subprocess.
4. **AgentLLM** (`core/agent_llm.py`) — LLM API client. Supports both Anthropic Messages API and OpenAI Chat Completions API formats, streaming and non-streaming.
5. **AgentTools** (`core/agent_tools.py`) — 6 core tool implementations: shell, read, write, edit, glob, grep. Compatible with Anthropic/OpenAI tool_use format. Includes a 9-strategy fuzzy edit engine.
6. **WorldModel** (`core/world_model.py`) — Compressed project state (2-4 KB), tracking completed tasks, findings, and TODOs, updated each iteration.
7. **Director Prompts** (`core/director_prompts.py`) — Role-specific system prompts for Director, Scout, Worker, Tester sub-agents.
8. **Director Tools** (`core/director_tools.py`) — Director's `decide` tool schema (actions: scout, worker, test, done, failed).
9. **SandboxBackend** (`core/sandbox_backend.py`) — Multi-backend sandbox abstraction supporting local, bubblewrap, and Docker isolation.
10. **DebugLogger** (`core/debug_logger.py`) — Detailed runtime logging, toggled via `debug_mode` in settings.json, writes to `data/debug.log` (with rotation).

Backend module responsibilities:
- `app/main.py` — FastAPI entry point (lifespan creates tables, connects event bus + sandbox + DirectorLoop + NodeRunner)
- `app/config.py` — Pydantic Settings, all env vars use `MAS_` prefix
- `app/api/` — REST routes: `workflows.py` (CRUD), `runs.py` (execute/cancel), `models.py` (available models), `planner_chat.py` (SSE interactive planning), `planner_tools.py` (planner research tools: web search, file read, grep), `settings.py` (global settings), `tasks.py` (task board CRUD), `artifacts.py` (run artifacts), `shared_doc.py` (workflow shared doc)
- `app/ws/hub.py` — WebSocket Hub, subscribes to event bus by run ID
- `app/core/local_bus.py` — In-process async pub/sub event bus
- `app/sandbox/checkpoint.py` — Git checkpoint / rollback
- `app/sandbox/provision.py` — Sandbox workspace initialization
- `app/models/db.py` — SQLAlchemy ORM models
- `app/models/schemas.py` — Pydantic request/response models

### Event Stream Pipeline
AgentRunner calls LLM API directly → emit events via callback → InProcessEventBus → WebSocketHub → WebSocket → frontend real-time rendering (Monaco renders llm_token, Xterm.js renders shell_stdout, tool call panel renders tool_call)

WebSocket behavior: 30s heartbeat ping, max buffer 500 events (for late-connecting clients to replay), 3s auto-reconnect, 10s fallback polling.

### Key Configuration
- Backend env vars use `MAS_` prefix, loaded from `apps/orchestrator/.env`
  - `MAS_HOST` / `MAS_PORT` — Service bind address/port
  - `MAS_DATABASE_URL` — SQLite path (default: `sqlite+aiosqlite:///data/multi_agent_studio.db`)
  - `MAS_ACCESS_PASSWORD` — Optional access password (for LAN deployment; API and WebSocket requests must carry it when set)
- Frontend env vars:
  - `BACKEND_URL` — Backend URL, Next.js rewrites proxy target (default: `http://localhost:8000`)
  - `NEXT_PUBLIC_API_URL` — API base path (default: `/api`)
  - `NEXT_PUBLIC_WS_URL` — WebSocket address (default: `ws://localhost:8000`)
  - `MAS_STATIC_EXPORT=1` — Enable static export (EXE packaging)
- Backend data stored in `apps/orchestrator/data/` (SQLite database + settings JSON)
- Model providers configured in `apps/orchestrator/app/api/models.json`
- Ports: frontend 3000, backend API 8000, Swagger docs at `/docs`

### CI/CD
GitHub Actions (push/PR to `main`):
- **Frontend** — lint, type-check, build (Node 20, pnpm)
- **Backend** — pytest, ruff (Python 3.12, Poetry)
- **Agent** — pytest (Python 3.12)
- **Release** — On `v*` tags: PyInstaller builds Windows EXE, publishes GitHub Release

## Development Notes

- The execution engine is **DirectorLoop** (`core/director_loop.py`), not a static DAG executor. The Director Agent dynamically dispatches sub-agents (scout/worker/tester) based on the World Model.
- Planner chat uses real research tools (`api/planner_tools.py`): web search, file read, grep, URL fetch — server-side execution, multi-turn tool loop.
- Ruff config: target Python 3.11, line-length 100, rules E/F/I/N/W
- pytest-asyncio mode is `auto`
- Frontend has TypeScript strict mode enabled
- Debug logging: enable via `debug_mode: true` in settings.json, logs write to `data/debug.log` (with rotation)
- `CLAUDE.md` is gitignored — it's a local-only file for AI guidance
- Package manager: pnpm 9.15.0, Python 3.11+, Node 20
