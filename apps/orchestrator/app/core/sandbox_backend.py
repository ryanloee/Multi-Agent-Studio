"""
多后端沙盒管理器 - 支持多种隔离方案
支持: local (默认), bubblewrap, docker
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import shutil
import subprocess
import tempfile
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, AsyncIterator, Optional

logger = logging.getLogger(__name__)


class SandboxBackend(str, Enum):
    """支持的沙盒后端"""
    LOCAL = "local"  # 当前方案,文件系统隔离
    BUBBLEWRAP = "bubblewrap"  # Linux 轻量容器
    DOCKER = "docker"  # Docker 容器


@dataclass
class SandboxConfig:
    """沙盒配置"""
    backend: SandboxBackend = SandboxBackend.LOCAL
    sandbox_root: Optional[Path] = None
    network_enabled: bool = False  # 默认禁止网络
    memory_limit_mb: int = 4096
    cpu_limit: float = 1.0
    enable_network: bool = False


@dataclass
class ProcessInfo:
    """进程信息"""
    running: bool
    exit_code: int | None = None


class BaseSandbox(ABC):
    """沙盒抽象基类"""
    
    @abstractmethod
    async def create(
        self,
        workspace_id: str,
        template_dir: str | Path | None = None,
    ) -> str:
        """创建沙盒, 返回 sandbox_id"""
        pass
    
    @abstractmethod
    async def clone(self, source_sandbox_id: str, new_workspace_id: str) -> str:
        """克隆沙盒"""
        pass
    
    @abstractmethod
    async def destroy(self, sandbox_id: str) -> None:
        """销毁沙盒"""
        pass
    
    @abstractmethod
    async def exec(
        self, sandbox_id: str, cmd: str, *, env: dict[str, str] | None = None
    ) -> tuple[str, str]:
        """同步执行命令"""
        pass
    
    @abstractmethod
    async def exec_async(
        self, sandbox_id: str, cmd: str, *, env: dict[str, str] | None = None
    ) -> str:
        """异步执行命令,返回 exec_id"""
        pass
    
    @abstractmethod
    async def wait_process(self, exec_id: str) -> int:
        """等待进程结束"""
        pass
    
    @abstractmethod
    async def get_process(self, exec_id: str) -> ProcessInfo:
        """获取进程状态"""
        pass
    
    @abstractmethod
    async def write_file(self, sandbox_id: str, path: str, content: str) -> None:
        """写入文件"""
        pass
    
    @abstractmethod
    async def read_file(self, sandbox_id: str, path: str) -> str:
        """读取文件"""
        pass
    
    @abstractmethod
    def resolve_virtual_path(self, sandbox_id: str, virtual_path: str) -> str:
        """解析虚拟路径到主机路径"""
        pass
    
    @abstractmethod
    async def sync_back(self, sandbox_id: str, target_dir: str | Path) -> bool:
        """同步沙盒更改回目标目录"""
        pass


class LocalSandboxV2(BaseSandbox):
    """
    改进版本地文件系统沙盒
    相比原版:
    - 更强的目录隔离
    - 更好的快照管理
    - 进程跟踪
    """
    
    def __init__(self, config: SandboxConfig):
        self.config = config
        self.root = config.sandbox_root or Path("./.sandboxes")
        self.root.mkdir(parents=True, exist_ok=True)
        self._sandboxes: dict[str, _SandboxStateV2] = {}
        self._processes: dict[str, _AsyncProcessShim] = {}
        logger.info("LocalSandboxV2 initialized: root=%s", self.root)
    
    async def create(
        self,
        workspace_id: str,
        template_dir: str | Path | None = None,
    ) -> str:
        sandbox_id = workspace_id
        sandbox_path = self.root / workspace_id
        
        # 创建目录结构
        (sandbox_path / "workspace").mkdir(parents=True, exist_ok=True)
        (sandbox_path / "sandbox-meta").mkdir(parents=True, exist_ok=True)
        
        # 复制模板
        if template_dir and Path(template_dir).exists():
            await asyncio.to_thread(
                shutil.copytree,
                str(template_dir),
                str(sandbox_path / "workspace"),
                dirs_exist_ok=True,
                ignore=lambda _, names: {n for n in names if n in (".git", ".agent", ".workflow")},
            )
        
        self._sandboxes[sandbox_id] = _SandboxStateV2(
            root=sandbox_path, workspace_id=workspace_id)
        logger.info("Created sandbox %s", sandbox_id)
        return sandbox_id
    
    async def clone(self, source_sandbox_id: str, new_workspace_id: str) -> str:
        source = self._sandboxes[source_sandbox_id]
        new_root = self.root / new_workspace_id
        
        await asyncio.to_thread(
            shutil.copytree,
            str(source.root),
            str(new_root),
            dirs_exist_ok=True,
        )
        
        self._sandboxes[new_workspace_id] = _SandboxStateV2(
            root=new_root, workspace_id=new_workspace_id)
        logger.info("Cloned sandbox %s -> %s", source_sandbox_id, new_workspace_id)
        return new_workspace_id
    
    async def destroy(self, sandbox_id: str) -> None:
        state = self._sandboxes.pop(sandbox_id, None)
        if state:
            # 终止所有进程
            for exec_id, proc in list(state.processes.items()):
                try:
                    proc.terminate()
                except:
                    pass
            # 删除目录
            try:
                shutil.rmtree(state.root, ignore_errors=True)
            except Exception as e:
                logger.warning("Failed to remove sandbox %s: %s", sandbox_id, e)
    
    async def exec(
        self, sandbox_id: str, cmd: str, *, env: dict[str, str] | None = None
    ) -> tuple[str, str]:
        state = self._sandboxes[sandbox_id]
        rewritten = self._rewrite_cmd(state, cmd)

        def _run():
            return subprocess.run(
                [*self._shell_prefix(), rewritten],
                cwd=str(state.workspace_dir),
                capture_output=True,
                text=True,
                env=env or os.environ,
            )

        result = await asyncio.to_thread(_run)
        return result.stdout, result.stderr

    async def exec_async(
        self, sandbox_id: str, cmd: str, *, env: dict[str, str] | None = None
    ) -> str:
        import uuid
        exec_id = uuid.uuid4().hex
        state = self._sandboxes[sandbox_id]
        rewritten = self._rewrite_cmd(state, cmd)

        def _start():
            proc = subprocess.Popen(
                [*self._shell_prefix(), rewritten],
                cwd=str(state.workspace_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env or os.environ,
            )
            return proc
        
        proc = await asyncio.to_thread(_start)
        shim = _AsyncProcessShim(proc)
        state.processes[exec_id] = shim
        self._processes[exec_id] = shim
        return exec_id
    
    async def wait_process(self, exec_id: str) -> int:
        shim = self._processes.get(exec_id)
        if not shim:
            return -1
        return await asyncio.to_thread(shim.wait)
    
    async def get_process(self, exec_id: str) -> ProcessInfo:
        shim = self._processes.get(exec_id)
        if not shim:
            return ProcessInfo(running=False, exit_code=-1)
        return ProcessInfo(
            running=shim.returncode is None,
            exit_code=shim.returncode
        )
    
    async def write_file(self, sandbox_id: str, path: str, content: str) -> None:
        state = self._sandboxes[sandbox_id]
        host_path = self._map_path(state, path)
        host_path.parent.mkdir(parents=True, exist_ok=True)
        host_path.write_text(content, encoding="utf-8")
    
    async def read_file(self, sandbox_id: str, path: str) -> str:
        state = self._sandboxes[sandbox_id]
        host_path = self._map_path(state, path)
        try:
            return host_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""
    
    def resolve_virtual_path(self, sandbox_id: str, virtual_path: str) -> str:
        state = self._sandboxes[sandbox_id]
        return str(self._map_path(state, virtual_path))
    
    async def sync_back(self, sandbox_id: str, target_dir: str | Path) -> bool:
        state = self._sandboxes[sandbox_id]
        target = Path(target_dir)
        try:
            await asyncio.to_thread(
                _sync_changed_files,
                state.workspace_dir,
                target,
            )
            return True
        except Exception as e:
            logger.error("sync_back failed: %s", e)
            return False
    
    def _map_path(self, state: _SandboxStateV2, path: str) -> Path:
        if path.startswith("/workspace/"):
            return state.workspace_dir / path[len("/workspace/"):]
        elif path == "/workspace":
            return state.workspace_dir
        elif path.startswith("/sandbox-meta/"):
            return state.meta_dir / path[len("/sandbox-meta/"):]
        elif path == "/sandbox-meta":
            return state.meta_dir
        return state.workspace_dir / path.lstrip("/")
    
    def _rewrite_cmd(self, state: _SandboxStateV2, cmd: str) -> str:
        """简单的路径重写"""
        import re
        meta_posix = str(state.meta_dir).replace("\\", "/")
        work_posix = str(state.workspace_dir).replace("\\", "/")

        cmd = re.sub(r"/sandbox-meta(?=/|\s|$)", meta_posix, cmd)
        cmd = re.sub(r"/workspace(?=/|\s|$)", work_posix, cmd)
        return cmd

    @staticmethod
    def _shell_prefix() -> list[str]:
        """Return the shell invocation appropriate for the current OS."""
        if platform.system() == "Windows":
            git_bash = shutil.which("bash")
            if git_bash:
                return [git_bash, "-c"]
            return ["cmd", "/C"]
        return ["/bin/bash", "-c"]


class BubblewrapSandbox(BaseSandbox):
    """
    Bubblewrap (bwrap) 沙盒后端
    仅 Linux 支持
    """
    
    def __init__(self, config: SandboxConfig):
        self.config = config
        self.root = config.sandbox_root or Path("./.sandboxes")
        self._sandboxes: dict[str, _SandboxStateV2] = {}
        self._processes: dict[str, Any] = {}
        self._bwrap_path = shutil.which("bwrap")
        if not self._bwrap_path:
            raise RuntimeError("bwrap not found")
    
    async def create(self, workspace_id: str, template_dir: str | Path | None = None) -> str:
        # 实现 bwrap 沙盒创建
        # 简化版:先创建目录结构
        sandbox_id = workspace_id
        sandbox_path = self.root / workspace_id
        (sandbox_path / "workspace").mkdir(parents=True, exist_ok=True)
        (sandbox_path / "sandbox-meta").mkdir(parents=True, exist_ok=True)
        
        if template_dir:
            await asyncio.to_thread(
                shutil.copytree,
                str(template_dir),
                str(sandbox_path / "workspace"),
                dirs_exist_ok=True,
            )
        
        self._sandboxes[sandbox_id] = _SandboxStateV2(
            root=sandbox_path, workspace_id=workspace_id)
        return sandbox_id
    
    async def clone(self, source_sandbox_id: str, new_workspace_id: str) -> str:
        # 克隆实现
        source = self._sandboxes[source_sandbox_id]
        new_root = self.root / new_workspace_id
        await asyncio.to_thread(
            shutil.copytree,
            str(source.root),
            str(new_root),
            dirs_exist_ok=True,
        )
        self._sandboxes[new_workspace_id] = _SandboxStateV2(
            root=new_root, workspace_id=new_workspace_id)
        return new_workspace_id
    
    async def destroy(self, sandbox_id: str) -> None:
        state = self._sandboxes.pop(sandbox_id, None)
        if state:
            shutil.rmtree(state.root, ignore_errors=True)
    
    def _build_bwrap_args(self, state: _SandboxStateV2) -> list[str]:
        """构建 bwrap 命令参数"""
        args = [
            self._bwrap_path]
        
        # 基础隔离
        args.extend(["--unshare-all"])
        args.extend(["--uid", "1000"])
        args.extend(["--gid", "1000"])
        
        # 挂载点
        args.extend(["--bind", str(state.workspace_dir), "/workspace"])
        args.extend(["--bind", str(state.meta_dir), "/sandbox-meta"])
        args.extend(["--proc", "/proc"])
        args.extend(["--dev", "/dev"])
        args.extend(["--tmpfs", "/tmp"])
        args.extend(["--ro-bind", "/lib", "/lib"])
        args.extend(["--ro-bind", "/lib64", "/lib64"])
        args.extend(["--ro-bind", "/usr", "/usr"])
        args.extend(["--ro-bind", "/bin", "/bin"])
        
        # 工作目录
        args.extend(["--chdir", "/workspace"])
        
        return args
    
    async def exec(
        self, sandbox_id: str, cmd: str, *, env: dict[str, str] | None = None
    ) -> tuple[str, str]:
        state = self._sandboxes[sandbox_id]
        bwrap_args = self._build_bwrap_args(state)
        full_cmd = bwrap_args + ["--", "/bin/sh", "-c", cmd]
        
        def _run():
            return subprocess.run(
                full_cmd,
                capture_output=True,
                text=True,
                env=env or os.environ,
            )
        
        result = await asyncio.to_thread(_run)
        return result.stdout, result.stderr
    
    async def exec_async(
        self, sandbox_id: str, cmd: str, *, env: dict[str, str] | None = None
    ) -> str:
        import uuid
        exec_id = uuid.uuid4().hex
        state = self._sandboxes[sandbox_id]
        bwrap_args = self._build_bwrap_args(state)
        full_cmd = bwrap_args + ["--", "/bin/sh", "-c", cmd]
        
        def _start():
            return subprocess.Popen(
                full_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env or os.environ,
            )
        
        proc = await asyncio.to_thread(_start)
        self._processes[exec_id] = proc
        state.processes[exec_id] = proc
        return exec_id
    
    async def wait_process(self, exec_id: str) -> int:
        proc = self._processes.get(exec_id)
        if not proc:
            return -1
        return await asyncio.to_thread(proc.wait)
    
    async def get_process(self, exec_id: str) -> ProcessInfo:
        proc = self._processes.get(exec_id)
        if not proc:
            return ProcessInfo(running=False, exit_code=-1)
        proc.poll()
        return ProcessInfo(
            running=proc.returncode is None,
            exit_code=proc.returncode
        )
    
    async def write_file(self, sandbox_id: str, path: str, content: str) -> None:
        state = self._sandboxes[sandbox_id]
        host_path = self._map_path(state, path)
        host_path.parent.mkdir(parents=True, exist_ok=True)
        host_path.write_text(content, encoding="utf-8")
    
    async def read_file(self, sandbox_id: str, path: str) -> str:
        state = self._sandboxes[sandbox_id]
        host_path = self._map_path(state, path)
        try:
            return host_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""
    
    def resolve_virtual_path(self, sandbox_id: str, virtual_path: str) -> str:
        state = self._sandboxes[sandbox_id]
        return str(self._map_path(state, virtual_path))
    
    async def sync_back(self, sandbox_id: str, target_dir: str | Path) -> bool:
        state = self._sandboxes[sandbox_id]
        try:
            await asyncio.to_thread(
                _sync_changed_files,
                state.workspace_dir,
                Path(target_dir),
            )
            return True
        except Exception as e:
            logger.error("sync_back failed: %s", e)
            return False
    
    def _map_path(self, state: _SandboxStateV2, path: str) -> Path:
        if path.startswith("/workspace/"):
            return state.workspace_dir / path[len("/workspace/"):]
        elif path == "/workspace":
            return state.workspace_dir
        elif path.startswith("/sandbox-meta/"):
            return state.meta_dir / path[len("/sandbox-meta/"):]
        elif path == "/sandbox-meta":
            return state.meta_dir
        return state.workspace_dir / path.lstrip("/")


@dataclass
class _SandboxStateV2:
    """沙盒状态"""
    root: Path
    workspace_id: str
    
    @property
    def workspace_dir(self) -> Path:
        return self.root / "workspace"
    
    @property
    def meta_dir(self) -> Path:
        return self.root / "sandbox-meta"
    
    processes: dict[str, Any] = None
    
    def __post_init__(self):
        if self.processes is None:
            self.processes = {}


class _AsyncProcessShim:
    """异步进程封装"""
    def __init__(self, proc: subprocess.Popen):
        self._proc = proc
        self._stdout_buf: list[bytes] = []
        self._stderr_buf: list[bytes] = []
        self.returncode: int | None = None
        
        import threading
        def _drain():
            if self._proc.stdout:
                for line in self._proc.stdout:
                    self._stdout_buf.append(line)
            if self._proc.stderr:
                for line in self._proc.stderr:
                    self._stderr_buf.append(line)
            self._proc.wait()
            self.returncode = self._proc.returncode
        
        threading.Thread(target=_drain, daemon=True).start()
    
    def terminate(self):
        self._proc.terminate()
    
    def kill(self):
        self._proc.kill()
    
    def wait(self) -> int:
        self._proc.wait()
        return self._proc.returncode
    
    def get_stdout(self) -> str:
        return b"".join(self._stdout_buf).decode("utf-8", errors="replace")
    
    def get_stderr(self) -> str:
        return b"".join(self._stderr_buf).decode("utf-8", errors="replace")


def _sync_changed_files(source: Path, target: Path) -> None:
    """同步变更文件"""
    skip_dirs = {".git", ".agent", ".workflow", ".mas", "sandbox-meta", "node_modules"}
    target.mkdir(parents=True, exist_ok=True)
    
    for root, dirs, files in os.walk(source):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        rel_root = Path(root).relative_to(source)
        target_root = target / rel_root
        target_root.mkdir(parents=True, exist_ok=True)
        
        for fname in files:
            src_file = Path(root) / fname
            dst_file = target_root / fname
            try:
                src_stat = src_file.stat()
                if dst_file.exists():
                    dst_stat = dst_file.stat()
                    if src_stat.st_size == dst_stat.st_size and src_stat.st_mtime <= dst_stat.st_mtime:
                        continue
                shutil.copy2(str(src_file), str(dst_file))
            except Exception:
                pass


def create_sandbox(config: Optional[SandboxConfig] = None) -> BaseSandbox:
    """根据配置创建沙盒实例"""
    config = config or SandboxConfig()
    
    if config.backend == SandboxBackend.BUBBLEWRAP:
        if platform.system() != "Linux":
            logger.warning("Bubblewrap only supported on Linux, falling back to local")
            return LocalSandboxV2(config)
        try:
            return BubblewrapSandbox(config)
        except Exception as e:
            logger.warning("Bubblewrap not available: %s, falling back to local", e)
    
    elif config.backend == SandboxBackend.DOCKER:
        logger.warning("Docker backend not yet implemented, falling back to local")
    
    return LocalSandboxV2(config)
