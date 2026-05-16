"""In-process event bus that replaces Redis pub/sub for local mode."""

import asyncio
import logging
from collections import deque
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)

Callback = Callable[[dict], Awaitable[None]]

# Max buffered events per channel
_MAX_BUFFER = 200


class InProcessEventBus:
    """In-process pub/sub event bus. Replaces Redis for local mode.

    Buffers events per channel so late subscribers don't miss early messages.
    """

    def __init__(self):
        # No lock needed: this event bus runs entirely within a single
        # asyncio event loop, so all mutations are naturally serialized.
        self._subscribers: dict[str, list[Callback]] = {}
        self._buffers: dict[str, deque[dict]] = {}

    async def publish(self, channel: str, event: dict) -> None:
        # Always buffer so late subscribers can catch up
        if channel not in self._buffers:
            self._buffers[channel] = deque(maxlen=_MAX_BUFFER)
        self._buffers[channel].append(event)

        # Backpressure: drop oldest if buffer exceeds max size
        if len(self._buffers[channel]) > self._buffers[channel].maxlen:
            dropped = self._buffers[channel].popleft()
            logger.debug("Dropped oldest buffered event for %s: %s", channel, dropped.get("type", ""))

        callbacks = list(self._subscribers.get(channel, []))
        if callbacks:
            await asyncio.gather(
                *[self._safe_callback(cb, channel, event) for cb in callbacks],
                return_exceptions=True,
            )

    async def _safe_callback(self, cb: Callback, channel: str, event: dict) -> None:
        try:
            await cb(event)
        except Exception:
            logger.warning("EventBus callback error on channel %s", channel, exc_info=True)

    async def subscribe(self, channel: str, callback: Callback) -> None:
        # Replay buffered events FIRST (before registering) to avoid race
        buffer = list(self._buffers.get(channel, []))
        if buffer:
            logger.info("Replaying %d buffered events on channel %s", len(buffer), channel)
            for event in buffer:
                try:
                    await callback(event)
                except Exception:
                    logger.warning("EventBus replay error on channel %s", channel, exc_info=True)
        # THEN register — new events will be delivered live
        self._subscribers.setdefault(channel, []).append(callback)

    async def unsubscribe(self, channel: str) -> None:
        self._subscribers.pop(channel, None)

    async def close(self) -> None:
        self._subscribers.clear()
        self._buffers.clear()
