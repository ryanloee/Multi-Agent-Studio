"""CLI entry point for mas_agent."""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from mas_agent.config import load_config
from mas_agent.loop import AgentLoop
from mas_agent.types import LoopConfig

logger = logging.getLogger(__name__)

# Fields where argparse defaults overlap with config-file defaults.
# We track these so we know which CLI values were explicitly provided vs.
# just the argparse default.
_CONFIG_OVERRIDABLE = {"max_turns", "max_tokens", "context_window", "thinking_level"}


def parse_args() -> tuple[argparse.Namespace, set[str]]:
    """Parse CLI arguments.

    Returns the parsed namespace *and* the set of argument names that were
    explicitly set by the user (i.e. not using the argparse default).
    """
    parser = argparse.ArgumentParser(
        prog="mas_agent",
        description="MAS Agent — lightweight agentic loop",
    )
    parser.add_argument("--provider", required=True, help="Provider ID (e.g. mimo, glm)")
    parser.add_argument("--model", required=True, help="Model ID (e.g. mimo-v2.5)")
    parser.add_argument("--agent-type", default="coder", help="Agent type (coder/plan/explore/merge/review/shell)")
    parser.add_argument("--run-id", required=True, help="Run ID")
    parser.add_argument("--node-id", required=True, help="Node ID")
    parser.add_argument("--prompt-file", required=True, help="Path to prompt file")
    parser.add_argument("--provider-url", default=None, help="Provider base URL")
    parser.add_argument("--provider-key", default=None, help="API key")
    parser.add_argument("--workspace", default="/workspace", help="Workspace directory")
    parser.add_argument("--stream-dir", default="/workspace/.agent", help="Stream output directory")
    parser.add_argument("--max-turns", type=int, default=50, help="Max LLM turns")
    parser.add_argument("--max-tokens", type=int, default=4096, help="Max output tokens per LLM call")
    parser.add_argument("--context-window", type=int, default=128000, help="Model context window in tokens")
    parser.add_argument(
        "--thinking-level",
        default="high",
        choices=["off", "low", "medium", "high"],
        help="Reasoning/thinking level for compatible models",
    )

    args = parser.parse_args()

    # Detect which overridable args were explicitly set on the command line.
    # We compare the string representation — if the user didn't pass the flag
    # the value stays at the parser default.
    explicitly_set: set[str] = set()
    for dest, action in parser._option_string_actions.items():  # noqa: SLF001
        arg_dest = action.dest
        if arg_dest in _CONFIG_OVERRIDABLE:
            # Check whether the attribute still has the parser-supplied default
            parser_default = parser.get_default(arg_dest)
            if getattr(args, arg_dest) != parser_default or arg_dest in (args.__dict__):
                # Heuristic: if it appears in sys.argv in any --xxx form, it was explicit
                flag_forms = [dest]
                # Also check the dashed form
                if "_" in dest:
                    pass
                for token in sys.argv[1:]:
                    if token == dest or token.startswith(dest + "="):
                        explicitly_set.add(arg_dest)
                        break

    return args, explicitly_set


async def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    args, explicitly_set = parse_args()

    # Read prompt from file
    try:
        with open(args.prompt_file, "r", encoding="utf-8") as f:
            prompt = f.read().strip()
    except FileNotFoundError:
        print(f"Error: prompt file not found: {args.prompt_file}", file=sys.stderr)
        return 1

    # Build CLI overrides — only include values the user explicitly passed.
    cli_overrides: dict = {}
    if "max_turns" in explicitly_set:
        cli_overrides["max_turns"] = args.max_turns
    if "max_tokens" in explicitly_set:
        cli_overrides["max_tokens"] = args.max_tokens
    if "context_window" in explicitly_set:
        cli_overrides["context_window"] = args.context_window
    if "thinking_level" in explicitly_set:
        cli_overrides["thinking_level"] = args.thinking_level

    # Load merged configuration (defaults → user config → project config → env → CLI)
    merged = load_config(cli_overrides=cli_overrides or None)

    # Build LoopConfig: CLI-required args win unconditionally; for
    # overridable fields fall back to the merged config value.
    config = LoopConfig(
        run_id=args.run_id,
        node_id=args.node_id,
        agent_type=args.agent_type,
        provider=args.provider,
        model=args.model,
        provider_url=args.provider_url,
        provider_key=args.provider_key,
        prompt=prompt,
        max_turns=merged.get("max_turns", args.max_turns),
        max_tokens=merged.get("max_tokens", args.max_tokens),
        context_window=merged.get("context_window", args.context_window),
        thinking_level=merged.get("thinking_level", args.thinking_level),
        workspace=args.workspace,
        stream_dir=args.stream_dir,
    )

    logger.debug(
        "LoopConfig max_turns=%s max_tokens=%s context_window=%s (from merged config)",
        config.max_turns, config.max_tokens, config.context_window,
    )

    loop = AgentLoop(config)
    return await loop.run()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
