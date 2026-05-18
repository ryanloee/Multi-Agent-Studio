"""Planner research tools — real execution tools for the planner chat.

These tools let the planner model search the web, read project files, grep
across the codebase, and fetch URLs during the planning phase.  They are
executed server-side and the results are fed back into the LLM conversation
so the model can produce a better-informed task plan.

The multi-turn tool execution loop (`run_planner_tool_loop`) is also here.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import re
import time
from fnmatch import fnmatch
from pathlib import Path
from typing import AsyncGenerator
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SKIP_DIRS = frozenset({
    ".git", "node_modules", "__pycache__", ".mas", ".sandboxes",
    ".venv", "venv", "dist", "build", ".next", "target",
    ".tox", ".mypy_cache", ".pytest_cache", ".ruff_cache",
})

BINARY_EXTENSIONS = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".svg",
    ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar",
    ".exe", ".dll", ".so", ".dylib", ".o", ".a",
    ".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx",
    ".mp3", ".mp4", ".avi", ".mov", ".mkv", ".flv",
    ".woff", ".woff2", ".ttf", ".eot", ".otf",
    ".pyc", ".pyo", ".class", ".obj", ".pdb",
    ".sqlite", ".db", ".wasm",
})

RESEARCH_TOOL_NAMES = frozenset({
    "planner_web_search",
    "planner_read_file",
    "planner_grep_files",
    "planner_web_fetch",
})

MAX_TOOL_ITERATIONS = 15
TOOL_EXECUTION_TIMEOUT = 30  # seconds


# ---------------------------------------------------------------------------
# Tool schema definitions
# ---------------------------------------------------------------------------

def planner_research_tools() -> list[dict]:
    """Return the active research tool schemas (Anthropic format).

    NOTE: planner_web_search is temporarily disabled (network unreliability).
    The code is kept for easy re-enablement when a stable search provider is available.
    """
    return [
        # --- Temporarily disabled: web search ---
        # {
        #     "name": "planner_web_search",
        #     "description": (
        #         "搜索网络以获取关于技术方案、API 文档、库、最佳实践等信息。"
        #         "返回相关结果的标题、URL 和摘要。"
        #     ),
        #     "input_schema": {
        #         "type": "object",
        #         "properties": {
        #             "query": {
        #                 "type": "string",
        #                 "description": "搜索查询字符串",
        #             },
        #             "max_results": {
        #                 "type": "integer",
        #                 "description": "最大返回结果数（默认 5，最大 10）",
        #                 "default": 5,
        #             },
        #         },
        #         "required": ["query"],
        #     },
        # },
        {
            "name": "planner_read_file",
            "description": (
                "读取项目工作区中的文件内容。用于检查现有代码、配置文件或文档。"
                "路径必须相对于项目根目录。"
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "项目工作区中的文件相对路径（如 'src/index.ts', 'package.json'）",
                    },
                    "start_line": {
                        "type": "integer",
                        "description": "起始行号（从 1 开始，默认 1）",
                    },
                    "end_line": {
                        "type": "integer",
                        "description": "结束行号（包含，默认到文件末尾）",
                    },
                },
                "required": ["path"],
            },
        },
        {
            "name": "planner_grep_files",
            "description": (
                "在项目文件中搜索文本模式或正则表达式。"
                "返回匹配的行，包含文件路径和行号。"
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "要搜索的文本模式或正则表达式",
                    },
                    "file_glob": {
                        "type": "string",
                        "description": "文件匹配模式（如 '*.py', '*.ts', 'src/**'）。默认搜索所有文件",
                        "default": "*",
                    },
                    "case_insensitive": {
                        "type": "boolean",
                        "description": "是否忽略大小写（默认 false）",
                        "default": False,
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "最大返回匹配行数（默认 30，最大 50）",
                        "default": 30,
                    },
                },
                "required": ["pattern"],
            },
        },
        {
            "name": "planner_web_fetch",
            "description": (
                "抓取并读取指定 URL 的内容。用于阅读文档页面、API 参考等。"
                "自动将 HTML 转为纯文本。"
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "要抓取的 URL",
                    },
                    "max_length": {
                        "type": "integer",
                        "description": "最大内容长度（字符数，默认 10000）",
                        "default": 10000,
                    },
                },
                "required": ["url"],
            },
        },
    ]


# ---------------------------------------------------------------------------
# Security helpers
# ---------------------------------------------------------------------------

def validate_workspace_path(path: str, workspace_directory: str) -> Path | None:
    """Validate and resolve a path within the workspace.

    Returns the resolved absolute Path if safe, or None if the path escapes
    the workspace directory.
    """
    if not workspace_directory:
        return None
    ws_root = Path(workspace_directory).expanduser().resolve()
    target = (ws_root / path).resolve()
    try:
        target.relative_to(ws_root)
    except ValueError:
        return None
    return target


def _is_binary_extension(path: Path) -> bool:
    return path.suffix.lower() in BINARY_EXTENSIONS


def _is_private_url(url: str) -> bool:
    """Block private/internal IPs to prevent SSRF."""
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname
        if not hostname:
            return True
        if hostname in ("localhost", "127.0.0.1", "::1"):
            return True
        addr = ipaddress.ip_address(hostname)
        return addr.is_private or addr.is_loopback or addr.is_reserved
    except ValueError:
        return False


def _strip_html(html: str) -> str:
    """Crude HTML-to-text: remove tags, decode entities."""
    text = re.sub(r"<[^>]+>", "", html)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"&quot;", '"', text)
    text = re.sub(r"&#39;", "'", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ---------------------------------------------------------------------------
# Tool execution functions
# ---------------------------------------------------------------------------

async def execute_web_search(query: str, max_results: int = 5) -> str:
    """Execute a web search.

    Provider priority (first success wins):
    1. Exa MCP  — https://mcp.exa.ai/mcp  (requires EXA_API_KEY env var)
    2. Parallel MCP — https://search.parallel.ai/mcp  (requires PARALLEL_API_KEY)
    3. DuckDuckGo local library (no key needed, may be rate-limited)
    """
    max_results = max(1, min(max_results, 10))
    import os

    exa_key = os.environ.get("EXA_API_KEY", "")
    parallel_key = os.environ.get("PARALLEL_API_KEY", "")

    # --- Provider 1: Exa MCP ---
    if exa_key:
        try:
            result = await _mcp_search_exa(query, max_results, exa_key)
            if result:
                return result
            logger.info("Exa returned empty, trying next provider")
        except Exception as exc:
            logger.warning("Exa search failed: %s", exc)

    # --- Provider 2: Parallel MCP ---
    if parallel_key:
        try:
            result = await _mcp_search_parallel(query, parallel_key)
            if result:
                return result
            logger.info("Parallel returned empty, trying next provider")
        except Exception as exc:
            logger.warning("Parallel search failed: %s", exc)

    # --- Provider 3: DuckDuckGo local ---
    try:
        return await _search_duckduckgo(query, max_results)
    except ImportError:
        return "[Error: no search provider available. Set EXA_API_KEY or install duckduckgo-search]"
    except Exception as exc:
        return f"[Search error: {exc}]"


async def _mcp_search_exa(query: str, max_results: int, api_key: str) -> str | None:
    """Call Exa search via MCP JSON-RPC protocol."""
    import httpx

    url = f"https://mcp.exa.ai/mcp?exaApiKey={api_key}"
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "web_search_exa",
            "arguments": {
                "query": query,
                "type": "auto",
                "numResults": max_results,
                "livecrawl": "fallback",
            },
        },
    }
    async with httpx.AsyncClient(timeout=25) as client:
        resp = await client.post(
            url,
            json=payload,
            headers={"Accept": "application/json, text/event-stream"},
        )
        resp.raise_for_status()
        return _parse_mcp_response(resp.text)


async def _mcp_search_parallel(query: str, api_key: str) -> str | None:
    """Call Parallel search via MCP JSON-RPC protocol."""
    import httpx

    url = "https://search.parallel.ai/mcp"
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "web_search",
            "arguments": {
                "objective": query,
                "search_queries": [query],
            },
        },
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=25) as client:
        resp = await client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        return _parse_mcp_response(resp.text)


def _parse_mcp_response(body: str) -> str | None:
    """Parse MCP JSON-RPC response, handling both JSON and SSE formats."""
    text = _extract_mcp_text(body)
    if text:
        return text[:4000]
    return None


def _extract_mcp_text(body: str) -> str | None:
    """Extract text content from MCP response (direct JSON or SSE lines)."""
    for chunk in [body.strip()] + [line[6:] for line in body.split("\n") if line.startswith("data: ")]:
        chunk = chunk.strip()
        if not chunk.startswith("{"):
            continue
        try:
            data = json.loads(chunk)
        except json.JSONDecodeError:
            continue
        result = data.get("result")
        if not isinstance(result, dict):
            continue
        content = result.get("content")
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text" and item.get("text"):
                    return item["text"]
    return None


async def _search_duckduckgo(query: str, max_results: int) -> str:
    """DuckDuckGo search via local library (fallback, no API key needed)."""
    def _search() -> list[dict]:
        from duckduckgo_search import DDGS
        ddgs = DDGS(timeout=20)
        return list(ddgs.text(query, max_results=max_results))

    raw = await asyncio.to_thread(_search)
    if not raw:
        import time as _t
        _t.sleep(2)
        raw = await asyncio.to_thread(_search)
    if not raw:
        return "No search results found."
    results = []
    for i, r in enumerate(raw[:max_results], 1):
        title = r.get("title", "")
        href = r.get("href", "")
        body = r.get("body", "")[:300]
        results.append(f"{i}. [{title}]({href})\n   {body}")
    return "\n\n".join(results)[:4000]


async def execute_read_file(
    path: str,
    workspace_directory: str,
    start_line: int | None = None,
    end_line: int | None = None,
) -> str:
    """Read a file from the workspace directory."""
    resolved = validate_workspace_path(path, workspace_directory)
    if resolved is None:
        return f"[Error: path '{path}' is outside the workspace directory]"
    if not resolved.exists():
        return f"[Error: file not found: {path}]"
    if not resolved.is_file():
        return f"[Error: not a file: {path}]"
    if _is_binary_extension(resolved):
        return f"[Error: binary file, cannot read: {path}]"

    try:
        content = await asyncio.to_thread(resolved.read_text, "utf-8")
    except UnicodeDecodeError:
        return f"[Error: file is not UTF-8 text: {path}]"
    except OSError as exc:
        return f"[Error reading file: {exc}]"

    # Check for null bytes (binary content without recognized extension)
    if "\x00" in content[:8192]:
        return f"[Error: file appears to be binary: {path}]"

    lines = content.splitlines()
    total_lines = len(lines)

    s = max(1, start_line or 1) - 1  # 0-based
    e = min(total_lines, end_line or total_lines)

    # Cap at 500 lines
    if e - s > 500:
        e = s + 500

    selected = lines[s:e]
    numbered = [f"{i + s + 1:4d} | {line}" for i, line in enumerate(selected)]

    header = f"File: {path} (lines {s + 1}-{e} of {total_lines})"
    result = header + "\n" + "\n".join(numbered)

    if len(result) > 50000:
        result = result[:50000] + "\n... [truncated]"
    return result


async def execute_grep_files(
    pattern: str,
    workspace_directory: str,
    file_glob: str = "*",
    case_insensitive: bool = False,
    max_results: int = 30,
) -> str:
    """Search for a text pattern across project files."""
    resolved_root = Path(workspace_directory).expanduser().resolve()
    if not resolved_root.is_dir():
        return "[Error: workspace directory does not exist]"

    max_results = max(1, min(max_results, 50))
    flags = re.IGNORECASE if case_insensitive else 0
    try:
        regex = re.compile(pattern, flags)
    except re.error as exc:
        return f"[Error: invalid regex pattern: {exc}]"

    matches: list[str] = []

    def _search() -> None:
        try:
            for fpath in sorted(resolved_root.rglob("*")):
                if len(matches) >= max_results:
                    break
                if not fpath.is_file():
                    continue
                rel = fpath.relative_to(resolved_root)
                if any(p in SKIP_DIRS for p in rel.parts):
                    continue
                if _is_binary_extension(fpath):
                    continue
                if not fnmatch(str(rel), file_glob) and file_glob != "*":
                    continue
                try:
                    chunk = fpath.read_bytes()
                    if b"\x00" in chunk[:8192]:
                        continue
                    text = chunk.decode("utf-8", errors="ignore")
                except OSError:
                    continue
                for lineno, line in enumerate(text.splitlines(), 1):
                    if len(matches) >= max_results:
                        break
                    if regex.search(line):
                        truncated = line.strip()[:200]
                        matches.append(f"{rel}:{lineno}: {truncated}")
        except OSError:
            pass

    await asyncio.to_thread(_search)

    if not matches:
        return f"No matches found for pattern: {pattern}"
    header = f"Found {len(matches)} matches for '{pattern}'"
    return header + "\n" + "\n".join(matches)


async def execute_web_fetch(url: str, max_length: int = 10000) -> str:
    """Fetch and read the content of a URL."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return f"[Error: only http/https URLs allowed, got: {parsed.scheme}]"
    if _is_private_url(url):
        return "[Error: private/internal URLs are not allowed]"

    max_length = max(1000, min(max_length, 20000))

    import httpx

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, follow_redirects=True, headers={
                "User-Agent": "Mozilla/5.0 (compatible; MAS-Planner/1.0)",
            })
            resp.raise_for_status()
            content_type = resp.headers.get("content-type", "")
            body = resp.text[:max_length * 2]  # extra room before stripping
    except httpx.HTTPError as exc:
        return f"[Error fetching URL: {exc}]"

    if "json" in content_type:
        try:
            data = json.loads(body)
            pretty = json.dumps(data, ensure_ascii=False, indent=2)
            return f"Content-Type: {content_type}\n\n{pretty[:max_length]}"
        except json.JSONDecodeError:
            pass

    if "html" in content_type:
        text = _strip_html(body)
    else:
        text = body

    if len(text) > max_length:
        text = text[:max_length] + "\n... [truncated]"
    return f"Content-Type: {content_type}\nURL: {url}\n\n{text}"


# ---------------------------------------------------------------------------
# Tool dispatch
# ---------------------------------------------------------------------------

_TOOL_EXECUTORS = {
    "planner_web_search": lambda args: execute_web_search(
        args.get("query", ""), args.get("max_results", 5),
    ),
    "planner_read_file": lambda args, ws: execute_read_file(
        args.get("path", ""), ws, args.get("start_line"), args.get("end_line"),
    ),
    "planner_grep_files": lambda args, ws: execute_grep_files(
        args.get("pattern", ""), ws,
        args.get("file_glob", "*"),
        args.get("case_insensitive", False),
        args.get("max_results", 30),
    ),
    "planner_web_fetch": lambda args: execute_web_fetch(
        args.get("url", ""), args.get("max_length", 10000),
    ),
}

_TOOLS_NEEDING_WORKSPACE = frozenset({"planner_read_file", "planner_grep_files"})


async def dispatch_tool(name: str, args: dict, workspace_directory: str | None) -> str:
    """Execute a named research tool and return its text result."""
    executor = _TOOL_EXECUTORS.get(name)
    if executor is None:
        return f"[Error: unknown tool '{name}']"
    try:
        if name in _TOOLS_NEEDING_WORKSPACE:
            if not workspace_directory:
                return "[Error: no workspace directory configured for this workflow]"
            return await executor(args, workspace_directory)
        return await executor(args)
    except Exception as exc:
        logger.exception("Tool %s execution failed", name)
        return f"[Error executing {name}: {exc}]"


# ---------------------------------------------------------------------------
# Message helpers for multi-turn tool results
# ---------------------------------------------------------------------------

def inject_tool_result_openai(
    messages: list[dict],
    tool_call_id: str,
    tool_name: str,
    tool_input: dict,
    result_text: str,
    thinking_text: str = "",
    content_text: str = "",
) -> None:
    """Append tool call + result in OpenAI format."""
    assistant_msg: dict = {
        "role": "assistant",
        "content": content_text or None,
        "tool_calls": [{
            "id": tool_call_id,
            "type": "function",
            "function": {
                "name": tool_name,
                "arguments": json.dumps(tool_input, ensure_ascii=False),
            },
        }],
    }
    if thinking_text:
        assistant_msg["reasoning_content"] = thinking_text
    messages.append(assistant_msg)
    messages.append({
        "role": "tool",
        "tool_call_id": tool_call_id,
        "content": result_text,
    })


def inject_tool_result_anthropic(
    messages: list[dict],
    tool_call_id: str,
    tool_name: str,
    tool_input: dict,
    result_text: str,
) -> None:
    """Append tool call + result in Anthropic format."""
    messages.append({
        "role": "assistant",
        "content": [
            {"type": "text", "text": f"Calling {tool_name}..."},
            {"type": "tool_use", "id": tool_call_id, "name": tool_name, "input": tool_input},
        ],
    })
    messages.append({
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": tool_call_id, "content": result_text},
        ],
    })


def inject_tool_result(
    messages: list[dict],
    fmt: str,
    tool_call_id: str,
    tool_name: str,
    tool_input: dict,
    result_text: str,
    thinking_text: str = "",
    content_text: str = "",
) -> None:
    """Inject tool result in the correct format."""
    if fmt == "openai":
        inject_tool_result_openai(
            messages, tool_call_id, tool_name, tool_input, result_text,
            thinking_text=thinking_text, content_text=content_text,
        )
    else:
        inject_tool_result_anthropic(messages, tool_call_id, tool_name, tool_input, result_text)


# ---------------------------------------------------------------------------
# Provider detection helper (shared with planner_chat.py)
# ---------------------------------------------------------------------------

def resolve_llm_provider(
    model_provider: str = "",
    model_id_override: str = "",
) -> tuple[str, str, str, str]:
    """Resolve LLM provider settings.

    Returns (provider_url, provider_key, model_id, fmt).

    Reads configuration from:
    1. Specific model match in settings.json
    2. First enabled model in settings.json
    3. models.json fallback
    4. Environment variables
    """
    import os

    from pathlib import Path

    provider_url = ""
    provider_key = ""
    model_id = ""
    fmt = "anthropic"

    # 1. Specific model match
    if model_provider and model_id_override:
        settings_path = Path(__file__).resolve().parent.parent.parent.parent / "data" / "settings.json"
        try:
            settings_data = json.loads(settings_path.read_text(encoding="utf-8"))
            settings_models = settings_data.get("models", [])
            if isinstance(settings_models, list):
                for m in settings_models:
                    if isinstance(m, dict):
                        m_fmt = m.get("format", "")
                        m_model = m.get("default_model", "") or m.get("name", "")
                        if m_fmt == model_provider and m_model == model_id_override:
                            if m.get("base_url") and m.get("api_key") and m.get("enabled", True):
                                provider_url = m["base_url"].rstrip("/")
                                provider_key = m["api_key"]
                                model_id = m_model
                                fmt = m_fmt
                                break
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass

    # 2. First enabled model in settings
    if not provider_url or not provider_key:
        settings_path = Path(__file__).resolve().parent.parent.parent.parent / "data" / "settings.json"
        try:
            settings_data = json.loads(settings_path.read_text(encoding="utf-8"))
            settings_models = settings_data.get("models", [])
            if isinstance(settings_models, list) and settings_models:
                for m in settings_models:
                    if isinstance(m, dict) and m.get("base_url") and m.get("api_key") and m.get("enabled", True):
                        provider_url = m["base_url"].rstrip("/")
                        provider_key = m["api_key"]
                        model_id = m.get("default_model", "")
                        fmt = m.get("format", "openai")
                        break
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass

    # 3. models.json fallback
    if not provider_url or not provider_key:
        try:
            from app.api.models import load_provider_config
            provider_cfg = load_provider_config()
            for name, cfg in provider_cfg.items():
                if cfg.get("url") and cfg.get("key"):
                    provider_url = cfg["url"].rstrip("/")
                    provider_key = cfg["key"]
                    model_id = cfg.get("default_model", "")
                    break
        except Exception:
            pass

    # 4. Environment variables
    if not provider_url or not provider_key:
        provider_url = os.environ.get("MIMO_API_URL", "")
        provider_key = os.environ.get("MIMO_API_KEY", "")
        model_id = os.environ.get("MIMO_MODEL", "minimax-m2.5-free")

    return provider_url, provider_key, model_id, fmt


# ---------------------------------------------------------------------------
# Multi-turn tool execution loop
# ---------------------------------------------------------------------------

async def run_planner_tool_loop(
    *,
    messages: list[dict],
    system: str,
    workspace_directory: str | None,
    thinking_level: str,
    model_provider: str,
    model_id: str,
    max_iterations: int = MAX_TOOL_ITERATIONS,
) -> AsyncGenerator[dict, None]:
    """Run the planner LLM in a multi-turn tool-use loop.

    The model can freely call research tools (web_search, read_file, grep,
    web_fetch) to gather information.  Each tool is executed server-side and
    the result is fed back.  The loop ends when the model calls
    ``planner_task_plan`` (final structured output).

    Yields events:
        {"type": "text", "content": "..."}        — LLM text output
        {"type": "thinking", "content": "..."}    — LLM thinking output
        {"type": "tool_executing", "name": ...}   — tool execution started
        {"type": "tool_result", "name": ...}      — tool execution completed
        {"type": "tool_call", "name": ...}        — final structured output tool
        {"type": "status", "content": ...}        — status message
    """
    from app.api.planner_chat import _call_llm_stream, _planner_task_plan_tool, _planner_alignment_tools

    all_tools = planner_research_tools() + _planner_task_plan_tool() + _planner_alignment_tools()

    # Will be overwritten by the first provider_info event from _call_llm_stream
    fmt = "openai"

    for iteration in range(max_iterations):
        tool_calls_this_turn: list[dict] = []
        turn_thinking: list[str] = []
        turn_text: list[str] = []

        async for event in _call_llm_stream(
            messages,
            system,
            thinking_level,
            tools=all_tools,
            tool_choice_mode="auto",
            max_tokens=32768,
            model_provider=model_provider,
            model_id_override=model_id,
        ):
            etype = event.get("type")

            if etype == "provider_info":
                fmt = event.get("format", fmt)
                continue
            elif etype == "text":
                turn_text.append(str(event.get("content") or ""))
                yield event
            elif etype == "thinking":
                turn_thinking.append(str(event.get("content") or ""))
                yield event
            elif etype == "status":
                yield event
            elif etype == "tool_call":
                tool_calls_this_turn.append(event)

        thinking_accumulated = "".join(turn_thinking)
        content_accumulated = "".join(turn_text)

        # Process tool calls
        final_tool_call: dict | None = None
        alignment_call: dict | None = None

        for tc in tool_calls_this_turn:
            name = tc.get("name", "")
            if name == "planner_task_plan":
                final_tool_call = tc
            elif name == "planner_alignment_check":
                alignment_call = tc
            elif name in RESEARCH_TOOL_NAMES:
                tc_id = tc.get("id", f"tool_{iteration}")
                tc_input = tc.get("input", {})

                # Emit execution start
                input_preview = json.dumps(tc_input, ensure_ascii=False)[:100]
                yield {
                    "type": "tool_executing",
                    "name": name,
                    "input_preview": input_preview,
                    "iteration": iteration + 1,
                }

                # Execute the tool
                t0 = time.monotonic()
                result = await asyncio.wait_for(
                    dispatch_tool(name, tc_input, workspace_directory),
                    timeout=TOOL_EXECUTION_TIMEOUT,
                )
                elapsed_ms = int((time.monotonic() - t0) * 1000)

                yield {
                    "type": "tool_result",
                    "name": name,
                    "result_preview": result[:300],
                    "result_length": len(result),
                    "duration_ms": elapsed_ms,
                    "iteration": iteration + 1,
                }

                # Inject result into messages for next turn
                inject_tool_result(
                    messages, fmt, tc_id, name, tc_input, result,
                    thinking_text=thinking_accumulated,
                    content_text=content_accumulated,
                )

        # If model called planner_task_plan, we're done
        if final_tool_call:
            yield final_tool_call
            if alignment_call:
                yield alignment_call
            return

        # If no tool calls at all (text-only), nudge the model
        if not tool_calls_this_turn:
            if iteration >= max_iterations - 3:
                # Last few iterations: be forceful
                nudge = "请立即调用 planner_task_plan 工具输出最终任务规划，不要输出其他内容。"
            else:
                nudge = (
                    "请继续你的分析。如果你已经有足够的信息，请调用 planner_task_plan 工具输出最终规划。"
                    "如果还需要更多信息，请使用可用的研究工具。"
                )
            messages.append({"role": "user", "content": nudge})

    # Max iterations reached — force a final call with tool_choice required
    logger.warning("Planner tool loop reached max iterations (%d), forcing final call", max_iterations)
    yield {"type": "status", "content": "研究轮次已达上限，正在生成最终规划"}

    messages.append({
        "role": "user",
        "content": (
            "研究轮次已达上限。请立即调用 planner_task_plan 工具输出最终任务规划，不要再使用其他工具。"
        ),
    })

    forced_tool_calls: list[dict] = []
    forced_text_parts: list[str] = []

    # Filter to only planner_task_plan tool
    plan_tool = [t for t in all_tools if t.get("name") == "planner_task_plan"]

    if plan_tool:
        async for event in _call_llm_stream(
            messages,
            system,
            thinking_level,
            tools=plan_tool,
            tool_choice_mode="required",
            max_tokens=32768,
            model_provider=model_provider,
            model_id_override=model_id,
        ):
            etype = event.get("type")
            if etype == "text":
                forced_text_parts.append(str(event.get("content") or ""))
                yield event
            elif etype == "thinking":
                yield event
            elif etype == "status":
                yield event
            elif etype == "tool_call":
                forced_tool_calls.append(event)

    for tc in forced_tool_calls:
        if tc.get("name") == "planner_task_plan":
            yield tc
            return

    # Forced call returned other tools — yield them so caller can still use them
    for tc in forced_tool_calls:
        yield tc
        return

    # No tool calls at all — yield a synthetic planner_task_plan from text as last resort
    forced_text = "".join(forced_text_parts).strip()
    if forced_text:
        logger.warning("Forced final call produced no tool_call, synthesizing from text (len=%d)", len(forced_text))
        yield {"type": "status", "content": "模型未输出结构化工具调用，尝试从文本提取规划"}
        yield {
            "type": "tool_call",
            "name": "planner_task_plan",
            "input": {"tasks": [], "reply": forced_text},
            "id": "forced_from_text",
        }
