"""Tests for GrepTool."""
from __future__ import annotations

import os
import shutil

import pytest

from mas_agent.tools.grep_tool import GrepTool


@pytest.fixture()
def grep() -> GrepTool:
    return GrepTool()


@pytest.mark.asyncio
async def test_basic_regex_search(grep: GrepTool, workspace: str) -> None:
    """Basic regex pattern returns matching lines with file:line:content."""
    result = await grep.execute({"pattern": "def \\w+"}, workspace)
    assert "def greet" in result
    assert "def add" in result
    assert "def multiply" in result
    # Each line should contain a colon separator (file:line:content),
    # except for rg context separators ("--") on Windows.
    for line in result.splitlines():
        if line.startswith("...") or line == "--":
            continue
        assert ":" in line


@pytest.mark.asyncio
async def test_no_match_returns_friendly_message(
    grep: GrepTool, workspace: str
) -> None:
    """When pattern matches nothing, a friendly message is returned."""
    result = await grep.execute({"pattern": "zzz_no_such_string_xyz"}, workspace)
    assert "No matches found" in result


@pytest.mark.asyncio
async def test_glob_filter(grep: GrepTool, workspace: str) -> None:
    """Glob filter restricts search to matching file types."""
    # Search for "print" only in Python files
    result_py = await grep.execute({"pattern": "print", "glob": "*.py"}, workspace)
    assert "print" in result_py
    # README mentions "project" but should not appear when filtering to *.py
    result_md = await grep.execute({"pattern": "Project", "glob": "*.md"}, workspace)
    assert "README.md" in result_md

    # Searching "print" in *.md should find nothing (no print in markdown)
    result_md_print = await grep.execute(
        {"pattern": "print", "glob": "*.md"}, workspace
    )
    assert "No matches found" in result_md_print


@pytest.mark.asyncio
async def test_context_parameter(grep: GrepTool, workspace: str) -> None:
    """Context=0 returns only the matching line; context>0 includes surrounding lines."""
    # With context=0 we only get exact match lines
    result_c0 = await grep.execute(
        {"pattern": "def multiply", "context": 0}, workspace
    )
    lines_c0 = [l for l in result_c0.splitlines() if not l.startswith("...")]
    # Should have exactly 1 match line
    match_lines = [l for l in lines_c0 if "def multiply" in l]
    assert len(match_lines) == 1

    # With context=2 we should get more lines
    result_c2 = await grep.execute(
        {"pattern": "def multiply", "context": 2}, workspace
    )
    lines_c2 = [l for l in result_c2.splitlines() if not l.startswith("...")]
    # context=2 should produce more output lines than context=0
    assert len(lines_c2) > len(lines_c0)


@pytest.mark.asyncio
async def test_result_count_limiting(grep: GrepTool, workspace: str) -> None:
    """When more than 50 matches exist, output is truncated with a notice."""
    # Create a file with many lines matching a simple pattern
    many_lines = "\n".join(f"line {i}: match_here" for i in range(80))
    big_file_path = os.path.join(workspace, "big.txt")
    with open(big_file_path, "w") as f:
        f.write(many_lines + "\n")

    result = await grep.execute({"pattern": "match_here"}, workspace)
    assert "more than 50 matches" in result or "..." in result
    # The result should be truncated (not all 80 lines)
    non_truncation_lines = [
        l for l in result.splitlines() if not l.startswith("...")
    ]
    assert len(non_truncation_lines) <= 55  # 50 matches + some context lines
