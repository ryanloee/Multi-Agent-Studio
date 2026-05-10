"""AgentLoop — core agentic loop: LLM call → tool execution → repeat."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from mas_agent.events import StreamWriter
from mas_agent.providers.base import StreamChunk
from mas_agent.providers import create_provider
from mas_agent.prompts import load_prompt
from mas_agent.tools import ToolRegistry
from mas_agent.types import LoopConfig, Message

logger = logging.getLogger(__name__)


class AgentLoop:
    """Runs the LLM ↔ tool execution cycle for a single agent node."""

    def __init__(self, config: LoopConfig) -> None:
        self.config = config
        self.messages: list[dict[str, str]] = []
        self.stream = StreamWriter(config.stream_dir, config.run_id, config.node_id)

    def _build_system_prompt(self) -> str:
        return load_prompt(
            self.config.agent_type,
            workspace=self.config.workspace,
        )

    def _get_tools(self) -> list[dict]:
        return ToolRegistry.for_agent_type(self.config.agent_type)

    async def _call_llm(self, system: str, tools: list[dict]) -> tuple[str, list[dict]]:
        """Call the LLM and return (assistant_text, tool_calls_to_execute).

        Streams tokens to StreamWriter and accumulates tool calls.
        """
        provider = create_provider(
            self.config.provider,
            self.config.model,
            self.config.provider_url,
            self.config.provider_key,
        )

        assistant_text = ""
        tool_calls: list[dict] = []

        async for chunk in provider.stream_chat(
            messages=self.messages,
            system=system,
            tools=tools if tools else None,
            max_tokens=self.config.max_tokens,
        ):
            if chunk.type == "text":
                assistant_text += chunk.text
                self.stream.emit_llm_token(chunk.text)

            elif chunk.type == "thinking":
                self.stream.emit_thinking(chunk.text)

            elif chunk.type == "tool_use":
                tool_calls.append({
                    "id": chunk.tool_call_id,
                    "name": chunk.tool_name,
                    "input": chunk.tool_input or {},
                })

            elif chunk.type == "error":
                self.stream.emit_error(chunk.text)
                raise RuntimeError(f"LLM error: {chunk.text}")

        return assistant_text, tool_calls

    async def _execute_tool(self, tool_call: dict) -> str:
        """Execute a single tool call and return the result string."""
        name = tool_call["name"]
        args = tool_call["input"]
        tool = ToolRegistry.get(name)

        if not tool:
            return f"Error: unknown tool '{name}'"

        self.stream.emit_tool_call(name, json.dumps(args, ensure_ascii=False)[:2000])

        try:
            result = await tool.execute(args, self.config.workspace)
        except Exception as e:
            result = f"Error executing {name}: {e}"

        self.stream.emit_tool_result(name, result[:5000])
        return result

    async def run(self) -> int:
        """Run the agent loop. Returns exit code (0=success, 1=failure)."""
        system = self._build_system_prompt()
        tools = self._get_tools()
        prompt = self.config.prompt

        self.messages.append({"role": "user", "content": prompt})
        self.stream.emit_status("running")

        try:
            for turn in range(self.config.max_turns):
                assistant_text, tool_calls = await self._call_llm(system, tools)

                # Build the assistant message content blocks
                if tool_calls:
                    content: list[dict] = []
                    if assistant_text:
                        content.append({"type": "text", "text": assistant_text})
                    for tc in tool_calls:
                        content.append({
                            "type": "tool_use",
                            "id": tc["id"],
                            "name": tc["name"],
                            "input": tc["input"],
                        })
                    self.messages.append({"role": "assistant", "content": json.dumps(content)})
                else:
                    self.messages.append({"role": "assistant", "content": assistant_text})

                # No tool calls → we're done
                if not tool_calls:
                    break

                # Execute all tool calls and add results
                for tc in tool_calls:
                    result = await self._execute_tool(tc)
                    tool_result_msg = {
                        "role": "user",
                        "content": json.dumps([{
                            "type": "tool_result",
                            "tool_use_id": tc["id"],
                            "content": result,
                        }]),
                    }
                    self.messages.append(tool_result_msg)

            self.stream.emit_status("completed")
            return 0

        except Exception as e:
            logger.exception("Agent loop failed")
            self.stream.emit_error(str(e))
            self.stream.emit_status("failed")
            return 1

        finally:
            self.stream.close()
