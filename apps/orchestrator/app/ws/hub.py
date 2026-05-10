"""WebSocket Hub: subscribes to the in-process event bus and pushes events
to all connected WebSocket clients for a given run.

Features:
- Per-run subscription: first client connects -> subscribe; last disconnects -> unsubscribe.
- 30-second heartbeat pings to keep connections alive.
"""

import asyncio
import json
import logging
from collections import deque
from typing import Any

from fastapi import WebSocket

from app.core.local_bus import InProcessEventBus

logger = logging.getLogger(__name__)

# Heartbeat interval in seconds
_HEARTBEAT_INTERVAL = 30

# Max buffered events per run (prevents unbounded memory growth)
_MAX_BUFFER_SIZE = 500


class WebSocketHub:
    """Manages WebSocket connections grouped by run_id and bridges
    in-process event bus messages to those connections."""

    def __init__(self, event_bus: InProcessEventBus):
        self._connections: dict[str, list[WebSocket]] = {}  # run_id -> [ws]
        self._event_bus = event_bus
        self._heartbeat_tasks: dict[str, asyncio.Task] = {}  # run_id -> heartbeat task
        self._event_buffer: dict[str, deque[dict]] = {}  # run_id -> buffered events

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
        """Send a ping to all WebSocket clients for *run_id* every 30 s."""
        try:
            while True:
                await asyncio.sleep(_HEARTBEAT_INTERVAL)
                await self.broadcast(run_id, {"type": "ping"})
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
        and heartbeat task.  Flushes any buffered events to the new client.
        """
        await websocket.accept()
        if run_id not in self._connections:
            self._connections[run_id] = []
            # First client -> start event bus subscription + heartbeat
            await self._start_listening(run_id)
            await self._start_heartbeat(run_id)
        self._connections[run_id].append(websocket)

        # Flush buffered events so late-connecting clients don't miss early ones
        buffer = self._event_buffer.get(run_id)
        if buffer:
            logger.info(
                "Flushing %d buffered events for run %s", len(buffer), run_id
            )
            for msg in buffer:
                try:
                    await websocket.send_text(json.dumps(msg))
                except Exception:
                    break

        logger.info(
            "WebSocket connected for run %s (%d clients)",
            run_id, len(self._connections[run_id]),
        )

    def disconnect(self, websocket: WebSocket, run_id: str) -> None:
        """Remove a WebSocket connection. Stops event bus subscription and
        heartbeat when the last client disconnects."""
        if run_id in self._connections:
            try:
                self._connections[run_id].remove(websocket)
            except ValueError:
                pass
            if not self._connections[run_id]:
                del self._connections[run_id]
                # Schedule cleanup (cannot await in sync context)
                asyncio.create_task(self._cleanup_run(run_id))
                logger.info("Last client disconnected for run %s", run_id)
            else:
                logger.info(
                    "WebSocket disconnected for run %s (%d remaining)",
                    run_id, len(self._connections[run_id]),
                )

    async def _cleanup_run(self, run_id: str) -> None:
        """Stop event bus listener and heartbeat when no clients remain."""
        await self._stop_listening(run_id)
        await self._stop_heartbeat(run_id)
        self._event_buffer.pop(run_id, None)

    async def broadcast(self, run_id: str, message: dict[str, Any]) -> None:
        """Send a JSON message to every WebSocket connected for *run_id*.

        Also buffers events so late-connecting clients can replay them.
        """
        # Buffer for late-connecting clients
        if run_id not in self._event_buffer:
            self._event_buffer[run_id] = deque(maxlen=_MAX_BUFFER_SIZE)
        self._event_buffer[run_id].append(message)

        if run_id not in self._connections:
            return
        data = json.dumps(message)
        stale: list[WebSocket] = []
        for ws in self._connections[run_id]:
            try:
                await ws.send_text(data)
            except Exception:
                stale.append(ws)
        for ws in stale:
            self.disconnect(ws, run_id)

    async def close(self) -> None:
        """Shutdown: cancel all heartbeat tasks and close event bus."""
        for run_id in list(self._heartbeat_tasks.keys()):
            await self._stop_heartbeat(run_id)
        logger.info("WebSocketHub closed")
