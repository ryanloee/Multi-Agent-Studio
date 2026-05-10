"""Apply-patch tool -- apply unified diff patches to workspace files.

Designed for GPT-series models that natively produce unified diff output.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any

from mas_agent.tools import Tool


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class Hunk:
    """One contiguous block of changes inside a file diff."""

    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: list[tuple[str, str]] = field(default_factory=list)
    # Each entry: (kind, text) where kind is ' '(context), '-'(removed), '+'(added)


@dataclass
class FilePatch:
    """All hunks for a single file."""

    old_path: str  # from --- line (may be /dev/null for new files)
    new_path: str  # from +++ line
    hunks: list[Hunk] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Patch parser
# ---------------------------------------------------------------------------

_HUNK_RE = re.compile(
    r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@"
)

_OLD_FILE_RE = re.compile(r"^--- ([^\t\n]+)")
_NEW_FILE_RE = re.compile(r"^\+\+\+ ([^\t\n]+)")


def parse_patch(patch_text: str) -> list[FilePatch]:
    """Parse a unified diff string into a list of *FilePatch* objects.

    Raises ``ValueError`` on malformed input.
    """
    patches: list[FilePatch] = []
    current: FilePatch | None = None
    current_hunk: Hunk | None = None

    lines = patch_text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]

        # --- file header ---
        m_old = _OLD_FILE_RE.match(line)
        if m_old:
            old_path = m_old.group(1).strip()
            # Look ahead for +++ line
            if i + 1 < len(lines):
                m_new = _NEW_FILE_RE.match(lines[i + 1])
                if m_new:
                    new_path = m_new.group(1).strip()
                    current = FilePatch(old_path=old_path, new_path=new_path)
                    patches.append(current)
                    i += 2
                    continue
            # --- without +++ is malformed
            raise ValueError(
                f"Malformed patch: '---' line at line {i + 1} "
                f"without following '+++' line"
            )

        # --- hunk header ---
        m_hunk = _HUNK_RE.match(line)
        if m_hunk and current is not None:
            old_start = int(m_hunk.group(1))
            old_count = int(m_hunk.group(2)) if m_hunk.group(2) is not None else 1
            new_start = int(m_hunk.group(3))
            new_count = int(m_hunk.group(4)) if m_hunk.group(4) is not None else 1
            current_hunk = Hunk(
                old_start=old_start,
                old_count=old_count,
                new_start=new_start,
                new_count=new_count,
            )
            current.hunks.append(current_hunk)
            i += 1
            # Consume hunk body lines
            while i < len(lines):
                hline = lines[i]
                # Stop conditions: next file header, next hunk header, empty line
                # that is NOT part of the hunk
                if _OLD_FILE_RE.match(hline) or _HUNK_RE.match(hline):
                    break
                if hline.startswith("---"):
                    # Could be the start of a new file diff
                    break
                if hline and hline[0] in (" ", "-", "+"):
                    current_hunk.lines.append((hline[0], hline[1:]))
                    i += 1
                elif hline == "":
                    # Blank line could be a context line with no space prefix
                    # (some models omit the leading space). Treat as context.
                    # But if the next line is a header, break.
                    if i + 1 < len(lines) and (
                        _OLD_FILE_RE.match(lines[i + 1])
                        or _HUNK_RE.match(lines[i + 1])
                        or lines[i + 1].startswith("---")
                    ):
                        break
                    current_hunk.lines.append((" ", ""))
                    i += 1
                else:
                    # Unknown prefix -- end of hunk body
                    break
            continue

        # Skip any other lines (e.g. "diff --git ..." preamble, "index ...")
        i += 1

    if not patches:
        raise ValueError("No valid file diffs found in patch")

    return patches


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def _resolve_patch_path(raw: str, workspace: str) -> str:
    """Turn a diff path like ``a/src/foo.py`` or ``b/src/foo.py`` into a
    real filesystem path under *workspace*.

    Strips the leading ``a/`` or ``b/`` prefix that unified diffs use.
    """
    # /dev/null stays as-is (new-file creation)
    if raw == "/dev/null":
        return raw

    p = raw
    for prefix in ("a/", "b/"):
        if p.startswith(prefix):
            p = p[len(prefix):]
            break
    return os.path.join(workspace, p)


# ---------------------------------------------------------------------------
# Hunk application
# ---------------------------------------------------------------------------


def _apply_hunk(file_lines: list[str], hunk: Hunk) -> list[str] | None:
    """Apply a single *Hunk* to *file_lines* (1-indexed line numbers expected).

    Returns the new list of lines, or ``None`` if the hunk could not be applied.
    """
    # Build old-side and new-side from hunk lines
    old_lines: list[str] = []
    new_lines: list[str] = []
    for kind, text in hunk.lines:
        if kind == " ":
            old_lines.append(text)
            new_lines.append(text)
        elif kind == "-":
            old_lines.append(text)
        elif kind == "+":
            new_lines.append(text)

    # Try to locate old_lines in file_lines starting from hunk.old_start
    # (1-based).  We search a window around the expected position.
    search_start = max(0, hunk.old_start - 1 - 3)
    search_end = min(len(file_lines), hunk.old_start - 1 + 3 + 1)

    pos = _find_context(file_lines, old_lines, search_start, search_end)
    if pos is None:
        # Wider fallback: scan entire file
        pos = _find_context(file_lines, old_lines, 0, len(file_lines))
    if pos is None:
        return None

    # Splice
    result = file_lines[:pos] + new_lines + file_lines[pos + len(old_lines):]
    return result


def _find_context(
    file_lines: list[str],
    old_lines: list[str],
    start: int,
    end: int,
) -> int | None:
    """Find the index in *file_lines* where *old_lines* matches.

    Searches from *start* (inclusive) to *end* (exclusive).  Returns ``None``
    if no match is found.
    """
    n = len(old_lines)
    if n == 0:
        # Pure-insertion hunk: just return the expected position
        if 0 <= start < len(file_lines) + 1:
            return start
        return 0

    for i in range(start, max(end - n + 1, start)):
        if i + n > len(file_lines):
            break
        if _lines_match(file_lines[i : i + n], old_lines):
            return i
    return None


def _lines_match(actual: list[str], expected: list[str]) -> bool:
    """Check whether *actual* lines match *expected*, with trailing-newline
    tolerance.
    """
    for a, e in zip(actual, expected):
        a = a.rstrip("\r\n")
        e = e.rstrip("\r\n")
        if a != e:
            return False
    return True


# ---------------------------------------------------------------------------
# ApplyPatchTool
# ---------------------------------------------------------------------------


class ApplyPatchTool(Tool):
    name = "apply_patch"
    description = (
        "Apply a unified diff patch to workspace files. "
        "Supports modifying existing files and creating new files. "
        "When hunk line numbers are slightly off, the tool searches for "
        "matching context to locate the correct position."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "patch": {
                "type": "string",
                "description": "Unified diff patch text to apply",
            },
        },
        "required": ["patch"],
    }

    async def execute(self, arguments: dict[str, Any], workspace: str) -> str:
        patch_text = arguments.get("patch", "")
        if not patch_text:
            return "Error: patch is required"

        try:
            file_patches = parse_patch(patch_text)
        except ValueError as exc:
            return f"Error parsing patch: {exc}"

        results: list[str] = []
        total_hunks = 0

        for fp in file_patches:
            try:
                summary = self._apply_file_patch(fp, workspace)
                results.append(summary)
                total_hunks += fp.hunks.__len__()
            except Exception as exc:
                results.append(f"Error on {fp.new_path}: {exc}")

        header = (
            f"Applied patch: {len(file_patches)} file(s), "
            f"{total_hunks} hunk(s)\n"
        )
        return header + "\n".join(results)

    # -----------------------------------------------------------------------
    # Per-file application
    # -----------------------------------------------------------------------

    def _apply_file_patch(self, fp: FilePatch, workspace: str) -> str:
        is_new_file = fp.old_path == "/dev/null"
        rel_path = (
            _strip_prefix(fp.new_path)
            if is_new_file
            else _strip_prefix(fp.old_path)
        )
        full_path = os.path.join(workspace, rel_path)

        if is_new_file:
            return self._create_new_file(fp, full_path, rel_path)

        # Existing file
        if not os.path.isfile(full_path):
            return f"Error: file not found: {rel_path}"

        with open(full_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()

        file_lines = content.splitlines()

        applied = 0
        for hunk in fp.hunks:
            result = _apply_hunk(file_lines, hunk)
            if result is None:
                return (
                    f"Error: could not apply hunk "
                    f"@@ -{hunk.old_start},{hunk.old_count} "
                    f"+{hunk.new_start},{hunk.new_count}@@ "
                    f"in {rel_path}"
                )
            file_lines = result
            applied += 1

        new_content = "\n".join(file_lines)
        if content.endswith("\n") or not content:
            new_content += "\n"

        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        with open(full_path, "w", encoding="utf-8") as f:
            f.write(new_content)

        return f"{rel_path}: {applied} hunk(s) applied"

    # -----------------------------------------------------------------------
    # New file creation
    # -----------------------------------------------------------------------

    def _create_new_file(
        self, fp: FilePatch, full_path: str, rel_path: str
    ) -> str:
        new_lines: list[str] = []
        for hunk in fp.hunks:
            for kind, text in hunk.lines:
                if kind == "+":
                    new_lines.append(text)
                elif kind == " ":
                    new_lines.append(text)

        content = "\n".join(new_lines)
        if content and not content.endswith("\n"):
            content += "\n"

        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        with open(full_path, "w", encoding="utf-8") as f:
            f.write(content)

        return f"{rel_path}: created (new file, {len(new_lines)} line(s))"


def _strip_prefix(path: str) -> str:
    """Remove leading ``a/`` or ``b/`` from a diff path."""
    for prefix in ("a/", "b/"):
        if path.startswith(prefix):
            return path[len(prefix):]
    return path
