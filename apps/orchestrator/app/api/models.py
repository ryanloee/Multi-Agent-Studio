from fastapi import APIRouter

router = APIRouter()


@router.get("")
async def list_models():
    """List available LLM models (OpenCode supports 75+ providers)."""
    return {
        "models": [
            # ── Free models (opencode built-in, no API key needed) ──
            {
                "provider": "opencode",
                "id": "minimax-m2.5-free",
                "name": "MiniMax M2.5 Free",
                "free": True,
            },
            {
                "provider": "opencode",
                "id": "nemotron-3-super-free",
                "name": "Nemotron 3 Super Free",
                "free": True,
            },
            {
                "provider": "opencode",
                "id": "hy3-preview-free",
                "name": "HY3 Preview Free",
                "free": True,
            },
            {
                "provider": "opencode",
                "id": "big-pickle",
                "name": "Big Pickle",
                "free": True,
            },
            # ── Paid models (require API keys) ──
            {"provider": "anthropic", "id": "claude-sonnet-4-20250514", "name": "Claude Sonnet 4"},
            {"provider": "anthropic", "id": "claude-opus-4-20250514", "name": "Claude Opus 4"},
            {"provider": "openai", "id": "gpt-4o", "name": "GPT-4o"},
            {"provider": "openai", "id": "o1", "name": "o1"},
            {"provider": "google", "id": "gemini-2.0-flash", "name": "Gemini 2.0 Flash"},
        ]
    }
