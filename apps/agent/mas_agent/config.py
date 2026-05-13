"""Configuration loader — merges defaults, user-level, project-level, env vars, and CLI overrides."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

USER_CONFIG_DIR = Path.home() / ".mas"
USER_CONFIG_PATH = USER_CONFIG_DIR / "config.json"
PROJECT_CONFIG_FILENAME = "mas.json"

DEFAULTS: dict[str, Any] = {
    "max_turns": 50,
    "max_tokens": 4096,
    "thinking_level": "high",
    "shell_timeout": 120,
    "permissions": [],
    "agents": {},
    "tools": {"disabled": [], "custom": []},
}

# Mapping from MAS_* environment variable suffix (lowercased) to config key.
_ENV_MAPPING: dict[str, str] = {
    "mas_max_turns": "max_turns",
    "mas_max_tokens": "max_tokens",
    "mas_shell_timeout": "shell_timeout",
}


def _load_json_file(path: Path) -> dict[str, Any] | None:
    """Load a JSON file, returning None and logging a warning on failure."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
        logger.warning("Config file %s: expected a JSON object, got %s — ignoring", path, type(data).__name__)
        return None
    except FileNotFoundError:
        return None
    except json.JSONDecodeError as exc:
        logger.warning("Config file %s: invalid JSON (%s) — using defaults", path, exc)
        return None
    except OSError as exc:
        logger.warning("Config file %s: read error (%s) — using defaults", path, exc)
        return None


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge *override* into *base*, returning a new dict.

    For dict values the merge is recursive; for everything else the override wins.
    """
    merged = dict(base)
    for key, value in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _env_overrides() -> dict[str, Any]:
    """Collect MAS_* environment variable overrides."""
    result: dict[str, Any] = {}
    for env_var, config_key in _ENV_MAPPING.items():
        value = os.environ.get(env_var.upper())
        if value is not None:
            # Attempt int conversion for numeric fields
            try:
                result[config_key] = int(value)
            except ValueError:
                logger.warning("Environment variable %s=%r is not an integer — ignoring", env_var.upper(), value)
    return result


def load_config(cli_overrides: dict[str, Any] | None = None, cwd: str | None = None) -> dict[str, Any]:
    """Load and merge configuration from all sources.

    Priority (highest to lowest):
        1. CLI overrides (``cli_overrides`` dict)
        2. Environment variables with ``MAS_`` prefix
        3. Project-level ``mas.json`` in *cwd*
        4. User-level ``~/.mas/config.json``
        5. Built-in defaults (``DEFAULTS``)

    Parameters
    ----------
    cli_overrides:
        Optional dict of values explicitly set via CLI arguments.
        Only keys present in this dict will override lower-priority sources.
    cwd:
        Working directory to look for the project config.  Defaults to
        ``os.getcwd()``.
    """
    # 1. Start with built-in defaults
    config: dict[str, Any] = dict(DEFAULTS)

    # 2. User-level config (~/.mas/config.json)
    user_cfg = _load_json_file(USER_CONFIG_PATH)
    if user_cfg is not None:
        config = _deep_merge(config, user_cfg)

    # 3. Project-level config (<cwd>/mas.json)
    project_dir = Path(cwd) if cwd is not None else Path.cwd()
    project_cfg = _load_json_file(project_dir / PROJECT_CONFIG_FILENAME)
    if project_cfg is not None:
        config = _deep_merge(config, project_cfg)

    # 4. Environment variables
    env_cfg = _env_overrides()
    if env_cfg:
        config = _deep_merge(config, env_cfg)

    # 5. CLI overrides (only apply keys that are present)
    if cli_overrides:
        # Filter out None values — they mean "not explicitly set"
        filtered = {k: v for k, v in cli_overrides.items() if v is not None}
        if filtered:
            config = _deep_merge(config, filtered)

    return config
