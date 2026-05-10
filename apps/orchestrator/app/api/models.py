"""Model list API — reads from models.json config file."""

import json
import logging
import os
from pathlib import Path

from fastapi import APIRouter

logger = logging.getLogger(__name__)

router = APIRouter()

_CONFIG_PATH = Path(__file__).parent / "models.json"


def load_provider_config() -> dict:
    """Load raw provider config from models.json.

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


def _load_models() -> list[dict]:
    models: list[dict] = []
    providers = load_provider_config()

    for provider_id, cfg in providers.items():
        for m in cfg.get("models", []):
            models.append({
                "provider": provider_id,
                "id": m["id"],
                "full_id": f"{provider_id}/{m['id']}",
                "name": m.get("label", m.get("name", m["id"])),
                "provider_label": cfg.get("label", provider_id),
                "free": m.get("free", cfg.get("free", False)),
                "max_tokens": m.get("max_tokens"),
                "context_length": m.get("context_length"),
            })

    return models


@router.get("")
async def list_models():
    """List available LLM models from config file."""
    return {"models": _load_models()}
