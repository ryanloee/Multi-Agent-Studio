import time
from typing import Optional


class StreamThrottler:
    """Throttles shell_stdout events to prevent Xterm.js from freezing.
    Merges multiple events within a time window into a single chunk."""

    def __init__(self, window_ms: int = 100):
        self.window_ms = window_ms
        self._buffer: list[str] = []
        self._last_flush = time.monotonic()

    def add(self, content: str) -> Optional[str]:
        self._buffer.append(content)
        now = time.monotonic()
        if (now - self._last_flush) * 1000 >= self.window_ms:
            merged = "".join(self._buffer)
            self._buffer.clear()
            self._last_flush = now
            return merged
        return None

    def flush(self) -> Optional[str]:
        if self._buffer:
            merged = "".join(self._buffer)
            self._buffer.clear()
            self._last_flush = time.monotonic()
            return merged
        return None
