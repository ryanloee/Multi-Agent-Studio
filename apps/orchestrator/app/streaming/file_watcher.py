import asyncio
import logging

from app.config import settings
from app.streaming.publisher import StreamPublisher
from app.streaming.throttler import StreamThrottler

logger = logging.getLogger(__name__)


class LogLimitExceeded(Exception):
    """Raised when stream.jsonl exceeds the configured size limit (Log Bomb defense)."""
    pass


class FileWatcher:
    """Watches stream.jsonl inside a sandbox container via mounted volume.

    Features:
    - Real-time JSONL line-by-line parsing
    - Uses tail -n +offset to read only new lines (BUG-4 fix)
    - 50MB hard limit (Log Bomb defense)
    - 100ms throttling for shell_stdout events (BUG-5 fix)
    - Graceful stop on process exit
    """

    def __init__(
        self,
        sandbox_manager,
        sandbox_id: str,
        file_path: str,
        run_id: str,
        node_id: str,
        publisher: StreamPublisher,
    ):
        self.sandbox = sandbox_manager
        self.sandbox_id = sandbox_id
        self.file_path = file_path
        self.run_id = run_id
        self.node_id = node_id
        self.publisher = publisher
        self._running = False
        self._task: asyncio.Task | None = None
        self._throttler = StreamThrottler(window_ms=settings.stream_throttle_window_ms)
        self._line_offset = 1  # tail -n +N is 1-indexed

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._watch_loop())

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

        # Flush remaining throttled content
        remaining = self._throttler.flush()
        if remaining:
            await self._publish_shell_stdout(remaining)

    async def _watch_loop(self) -> None:
        """Poll the stream file using tail -n +offset, parse lines, publish events."""
        from app.streaming.parser import OpenCodeOutputParser

        parser = OpenCodeOutputParser()
        max_bytes = settings.opencode_log_max_bytes

        while self._running:
            try:
                # BUG-4 fix: Use tail -n +offset to read only new lines
                # instead of reading the entire file every iteration.
                stdout, stderr = await self.sandbox.exec(
                    self.sandbox_id,
                    f"tail -n +{self._line_offset} {self.file_path}",
                )

                if not stdout or not stdout.strip():
                    await asyncio.sleep(0.2)
                    continue

                # Log Bomb defense: check total file size periodically
                size_stdout, _ = await self.sandbox.exec(
                    self.sandbox_id,
                    f"stat -c %s {self.file_path} 2>/dev/null || echo 0",
                )
                try:
                    file_size = int(size_stdout.strip())
                except ValueError:
                    file_size = 0

                if file_size > max_bytes:
                    await self.publisher.publish({
                        "type": "error",
                        "content": f"Stream file exceeded {max_bytes // (1024*1024)}MB limit",
                        "node_id": self.node_id,
                        "run_id": self.run_id,
                    })
                    raise LogLimitExceeded(
                        f"Stream file exceeded {max_bytes // (1024*1024)}MB limit"
                    )

                lines = stdout.split("\n")
                # Count non-empty lines to advance offset
                new_line_count = 0

                for line in lines:
                    stripped = line.strip()
                    if not stripped:
                        # Still count empty lines to keep offset accurate
                        # (tail counts all lines including blank)
                        continue
                    new_line_count += 1

                    event = parser.parse(stripped, self.run_id, self.node_id)
                    if event:
                        if event.event_type == "shell_stdout":
                            # BUG-5 fix: shell_stdout events go through throttler
                            merged = self._throttler.add(event.content)
                            if merged is not None:
                                await self._publish_shell_stdout(merged)
                        else:
                            # Non-shell events published immediately
                            await self.publisher.publish(event.to_dict())

                # Advance line offset by total lines returned (including empty)
                # tail -n +offset returns lines starting at offset, 1-indexed
                self._line_offset += len(lines)
                # Subtract 1 because the last element from split might be trailing empty
                if lines and lines[-1] == "":
                    self._line_offset -= 1

                await asyncio.sleep(0.1)

            except LogLimitExceeded:
                raise
            except Exception:
                logger.warning(
                    "FileWatcher error for run=%s node=%s",
                    self.run_id,
                    self.node_id,
                    exc_info=True,
                )
                await asyncio.sleep(0.5)

    async def _publish_shell_stdout(self, content: str) -> None:
        """Publish a shell_stdout event with the given content."""
        from app.agents.base import StreamEvent

        event = StreamEvent(
            event_type="shell_stdout",
            content=content,
            node_id=self.node_id,
            run_id=self.run_id,
        )
        await self.publisher.publish(event.to_dict())
