"""FastAPI application entry point.

- Lifespan: auto-creates DB tables on startup (MVP, no Alembic).
- Routers: workflows, runs, models.
- WebSocket: /ws/runs/{run_id}/stream for real-time event streaming.
- All infrastructure is local: SQLite, in-process event bus, subprocess sandbox.
"""

import asyncio
import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

# Windows: uvicorn's reloader may switch to SelectorEventLoop which doesn't
# support create_subprocess_exec.  Force ProactorEventLoop so that the sandbox
# can spawn agent subprocesses.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from dotenv import load_dotenv

# Load .env into os.environ so non-MAS_ vars (like MIMO_API_KEY) are available
load_dotenv(Path(__file__).parent.parent / ".env", override=False)

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from app.api.models import router as models_router
from app.api.runs import router as runs_router, init_engine as init_runs_engine
from app.api.planner_chat import router as planner_chat_router
from app.api.settings import router as settings_router
from app.api.tasks import router as tasks_router, init_task_deps as init_tasks_deps
from app.api.workflows import router as workflows_router
from app.config import settings
from app.core.database import engine as db_engine
from app.core.local_bus import InProcessEventBus
from app.core.local_engine import LocalDAGExecutor
from app.core.local_sandbox import LocalSandbox
from app.models.db import Base
from app.models.task import Task, TaskMessage  # noqa: F401 — ensure tables are registered
from app.models.db import ChatMessage  # noqa: F401 — ensure chat_messages table is registered
from app.sandbox.checkpoint import GitCheckpointManager
from app.sandbox.provision import SandboxProvisioner
from app.ws.hub import WebSocketHub

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- Startup ---

    # MVP schema migration: SQLite doesn't support ALTER TABLE well, so we
    # detect schema changes and recreate the database.  Safe for the MVP
    # because data is ephemeral.
    db_path = Path(settings.database_url.split("///")[-1])
    if db_path.exists():
        needs_reset = False
        async with db_engine.begin() as conn:
            from sqlalchemy import text as sa_text
            result = await conn.execute(sa_text("PRAGMA table_info(workflows)"))
            columns = {row[1] for row in result.fetchall()}
            if "workspace_directory" not in columns:
                logger.info(
                    "Schema migration: workflows table missing workspace_directory, "
                    "will recreate database"
                )
                needs_reset = True
            if "mode" not in columns or "goal" not in columns:
                logger.info(
                    "Schema migration: workflows table missing mode/goal, "
                    "will recreate database"
                )
                needs_reset = True
            # Check for chat_messages table (new table for context persistence)
            result2 = await conn.execute(sa_text("SELECT name FROM sqlite_master WHERE type='table' AND name='chat_messages'"))
            if not result2.fetchone():
                logger.info("Schema migration: chat_messages table not found, will be created by create_all")
        if needs_reset:
            await db_engine.dispose()
            db_path.unlink(missing_ok=True)

    async with db_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database tables verified / created")

    # Shared event bus (replaces Redis)
    event_bus = InProcessEventBus()
    app.state.event_bus = event_bus

    # WebSocket hub
    app.state.ws_hub = WebSocketHub(event_bus)

    # Local sandbox (replaces Docker containers)
    sandbox = LocalSandbox(settings.sandbox_root)
    checkpoint = GitCheckpointManager(sandbox)
    provisioner = SandboxProvisioner(sandbox)

    # DAG executor (replaces Temporal)
    dag_executor = LocalDAGExecutor(
        sandbox=sandbox,
        event_bus=event_bus,
        checkpoint=checkpoint,
        provisioner=provisioner,
    )
    init_runs_engine(dag_executor)
    init_tasks_deps(dag_executor, event_bus)
    logger.info("Local DAG executor initialised (no Docker/Temporal/Redis)")

    yield

    # --- Shutdown ---
    await app.state.ws_hub.close()
    await db_engine.dispose()
    logger.info("Shutdown complete")


app = FastAPI(
    title="Multi-Agent Studio Orchestrator",
    version="0.1.0",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(workflows_router, prefix="/api/workflows", tags=["workflows"])
app.include_router(runs_router, prefix="/api/runs", tags=["runs"])
app.include_router(planner_chat_router, prefix="/api/planner", tags=["planner"])
app.include_router(settings_router, prefix="/api/settings", tags=["settings"])
app.include_router(tasks_router, prefix="/api/runs", tags=["tasks"])
app.include_router(models_router, prefix="/api/models", tags=["models"])


@app.websocket("/ws/runs/{run_id}/stream")
async def ws_run_stream(websocket: WebSocket, run_id: str):
    hub: WebSocketHub = app.state.ws_hub
    await hub.connect(websocket, run_id)
    try:
        while True:
            try:
                await websocket.receive_text()
            except WebSocketDisconnect:
                break
    finally:
        hub.disconnect(websocket, run_id)


@app.get("/health")
async def health():
    return {"status": "ok"}


def run_server():
    import uvicorn
    import logging
    logging.basicConfig(level=logging.INFO)
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True, reload_dirs=["app"], log_level="info")


if __name__ == "__main__":
    run_server()
