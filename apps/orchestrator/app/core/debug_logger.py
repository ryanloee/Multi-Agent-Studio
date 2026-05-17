"""Debug logger — verbose runtime logging with file output.

When debug_mode is enabled via settings.json:
  - All module loggers are promoted to DEBUG level
  - Detailed logs are written to data/debug.log (with rotation)
  - Key events (director decisions, node lifecycle, LLM calls, errors)
    are captured with full context

Toggle: settings.json → debug_mode: true/false
The setting is read at each log call so changes take effect immediately.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import traceback
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_DEBUG_LOG_PATH = Path(__file__).resolve().parent.parent.parent.parent / "data" / "debug.log"
_MAX_LOG_BYTES = 10 * 1024 * 1024  # 10 MB
_BACKUP_COUNT = 3
_SETTINGS_PATH = Path(__file__).resolve().parent.parent.parent.parent / "data" / "settings.json"

_handler: RotatingFileHandler | None = None
_initialized = False


def _read_debug_mode() -> bool:
    try:
        if _SETTINGS_PATH.exists():
            data = json.loads(_SETTINGS_PATH.read_text(encoding="utf-8"))
            return bool(data.get("debug_mode", False))
    except Exception:
        pass
    return False


def _ensure_handler() -> RotatingFileHandler | None:
    global _handler, _initialized
    if _handler is not None:
        return _handler

    try:
        _DEBUG_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        _handler = RotatingFileHandler(
            str(_DEBUG_LOG_PATH),
            maxBytes=_MAX_LOG_BYTES,
            backupCount=_BACKUP_COUNT,
            encoding="utf-8",
        )
        _handler.setFormatter(logging.Formatter(
            "%(asctime)s.%(msecs)03d | %(levelname)-7s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))
        _initialized = True
        return _handler
    except Exception:
        return None


def sync_log_levels() -> None:
    """Synchronize all module loggers with the current debug_mode setting."""
    debug_on = _read_debug_mode()
    root = logging.getLogger("app")

    if debug_on:
        root.setLevel(logging.DEBUG)
        handler = _ensure_handler()
        if handler and handler not in root.handlers:
            root.addHandler(handler)
        # Promote known noisy loggers
        for name in ("app.core.director_loop", "app.core.node_runner",
                     "app.api.planner_chat", "app.core.local_sandbox",
                     "app.core.local_bus", "app.ws.hub"):
            logging.getLogger(name).setLevel(logging.DEBUG)
    else:
        root.setLevel(logging.INFO)
        if _handler and _handler in root.handlers:
            root.removeHandler(_handler)
        for name in ("app.core.director_loop", "app.core.node_runner",
                     "app.api.planner_chat", "app.core.local_sandbox",
                     "app.core.local_bus", "app.ws.hub"):
            logging.getLogger(name).setLevel(logging.INFO)


# ---------------------------------------------------------------------------
# High-level debug helpers — always safe to call, no-op when debug is off
# ---------------------------------------------------------------------------

def _log(level: int, module: str, msg: str, **kwargs: Any) -> None:
    if not _read_debug_mode():
        return
    logger = logging.getLogger(module)
    if logger.isEnabledFor(level):
        extra = ""
        if kwargs:
            parts = []
            for k, v in kwargs.items():
                try:
                    sv = json.dumps(v, ensure_ascii=False, default=str)
                except Exception:
                    sv = repr(v)
                parts.append(f"{k}={sv}")
            extra = " | " + " ".join(parts)
        logger.log(level, msg + extra)


def debug(module: str, msg: str, **kwargs: Any) -> None:
    _log(logging.DEBUG, module, msg, **kwargs)


def info(module: str, msg: str, **kwargs: Any) -> None:
    _log(logging.INFO, module, msg, **kwargs)


def warning(module: str, msg: str, **kwargs: Any) -> None:
    _log(logging.WARNING, module, msg, **kwargs)


def error(module: str, msg: str, **kwargs: Any) -> None:
    """Always log errors to debug log if enabled, and include traceback."""
    if not _read_debug_mode():
        return
    logger = logging.getLogger(module)
    if logger.isEnabledFor(logging.ERROR):
        extra = ""
        if kwargs:
            parts = []
            for k, v in kwargs.items():
                try:
                    sv = json.dumps(v, ensure_ascii=False, default=str)
                except Exception:
                    sv = repr(v)
                parts.append(f"{k}={sv}")
            extra = " | " + " ".join(parts)
        tb = traceback.format_exc()
        if tb.strip() != "NoneType: None":
            logger.error(msg + extra + "\n" + tb)
        else:
            logger.error(msg + extra)


def log_event(module: str, event_type: str, **kwargs: Any) -> None:
    """Log a structured event with type tag."""
    debug(module, f"[{event_type}]", **kwargs)


def log_llm_call(
    module: str,
    *,
    provider: str = "",
    model: str = "",
    prompt_preview: str = "",
    response_preview: str = "",
    duration_ms: float = 0,
    error: str = "",
    **kwargs: Any,
) -> None:
    """Log an LLM API call with standard fields."""
    debug(
        module, "[LLM_CALL]",
        provider=provider, model=model,
        prompt_preview=prompt_preview[:500] if prompt_preview else "",
        response_preview=response_preview[:500] if response_preview else "",
        duration_ms=round(duration_ms, 1),
        error=error,
        **kwargs,
    )


def log_node_lifecycle(
    module: str,
    *,
    node_id: str = "",
    agent_type: str = "",
    event: str = "",
    exit_code: int | None = None,
    error: str = "",
    duration_s: float = 0,
    **kwargs: Any,
) -> None:
    """Log a node lifecycle event (started/completed/failed)."""
    debug(
        module, f"[NODE_{event.upper()}]",
        node_id=node_id, agent_type=agent_type,
        exit_code=exit_code, error=error,
        duration_s=round(duration_s, 2) if duration_s else 0,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Log reader — for the frontend API to fetch recent debug logs
# ---------------------------------------------------------------------------

def read_recent_logs(lines: int = 200, level_filter: str = "") -> list[dict]:
    """Read recent debug log entries, optionally filtered by level."""
    if not _DEBUG_LOG_PATH.exists():
        return []

    entries: list[dict] = []
    try:
        raw_lines = _DEBUG_LOG_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []

    for line in raw_lines[-lines * 2:]:  # read extra in case some are multi-line
        line = line.strip()
        if not line:
            continue
        # Parse: 2026-05-17 13:00:00.000 | DEBUG   | app.core.xxx | message
        parts = line.split(" | ", 3)
        if len(parts) < 4:
            continue
        timestamp_str, level_str, module_str, message = parts
        if level_filter and level_str.strip() != level_filter:
            continue
        entries.append({
            "timestamp": timestamp_str.strip(),
            "level": level_str.strip(),
            "module": module_str.strip(),
            "message": message.strip(),
        })

    return entries[-lines:]


def clear_logs() -> bool:
    """Clear the debug log file."""
    try:
        if _DEBUG_LOG_PATH.exists():
            _DEBUG_LOG_PATH.write_text("", encoding="utf-8")
        # Also clear rotated files
        for f in _DEBUG_LOG_PATH.parent.glob("debug.log.*"):
            f.unlink(missing_ok=True)
        return True
    except OSError:
        return False
