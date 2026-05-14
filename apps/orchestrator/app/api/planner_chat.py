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

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession as AsyncSessionType
from sqlalchemy.orm.attributes import flag_modified

from app.core.database import get_db
from app.models.db import ChatMessage as ChatMessageORM
from app.models.db import SharedDocument, Workflow

logger = logging.getLogger("uvicorn.error")

router = APIRouter()

_THINKING_BUDGETS = {
    "low": 256,
    "medium": 1024,
    "high": 4096,
}

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
    thinking_level: str = Field(default="medium", pattern="^(off|low|medium|high)$")
    alignment_max_attempts: int = Field(default=3, ge=1, le=10)
    history: list[ChatMessage] = Field(default_factory=list)


class ChatMessageResponse(BaseModel):
    id: str
    workflow_id: str
    node_id: str
    role: str
    content: str
    created_at: str | None = None


def _recent_messages(messages: list[dict[str, str]], limit: int = 4) -> list[dict[str, str]]:
    if not messages:
        return []
    return [dict(item) for item in messages[-limit:]]


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
    messages: list[dict],
    system: str,
    thinking_level: str = "medium",
    tools: list[dict] | None = None,
    tool_choice_mode: str = "force_first",
    max_tokens: int = 4096,
):
    """Call the configured LLM and yield stream events.

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
        yield {"type": "text", "content": "错误：未配置 LLM Provider。请在设置中添加模型配置。"}
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
            "max_tokens": max_tokens,
            "messages": openai_messages,
            "stream": True,
        }
        if tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool["name"],
                        "description": tool.get("description", ""),
                        "parameters": tool.get("input_schema", {"type": "object", "properties": {}}),
                    },
                }
                for tool in tools
            ]
            if tool_choice_mode == "force_first":
                forced_tool = tools[0]["name"]
                body["tool_choice"] = {"type": "function", "function": {"name": forced_tool}}
            elif tool_choice_mode == "required":
                body["tool_choice"] = "required"
            elif tool_choice_mode == "auto":
                body["tool_choice"] = "auto"
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
            "max_tokens": max_tokens,
            "system": system,
            "messages": messages,
            "stream": True,
        }
        if thinking_level in _THINKING_BUDGETS:
            body["thinking"] = {
                "type": "enabled",
                "budget_tokens": _THINKING_BUDGETS[thinking_level],
            }
        if tools:
            body["tools"] = tools
            if tool_choice_mode == "force_first":
                body["tool_choice"] = {"type": "tool", "name": tools[0]["name"]}
            elif tool_choice_mode == "auto":
                body["tool_choice"] = {"type": "auto"}

    timeout = httpx.Timeout(connect=15, read=120, write=30, pool=15)

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("POST", url, json=body, headers=headers) as resp:
                if resp.status_code >= 400:
                    error_body = await resp.aread()
                    error_text = error_body.decode()
                    yield {"type": "text", "content": f"\n\n[LLM 请求失败: {resp.status_code} {error_text[:200]}]"}
                    return

                current_tool_id = ""
                current_tool_name = ""
                partial_json_buffers: dict[str, str] = {}
                openai_tool_buffers: dict[int, dict[str, str]] = {}

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
                            reasoning = _extract_stream_reasoning(delta)
                            if reasoning:
                                yield {"type": "thinking", "content": reasoning}
                            content = delta.get("content", "")
                            if content:
                                yield {"type": "text", "content": content}
                            for tool_delta in delta.get("tool_calls") or []:
                                index = int(tool_delta.get("index", 0))
                                slot = openai_tool_buffers.setdefault(index, {"id": "", "name": "", "arguments": ""})
                                if tool_delta.get("id"):
                                    slot["id"] = tool_delta["id"]
                                function_delta = tool_delta.get("function") or {}
                                if function_delta.get("name"):
                                    slot["name"] = function_delta["name"]
                                if function_delta.get("arguments"):
                                    slot["arguments"] += function_delta["arguments"]
                            # Check finish reason
                            if choices[0].get("finish_reason"):
                                for slot in openai_tool_buffers.values():
                                    if not slot.get("name"):
                                        continue
                                    try:
                                        tool_input = json.loads(slot.get("arguments") or "{}")
                                    except json.JSONDecodeError:
                                        tool_input = {}
                                    yield {
                                        "type": "tool_call",
                                        "name": slot["name"],
                                        "input": tool_input,
                                        "id": slot.get("id", ""),
                                    }
                                break
                        continue

                    # --- Anthropic format ---
                    etype = event.get("type", "")
                    if etype in {"ping", "message_start", "content_block_start"}:
                        yield {
                            "type": "status",
                            "content": etype,
                        }

                    if etype == "content_block_start":
                        block = event.get("content_block", {})
                        if block.get("type") == "tool_use":
                            current_tool_id = block.get("id", "")
                            current_tool_name = block.get("name", "")
                            initial_input = block.get("input")
                            partial_json_buffers[current_tool_id] = (
                                json.dumps(initial_input, ensure_ascii=False)
                                if isinstance(initial_input, dict) and initial_input
                                else ""
                            )

                    elif etype == "content_block_delta":
                        delta = event.get("delta", {})
                        dtype = delta.get("type", "")
                        if dtype == "text_delta":
                            yield {"type": "text", "content": delta.get("text", "")}
                        elif dtype == "thinking_delta":
                            reasoning = _extract_stream_reasoning(delta)
                            if reasoning:
                                yield {"type": "thinking", "content": reasoning}
                        elif dtype == "input_json_delta":
                            if current_tool_id:
                                partial_json_buffers[current_tool_id] += delta.get(
                                    "partial_json", ""
                                )

                    elif etype == "content_block_stop":
                        if current_tool_id and current_tool_name:
                            raw_json = partial_json_buffers.get(current_tool_id, "")
                            try:
                                tool_input = json.loads(raw_json or "{}")
                            except json.JSONDecodeError:
                                tool_input = {}
                            yield {
                                "type": "tool_call",
                                "name": current_tool_name,
                                "input": tool_input,
                                "id": current_tool_id,
                            }
                            current_tool_id = ""
                            current_tool_name = ""

                    elif etype == "message_stop":
                        break

                    elif etype == "error":
                        error_msg = event.get("error", {}).get("message", "Unknown error")
                        yield {"type": "text", "content": f"\n\n[LLM 错误: {error_msg}]"}
                        return

    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        yield {"type": "text", "content": f"\n\n[连接失败: {exc}]"}
    except Exception as exc:
        logger.exception("Planner chat LLM call failed")
        yield {"type": "text", "content": f"\n\n[内部错误: {exc}]"}


# ---------------------------------------------------------------------------
# Extract DAG from assistant message
# ---------------------------------------------------------------------------

PLANNER_SPEC_SYSTEM = """你是白盒多 Agent 编排系统的顶级 Planner。

本轮不要调用任何增量建图工具，也不要逐个添加节点。你必须一次性输出一个完整、可解析、可执行的标准规划规格。

只输出一个 fenced JSON 块，代码块语言必须是 `planner-spec`，不要在代码块外输出正文：

```planner-spec
{
  "reply": "给用户看的中文规划说明，必须按阶段写出节点数量，例如：基础搭建（3 节点）：A → B → C。",
  "observable_trace": ["识别目标", "拆解模块", "生成完整 DAG"],
  "task_object": {
    "title": "任务标题",
    "objective": "任务目标",
    "background": "背景",
    "constraints": ["约束"],
    "success_criteria": ["验收标准"],
    "assumptions": ["默认假设"],
    "open_questions": []
  },
  "project_summary": {
    "project_type": "项目类型",
    "tech_stack": ["待 Assess 或已知技术栈"],
    "startup": ["启动方式"],
    "build": ["构建方式"],
    "tests": ["测试方式"],
    "key_directories": ["关键目录"],
    "risk_points": ["风险点"],
    "suggested_next_steps": ["下一步"]
  },
  "shared_doc": "# 项目规划\\n\\nMarkdown 项目文档",
  "dag": {
    "nodes": [
      {
        "id": "explore_requirements",
        "type": "explore",
        "label": "需求探索",
        "prompt": "目标：...\\n具体要求：...\\n产出格式：...\\n验收标准：...",
        "depends_on": []
      }
    ],
    "edges": [
      {"source": "explore_requirements", "target": "design_architecture"}
    ]
  },
  "action": {
    "action": "update_dag",
    "message": "已生成完整工作流 DAG。",
    "blockers": []
  }
}
```

硬性规则：
- 复杂项目必须拆成细粒度的任务，按照任务规划的多个阶段的节点；不要缩水成 2-3 个泛化节点。
- 节点类型只允许 explore / design / coder / merge / review / shell。
- 不要生成 human/人工节点，不要生成顶级 plan/planner 节点。
- design 只是局部方案/接口设计节点，给下游 coder 明确任务；它不能继续规划整套 DAG。
- 两个及以上并行 coder 后必须有 merge。
- 关键 coder/merge 结果后必须有 review。
- 末尾必须有 shell/test 验证节点。
- prompt 必须让子 agent 可直接工作：目标、上下文、具体要求、产出格式、验收标准、边界、协作规则。
- 不确定支付渠道、第三方服务或技术取舍时，用 design 节点制定可执行默认方案并要求必要时 ESCALATE_TO_PLANNER；不要因此 blocked。
- 每个节点必须有 id/type/label/prompt/depends_on；edges 必须与 depends_on 一致。
"""


def _planner_alignment_tools() -> list[dict]:
    return [
        {
            "name": "planner_alignment_check",
            "description": "检查当前 DAG 是否完整对齐 Planner 自己的完整规划说明；如未对齐，返回修正后的完整 DAG。",
            "input_schema": {
                "type": "object",
                "properties": {
                    "aligned": {"type": "boolean"},
                    "missing_items": {"type": "array", "items": {"type": "string"}},
                    "corrected_dag": {
                        "type": "object",
                        "properties": {
                            "nodes": {"type": "array", "items": {"type": "object"}},
                            "edges": {"type": "array", "items": {"type": "object"}},
                            "metadata": {"type": "object"},
                        },
                    },
                    "message": {"type": "string"},
                },
                "required": ["aligned", "missing_items", "message"],
            },
        }
    ]


def _tool_input(tool_calls: list[dict], name: str) -> dict | None:
    for call in reversed(tool_calls):
        if call.get("name") == name and isinstance(call.get("input"), dict):
            return call["input"]
    return None


def _has_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _coerce_string_list(value: object, fallback: list[str] | None = None) -> list[str]:
    if isinstance(value, list):
        items = [str(item).strip() for item in value if str(item).strip()]
        return items if items else (fallback or [])
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return fallback or []


def _first_value(data: dict, *keys: str) -> object:
    for key in keys:
        if key in data and data[key] not in (None, ""):
            return data[key]
    return None


def _normalize_task_object(task_object: dict | None, fallback_goal: str, reply: str = "") -> dict:
    source = dict(task_object or {})
    for raw_key in list(source.keys()):
        canonical_key = raw_key.strip().lower().replace(" ", "_").replace("[", "").replace("]", "")
        if canonical_key.startswith("assumptions") and "assumptions" not in source:
            source["assumptions"] = source.get(raw_key)
        if canonical_key.startswith("open_questions") and "open_questions" not in source:
            source["open_questions"] = source.get(raw_key)
    return {
        "title": str(_first_value(source, "title", "name") or fallback_goal or "未命名任务").strip(),
        "objective": str(
            _first_value(source, "objective", "goal", "description") or reply or fallback_goal or "待明确目标"
        ).strip(),
        "background": str(
            _first_value(source, "background", "context") or reply or "由 Planner 生成的任务草案。"
        ).strip(),
        "constraints": _coerce_string_list(source.get("constraints"), ["按当前工作目录和画布 DAG 执行"]),
        "success_criteria": _coerce_string_list(
            _first_value(source, "success_criteria", "acceptance_criteria"),
            ["DAG 节点和左侧面板均已正确生成"],
        ),
        "assumptions": _coerce_string_list(source.get("assumptions"), []),
        "open_questions": _coerce_string_list(source.get("open_questions"), []),
    }


def _normalize_project_summary(project_summary: dict | None) -> dict:
    source = dict(project_summary or {})
    return {
        "project_type": str(_first_value(source, "project_type", "type", "项目类型") or "待 Assess").strip(),
        "tech_stack": _coerce_string_list(_first_value(source, "tech_stack", "stack", "技术栈"), ["待 Assess"]),
        "startup": _coerce_string_list(_first_value(source, "startup", "start", "启动方式"), ["待 Assess"]),
        "build": _coerce_string_list(_first_value(source, "build", "build_commands", "构建方式"), ["待 Assess"]),
        "tests": _coerce_string_list(_first_value(source, "tests", "test", "测试方式"), ["待 Assess"]),
        "key_directories": _coerce_string_list(
            _first_value(source, "key_directories", "key_files", "directories", "关键目录"),
            ["待 Assess"],
        ),
        "risk_points": _coerce_string_list(
            _first_value(source, "risk_points", "risks", "风险点"),
            ["需要在运行前确认工作目录和依赖"],
        ),
        "suggested_next_steps": _coerce_string_list(
            _first_value(source, "suggested_next_steps", "next_steps", "建议切入点"),
            ["确认规划后点击运行"],
        ),
    }


def _normalize_action(action: dict | None, reply: str = "", default_action: str = "update_dag") -> dict:
    source = dict(action or {})
    normalized = {
        "action": str(source.get("action") or default_action).strip() or default_action,
        "message": str(source.get("message") or reply or "已更新当前规划。").strip(),
        "blockers": source.get("blockers") if isinstance(source.get("blockers"), list) else [],
    }
    if isinstance(source.get("assess_request"), dict):
        normalized["assess_request"] = source["assess_request"]
    return normalized


def _build_task_board_from_dag(dag: dict | None) -> list[dict]:
    nodes = dag.get("nodes") if isinstance(dag, dict) and isinstance(dag.get("nodes"), list) else []
    task_board = []
    for node in nodes:
        if not isinstance(node, dict) or str(node.get("id") or "") == "planner":
            continue
        node_id = str(node.get("id") or f"node_{len(task_board) + 1}")
        label = str(node.get("label") or (node.get("data") or {}).get("label") or node_id)
        task_board.append({
            "id": f"TB-{len(task_board) + 1:03d}",
            "title": label,
            "description": str(node.get("prompt") or (node.get("data") or {}).get("prompt") or label)[:240],
            "node_id": node_id,
            "status": "planned",
            "depends_on": _coerce_string_list(node.get("depends_on"), []),
        })
    return task_board


def _normalize_task_board(task_board: list | None, dag: dict | None = None) -> list[dict]:
    if not isinstance(task_board, list) or not task_board:
        return _build_task_board_from_dag(dag)
    fixed_board = []
    for index, item in enumerate(task_board):
        if not isinstance(item, dict):
            continue
        fixed_board.append({
            "id": str(item.get("id") or f"TB-{index + 1:03d}"),
            "title": str(item.get("title") or item.get("node_id") or f"任务 {index + 1}"),
            "description": str(item.get("description") or ""),
            "node_id": str(item.get("node_id") or ""),
            "status": str(item.get("status") or "planned"),
            "depends_on": _coerce_string_list(item.get("depends_on"), []),
        })
    return fixed_board


def _default_shared_doc(task_object: dict | None, reply: str = "") -> str:
    objective = (task_object or {}).get("objective") or "待补充"
    return (
        f"# 项目规划\n\n## 目标\n{objective}\n\n"
        f"## 当前说明\n{reply or 'Planner 已提交结构化规划。'}"
    )


def _build_minimal_planner_dag(title: str, objective: str) -> dict:
    slug = "".join(ch.lower() if ch.isalnum() else "_" for ch in (title or "task")).strip("_") or "task"
    slug = "_".join(part for part in slug.split("_") if part)[:36] or "task"
    explore_id = f"explore_{slug}"
    coder_id = f"coder_{slug}"
    review_id = f"review_{slug}"
    return {
        "nodes": [
            {
                "id": explore_id,
                "type": "explore",
                "label": "梳理需求与现状",
                "prompt": (
                    f"目标：围绕“{title}”梳理需求与当前上下文。\n"
                    f"上下文：用户当前目标是“{objective}”。\n"
                    "具体要求：总结关键范围、前置依赖、潜在风险和推荐切入点。\n"
                    "产出格式：research_note。\n"
                    "验收标准：给出明确的实施边界和后续编码输入。"
                ),
                "depends_on": [],
            },
            {
                "id": coder_id,
                "type": "coder",
                "label": "实现核心改动",
                "prompt": (
                    f"目标：基于需求梳理结果，完成“{title}”的核心实现。\n"
                    f"上下文：围绕用户目标“{objective}”落地最小闭环。\n"
                    "具体要求：优先完成最核心的代码或配置改动，并记录关键变更点。\n"
                    "产出格式：file_change。\n"
                    "验收标准：产出可集成、可验证，不越权修改无关模块。"
                ),
                "depends_on": [explore_id],
            },
            {
                "id": review_id,
                "type": "review",
                "label": "审查结果与风险",
                "prompt": (
                    f"目标：审查“{title}”本轮实现结果。\n"
                    "具体要求：检查功能闭环、明显回归风险、缺失验证项和需要补充的后续工作。\n"
                    "产出格式：review_report。\n"
                    "验收标准：指出高风险问题或确认当前草案可继续推进。"
                ),
                "depends_on": [coder_id],
            },
        ],
        "edges": [
            {"source": explore_id, "target": coder_id},
            {"source": coder_id, "target": review_id},
        ],
    }


def _infer_node_type_from_label(label: str, phase_title: str = "") -> str:
    label_text = label.lower()
    phase_text = phase_title.lower()
    if any(token in label_text for token in ("merge", "合并", "集成改动")):
        return "merge"
    if any(token in label_text for token in ("审查", "review", "安全")):
        return "review"
    if any(token in label_text for token in ("测试", "验证", "运行", "shell")):
        return "shell"
    if any(token in label_text for token in ("人工", "审批", "确认", "决策", "方案")):
        return "design"
    text = f"{phase_text} {label_text}"
    if any(token in text for token in ("探索", "调研", "梳理", "需求", "分析")):
        return "explore"
    return "coder"


def _slugify_node_id(text: str, fallback: str) -> str:
    slug = "".join(ch.lower() if ch.isalnum() else "_" for ch in text).strip("_")
    slug = "_".join(part for part in slug.split("_") if part)
    return (slug[:42] or fallback).strip("_") or fallback


def _split_outline_items(text: str) -> list[str]:
    items: list[str] = []
    current: list[str] = []
    depth = 0
    index = 0
    while index < len(text):
        char = text[index]
        next_two = text[index:index + 2]
        if char in "（(":
            depth += 1
        elif char in "）)" and depth > 0:
            depth -= 1
        if depth == 0 and (char in "、；;→" or next_two == "->"):
            item = "".join(current).strip(" ；;，,。")
            if item:
                items.append(item)
            current = []
            index += 2 if next_two == "->" else 1
            continue
        current.append(char)
        index += 1
    item = "".join(current).strip(" ；;，,。")
    if item:
        items.append(item)
    return items


def _build_outline_based_dag(outline: str, title: str, objective: str) -> dict | None:
    """Turn a user-visible outline into a usable draft DAG when tool generation fails."""
    import re

    if not outline.strip():
        return None

    phase_matches = re.findall(
        r"([^\n：:（(]{2,24})[（(]\s*(\d+)\s*节点\s*[）)]\s*[：:]\s*([^\n]+)",
        outline,
    )
    if not phase_matches:
        return None

    nodes: list[dict] = []
    edges: list[dict] = []
    previous_phase_terminal_ids: list[str] = []

    for phase_index, (phase_title_raw, expected_count_raw, body_raw) in enumerate(phase_matches, start=1):
        phase_title = phase_title_raw.strip(" -，,。")
        expected_count = max(1, min(int(expected_count_raw), 20))
        body = body_raw.strip()
        parts = _split_outline_items(body)
        expanded_parts: list[str] = []
        for part in parts:
            # Keep parenthesized domain details in the prompt, but split obvious parallel lists.
            if "并行" in part and "：" in part:
                _, detail = part.split("：", 1)
                expanded_parts.extend([item.strip() for item in re.split(r"[、,，]", detail) if item.strip()])
            else:
                expanded_parts.append(part)
        parts = expanded_parts[:expected_count] if expanded_parts else [phase_title]
        while len(parts) < expected_count:
            parts.append(f"{phase_title}子任务 {len(parts) + 1}")

        phase_node_ids: list[str] = []
        for item_index, label_raw in enumerate(parts[:expected_count], start=1):
            label = label_raw.strip() or f"{phase_title}子任务 {item_index}"
            node_type = _infer_node_type_from_label(label, phase_title)
            node_id = _slugify_node_id(f"{phase_title}_{label}", f"node_{phase_index}_{item_index}")
            # Avoid duplicate ids after slug truncation.
            existing_ids = {str(node.get("id")) for node in nodes}
            base_id = node_id
            suffix = 2
            while node_id in existing_ids:
                node_id = f"{base_id}_{suffix}"
                suffix += 1
            depends_on = previous_phase_terminal_ids[:] if item_index == 1 else [phase_node_ids[-1]]
            if "并行" in body and item_index > 1 and previous_phase_terminal_ids:
                depends_on = previous_phase_terminal_ids[:]
            nodes.append({
                "id": node_id,
                "type": node_type,
                "label": label[:80],
                "prompt": (
                    f"目标：完成“{label}”。\n"
                    f"上下文：这是“{title}”规划中“{phase_title}”阶段的子任务；整体目标是“{objective}”。\n"
                    "具体要求：按当前项目结构定位相关文件或模块，完成该子任务所需的实现、配置或验证，并记录关键产物。\n"
                    "产出格式：给出变更摘要、涉及文件、验证方式和阻塞项。\n"
                    "验收标准：该子任务可被下游节点继续使用，且不越权修改无关范围。"
                ),
                "depends_on": depends_on,
            })
            for dep in depends_on:
                edges.append({"source": dep, "target": node_id})
            phase_node_ids.append(node_id)
        previous_phase_terminal_ids = [phase_node_ids[-1]] if phase_node_ids else previous_phase_terminal_ids

    if len(nodes) < 4:
        return None
    return {"nodes": nodes, "edges": edges, "metadata": {"fallback_source": "outline_reply"}}


def _canonicalize_planner_submit(submit: dict | None, fallback_goal: str) -> dict | None:
    """Normalize imperfect tool arguments into the canonical planner contract.

    Some Anthropic-compatible providers occasionally emit slightly malformed
    property names inside large tool payloads. We still require a tool call, but
    normalize field aliases and fill safe defaults from other structured fields
    instead of asking the user to retry.
    """
    if not isinstance(submit, dict):
        return None

    normalized = dict(submit)
    action = normalized.get("action") if isinstance(normalized.get("action"), dict) else {}
    dag = normalized.get("dag") if isinstance(normalized.get("dag"), dict) else {}
    reply = str(normalized.get("reply") or action.get("message") or fallback_goal or "").strip()

    task_object = normalized.get("task_object") if isinstance(normalized.get("task_object"), dict) else {}
    task_object = dict(task_object)
    for raw_key in list(task_object.keys()):
        canonical_key = raw_key.strip().lower().replace(" ", "_").replace("[", "").replace("]", "")
        if canonical_key.startswith("assumptions") and "assumptions" not in task_object:
            task_object["assumptions"] = task_object.get(raw_key)
        if canonical_key.startswith("open_questions") and "open_questions" not in task_object:
            task_object["open_questions"] = task_object.get(raw_key)

    task_object["title"] = str(
        _first_value(task_object, "title", "name") or fallback_goal or "未命名任务"
    ).strip()
    task_object["objective"] = str(
        _first_value(task_object, "objective", "goal", "description") or reply or fallback_goal or "待明确目标"
    ).strip()
    task_object["background"] = str(
        _first_value(task_object, "background", "context") or reply or "由 Planner 工具提交的任务规划。"
    ).strip()
    task_object["constraints"] = _coerce_string_list(
        task_object.get("constraints"),
        ["按当前工作目录和画布 DAG 执行"],
    )
    task_object["success_criteria"] = _coerce_string_list(
        _first_value(task_object, "success_criteria", "acceptance_criteria"),
        ["DAG 节点和左侧面板均已正确生成"],
    )
    task_object["assumptions"] = _coerce_string_list(task_object.get("assumptions"), [])
    task_object["open_questions"] = _coerce_string_list(task_object.get("open_questions"), [])
    normalized["task_object"] = task_object

    if not isinstance(dag, dict) or not isinstance(dag.get("nodes"), list) or not dag.get("nodes"):
        dag = _build_minimal_planner_dag(task_object["title"], task_object["objective"])
    normalized["dag"] = dag

    if not isinstance(action, dict):
        action = {}
    action["action"] = str(action.get("action") or "update_dag").strip() or "update_dag"
    action["message"] = str(
        action.get("message") or reply or "已根据当前目标生成初步工作流草案。"
    ).strip()
    if not isinstance(action.get("blockers"), list):
        action["blockers"] = []
    normalized["action"] = action

    project_summary = normalized.get("project_summary") if isinstance(normalized.get("project_summary"), dict) else {}
    project_summary = dict(project_summary)
    project_summary["project_type"] = str(
        _first_value(project_summary, "project_type", "type", "项目类型") or "待 Assess"
    ).strip()
    project_summary["tech_stack"] = _coerce_string_list(
        _first_value(project_summary, "tech_stack", "stack", "技术栈"),
        ["待 Assess"],
    )
    project_summary["startup"] = _coerce_string_list(
        _first_value(project_summary, "startup", "start", "启动方式"),
        ["待 Assess"],
    )
    project_summary["build"] = _coerce_string_list(
        _first_value(project_summary, "build", "build_commands", "构建方式"),
        ["待 Assess"],
    )
    project_summary["tests"] = _coerce_string_list(
        _first_value(project_summary, "tests", "test", "测试方式"),
        ["待 Assess"],
    )
    project_summary["key_directories"] = _coerce_string_list(
        _first_value(project_summary, "key_directories", "key_files", "directories", "关键目录"),
        ["待 Assess"],
    )
    project_summary["risk_points"] = _coerce_string_list(
        _first_value(project_summary, "risk_points", "risks", "风险点"),
        ["需要在运行前确认工作目录和依赖"],
    )
    project_summary["suggested_next_steps"] = _coerce_string_list(
        _first_value(project_summary, "suggested_next_steps", "next_steps", "建议切入点"),
        ["确认规划后点击运行"],
    )
    normalized["project_summary"] = project_summary

    if not _has_text(normalized.get("shared_doc")):
        normalized["shared_doc"] = (
            f"# 项目规划\n\n## 目标\n{task_object['objective']}\n\n"
            f"## 当前说明\n{reply or action.get('message') or 'Planner 已提交结构化规划。'}"
        )

    task_board = normalized.get("task_board")
    if not isinstance(task_board, list) or not task_board:
        nodes = dag.get("nodes") if isinstance(dag, dict) else []
        task_board = []
        if isinstance(nodes, list):
            for node in nodes:
                if not isinstance(node, dict) or str(node.get("id") or "") == "planner":
                    continue
                node_id = str(node.get("id") or f"node_{len(task_board) + 1}")
                label = str(node.get("label") or (node.get("data") or {}).get("label") or node_id)
                task_board.append({
                    "id": f"TB-{len(task_board) + 1:03d}",
                    "title": label,
                    "description": str(node.get("prompt") or (node.get("data") or {}).get("prompt") or label)[:240],
                    "node_id": node_id,
                    "status": "planned",
                    "depends_on": _coerce_string_list(node.get("depends_on"), []),
                })
    else:
        fixed_board = []
        for index, item in enumerate(task_board):
            if not isinstance(item, dict):
                continue
            fixed_board.append({
                "id": str(item.get("id") or f"TB-{index + 1:03d}"),
                "title": str(item.get("title") or item.get("node_id") or f"任务 {index + 1}"),
                "description": str(item.get("description") or ""),
                "node_id": str(item.get("node_id") or ""),
                "status": str(item.get("status") or "planned"),
                "depends_on": _coerce_string_list(item.get("depends_on"), []),
            })
        task_board = fixed_board
    normalized["task_board"] = task_board

    return normalized


def _json_loads_loose(text: str) -> dict | None:
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        data = json.loads(text.strip())
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _iter_fenced_json_payloads(text: str) -> list[str]:
    import re

    payloads: list[str] = []
    for match in re.finditer(r"```(?:planner-spec|planner_spec|json|plan)?\s*\n(.*?)```", text or "", re.DOTALL | re.IGNORECASE):
        payload = match.group(1).strip()
        if payload:
            payloads.append(payload)
    return payloads


def _extract_outer_json_object(text: str) -> dict | None:
    if not isinstance(text, str):
        return None
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escape = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return _json_loads_loose(text[start:index + 1])
    return None


def _canonicalize_planner_spec(spec: dict | None, fallback_goal: str) -> dict | None:
    if not isinstance(spec, dict):
        return None
    source = dict(spec)
    if "dag" not in source and isinstance(source.get("nodes"), list):
        source["dag"] = {
            "nodes": source.get("nodes") or [],
            "edges": source.get("edges") if isinstance(source.get("edges"), list) else [],
        }
    dag_source = source.get("dag") if isinstance(source.get("dag"), dict) else None
    if not dag_source or not isinstance(dag_source.get("nodes"), list) or not dag_source.get("nodes"):
        return None
    if "action" not in source:
        source["action"] = {"action": "update_dag", "message": source.get("reply") or "已生成完整工作流 DAG。"}
    normalized = _canonicalize_planner_submit(source, fallback_goal)
    if not normalized:
        return None
    dag = normalized.get("dag")
    if not isinstance(dag, dict) or not isinstance(dag.get("nodes"), list) or not dag.get("nodes"):
        return None
    normalized["dag"] = _normalize_dag(dag)
    normalized["task_board"] = _normalize_task_board(normalized.get("task_board"), normalized["dag"])
    normalized["shared_doc"] = str(
        normalized.get("shared_doc") or _default_shared_doc(normalized.get("task_object"), normalized.get("reply") or "")
    )
    return normalized


def _extract_planner_spec(text: str, fallback_goal: str) -> dict | None:
    for payload in _iter_fenced_json_payloads(text):
        spec = _canonicalize_planner_spec(_json_loads_loose(payload), fallback_goal)
        if spec:
            return spec
    spec = _canonicalize_planner_spec(_json_loads_loose(text), fallback_goal)
    if spec:
        return spec
    return _canonicalize_planner_spec(_extract_outer_json_object(text), fallback_goal)


def _extract_dag_from_any_text(text: str) -> dict | None:
    dag = _extract_dag_from_text(text)
    if dag:
        return dag
    try:
        from app.workflows.plan_parser import parse_plan_to_dag
    except Exception:
        return None
    parsed = parse_plan_to_dag(text)
    if not parsed:
        return None
    nodes, edges = parsed
    fixed_nodes = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        data = node.get("data") if isinstance(node.get("data"), dict) else {}
        fixed_nodes.append({
            "id": node.get("id"),
            "type": node.get("type") or data.get("agent_type") or data.get("agentType") or "coder",
            "label": data.get("label") or node.get("label") or node.get("id"),
            "prompt": data.get("prompt") or node.get("prompt") or "",
            "depends_on": [],
        })
    for edge in edges:
        if not isinstance(edge, dict):
            continue
        source = edge.get("source")
        target = edge.get("target")
        for node in fixed_nodes:
            if node.get("id") == target and source:
                node.setdefault("depends_on", []).append(str(source))
    return _normalize_dag({"nodes": fixed_nodes, "edges": edges})


def _declared_outline_node_count(outline: str) -> int:
    import re
    return sum(int(match) for match in re.findall(r"[（(]\s*(\d+)\s*节点\s*[）)]", outline or ""))


def _extract_planned_node_ids(text: str) -> list[str]:
    """Find explicit node ids from model planning text or partial JSON."""
    import re

    if not isinstance(text, str) or not text.strip():
        return []
    candidates: list[str] = []
    patterns = [
        r'"id"\s*:\s*"([a-zA-Z][a-zA-Z0-9_]{2,80})"',
        r"`([a-zA-Z][a-zA-Z0-9_]{2,80})`",
        r"^\s*\d+\.\s*([a-zA-Z][a-zA-Z0-9_]{2,80})\b",
        r"^\s*[-*]\s*([a-zA-Z][a-zA-Z0-9_]{2,80})\b",
    ]
    for pattern in patterns:
        candidates.extend(re.findall(pattern, text, re.MULTILINE))

    valid_prefixes = ("explore_", "design_", "coder_", "merge_", "review_", "shell_", "init_")
    valid_exact = {"explore", "design", "coder", "merge", "review", "shell"}
    seen: set[str] = set()
    node_ids: list[str] = []
    for raw in candidates:
        node_id = str(raw).strip()
        if node_id in seen:
            continue
        prefix = node_id.split("_", 1)[0]
        if node_id.startswith(valid_prefixes) or prefix in valid_exact:
            seen.add(node_id)
            node_ids.append(node_id)
    return node_ids


def _infer_node_type_from_id(node_id: str) -> str:
    prefix = (node_id or "").split("_", 1)[0].lower()
    if prefix in {"explore", "design", "coder", "merge", "review", "shell"}:
        return prefix
    if prefix == "init":
        return "coder"
    return "coder"


def _label_from_node_id(node_id: str, text: str = "") -> str:
    import re

    if text:
        pattern = rf"(?:`{re.escape(node_id)}`|{re.escape(node_id)})\s*(?:[:：\-]\s*)?([^\n。；;]+)"
        match = re.search(pattern, text)
        if match:
            label = re.sub(r"\s+", " ", match.group(1)).strip(" ，,。；;")
            label = re.sub(r"\s*\(.*?\)\s*", "", label).strip()
            if label and not label.startswith(("depends", "depends_on")):
                return label[:80]
    parts = [part for part in node_id.split("_") if part]
    return " ".join(parts).title() if parts else node_id


def _build_dag_from_planned_ids(text: str, title: str, objective: str) -> dict | None:
    """Build a best-effort DAG from an explicit node-id outline when JSON is truncated."""
    import re

    node_ids = _extract_planned_node_ids(text)
    if len(node_ids) < 4:
        return None
    node_id_set = set(node_ids)
    edges: list[dict] = []
    edge_keys: set[tuple[str, str]] = set()
    for source, target in re.findall(r"\b([a-zA-Z][a-zA-Z0-9_]{2,80})\s*(?:->|→)\s*([a-zA-Z][a-zA-Z0-9_]{2,80})\b", text):
        if source in node_id_set and target in node_id_set and source != target:
            edge_keys.add((source, target))
    for node_id in node_ids:
        depends_matches = re.findall(
            rf"{re.escape(node_id)}[^\n]*(?:depends(?:_on)?|依赖|depends:|depends on)\s*[:：]?\s*([^\n]+)",
            text,
            flags=re.IGNORECASE,
        )
        for depends_raw in depends_matches:
            for dep in re.findall(r"[a-zA-Z][a-zA-Z0-9_]{2,80}", depends_raw):
                if dep in node_id_set and dep != node_id:
                    edge_keys.add((dep, node_id))
    if not edge_keys:
        for previous, current in zip(node_ids, node_ids[1:]):
            edge_keys.add((previous, current))
    edges = [{"source": source, "target": target} for source, target in sorted(edge_keys)]
    depends_by_target: dict[str, list[str]] = {node_id: [] for node_id in node_ids}
    for edge in edges:
        depends_by_target.setdefault(edge["target"], []).append(edge["source"])
    nodes = []
    for node_id in node_ids:
        node_type = _infer_node_type_from_id(node_id)
        label = _label_from_node_id(node_id, text)
        nodes.append({
            "id": node_id,
            "type": node_type,
            "label": label,
            "prompt": (
                f"目标：完成“{label}”。\n"
                f"上下文：这是“{title}”规划中的 {node_type} 子 agent 节点；整体目标是“{objective}”。\n"
                "具体要求：读取上游节点产物，按当前项目结构完成本节点职责；不要重新规划整个 DAG。\n"
                "产出格式：说明完成内容、涉及文件、验证方式、风险和阻塞项。\n"
                "验收标准：产物可被下游节点直接消费，且边界清晰。"
            ),
            "depends_on": depends_by_target.get(node_id, []),
        })
    return {"nodes": nodes, "edges": edges, "metadata": {"fallback_source": "planned_node_ids"}}


def _expected_min_node_count(text: str, current_count: int = 0) -> int:
    declared = _declared_outline_node_count(text)
    explicit_ids = len(_extract_planned_node_ids(text))
    expected = max(declared, explicit_ids)
    if expected:
        return max(current_count, expected)
    return current_count


def _ensure_node_id(dag: dict, node_type: str, label: str, prompt: str, depends_on: list[str]) -> str:
    nodes = dag.setdefault("nodes", [])
    node_id = _slugify_node_id(label, f"{node_type}_{len(nodes) + 1}")
    existing = {str(node.get("id")) for node in nodes if isinstance(node, dict)}
    base = node_id
    suffix = 2
    while node_id in existing:
        node_id = f"{base}_{suffix}"
        suffix += 1
    nodes.append({
        "id": node_id,
        "type": node_type,
        "label": label,
        "prompt": prompt,
        "depends_on": depends_on,
    })
    edges = dag.setdefault("edges", [])
    for dep in depends_on:
        edges.append({"source": dep, "target": node_id})
    return node_id


def _repair_planner_dag(dag: dict, outline: str, title: str, objective: str) -> tuple[dict, list[dict]]:
    blockers: list[dict] = []
    if not isinstance(dag, dict):
        dag = {"nodes": [], "edges": []}
    dag = _normalize_dag(dag)
    nodes = dag.get("nodes") if isinstance(dag.get("nodes"), list) else []
    declared_count = _declared_outline_node_count(outline)
    if declared_count and len(nodes) < max(6, int(declared_count * 0.7)):
        outline_dag = _build_outline_based_dag(outline, title, objective)
        if outline_dag:
            dag = _normalize_dag(outline_dag)
            nodes = dag.get("nodes") or []

    coder_nodes = [
        node for node in nodes
        if isinstance(node, dict) and str(node.get("type") or (node.get("data") or {}).get("agentType") or "") == "coder"
    ]
    merge_nodes = [
        node for node in nodes
        if isinstance(node, dict) and str(node.get("type") or (node.get("data") or {}).get("agentType") or "") == "merge"
    ]
    if len(coder_nodes) >= 2 and not merge_nodes:
        deps = [str(node.get("id")) for node in coder_nodes if node.get("id")]
        merge_id = _ensure_node_id(
            dag,
            "merge",
            "合并并行实现改动",
            "目标：合并所有并行 coder 节点的改动。\n具体要求：读取上游 diff/report/commit 信息，处理冲突，形成集成工作区。\n产出格式：merge_report。\n验收标准：所有上游改动已集成或明确列出阻塞冲突。",
            deps,
        )
        merge_nodes = [node for node in dag.get("nodes", []) if node.get("id") == merge_id]

    nodes = dag.get("nodes") or []
    review_nodes = [
        node for node in nodes
        if isinstance(node, dict) and str(node.get("type") or (node.get("data") or {}).get("agentType") or "") == "review"
    ]
    if not review_nodes:
        deps = [str(node.get("id")) for node in (merge_nodes or coder_nodes[-3:]) if isinstance(node, dict) and node.get("id")]
        if deps:
            _ensure_node_id(
                dag,
                "review",
                "审查实现质量与风险",
                "目标：审查本轮实现的正确性、安全性、边界条件和回归风险。\n产出格式：review_report。\n验收标准：列出高风险问题或确认可以进入测试。",
                deps,
            )

    nodes = dag.get("nodes") or []
    shell_nodes = [
        node for node in nodes
        if isinstance(node, dict) and str(node.get("type") or (node.get("data") or {}).get("agentType") or "") == "shell"
    ]
    if not shell_nodes:
        review_or_merge = [
            node for node in nodes
            if isinstance(node, dict) and str(node.get("type") or (node.get("data") or {}).get("agentType") or "") in {"review", "merge"}
        ]
        deps = [str(review_or_merge[-1].get("id"))] if review_or_merge and review_or_merge[-1].get("id") else []
        _ensure_node_id(
            dag,
            "shell",
            "运行集成验证",
            "目标：运行项目可用的构建、lint、测试或启动验证命令。\n产出格式：test_result。\n验收标准：报告命令、通过/失败数量和失败原因。",
            deps,
        )

    if "支付" in outline and not any(
        isinstance(node, dict) and str(node.get("type") or (node.get("data") or {}).get("agentType") or "") in {"design", "plan"}
        and "支付" in str(node.get("label") or (node.get("data") or {}).get("label") or node.get("prompt") or "")
        for node in dag.get("nodes", [])
    ):
        roots = [
            str(node.get("id")) for node in dag.get("nodes", [])
            if isinstance(node, dict) and not node.get("depends_on") and node.get("id")
        ][:1]
        decision_id = _ensure_node_id(
            dag,
            "design",
            "制定支付集成方案",
            "目标：为下游支付实现节点制定局部技术方案，不要继续拆分完整 DAG。\n具体要求：明确支付渠道候选、回调流程、密钥配置、订单状态流转、异常处理和可替代的模拟支付方案。\n产出格式：Markdown 方案说明，包含下游 coder 可直接执行的接口、文件范围、验收标准和风险。\n验收标准：支付相关 coder 能基于该方案继续实现；如仍缺用户决策，用 ESCALATE_TO_PLANNER 提出一个具体问题。",
            roots,
        )
        for node in dag.get("nodes", []):
            if not isinstance(node, dict) or not node.get("id") or node.get("id") == decision_id:
                continue
            label_or_prompt = str(node.get("label") or (node.get("data") or {}).get("label") or node.get("prompt") or "")
            if "支付" not in label_or_prompt:
                continue
            depends_on = node.setdefault("depends_on", [])
            if isinstance(depends_on, list) and decision_id not in depends_on:
                depends_on.append(decision_id)
                dag.setdefault("edges", []).append({"source": decision_id, "target": str(node["id"])})

    repaired = _normalize_dag(dag)
    if len(repaired.get("nodes") or []) < 6:
        blockers.append({
            "code": "dag_too_small",
            "message": "DAG 节点仍少于 6 个，需要重新生成更完整的规划。",
        })
    return repaired, blockers


def _draft_state_ui_payload(draft_state: dict) -> dict:
    def _safe_action_payload(action: dict | None) -> dict | None:
        if not isinstance(action, dict):
            return None
        safe_action = {
            "action": action.get("action"),
            "message": action.get("message"),
            "blockers": action.get("blockers") if isinstance(action.get("blockers"), list) else [],
        }
        if isinstance(action.get("assess_request"), dict):
            safe_action["assess_request"] = action.get("assess_request")
        return safe_action

    def _safe_dag_payload(dag: dict | None) -> dict | None:
        if not isinstance(dag, dict):
            return None
        safe_nodes: list[dict] = []
        for node in (dag.get("nodes") if isinstance(dag.get("nodes"), list) else []):
            if not isinstance(node, dict):
                continue
            data = node.get("data") if isinstance(node.get("data"), dict) else {}
            safe_data = {
                key: value
                for key, value in data.items()
                if key not in {"metadata", "planner_draft_state"}
            }
            safe_nodes.append({
                "id": node.get("id"),
                "type": node.get("type"),
                "agent_type": node.get("agent_type"),
                "label": node.get("label") or data.get("label"),
                "prompt": node.get("prompt") or data.get("prompt"),
                "depends_on": node.get("depends_on") if isinstance(node.get("depends_on"), list) else [],
                "data": safe_data,
            })
        safe_edges: list[dict] = []
        for edge in (dag.get("edges") if isinstance(dag.get("edges"), list) else []):
            if isinstance(edge, dict) and edge.get("source") and edge.get("target"):
                safe_edges.append({"source": edge.get("source"), "target": edge.get("target")})
        return {"nodes": safe_nodes, "edges": safe_edges}

    return {
        "current_stage": draft_state.get("current_stage"),
        "lifecycle_phase": draft_state.get("lifecycle_phase"),
        "outline_reply": draft_state.get("outline_reply"),
        "observable_trace": draft_state.get("observable_trace") or [],
        "task_object": draft_state.get("task_object"),
        "project_summary": draft_state.get("project_summary"),
        "shared_doc": draft_state.get("shared_doc"),
        "task_board": draft_state.get("task_board"),
        "dag": _safe_dag_payload(draft_state.get("dag")),
        "blockers": draft_state.get("blockers") or [],
        "action": _safe_action_payload(draft_state.get("action")),
        "system_generated_dag": bool(draft_state.get("system_generated_dag")),
        "updated_at": draft_state.get("updated_at"),
    }


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


def _extract_ui_state_from_text(text: str) -> dict | None:
    """Extract the latest structured UI state block from planner output."""
    import re

    matches = re.findall(r"```ui-state\s*\n(.*?)\n```", text, re.DOTALL)
    for raw in reversed(matches):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _extract_stream_reasoning(delta: dict) -> str:
    """Read thinking/reasoning content across Anthropic and OpenAI-compatible streams."""
    for key in (
        "thinking",
        "reasoning",
        "reasoning_content",
        "reasoning_text",
        "thought",
        "thoughts",
    ):
        value = delta.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _load_configured_models() -> list[dict]:
    settings_path = Path(__file__).resolve().parents[3] / "data" / "settings.json"
    try:
        payload = json.loads(settings_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        logger.warning("Planner DAG model defaults unavailable: settings file not readable at %s", settings_path)
        return []
    models = payload.get("models", [])
    if not isinstance(models, list):
        return []
    return [item for item in models if isinstance(item, dict) and item.get("default_model")]


def _choose_default_model(agent_type: str, metadata: dict | None) -> tuple[str, str]:
    auto_map = (metadata or {}).get("auto_child_model_map", {}) if isinstance(metadata, dict) else {}
    if isinstance(auto_map, dict):
        raw = str(auto_map.get(agent_type) or "").strip()
        if "/" in raw:
            provider, model_id = raw.split("/", 1)
            if provider and model_id:
                return provider, model_id

    models = _load_configured_models()
    if not models:
        return "", ""

    preferences = {
        "design": ("4.7", "5.1", "5"),
        "plan": ("5.1", "5"),
        "merge": ("5.1", "5"),
        "review": ("5.1", "5"),
        "explore": ("4.7", "5.1", "5"),
        "coder": ("4.7", "5.1", "5"),
        "shell": ("4.7", "5.1", "5"),
    }.get(agent_type, ("4.7", "5.1", "5"))

    chosen = models[0]
    for needle in preferences:
        match = next(
            (
                item for item in models
                if needle in str(item.get("default_model") or item.get("name") or "")
            ),
            None,
        )
        if match is not None:
            chosen = match
            break

    return str(chosen.get("format") or ""), str(chosen.get("default_model") or chosen.get("name") or "")


def _normalize_dag(dag: dict) -> dict:
    """Ensure planner DAGs include edges and executable model metadata."""
    nodes = dag.get("nodes", [])
    raw_edges = dag.get("edges", [])
    metadata = dag.get("metadata", {}) if isinstance(dag.get("metadata", {}), dict) else {}
    edge_keys: set[tuple[str, str]] = set()
    model_assignments: list[str] = []

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
            data = node.get("data")
            if not isinstance(data, dict):
                data = {}
                node["data"] = data
            agent_type = str(
                node.get("agent_type")
                or data.get("agent_type")
                or data.get("agentType")
                or node.get("type")
                or "coder"
            )
            if agent_type and not data.get("agentType"):
                data["agentType"] = agent_type
            if node.get("label") and not data.get("label"):
                data["label"] = node.get("label")
            if node.get("prompt") and not data.get("prompt"):
                data["prompt"] = node.get("prompt")

            provider = str(
                data.get("modelProvider")
                or data.get("model_provider")
                or node.get("model_provider")
                or ""
            )
            model_id = str(
                data.get("modelId")
                or data.get("model_id")
                or node.get("model_id")
                or ""
            )
            if node_id != "planner" and agent_type == "plan":
                agent_type = "design"
                node["type"] = "design"
                node["agent_type"] = "design"
                data["agentType"] = "design"
            if node_id != "planner" and agent_type in {"design", "coder", "explore", "merge", "review", "shell"}:
                fallback_provider, fallback_model_id = _choose_default_model(agent_type, metadata)
                if not provider:
                    provider = fallback_provider
                if not model_id:
                    model_id = fallback_model_id
                if provider:
                    data["modelProvider"] = provider
                    node["model_provider"] = provider
                if model_id:
                    data["modelId"] = model_id
                    node["model_id"] = model_id
                model_assignments.append(f"{node_id}:{agent_type}:{provider or '-'}:{model_id or '-'}")

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
    logger.info(
        "Planner DAG normalized: nodes=%d edges=%d model_assignments=%s",
        len(nodes) if isinstance(nodes, list) else 0,
        len(dag["edges"]),
        model_assignments,
    )
    return dag


async def _update_shared_doc_from_planner(
    text: str, workflow_id: uuid.UUID, db: "AsyncSessionType",
) -> None:
    """Parse ```shared-doc ... ``` blocks from planner output and persist."""
    import re as _re

    pattern = r"```shared-doc\s*\n(.*?)\n```"
    matches = _re.findall(pattern, text, _re.DOTALL)
    if not matches:
        return

    content = matches[-1].strip()
    if not content:
        return

    result = await db.execute(
        select(SharedDocument).where(SharedDocument.workflow_id == workflow_id)
    )
    doc = result.scalar_one_or_none()
    if doc is None:
        doc = SharedDocument(workflow_id=workflow_id, content=content, updated_by="planner")
        db.add(doc)
    else:
        doc.content = content
        doc.updated_by = "planner"
    try:
        await db.commit()
    except Exception as exc:
        logger.warning("Failed to update shared doc from planner: %s", exc)


async def _persist_planner_ui_state(
    ui_state: dict,
    draft_state: dict | None,
    workflow: Workflow,
    workflow_id: uuid.UUID,
    db: "AsyncSessionType",
) -> dict:
    """Persist planner-filled UI state into workflow metadata and related panels."""
    normalized: dict = {}

    task_object = ui_state.get("task_object")
    if isinstance(task_object, dict):
        task_object = dict(task_object)
        task_object.setdefault("assumptions", [])
        task_object.setdefault("open_questions", [])
        normalized["task_object"] = task_object

    task_board = ui_state.get("task_board")
    if isinstance(task_board, list):
        normalized["task_board"] = [item for item in task_board if isinstance(item, dict)]

    if isinstance(draft_state, dict):
        if isinstance(draft_state.get("outline_reply"), str) and draft_state.get("outline_reply").strip():
            normalized["outline_reply"] = draft_state.get("outline_reply").strip()
        if isinstance(draft_state.get("observable_trace"), list):
            normalized["observable_trace"] = [
                str(item).strip() for item in draft_state.get("observable_trace") if str(item).strip()
            ]

    normalized["updated_at"] = datetime.now(timezone.utc).isoformat()

    dag_json = workflow.dag_json if isinstance(workflow.dag_json, dict) else {"nodes": [], "edges": []}
    metadata = dag_json.get("metadata") if isinstance(dag_json.get("metadata"), dict) else {}
    metadata["planner_ui_state"] = normalized
    if isinstance(draft_state, dict):
        metadata["planner_draft_state"] = _draft_state_ui_payload(draft_state)
    dag_json["metadata"] = metadata
    workflow.dag_json = dag_json
    flag_modified(workflow, "dag_json")

    project_summary = ui_state.get("project_summary")
    if isinstance(project_summary, dict):
        workflow.project_summary_json = project_summary
        flag_modified(workflow, "project_summary_json")

    shared_doc = ui_state.get("shared_doc")
    if isinstance(shared_doc, str) and shared_doc.strip():
        result = await db.execute(
            select(SharedDocument).where(SharedDocument.workflow_id == workflow_id)
        )
        doc = result.scalar_one_or_none()
        if doc is None:
            doc = SharedDocument(workflow_id=workflow_id, content=shared_doc.strip(), updated_by="planner")
            db.add(doc)
        else:
            doc.content = shared_doc.strip()
            doc.updated_by = "planner"

    return normalized


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

    # 4.5. Inject shared document context
    doc_result = await db.execute(
        select(SharedDocument).where(SharedDocument.workflow_id == uuid.UUID(body.workflow_id))
    )
    shared_doc = doc_result.scalar_one_or_none()
    if shared_doc and shared_doc.content.strip():
        messages[-1]["content"] += (
            f"\n\n## 项目共享文档\n{shared_doc.content}"
        )

    if workflow.project_summary_json:
        messages[-1]["content"] += (
            "\n\n## 项目现状摘要\n"
            f"{json.dumps(workflow.project_summary_json, ensure_ascii=False, indent=2)}"
        )

    metadata = existing_dag.get("metadata", {}) if isinstance(existing_dag.get("metadata"), dict) else {}
    if isinstance(metadata.get("planner_ui_state"), dict):
        messages[-1]["content"] += (
            "\n\n## 当前左侧面板结构化状态\n"
            f"{json.dumps(metadata['planner_ui_state'], ensure_ascii=False, indent=2)}"
        )

    # 5. Stream the response as SSE
    async def generate():
        def _sse(payload: dict) -> str:
            return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

        workflow_goal = workflow.goal or body.message
        alignment_max_attempts = max(1, min(int(body.alignment_max_attempts or 3), 10))
        existing_ui_state = metadata.get("planner_ui_state") if isinstance(metadata.get("planner_ui_state"), dict) else {}
        existing_draft_state = metadata.get("planner_draft_state") if isinstance(metadata.get("planner_draft_state"), dict) else {}
        draft_state = {
            "current_stage": "plan_outline",
            "lifecycle_phase": workflow.lifecycle_phase,
            "outline_reply": existing_draft_state.get("outline_reply"),
            "observable_trace": existing_draft_state.get("observable_trace") or [],
            "task_object": existing_ui_state.get("task_object") or existing_draft_state.get("task_object"),
            "project_summary": workflow.project_summary_json or existing_draft_state.get("project_summary"),
            "shared_doc": shared_doc.content.strip() if shared_doc and shared_doc.content else existing_draft_state.get("shared_doc"),
            "task_board": existing_ui_state.get("task_board") or existing_draft_state.get("task_board"),
            "dag": existing_dag if isinstance(existing_dag, dict) and existing_dag.get("nodes") else existing_draft_state.get("dag"),
            "blockers": workflow.blockers_json or existing_draft_state.get("blockers") or [],
            "action": existing_draft_state.get("action"),
            "system_generated_dag": bool(existing_draft_state.get("system_generated_dag")),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        assistant_visible_response = ""
        stage_history: list[dict] = []
        final_action = _normalize_action(draft_state.get("action"), "", "update_dag")

        async def _persist_draft_checkpoint(dag: dict | None, reason: str) -> None:
            """Save usable draft DAGs immediately so a long alignment pass cannot leave an empty canvas."""
            if not isinstance(dag, dict) or not isinstance(dag.get("nodes"), list) or not dag.get("nodes"):
                return
            try:
                old_metadata = (
                    workflow.dag_json.get("metadata", {})
                    if isinstance(workflow.dag_json, dict) and isinstance(workflow.dag_json.get("metadata"), dict)
                    else {}
                )
                new_metadata = dag.get("metadata", {}) if isinstance(dag.get("metadata"), dict) else {}
                checkpoint_dag = {
                    **dag,
                    "metadata": {
                        **old_metadata,
                        **new_metadata,
                        "planner_draft_state": _draft_state_ui_payload(draft_state),
                    },
                }
                workflow.dag_json = checkpoint_dag
                workflow.lifecycle_phase = "planning"
                workflow.blockers_json = []
                flag_modified(workflow, "dag_json")
                flag_modified(workflow, "blockers_json")
                await db.execute(
                    update(Workflow)
                    .where(Workflow.id == uuid.UUID(body.workflow_id))
                    .values(
                        dag_json=workflow.dag_json,
                        lifecycle_phase=workflow.lifecycle_phase,
                        blockers_json=workflow.blockers_json,
                        project_summary_json=workflow.project_summary_json,
                    )
                )
                await db.commit()
                logger.info(
                    "Planner draft checkpoint saved: workflow=%s reason=%s nodes=%d edges=%d",
                    body.workflow_id,
                    reason,
                    len(checkpoint_dag.get("nodes") or []),
                    len(checkpoint_dag.get("edges") or []),
                )
            except Exception as exc:
                await db.rollback()
                logger.warning("Failed to save planner draft checkpoint (%s): %s", reason, exc)

        yield _sse({"type": "planner_status", "message": "Planner 请求已发送，正在生成完整标准规划规格。"})
        draft_state["current_stage"] = "plan_outline"
        yield _sse({"type": "planner_stage_status", "stage": "plan_outline", "message": "正在生成完整规划规格"})
        yield _sse({
            "type": "planner_observable_progress",
            "stage": "plan_outline",
            "status": "started",
            "attempt": 1,
            "received_fields": [],
            "missing_fields": ["planner_spec"],
            "next_action": "模型将一次性输出标准 planner-spec，服务端随后解析 DAG、项目文档和任务面板。",
            "draft_state": _draft_state_ui_payload(draft_state),
        })

        spec_messages: list[dict] = _recent_messages(messages, limit=4)
        spec_messages.append({
            "role": "user",
            "content": (
                "请根据当前用户需求和已有状态输出一个完整 planner-spec JSON。\n"
                "不要使用增量工具，不要让系统猜节点，不要只输出自然语言规划。\n"
                "DAG 必须完整覆盖 reply 中声明的所有阶段和节点数量。\n\n"
                f"## 用户最新需求\n{body.message}\n\n"
                f"## 工作流目标\n{workflow_goal}\n\n"
                f"## 当前结构化草稿摘要\n{json.dumps(_draft_state_ui_payload(draft_state), ensure_ascii=False, indent=2)}"
            ),
        })
        raw_response = ""
        planner_spec = None
        last_spec_output = ""
        for spec_attempt in range(2):
            if spec_attempt > 0:
                yield _sse({
                    "type": "planner_observable_progress",
                    "stage": "plan_outline",
                    "status": "retrying",
                    "attempt": spec_attempt + 1,
                    "received_fields": [],
                    "missing_fields": ["valid_planner_spec"],
                    "next_action": "标准规格解析失败，正在要求模型只修复 JSON 格式，不重新规划。",
                    "draft_state": _draft_state_ui_payload(draft_state),
                })
                spec_messages = [{
                    "role": "user",
                    "content": (
                        "上一次输出没有被系统解析成合法 planner-spec。"
                        "请只修复为一个合法 fenced `planner-spec` JSON 块，不要改变规划语义，不要输出其他正文。\n\n"
                        f"## 用户需求\n{body.message}\n\n"
                        f"## 上一次输出\n{last_spec_output}"
                    ),
                }]

            raw_response_parts: list[str] = []
            async for event in _call_llm_stream(
                spec_messages,
                PLANNER_SPEC_SYSTEM,
                body.thinking_level,
                tools=None,
                tool_choice_mode="auto",
                max_tokens=12000,
            ):
                event_type = event.get("type")
                chunk = str(event.get("content") or "")
                if event_type == "status":
                    yield _sse({"type": "planner_status", "message": chunk})
                    continue
                if event_type == "thinking":
                    yield _sse({"type": "thinking_delta", "content": chunk})
                    continue
                if event_type == "text":
                    raw_response_parts.append(chunk)

            raw_response = "".join(raw_response_parts)
            last_spec_output = raw_response
            planner_spec = _extract_planner_spec(raw_response, workflow_goal)
            if planner_spec:
                break

        if planner_spec:
            draft_state["current_stage"] = "fill_task_context"
            draft_state["outline_reply"] = planner_spec.get("reply") or workflow_goal
            draft_state["observable_trace"] = planner_spec.get("observable_trace") or draft_state.get("observable_trace") or []
            draft_state["task_object"] = planner_spec.get("task_object")
            draft_state["project_summary"] = planner_spec.get("project_summary")
            draft_state["shared_doc"] = planner_spec.get("shared_doc")
            draft_state["dag"] = planner_spec.get("dag")
            draft_state["task_board"] = planner_spec.get("task_board")
            draft_state["action"] = planner_spec.get("action")
            draft_state["system_generated_dag"] = False
            stage_history.append({
                "stage": "plan_outline",
                "status": "completed",
                "attempt": 1,
                "summary": "parsed planner-spec",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
            yield _sse({
                "type": "planner_stage_result",
                "stage": "plan_outline",
                "status": "completed",
                "attempt": 1,
                "applied_fields": ["reply", "task_object", "project_summary", "shared_doc", "dag", "task_board", "action"],
                "summary": f"已解析标准 planner-spec：{len((draft_state.get('dag') or {}).get('nodes') or [])} 个节点。",
                "draft_state": _draft_state_ui_payload(draft_state),
            })
            yield _sse({"type": "dag_update", "dag": draft_state["dag"], "draft": True})
            await _persist_draft_checkpoint(draft_state["dag"], "planner_spec")
            yield _sse({
                "type": "planner_ui_update",
                "ui_state": {
                    "task_object": draft_state.get("task_object"),
                    "project_summary": draft_state.get("project_summary"),
                    "shared_doc": draft_state.get("shared_doc"),
                    "task_board": draft_state.get("task_board"),
                },
                "draft": True,
            })
        else:
            yield _sse({
                "type": "planner_observable_progress",
                "stage": "plan_outline",
                "status": "retrying",
                "attempt": 2,
                "received_fields": [],
                "missing_fields": ["valid_planner_spec"],
                "next_action": "标准规格修复仍失败，改用文本 DAG 解析兜底。",
                "draft_state": _draft_state_ui_payload(draft_state),
            })
            await _persist_draft_checkpoint(draft_state["dag"], "text_parse")

        reply = str(draft_state.get("outline_reply") or workflow_goal).strip()
        complete_plan_text = "\n".join(part for part in [reply, raw_response] if part.strip())
        parsed_candidates = [
            _extract_dag_from_any_text(complete_plan_text),
            _build_dag_from_planned_ids(complete_plan_text, workflow_goal, workflow_goal),
            _build_outline_based_dag(complete_plan_text, workflow_goal, workflow_goal),
        ]
        parsed_dag = max(
            (candidate for candidate in parsed_candidates if candidate),
            key=lambda candidate: len((candidate or {}).get("nodes") or []),
            default=None,
        )
        current_dag = draft_state.get("dag") if isinstance(draft_state.get("dag"), dict) else None
        current_node_count = len((current_dag or {}).get("nodes") or [])
        parsed_node_count = len((parsed_dag or {}).get("nodes") or [])
        declared_node_count = _declared_outline_node_count(complete_plan_text)
        expected_min_nodes = _expected_min_node_count(complete_plan_text, current_node_count)
        if parsed_dag and (
            current_node_count == 0
            or parsed_node_count > current_node_count
            or (expected_min_nodes and current_node_count < expected_min_nodes)
        ):
            draft_state["dag"] = parsed_dag
            draft_state["system_generated_dag"] = True
            current_node_count = parsed_node_count
            yield _sse({
                "type": "planner_observable_progress",
                "stage": "fill_dag",
                "status": "completed",
                "attempt": 1,
                "received_fields": ["dag"],
                "missing_fields": [],
                "next_action": f"已从完整规划文本解析 DAG：{parsed_node_count} 个节点。",
                "draft_state": _draft_state_ui_payload(draft_state),
            })

        if not isinstance(draft_state.get("dag"), dict) or not (draft_state.get("dag") or {}).get("nodes"):
            draft_state["dag"] = _build_minimal_planner_dag(workflow_goal, workflow_goal)
            draft_state["system_generated_dag"] = True

        for align_attempt in range(alignment_max_attempts):
            dag_for_check = draft_state.get("dag") if isinstance(draft_state.get("dag"), dict) else {"nodes": [], "edges": []}
            node_count_for_check = len(dag_for_check.get("nodes") or [])
            expected_min_nodes = _expected_min_node_count(complete_plan_text, node_count_for_check)
            yield _sse({
                "type": "planner_stage_status",
                "stage": "fill_dag",
                "message": f"正在让模型对齐检查 DAG（第 {align_attempt + 1}/{alignment_max_attempts} 次）",
            })
            alignment_messages = [{
                "role": "user",
                "content": (
                    "请检查“完整规划说明”和“当前 DAG”是否一一对齐。\n"
                    f"完整规划中可识别的最小节点数是 {expected_min_nodes}。当前 DAG 只有 {node_count_for_check} 个节点时，不能判定 aligned=true。\n"
                    "如果当前 DAG 缺少规划中的节点、阶段、merge/review/shell，请调用 planner_alignment_check，aligned=false，"
                    "并在 corrected_dag 中返回完整修正 DAG。\n"
                    "如果已经对齐，调用 planner_alignment_check，aligned=true，message 必须包含 CONFIRMED_DAG_ALIGNED。\n"
                    "除非 aligned=true，否则不要停止；corrected_dag 必须是完整 DAG，不是差异补丁。\n\n"
                    f"## 规划中显式节点 ID\n{json.dumps(_extract_planned_node_ids(complete_plan_text), ensure_ascii=False)}\n\n"
                    f"## 完整规划说明\n{complete_plan_text}\n\n"
                    f"## 当前 DAG\n```json\n{json.dumps(dag_for_check, ensure_ascii=False, indent=2)}\n```"
                ),
            }]
            alignment_tool_calls: list[dict] = []
            async for event in _call_llm_stream(
                alignment_messages,
                "你是 Planner DAG 对齐审查器。只检查当前 DAG 是否完整覆盖原规划，不要输出普通正文，必须调用工具。",
                body.thinking_level,
                tools=_planner_alignment_tools(),
                tool_choice_mode="force_first",
                max_tokens=12000,
            ):
                event_type = event.get("type")
                if event_type == "thinking":
                    yield _sse({"type": "thinking_delta", "content": str(event.get("content") or "")})
                elif event_type == "tool_call":
                    alignment_tool_calls.append(event)
                    yield _sse({
                        "type": "planner_tool_call",
                        "name": event.get("name"),
                        "id": event.get("id", ""),
                        "input_keys": sorted((event.get("input") or {}).keys()) if isinstance(event.get("input"), dict) else [],
                    })
            alignment = _tool_input(alignment_tool_calls, "planner_alignment_check") or {}
            corrected_dag = alignment.get("corrected_dag") if isinstance(alignment.get("corrected_dag"), dict) else None
            corrected_count = len((corrected_dag or {}).get("nodes") or [])
            missing_items = alignment.get("missing_items") if isinstance(alignment.get("missing_items"), list) else []
            if corrected_dag and corrected_count >= max(node_count_for_check, expected_min_nodes):
                draft_state["dag"] = _normalize_dag(corrected_dag)
                draft_state["system_generated_dag"] = True
                yield _sse({"type": "dag_update", "dag": draft_state["dag"], "draft": True})
                await _persist_draft_checkpoint(draft_state["dag"], f"alignment_{align_attempt + 1}")
            elif corrected_dag and corrected_count < expected_min_nodes:
                missing_items.append(f"corrected_dag 节点数 {corrected_count} 小于规划最小节点数 {expected_min_nodes}")
            yield _sse({
                "type": "planner_observable_progress",
                "stage": "fill_dag",
                "status": "completed" if alignment.get("aligned") else "retrying",
                "attempt": align_attempt + 1,
                "received_fields": ["alignment_check"],
                "missing_fields": [str(item) for item in missing_items],
                "next_action": str(alignment.get("message") or "DAG 对齐检查完成。"),
                "draft_state": _draft_state_ui_payload(draft_state),
            })
            if alignment.get("aligned") and len((draft_state.get("dag") or {}).get("nodes") or []) >= expected_min_nodes:
                break

        assistant_visible_response = ""
        trace = draft_state.get("observable_trace") if isinstance(draft_state.get("observable_trace"), list) else []
        if trace:
            assistant_visible_response += "```observe\n" + "\n".join(f"- {line}" for line in trace if line) + "\n```\n"
        if reply:
            assistant_visible_response += f"```reply\n{reply}\n```"
            yield _sse({"type": "text", "content": assistant_visible_response})

        yield _sse({"type": "planner_stage_status", "stage": "fill_task_board", "message": "正在校验并修复 DAG"})
        title = str((draft_state.get("task_object") or {}).get("title") or workflow_goal)
        objective = str((draft_state.get("task_object") or {}).get("objective") or workflow_goal)
        repaired_dag, repair_blockers = _repair_planner_dag(draft_state["dag"], complete_plan_text, title, objective)
        draft_state["dag"] = repaired_dag
        draft_state["blockers"] = repair_blockers
        draft_state["task_object"] = _normalize_task_object(draft_state.get("task_object"), workflow_goal, reply)
        draft_state["project_summary"] = _normalize_project_summary(draft_state.get("project_summary"))
        draft_state["shared_doc"] = draft_state.get("shared_doc") or _default_shared_doc(draft_state.get("task_object"), reply)
        draft_state["current_stage"] = "fill_task_board"
        draft_state["task_board"] = _build_task_board_from_dag(repaired_dag)
        yield _sse({"type": "dag_update", "dag": repaired_dag, "draft": True})
        yield _sse({
            "type": "planner_stage_result",
            "stage": "fill_task_board",
            "status": "completed",
            "attempt": 1,
            "applied_fields": ["task_board", "task_object", "project_summary", "shared_doc"],
            "summary": "已从 DAG 派生面板与项目文档。",
            "draft_state": _draft_state_ui_payload(draft_state),
        })

        requested_run = any(token in body.message for token in ("运行", "执行", "开始", "run", "start"))
        if repair_blockers:
            draft_state["action"] = _normalize_action({
                "action": "report_blocker",
                "message": "系统无法把当前规划修复为可执行 DAG。",
                "blockers": repair_blockers,
            }, default_action="report_blocker")
        elif not isinstance(draft_state.get("action"), dict):
            draft_state["action"] = _normalize_action({
                "action": "set_ready" if requested_run else "update_dag",
                "message": "方案已就绪，请点击顶部运行按钮开始执行。" if requested_run else "已生成完整工作流 DAG，可继续调整或准备运行。",
            })

        ui_state = {
            "task_object": draft_state.get("task_object"),
            "project_summary": draft_state.get("project_summary"),
            "shared_doc": draft_state.get("shared_doc"),
            "task_board": draft_state.get("task_board"),
        }
        dag = draft_state.get("dag") if isinstance(draft_state.get("dag"), dict) else None
        final_action = _normalize_action(
            draft_state.get("action"),
            assistant_visible_response,
            "update_dag",
        )
        action_name = str(final_action.get("action") or "update_dag")
        draft_state["action"] = final_action
        draft_state["blockers"] = final_action.get("blockers") or []
        draft_state["lifecycle_phase"] = (
            "ready" if action_name == "set_ready"
            else "blocked" if action_name == "report_blocker"
            else "assessing" if action_name == "assess"
            else "planning"
        )

        if dag and action_name in {"update_dag", "set_ready"}:
            old_metadata = (
                workflow.dag_json.get("metadata", {})
                if isinstance(workflow.dag_json, dict) and isinstance(workflow.dag_json.get("metadata"), dict)
                else {}
            )
            new_metadata = dag.get("metadata", {}) if isinstance(dag.get("metadata"), dict) else {}
            dag["metadata"] = {
                **old_metadata,
                **new_metadata,
                "planner_draft_state": _draft_state_ui_payload(draft_state),
            }
            final_action["dag"] = dag
        final_action["ui_state"] = ui_state
        full_response = assistant_visible_response or "```reply\n规划已按阶段完成结构化填充。\n```"
        logger.info(
            "Planner chat completed: workflow=%s node=%s action=%s dag_nodes=%d dag_edges=%d blockers=%d ui_state=%s stage_history=%d",
            body.workflow_id,
            body.node_id,
            action_name,
            len((dag or {}).get("nodes", []) or []),
            len((dag or {}).get("edges", []) or []),
            len(final_action.get("blockers") or []),
            bool(ui_state),
            len(stage_history),
        )

        # Save the assistant message to DB
        try:
            assistant_msg = ChatMessageORM(
                workflow_id=uuid.UUID(body.workflow_id),
                node_id=body.node_id,
                role="assistant",
                content=full_response,
            )
            db.add(assistant_msg)
            if action_name == "update_dag" and dag:
                workflow.dag_json = dag
                flag_modified(workflow, "dag_json")
                workflow.lifecycle_phase = "planning"
                workflow.blockers_json = []
                flag_modified(workflow, "blockers_json")
            elif action_name == "set_ready":
                if dag:
                    workflow.dag_json = dag
                    flag_modified(workflow, "dag_json")
                workflow.lifecycle_phase = "ready"
                workflow.blockers_json = []
                flag_modified(workflow, "blockers_json")
            elif action_name == "assess":
                workflow.lifecycle_phase = "assessing"
                workflow.blockers_json = []
                flag_modified(workflow, "blockers_json")
            elif action_name == "report_blocker":
                workflow.lifecycle_phase = "blocked"
                workflow.blockers_json = final_action.get("blockers") or [{
                    "code": "planner_blocked",
                    "message": final_action.get("message") or "Planner 报告当前方案存在阻塞项。",
                }]
                flag_modified(workflow, "blockers_json")
            elif workflow.lifecycle_phase == "draft":
                workflow.lifecycle_phase = "planning"
            persisted_ui_state = None
            if ui_state:
                persisted_ui_state = await _persist_planner_ui_state(
                    ui_state, draft_state, workflow, uuid.UUID(body.workflow_id), db
                )
                final_action["ui_state"] = persisted_ui_state
            elif isinstance(workflow.dag_json, dict):
                metadata_local = workflow.dag_json.get("metadata") if isinstance(workflow.dag_json.get("metadata"), dict) else {}
                metadata_local["planner_draft_state"] = _draft_state_ui_payload(draft_state)
                workflow.dag_json["metadata"] = metadata_local
                flag_modified(workflow, "dag_json")
            await db.execute(
                update(Workflow)
                .where(Workflow.id == uuid.UUID(body.workflow_id))
                .values(
                    dag_json=workflow.dag_json,
                    lifecycle_phase=workflow.lifecycle_phase,
                    blockers_json=workflow.blockers_json,
                    project_summary_json=workflow.project_summary_json,
                )
            )
            await db.commit()
        except Exception as exc:
            await db.rollback()
            logger.exception("Failed to save planner turn: %s", exc)

        # Parse shared-doc update from planner output
        if not ui_state:
            await _update_shared_doc_from_planner(full_response, uuid.UUID(body.workflow_id), db)

        if ui_state:
            data = json.dumps({
                "type": "planner_ui_update",
                "ui_state": final_action.get("ui_state") or ui_state,
                "draft": False,
            }, ensure_ascii=False)
            yield f"data: {data}\n\n"

        yield _sse({
            "type": "planner_observable_progress",
            "stage": "finalize_ready",
            "status": "completed",
            "attempt": 1,
            "received_fields": ["action"],
            "missing_fields": [],
            "next_action": "本轮规划已完成。",
            "draft_state": _draft_state_ui_payload(draft_state),
        })

        if final_action:
            data = json.dumps({
                "type": "planner_action",
                "action": final_action,
            }, ensure_ascii=False)
            yield f"data: {data}\n\n"

        # After the full response, check if it contains a DAG
        if dag:
            # Emit the DAG update event
            data = json.dumps({
                "type": "dag_update",
                "dag": dag,
                "draft": False,
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
