"""Shared fixtures for mas_agent tests."""
from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.fixture()
def workspace(tmp_path: Path) -> str:
    """Return a temporary workspace directory populated with sample files."""
    # Create a few sample files for grep/glob tests
    (tmp_path / "hello.py").write_text(
        "def greet(name):\n    print(f'Hello {name}!')\n    return True\n"
    )
    (tmp_path / "utils.py").write_text(
        "def add(a, b):\n    return a + b\n\ndef multiply(a, b):\n    return a * b\n"
    )
    (tmp_path / "README.md").write_text(
        "# Project\n\nThis is a sample project.\n\n## Usage\n\nRun the code.\n"
    )
    # Nested file
    subdir = tmp_path / "src"
    subdir.mkdir()
    (subdir / "main.py").write_text(
        "from utils import add\n\nresult = add(1, 2)\nprint(result)\n"
    )
    return str(tmp_path)
