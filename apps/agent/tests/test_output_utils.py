"""Tests for output_utils.truncate_output."""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from mas_agent.tools.output_utils import truncate_output


class TestTruncateOutputPassthrough:
    """Short output should pass through unchanged."""

    def test_short_output_unchanged(self) -> None:
        content = "x" * 100
        result = truncate_output(content, max_chars=5000)
        assert result == content

    def test_exactly_max_chars_passthrough(self) -> None:
        content = "a" * 5000
        result = truncate_output(content, max_chars=5000)
        assert result == content

    def test_one_over_max_triggers_truncation(self) -> None:
        content = "a" * 5001
        result = truncate_output(content, max_chars=5000, workspace="/tmp/fake")
        assert result != content
        assert "truncated" in result


class TestTruncateOutputWithWorkspace:
    """Long output with a workspace should be saved and truncated."""

    def test_long_output_saved_to_file(self, tmp_path: Path) -> None:
        workspace = str(tmp_path)
        content = "A" * 10000

        result = truncate_output(content, max_chars=5000, workspace=workspace, label="shell")

        # Result should not be the full content
        assert len(result) < len(content)

        # A file should have been created
        output_dir = os.path.join(workspace, ".agent", "outputs")
        assert os.path.isdir(output_dir)
        files = os.listdir(output_dir)
        assert len(files) == 1
        assert files[0].startswith("shell_")
        assert files[0].endswith(".txt")

        # Saved file should contain full content
        saved_path = os.path.join(output_dir, files[0])
        saved_content = Path(saved_path).read_text(encoding="utf-8")
        assert saved_content == content

    def test_return_format_head_hint_tail(self, tmp_path: Path) -> None:
        workspace = str(tmp_path)
        content = "B" * 10000

        result = truncate_output(content, max_chars=5000, workspace=workspace, label="test")

        # First 500 chars
        assert result.startswith("B" * 500)
        # Last 500 chars
        assert result.endswith("B" * 500)
        # Hint in the middle
        assert "(truncated, full output saved to" in result
        assert ".agent" in result
        assert "outputs" in result

    def test_multiple_calls_dont_overwrite(self, tmp_path: Path) -> None:
        workspace = str(tmp_path)

        # Ensure different timestamps by using distinct labels or sleeping
        result1 = truncate_output("X" * 6000, max_chars=5000, workspace=workspace, label="run_a")
        time.sleep(0.01)  # ensure unique timestamp
        result2 = truncate_output("Y" * 7000, max_chars=5000, workspace=workspace, label="run_b")

        output_dir = os.path.join(workspace, ".agent", "outputs")
        files = sorted(os.listdir(output_dir))
        assert len(files) == 2

        # Different labels produce different filenames
        assert files[0].startswith("run_a_")
        assert files[1].startswith("run_b_")

        # Check saved contents are correct
        content_a = Path(os.path.join(output_dir, files[0])).read_text()
        content_b = Path(os.path.join(output_dir, files[1])).read_text()
        assert content_a == "X" * 6000
        assert content_b == "Y" * 7000

    def test_saved_file_is_complete_and_readable(self, tmp_path: Path) -> None:
        workspace = str(tmp_path)
        # Use enough lines to exceed 5000 chars so truncation triggers
        content = "Line %d\n" * 5000
        content = content % tuple(range(5000))

        truncate_output(content, max_chars=5000, workspace=workspace, label="read")

        output_dir = os.path.join(workspace, ".agent", "outputs")
        files = os.listdir(output_dir)
        saved = Path(os.path.join(output_dir, files[0])).read_text(encoding="utf-8")
        assert saved == content
        # Spot-check some lines
        assert "Line 0" in saved
        assert "Line 499" in saved


class TestTruncateOutputWithoutWorkspace:
    """When workspace is None, no file is saved, just truncation."""

    def test_no_workspace_truncation_only(self) -> None:
        content = "Z" * 10000
        result = truncate_output(content, max_chars=5000, workspace=None, label="grep")

        # Should still be truncated
        assert len(result) < len(content)
        assert result.startswith("Z" * 500)
        assert result.endswith("Z" * 500)
        assert "truncated" in result
        # Should NOT mention a file path
        assert "saved to" not in result
        assert "no workspace provided" in result

    def test_no_directory_created_without_workspace(self, tmp_path: Path) -> None:
        content = "Z" * 10000
        truncate_output(content, max_chars=5000, workspace=None, label="grep")

        # No .agent directory should be created in cwd
        assert not os.path.exists(os.path.join(str(tmp_path), ".agent"))
