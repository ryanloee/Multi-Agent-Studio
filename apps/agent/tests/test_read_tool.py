"""Tests for ReadTool — pagination, encoding, BOM, binary, line range, large file."""
from __future__ import annotations

import os

import pytest

from mas_agent.tools.read_tool import ReadTool


@pytest.fixture()
def reader() -> ReadTool:
    return ReadTool()


# ---------------------------------------------------------------------------
# 1. Pagination hint
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_pagination_hint(reader: ReadTool, tmp_path: str) -> None:
    """Reading the first 500 lines of a 1000-line file shows a pagination hint."""
    workspace = str(tmp_path)
    lines = [f"line {i}" for i in range(1000)]
    p = os.path.join(workspace, "long.txt")
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    result = await reader.execute({"path": "long.txt", "limit": 500}, workspace)

    # Should contain pagination hint at the bottom.
    assert "Lines 1-500 of 1000" in result
    assert "Use offset=500 to read more." in result


# ---------------------------------------------------------------------------
# 2. Encoding detection — GBK
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_gbk_encoding(reader: ReadTool, tmp_path: str) -> None:
    """A GBK-encoded file is auto-detected and read correctly."""
    workspace = str(tmp_path)
    content = "你好世界\n第二行\n"
    p = os.path.join(workspace, "gbk.txt")
    with open(p, "w", encoding="gbk") as f:
        f.write(content)

    result = await reader.execute({"path": "gbk.txt"}, workspace)
    assert "你好世界" in result
    assert "第二行" in result


# ---------------------------------------------------------------------------
# 3. BOM handling
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_utf8_bom_stripped(reader: ReadTool, tmp_path: str) -> None:
    """UTF-8 BOM is stripped and does not appear in the output."""
    workspace = str(tmp_path)
    p = os.path.join(workspace, "bom.txt")
    with open(p, "wb") as f:
        f.write(b"\xef\xbb\xbfHello BOM world\n")

    result = await reader.execute({"path": "bom.txt"}, workspace)
    # The BOM character should NOT appear in the numbered output.
    assert "﻿" not in result
    assert "Hello BOM world" in result


# ---------------------------------------------------------------------------
# 4. Binary file detection
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_binary_file_detection(reader: ReadTool, tmp_path: str) -> None:
    """A file containing NUL bytes is detected as binary."""
    workspace = str(tmp_path)
    p = os.path.join(workspace, "binary.bin")
    with open(p, "wb") as f:
        f.write(b"some\x00binary\x00data\x00" * 100)

    result = await reader.execute({"path": "binary.bin"}, workspace)
    assert "Binary file, cannot display" in result


# ---------------------------------------------------------------------------
# 5. Line range — start_line / end_line
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_line_range_parameters(reader: ReadTool, tmp_path: str) -> None:
    """start_line=10, end_line=20 returns exactly those lines (1-based inclusive)."""
    workspace = str(tmp_path)
    lines = [f"line {i}" for i in range(1, 31)]  # lines 1..30
    p = os.path.join(workspace, "range.txt")
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    result = await reader.execute(
        {"path": "range.txt", "start_line": 10, "end_line": 20}, workspace
    )

    # Should include lines 10..20 (1-based).
    assert "line 10" in result
    assert "line 20" in result
    # Should NOT include lines before 10 or after 20.
    assert "line 9" not in result
    assert "line 21" not in result


# ---------------------------------------------------------------------------
# 6. Large file warning
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_large_file_warning(reader: ReadTool, tmp_path: str) -> None:
    """A file with >1000 lines shows a large-file warning at the top."""
    workspace = str(tmp_path)
    lines = [f"line {i}" for i in range(1500)]
    p = os.path.join(workspace, "huge.txt")
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    result = await reader.execute({"path": "huge.txt"}, workspace)
    assert "Large file (1500 lines)" in result
    assert "Showing lines 1-500" in result
