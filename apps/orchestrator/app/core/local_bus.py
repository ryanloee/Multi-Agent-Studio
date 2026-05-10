"""In-process event bus that replaces Redis pub/sub for local mode."""

import asyncio
import logging
from collections import deque
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

Callback = Callable[[dict], Awaitable[None]]

# Max buffered events per channel
_MAX_BUFFER = 200


class InProcessEventBus:
    """In-process pub/sub event bus. Replaces Redis for local mode.

    Buffers events per channel so late subscribers don't miss early messages.
    """

    def __init__(self):
        self._subscribers: dict[str, list[Callback]] = {}
        self._buffers: dict[str, deque[dict]] = {}
        self._lock = asyncio.Lock()

    async def publish(self, channel: str, event: dict) -> None:
        # Always buffer so late subscribers can catch up
        if channel not in self._buffers:
            self._buffers[channel] = deque(maxlen=_MAX_BUFFER)
        self._buffers[channel].append(event)

        callbacks = self._subscribers.get(channel, [])
        for cb in callbacks:
            try:
                await cb(event)
            except Exception:
                logger.warning("EventBus callback error on channel %s", channel, exc_info=True)

    async def subscribe(self, channel: str, callback: Callback) -> None:
        if channel not in self._subscribers:
            self._subscribers[channel] = []
        self._subscribers[channel].append(callback)

        # Replay buffered events to the new subscriber
        buffer = self._buffers.get(channel)
        if buffer:
            logger.info("Replaying %d buffered events on channel %s", len(buffer), channel)
            for event in buffer:
                try:
                    await callback(event)
                except Exception:
                    logger.warning("EventBus replay error on channel %s", channel, exc_info=True)

    async def unsubscribe(self, channel: str) -> None:
        self._subscribers.pop(channel, None)

    async def close(self) -> None:
        self._subscribers.clear()
        self._buffers.clear()
