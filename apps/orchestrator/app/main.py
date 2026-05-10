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
from app.api.workflows import router as workflows_router
from app.config import settings
from app.core.database import engine as db_engine
from app.core.local_bus import InProcessEventBus
from app.core.local_engine import LocalDAGExecutor
from app.core.local_sandbox import LocalSandbox
from app.models.db import Base
from app.sandbox.checkpoint import GitCheckpointManager
from app.sandbox.provision import SandboxProvisioner
from app.ws.hub import WebSocketHub

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- Startup ---
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
