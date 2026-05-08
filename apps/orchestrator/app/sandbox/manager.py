import asyncio
import io
import logging
import tarfile

import docker
from docker.errors import NotFound, APIError

from typing import AsyncIterator

logger = logging.getLogger(__name__)


class ProcessInfo:
    def __init__(self, running: bool, exit_code: int | None = None):
        self.running = running
        self.exit_code = exit_code


class SandboxManager:
    """Manages Docker sandbox containers. Each workflow run gets an isolated sandbox."""

    def __init__(self, docker_url: str, base_image: str):
        self.docker_url = docker_url
        self.base_image = base_image
        self.client = docker.DockerClient(base_url=docker_url)
        logger.info("SandboxManager initialized: url=%s, image=%s", docker_url, base_image)

    # ------------------------------------------------------------------
    # Container lifecycle
    # ------------------------------------------------------------------

    async def create(self, workspace_id: str, template: str = "base") -> str:
        """Create sandbox container with OpenCode pre-installed and Git repo initialized.

        Directory layout:
          /workspace       - Agent work tree (no .git, Agent cannot destroy checkpoints)
          /sandbox-meta/.git - Git metadata (only Python control layer operates this)
        """
        def _create() -> str:
            # Create named volumes for workspace and metadata persistence
            ws_volume_name = f"{workspace_id}-workspace"
            meta_volume_name = f"{workspace_id}-meta"

            self.client.volumes.create(ws_volume_name)
            self.client.volumes.create(meta_volume_name)

            container = self.client.containers.run(
                image=self.base_image,
                detach=True,
                environment={"TERM": "dumb"},
                volumes={
                    ws_volume_name: {"bind": "/workspace", "mode": "rw"},
                    meta_volume_name: {"bind": "/sandbox-meta", "mode": "rw"},
                },
                working_dir="/workspace",
                stdin_open=True,
                tty=False,
                labels={
                    "mas.workspace_id": workspace_id,
                    "mas.template": template,
                },
            )
            logger.info("Created sandbox container %s for workspace %s", container.id[:12], workspace_id)
            return container.id

        return await asyncio.to_thread(_create)

    async def destroy(self, container_id: str) -> None:
        """Stop and remove container."""
        def _destroy() -> None:
            try:
                container = self.client.containers.get(container_id)
            except NotFound:
                logger.warning("Container %s not found, skipping destroy", container_id[:12])
                return

            try:
                container.stop(timeout=5)
            except APIError:
                logger.warning("Failed to stop container %s gracefully", container_id[:12])

            container.remove(force=True)
            logger.info("Destroyed container %s", container_id[:12])

        await asyncio.to_thread(_destroy)

    # ------------------------------------------------------------------
    # Command execution
    # ------------------------------------------------------------------

    async def exec(self, container_id: str, cmd: str) -> tuple[str, str]:
        """Execute command in container, return (stdout, stderr)."""
        def _exec() -> tuple[str, str]:
            container = self.client.containers.get(container_id)
            exit_code, output = container.exec_run(
                cmd,
                workdir="/workspace",
                demux=True,
            )
            stdout = (output[0] or b"").decode("utf-8", errors="replace")
            stderr = (output[1] or b"").decode("utf-8", errors="replace")

            if exit_code != 0:
                logger.debug(
                    "exec exit_code=%d cmd=%s stderr=%s",
                    exit_code,
                    cmd[:80],
                    stderr[:200],
                )

            return stdout, stderr

        return await asyncio.to_thread(_exec)

    async def exec_async(self, container_id: str, cmd: str) -> str:
        """Start command in background, return execution ID.

        Wraps the command in ``bash -c`` so that shell features like
        redirects (``>``, ``2>&1``), pipes, and ``&&`` are interpreted
        correctly.  The Docker API's ``exec_create`` does not invoke a
        shell, so without this wrapper shell operators would be passed as
        literal arguments.
        """
        def _exec_async() -> str:
            container = self.client.containers.get(container_id)
            exec_instance = self.client.api.exec_create(
                container.id,
                ["/bin/bash", "-c", cmd],
                workdir="/workspace",
            )
            self.client.api.exec_start(exec_instance["Id"], detach=True)
            logger.debug("Started async exec %s: %s", exec_instance["Id"][:12], cmd[:80])
            return exec_instance["Id"]

        return await asyncio.to_thread(_exec_async)

    async def exec_stream(self, container_id: str, cmd: str) -> AsyncIterator[str]:
        """Stream command stdout line by line."""
        def _exec_stream():
            container = self.client.containers.get(container_id)
            exit_code, output_stream = container.exec_run(
                cmd,
                workdir="/workspace",
                stream=True,
            )
            return output_stream

        output_stream = await asyncio.to_thread(_exec_stream)

        loop = asyncio.get_event_loop()

        def _read_line():
            try:
                for chunk in output_stream:
                    if chunk:
                        return chunk.decode("utf-8", errors="replace")
                return None
            except StopIteration:
                return None

        while True:
            line = await loop.run_in_executor(None, _read_line)
            if line is None:
                break
            yield line

    # ------------------------------------------------------------------
    # Process management
    # ------------------------------------------------------------------

    async def wait_process(self, exec_id: str) -> int:
        """Wait for background process to finish, return exit code."""
        while True:
            info = await self.get_process(exec_id)
            if not info.running:
                return info.exit_code if info.exit_code is not None else -1
            await asyncio.sleep(0.2)

    async def get_process(self, exec_id: str) -> ProcessInfo:
        """Get process status."""
        def _inspect() -> ProcessInfo:
            info = self.client.api.exec_inspect(exec_id)
            running = info.get("Running", False)
            exit_code = info.get("ExitCode")
            return ProcessInfo(running=running, exit_code=exit_code)

        return await asyncio.to_thread(_inspect)

    # ------------------------------------------------------------------
    # File I/O
    # ------------------------------------------------------------------

    async def write_file(self, container_id: str, path: str, content: str) -> None:
        """Write file into container."""
        def _write_file() -> None:
            container = self.client.containers.get(container_id)

            # Build a tar archive in memory containing the target file
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w") as tar:
                encoded = content.encode("utf-8")
                info = tarfile.TarInfo(name=path.lstrip("/"))
                info.size = len(encoded)
                tar.addfile(info, io.BytesIO(encoded))
            buf.seek(0)

            # Determine the directory to extract into
            # put_archive extracts into the container's root,
            # so the TarInfo name should be a relative or absolute path.
            put_dir = "/"
            ok = container.put_archive(put_dir, buf)
            if not ok:
                raise RuntimeError(f"put_archive returned False for {container_id[:12]}:{path}")
            logger.debug("Wrote %d bytes to %s:%s", len(encoded), container_id[:12], path)

        await asyncio.to_thread(_write_file)

    async def read_file(self, container_id: str, path: str) -> str:
        """Read file from container."""
        stdout, stderr = await self.exec(container_id, f"cat {path}")
        return stdout

    # ------------------------------------------------------------------
    # Snapshot
    # ------------------------------------------------------------------

    async def snapshot(self, container_id: str) -> str:
        """Create Docker image snapshot of container."""
        def _snapshot() -> str:
            container = self.client.containers.get(container_id)
            image = container.commit()
            image_id = image.id
            logger.info("Snapshot container %s -> image %s", container_id[:12], image_id[:12])
            return image_id

        return await asyncio.to_thread(_snapshot)
