"""Local sandbox: subprocess + host filesystem.

Path mapping:
    /workspace                    -> {sandbox_root}/{workspace_id}/workspace
    /workspace/.mas/containers/{id} -> 节点容器（独立工作目录）
    /sandbox-meta                 -> {sandbox_root}/{workspace_id}/sandbox-meta

Each node gets an isolated container under .mas/containers/{node_id}/ with its own
.agent/ directory for runtime state. After node completion, changes are committed
and merged back to the main workspace via git.
"""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import shlex
import shutil
import subprocess
import tarfile
import time
from pathlib import Path
from typing import AsyncIterator
from uuid import uuid4

from app.config import settings

# Debounce sync_back to avoid redundant syncs when multiple nodes complete quickly
_last_sync_back: dict[str, float] = {}
_SYNC_BACK_DEBOUNCE = 2.0  # seconds


class _AsyncProcessShim:
    """Wraps subprocess.Popen to provide the same interface as
    asyncio.subprocess.Process (returncode, terminate, kill, wait)."""

    def __init__(
        self,
        proc: subprocess.Popen,
        stdout_buf: list[bytes] | None = None,
        stderr_buf: list[bytes] | None = None,
    ) -> None:
        self._proc = proc
        self.stdout_buf: list[bytes] = stdout_buf or []
        self.stderr_buf: list[bytes] = stderr_buf or []

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

    def get_stdout(self) -> str:
        return b"".join(self.stdout_buf).decode("utf-8", errors="replace")

    def get_stderr(self) -> str:
        return b"".join(self.stderr_buf).decode("utf-8", errors="replace")

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

    def __init__(self, root: Path, workspace_id: str,
                 user_workspace: Path | None = None) -> None:
        self.root = root
        self.workspace_id = workspace_id
        self.workspace_dir = root / "workspace"
        self.meta_dir = root / "sandbox-meta"
        self.user_workspace = user_workspace
        if user_workspace is not None:
            self.containers_dir = user_workspace / ".mas" / "containers"
        else:
            self.containers_dir = root / "workspace" / ".mas" / "containers"
        self.processes: dict[str, asyncio.subprocess.Process] = {}

    def node_container(self, node_id: str) -> Path:
        """Return the container path for a specific node."""
        return self.containers_dir / node_id

    def node_agent_dir(self, node_id: str) -> Path:
        """Return the .agent directory for a specific node."""
        return self.containers_dir / node_id / ".agent"

    def node_stream_file(self, node_id: str) -> Path:
        """Return the stream.jsonl path for a specific node."""
        return self.containers_dir / node_id / ".agent" / "stream.jsonl"

    def node_runner_port(self, node_id: str) -> Path:
        """Return the runner.port path for a specific node."""
        return self.containers_dir / node_id / ".agent" / "runner.port"


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

    @staticmethod
    def _workspace_copy_ignore(_src: str, names: list[str]) -> set[str]:
        """Skip runtime state directories when copying workspaces."""
        ignored = {".mas", ".agent", ".workflow"}
        return {name for name in names if name in ignored}

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

        Uses word-boundary-aware replacement so that only standalone
        ``/workspace`` and ``/sandbox-meta`` tokens are replaced, not
        substrings inside other paths (e.g. ``D:\\...\\workspace``).

        On Windows the real paths are converted to POSIX style (/c/foo/bar)
        so they work inside Git Bash without backslash/escaping issues.
        """
        import re
        meta_posix = self._to_posix(state.meta_dir)
        work_posix = self._to_posix(state.workspace_dir)
        # Replace /sandbox-meta first (longer prefix), then /workspace.
        # Only match when followed by end-of-string, /, or whitespace —
        # this avoids corrupting paths like D:\...\workspace\...
        cmd = re.sub(r"/sandbox-meta(?=/|\s|$)", meta_posix, cmd)
        cmd = re.sub(r"/workspace(?=/|\s|$)", work_posix, cmd)
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

    def resolve_virtual_path(self, sandbox_id: str, virtual_path: str) -> str:
        """Map a virtual path to a **native** host path string.

        Unlike ``_map_path`` which returns a ``Path``, this returns a string
        using the platform's native separator.  This is intended for passing
        to tools like ``bun`` that run natively on Windows and do not
        understand Git Bash / POSIX paths (``/d/foo/bar``).
        """
        state = self._state(sandbox_id)
        host = self._map_path(state, virtual_path)
        return str(host)

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

    async def create(
        self,
        workspace_id: str,
        template: str = "base",
        template_dir: str | None = None,
        storage_root: str | Path | None = None,
        user_workspace: str | None = None,
    ) -> str:
        """Create a local sandbox directory layout.

        If *template_dir* is provided and the directory exists on the host,
        its contents are copied into the sandbox workspace (overwriting any
        existing files).  A git repository is then initialised with an initial
        commit so that ``sync_back`` can later compute a diff.

        If *user_workspace* is provided, a directory junction (Windows) or
        symlink (Linux/Mac) is created from the sandbox's ``.mas`` directory
        to ``{user_workspace}/.mas`` so that runtime state persists in the
        user's actual workspace.

        Returns *workspace_id* as the sandbox identifier (matching the
        convention that callers treat the return value as an opaque sandbox
        handle).
        """
        sandbox_id = workspace_id
        root_base = Path(storage_root).resolve() if storage_root else self.root
        root = root_base / workspace_id

        # Create directories on the host
        (root / "workspace").mkdir(parents=True, exist_ok=True)
        (root / "sandbox-meta" / ".git").mkdir(parents=True, exist_ok=True)

        # If a template directory is provided, copy its contents into the sandbox
        if template_dir is not None and Path(template_dir).is_dir():
            logger.info("Copying template dir %s into sandbox %s", template_dir, sandbox_id)
            await asyncio.to_thread(
                shutil.copytree,
                template_dir,
                str(root / "workspace"),
                dirs_exist_ok=True,
                ignore=self._workspace_copy_ignore,
            )
            # Initialise git and create an initial commit so we can diff later
            await self._git_init_with_commit(root / "workspace")

        # Create .mas junction to user workspace (if requested)
        resolved_user_ws: Path | None = None
        if user_workspace:
            resolved_user_ws = Path(user_workspace).resolve()
            user_mas = resolved_user_ws / ".mas"
            sandbox_mas = root / "workspace" / ".mas"
            try:
                user_mas.mkdir(parents=True, exist_ok=True)
                if not sandbox_mas.exists():
                    _create_dir_link(sandbox_mas, user_mas)
                logger.info("Linked sandbox .mas -> %s", user_mas)
            except Exception:
                logger.warning("Failed to create .mas junction, using sandbox-local .mas",
                               exc_info=True)
                resolved_user_ws = None

        state = _SandboxState(root, workspace_id, user_workspace=resolved_user_ws)
        self._sandboxes[sandbox_id] = state

        logger.info(
            "Created local sandbox %s at %s (template=%s, template_dir=%s, user_ws=%s)",
            sandbox_id,
            root,
            template,
            template_dir,
            user_workspace,
        )
        return sandbox_id

    async def clone(self, source_sandbox_id: str, new_workspace_id: str) -> str:
        """Clone an existing sandbox into a new one with a copy of its workspace.

        Creates a new sandbox with *new_workspace_id*, copies the entire workspace
        directory from the source sandbox (including the git meta directory), and
        registers the new sandbox state.  Returns the new sandbox_id.
        """
        source_state = self._state(source_sandbox_id)
        sandbox_id = new_workspace_id
        # Keep clones next to their source sandbox. This matters for workflow
        # runs whose live workspaces are stored under .mas/runs/<run>/sandboxes.
        root = source_state.root.parent / new_workspace_id

        # Create base directories for the new sandbox
        (root / "workspace").mkdir(parents=True, exist_ok=True)
        (root / "sandbox-meta").mkdir(parents=True, exist_ok=True)

        # Copy workspace directory from source sandbox
        await asyncio.to_thread(
            shutil.copytree,
            str(source_state.workspace_dir),
            str(root / "workspace"),
            dirs_exist_ok=True,
            ignore=self._workspace_copy_ignore,
        )

        # Preserve .agent/runner.port (needed for SSE runner communication)
        runner_port_src = source_state.workspace_dir / ".agent" / "runner.port"
        runner_port_dst = root / "workspace" / ".agent" / "runner.port"
        if runner_port_src.exists():
            runner_port_dst.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(shutil.copy2, str(runner_port_src), str(runner_port_dst))

        # Copy sandbox-meta directory (preserves git history / checkpoints)
        meta_src = source_state.root / "sandbox-meta"
        if meta_src.is_dir():
            await asyncio.to_thread(
                shutil.copytree,
                str(meta_src),
                str(root / "sandbox-meta"),
                dirs_exist_ok=True,
            )

        # Rebuild .mas junction if source had one
        resolved_user_ws: Path | None = None
        if source_state.user_workspace is not None:
            user_mas = source_state.user_workspace / ".mas"
            sandbox_mas = root / "workspace" / ".mas"
            try:
                user_mas.mkdir(parents=True, exist_ok=True)
                if not sandbox_mas.exists():
                    _create_dir_link(sandbox_mas, user_mas)
                resolved_user_ws = source_state.user_workspace
            except Exception:
                logger.warning("Failed to rebuild .mas junction in clone", exc_info=True)

        # Register the new sandbox state
        state = _SandboxState(root, new_workspace_id, user_workspace=resolved_user_ws)
        self._sandboxes[sandbox_id] = state

        logger.info(
            "Cloned sandbox %s -> %s", source_sandbox_id, sandbox_id,
        )
        return sandbox_id

    def get_workspace_path(self, sandbox_id: str) -> Path:
        """Return the real host workspace path for a sandbox."""
        return self._state(sandbox_id).workspace_dir

    async def _git_init_with_commit(self, workspace_path: Path) -> None:
        """Initialise a git repo in *workspace_path* and commit all contents.

        Uses subprocess directly — no shell invocation.
        """
        def _run_git(*args: str) -> subprocess.CompletedProcess:
            return subprocess.run(
                ["git", *args],
                cwd=str(workspace_path),
                capture_output=True,
                text=True,
            )

        await asyncio.to_thread(_run_git, "init")
        await asyncio.to_thread(_run_git, "add", "-A")
        await asyncio.to_thread(
            _run_git, "commit", "-m", "initial snapshot from template", "--allow-empty",
        )

    async def sync_back(self, sandbox_id: str, target_dir: str) -> bool:
        """Sync sandbox workspace changes back to *target_dir*.

        Strategy: compare files by mtime/size and copy changed/new files directly.
        Skips MAS-internal directories (.mas, .agent, .workflow, .git, sandbox-meta).
        Falls back to full copy if comparison fails.
        """
        state = self._state(sandbox_id)
        workspace = state.workspace_dir

        # Debounce: skip if synced recently
        now = time.monotonic()
        last = _last_sync_back.get(sandbox_id, 0)
        if now - last < _SYNC_BACK_DEBOUNCE:
            logger.debug("sync_back skipped for %s (debounced)", sandbox_id)
            return True

        if not workspace.exists():
            raise RuntimeError(
                f"Cannot sync_back: sandbox workspace does not exist: {workspace}"
            )

        target = Path(target_dir)
        target.mkdir(parents=True, exist_ok=True)

        try:
            copied, skipped = await asyncio.to_thread(
                _sync_changed_files, workspace, target,
            )
            _last_sync_back[sandbox_id] = time.monotonic()
            logger.info(
                "sync_back: %s -> %s: %d files copied, %d skipped",
                sandbox_id[:12], target_dir, copied, skipped,
            )
            return True
        except Exception as exc:
            logger.error("sync_back failed for %s -> %s: %s", sandbox_id[:12], target_dir, exc)
            return False

    async def destroy(self, sandbox_id: str) -> None:
        """Remove sandbox directories and kill any remaining processes."""
        _last_sync_back.pop(sandbox_id, None)
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

        # Remove .mas junction first (so rmtree doesn't fail on the junction entry)
        mas_link = state.workspace_dir / ".mas"
        if mas_link.exists():
            try:
                _remove_dir_link(mas_link)
            except Exception:
                logger.debug("Could not remove .mas junction at %s", mas_link, exc_info=True)

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
            import threading

            proc = sp.Popen(
                [*shell, rewritten],
                cwd=str(state.workspace_dir),
                stdout=sp.PIPE,
                stderr=sp.PIPE,
                env=env,
            )
            # Drain stdout/stderr to prevent pipe buffer deadlock (64KB limit)
            stdout_buf: list[bytes] = []
            stderr_buf: list[bytes] = []

            def _drain(stream, buf: list[bytes]) -> None:
                for line in iter(stream.readline, b""):
                    buf.append(line)

            threading.Thread(
                target=_drain, args=(proc.stdout, stdout_buf), daemon=True,
            ).start()
            threading.Thread(
                target=_drain, args=(proc.stderr, stderr_buf), daemon=True,
            ).start()
            return proc, stdout_buf, stderr_buf

        proc, stdout_buf, stderr_buf = await asyncio.to_thread(_start_proc)

        # Wrap in a shim that provides the same interface as asyncio.subprocess.Process
        shim = _AsyncProcessShim(proc, stdout_buf, stderr_buf)
        state.processes[exec_id] = shim
        logger.debug("Started async exec %s: %s", exec_id[:12], cmd[:80])
        return exec_id

    async def launch_native_process(
        self,
        sandbox_id: str,
        args: list[str],
        *,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
    ) -> str:
        """Launch a process directly (no shell wrapping) and return exec_id.

        Unlike ``exec_async`` which wraps commands in ``bash -c``, this launches
        the process with the exact ``args`` list via ``subprocess.Popen``.
        This is essential for tools like ``bun`` on Windows where the
        ``bash -c`` intermediary corrupts file paths and causes silent failures.
        """
        state = self._state(sandbox_id)
        exec_id = uuid4().hex
        work_dir = cwd or str(state.workspace_dir)

        def _start_proc():
            import subprocess as sp
            import threading

            proc = sp.Popen(
                args,
                cwd=work_dir,
                stdout=sp.PIPE,
                stderr=sp.PIPE,
                env=env,
            )
            stdout_buf: list[bytes] = []
            stderr_buf: list[bytes] = []

            def _drain(stream, buf: list[bytes]) -> None:
                for line in iter(stream.readline, b""):
                    buf.append(line)

            threading.Thread(
                target=_drain, args=(proc.stdout, stdout_buf), daemon=True,
            ).start()
            threading.Thread(
                target=_drain, args=(proc.stderr, stderr_buf), daemon=True,
            ).start()
            return proc, stdout_buf, stderr_buf

        proc, stdout_buf, stderr_buf = await asyncio.to_thread(_start_proc)
        shim = _AsyncProcessShim(proc, stdout_buf, stderr_buf)
        state.processes[exec_id] = shim
        logger.debug("Started native process %s: %s", exec_id[:12], args[0] if args else "")
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


def _sync_changed_files(source: Path, target: Path) -> tuple[int, int]:
    """Copy changed/new files from sandbox workspace to target directory.

    Skips MAS-internal directories. Only copies files that are new or modified
    (by size + mtime comparison).

    Returns (copied_count, skipped_count).
    """
    skip_dirs = {".git", ".agent", ".workflow", ".mas", "sandbox-meta", "node_modules"}
    copied = 0
    skipped = 0

    for root, dirs, files in os.walk(source):
        # Skip internal directories
        dirs[:] = [d for d in dirs if d not in skip_dirs]

        rel_root = Path(root).relative_to(source)
        target_root = target / rel_root

        for fname in files:
            src_file = Path(root) / fname
            dst_file = target_root / fname

            try:
                src_stat = src_file.stat()
                if dst_file.exists():
                    dst_stat = dst_file.stat()
                    # Skip if same size and not newer
                    if src_stat.st_size == dst_stat.st_size and src_stat.st_mtime <= dst_stat.st_mtime:
                        skipped += 1
                        continue
                dst_file.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(src_file), str(dst_file))
                copied += 1
            except OSError:
                skipped += 1

    return copied, skipped


# ---------------------------------------------------------------------------
# Directory junction / symlink helpers
# ---------------------------------------------------------------------------

def _create_dir_link(src: Path, dst: Path) -> None:
    """Create a directory junction (Windows) or symlink (Linux/Mac) from *src* to *dst*.

    *src* is the link path, *dst* is the target directory.
    """
    if platform.system() == "Windows":
        subprocess.run(
            ["cmd", "/C", "mklink", "/J", str(src), str(dst)],
            check=True, capture_output=True,
        )
    else:
        os.symlink(str(dst), str(src), target_is_directory=True)


def _remove_dir_link(link: Path) -> None:
    """Remove a directory junction or symlink without deleting the target.

    On Windows, ``os.rmdir`` removes the junction entry but leaves the target
    directory intact.  On Linux/Mac, ``Path.unlink`` does the same for symlinks.
    """
    if platform.system() == "Windows":
        os.rmdir(str(link))
    else:
        link.unlink()


# ---------------------------------------------------------------------------
# SandboxRegistry -- reference-counted sandbox manager
# ---------------------------------------------------------------------------

class SandboxRegistry:
    """Reference-counted sandbox manager.

    Replaces the three-mechanism system (_owns_sandbox, destroy_owned_sandbox,
    retained_sandboxes) with a single reference-counted approach.
    """

    def __init__(self, sandbox: LocalSandbox) -> None:
        self._sandbox = sandbox
        self._refcounts: dict[str, int] = {}
        self._owners: dict[str, set[str]] = {}

    def acquire(self, node_id: str, upstream_sandbox_id: str | None = None) -> str:
        """Acquire a sandbox for a node.

        If *upstream_sandbox_id* is provided, increment its refcount.
        Returns the sandbox_id to use (empty string means caller should create
        a new one and call ``register_new``).
        """
        if upstream_sandbox_id:
            self._refcounts[upstream_sandbox_id] = self._refcounts.get(upstream_sandbox_id, 0) + 1
            self._owners.setdefault(upstream_sandbox_id, set()).add(node_id)
            return upstream_sandbox_id
        return ""

    def register_new(self, sandbox_id: str, node_id: str) -> None:
        """Register a newly created sandbox."""
        self._refcounts[sandbox_id] = 1
        self._owners.setdefault(sandbox_id, set()).add(node_id)

    def release(self, sandbox_id: str, node_id: str) -> None:
        """Release a sandbox. Decrement refcount; caller should destroy when
        refcount reaches 0."""
        if sandbox_id not in self._refcounts:
            return
        self._refcounts[sandbox_id] -= 1
        self._owners[sandbox_id].discard(node_id)
        if self._refcounts[sandbox_id] <= 0:
            del self._refcounts[sandbox_id]
            del self._owners[sandbox_id]

    def get_active_sandboxes(self) -> set[str]:
        """Return set of sandboxes still in use."""
        return set(self._refcounts.keys())
