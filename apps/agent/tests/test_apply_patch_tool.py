"""Tests for ApplyPatchTool."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from mas_agent.tools.apply_patch_tool import ApplyPatchTool

# Reusable tool instance
_tool = ApplyPatchTool()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _apply(patch: str, workspace: str) -> str:
    """Shortcut: run the tool with the given patch text."""
    return await _tool.execute({"patch": patch}, workspace)


# ---------------------------------------------------------------------------
# 1. Basic single hunk patch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_single_hunk(workspace: str) -> None:
    # Write a source file
    src = os.path.join(workspace, "calc.py")
    Path(src).write_text("def add(a, b):\n    return a + b\n\ndef sub(a, b):\n    return a - b\n")

    patch = (
        "--- a/calc.py\n"
        "+++ b/calc.py\n"
        "@@ -1,4 +1,4 @@\n"
        " def add(a, b):\n"
        "-    return a + b\n"
        "+    return a * b\n"
        " \n"
        " def sub(a, b):\n"
    )

    result = await _apply(patch, workspace)
    assert "1 hunk(s) applied" in result
    assert "calc.py" in result

    updated = Path(src).read_text()
    assert "return a * b" in updated
    assert "return a + b" not in updated


# ---------------------------------------------------------------------------
# 2. Multiple hunks in one file
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_multiple_hunks(workspace: str) -> None:
    src = os.path.join(workspace, "multi.py")
    Path(src).write_text(
        "line1\nline2\nline3\nline4\nline5\nline6\nline7\nline8\nline9\nline10\n"
    )

    patch = (
        "--- a/multi.py\n"
        "+++ b/multi.py\n"
        "@@ -1,3 +1,3 @@\n"
        " line1\n"
        "-line2\n"
        "+LINE_TWO\n"
        " line3\n"
        "@@ -8,3 +8,3 @@\n"
        " line8\n"
        "-line9\n"
        "+LINE_NINE\n"
        " line10\n"
    )

    result = await _apply(patch, workspace)
    assert "2 hunk(s) applied" in result

    updated = Path(src).read_text()
    assert "LINE_TWO" in updated
    assert "LINE_NINE" in updated
    assert "line2" not in updated
    assert "line9" not in updated
    # Other lines unchanged
    assert "line1" in updated
    assert "line5" in updated


# ---------------------------------------------------------------------------
# 3. Multiple files in one patch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_multiple_files(workspace: str) -> None:
    f1 = os.path.join(workspace, "first.py")
    f2 = os.path.join(workspace, "second.py")
    Path(f1).write_text("hello\nworld\n")
    Path(f2).write_text("foo\nbar\nbaz\n")

    patch = (
        "--- a/first.py\n"
        "+++ b/first.py\n"
        "@@ -1,2 +1,2 @@\n"
        "-hello\n"
        "+greetings\n"
        " world\n"
        "--- a/second.py\n"
        "+++ b/second.py\n"
        "@@ -1,3 +1,3 @@\n"
        " foo\n"
        "-bar\n"
        "+BAR\n"
        " baz\n"
    )

    result = await _apply(patch, workspace)
    assert "2 file(s)" in result
    assert "first.py" in result
    assert "second.py" in result

    assert "greetings" in Path(f1).read_text()
    assert "BAR" in Path(f2).read_text()


# ---------------------------------------------------------------------------
# 4. Line number offset (context-based fuzzy location)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_line_number_offset(workspace: str) -> None:
    src = os.path.join(workspace, "offset.py")
    # Extra comment line at top shifts actual positions by 1
    Path(src).write_text(
        "# comment\n"
        "def greet(name):\n"
        "    print(f'Hello {name}!')\n"
        "    return True\n"
    )

    # Hunk says line 1 but the content is actually at line 2
    patch = (
        "--- a/offset.py\n"
        "+++ b/offset.py\n"
        "@@ -1,4 +1,4 @@\n"
        " def greet(name):\n"
        "-    print(f'Hello {name}!')\n"
        "+    print(f'Hi {name}!')\n"
        "     return True\n"
    )

    result = await _apply(patch, workspace)
    assert "hunk(s) applied" in result

    updated = Path(src).read_text()
    assert "Hi" in updated
    assert "# comment" in updated  # pre-existing line preserved


# ---------------------------------------------------------------------------
# 5. Invalid / malformed patch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invalid_patch(workspace: str) -> None:
    # Completely garbled input
    result = await _apply("this is not a patch at all", workspace)
    assert "Error" in result

    # --- without +++
    result2 = await _apply("--- a/file.py\nsome content\n", workspace)
    assert "Error" in result2

    # Empty patch
    result3 = await _apply("", workspace)
    assert "Error" in result3


# ---------------------------------------------------------------------------
# 6. New file creation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_new_file_creation(workspace: str) -> None:
    new_rel = "brand_new.py"
    new_abs = os.path.join(workspace, new_rel)
    assert not os.path.exists(new_abs)

    patch = (
        "--- /dev/null\n"
        f"+++ b/{new_rel}\n"
        "@@ -0,0 +1,4 @@\n"
        "+# auto-generated\n"
        "+def hello():\n"
        "+    print('hello')\n"
        "+\n"
    )

    result = await _apply(patch, workspace)
    assert "created" in result.lower() or "new file" in result.lower()
    assert new_rel in result

    assert os.path.isfile(new_abs)
    content = Path(new_abs).read_text()
    assert "def hello():" in content
    assert "# auto-generated" in content
