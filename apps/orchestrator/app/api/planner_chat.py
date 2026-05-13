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
# Planner system prompt for iterative conversation mode
# ---------------------------------------------------------------------------

PLANNER_CHAT_SYSTEM = """你是白盒多 Agent 编排系统的高级 Planner / Orchestrator，正在与用户进行交互式对话来设计可执行工作流。

你的职责不是生成普通任务列表，而是理解目标、拆解 DAG、分配 Worker、监督执行、处理 Worker 咨询、约束 Worker 间协作，并确保所有关键产物进入 Artifact 管理系统。

重要：当前系统启用强制 Planner 工具接口。你必须调用 `planner_submit_turn` 提交本轮结果，不能把 DAG、面板数据或 action 当作普通文本输出。普通文本只用于简短沟通，最终给用户看的内容也应填写在工具参数 `reply` 中。

## 对话模式
- 用户会用自然语言描述他们想要实现的目标
- 你需要分析需求，提出工作流方案
- 你可以设计工作流 DAG，但不要把 DAG JSON 展示在普通正文里
- 用户可以随时要求修改（增加/删除/调整节点和连线）
- 每次修改后，你需要输出完整的更新后的 DAG
- 你不能直接启动执行；你只能规划、澄清、评估、设置 ready 或报告阻塞

## 可观察规划轨迹
- 每次回复开头，先输出一个 ```observe 代码块，记录本轮真实发生的可观察规划动作
- 轨迹必须是 3-7 行的简短中文列表，每行以 `- ` 开头
- 轨迹内容应具体说明你正在做什么，例如读取了哪些上下文、准备增删哪些节点、为什么要调整依赖或分工
- 不要写“隐藏推理不会展示”之类的说明，也不要声称展示内部推理；这里只记录用户可见的规划过程
- ` ```observe` 代码块结束后，先输出结构化接口块，再在最后输出 ` ```reply` 代码块，里面写给用户看的可讨论规划说明

## 框架填充接口
系统会向你提供 Planner 工具接口。你不能依赖系统从自然语言里猜测，也不要把接口数据展示给用户；每轮必须调用：
- `planner_submit_turn`：一次性提交左侧“任务对象 / 项目摘要 / 项目文档 / 任务面板”、画布 DAG、本轮 action 和给用户看的 reply

接口规则：
- 生成或修改规划时，必须在工具参数中填写 `task_object / project_summary / shared_doc / task_board`
- 生成或修改节点时，必须在工具参数中填写 `dag`
- 每轮必须在工具参数中填写 `action`
- 给用户看的说明只写入工具参数 `reply`；不要在 reply 里粘贴 JSON、节点 prompt 或工具参数
- 如果不能完整填写面板字段，不要提交 DAG；应将 action 设为 `report_blocker` 并说明缺少什么

## DAG 输出格式
当你需要展示或更新工作流时，在对话中输出以下 JSON 块：

```plan
{
  "nodes": [
    {
      "id": "explore_auth_structure",
      "type": "explore",
      "label": "探索认证模块结构",
      "prompt": "搜索项目中所有与用户认证相关的文件和模块。重点关注：1) 现有的认证中间件和路由 2) 用户模型和数据库 schema 3) 现有的密码处理逻辑。列出所有相关文件路径和关键函数名。"
    },
    {
      "id": "explore_jwt_libs",
      "type": "explore",
      "label": "调研JWT库和配置",
      "prompt": "搜索项目的依赖配置文件（package.json/requirements.txt/pyproject.toml），查找已有的JWT或加密库。同时查看项目的配置文件了解现有的密钥管理和环境变量方案。报告找到的库版本和配置格式。",
      "depends_on": []
    },
    {
      "id": "coder_jwt_issuer",
      "type": "coder",
      "label": "实现JWT令牌签发",
      "prompt": "基于前面的探索结果，实现JWT令牌签发功能。具体要求：\n1. 在认证路由模块中创建 POST /auth/login 端点\n2. 接收 username 和 password 参数\n3. 使用 bcrypt 验证密码（复用已有依赖）\n4. 签发包含 {sub, exp, iat} 声明的JWT令牌\n5. 令牌过期时间设为24小时\n6. 返回 {access_token, token_type, expires_in} 格式的响应\n7. 添加必要的错误处理（401 Unauthorized, 422 Validation Error）\n\n验收标准：登录成功返回有效JWT，密码错误返回401，请求格式错误返回422。",
      "depends_on": ["explore_auth_structure", "explore_jwt_libs"]
    },
    {
      "id": "coder_jwt_middleware",
      "type": "coder",
      "label": "实现JWT验证中间件",
      "prompt": "实现JWT令牌验证中间件。具体要求：\n1. 创建认证中间件函数，从 Authorization: Bearer <token> 头部提取令牌\n2. 验证令牌签名和过期时间\n3. 验证成功后将用户信息注入请求上下文\n4. 创建 @require_auth 装饰器保护需要认证的路由\n5. 编写单元测试验证中间件行为\n\n验收标准：有效令牌通过验证，过期/无效令牌返回401，无令牌返回401。",
      "depends_on": ["coder_jwt_issuer"]
    },
    {
      "id": "review_auth_security",
      "type": "review",
      "label": "安全审查认证模块",
      "prompt": "对认证模块进行全面安全审查。检查项：\n1. JWT密钥是否安全存储（不在代码中硬编码）\n2. 密码是否使用bcrypt/scrypt等强哈希算法\n3. 是否存在时序攻击漏洞\n4. 令牌刷新机制是否安全\n5. 是否防止了暴力破解攻击\n6. 错误消息是否泄露了敏感信息\n\n输出格式：列出每个检查项的通过/不通过状态及改进建议。",
      "depends_on": ["coder_jwt_middleware"]
    },
    {
      "id": "test_auth_integration",
      "type": "shell",
      "label": "运行认证模块集成测试",
      "prompt": "运行认证模块的集成测试。执行步骤：\n1. 运行 pytest tests/test_auth.py -v --tb=short\n2. 如果测试失败，分析失败原因并报告\n3. 统计通过/失败/跳过的测试数量\n\n报告最终测试结果摘要。",
      "depends_on": ["review_auth_security"]
    }
  ],
  "edges": [
    {"source": "explore_auth_structure", "target": "coder_jwt_issuer"},
    {"source": "explore_jwt_libs", "target": "coder_jwt_issuer"},
    {"source": "coder_jwt_issuer", "target": "coder_jwt_middleware"},
    {"source": "coder_jwt_middleware", "target": "review_auth_security"},
    {"source": "review_auth_security", "target": "test_auth_integration"}
  ]
}
```

## 支持的节点类型
- `plan`（规划器）: 分析任务、拆解子任务
- `coder`（编码器）: 编写和修改代码
- `explore`（探索器）: 搜索代码库、收集信息（只读）
- `merge`（合并器）: 集成并行 coder 的代码改动、处理冲突、向相关节点或 Planner 升级决策
- `review`（审查器）: 审查代码质量、发现bug
- `shell`（执行器）: 运行命令、测试、部署
- `human`（人工审批）: 暂停等待人工确认

## 任务粒度原则
1. **单一职责**：每个节点只做一件事，聚焦单一操作（如"实现登录端点"而非"实现认证系统"）
2. **精确到文件/函数级**：prompt 应明确目标文件路径或函数名
3. **明确的输入输出**：每个任务 prompt 必须描述：目标、上下文、具体要求、验收标准
4. **合理数量**：根据项目复杂度创建 15-30 个细粒度子任务，确保每个任务可独立执行
5. **并行友好**：将无依赖的探索任务标记为可并行执行
6. **禁止粗任务**：不要输出“开发前端”“实现后端”“完成测试”“修复全部问题”这类粗粒度任务，必须拆成 Worker 可独立执行的文件/模块级任务

## Prompt 编写规范
每个节点的 prompt 必须包含：
- **目标**：明确要完成什么
- **上下文**：相关背景信息（如依赖的前置任务输出）
- **输入**：该 Worker 需要使用的上游结果、文件、命令或 Artifact
- **具体要求**：编号列出具体实现步骤
- **产出格式**：要求 Worker 最后给出结构化摘要
- **验收标准**：如何判断任务完成且质量达标
- **边界**：明确不能修改或不能越权处理的范围
- **咨询/协作规则**：阻塞时输出 `ESCALATE_TO_PLANNER: <question>`；仅可向直接上游、直接下游、同层并行节点输出 `ASK_WORKER: <target_node_id>: <question>` 或 `BROADCAST_TO_PEERS: <message>`
- **Artifact 要求**：explore 生成 research_note，coder 生成 file_change，merge 生成 merge_report，review 生成 review_report，shell/test 生成 test_result，Planner 最终生成 final_output

## 白盒协议
- Worker 汇报进度时输出：`TASK_PROGRESS: <0-100>`
- Worker 需要 Planner 决策时输出：`ESCALATE_TO_PLANNER: <question>`
- Worker 请求相关 Worker 协助时输出：`ASK_WORKER: <target_node_id>: <question>`
- Worker 向相关同伴广播信息时输出：`BROADCAST_TO_PEERS: <message>`
- Worker 间通信默认只允许直接上游、直接下游、同层并行节点；跨无关节点必须由 Planner 授权
- 所有 DAG 节点必须包含 `id`、`type`、`label`、`prompt`、`depends_on`
- 能并行的任务必须并行，复杂任务通常拆成 explore → coder → review → shell/test 多阶段

## 交互规则
1. 首次对话：分析用户目标，提出初步方案并输出 DAG
2. 后续对话：根据用户的修改意见调整 DAG，输出更新后的完整 DAG
3. 始终用中文回复
4. 每次输出 DAG 时确保是完整的工作流（不要只输出变更部分）
5. 给用户看的说明必须放在 ` ```reply` 块里。正文不要变成 JSON，但也不要过短；通常用 4-8 句解释思路、权衡、风险和下一步，并把用户当作一起讨论方案的参与者
6. 当用户说"运行"/"执行"/"开始"时，你只能判断当前方案是否 ready；不能代替系统启动执行
7. 只要存在两个或更多并行 coder 节点，必须在它们之后增加一个 merge 节点，再让 review/shell 依赖 merge 节点
8. merge 节点 prompt 必须要求读取上游 coder 的 diff/report/commit 信息，合并到集成工作区，记录冲突；遇到冲突时先询问相关 coder，涉及架构或产品取舍时升级询问 Planner
9. `plan` 和 `action` 必须完整闭合，不能输出半截 JSON；节点 prompt 保持可执行但要简洁，单节点 prompt 通常不超过 500 个中文字符

## 用户可见回复格式
每次回复都必须包含一个 `reply` 代码块，且用户可见讨论内容只放在这里。`reply` 必须放在本轮输出最后；工具调用由系统消费：

```reply
我会把这个需求拆成商品展示、后台管理、支付发卡、库存提醒和集成验证几个阶段。
当前方案里，数据库和基础工具会先完成，因为后面的 API、页面和支付逻辑都依赖这些模型。
并行开发完成后会进入 merge 节点，再经过 review 和 shell 测试，避免多个 coder 的改动互相覆盖。
你可以继续调整支付方式、后台鉴权强度、邮件服务或页面范围；确认后顶部运行按钮会执行当前画布里的流程。
```

`reply` 中禁止出现：
- 原始 JSON
- 节点完整 prompt
- 代码片段
- `"id"`, `"type"`, `"prompt"`, `"depends_on"` 这类 DAG 字段
- “下面是完整 DAG”之后直接粘贴结构化内容

## 结构化动作协议
每次生成或调整规划时必须输出一个 ```action 代码块，内容是 JSON，schema 固定如下。`action` 放在 `reply` 之前，最终用户可见说明仍以最后的 `reply` 收尾：

```action
{
  "action": "clarify | assess | update_dag | set_ready | report_blocker",
  "message": "给用户看的简短说明",
  "dag": {
    "nodes": [],
    "edges": []
  },
  "blockers": [
    {
      "code": "workspace_missing | dag_empty | model_missing | assess_required",
      "message": "阻塞说明"
    }
  ],
  "assess_request": {
    "scope": "project | current_module | selected_path",
    "paths": []
  }
}
```

动作规则：
- `clarify`：只澄清，不改 DAG
- `assess`：要求系统先做 Assess；适用于刚导入现有项目、用户要求先看项目现状、工作目录刚设定后需要项目摘要
- `update_dag`：本轮修改了 DAG，需要同时输出完整 ` ```plan` DAG
- `set_ready`：当前 DAG 已可执行；如果用户说“开始/运行”，回复里明确提示“方案已就绪，请点击顶部运行按钮开始执行。”
- `report_blocker`：当前无法进入 ready；必须给 blocker
- 不允许输出 `run_now`
- 重要：不要在可见正文里直接复述整段 DAG JSON；结构化内容交给 `plan/action/shared-doc` 代码块，正文应是人类可讨论的规划说明、取舍解释和下一步建议
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
    thinking_level: str = Field(default="medium", pattern="^(off|low|medium|high)$")
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
    thinking_level: str = "medium",
    tools: list[dict] | None = None,
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
            "max_tokens": 4096,
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
            forced_tool = tools[0]["name"]
            body["tool_choice"] = {"type": "function", "function": {"name": forced_tool}}
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
        if thinking_level in _THINKING_BUDGETS:
            body["thinking"] = {
                "type": "enabled",
                "budget_tokens": _THINKING_BUDGETS[thinking_level],
            }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = {"type": "tool", "name": tools[0]["name"]}

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

def _planner_tools() -> list[dict]:
    """Tool interfaces the Planner can call to update the MAS framework."""
    return [
        {
            "name": "planner_submit_turn",
            "description": "提交本轮 Planner 结果：左侧面板、DAG、工作流动作和给用户看的回复。聊天不能直接运行，只能规划或设置 ready。",
            "input_schema": {
                "type": "object",
                "properties": {
                    "observable_trace": {"type": "array", "items": {"type": "string"}},
                    "reply": {"type": "string"},
                    "task_object": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string"},
                            "objective": {"type": "string"},
                            "background": {"type": "string"},
                            "constraints": {"type": "array", "items": {"type": "string"}},
                            "success_criteria": {"type": "array", "items": {"type": "string"}},
                            "assumptions": {"type": "array", "items": {"type": "string"}},
                            "open_questions": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": [
                            "title",
                            "objective",
                            "background",
                            "constraints",
                            "success_criteria",
                            "assumptions",
                            "open_questions",
                        ],
                    },
                    "project_summary": {
                        "type": "object",
                        "properties": {
                            "project_type": {"type": "string"},
                            "tech_stack": {"type": "array", "items": {"type": "string"}},
                            "startup": {"type": "array", "items": {"type": "string"}},
                            "build": {"type": "array", "items": {"type": "string"}},
                            "tests": {"type": "array", "items": {"type": "string"}},
                            "key_directories": {"type": "array", "items": {"type": "string"}},
                            "risk_points": {"type": "array", "items": {"type": "string"}},
                            "suggested_next_steps": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": [
                            "project_type",
                            "tech_stack",
                            "startup",
                            "build",
                            "tests",
                            "key_directories",
                            "risk_points",
                            "suggested_next_steps",
                        ],
                    },
                    "shared_doc": {"type": "string"},
                    "task_board": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string"},
                                "title": {"type": "string"},
                                "description": {"type": "string"},
                                "node_id": {"type": "string"},
                                "status": {"type": "string", "enum": ["planned", "blocked", "ready"]},
                                "depends_on": {"type": "array", "items": {"type": "string"}},
                            },
                            "required": ["id", "title"],
                        },
                    },
                    "dag": {
                        "type": "object",
                        "properties": {
                            "nodes": {"type": "array", "items": {"type": "object"}},
                            "edges": {"type": "array", "items": {"type": "object"}},
                            "metadata": {"type": "object"},
                        },
                    },
                    "action": {
                        "type": "object",
                        "properties": {
                            "action": {
                                "type": "string",
                                "enum": ["clarify", "assess", "update_dag", "set_ready", "report_blocker"],
                            },
                            "message": {"type": "string"},
                            "blockers": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "code": {"type": "string"},
                                        "message": {"type": "string"},
                                    },
                                    "required": ["code", "message"],
                                },
                            },
                            "assess_request": {
                                "type": "object",
                                "properties": {
                                    "scope": {"type": "string", "enum": ["project", "current_module", "selected_path"]},
                                    "paths": {"type": "array", "items": {"type": "string"}},
                                },
                            },
                        },
                        "required": ["action", "message"],
                    },
                },
                "required": [
                    "reply",
                    "task_object",
                    "project_summary",
                    "shared_doc",
                    "task_board",
                    "dag",
                    "action",
                ],
            },
        },
    ]


def _tool_input(tool_calls: list[dict], name: str) -> dict | None:
    for call in reversed(tool_calls):
        if call.get("name") == name and isinstance(call.get("input"), dict):
            return call["input"]
    return None


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


def _extract_action_from_text(text: str) -> dict | None:
    """Extract the final structured planner action block."""
    import re

    matches = re.findall(r"```action\s*\n(.*?)\n```", text, re.DOTALL)
    for raw in reversed(matches):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            continue
        action = str(parsed.get("action") or "").strip()
        if action in {"clarify", "assess", "update_dag", "set_ready", "report_blocker"}:
            return parsed
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
            if node_id != "planner" and agent_type in {"plan", "coder", "explore", "merge", "review", "shell"}:
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
    workflow: Workflow,
    workflow_id: uuid.UUID,
    db: "AsyncSessionType",
) -> dict:
    """Persist planner-filled UI state into workflow metadata and related panels."""
    normalized: dict = {}

    task_object = ui_state.get("task_object")
    if isinstance(task_object, dict):
        normalized["task_object"] = task_object

    task_board = ui_state.get("task_board")
    if isinstance(task_board, list):
        normalized["task_board"] = [item for item in task_board if isinstance(item, dict)]

    normalized["updated_at"] = datetime.now(timezone.utc).isoformat()

    dag_json = workflow.dag_json if isinstance(workflow.dag_json, dict) else {"nodes": [], "edges": []}
    metadata = dag_json.get("metadata") if isinstance(dag_json.get("metadata"), dict) else {}
    metadata["planner_ui_state"] = normalized
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
        full_response = ""
        tool_calls: list[dict] = []
        yield f"data: {json.dumps({'type': 'planner_status', 'message': 'Planner 请求已发送，等待模型流式事件。'}, ensure_ascii=False)}\n\n"

        async for event in _call_llm_stream(
            messages,
            PLANNER_CHAT_SYSTEM,
            body.thinking_level,
            tools=_planner_tools(),
        ):
            event_type = event.get("type")
            chunk = str(event.get("content") or "")
            if event_type == "status":
                data = json.dumps({"type": "planner_status", "message": chunk}, ensure_ascii=False)
                yield f"data: {data}\n\n"
                continue
            if event_type == "thinking":
                data = json.dumps({"type": "thinking_delta", "content": chunk}, ensure_ascii=False)
                yield f"data: {data}\n\n"
                continue
            if event_type == "tool_call":
                tool_calls.append(event)
                data = json.dumps({
                    "type": "planner_tool_call",
                    "name": event.get("name"),
                    "id": event.get("id", ""),
                    "input_keys": sorted((event.get("input") or {}).keys()) if isinstance(event.get("input"), dict) else [],
                }, ensure_ascii=False)
                yield f"data: {data}\n\n"
                continue

            full_response += chunk

            # Emit as SSE text event
            data = json.dumps({"type": "text", "content": chunk}, ensure_ascii=False)
            yield f"data: {data}\n\n"

        submit = _tool_input(tool_calls, "planner_submit_turn")
        if submit:
            trace = submit.get("observable_trace") if isinstance(submit.get("observable_trace"), list) else []
            reply = str(submit.get("reply") or "").strip()
            synthetic_response = ""
            if trace:
                synthetic_response += "```observe\n" + "\n".join(f"- {line}" for line in trace if line) + "\n```\n"
            if reply:
                synthetic_response += f"```reply\n{reply}\n```"
            if synthetic_response:
                full_response = synthetic_response
                data = json.dumps({"type": "text", "content": synthetic_response}, ensure_ascii=False)
                yield f"data: {data}\n\n"

        dag_tool_input = submit.get("dag") if isinstance(submit, dict) and isinstance(submit.get("dag"), dict) else None
        ui_tool_input = (
            {
                "task_object": submit.get("task_object"),
                "project_summary": submit.get("project_summary"),
                "shared_doc": submit.get("shared_doc"),
                "task_board": submit.get("task_board"),
            }
            if isinstance(submit, dict)
            else None
        )
        action_tool_input = submit.get("action") if isinstance(submit, dict) and isinstance(submit.get("action"), dict) else None

        dag = _normalize_dag(dag_tool_input) if dag_tool_input else _extract_dag_from_text(full_response)
        action = action_tool_input or _extract_action_from_text(full_response) or {}
        ui_state = ui_tool_input or _extract_ui_state_from_text(full_response)
        action_name = str(action.get("action") or ("update_dag" if dag else "clarify"))
        plan_parse_failed = "```plan" in full_response and dag is None
        if plan_parse_failed:
            action_name = "report_blocker"
            action = {
                "action": "report_blocker",
                "message": "Planner 输出了结构化计划，但 JSON 没有完整闭合，系统无法安全更新画布。请让 Planner 重新生成更简洁的 DAG。",
                "blockers": [{
                    "code": "planner_dag_parse_failed",
                    "message": "Planner 结构化 DAG 解析失败：plan 代码块不完整或 JSON 无效。",
                }],
            }
        if dag and action_name in {"update_dag", "set_ready"}:
            old_metadata = (
                workflow.dag_json.get("metadata", {})
                if isinstance(workflow.dag_json, dict) and isinstance(workflow.dag_json.get("metadata"), dict)
                else {}
            )
            new_metadata = dag.get("metadata", {}) if isinstance(dag.get("metadata"), dict) else {}
            dag["metadata"] = {**old_metadata, **new_metadata}
            action["dag"] = dag
        if ui_state:
            action["ui_state"] = ui_state
        logger.info(
            "Planner chat completed: workflow=%s node=%s action=%s dag_nodes=%d dag_edges=%d blockers=%d ui_state=%s plan_parse_failed=%s",
            body.workflow_id,
            body.node_id,
            action_name,
            len((dag or {}).get("nodes", []) or []),
            len((dag or {}).get("edges", []) or []),
            len(action.get("blockers") or []) if isinstance(action, dict) else 0,
            bool(ui_state),
            plan_parse_failed,
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
                workflow.blockers_json = action.get("blockers") or [{
                    "code": "planner_blocked",
                    "message": action.get("message") or "Planner 报告当前方案存在阻塞项。",
                }]
                flag_modified(workflow, "blockers_json")
            elif workflow.lifecycle_phase == "draft":
                workflow.lifecycle_phase = "planning"
            persisted_ui_state = None
            if ui_state:
                persisted_ui_state = await _persist_planner_ui_state(
                    ui_state, workflow, uuid.UUID(body.workflow_id), db
                )
                action["ui_state"] = persisted_ui_state
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
                "ui_state": action.get("ui_state") or ui_state,
            }, ensure_ascii=False)
            yield f"data: {data}\n\n"

        if action:
            data = json.dumps({
                "type": "planner_action",
                "action": action,
            }, ensure_ascii=False)
            yield f"data: {data}\n\n"

        # After the full response, check if it contains a DAG
        if dag:
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
