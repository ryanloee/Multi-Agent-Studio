"""Planner streaming chat endpoint for auto-mode workflow design.

Provides a ChatGPT-like interactive experience where users describe their
goal in natural language and the Planner proposes/modifies a DAG in
real-time.  The Planner maintains conversation history and an evolving
"current plan" that the user can iteratively refine.

POST /api/planner/chat
  Body: { "workflow_id": "...", "message": "...", "history": [...] }
  Response: SSE stream of tokens + structured DAG updates
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.db import Workflow
from app.models.db import ChatMessage as ChatMessageORM
from app.workflows.plan_parser import parse_plan_to_dag

logger = logging.getLogger(__name__)

router = APIRouter()

# ---------------------------------------------------------------------------
# Planner system prompt for iterative conversation mode
# ---------------------------------------------------------------------------

PLANNER_CHAT_SYSTEM = """你是一个项目管理规划器（Planner），正在与用户进行交互式对话来设计工作流。

## 对话模式
- 用户会用自然语言描述他们想要实现的目标
- 你需要分析需求，提出工作流方案
- 你可以展示工作流的 DAG 拓扑结构
- 用户可以随时要求修改（增加/删除/调整节点和连线）
- 每次修改后，你需要输出完整的更新后的 DAG

## DAG 输出格式
当你需要展示或更新工作流时，在对话中输出以下 JSON 块：

```plan
{
  "nodes": [
    {
      "id": "explore_1",
      "type": "explore",
      "label": "代码探索",
      "prompt": "搜索项目中的认证模块实现"
    },
    {
      "id": "coder_1",
      "type": "coder",
      "label": "实现登录API",
      "prompt": "基于探索结果实现 JWT 登录接口",
      "depends_on": ["explore_1"]
    },
    {
      "id": "review_1",
      "type": "review",
      "label": "代码审查",
      "prompt": "审查登录API的安全性和代码质量",
      "depends_on": ["coder_1"]
    },
    {
      "id": "test_1",
      "type": "shell",
      "label": "运行测试",
      "prompt": "运行 pytest 执行认证模块的集成测试",
      "depends_on": ["review_1"]
    }
  ],
  "edges": [
    {"source": "explore_1", "target": "coder_1"},
    {"source": "coder_1", "target": "review_1"},
    {"source": "review_1", "target": "test_1"}
  ]
}
```

## 支持的节点类型
- `plan`（规划器）: 分析任务、拆解子任务
- `coder`（编码器）: 编写和修改代码
- `explore`（探索器）: 搜索代码库、收集信息（只读）
- `review`（审查器）: 审查代码质量、发现bug
- `shell`（执行器）: 运行命令、测试、部署
- `human`（人工审批）: 暂停等待人工确认

## 交互规则
1. 首次对话：分析用户目标，提出初步方案并输出 DAG
2. 后续对话：根据用户的修改意见调整 DAG，输出更新后的完整 DAG
3. 始终用中文回复
4. 每次输出 DAG 时确保是完整的工作流（不要只输出变更部分）
5. 在 DAG 前后用自然语言解释方案和改动
6. 不要创建超过 10 个节点
7. 当用户说"运行"/"执行"/"开始"时，确认方案并输出最终 DAG
"""

# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class ChatMessage(BaseModel):
    role: str = Field(..., pattern="^(user|assistant)$")
    content: str


class PlannerChatRequest(BaseModel):
    workflow_id: str
    message: str
    node_id: str = Field(default="planner", description="Target node ID (planner or other node)")
    history: list[ChatMessage] = Field(default_factory=list)


class ChatMessageResponse(BaseModel):
    id: str
    workflow_id: str
    node_id: str
    role: str
    content: str
    created_at: str | None = None


# ---------------------------------------------------------------------------
# Chat history endpoint — load persisted messages for a node conversation
# ---------------------------------------------------------------------------

@router.get("/history/{workflow_id}", response_model=list[ChatMessageResponse])
async def get_chat_history(
    workflow_id: str,
    node_id: str = "planner",
    db: AsyncSession = Depends(get_db),
):
    """Load persisted chat messages for a workflow + node conversation."""
    result = await db.execute(
        select(ChatMessageORM)
        .where(
            ChatMessageORM.workflow_id == uuid.UUID(workflow_id),
            ChatMessageORM.node_id == node_id,
        )
        .order_by(ChatMessageORM.created_at)
    )
    messages = result.scalars().all()
    return [
        ChatMessageResponse(
            id=str(m.id),
            workflow_id=str(m.workflow_id),
            node_id=m.node_id,
            role=m.role,
            content=m.content,
            created_at=m.created_at.isoformat() if m.created_at else None,
        )
        for m in messages
    ]


# ---------------------------------------------------------------------------
# LLM call helper (reuses the same provider infrastructure as mas_agent)
# ---------------------------------------------------------------------------

async def _call_llm_stream(
    messages: list[dict[str, str]],
    system: str,
):
    """Call the configured LLM and yield text chunks.

    Reads model configuration from user settings (data/settings.json) first,
    then falls back to models.json and environment variables.
    """
    import os
    from pathlib import Path

    provider_url = ""
    provider_key = ""
    model_id = ""

    # 1. Try user settings first
    settings_path = Path(__file__).resolve().parent.parent.parent.parent / "data" / "settings.json"
    fmt = "anthropic"  # default format
    try:
        settings_data = json.loads(settings_path.read_text(encoding="utf-8"))
        settings_models = settings_data.get("models", [])
        if isinstance(settings_models, list) and settings_models:
            # Use the first configured model
            first = settings_models[0]
            if isinstance(first, dict) and first.get("base_url") and first.get("api_key"):
                provider_url = first["base_url"].rstrip("/")
                provider_key = first["api_key"]
                model_id = first.get("default_model", "")
                fmt = first.get("format", "openai")
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass

    # 2. Fallback to models.json
    if not provider_url or not provider_key:
        from app.api.models import load_provider_config
        provider_cfg = load_provider_config()
        for name, cfg in provider_cfg.items():
            if cfg.get("url") and cfg.get("key"):
                provider_url = cfg["url"].rstrip("/")
                provider_key = cfg["key"]
                model_id = cfg.get("default_model", "")
                break

    # 3. Fallback to environment variables
    if not provider_url or not provider_key:
        provider_url = os.environ.get("MIMO_API_URL", "")
        provider_key = os.environ.get("MIMO_API_KEY", "")
        model_id = os.environ.get("MIMO_MODEL", "minimax-m2.5-free")

    if not provider_url or not provider_key:
        yield "错误：未配置 LLM Provider。请在设置中添加模型配置。"
        return

    import httpx

    # Build request based on format (openai vs anthropic)
    if fmt == "openai":
        url = f"{provider_url}/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {provider_key}",
        }
        # OpenAI format: system is a separate message
        openai_messages = [{"role": "system", "content": system}] + messages
        body = {
            "model": model_id,
            "max_tokens": 4096,
            "messages": openai_messages,
            "stream": True,
        }
    else:
        # Anthropic format
        url = f"{provider_url}/v1/messages"
        headers = {
            "Content-Type": "application/json",
            "x-api-key": provider_key,
            "anthropic-version": "2023-06-01",
        }
        body = {
            "model": model_id,
            "max_tokens": 4096,
            "system": system,
            "messages": messages,
            "stream": True,
        }

    timeout = httpx.Timeout(connect=15, read=120, write=30, pool=15)

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("POST", url, json=body, headers=headers) as resp:
                if resp.status_code >= 400:
                    error_body = await resp.aread()
                    error_text = error_body.decode()
                    yield f"\n\n[LLM 请求失败: {resp.status_code} {error_text[:200]}]"
                    return

                current_tool_id = ""
                current_tool_name = ""
                partial_json_buffers: dict[str, str] = {}

                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    payload = line[6:]
                    if payload.strip() == "[DONE]":
                        break

                    try:
                        event = json.loads(payload)
                    except json.JSONDecodeError:
                        continue

                    # --- OpenAI format ---
                    if fmt == "openai":
                        choices = event.get("choices", [])
                        if choices:
                            delta = choices[0].get("delta", {})
                            content = delta.get("content", "")
                            if content:
                                yield content
                            # Check finish reason
                            if choices[0].get("finish_reason"):
                                break
                        continue

                    # --- Anthropic format ---
                    etype = event.get("type", "")

                    if etype == "content_block_start":
                        block = event.get("content_block", {})
                        if block.get("type") == "tool_use":
                            current_tool_id = block.get("id", "")
                            current_tool_name = block.get("name", "")
                            partial_json_buffers[current_tool_id] = ""

                    elif etype == "content_block_delta":
                        delta = event.get("delta", {})
                        dtype = delta.get("type", "")
                        if dtype == "text_delta":
                            yield delta.get("text", "")
                        elif dtype == "input_json_delta":
                            if current_tool_id:
                                partial_json_buffers[current_tool_id] += delta.get(
                                    "partial_json", ""
                                )

                    elif etype == "content_block_stop":
                        if current_tool_id and current_tool_name:
                            raw_json = partial_json_buffers.get(current_tool_id, "")
                            # We don't process tool calls in chat mode
                            current_tool_id = ""
                            current_tool_name = ""

                    elif etype == "message_stop":
                        break

                    elif etype == "error":
                        error_msg = event.get("error", {}).get("message", "Unknown error")
                        yield f"\n\n[LLM 错误: {error_msg}]"
                        return

    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        yield f"\n\n[连接失败: {exc}]"
    except Exception as exc:
        logger.exception("Planner chat LLM call failed")
        yield f"\n\n[内部错误: {exc}]"


# ---------------------------------------------------------------------------
# Extract DAG from assistant message
# ---------------------------------------------------------------------------

def _extract_dag_from_text(text: str) -> dict | None:
    """Extract the latest DAG JSON from the planner's response text."""
    # Look for ```plan ... ``` blocks
    import re
    pattern = r"```plan\s*\n(.*?)\n```"
    matches = re.findall(pattern, text, re.DOTALL)
    if not matches:
        # Fallback: look for JSON with "nodes" and "edges" keys
        pattern2 = r"```json\s*\n(.*?)\n```"
        matches2 = re.findall(pattern2, text, re.DOTALL)
        for m in reversed(matches2):
            try:
                parsed = json.loads(m)
                if "nodes" in parsed:
                    return _normalize_dag(parsed)
            except json.JSONDecodeError:
                continue
        return None

    # Return the last match (most recent update)
    for raw in reversed(matches):
        try:
            parsed = json.loads(raw)
            if "nodes" in parsed:
                return _normalize_dag(parsed)
        except json.JSONDecodeError:
            continue
    return None


def _normalize_dag(dag: dict) -> dict:
    """Ensure planner DAGs always include edge records for node dependencies."""
    nodes = dag.get("nodes", [])
    raw_edges = dag.get("edges", [])
    edge_keys: set[tuple[str, str]] = set()

    if isinstance(raw_edges, list):
        for edge in raw_edges:
            if not isinstance(edge, dict):
                continue
            source = edge.get("source")
            target = edge.get("target")
            if source and target:
                edge_keys.add((str(source), str(target)))

    if isinstance(nodes, list):
        for node in nodes:
            if not isinstance(node, dict):
                continue
            node_id = node.get("id")
            depends_on = node.get("depends_on", [])
            if not node_id or not isinstance(depends_on, list):
                continue
            for dep in depends_on:
                if dep:
                    edge_keys.add((str(dep), str(node_id)))

    dag["edges"] = [
        {"source": source, "target": target}
        for source, target in sorted(edge_keys)
    ]
    return dag


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@router.post("/chat")
async def planner_chat(
    body: PlannerChatRequest,
    db: AsyncSession = Depends(get_db),
):
    """Interactive Planner chat — streams LLM response and DAG updates."""

    # 1. Fetch workflow
    result = await db.execute(
        select(Workflow).where(Workflow.id == uuid.UUID(body.workflow_id))
    )
    workflow = result.scalar_one_or_none()
    if workflow is None:
        raise HTTPException(status_code=404, detail="Workflow not found")

    # 2. Build messages list
    messages: list[dict[str, str]] = []

    # Load persisted history from DB (if not provided by frontend)
    if body.history:
        for msg in body.history:
            messages.append({"role": msg.role, "content": msg.content})
    else:
        # Load from DB
        result = await db.execute(
            select(ChatMessageORM)
            .where(
                ChatMessageORM.workflow_id == uuid.UUID(body.workflow_id),
                ChatMessageORM.node_id == body.node_id,
            )
            .order_by(ChatMessageORM.created_at)
        )
        for msg in result.scalars().all():
            messages.append({"role": msg.role, "content": msg.content})

    # Add the current user message
    messages.append({"role": "user", "content": body.message})

    # 3. Save the user message to DB
    user_msg = ChatMessageORM(
        workflow_id=uuid.UUID(body.workflow_id),
        node_id=body.node_id,
        role="user",
        content=body.message,
    )
    db.add(user_msg)

    # 3.5. For auto-mode workflows, save the first user message as the goal
    if workflow.mode == "auto" and not workflow.goal:
        workflow.goal = body.message
        await db.flush()

    # 4. If the workflow already has a DAG, include it in context
    existing_dag = workflow.dag_json or {}
    if existing_dag and existing_dag.get("nodes"):
        dag_context = f"\n\n## 当前工作流状态\n已有 {len(existing_dag.get('nodes', []))} 个节点和 {len(existing_dag.get('edges', []))} 条连线。"
        dag_context += f"\n```plan\n{json.dumps(existing_dag, ensure_ascii=False, indent=2)}\n```"
        messages[-1]["content"] += dag_context

    # 4. If the workflow has a goal, include it
    if workflow.goal:
        messages[-1]["content"] = f"项目目标: {workflow.goal}\n\n{messages[-1]['content']}"

    # 5. Stream the response as SSE
    async def generate():
        full_response = ""

        async for chunk in _call_llm_stream(messages, PLANNER_CHAT_SYSTEM):
            full_response += chunk

            # Emit as SSE text event
            data = json.dumps({"type": "text", "content": chunk}, ensure_ascii=False)
            yield f"data: {data}\n\n"

        # Save the assistant message to DB
        try:
            assistant_msg = ChatMessageORM(
                workflow_id=uuid.UUID(body.workflow_id),
                node_id=body.node_id,
                role="assistant",
                content=full_response,
            )
            db.add(assistant_msg)
            await db.commit()
        except Exception as exc:
            logger.warning("Failed to save assistant message: %s", exc)

        # After the full response, check if it contains a DAG
        dag = _extract_dag_from_text(full_response)
        if dag:
            # Auto-save the DAG before notifying the frontend so a subsequent
            # run request cannot race against this commit.
            try:
                workflow.dag_json = dag
                await db.commit()
            except Exception as exc:
                logger.warning("Failed to auto-save DAG: %s", exc)

            # Emit the DAG update event
            data = json.dumps({
                "type": "dag_update",
                "dag": dag,
            }, ensure_ascii=False)
            yield f"data: {data}\n\n"

        # Done
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
