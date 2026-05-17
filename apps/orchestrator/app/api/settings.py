"""Global settings API — reads / writes data/settings.json.

The settings file is a flat JSON object with sections for general, display,
and model configuration.  The backend reads this file when spawning agent
subprocesses so that API keys and base URLs are available at runtime.
"""

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

router = APIRouter()

# ---------------------------------------------------------------------------
# Settings file path
# ---------------------------------------------------------------------------

_SETTINGS_PATH = Path(__file__).resolve().parent.parent.parent.parent / "data" / "settings.json"


def _ensure_dir() -> None:
    _SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)


def _read_settings() -> dict[str, Any]:
    if not _SETTINGS_PATH.exists():
        return {}
    try:
        return json.loads(_SETTINGS_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to read settings file: %s", exc)
        return {}


def _write_settings(data: dict[str, Any]) -> None:
    _ensure_dir()
    try:
        _SETTINGS_PATH.write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"写入配置文件失败: {exc}") from exc


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------

class ModelEntry(BaseModel):
    id: str = Field(default="", description="唯一标识，自动生成")
    name: str = Field(default="", description="显示名称")
    format: str = Field(default="openai", description="openai 或 anthropic")
    base_url: str = Field(default="", description="API Base URL")
    api_key: str = Field(default="", description="API Key")
    default_model: str = Field(default="", description="默认模型 ID")
    context_window: int = Field(default=128000, ge=1024, description="模型上下文窗口 token 数")
    max_output_tokens: int = Field(default=4096, ge=256, description="单次输出 token 上限")
    enabled: bool = Field(default=True, description="是否启用该模型")


class GeneralSettings(BaseModel):
    language: str = Field(default="zh", description="界面语言")
    default_workspace: str = Field(default="", description="默认工作区目录")


class DisplaySettings(BaseModel):
    theme: str = Field(default="system", description="主题: light / dark / system")
    compact_mode: bool = Field(default=False, description="紧凑模式")


class ModelStrategy(BaseModel):
    """Per-role model assignments.  Value format: 'provider/model_id' or just 'model_id'."""
    planner: str = Field(default="", description="Planner 模型")
    design: str = Field(default="", description="Design 模型")
    review: str = Field(default="", description="Review 模型")
    merge: str = Field(default="", description="Merge 模型")
    explore: str = Field(default="", description="Explore 模型")
    coder: str = Field(default="", description="Coder 模型")
    shell: str = Field(default="", description="Shell 模型")


class DebugSettings(BaseModel):
    enabled: bool = Field(default=False, description="调试模式开关")
    log_level: str = Field(default="DEBUG", description="日志级别: DEBUG/INFO/WARNING/ERROR")


class SettingsResponse(BaseModel):
    general: GeneralSettings = Field(default_factory=GeneralSettings)
    display: DisplaySettings = Field(default_factory=DisplaySettings)
    models: list[ModelEntry] = Field(default_factory=list)
    model_strategy: ModelStrategy = Field(default_factory=ModelStrategy)
    debug_mode: bool = Field(default=False, description="调试模式开关")
    debug_settings: DebugSettings = Field(default_factory=DebugSettings)


class UpdateSettingsRequest(BaseModel):
    general: GeneralSettings | None = None
    display: DisplaySettings | None = None
    models: list[ModelEntry] | None = None
    model_strategy: ModelStrategy | None = None
    debug_mode: bool | None = None
    debug_settings: DebugSettings | None = None


# ---------------------------------------------------------------------------
# Default settings
# ---------------------------------------------------------------------------

_DEFAULT_SETTINGS: dict[str, Any] = SettingsResponse().model_dump()


# ---------------------------------------------------------------------------
# Settings CRUD endpoints
# ---------------------------------------------------------------------------

@router.get("")
async def get_settings() -> SettingsResponse:
    stored = _read_settings()
    merged = _deep_merge(_DEFAULT_SETTINGS, stored)
    if not isinstance(merged.get("models"), list):
        merged["models"] = []
    # Flatten debug_mode / debug_settings from stored data
    merged["debug_mode"] = bool(stored.get("debug_mode", False))
    ds = stored.get("debug_settings", {})
    merged.setdefault("debug_settings", {})
    merged["debug_settings"]["enabled"] = merged["debug_mode"]
    merged["debug_settings"].setdefault("log_level", "DEBUG")
    return SettingsResponse(**merged)


@router.put("")
async def update_settings(body: UpdateSettingsRequest) -> SettingsResponse:
    stored = _read_settings()

    if body.general is not None:
        stored.setdefault("general", {}).update(body.general.model_dump(exclude_unset=True))
    if body.display is not None:
        stored.setdefault("display", {}).update(body.display.model_dump(exclude_unset=True))
    if body.models is not None:
        stored["models"] = [m.model_dump() for m in body.models]
    if body.model_strategy is not None:
        stored.setdefault("model_strategy", {}).update(
            body.model_strategy.model_dump(exclude_unset=True)
        )
    if body.debug_mode is not None:
        stored["debug_mode"] = body.debug_mode
    if body.debug_settings is not None:
        stored["debug_settings"] = body.debug_settings.model_dump(exclude_unset=True)
        stored["debug_mode"] = body.debug_settings.enabled

    _write_settings(stored)

    # Sync logger levels when debug mode changes
    from app.core.debug_logger import sync_log_levels
    sync_log_levels()

    merged = _deep_merge(_DEFAULT_SETTINGS, stored)
    if not isinstance(merged.get("models"), list):
        merged["models"] = []
    merged["debug_mode"] = bool(stored.get("debug_mode", False))
    ds = stored.get("debug_settings", {})
    merged.setdefault("debug_settings", {})
    merged["debug_settings"]["enabled"] = merged["debug_mode"]
    merged["debug_settings"].setdefault("log_level", "DEBUG")
    return SettingsResponse(**merged)


# ---------------------------------------------------------------------------
# Path validation endpoint
# ---------------------------------------------------------------------------

class PathValidateRequest(BaseModel):
    path: str = Field(..., description="要验证的路径")


class PathValidateResponse(BaseModel):
    valid: bool
    exists: bool
    is_dir: bool
    is_absolute: bool
    message: str


@router.post("/validate-path", response_model=PathValidateResponse)
async def validate_path(body: PathValidateRequest) -> PathValidateResponse:
    p = body.path.strip()
    if not p:
        return PathValidateResponse(
            valid=False, exists=False, is_dir=False, is_absolute=False,
            message="路径不能为空",
        )

    try:
        resolved = Path(p)
    except Exception as exc:
        return PathValidateResponse(
            valid=False, exists=False, is_dir=False, is_absolute=False,
            message=f"路径格式无效: {exc}",
        )

    is_absolute = resolved.is_absolute()
    exists = resolved.exists()
    is_dir = resolved.is_dir() if exists else False

    invalid_chars = set('<>"|?*')
    found_invalid = set(p) & invalid_chars
    if found_invalid:
        return PathValidateResponse(
            valid=False, exists=False, is_dir=False, is_absolute=is_absolute,
            message=f"路径包含非法字符: {' '.join(found_invalid)}",
        )

    if not exists:
        parent = resolved.parent
        parent_ok = parent.exists() and parent.is_dir() if str(parent) != str(resolved) else False
        return PathValidateResponse(
            valid=True, exists=False, is_dir=False, is_absolute=is_absolute,
            message="路径不存在" + ("，但父目录有效，运行时可自动创建" if parent_ok else ""),
        )

    if not is_dir:
        return PathValidateResponse(
            valid=False, exists=True, is_dir=False, is_absolute=is_absolute,
            message="路径存在但不是目录",
        )

    try:
        test_file = resolved / ".__mas_write_test__"
        test_file.write_text("ok", encoding="utf-8")
        test_file.unlink(missing_ok=True)
        writable = True
    except OSError:
        writable = False

    return PathValidateResponse(
        valid=True, exists=True, is_dir=True, is_absolute=is_absolute,
        message="路径有效" + ("" if writable else "，但当前用户无写入权限"),
    )


# ---------------------------------------------------------------------------
# Model URL test endpoint — tests connectivity to a model provider API
# ---------------------------------------------------------------------------

class ModelTestRequest(BaseModel):
    format: str = Field(..., description="openai 或 anthropic")
    base_url: str = Field(..., description="API Base URL")
    api_key: str = Field(default="", description="API Key（可选，无 key 则仅测试连通性）")
    default_model: str = Field(default="", description="默认模型 ID（Anthropic 测试时使用）")


class ModelTestResponse(BaseModel):
    success: bool
    status_code: int | None = None
    latency_ms: int | None = None
    models_count: int | None = None
    model_names: list[str] = Field(default_factory=list, description="从 API 获取到的模型 ID 列表")
    error: str | None = None


@router.post("/test-model-url", response_model=ModelTestResponse)
async def test_model_url(body: ModelTestRequest) -> ModelTestResponse:
    """Test connectivity to a model provider API.

    For OpenAI-compatible format: calls GET {base_url}/models
    For Anthropic format: calls POST {base_url}/v1/messages with a minimal payload
    """
    import httpx

    base = body.base_url.rstrip("/")
    api_key = body.api_key.strip()

    if body.format == "openai":
        # Test by listing models (GET /models)
        url = f"{base}/models"
        headers: dict[str, str] = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        start = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(url, headers=headers)
            elapsed = int((time.monotonic() - start) * 1000)

            if resp.status_code == 200:
                data = resp.json()
                model_list = data.get("data", []) if isinstance(data, dict) else []
                models_count = len(model_list)
                model_names = [m.get("id", "") for m in model_list if isinstance(m, dict) and m.get("id")]
                return ModelTestResponse(
                    success=True,
                    status_code=200,
                    latency_ms=elapsed,
                    models_count=models_count,
                    model_names=model_names,
                    error=None,
                )
            else:
                return ModelTestResponse(
                    success=False,
                    status_code=resp.status_code,
                    latency_ms=elapsed,
                    models_count=None,
                    error=f"HTTP {resp.status_code}: {resp.text[:200]}",
                )
        except httpx.ConnectError:
            return ModelTestResponse(success=False, status_code=None, latency_ms=None,
                                     models_count=None, error="连接失败：无法到达服务器")
        except httpx.TimeoutException:
            return ModelTestResponse(success=False, status_code=None, latency_ms=None,
                                     models_count=None, error="连接超时（15秒）")
        except Exception as exc:
            return ModelTestResponse(success=False, status_code=None, latency_ms=None,
                                     models_count=None, error=f"请求错误: {exc}")

    elif body.format == "anthropic":
        # Anthropic doesn't have a /models endpoint.
        # Test by sending a minimal messages request (will fail with 400 but proves connectivity)
        url = f"{base}/v1/messages"
        headers = {
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01",
        }
        if api_key:
            headers["x-api-key"] = api_key

        # Minimal valid payload (will get 400/4xx but proves connectivity)
        model_name = body.default_model.strip() if body.default_model else "claude-3-5-sonnet-20241022"
        payload = {
            "model": model_name,
            "max_tokens": 1,
            "messages": [{"role": "user", "content": "hi"}],
        }

        start = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(url, headers=headers, json=payload)
            elapsed = int((time.monotonic() - start) * 1000)

            # Any response means the server is reachable
            # 200 = actually worked, 400 = bad request (but reachable),
            # 401 = auth issue (but reachable), 429 = rate limited (but reachable)
            if resp.status_code in (200, 400, 401, 403, 429):
                return ModelTestResponse(
                    success=True,
                    status_code=resp.status_code,
                    latency_ms=elapsed,
                    models_count=None,
                    model_names=[],
                    error=None if resp.status_code == 200 else f"可达但返回 {resp.status_code}",
                )
            else:
                return ModelTestResponse(
                    success=False,
                    status_code=resp.status_code,
                    latency_ms=elapsed,
                    models_count=None,
                    error=f"HTTP {resp.status_code}: {resp.text[:200]}",
                )
        except httpx.ConnectError:
            return ModelTestResponse(success=False, status_code=None, latency_ms=None,
                                     models_count=None, error="连接失败：无法到达服务器")
        except httpx.TimeoutException:
            return ModelTestResponse(success=False, status_code=None, latency_ms=None,
                                     models_count=None, error="连接超时（15秒）")
        except Exception as exc:
            return ModelTestResponse(success=False, status_code=None, latency_ms=None,
                                     models_count=None, error=f"请求错误: {exc}")
    else:
        return ModelTestResponse(
            success=False, status_code=None, latency_ms=None,
            models_count=None, error=f"不支持的格式: {body.format}",
        )


# ---------------------------------------------------------------------------
# Native directory browse endpoint
# ---------------------------------------------------------------------------

class BrowseDirRequest(BaseModel):
    current_path: str = Field(default="", description="当前路径")


class BrowseDirResponse(BaseModel):
    path: str = Field(default="")


class ListDirRequest(BaseModel):
    path: str = Field(default="", description="要浏览的目录")


class DirEntry(BaseModel):
    name: str
    path: str


class ListDirResponse(BaseModel):
    path: str
    parent: str
    entries: list[DirEntry] = Field(default_factory=list)
    error: str = ""


@router.post("/browse-dir", response_model=BrowseDirResponse)
async def browse_directory(body: BrowseDirRequest) -> BrowseDirResponse:
    import sys

    def _pick_folder(initial_dir: str) -> str:
        try:
            import tkinter as tk
            from tkinter import filedialog
        except ImportError:
            return ""

        root = None

        try:
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            init_dir = initial_dir if initial_dir and Path(initial_dir).is_dir() else ""
            selected = filedialog.askdirectory(
                title="选择工作目录" if sys.platform == "win32" else "Select Directory",
                initialdir=init_dir or None,
            )
            return selected if isinstance(selected, str) else ""
        except Exception:
            return ""
        finally:
            if root is not None:
                root.destroy()

    loop = asyncio.get_event_loop()
    selected_path = await loop.run_in_executor(None, _pick_folder, body.current_path)
    return BrowseDirResponse(path=selected_path if isinstance(selected_path, str) else "")


@router.post("/list-dir", response_model=ListDirResponse)
async def list_directory(body: ListDirRequest) -> ListDirResponse:
    raw_path = body.path.strip() or str(Path.home())
    try:
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            path = (Path.cwd() / path).resolve()
        else:
            path = path.resolve()
    except OSError as exc:
        return ListDirResponse(
            path=str(Path.home()),
            parent=str(Path.home().parent),
            entries=[],
            error=f"路径无效: {exc}",
        )

    if not path.exists() or not path.is_dir():
        path = Path.home()

    entries: list[DirEntry] = []
    error = ""
    try:
        for child in path.iterdir():
            try:
                if child.is_dir():
                    entries.append(DirEntry(name=child.name, path=str(child)))
            except OSError:
                continue
    except OSError as exc:
        error = f"无法读取目录: {exc}"

    entries.sort(key=lambda item: (item.name.startswith("."), item.name.lower()))
    return ListDirResponse(
        path=str(path),
        parent=str(path.parent),
        entries=entries,
        error=error,
    )


# ---------------------------------------------------------------------------
# Debug log endpoints
# ---------------------------------------------------------------------------

class DebugLogRequest(BaseModel):
    lines: int = Field(default=200, ge=1, le=2000, description="读取行数")
    level: str = Field(default="", description="按级别过滤: DEBUG/INFO/WARNING/ERROR")


class DebugLogEntry(BaseModel):
    timestamp: str
    level: str
    module: str
    message: str


class DebugLogResponse(BaseModel):
    entries: list[DebugLogEntry] = Field(default_factory=list)
    total: int = 0
    debug_mode: bool = False
    log_file: str = ""


@router.post("/debug-logs", response_model=DebugLogResponse)
async def get_debug_logs(body: DebugLogRequest) -> DebugLogResponse:
    from app.core.debug_logger import read_recent_logs, _read_debug_mode, _DEBUG_LOG_PATH
    entries = read_recent_logs(lines=body.lines, level_filter=body.level)
    return DebugLogResponse(
        entries=[DebugLogEntry(**e) for e in entries],
        total=len(entries),
        debug_mode=_read_debug_mode(),
        log_file=str(_DEBUG_LOG_PATH),
    )


class DebugLogClearResponse(BaseModel):
    success: bool


@router.post("/debug-logs/clear", response_model=DebugLogClearResponse)
async def clear_debug_logs() -> DebugLogClearResponse:
    from app.core.debug_logger import clear_logs
    return DebugLogClearResponse(success=clear_logs())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _deep_merge(base: dict, override: dict) -> dict:
    result = dict(base)
    for key, val in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(val, dict):
            result[key] = _deep_merge(result[key], val)
        elif key in result and isinstance(result[key], list) and isinstance(val, list):
            # For lists, use the override value (full replacement)
            result[key] = val
        else:
            result[key] = val
    return result
