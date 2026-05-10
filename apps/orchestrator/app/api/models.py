"""Model list API — reads from user-configured models in settings.

The model list is sourced exclusively from the user's settings (data/settings.json).
The old models.json static config file is no longer used for the model list,
but may still be referenced by the planner chat LLM call for backward compatibility.
"""

import json
import logging
import os
from pathlib import Path

from fastapi import APIRouter

logger = logging.getLogger(__name__)

router = APIRouter()

_CONFIG_PATH = Path(__file__).parent / "models.json"
_SETTINGS_PATH = Path(__file__).resolve().parent.parent.parent.parent / "data" / "settings.json"


def load_provider_config() -> dict:
    """Load raw provider config from models.json (for backward compatibility with planner_chat).

    Returns dict mapping provider_id -> {"url": str, "key": str, "models": [...]}.
    """
    try:
        data = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        logger.warning("Failed to load models.json")
        return {}

    providers = {}
    for p in data.get("providers", []):
        pid = p["id"]
        key_env = p.get("key", "")
        providers[pid] = {
            "url": p.get("url", ""),
            "key": os.environ.get(key_env, "") if key_env else "",
            "label": p.get("label", pid),
            "free": p.get("free", False),
            "models": p.get("models", []),
        }
    return providers


def _load_models_from_settings() -> list[dict]:
    """Load models exclusively from user settings (data/settings.json)."""
    models: list[dict] = []

    try:
        settings_data = json.loads(_SETTINGS_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return models

    settings_models = settings_data.get("models", [])
    if not isinstance(settings_models, list):
        return models

    for m in settings_models:
        if not isinstance(m, dict):
            continue
        model_id = m.get("id", "")
        name = m.get("name", "") or m.get("default_model", "")
        fmt = m.get("format", "openai")
        base_url = m.get("base_url", "")
        default_model = m.get("default_model", "")
        api_key = m.get("api_key", "")

        # Build full_id in format: format/base_url/model_name
        full_id = f"{fmt}/{base_url}/{default_model}" if default_model else f"{fmt}/{base_url}/{name}"

        models.append({
            "provider": fmt,
            "id": default_model or name,
            "full_id": full_id,
            "name": name or default_model,
            "provider_label": f"{fmt.upper()} - {base_url.replace('https://', '').replace('http://', '').split('/')[0]}",
            "free": False,
            "max_tokens": None,
            "context_length": None,
            "base_url": base_url,
            "api_key": api_key,
            "format": fmt,
        })

    return models


@router.get("")
async def list_models():
    """List available LLM models from user settings only."""
    models = _load_models_from_settings()
    return {"models": models}
