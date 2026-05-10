"""CLI entry point for mas_agent."""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from mas_agent.loop import AgentLoop
from mas_agent.types import LoopConfig


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="mas_agent",
        description="MAS Agent — lightweight agentic loop",
    )
    parser.add_argument("--provider", required=True, help="Provider ID (e.g. mimo, glm)")
    parser.add_argument("--model", required=True, help="Model ID (e.g. mimo-v2.5)")
    parser.add_argument("--agent-type", default="coder", help="Agent type (coder/plan/explore/review/shell)")
    parser.add_argument("--run-id", required=True, help="Run ID")
    parser.add_argument("--node-id", required=True, help="Node ID")
    parser.add_argument("--prompt-file", required=True, help="Path to prompt file")
    parser.add_argument("--provider-url", default=None, help="Provider base URL")
    parser.add_argument("--provider-key", default=None, help="API key")
    parser.add_argument("--workspace", default="/workspace", help="Workspace directory")
    parser.add_argument("--stream-dir", default="/workspace/.agent", help="Stream output directory")
    parser.add_argument("--max-turns", type=int, default=50, help="Max LLM turns")
    parser.add_argument("--max-tokens", type=int, default=4096, help="Max tokens per LLM call")
    return parser.parse_args()


async def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    args = parse_args()

    # Read prompt from file
    try:
        with open(args.prompt_file, "r", encoding="utf-8") as f:
            prompt = f.read().strip()
    except FileNotFoundError:
        print(f"Error: prompt file not found: {args.prompt_file}", file=sys.stderr)
        return 1

    config = LoopConfig(
        run_id=args.run_id,
        node_id=args.node_id,
        agent_type=args.agent_type,
        provider=args.provider,
        model=args.model,
        provider_url=args.provider_url,
        provider_key=args.provider_key,
        prompt=prompt,
        max_turns=args.max_turns,
        max_tokens=args.max_tokens,
        workspace=args.workspace,
        stream_dir=args.stream_dir,
    )

    loop = AgentLoop(config)
    return await loop.run()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
