"""WebSocket Hub: subscribes to Redis Pub/Sub channels and pushes events
to all connected WebSocket clients for a given run.

Features:
- Lazy Redis connection (created on first subscriber).
- Per-run pubsub: first client connects -> subscribe; last disconnects -> unsubscribe.
- 30-second heartbeat pings to keep connections alive.
"""

import asyncio
import json
import logging
from typing import Any

import redis.asyncio as redis
from fastapi import WebSocket

from app.config import settings

logger = logging.getLogger(__name__)

# Heartbeat interval in seconds
_HEARTBEAT_INTERVAL = 30


class WebSocketHub:
    """Manages WebSocket connections grouped by run_id and bridges Redis
    pub/sub messages to those connections."""

    def __init__(self):
        self._connections: dict[str, list[WebSocket]] = {}  # run_id -> [ws]
        self._redis: redis.Redis | None = None
        self._pubsub: redis.client.PubSub | None = None
        self._sub_tasks: dict[str, asyncio.Task] = {}  # run_id -> listener task
        self._heartbeat_tasks: dict[str, asyncio.Task] = {}  # run_id -> heartbeat task

    # ------------------------------------------------------------------
    # Redis helpers
    # ------------------------------------------------------------------

    async def _get_redis(self) -> redis.Redis:
        if self._redis is None:
            self._redis = redis.from_url(settings.redis_url, decode_responses=True)
        return self._redis

    @staticmethod
    def _channel_name(run_id: str) -> str:
        return f"run:{run_id}:stream"

    async def _start_pubsub(self, run_id: str) -> None:
        """Subscribe to Redis channel for *run_id* and start the listener."""
        if run_id in self._sub_tasks:
            return  # Already subscribed

        r = await self._get_redis()
        if self._pubsub is None:
            self._pubsub = r.pubsub()

        channel = self._channel_name(run_id)
        await self._pubsub.subscribe(channel)
        self._sub_tasks[run_id] = asyncio.create_task(
            self._listen(run_id), name=f"pubsub-{run_id}"
        )
        logger.info("Subscribed to Redis channel %s", channel)

    async def _stop_pubsub(self, run_id: str) -> None:
        """Unsubscribe from Redis channel for *run_id* and cancel listener."""
        task = self._sub_tasks.pop(run_id, None)
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        if self._pubsub is not None:
            channel = self._channel_name(run_id)
            await self._pubsub.unsubscribe(channel)
            logger.info("Unsubscribed from Redis channel %s", channel)

    async def _listen(self, run_id: str) -> None:
        """Background task: read messages from Redis pub/sub and broadcast."""
        channel = self._channel_name(run_id)
        try:
            while True:
                message = await self._pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=1.0
                )
                if message and message["type"] == "message":
                    try:
                        data = json.loads(message["data"])
                    except (json.JSONDecodeError, TypeError):
                        data = {"type": "raw", "content": message["data"]}
                    await self.broadcast(run_id, data)
                await asyncio.sleep(0.05)  # Small sleep to avoid busy loop
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Redis listener error for run %s", run_id)

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

        On first connection for a run, also starts the Redis pubsub listener
        and heartbeat task.
        """
        await websocket.accept()
        if run_id not in self._connections:
            self._connections[run_id] = []
            # First client -> start Redis subscription + heartbeat
            await self._start_pubsub(run_id)
            await self._start_heartbeat(run_id)
        self._connections[run_id].append(websocket)
        logger.info(
            "WebSocket connected for run %s (%d clients)",
            run_id, len(self._connections[run_id]),
        )

    def disconnect(self, websocket: WebSocket, run_id: str) -> None:
        """Remove a WebSocket connection. Stops Redis subscription and
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
        """Stop pubsub and heartbeat when no clients remain."""
        await self._stop_pubsub(run_id)
        await self._stop_heartbeat(run_id)

    async def broadcast(self, run_id: str, message: dict[str, Any]) -> None:
        """Send a JSON message to every WebSocket connected for *run_id*."""
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
        """Shutdown: cancel all tasks, close Redis connection."""
        for run_id in list(self._sub_tasks.keys()):
            await self._stop_pubsub(run_id)
        for run_id in list(self._heartbeat_tasks.keys()):
            await self._stop_heartbeat(run_id)
        if self._pubsub is not None:
            await self._pubsub.close()
            self._pubsub = None
        if self._redis is not None:
            await self._redis.close()
            self._redis = None
        logger.info("WebSocketHub closed")
