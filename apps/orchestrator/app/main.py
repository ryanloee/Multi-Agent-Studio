"""FastAPI application entry point.

- Lifespan: auto-creates DB tables on startup (MVP, no Alembic).
- Routers: workflows, runs, models.
- WebSocket: /ws/runs/{run_id}/stream for real-time event streaming.
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from app.api.models import router as models_router
from app.api.runs import router as runs_router
from app.api.workflows import router as workflows_router
from app.core.database import engine
from app.models.db import Base
from app.ws.hub import WebSocketHub

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- Startup ---
    # Auto-create tables (MVP; switch to Alembic for production)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database tables verified / created")

    # Initialise WebSocket hub
    app.state.ws_hub = WebSocketHub()

    yield

    # --- Shutdown ---
    await app.state.ws_hub.close()
    await engine.dispose()
    logger.info("Shutdown complete")


app = FastAPI(
    title="Multi-Agent Studio Orchestrator",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS — allow local frontend development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# REST routers
app.include_router(workflows_router, prefix="/api/workflows", tags=["workflows"])
app.include_router(runs_router, prefix="/api/runs", tags=["runs"])
app.include_router(models_router, prefix="/api/models", tags=["models"])


# ---------------------------------------------------------------------------
# WebSocket endpoint
# ---------------------------------------------------------------------------

@app.websocket("/ws/runs/{run_id}/stream")
async def ws_run_stream(websocket: WebSocket, run_id: str):
    """WebSocket endpoint for real-time run event streaming.

    Protocol:
    - Server sends JSON events received from Redis pub/sub.
    - Server sends periodic ``{"type": "ping"}`` heartbeats every 30 s.
    - Client can send ``{"type": "pong"}`` or any JSON; currently ignored.
    """
    hub: WebSocketHub = app.state.ws_hub
    await hub.connect(websocket, run_id)
    try:
        while True:
            # Receive and discard client messages (kept alive for ping/pong)
            try:
                await websocket.receive_text()
            except WebSocketDisconnect:
                break
    finally:
        hub.disconnect(websocket, run_id)


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok"}


def run_server():
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True, reload_dirs=["app"])


if __name__ == "__main__":
    run_server()
