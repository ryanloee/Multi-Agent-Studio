"""Tool call repair — fix common LLM formatting mistakes in tool calls."""
from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# (a) Tool-name aliases: LLM sometimes outputs CamelCase or alternate names
# ---------------------------------------------------------------------------
_TOOL_NAME_ALIASES: dict[str, str] = {
    "Grep": "grep",
    "ReadFile": "read",
    "Readfile": "read",
    "read_file": "read",
    "Read": "read",
    "WriteFile": "write",
    "Writefile": "write",
    "write_file": "write",
    "Write": "write",
    "EditFile": "edit",
    "Editfile": "edit",
    "edit_file": "edit",
    "Edit": "edit",
    "Shell": "shell",
    "shell_exec": "shell",
    "exec": "shell",
    "run": "shell",
    "Glob": "glob",
    "find": "glob",
    "Find": "glob",
    "Search": "grep",
    "search": "grep",
}

# ---------------------------------------------------------------------------
# (b) Per-tool parameter-name aliases: common misnamings
# ---------------------------------------------------------------------------
_PARAM_ALIASES: dict[str, dict[str, str]] = {
    # Tools that take 'path'
    "read": {
        "file_path": "path",
        "filepath": "path",
        "file": "path",
        "filename": "path",
    },
    "write": {
        "file_path": "path",
        "filepath": "path",
        "file": "path",
        "filename": "path",
    },
    "edit": {
        "file_path": "path",
        "filepath": "path",
        "file": "path",
        "filename": "path",
        "content": "new_text",
        "replacement": "new_text",
        "replace_text": "new_text",
        "text": "old_text",
        "search": "old_text",
        "search_text": "old_text",
        "find": "old_text",
        "find_text": "old_text",
    },
    "grep": {
        "query": "pattern",
        "search": "pattern",
        "regex": "pattern",
        "search_term": "pattern",
        "search_string": "pattern",
        "file_path": "path",
        "filepath": "path",
        "file": "path",
        "dir": "path",
        "directory": "path",
    },
    "shell": {
        "cmd": "command",
        "run": "command",
        "script": "command",
    },
    "glob": {
        "glob_pattern": "pattern",
        "file_pattern": "pattern",
        "match": "pattern",
    },
}

# ---------------------------------------------------------------------------
# (d) Numeric fields per tool that should be converted from string to int
# ---------------------------------------------------------------------------
_NUMERIC_FIELDS: dict[str, set[str]] = {
    "read": {"offset", "limit"},
    "grep": {"context"},
    "shell": {"timeout"},
}


def _fix_tool_name(name: str) -> tuple[str, list[str]]:
    """Fix tool name casing/alias issues."""
    repairs: list[str] = []
    if name in _TOOL_NAME_ALIASES:
        corrected = _TOOL_NAME_ALIASES[name]
        repairs.append(f"tool name: '{name}' -> '{corrected}'")
        return corrected, repairs
    return name, repairs


def _fix_param_names(tool_name: str, args: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Fix parameter name aliases for a given tool."""
    repairs: list[str] = []
    aliases = _PARAM_ALIASES.get(tool_name, {})
    if not aliases:
        return args, repairs

    fixed: dict[str, Any] = {}
    for key, value in args.items():
        if key in aliases:
            corrected = aliases[key]
            # Don't overwrite if the correct key already exists
            if corrected not in args:
                repairs.append(f"param: '{key}' -> '{corrected}'")
                fixed[corrected] = value
            else:
                # Keep original key; the correct one is already present
                fixed[key] = value
        else:
            fixed[key] = value
    return fixed, repairs


def _fix_json_in_values(args: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Attempt to repair JSON formatting issues in string argument values.

    Handles:
    - Single quotes used instead of double quotes
    - Trailing commas before closing braces/brackets
    - Truncated JSON (best-effort: tries to close open structures)
    """
    repairs: list[str] = []
    fixed: dict[str, Any] = {}

    for key, value in args.items():
        if not isinstance(value, str):
            fixed[key] = value
            continue

        original = value
        repaired = value

        # Only attempt repairs on strings that look like they might contain JSON
        stripped = value.strip()
        if not stripped.startswith(("{", "[")):
            fixed[key] = value
            continue

        # (c.3) Replace single quotes with double quotes
        if "'" in repaired and '"' not in repaired:
            repaired = repaired.replace("'", '"')
            if repaired != original:
                repairs.append(f"json_fix[{key}]: replaced single quotes with double quotes")

        # (c.2) Remove trailing commas before } or ]
        import re
        new_val = re.sub(r",\s*([}\]])", r"\1", repaired)
        if new_val != repaired:
            repairs.append(f"json_fix[{key}]: removed trailing comma(s)")
            repaired = new_val

        # (c.1) Attempt to parse; if it fails, try to fix truncated JSON
        try:
            json.loads(repaired)
        except json.JSONDecodeError:
            # Attempt truncation repair: count open brackets/braces and close them
            repaired = _attempt_close_json(repaired, key, repairs)

        # Validate the final result is parseable JSON
        try:
            json.loads(repaired)
            if repaired != original:
                fixed[key] = repaired
            else:
                fixed[key] = value
        except json.JSONDecodeError:
            # Still broken — keep original value, don't crash
            if repaired != original:
                logger.debug("Could not repair JSON in arg '%s', keeping original", key)
            fixed[key] = value

    return fixed, repairs


def _attempt_close_json(s: str, key: str, repairs: list[str]) -> str:
    """Try to close unclosed JSON structures in a string."""
    open_braces = 0
    open_brackets = 0
    in_string = False
    escape_next = False

    for ch in s:
        if escape_next:
            escape_next = False
            continue
        if ch == "\\":
            escape_next = True
            continue
        if ch == '"' and not escape_next:
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            open_braces += 1
        elif ch == "}":
            open_braces -= 1
        elif ch == "[":
            open_brackets += 1
        elif ch == "]":
            open_brackets -= 1

    # If we're in an unterminated string, close it
    suffix = ""
    if in_string:
        suffix += '"'
        # Also need to account for the value being a string containing JSON
        # After closing the string, we may need a colon, comma, etc.
        # Best-effort: just close the string and then close structures

    suffix += "]" * max(0, open_brackets) + "}" * max(0, open_braces)

    if suffix:
        repairs.append(f"json_fix[{key}]: attempted to close truncated JSON")
        return s + suffix

    return s


def _fix_numeric_types(tool_name: str, args: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Convert string numbers to actual numbers for known numeric fields."""
    repairs: list[str] = []
    numeric_fields = _NUMERIC_FIELDS.get(tool_name, set())
    if not numeric_fields:
        return args, repairs

    fixed: dict[str, Any] = {}
    for key, value in args.items():
        if key in numeric_fields and isinstance(value, str):
            try:
                converted = int(value)
                fixed[key] = converted
                repairs.append(f"type_fix[{key}]: converted string '{value}' to int {converted}")
            except ValueError:
                fixed[key] = value
        else:
            fixed[key] = value
    return fixed, repairs


def repair_tool_call(name: str, arguments: dict) -> tuple[str, dict, list[str]]:
    """Repair common LLM tool-call formatting errors.

    Parameters
    ----------
    name : str
        The tool name from the LLM output.
    arguments : dict
        The tool arguments from the LLM output.

    Returns
    -------
    tuple[str, dict, list[str]]
        ``(repaired_name, repaired_args, repairs_made)`` where
        *repairs_made* is a list of human-readable description strings
        describing each repair applied.  Empty list means no repairs were
        needed.
    """
    if not isinstance(arguments, dict):
        arguments = {}

    repairs: list[str] = []

    # (a) Fix tool name
    name, name_repairs = _fix_tool_name(name)
    repairs.extend(name_repairs)

    # (b) Fix parameter names
    arguments, param_repairs = _fix_param_names(name, arguments)
    repairs.extend(param_repairs)

    # (c) Fix JSON in string values
    arguments, json_repairs = _fix_json_in_values(arguments)
    repairs.extend(json_repairs)

    # (d) Fix numeric type conversions
    arguments, type_repairs = _fix_numeric_types(name, arguments)
    repairs.extend(type_repairs)

    return name, arguments, repairs
