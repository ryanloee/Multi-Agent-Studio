"""WebSocket Hub: subscribes to the in-process event bus and pushes events
to all connected WebSocket clients for a given run.

Features:
- Per-run subscription: first client connects -> subscribe; last disconnects -> unsubscribe.
- 30-second heartbeat pings to keep connections alive.
"""

import asyncio
import json
import logging
import os
import time
from typing import Any

from fastapi import WebSocket

from app.core.local_bus import InProcessEventBus

logger = logging.getLogger(__name__)

# Heartbeat interval in seconds
_HEARTBEAT_INTERVAL = 30

# Stale connection timeout in seconds (no pong received within this window)
_STALE_TIMEOUT = 60

# Cleanup grace period in seconds (time to wait before stopping subscriptions)
_CLEANUP_GRACE_PERIOD = int(os.environ.get("MAS_WS_CLEANUP_GRACE", "5"))


class WebSocketHub:
    """Manages WebSocket connections grouped by run_id and bridges
    in-process event bus messages to those connections."""

    def __init__(self, event_bus: InProcessEventBus):
        self._connections: dict[str, list[WebSocket]] = {}  # run_id -> [ws]
        self._event_bus = event_bus
        self._heartbeat_tasks: dict[str, asyncio.Task] = {}  # run_id -> heartbeat task
        self._last_active: dict[WebSocket, float] = {}  # ws -> timestamp of last successful send
        self._cleanup_locks: dict[str, asyncio.Lock] = {}  # run_id -> lock for cleanup race

    # ------------------------------------------------------------------
    # Event bus helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _channel_name(run_id: str) -> str:
        return f"run:{run_id}:stream"

    async def _start_listening(self, run_id: str) -> None:
        """Subscribe to event bus channel for *run_id*."""
        if run_id in self._connections and len(self._connections[run_id]) > 1:
            return  # Already listening
        channel = self._channel_name(run_id)

        async def callback(event: dict):
            await self.broadcast(run_id, event)

        await self._event_bus.subscribe(channel, callback)
        logger.info("Subscribed to event bus channel %s", channel)

    async def _stop_listening(self, run_id: str) -> None:
        """Unsubscribe from event bus channel for *run_id*."""
        channel = self._channel_name(run_id)
        await self._event_bus.unsubscribe(channel)
        logger.info("Unsubscribed from event bus channel %s", channel)

    # ------------------------------------------------------------------
    # Heartbeat
    # ------------------------------------------------------------------

    async def _start_heartbeat(self, run_id: str) -> None:
        if run_id in self._heartbeat_tasks:
            return
        self._heartbeat_tasks[run_id] = asyncio.create_task(
            self._heartbeat_loop(run_id), name=f"heartbeat-{run_id}"
        )

    async def _stop_heartbeat(self, run_id: str) -> None:
        task = self._heartbeat_tasks.pop(run_id, None)
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _heartbeat_loop(self, run_id: str) -> None:
        """Send a ping to all WebSocket clients for *run_id* every 30 s.

        Tracks last successful send and removes stale connections that
        haven't received a message within _STALE_TIMEOUT seconds.
        """
        try:
            while True:
                await asyncio.sleep(_HEARTBEAT_INTERVAL)
                now = time.monotonic()
                stale: list[WebSocket] = []
                for ws in list(self._connections.get(run_id, [])):
                    last = self._last_active.get(ws, 0)
                    if last > 0 and (now - last) > _STALE_TIMEOUT:
                        stale.append(ws)
                        continue
                    try:
                        await ws.send_text(json.dumps({"type": "ping"}))
                        self._last_active[ws] = time.monotonic()
                    except Exception:
                        stale.append(ws)
                for ws in stale:
                    self.disconnect(ws, run_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Heartbeat error for run %s", run_id)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def connect(self, websocket: WebSocket, run_id: str) -> None:
        """Accept a WebSocket connection and register it for *run_id*.

        On first connection for a run, also starts the event bus listener
        and heartbeat task.  Late-connecting clients replay from the event
        bus buffer (handled by InProcessEventBus.subscribe).
        """
        await websocket.accept()
        is_first = run_id not in self._connections
        if is_first:
            self._connections[run_id] = []
        self._connections[run_id].append(websocket)
        self._last_active[websocket] = time.monotonic()

        if is_first:
            # First client -> start event bus subscription + heartbeat
            await self._start_listening(run_id)
            await self._start_heartbeat(run_id)
            # Cancel any pending cleanup
            if run_id in self._cleanup_locks:
                async with self._cleanup_locks[run_id]:
                    pass  # just acquire to block if cleanup is in flight

        logger.info(
            "WebSocket connected for run %s (%d clients)",
            run_id, len(self._connections[run_id]),
        )

    def disconnect(self, websocket: WebSocket, run_id: str) -> None:
        """Remove a WebSocket connection. Stops event bus subscription and
        heartbeat when the last client disconnects."""
        conns = self._connections.get(run_id, [])
        try:
            conns.remove(websocket)
        except ValueError:
            pass
        self._last_active.pop(websocket, None)

        if not conns:
            self._connections.pop(run_id, None)
            # Schedule cleanup (cannot await in sync context)
            task = asyncio.create_task(self._cleanup_run(run_id))
            task.add_done_callback(
                lambda t: logger.exception("Cleanup failed for run %s", run_id)
                if (not t.cancelled() and t.exception()) else None
            )
            logger.info("Last client disconnected for run %s", run_id)
        else:
            logger.info(
                "WebSocket disconnected for run %s (%d remaining)",
                run_id, len(conns),
            )

    async def _cleanup_run(self, run_id: str) -> None:
        """Stop event bus listener and heartbeat when no clients remain.

        Uses a grace period and re-check to prevent race with new connect().
        """
        # Ensure a lock exists for this run_id
        if run_id not in self._cleanup_locks:
            self._cleanup_locks[run_id] = asyncio.Lock()

        async with self._cleanup_locks[run_id]:
            await asyncio.sleep(_CLEANUP_GRACE_PERIOD)  # grace period

            # Check if someone reconnected during grace period
            if run_id in self._connections and self._connections[run_id]:
                logger.info("Client reconnected during cleanup grace period for run %s", run_id)
                return

            await self._stop_listening(run_id)
            await self._stop_heartbeat(run_id)
            self._cleanup_locks.pop(run_id, None)

    async def broadcast(self, run_id: str, message: dict[str, Any]) -> None:
        """Send a JSON message to every WebSocket connected for *run_id*."""
        if run_id not in self._connections:
            return
        data = json.dumps(message)
        now = time.monotonic()

        async def _send(ws: WebSocket) -> bool:
            try:
                await ws.send_text(data)
                self._last_active[ws] = now
                return True
            except Exception:
                return False

        results = await asyncio.gather(
            *[_send(ws) for ws in self._connections[run_id]],
            return_exceptions=False,
        )

        # Remove stale connections
        stale = [
            ws for ws, ok in zip(self._connections[run_id], results) if not ok
        ]
        for ws in stale:
            self.disconnect(ws, run_id)

    async def close(self) -> None:
        """Shutdown: cancel all heartbeat tasks and close event bus."""
        for run_id in list(self._heartbeat_tasks.keys()):
            await self._stop_heartbeat(run_id)
        logger.info("WebSocketHub closed")
