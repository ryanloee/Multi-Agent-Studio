import json
import logging

import redis.asyncio as redis

from app.config import settings

logger = logging.getLogger(__name__)

_CONNECT_RETRIES = 3
_CONNECT_BACKOFF_S = [1, 2, 4]  # exponential: 1s, 2s, 4s

_PUBLISH_RETRIES = 5
_PUBLISH_BACKOFF_BASE_S = 0.1  # 100ms initial backoff
_PUBLISH_BACKOFF_MAX_S = 5.0   # 5s max backoff


class StreamPublisher:
    """Publishes stream events to Redis Pub/Sub for WebSocket distribution.

    Features:
    - Lazy Redis connection initialization
    - Automatic retry with exponential backoff on publish failure
    - Reconnection on connection loss
    """

    def __init__(self):
        self._redis: redis.Redis | None = None

    async def _get_redis(self) -> redis.Redis:
        if self._redis is None:
            self._redis = await self._connect_with_retry()
        return self._redis

    async def _connect_with_retry(self) -> redis.Redis:
        """Create Redis connection with retry + exponential backoff (3 attempts: 1s/2s/4s)."""
        last_exc: Exception | None = None
        for attempt in range(_CONNECT_RETRIES):
            try:
                r = redis.from_url(settings.redis_url)
                # Verify connection is actually alive
                await r.ping()
                logger.info("Redis connection established (attempt %d/%d)", attempt + 1, _CONNECT_RETRIES)
                return r
            except (redis.ConnectionError, redis.TimeoutError, OSError) as exc:
                last_exc = exc
                if attempt < _CONNECT_RETRIES - 1:
                    wait = _CONNECT_BACKOFF_S[attempt]
                    logger.warning(
                        "Redis connect failed (attempt %d/%d), retrying in %ds: %s",
                        attempt + 1, _CONNECT_RETRIES, wait, exc,
                    )
                    import asyncio
                    await asyncio.sleep(wait)
        raise redis.ConnectionError(f"Redis connect failed after {_CONNECT_RETRIES} attempts") from last_exc

    async def _reconnect(self) -> redis.Redis:
        """Force reconnection by closing old client and creating new one."""
        if self._redis:
            try:
                await self._redis.close()
            except Exception:
                pass
        self._redis = redis.from_url(settings.redis_url)
        return self._redis

    async def publish(self, event: dict) -> None:
        """Publish event to Redis channel run:{run_id}:stream.

        Retries with exponential backoff on connection failure.
        """
        run_id = event.get("run_id", "unknown")
        channel = f"run:{run_id}:stream"
        payload = json.dumps(event)

        for attempt in range(_PUBLISH_RETRIES):
            try:
                r = await self._get_redis()
                await r.publish(channel, payload)
                return
            except (redis.ConnectionError, redis.TimeoutError, OSError) as exc:
                wait = min(_PUBLISH_BACKOFF_BASE_S * (2 ** attempt), _PUBLISH_BACKOFF_MAX_S)
                logger.warning(
                    "Redis publish failed (attempt %d/%d), retrying in %.2fs: %s",
                    attempt + 1,
                    _PUBLISH_RETRIES,
                    wait,
                    exc,
                )
                # Force reconnect on connection errors
                try:
                    await self._reconnect()
                except Exception:
                    pass
                import asyncio
                await asyncio.sleep(wait)
            except Exception as exc:
                logger.error(
                    "Unexpected Redis publish error for channel %s: %s",
                    channel,
                    exc,
                )
                return

        logger.error(
            "Redis publish gave up after %d attempts for channel %s",
            _PUBLISH_RETRIES,
            channel,
        )

    async def close(self) -> None:
        if self._redis:
            await self._redis.close()
            self._redis = None
