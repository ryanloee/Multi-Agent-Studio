"""Edit tool — search and replace text fragments in files."""
from __future__ import annotations

import difflib
import os
import re
import threading
from typing import Any

from mas_agent.snapshot import SnapshotManager
from mas_agent.tools import Tool

# ---------------------------------------------------------------------------
# Per-file locks for concurrent-edit protection
# ---------------------------------------------------------------------------
_file_locks: dict[str, threading.Lock] = {}
_lock_registry_lock = threading.Lock()


def _get_file_lock(path: str) -> threading.Lock:
    """Return a threading.Lock unique to *path* (thread-safe lookup)."""
    with _lock_registry_lock:
        if path not in _file_locks:
            _file_locks[path] = threading.Lock()
        return _file_locks[path]


# ---------------------------------------------------------------------------
# Matching helpers
# ---------------------------------------------------------------------------

def _normalize_whitespace(text: str) -> str:
    """Compress consecutive whitespace (including newlines) into a single space."""
    return re.sub(r"\s+", " ", text)


def _strip_leading_whitespace_per_line(text: str) -> str:
    """Remove leading whitespace from every line."""
    return re.sub(r"^[ \t]+", "", text, flags=re.MULTILINE)


def _fuzzy_match(
    content: str,
    old_text: str,
    threshold: float = 0.8,
) -> list[tuple[int, int]]:
    """Find all spans in *content* that fuzzy-match *old_text* above *threshold*.

    Uses a sliding window the same length as *old_text* (character-level).
    Returns list of ``(start, end)`` tuples.
    """
    window_len = len(old_text)
    if window_len == 0 or window_len > len(content):
        return []

    matches: list[tuple[int, int]] = []
    step = max(1, window_len // 4)  # coarser stepping for long patterns
    i = 0
    while i <= len(content) - window_len:
        window = content[i : i + window_len]
        ratio = difflib.SequenceMatcher(None, old_text, window).ratio()
        if ratio >= threshold:
            # Extend to end of line for a cleaner replacement region
            end = i + window_len
            matches.append((i, end))
            i = end  # skip past this match
        else:
            i += step
    return matches


# ---------------------------------------------------------------------------
# EditTool
# ---------------------------------------------------------------------------

class EditTool(Tool):
    name = "edit"
    description = (
        "Search for a text fragment in a file and replace it with new content. "
        "Supports exact match, whitespace-normalized match, indentation-flexible "
        "match, and fuzzy match fallback."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "File path relative to workspace root",
            },
            "old_text": {
                "type": "string",
                "description": "Text fragment to search for in the file",
            },
            "new_text": {
                "type": "string",
                "description": "Replacement text",
            },
        },
        "required": ["path", "old_text", "new_text"],
    }

    async def execute(self, arguments: dict[str, Any], workspace: str) -> str:
        rel_path = arguments.get("path", "")
        old_text = arguments.get("old_text", "")
        new_text = arguments.get("new_text", "")

        if not rel_path:
            return "Error: path is required"
        if not old_text:
            return "Error: old_text is required"

        full_path = os.path.join(workspace, rel_path)
        if not os.path.isfile(full_path):
            return f"Error: file not found: {rel_path}"

        # Best-effort auto-commit before mutating the file
        snapshot = SnapshotManager(workspace)
        await snapshot.auto_commit("edit", rel_path)

        lock = _get_file_lock(full_path)
        acquired = lock.acquire(timeout=5)
        if not acquired:
            return f"Error: file {rel_path} is currently being edited by another operation"

        try:
            return self._do_edit(full_path, rel_path, old_text, new_text)
        finally:
            lock.release()

    # -----------------------------------------------------------------------
    # Internal edit logic (called under lock)
    # -----------------------------------------------------------------------

    def _do_edit(
        self,
        full_path: str,
        rel_path: str,
        old_text: str,
        new_text: str,
    ) -> str:
        with open(full_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()

        match_method = ""
        replacements: list[tuple[int, int]] = []  # (start, end) byte offsets

        # 1. Exact match
        if old_text in content:
            match_method = "exact"
            replacements = self._find_all_offsets(content, old_text)
        else:
            # 2. Whitespace-normalized match
            norm_content = _normalize_whitespace(content)
            norm_old = _normalize_whitespace(old_text)
            if norm_old in norm_content:
                match_method = "whitespace_normalized"
                replacements = self._find_all_offsets_normalized(content, old_text)
            else:
                # 3. Indentation-flexible match
                stripped_content = _strip_leading_whitespace_per_line(content)
                stripped_old = _strip_leading_whitespace_per_line(old_text)
                if stripped_old in stripped_content:
                    match_method = "indentation_flexible"
                    replacements = self._find_all_offsets_indent_flex(content, old_text)
                else:
                    # 4. Fuzzy match
                    fuzzy_hits = _fuzzy_match(content, old_text, threshold=0.8)
                    if fuzzy_hits:
                        match_method = "fuzzy"
                        replacements = fuzzy_hits

        # --- No match found ------------------------------------------------
        if not replacements:
            return self._no_match_report(content, rel_path, old_text)

        # --- Perform replacements (reverse order to preserve offsets) -------
        line_numbers: list[int] = []
        new_content = content
        for start, end in sorted(replacements, reverse=True):
            # Compute line number at replacement position (1-based)
            line_no = content[:start].count("\n") + 1
            line_numbers.append(line_no)
            new_content = new_content[:start] + new_text + new_content[end:]

        # Strip trailing blank lines
        new_content = new_content.rstrip("\n") + "\n"

        with open(full_path, "w", encoding="utf-8") as f:
            f.write(new_content)

        count = len(replacements)
        lines_str = ", ".join(str(ln) for ln in sorted(line_numbers))
        return (
            f"Replaced {count} occurrence(s) in {rel_path} "
            f"(match method: {match_method}, line(s): {lines_str})"
        )

    # -----------------------------------------------------------------------
    # Offset-finding helpers
    # -----------------------------------------------------------------------

    @staticmethod
    def _find_all_offsets(content: str, needle: str) -> list[tuple[int, int]]:
        """Find all exact occurrences of *needle* in *content*."""
        results: list[tuple[int, int]] = []
        start = 0
        while True:
            idx = content.find(needle, start)
            if idx == -1:
                break
            results.append((idx, idx + len(needle)))
            start = idx + len(needle)
        return results

    @staticmethod
    def _find_all_offsets_normalized(
        content: str,
        old_text: str,
    ) -> list[tuple[int, int]]:
        """Find offsets by normalizing whitespace in both text and matching.

        Builds a character-level mapping from normalized content back to
        original offsets so we can recover exact byte spans.
        """
        results: list[tuple[int, int]] = []
        norm_old = _normalize_whitespace(old_text)

        # Build normalized content with a mapping back to original offsets.
        # For each character in the normalized string, record the range of
        # original characters it came from.
        norm_chars: list[str] = []
        orig_ranges: list[tuple[int, int]] = []  # (start, end) in original

        i = 0
        while i < len(content):
            if content[i].isspace():
                # Skip run of whitespace, emit single space in normalized
                start = i
                while i < len(content) and content[i].isspace():
                    i += 1
                norm_chars.append(" ")
                orig_ranges.append((start, i))
            else:
                norm_chars.append(content[i])
                orig_ranges.append((i, i + 1))
                i += 1

        norm_content = "".join(norm_chars)

        # Find all occurrences of norm_old in norm_content
        start = 0
        while True:
            idx = norm_content.find(norm_old, start)
            if idx == -1:
                break
            # Map back to original offsets
            orig_start = orig_ranges[idx][0]
            orig_end = orig_ranges[idx + len(norm_old) - 1][1]
            results.append((orig_start, orig_end))
            start = idx + len(norm_old)

        return results

    @staticmethod
    def _find_all_offsets_indent_flex(
        content: str,
        old_text: str,
    ) -> list[tuple[int, int]]:
        """Find offsets ignoring leading whitespace per line."""
        stripped_old = _strip_leading_whitespace_per_line(old_text)

        results: list[tuple[int, int]] = []
        # Split content into lines, try to find runs of lines that match
        content_lines = content.split("\n")
        old_lines = stripped_old.split("\n")

        if not old_lines:
            return results

        i = 0
        while i <= len(content_lines) - len(old_lines):
            # Check if a run starting at line i matches
            match = True
            for j, ol in enumerate(old_lines):
                cl_stripped = re.sub(r"^[ \t]+", "", content_lines[i + j])
                if cl_stripped != ol:
                    match = False
                    break
            if match:
                # Compute byte offsets of this region
                start_offset = sum(
                    len(line) + 1 for line in content_lines[:i]
                )
                end_offset = sum(
                    len(line) + 1 for line in content_lines[: i + len(old_lines)]
                )
                results.append((start_offset, end_offset))
                i += len(old_lines)
            else:
                i += 1

        return results

    # -----------------------------------------------------------------------
    # No-match report
    # -----------------------------------------------------------------------

    @staticmethod
    def _no_match_report(content: str, rel_path: str, old_text: str) -> str:
        lines = content.split("\n")
        preview_lines = lines[:20]
        preview = "\n".join(
            f"{idx + 1}\t{line}" for idx, line in enumerate(preview_lines)
        )
        header = (
            f"Error: could not find old_text in {rel_path}. "
            f"File preview (first 20 lines):\n"
        )
        return header + preview
