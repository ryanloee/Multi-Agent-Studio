from __future__ import annotations

import sys
from pathlib import Path


def _ensure_orchestrator_package_on_syspath() -> None:
    """Make the orchestrator package importable as `app.*` in tests.

    Pytest may execute from different working directories (repo root, apps/orchestrator,
    CI runners). Adding the orchestrator directory to `sys.path` ensures imports like
    `from app.core...` work consistently.
    """

    orchestrator_root = Path(__file__).resolve().parents[1]
    orchestrator_root_str = str(orchestrator_root)
    if orchestrator_root_str not in sys.path:
        sys.path.insert(0, orchestrator_root_str)


def pytest_configure() -> None:
    _ensure_orchestrator_package_on_syspath()
