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

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession as AsyncSessionType

from app.core.database import get_db
from app.models.db import ChatMessage as ChatMessageORM
from app.models.db import SharedDocument, Workflow

logger = logging.getLogger(__name__)

router = APIRouter()

# ---------------------------------------------------------------------------
# Planner system prompt for iterative conversation mode
# ---------------------------------------------------------------------------

PLANNER_CHAT_SYSTEM = """你是白盒多 Agent 编排系统的高级 Planner / Orchestrator，正在与用户进行交互式对话来设计可执行工作流。

你的职责不是生成普通任务列表，而是理解目标、拆解 DAG、分配 Worker、监督执行、处理 Worker 咨询、约束 Worker 间协作，并确保所有关键产物进入 Artifact 管理系统。

## 对话模式
- 用户会用自然语言描述他们想要实现的目标
- 你需要分析需求，提出工作流方案
- 你可以展示工作流的 DAG 拓扑结构
- 用户可以随时要求修改（增加/删除/调整节点和连线）
- 每次修改后，你需要输出完整的更新后的 DAG

## 可观察规划轨迹
- 每次回复开头，先输出一个 ```observe 代码块，记录本轮真实发生的可观察规划动作
- 轨迹必须是 3-7 行的简短中文列表，每行以 `- ` 开头
- 轨迹内容应具体说明你正在做什么，例如读取了哪些上下文、准备增删哪些节点、为什么要调整依赖或分工
- 不要写“隐藏推理不会展示”之类的说明，也不要声称展示内部推理；这里只记录用户可见的规划过程
- ` ```observe` 代码块结束后，再输出正常解释和 ` ```plan` DAG

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
5. 在 DAG 前后用自然语言解释方案和改动
6. 当用户说"运行"/"执行"/"开始"时，确认方案并输出最终 DAG
7. 只要存在两个或更多并行 coder 节点，必须在它们之后增加一个 merge 节点，再让 review/shell 依赖 merge 节点
8. merge 节点 prompt 必须要求读取上游 coder 的 diff/report/commit 信息，合并到集成工作区，记录冲突；遇到冲突时先询问相关 coder，涉及架构或产品取舍时升级询问 Planner
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
            "max_tokens": 8192,
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
            "max_tokens": 8192,
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

        # Parse shared-doc update from planner output
        _update_shared_doc_from_planner(full_response, uuid.UUID(body.workflow_id), db)

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
