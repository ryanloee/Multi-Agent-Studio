"""MAS Studio Launcher -- bundles frontend static files and starts the backend server.

When running from a PyInstaller bundle this module:
  1. Locates the embedded ``static/`` directory (Next.js export output).
  2. Mounts it under FastAPI so the frontend is served from the same process.
  3. Opens the user's browser after a short startup delay.
  4. Starts uvicorn.

When running from source (``python -m app.launcher``) it behaves identically
but looks for ``apps/web/out/`` relative to the repo root.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
import webbrowser
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Static-file discovery
# ---------------------------------------------------------------------------

_STATIC_DIR_ENV = "MAS_STATIC_DIR"


def _static_dir_from_env() -> Path | None:
    """Return the static dir if the env override is set."""
    val = os.environ.get(_STATIC_DIR_ENV)
    return Path(val) if val else None


def _static_dir_from_bundle() -> Path | None:
    """When running inside a PyInstaller bundle, look next to the executable."""
    if getattr(sys, "frozen", False):
        base = Path(sys._MEIPASS)  # type: ignore[attr-defined]
        candidate = base / "static"
        if candidate.is_dir():
            return candidate
    return None


def _static_dir_from_source() -> Path | None:
    """When running from source, look at ``apps/web/out/``."""
    # launcher.py lives at apps/orchestrator/app/launcher.py
    this_dir = Path(__file__).resolve().parent
    candidate = this_dir.parent.parent.parent / "web" / "out"
    if candidate.is_dir():
        return candidate
    return None


def get_static_dir() -> Path | None:
    """Return the frontend static directory, or *None* if not found."""
    return (
        _static_dir_from_env()
        or _static_dir_from_bundle()
        or _static_dir_from_source()
    )


# ---------------------------------------------------------------------------
# Browser opener
# ---------------------------------------------------------------------------

def _open_browser(url: str) -> None:
    """Open *url* in the default browser after a short delay."""
    time.sleep(2)
    logger.info("Opening browser: %s", url)
    webbrowser.open(url)


# ---------------------------------------------------------------------------
# SPA catch-all route
# ---------------------------------------------------------------------------

def _mount_spa(app, static_dir: Path) -> None:
    """Mount static assets and add a catch-all route for SPA routing."""
    from fastapi.responses import FileResponse
    from fastapi.staticfiles import StaticFiles

    # Mount everything under /_next, /images, etc. as static files.
    app.mount("/_next", StaticFiles(directory=static_dir / "_next"), name="next-static")

    # Other asset subdirectories that Next.js export may create.
    for child in static_dir.iterdir():
        if child.is_dir() and child.name not in ("_next",):
            try:
                app.mount(
                    f"/{child.name}",
                    StaticFiles(directory=child),
                    name=f"static-{child.name}",
                )
            except Exception:
                pass

    @app.get("/{full_path:path}")
    async def serve_spa(full_path: str):  # type: ignore[union-attr]
        """Serve ``index.html`` for any unmatched path (SPA client-side routing)."""
        # If a matching file exists under the static dir, serve it directly.
        requested = static_dir / full_path
        if full_path and requested.is_file():
            return FileResponse(requested)
        # Otherwise fall back to index.html for SPA routing.
        index = static_dir / "index.html"
        if index.is_file():
            return FileResponse(index)
        return {"detail": "Frontend not built -- index.html not found"}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    # Force ProactorEventLoop on Windows (subprocess support).
    if sys.platform == "win32":
        import asyncio
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

    host = os.environ.get("MAS_HOST", "127.0.0.1")
    port = int(os.environ.get("MAS_PORT", "3000"))

    # Import the FastAPI app *after* the event-loop policy is set so the
    # lifespan handler runs under ProactorEventLoop on Windows.
    from app.main import app  # noqa: F811

    static_dir = get_static_dir()
    if static_dir is not None:
        logger.info("Serving frontend from: %s", static_dir)
        _mount_spa(app, static_dir)
    else:
        logger.warning(
            "Frontend static directory not found. "
            "Set %s or ensure 'next build' has been run with output: 'export'.",
            _STATIC_DIR_ENV,
        )

    url = f"http://{host}:{port}"
    threading.Thread(target=_open_browser, args=(url,), daemon=True).start()

    import uvicorn
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
