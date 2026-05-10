"""Local sandbox: subprocess + host filesystem.

Path mapping:
    /workspace   -> {sandbox_root}/{workspace_id}/workspace
    /sandbox-meta -> {sandbox_root}/{workspace_id}/sandbox-meta
"""

from __future__ import annotations

import asyncio
import logging
import platform
import shutil
import subprocess
import tarfile
from pathlib import Path
from typing import AsyncIterator
from uuid import uuid4

from app.config import settings


class _AsyncProcessShim:
    """Wraps subprocess.Popen to provide the same interface as
    asyncio.subprocess.Process (returncode, terminate, kill, wait)."""

    def __init__(self, proc: subprocess.Popen) -> None:
        self._proc = proc

    @property
    def returncode(self) -> int | None:
        self._proc.poll()
        return self._proc.returncode

    def terminate(self) -> None:
        self._proc.terminate()

    def kill(self) -> None:
        self._proc.kill()

    async def wait(self) -> int:
        return await asyncio.to_thread(self._proc.wait)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ProcessInfo -- mirrors app.sandbox.manager.ProcessInfo
# ---------------------------------------------------------------------------

class ProcessInfo:
    """Status of a background process."""

    def __init__(self, running: bool, exit_code: int | None = None):
        self.running = running
        self.exit_code = exit_code


# ---------------------------------------------------------------------------
# Per-sandbox state
# ---------------------------------------------------------------------------

class _SandboxState:
    """Bookkeeping for one local sandbox."""

    def __init__(self, root: Path, workspace_id: str) -> None:
        self.root = root
        self.workspace_id = workspace_id
        self.workspace_dir = root / "workspace"
        self.meta_dir = root / "sandbox-meta"
        self.processes: dict[str, asyncio.subprocess.Process] = {}


# ---------------------------------------------------------------------------
# LocalSandbox
# ---------------------------------------------------------------------------

class LocalSandbox:
    """Sandbox using host filesystem + subprocess.

    Virtual paths /workspace and /sandbox-meta are rewritten to real host
    directories under settings.sandbox_root.
    """

    def __init__(self, sandbox_root: str | None = None) -> None:
        self.root = Path(sandbox_root or settings.sandbox_root).resolve()
        self._sandboxes: dict[str, _SandboxState] = {}
        logger.info("LocalSandbox initialized: root=%s", self.root)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _state(self, sandbox_id: str) -> _SandboxState:
        """Retrieve sandbox state or raise KeyError."""
        state = self._sandboxes.get(sandbox_id)
        if state is None:
            raise KeyError(f"Sandbox {sandbox_id} not found")
        return state

    @staticmethod
    def _to_posix(p: Path) -> str:
        """Convert a Path to a POSIX-style string safe for bash -c on Windows.

        On Windows this produces /c/foo/bar style paths that Git Bash natively
        understands.  On other platforms it returns the path unchanged.
        """
        s = str(p)
        if platform.system() != "Windows":
            return s
        # D:\foo\bar → /d/foo/bar  (Git Bash / MSYS2 convention)
        if len(s) >= 2 and s[1] == ":":
            drive = s[0].lower()
            rest = s[2:].replace("\\", "/")
            return f"/{drive}{rest}"
        return s.replace("\\", "/")

    def _rewrite_cmd(self, state: _SandboxState, cmd: str) -> str:
        """Replace virtual paths in *cmd* with real host paths.

        ``/workspace`` and ``/sandbox-meta`` are replaced with the actual
        directories so that git commands, shell redirects, etc. all work on
        the host filesystem.

        On Windows the real paths are converted to POSIX style (/c/foo/bar)
        so they work inside Git Bash without backslash/escaping issues.
        """
        # Replace longer prefix first to avoid partial matches.
        meta_posix = self._to_posix(state.meta_dir)
        work_posix = self._to_posix(state.workspace_dir)
        cmd = cmd.replace("/sandbox-meta", meta_posix)
        cmd = cmd.replace("/workspace", work_posix)
        return cmd

    def _map_path(self, state: _SandboxState, path: str) -> Path:
        """Map a virtual container path to a real host path."""
        if path.startswith("/workspace/"):
            return state.workspace_dir / path[len("/workspace/"):]
        if path.startswith("/workspace"):
            return state.workspace_dir
        if path.startswith("/sandbox-meta/"):
            return state.meta_dir / path[len("/sandbox-meta/"):]
        if path.startswith("/sandbox-meta"):
            return state.meta_dir
        # For any other absolute path, treat as relative to workspace
        return state.workspace_dir / path.lstrip("/")

    @staticmethod
    def _shell_prefix() -> list[str]:
        """Return the shell invocation appropriate for the current OS."""
        if platform.system() == "Windows":
            # Windows: use cmd.exe for basic commands, but prefer git-bash
            # if available.  Most commands here are POSIX-flavoured (mkdir -p,
            # cat, python3 -m ...), so Git Bash is the better choice when
            # present.
            git_bash = shutil.which("bash")
            if git_bash:
                return [git_bash, "-c"]
            # Fallback to cmd.exe -- note that some POSIX idioms will fail.
            return ["cmd", "/C"]
        return ["/bin/bash", "-c"]

    # ------------------------------------------------------------------
    # Container lifecycle
    # ------------------------------------------------------------------

    async def create(self, workspace_id: str, template: str = "base") -> str:
        """Create a local sandbox directory layout.

        Returns *workspace_id* as the sandbox identifier (matching the
        convention that callers treat the return value as an opaque sandbox
        handle).
        """
        sandbox_id = workspace_id
        root = self.root / workspace_id

        # Create directories on the host
        (root / "workspace").mkdir(parents=True, exist_ok=True)
        (root / "sandbox-meta" / ".git").mkdir(parents=True, exist_ok=True)

        state = _SandboxState(root, workspace_id)
        self._sandboxes[sandbox_id] = state

        logger.info(
            "Created local sandbox %s at %s (template=%s)",
            sandbox_id,
            root,
            template,
        )
        return sandbox_id

    async def destroy(self, sandbox_id: str) -> None:
        """Remove sandbox directories and kill any remaining processes."""
        state = self._sandboxes.pop(sandbox_id, None)
        if state is None:
            logger.warning("Sandbox %s not found, skipping destroy", sandbox_id)
            return

        # Terminate lingering processes
        for exec_id, proc in list(state.processes.items()):
            if proc.returncode is None:
                try:
                    proc.terminate()
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
        state.processes.clear()

        # Remove directories
        try:
            shutil.rmtree(state.root, ignore_errors=True)
            logger.info("Destroyed local sandbox %s", sandbox_id)
        except Exception as exc:
            logger.warning("Failed to remove sandbox dir %s: %s", state.root, exc)

    # ------------------------------------------------------------------
    # Command execution
    # ------------------------------------------------------------------

    async def exec(
        self, sandbox_id: str, cmd: str, *, env: dict[str, str] | None = None,
    ) -> tuple[str, str]:
        """Execute *cmd* synchronously and return (stdout, stderr).

        Uses subprocess.run in a thread for Windows compatibility.
        """
        state = self._state(sandbox_id)
        rewritten = self._rewrite_cmd(state, cmd)
        shell = self._shell_prefix()

        logger.debug("exec [%s]: %s", sandbox_id, rewritten[:120])

        def _run():
            return subprocess.run(
                [*shell, rewritten],
                cwd=str(state.workspace_dir),
                capture_output=True,
                env=env,
            )

        result = await asyncio.to_thread(_run)

        stdout = result.stdout.decode("utf-8", errors="replace")
        stderr = result.stderr.decode("utf-8", errors="replace")

        if result.returncode != 0:
            logger.debug(
                "exec exit_code=%d cmd=%s stderr=%s",
                result.returncode,
                cmd[:80],
                stderr[:200],
            )

        return stdout, stderr

    async def exec_async(
        self, sandbox_id: str, cmd: str, *, env: dict[str, str] | None = None,
    ) -> str:
        """Start *cmd* in the background and return an execution ID.

        Uses subprocess.Popen in a thread for Windows compatibility (uvicorn's
        reloader may switch to SelectorEventLoop which doesn't support
        asyncio.create_subprocess_exec).
        """
        state = self._state(sandbox_id)
        rewritten = self._rewrite_cmd(state, cmd)
        shell = self._shell_prefix()

        exec_id = uuid4().hex

        def _start_proc():
            import subprocess as sp
            return sp.Popen(
                [*shell, rewritten],
                cwd=str(state.workspace_dir),
                stdout=sp.PIPE,
                stderr=sp.PIPE,
                env=env,
            )

        proc = await asyncio.to_thread(_start_proc)

        # Wrap in a shim that provides the same interface as asyncio.subprocess.Process
        shim = _AsyncProcessShim(proc)
        state.processes[exec_id] = shim
        logger.debug("Started async exec %s: %s", exec_id[:12], cmd[:80])
        return exec_id

    async def exec_stream(self, sandbox_id: str, cmd: str) -> AsyncIterator[str]:
        """Stream command stdout line by line."""
        state = self._state(sandbox_id)
        rewritten = self._rewrite_cmd(state, cmd)
        shell = self._shell_prefix()

        proc = await asyncio.create_subprocess_exec(
            *shell,
            rewritten,
            cwd=str(state.workspace_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )

        # Read stdout line-by-line
        if proc.stdout is None:
            return

        while True:
            line_bytes = await proc.stdout.readline()
            if not line_bytes:
                break
            yield line_bytes.decode("utf-8", errors="replace")

        await proc.wait()

    # ------------------------------------------------------------------
    # Process management
    # ------------------------------------------------------------------

    async def wait_process(self, exec_id: str) -> int:
        """Wait for a background process to finish, return exit code."""
        while True:
            info = await self.get_process(exec_id)
            if not info.running:
                return info.exit_code if info.exit_code is not None else -1
            await asyncio.sleep(0.2)

    async def get_process(self, exec_id: str) -> ProcessInfo:
        """Get status of a background process by execution ID."""
        for state in self._sandboxes.values():
            proc = state.processes.get(exec_id)
            if proc is not None:
                returncode = proc.returncode
                running = returncode is None
                return ProcessInfo(running=running, exit_code=returncode)
        # Not found in any sandbox -- treat as already finished
        return ProcessInfo(running=False, exit_code=-1)

    def _find_process(self, exec_id: str) -> "_AsyncProcessShim | None":
        """Find the raw process shim by exec_id (synchronous)."""
        for state in self._sandboxes.values():
            proc = state.processes.get(exec_id)
            if proc is not None:
                return proc
        return None

    # ------------------------------------------------------------------
    # File I/O
    # ------------------------------------------------------------------

    async def write_file(self, sandbox_id: str, path: str, content: str) -> None:
        """Write *content* to a virtual path inside the sandbox."""
        state = self._state(sandbox_id)
        host_path = self._map_path(state, path)
        host_path.parent.mkdir(parents=True, exist_ok=True)
        host_path.write_text(content, encoding="utf-8")
        logger.debug(
            "Wrote %d bytes to %s:%s", len(content), sandbox_id, path,
        )

    async def read_file(self, sandbox_id: str, path: str) -> str:
        """Read file from a virtual path inside the sandbox."""
        state = self._state(sandbox_id)
        host_path = self._map_path(state, path)
        try:
            return host_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""

    # ------------------------------------------------------------------
    # Snapshot
    # ------------------------------------------------------------------

    async def snapshot(self, sandbox_id: str) -> str:
        """Create a tar.gz snapshot of the sandbox workspace.

        Returns the absolute path to the created archive.
        """
        state = self._state(sandbox_id)
        snapshot_dir = state.root / ".snapshots"
        snapshot_dir.mkdir(parents=True, exist_ok=True)

        archive_name = f"snapshot-{uuid4().hex[:12]}.tar.gz"
        archive_path = snapshot_dir / archive_name

        await asyncio.to_thread(
            _make_tar_gz,
            str(state.workspace_dir),
            str(archive_path),
        )

        logger.info(
            "Snapshot sandbox %s -> %s", sandbox_id, archive_path,
        )
        return str(archive_path)


# ---------------------------------------------------------------------------
# Utility (runs in thread via asyncio.to_thread)
# ---------------------------------------------------------------------------

def _make_tar_gz(source_dir: str, output_path: str) -> None:
    """Create a tar.gz archive of *source_dir* at *output_path*."""
    source = Path(source_dir)
    with tarfile.open(output_path, "w:gz") as tar:
        for file in sorted(source.rglob("*")):
            tar.add(str(file), arcname=file.relative_to(source.parent))
