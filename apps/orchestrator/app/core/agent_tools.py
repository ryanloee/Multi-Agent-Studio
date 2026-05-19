"""Agent tools — 6 core tools for the Python agent runner.

Each tool has:
  - A definition dict (name, description, input_schema) compatible with
    Anthropic/OpenAI tool_use format
  - An async execute function: (args: dict, workspace: str) -> str

Tools: shell, read, write, edit, glob, grep
"""

from __future__ import annotations

import asyncio
import glob as glob_mod
import logging
import os
import platform
import re
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any, Iterator

logger = logging.getLogger(__name__)


# ===========================================================================
# Fuzzy Edit Engine — 9 matching strategies (ported from opencode edit.ts)
# ===========================================================================


def levenshtein(a: str, b: str) -> int:
    """Standard DP edit distance."""
    if not a:
        return len(b)
    if not b:
        return len(a)
    n, m = len(a), len(b)
    # Optimize: use two rows instead of full matrix
    prev = list(range(m + 1))
    curr = [0] * (m + 1)
    for i in range(1, n + 1):
        curr[0] = i
        for j in range(1, m + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            curr[j] = min(curr[j - 1] + 1, prev[j] + 1, prev[j - 1] + cost)
        prev, curr = curr, prev
    return prev[m]


# --- Strategy 1: SimpleReplacer ---

def _simple_replacer(find: str, content: str) -> Iterator[str]:
    yield find


# --- Strategy 2: LineTrimmedReplacer ---

def _line_trimmed_replacer(find: str, content: str) -> Iterator[str]:
    search_lines = find.split("\n")
    if search_lines and search_lines[-1] == "":
        search_lines.pop()
    content_lines = content.split("\n")
    n_search = len(search_lines)
    if n_search == 0:
        return

    for i in range(len(content_lines) - n_search + 1):
        match = True
        for j in range(n_search):
            if content_lines[i + j].strip() != search_lines[j].strip():
                match = False
                break
        if match:
            # Yield the original (untrimmed) substring
            start = sum(len(content_lines[k]) + 1 for k in range(i))
            end = start + sum(len(content_lines[i + j]) + 1 for j in range(n_search)) - 1
            yield content[start:end]


# --- Strategy 3: BlockAnchorReplacer ---

SINGLE_CANDIDATE_SIMILARITY_THRESHOLD = 0.0
MULTIPLE_CANDIDATES_SIMILARITY_THRESHOLD = 0.3


def _block_anchor_replacer(find: str, content: str) -> Iterator[str]:
    search_lines = find.split("\n")
    if search_lines and search_lines[-1] == "":
        search_lines.pop()
    if len(search_lines) < 3:
        return

    content_lines = content.split("\n")
    first_anchor = search_lines[0].strip()
    last_anchor = search_lines[-1].strip()
    n_search = len(search_lines)

    # Find all candidate positions
    candidates = []
    for i in range(len(content_lines)):
        if content_lines[i].strip() != first_anchor:
            continue
        for j in range(i + 2, min(i + n_search + 5, len(content_lines))):
            if content_lines[j].strip() == last_anchor:
                candidates.append((i, j))
                break

    if not candidates:
        return

    middle_lines = search_lines[1:-1]
    n_middle = len(middle_lines)

    def _calc_similarity(start: int, end: int) -> float:
        if n_middle == 0:
            return 1.0
        sim = 0.0
        block = content_lines[start + 1:end]
        for k in range(min(n_middle, len(block))):
            orig = block[k].strip()
            target = middle_lines[k].strip()
            if orig == target:
                sim += 1.0
            else:
                max_len = max(len(orig), len(target), 1)
                dist = levenshtein(orig, target)
                sim += 1.0 - dist / max_len
        return sim / n_middle

    if len(candidates) == 1:
        si, ei = candidates[0]
        sim = _calc_similarity(si, ei)
        if sim >= SINGLE_CANDIDATE_SIMILARITY_THRESHOLD:
            start = sum(len(content_lines[k]) + 1 for k in range(si))
            end = start + sum(len(content_lines[si + j]) + 1 for j in range(ei - si + 1)) - 1
            yield content[start:end]
    else:
        best_sim = -1.0
        best_candidate = None
        for si, ei in candidates:
            sim = _calc_similarity(si, ei)
            if sim > best_sim:
                best_sim = sim
                best_candidate = (si, ei)
        if best_sim >= MULTIPLE_CANDIDATES_SIMILARITY_THRESHOLD and best_candidate:
            si, ei = best_candidate
            start = sum(len(content_lines[k]) + 1 for k in range(si))
            end = start + sum(len(content_lines[si + j]) + 1 for j in range(ei - si + 1)) - 1
            yield content[start:end]


# --- Strategy 4: WhitespaceNormalizedReplacer ---

def _normalize_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _normalize_ws_aggressive(text: str) -> str:
    """Collapse ALL whitespace (remove it entirely) for looser comparison."""
    return re.sub(r"\s+", "", text)


def _whitespace_normalized_replacer(find: str, content: str) -> Iterator[str]:
    normalized_find = _normalize_ws(find)
    aggressive_find = _normalize_ws_aggressive(find)

    if "\n" not in find:
        # Single-line matching
        # Build regex: escape non-WS parts, replace WS with \s+
        norm_find = _normalize_ws(find)
        parts = re.split(r"(\s+)", norm_find)
        regex_parts = []
        for part in parts:
            if not part:
                continue
            if part.isspace():
                regex_parts.append(r"\s+")
            else:
                regex_parts.append(re.escape(part))
        pattern = "".join(regex_parts) if regex_parts else re.escape(norm_find)

        for line in content.split("\n"):
            norm_line = _normalize_ws(line)
            if norm_line == normalized_find:
                yield line
            elif normalized_find in norm_line:
                m = re.search(pattern, line)
                if m:
                    yield m.group(0)
            elif aggressive_find and _normalize_ws_aggressive(line) == aggressive_find:
                # Last resort: whitespace-agnostic match — yield trimmed version
                stripped = line.strip()
                if stripped:
                    yield stripped
    else:
        # Multi-line matching
        find_lines = find.split("\n")
        if find_lines and find_lines[-1] == "":
            find_lines.pop()
        content_lines = content.split("\n")
        n = len(find_lines)
        for i in range(len(content_lines) - n + 1):
            block = "\n".join(content_lines[i:i + n])
            if _normalize_ws(block) == normalized_find:
                yield block


# --- Strategy 5: IndentationFlexibleReplacer ---

def _remove_indent(text: str) -> str:
    lines = text.split("\n")
    non_empty = [l for l in lines if l.strip()]
    if not non_empty:
        return text
    min_indent = min(len(l) - len(l.lstrip()) for l in non_empty)
    if min_indent == 0:
        return text
    result = []
    for l in lines:
        if l.strip():
            result.append(l[min_indent:])
        else:
            result.append(l)
    return "\n".join(result)


def _indentation_flexible_replacer(find: str, content: str) -> Iterator[str]:
    normalized_find = _remove_indent(find)
    find_lines = find.split("\n")
    if find_lines and find_lines[-1] == "":
        find_lines.pop()
    content_lines = content.split("\n")
    n = len(find_lines)
    for i in range(len(content_lines) - n + 1):
        block = "\n".join(content_lines[i:i + n])
        if _remove_indent(block) == normalized_find:
            yield block


# --- Strategy 6: EscapeNormalizedReplacer ---

_ESCAPE_MAP = {
    "\\n": "\n", "\\t": "\t", "\\'": "'", '\\"': '"',
    "\\\\": "\\", "\\$": "$", "\r": "\r",
}


def _unescape(s: str) -> str:
    for esc, char in _ESCAPE_MAP.items():
        s = s.replace(esc, char)
    return s


def _escape_normalized_replacer(find: str, content: str) -> Iterator[str]:
    unescaped_find = _unescape(find)
    if unescaped_find in content:
        yield unescaped_find
        return

    find_lines = find.split("\n")
    if find_lines and find_lines[-1] == "":
        find_lines.pop()
    content_lines = content.split("\n")
    n = len(find_lines)
    unescaped_content = _unescape(content)
    if unescaped_find in unescaped_content:
        # Find the corresponding original block
        for i in range(len(content_lines) - n + 1):
            block = "\n".join(content_lines[i:i + n])
            if _unescape(block) == unescaped_find:
                yield block
                return


# --- Strategy 7: TrimmedBoundaryReplacer ---

def _trimmed_boundary_replacer(find: str, content: str) -> Iterator[str]:
    trimmed_find = find.strip()
    if trimmed_find == find:
        return  # Already trimmed, nothing to do

    if trimmed_find in content:
        yield trimmed_find
        return

    find_lines = find.split("\n")
    if find_lines and find_lines[-1] == "":
        find_lines.pop()
    content_lines = content.split("\n")
    n = len(find_lines)
    for i in range(len(content_lines) - n + 1):
        block = "\n".join(content_lines[i:i + n])
        if block.strip() == trimmed_find:
            yield block


# --- Strategy 8: ContextAwareReplacer ---

def _context_aware_replacer(find: str, content: str) -> Iterator[str]:
    search_lines = find.split("\n")
    if search_lines and search_lines[-1] == "":
        search_lines.pop()
    if len(search_lines) < 3:
        return

    content_lines = content.split("\n")
    first_anchor = search_lines[0].strip()
    last_anchor = search_lines[-1].strip()
    n_search = len(search_lines)

    for i in range(len(content_lines)):
        if content_lines[i].strip() != first_anchor:
            continue
        for j in range(i + 2, min(i + n_search + 5, len(content_lines))):
            if content_lines[j].strip() != last_anchor:
                continue
            block_len = j - i + 1
            if block_len != n_search:
                continue
            # Check middle lines: 50% must match when trimmed
            middle_block = content_lines[i + 1:j]
            middle_search = search_lines[1:-1]
            matching = 0
            total_non_empty = 0
            for k in range(min(len(middle_block), len(middle_search))):
                ob = middle_block[k].strip()
                sb = middle_search[k].strip()
                if ob or sb:
                    total_non_empty += 1
                    if ob == sb:
                        matching += 1
            if total_non_empty == 0 or matching / total_non_empty >= 0.5:
                start = sum(len(content_lines[k]) + 1 for k in range(i))
                end = start + sum(len(content_lines[i + k]) + 1 for k in range(n_search)) - 1
                yield content[start:end]
                return


# --- Strategy 9: MultiOccurrenceReplacer ---

def _multi_occurrence_replacer(find: str, content: str) -> Iterator[str]:
    start = 0
    while True:
        idx = content.find(find, start)
        if idx == -1:
            return
        yield find
        start = idx + 1


# --- Main dispatch ---

_REPLACERS = [
    _simple_replacer,
    _line_trimmed_replacer,
    _block_anchor_replacer,
    _whitespace_normalized_replacer,
    _indentation_flexible_replacer,
    _escape_normalized_replacer,
    _trimmed_boundary_replacer,
    _context_aware_replacer,
    _multi_occurrence_replacer,
]


def fuzzy_replace(
    content: str, find: str, replace: str, replace_all: bool = False,
) -> tuple[str, str]:
    """Try 9 fuzzy strategies to find and replace text.

    Returns (new_content, status) where status is:
      - "ok"          — replacement succeeded
      - "not_found"   — no strategy found a match
      - "multi_match" — found multiple matches but replace_all=False
    """
    if not find:
        return content, "not_found"

    not_found = True

    for replacer in _REPLACERS:
        for search in replacer(find, content):
            if search not in content:
                continue
            not_found = False
            idx = content.find(search)
            last_idx = content.rfind(search)
            if idx != last_idx and not replace_all:
                continue  # Multiple matches — try next strategy
            if replace_all:
                return content.replace(search, replace), "ok"
            return content[:idx] + replace + content[idx + len(search):], "ok"

    if not_found:
        return content, "not_found"
    return content, "multi_match"


# ===========================================================================
# Shell Command Analyzer — regex + heuristic (replaces tree-sitter WASM)
# ===========================================================================

_FILE_COMMANDS = frozenset({
    "rm", "cp", "mv", "mkdir", "touch", "chmod", "chown", "cat", "ln", "ls",
    "head", "tail", "less", "more", "tee", "install", "unlink", "rmdir",
})
_BUILD_COMMANDS = frozenset({
    "make", "cmake", "cargo", "go", "mvn", "gradle", "ninja", "meson",
    "zig", "scons",
})
_PKG_COMMANDS = frozenset({
    "pip", "pip3", "npm", "pnpm", "yarn", "bun", "apt", "apt-get", "brew",
    "pacman", "dnf", "yum", "zypper", "snap", "flatpak", "gem", "cargo-install",
})
_VCS_COMMANDS = frozenset({"git", "hg", "svn", "fossil"})
_TEST_COMMANDS = frozenset({"pytest", "jest", "mocha", "vitest", "cargo-test", "go-test"})

_DANGEROUS_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\brm\s+(-[rRf]+\s+)*\/"), "rm on absolute path"),
    (re.compile(r"\bmkfs\b"), "filesystem format"),
    (re.compile(r"\bdd\s+.*of=\/dev\/"), "dd to device"),
    (re.compile(r"chmod\s+777"), "chmod 777"),
    (re.compile(r">\s*\/dev\/s"), "write to block device"),
    (re.compile(r"\bshutdown\b|\breboot\b|\bhalt\b"), "system shutdown"),
    (re.compile(r"\bkill\s+-9\s+1\b"), "kill init"),
    (re.compile(r"\brm\s+-rf\s+\/\s*$"), "rm -rf /"),
]


def _is_dynamic(text: str) -> bool:
    """Detect $(), ${}, backtick commands, $VARIABLE."""
    return bool(re.search(r"\$\(|\$\{|`[^`]+`|\$[A-Z_]", text))


def _split_chained(cmd: str) -> list[str]:
    """Split on &&, ||, ;, | while respecting quotes."""
    segments = []
    current = []
    in_single = False
    in_double = False
    i = 0
    while i < len(cmd):
        c = cmd[i]
        if c == "'" and not in_double:
            in_single = not in_single
            current.append(c)
        elif c == '"' and not in_single:
            in_double = not in_double
            current.append(c)
        elif not in_single and not in_double:
            if c == "&" and i + 1 < len(cmd) and cmd[i + 1] == "&":
                if current:
                    segments.append("".join(current).strip())
                current = []
                i += 2
                continue
            elif c == "|" and i + 1 < len(cmd) and cmd[i + 1] == "|":
                if current:
                    segments.append("".join(current).strip())
                current = []
                i += 2
                continue
            elif c in (";", "|"):
                if current:
                    segments.append("".join(current).strip())
                current = []
                i += 1
                continue
            current.append(c)
        else:
            current.append(c)
        i += 1
    if current:
        seg = "".join(current).strip()
        if seg:
            segments.append(seg)
    return [s for s in segments if s]


def _extract_command_name(parts: list[str]) -> str:
    """Extract the base command name from parsed parts."""
    if not parts:
        return ""
    cmd = parts[0]
    # Handle env assignments: VAR=val cmd ...
    while "=" in cmd and not cmd.startswith("-"):
        if len(parts) > 1:
            parts = parts[1:]
            cmd = parts[0]
        else:
            break
    # Strip path prefix
    base = cmd.rsplit("/", 1)[-1]
    # Handle sudo
    if base == "sudo" and len(parts) > 1:
        return _extract_command_name(parts[1:])
    return base


def _classify_command(cmd_name: str) -> dict[str, Any]:
    """Classify a command into categories."""
    result: dict[str, Any] = {
        "name": cmd_name,
        "category": "other",
        "dangerous": False,
    }
    if cmd_name in _FILE_COMMANDS:
        result["category"] = "file"
    elif cmd_name in _BUILD_COMMANDS:
        result["category"] = "build"
    elif cmd_name in _PKG_COMMANDS:
        result["category"] = "package"
    elif cmd_name in _VCS_COMMANDS:
        result["category"] = "vcs"
    elif cmd_name in _TEST_COMMANDS:
        result["category"] = "test"
    return result


def _extract_file_args(cmd_name: str, args: list[str]) -> list[str]:
    """Extract file path arguments from command args."""
    file_args: list[str] = []
    skip_next = False

    for i, arg in enumerate(args):
        if skip_next:
            skip_next = False
            continue
        if arg.startswith("-"):
            if arg in ("-o", "-f", "--output", "--file", "--config",
                       "--destination", "-d", "-t", "--target"):
                if i + 1 < len(args) and not args[i + 1].startswith("-"):
                    file_args.append(args[i + 1])
                    skip_next = True
            continue
        if _is_dynamic(arg):
            continue
        if cmd_name in _FILE_COMMANDS:
            file_args.append(arg)

    return file_args


def _resolve_path(text: str, workspace: str) -> str:
    """Resolve ~ and relative paths."""
    text = text.strip()
    if text.startswith("~/"):
        text = os.path.expanduser(text)
    elif text == "~":
        text = os.path.expanduser("~")
    p = Path(text)
    if not p.is_absolute():
        p = Path(workspace) / p
    try:
        return str(p.resolve())
    except OSError:
        return str(p)


def _check_dangerous(cmd: str) -> str | None:
    """Check if a command matches dangerous patterns. Returns reason or None."""
    for pattern, reason in _DANGEROUS_PATTERNS:
        if pattern.search(cmd):
            return reason
    return None


def analyze_shell_command(cmd: str, workspace: str) -> dict[str, Any]:
    """Analyze a shell command for safety and categorization.

    Returns dict with:
      - commands: list of command info dicts
      - file_args: resolved file paths referenced
      - dangerous: bool
      - danger_reason: str or None
    """
    result: dict[str, Any] = {
        "commands": [],
        "file_args": [],
        "dangerous": False,
        "danger_reason": None,
    }

    danger = _check_dangerous(cmd)
    if danger:
        result["dangerous"] = True
        result["danger_reason"] = danger

    segments = _split_chained(cmd)
    for segment in segments:
        try:
            parts = shlex.split(segment)
        except ValueError:
            # Malformed quoting — skip analysis for this segment
            continue
        if not parts:
            continue

        cmd_name = _extract_command_name(list(parts))
        info = _classify_command(cmd_name)
        info["raw"] = segment
        result["commands"].append(info)

        file_args = _extract_file_args(cmd_name, parts[1:])
        for fpath in file_args:
            resolved = _resolve_path(fpath, workspace)
            if resolved not in result["file_args"]:
                result["file_args"].append(resolved)

    return result


# ---------------------------------------------------------------------------
# Tool definitions (Anthropic/OpenAI tool_use format)
# ---------------------------------------------------------------------------

TOOLS: list[dict[str, Any]] = [
    {
        "name": "shell",
        "description": (
            "Execute a shell command in the workspace directory. "
            "Use for running tests, installing packages, building, git operations, etc. "
            "Commands have a 120-second timeout."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The shell command to execute",
                },
            },
            "required": ["command"],
        },
    },
    {
        "name": "read",
        "description": (
            "Read a file or list a directory. For files, returns the content with line numbers. "
            "For directories, returns a listing of files and subdirectories. "
            "Use offset and limit to read parts of large files."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File or directory path (relative to workspace or absolute)",
                },
                "offset": {
                    "type": "integer",
                    "description": "Line number to start reading from (0-based, default 0)",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of lines to read (default 2000)",
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "write",
        "description": (
            "Write content to a file. Creates the file and any missing parent directories. "
            "Overwrites existing files completely."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File path (relative to workspace or absolute)",
                },
                "content": {
                    "type": "string",
                    "description": "The content to write to the file",
                },
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "edit",
        "description": (
            "Replace text in a file. Finds the old_string and replaces it with new_string. "
            "The old_string must be an exact match of existing text in the file. "
            "If replace_all is true, replaces all occurrences."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File path (relative to workspace or absolute)",
                },
                "old_string": {
                    "type": "string",
                    "description": "The exact text to find and replace",
                },
                "new_string": {
                    "type": "string",
                    "description": "The replacement text",
                },
                "replace_all": {
                    "type": "boolean",
                    "description": "Replace all occurrences (default false)",
                },
            },
            "required": ["path", "old_string", "new_string"],
        },
    },
    {
        "name": "glob",
        "description": (
            "Find files matching a glob pattern. Returns matching file paths sorted by "
            "modification time. Use for finding files by name or extension."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Glob pattern (e.g. '**/*.py', 'src/**/*.ts')",
                },
                "path": {
                    "type": "string",
                    "description": "Directory to search in (default: workspace root)",
                },
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "grep",
        "description": (
            "Search for a pattern in file contents. Returns matching lines with file paths "
            "and line numbers. Supports regex patterns."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Regex pattern to search for",
                },
                "path": {
                    "type": "string",
                    "description": "File or directory to search in (default: workspace root)",
                },
                "glob": {
                    "type": "string",
                    "description": "File glob filter (e.g. '*.py', '*.{ts,tsx}')",
                },
            },
            "required": ["pattern"],
        },
    },
]


# ---------------------------------------------------------------------------
# Tool execution
# ---------------------------------------------------------------------------


def _resolve_path(workspace: str, path_str: str) -> Path:
    """Resolve a path relative to workspace, or use absolute path."""
    p = Path(path_str)
    if p.is_absolute():
        return p
    return Path(workspace) / p


def _shell_prefix() -> list[str]:
    """Return the shell invocation for the current OS."""
    if platform.system() == "Windows":
        git_bash = shutil.which("bash")
        if git_bash:
            return [git_bash, "-c"]
        return ["cmd", "/C"]
    return ["/bin/bash", "-c"]


async def execute_tool(
    name: str,
    args: dict[str, Any],
    workspace: str,
) -> str:
    """Execute a tool by name and return the result string."""
    handler = _TOOL_HANDLERS.get(name)
    if handler is None:
        return f"Error: unknown tool '{name}'"
    try:
        return await handler(args, workspace)
    except Exception as exc:
        logger.warning("Tool %s failed: %s", name, exc, exc_info=True)
        return f"Error executing {name}: {exc}"


# ---------------------------------------------------------------------------
# Individual tool implementations
# ---------------------------------------------------------------------------


async def _tool_shell(args: dict[str, Any], workspace: str) -> str:
    """Execute a shell command."""
    command = args.get("command", "")
    if not command.strip():
        return "Error: empty command"

    # Analyze command for safety
    analysis = analyze_shell_command(command, workspace)
    if analysis["dangerous"]:
        logger.warning("Potentially dangerous command detected [%s]: %s",
                        analysis["danger_reason"], command)

    shell = _shell_prefix()

    def _run():
        return subprocess.run(
            [*shell, command],
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=120,
        )

    try:
        result = await asyncio.to_thread(_run)
    except subprocess.TimeoutExpired:
        return "Error: command timed out after 120 seconds"

    parts: list[str] = []
    if result.stdout:
        parts.append(result.stdout)
    if result.stderr:
        parts.append(f"[stderr]\n{result.stderr}")
    if result.returncode != 0:
        parts.append(f"[exit code: {result.returncode}]")

    output = "\n".join(parts) if parts else "(no output)"

    # Truncate very long output (UTF-8 safe)
    if len(output) > 50000:
        output = output[:25000] + f"\n\n... [truncated, {len(output)} chars total] ...\n\n" + output[-25000:]

    return output


async def _tool_read(args: dict[str, Any], workspace: str) -> str:
    """Read a file or list a directory."""
    path_str = args.get("path", "")
    if not path_str:
        return "Error: path is required"

    target = _resolve_path(workspace, path_str)

    if not target.exists():
        return f"Error: path does not exist: {target}"

    if target.is_dir():
        # List directory
        try:
            entries = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
            lines: list[str] = []
            for entry in entries[:500]:  # limit to 500 entries
                if entry.is_dir():
                    lines.append(f"  {entry.name}/")
                else:
                    size = entry.stat().st_size
                    lines.append(f"  {entry.name}  ({size} bytes)")
            if len(entries) > 500:
                lines.append(f"  ... ({len(entries) - 500} more entries)")
            return "\n".join(lines) if lines else "(empty directory)"
        except PermissionError:
            return f"Error: permission denied: {target}"

    # Read file
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except PermissionError:
        return f"Error: permission denied: {target}"
    except OSError as exc:
        return f"Error reading file: {exc}"

    lines = text.split("\n")
    offset = int(args.get("offset", 0))
    limit = int(args.get("limit", 2000))

    if offset > 0:
        lines = lines[offset:]
    if limit > 0:
        lines = lines[:limit]

    # Add line numbers
    numbered = []
    for i, line in enumerate(lines, start=offset + 1):
        numbered.append(f"{i:6d}\t{line}")

    return "\n".join(numbered)


async def _tool_write(args: dict[str, Any], workspace: str) -> str:
    """Write content to a file."""
    path_str = args.get("path", "")
    content = args.get("content", "")

    if not path_str:
        return "Error: path is required"

    target = _resolve_path(workspace, path_str)
    target.parent.mkdir(parents=True, exist_ok=True)

    try:
        target.write_text(content, encoding="utf-8")
        return f"Successfully wrote {len(content)} bytes to {target}"
    except OSError as exc:
        return f"Error writing file: {exc}"


async def _tool_edit(args: dict[str, Any], workspace: str) -> str:
    """Replace text in a file using fuzzy matching."""
    path_str = args.get("path", "")
    old_string = args.get("old_string", "")
    new_string = args.get("new_string", "")
    replace_all = args.get("replace_all", False)

    if not path_str:
        return "Error: path is required"
    if not old_string:
        return "Error: old_string is required"

    target = _resolve_path(workspace, path_str)

    if not target.exists():
        return f"Error: file does not exist: {target}"

    try:
        text = target.read_text(encoding="utf-8")
    except OSError as exc:
        return f"Error reading file: {exc}"

    new_text, status = fuzzy_replace(text, old_string, new_string, replace_all)

    if status == "not_found":
        return (
            f"Error: old_string not found in {target}. "
            "Make sure it matches exactly or is close enough."
        )
    if status == "multi_match":
        return (
            f"Error: found multiple matches for old_string in {target}. "
            "Use replace_all=true to replace all, or provide more context to match uniquely."
        )

    # Count replacements
    if replace_all:
        count = text.count(old_string) if old_string in text else 1
    else:
        count = 1

    try:
        target.write_text(new_text, encoding="utf-8")
        return f"Successfully replaced {count} occurrence(s) in {target}"
    except OSError as exc:
        return f"Error writing file: {exc}"


async def _tool_glob(args: dict[str, Any], workspace: str) -> str:
    """Find files matching a glob pattern."""
    pattern = args.get("pattern", "")
    if not pattern:
        return "Error: pattern is required"

    search_dir = args.get("path", workspace)
    search_path = _resolve_path(workspace, search_dir)

    if not search_path.is_dir():
        return f"Error: directory does not exist: {search_path}"

    try:
        matches = sorted(
            search_path.glob(pattern),
            key=lambda p: p.stat().st_mtime if p.exists() else 0,
            reverse=True,
        )
    except Exception as exc:
        return f"Error: {exc}"

    if not matches:
        return "(no matches)"

    lines: list[str] = []
    for m in matches[:200]:  # limit to 200 results
        try:
            rel = m.relative_to(search_path)
        except ValueError:
            rel = m
        if m.is_dir():
            lines.append(f"  {rel}/")
        else:
            lines.append(f"  {rel}")

    if len(matches) > 200:
        lines.append(f"  ... ({len(matches) - 200} more matches)")

    return "\n".join(lines)


async def _tool_grep(args: dict[str, Any], workspace: str) -> str:
    """Search for a pattern in file contents."""
    import re

    pattern = args.get("pattern", "")
    if not pattern:
        return "Error: pattern is required"

    search_path = args.get("path", workspace)
    target = _resolve_path(workspace, search_path)
    file_glob = args.get("glob", "")

    try:
        regex = re.compile(pattern)
    except re.error as exc:
        return f"Error: invalid regex pattern: {exc}"

    results: list[str] = []
    max_results = 200

    def _search_file(filepath: Path) -> None:
        try:
            text = filepath.read_text(encoding="utf-8", errors="replace")
        except (OSError, PermissionError):
            return
        for i, line in enumerate(text.split("\n"), 1):
            if regex.search(line):
                try:
                    rel = filepath.relative_to(target)
                except ValueError:
                    rel = filepath
                results.append(f"  {rel}:{i}: {line.rstrip()[:500]}")
                if len(results) >= max_results:
                    return

    if target.is_file():
        _search_file(target)
    elif target.is_dir():
        skip_dirs = {".git", "node_modules", "__pycache__", ".venv", "venv", ".sandboxes"}
        for root, dirs, files in os.walk(target):
            dirs[:] = [d for d in dirs if d not in skip_dirs]
            for fname in files:
                if len(results) >= max_results:
                    break
                fpath = Path(root) / fname
                if file_glob:
                    import fnmatch
                    if not fnmatch.fnmatch(fname, file_glob):
                        continue
                # Skip binary files
                if fpath.suffix in {".pyc", ".pyo", ".so", ".dll", ".exe", ".bin", ".jpg", ".png", ".gif", ".pdf", ".zip", ".tar", ".gz"}:
                    continue
                _search_file(fpath)
            if len(results) >= max_results:
                break
    else:
        return f"Error: path does not exist: {target}"

    if not results:
        return "(no matches)"

    header = f"Found {len(results)} match(es)" + (" (limited)" if len(results) >= max_results else "") + ":"
    return header + "\n" + "\n".join(results)


# ---------------------------------------------------------------------------
# Handler dispatch
# ---------------------------------------------------------------------------

_TOOL_HANDLERS = {
    "shell": _tool_shell,
    "read": _tool_read,
    "write": _tool_write,
    "edit": _tool_edit,
    "glob": _tool_glob,
    "grep": _tool_grep,
}


# ---------------------------------------------------------------------------
# System prompts per agent type
# ---------------------------------------------------------------------------

AGENT_SYSTEM_PROMPTS: dict[str, str] = {
    "coder": (
        "You are a coding agent. Your job is to write and modify code files. "
        "Use the available tools to read existing code, make changes, and verify your work. "
        "Be precise with edits — use the edit tool for surgical changes, write for new files. "
        "Always check your changes compile or run correctly."
    ),
    "worker": (
        "You are a coding agent. Your job is to write and modify code files. "
        "Use the available tools to read existing code, make changes, and verify your work. "
        "Be precise with edits — use the edit tool for surgical changes, write for new files. "
        "Always check your changes compile or run correctly."
    ),
    "explore": (
        "You are a code exploration agent. Your job is to search and analyze the codebase. "
        "Use glob to find files, grep to search content, and read to examine code. "
        "Report your findings clearly and concisely."
    ),
    "scout": (
        "You are a code exploration agent. Your job is to search and analyze the codebase. "
        "Use glob to find files, grep to search content, and read to examine code. "
        "Report your findings clearly and concisely."
    ),
    "shell": (
        "You are a shell command agent. Your job is to execute commands and report results. "
        "Use the shell tool to run tests, build commands, install packages, etc. "
        "Report output clearly and handle errors gracefully."
    ),
    "tester": (
        "You are a testing agent. Your job is to run tests and verify code correctness. "
        "Use the shell tool to run test commands. Report results clearly."
    ),
    "review": (
        "You are a code review agent. Review code changes and provide constructive feedback. "
        "Read the changed files, analyze them for bugs, style issues, and improvements."
    ),
    "plan": (
        "You are a planning agent. Analyze the project and create execution plans. "
        "Use exploration tools to understand the codebase, then produce a clear plan."
    ),
    "design": (
        "You are a design agent. Create architecture and design documents. "
        "Explore the codebase to understand the current architecture, then produce design guidance."
    ),
    "merge": (
        "You are a merge agent. Your job is to integrate code changes from multiple sources. "
        "Read the relevant files, resolve conflicts, and produce a coherent result."
    ),
}


def get_system_prompt(agent_type: str, custom_prompt: str = "") -> str:
    """Get the system prompt for an agent type, with optional custom override."""
    base = AGENT_SYSTEM_PROMPTS.get(agent_type, AGENT_SYSTEM_PROMPTS["coder"])

    parts = [base]

    if custom_prompt:
        parts.append(custom_prompt)

    parts.append(
        "\n## Available Tools\n"
        "You have access to the following tools. Use them to accomplish your task.\n"
        "- **shell**: Execute shell commands (tests, builds, git, etc.)\n"
        "- **read**: Read files or list directories (with line numbers)\n"
        "- **write**: Write content to files (creates parent dirs)\n"
        "- **edit**: Replace exact text in files (surgical edits)\n"
        "- **glob**: Find files by pattern (e.g. '**/*.py')\n"
        "- **grep**: Search file contents by regex\n"
    )

    return "\n\n".join(parts)
