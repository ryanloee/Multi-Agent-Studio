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

重要：当前系统启用强制 Planner 工具接口。你必须调用 `planner_submit_turn` 分阶段提交结果，不能把 DAG、面板数据或 action 当作普通文本输出。普通文本只用于简短沟通，最终给用户看的内容也应填写在工具参数 `reply` 中。

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
系统会向你提供 Planner 工具接口。你不能依赖系统从自然语言里猜测，也不要把接口数据展示给用户；每个阶段都必须调用：
- `planner_submit_turn`：带 `stage`、`stage_complete` 和 `patch` 的分阶段增量提交；`patch` 只提交当前阶段相关字段

接口规则：
- 先做 `plan_outline`，只给用户可讨论的规划说明与 observable trace
- 再做 `fill_task_context`，只填写 `task_object / project_summary / shared_doc`
- 然后做 `fill_dag`，只填写 `dag / action`
- 接着做 `fill_task_board`，只填写 `task_board`
- 最后做 `finalize_ready`，只填写最终 `action`
- 每个阶段都要正确填写 `stage`，并在本阶段字段完整时把 `stage_complete` 设为 true
- 给用户看的说明只写入工具参数 `reply`；不要在 reply 里粘贴 JSON、节点 prompt 或工具参数
- 如果当前阶段缺字段，就只修正当前阶段，不要回退整个流程

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


PLANNER_STAGE_SEQUENCE = [
    "plan_outline",
    "fill_task_context",
    "fill_dag",
    "fill_task_board",
    "finalize_ready",
]

PLANNER_STAGE_LABELS = {
    "plan_outline": "正在生成规划说明",
    "fill_task_context": "正在填写任务对象与项目摘要",
    "fill_dag": "正在生成 DAG",
    "fill_task_board": "正在填充任务面板",
    "finalize_ready": "正在判断是否可进入 Ready",
}

PLANNER_STAGE_TIMELINE_MESSAGES = {
    "plan_outline": "先给出可讨论的规划说明和可观察轨迹，再继续结构化填充。",
    "fill_task_context": "基于规划说明补任务对象、项目摘要和项目文档。",
    "fill_dag": "基于已确认的任务对象和项目摘要生成 DAG 节点与依赖。",
    "fill_task_board": "基于当前 DAG 生成左侧任务面板任务卡。",
    "finalize_ready": "判断当前工作流进入 planning、ready 还是 blocked。",
}

PLANNER_STAGE_FIELDS = {
    "plan_outline": ["reply", "observable_trace"],
    "fill_task_context": ["task_object", "project_summary", "shared_doc"],
    "fill_dag": ["dag", "action"],
    "fill_task_board": ["task_board"],
    "finalize_ready": ["action"],
}


def _recent_messages(messages: list[dict[str, str]], limit: int = 4) -> list[dict[str, str]]:
    if not messages:
        return []
    return [dict(item) for item in messages[-limit:]]


def _summarize_text(value: str | None, limit: int = 280) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    return text[:limit] + ("…" if len(text) > limit else "")


def _summarize_dag(dag: dict | None) -> dict:
    if not isinstance(dag, dict):
        return {}
    nodes = dag.get("nodes") if isinstance(dag.get("nodes"), list) else []
    edges = dag.get("edges") if isinstance(dag.get("edges"), list) else []
    node_summary = []
    for node in nodes[:8]:
        if not isinstance(node, dict):
            continue
        node_summary.append({
            "id": node.get("id"),
            "type": node.get("type") or (node.get("data") or {}).get("agentType"),
            "label": node.get("label") or (node.get("data") or {}).get("label"),
            "depends_on": node.get("depends_on") or [],
        })
    return {
        "node_count": len(nodes),
        "edge_count": len(edges),
        "nodes": node_summary,
    }


def _state_summary_payload(
    latest_user_message: str,
    workflow_goal: str,
    stage: str,
    lifecycle_phase: str,
    draft_state: dict,
    blockers: list[dict],
) -> dict:
    return {
        "latest_user_requirement": latest_user_message,
        "workflow_goal": workflow_goal,
        "stage": stage,
        "lifecycle_phase": lifecycle_phase,
        "task_object": draft_state.get("task_object"),
        "project_summary": draft_state.get("project_summary"),
        "shared_doc": _summarize_text(draft_state.get("shared_doc"), 1600),
        "shared_doc_summary": _summarize_text(draft_state.get("shared_doc")),
        "dag_summary": _summarize_dag(draft_state.get("dag")),
        "task_board_count": len(draft_state.get("task_board") or []),
        "blockers": blockers,
    }


def _build_stage_prompt(
    *,
    stage: str,
    latest_user_message: str,
    workflow_goal: str,
    lifecycle_phase: str,
    draft_state: dict,
    blockers: list[dict],
    retry_feedback: str = "",
) -> str:
    summary = json.dumps(
        _state_summary_payload(
            latest_user_message,
            workflow_goal,
            stage,
            lifecycle_phase,
            draft_state,
            blockers,
        ),
        ensure_ascii=False,
        indent=2,
    )
    stage_instructions = {
        "plan_outline": (
            "当前是第一阶段。只输出可讨论的规划说明和 observable_trace。"
            " task_object 只允许给粗粒度草案；不要生成 DAG、task_board 或最终 ready 判定。"
        ),
        "fill_task_context": (
            "请基于你刚才已经给出的规划说明，只填写 task_object、project_summary、shared_doc。"
            " 不要重复长篇解释，不要生成 DAG 或 task_board。"
        ),
        "fill_dag": (
            "请基于已确认的 task_object、project_summary、shared_doc 和最新用户需求，只生成 dag 和 action。"
            " DAG 必须是完整工作流，不要退化成 2-3 个泛化节点。"
            " 优先拆成 8-20 个文件/模块级节点；只有非常小的任务才允许低于 6 个节点。"
            " 能并行的 explore/coder 必须并行；两个及以上并行 coder 之后必须加 merge。"
            " action 只能是 update_dag、assess 或 report_blocker。不要输出额外正文。"
        ),
        "fill_task_board": (
            "请基于当前 DAG，为左侧任务面板生成 task_board。不要修改 DAG，不要重复正文。"
        ),
        "finalize_ready": (
            "请判断当前工作流是否可进入 ready；如不可进入，给出 blocker。"
            " 只填写 action，不要重复前面内容。"
        ),
    }[stage]
    prompt = [
        f"当前阶段：{stage}",
        stage_instructions,
        "上下文策略：优先根据下面的结构化状态摘要继续，不要复读全量历史聊天。",
        "结构化状态摘要：",
        summary,
        "",
        "本阶段必须调用 planner_submit_turn，并且：",
        f"- `stage` 必须是 `{stage}`",
        "- `stage_complete` 只有在本阶段字段已填写完成时才能设为 true",
        "- `patch` 里只提交本阶段相关字段的增量",
        "- `reply` 只有 plan_outline 阶段需要给用户看；其他阶段尽量留空",
        "- `observable_trace` 在 plan_outline 阶段填写 3-7 行，其他阶段可为空或简短阶段说明",
        "",
        PLANNER_STAGE_TIMELINE_MESSAGES[stage],
    ]
    if retry_feedback:
        prompt.extend(["", "你上一轮本阶段提交仍有缺项，请只修正当前阶段：", retry_feedback])
    return "\n".join(prompt)


def _build_stage_messages(
    *,
    stage: str,
    latest_user_message: str,
    workflow_goal: str,
    lifecycle_phase: str,
    draft_state: dict,
    blockers: list[dict],
    recent_messages: list[dict[str, str]],
    retry_feedback: str = "",
) -> list[dict[str, str]]:
    if stage == "plan_outline":
        stage_messages = _recent_messages(recent_messages)
        stage_messages.append({
            "role": "user",
            "content": _build_stage_prompt(
                stage=stage,
                latest_user_message=latest_user_message,
                workflow_goal=workflow_goal,
                lifecycle_phase=lifecycle_phase,
                draft_state=draft_state,
                blockers=blockers,
                retry_feedback=retry_feedback,
            ),
        })
        return stage_messages
    return [{
        "role": "user",
        "content": _build_stage_prompt(
            stage=stage,
            latest_user_message=latest_user_message,
            workflow_goal=workflow_goal,
            lifecycle_phase=lifecycle_phase,
            draft_state=draft_state,
            blockers=blockers,
            retry_feedback=retry_feedback,
        ),
    }]


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

def _task_object_schema() -> dict:
    return {
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
    }


def _project_summary_schema() -> dict:
    return {
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
    }


def _task_board_schema() -> dict:
    return {
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
    }


def _action_schema() -> dict:
    return {
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
    }


def _planner_tools(stage: str) -> list[dict]:
    """Tool interfaces the Planner can call to update the MAS framework."""
    patch_properties: dict[str, dict] = {}
    if stage == "fill_task_context":
        patch_properties = {
            "task_object": _task_object_schema(),
            "project_summary": _project_summary_schema(),
            "shared_doc": {"type": "string"},
        }
    elif stage == "fill_dag":
        patch_properties = {
            "dag": {
                "type": "object",
                "properties": {
                    "nodes": {"type": "array", "items": {"type": "object"}},
                    "edges": {"type": "array", "items": {"type": "object"}},
                    "metadata": {"type": "object"},
                },
            },
            "action": _action_schema(),
        }
    elif stage == "fill_task_board":
        patch_properties = {
            "task_board": _task_board_schema(),
        }
    elif stage == "finalize_ready":
        patch_properties = {
            "action": _action_schema(),
        }
    elif stage == "plan_outline":
        patch_properties = {
            "task_object": _task_object_schema(),
        }
    return [
        {
            "name": "planner_submit_turn",
            "description": "分阶段提交本轮 Planner 结果。必须带 stage、stage_complete、patch；patch 只提交当前阶段相关字段。",
            "input_schema": {
                "type": "object",
                "properties": {
                    "stage": {"type": "string", "enum": PLANNER_STAGE_SEQUENCE},
                    "stage_complete": {"type": "boolean"},
                    "observable_trace": {"type": "array", "items": {"type": "string"}},
                    "reply": {"type": "string"},
                    "patch": {
                        "type": "object",
                        "properties": patch_properties,
                    },
                },
                "required": [
                    "stage",
                    "stage_complete",
                    "reply",
                    "patch",
                ],
            },
        },
    ]


def _tool_input(tool_calls: list[dict], name: str) -> dict | None:
    for call in reversed(tool_calls):
        if call.get("name") == name and isinstance(call.get("input"), dict):
            return call["input"]
    return None


def _has_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _has_string_list(value: object, *, allow_empty: bool = False) -> bool:
    if not isinstance(value, list):
        return False
    if not value:
        return allow_empty
    return all(isinstance(item, str) and bool(item.strip()) for item in value)


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


def _canonicalize_stage_submit(
    submit: dict | None,
    *,
    expected_stage: str,
    fallback_goal: str,
    draft_state: dict,
) -> dict | None:
    if not isinstance(submit, dict):
        return None

    normalized = dict(submit)
    stage = str(normalized.get("stage") or expected_stage).strip() or expected_stage
    patch = normalized.get("patch") if isinstance(normalized.get("patch"), dict) else {}
    patch = dict(patch)

    # Backward-compatible: accept older full-payload fields and fold them into patch.
    for key in ("task_object", "project_summary", "shared_doc", "dag", "task_board", "action"):
        if key in normalized and key not in patch:
            patch[key] = normalized.get(key)

    reply = str(
        normalized.get("reply")
        or patch.get("reply")
        or ((patch.get("action") or {}).get("message") if isinstance(patch.get("action"), dict) else "")
        or ""
    ).strip()
    observable_trace = normalized.get("observable_trace")
    if not isinstance(observable_trace, list):
        observable_trace = []

    if "task_object" in patch and isinstance(patch.get("task_object"), dict):
        patch["task_object"] = _normalize_task_object(patch.get("task_object"), fallback_goal, reply)
    if "project_summary" in patch and isinstance(patch.get("project_summary"), dict):
        patch["project_summary"] = _normalize_project_summary(patch.get("project_summary"))
    if "shared_doc" in patch and not _has_text(patch.get("shared_doc")):
        patch["shared_doc"] = _default_shared_doc(
            patch.get("task_object") if isinstance(patch.get("task_object"), dict) else draft_state.get("task_object"),
            reply,
        )
    if "dag" in patch and isinstance(patch.get("dag"), dict):
        patch["dag"] = _normalize_dag(patch["dag"])
    if "task_board" in patch:
        patch["task_board"] = _normalize_task_board(
            patch.get("task_board"),
            patch.get("dag") if isinstance(patch.get("dag"), dict) else draft_state.get("dag"),
        )
    if "action" in patch and isinstance(patch.get("action"), dict):
        default_action = "update_dag" if expected_stage == "fill_dag" else "report_blocker"
        patch["action"] = _normalize_action(patch.get("action"), reply, default_action)

    return {
        "stage": stage,
        "stage_complete": bool(normalized.get("stage_complete")),
        "reply": reply,
        "observable_trace": [str(item).strip() for item in observable_trace if str(item).strip()],
        "patch": patch,
    }


def _validate_stage_submit(submit: dict | None, stage: str) -> list[dict]:
    blockers: list[dict] = []
    if not isinstance(submit, dict):
        return [{"code": "planner_tool_missing", "message": "Planner 没有调用 planner_submit_turn。"}]

    if str(submit.get("stage") or "") != stage:
        blockers.append({
            "code": "planner_stage_mismatch",
            "message": f"当前阶段应为 {stage}，但提交成了 {submit.get('stage') or '空'}。",
        })
    if not submit.get("stage_complete"):
        blockers.append({
            "code": "planner_stage_incomplete",
            "message": "当前阶段没有标记 stage_complete=true。",
        })

    patch = submit.get("patch") if isinstance(submit.get("patch"), dict) else {}
    if stage == "plan_outline":
        if not _has_text(submit.get("reply")):
            blockers.append({"code": "reply_missing", "message": "plan_outline 缺少给用户看的规划说明 reply。"})
        if not isinstance(submit.get("observable_trace"), list) or not submit.get("observable_trace"):
            blockers.append({"code": "observable_trace_missing", "message": "plan_outline 缺少 observable_trace。"})
    elif stage == "fill_task_context":
        task_object = patch.get("task_object")
        project_summary = patch.get("project_summary")
        shared_doc = patch.get("shared_doc")
        if not isinstance(task_object, dict) or not _has_text(task_object.get("title")) or not _has_text(task_object.get("objective")):
            blockers.append({"code": "task_object_incomplete", "message": "fill_task_context 缺少完整 task_object。"})
        if not isinstance(project_summary, dict) or not _has_text(project_summary.get("project_type")):
            blockers.append({"code": "project_summary_incomplete", "message": "fill_task_context 缺少 project_summary。"})
        if not _has_text(shared_doc):
            blockers.append({"code": "shared_doc_missing", "message": "fill_task_context 缺少 shared_doc。"})
    elif stage == "fill_dag":
        dag = patch.get("dag")
        action = patch.get("action")
        if not isinstance(dag, dict) or not isinstance(dag.get("nodes"), list) or not dag.get("nodes"):
            blockers.append({"code": "dag_missing", "message": "fill_dag 缺少可用 dag.nodes。"})
        elif len(dag.get("nodes") or []) < 6:
            blockers.append({"code": "dag_too_small", "message": "fill_dag 生成的 DAG 节点过少，疑似退化成最小草案。"})
        if not isinstance(action, dict) or str(action.get("action") or "") not in {"update_dag", "assess", "report_blocker"}:
            blockers.append({"code": "action_invalid", "message": "fill_dag 缺少有效 action。"})
    elif stage == "fill_task_board":
        board = patch.get("task_board")
        if not isinstance(board, list) or not board:
            blockers.append({"code": "task_board_missing", "message": "fill_task_board 缺少 task_board。"})
    elif stage == "finalize_ready":
        action = patch.get("action")
        if not isinstance(action, dict) or str(action.get("action") or "") not in {"update_dag", "set_ready", "report_blocker", "assess"}:
            blockers.append({"code": "final_action_invalid", "message": "finalize_ready 缺少最终 action。"})
    return blockers


def _merge_stage_patch(draft_state: dict, stage_submit: dict) -> dict:
    patch = stage_submit.get("patch") if isinstance(stage_submit.get("patch"), dict) else {}
    merged = dict(draft_state)
    for key in ("task_object", "project_summary", "shared_doc", "dag", "task_board", "action"):
        if key in patch and patch.get(key) is not None:
            merged[key] = patch.get(key)
    merged["current_stage"] = stage_submit.get("stage")
    merged["updated_at"] = datetime.now(timezone.utc).isoformat()
    if isinstance(merged.get("action"), dict):
        merged["blockers"] = merged["action"].get("blockers") or []
    return merged


def _missing_stage_fields(stage: str, draft_state: dict) -> list[str]:
    missing = []
    for field in PLANNER_STAGE_FIELDS.get(stage, []):
        value = draft_state.get(field)
        if field == "reply":
            continue
        if field == "observable_trace":
            continue
        if field == "shared_doc":
            if not _has_text(value):
                missing.append(field)
        elif field == "dag":
            if not isinstance(value, dict) or not isinstance(value.get("nodes"), list) or not value.get("nodes"):
                missing.append(field)
        elif field == "task_board":
            if not isinstance(value, list) or not value:
                missing.append(field)
        elif field == "action":
            if not isinstance(value, dict) or not _has_text(value.get("action")):
                missing.append(field)
        elif not value:
            missing.append(field)
    return missing


def _draft_state_ui_payload(draft_state: dict) -> dict:
    return {
        "current_stage": draft_state.get("current_stage"),
        "lifecycle_phase": draft_state.get("lifecycle_phase"),
        "task_object": draft_state.get("task_object"),
        "project_summary": draft_state.get("project_summary"),
        "shared_doc": draft_state.get("shared_doc"),
        "task_board": draft_state.get("task_board"),
        "dag": draft_state.get("dag"),
        "blockers": draft_state.get("blockers") or [],
        "action": draft_state.get("action"),
        "system_generated_dag": bool(draft_state.get("system_generated_dag")),
        "updated_at": draft_state.get("updated_at"),
    }


def _salvage_stage_submit_from_text(
    *,
    stage: str,
    raw_response: str,
    fallback_goal: str,
    draft_state: dict,
) -> dict | None:
    if not raw_response.strip():
        return None

    patch: dict = {}
    if stage == "fill_dag":
        dag = _extract_dag_from_text(raw_response)
        action = _extract_action_from_text(raw_response)
        if dag:
            patch["dag"] = dag
        if action:
            patch["action"] = _normalize_action(action, "", "update_dag")
    elif stage == "plan_outline":
        parsed = parse_reply = ""
        import re
        reply_matches = re.findall(r"```reply\s*\n(.*?)\n```", raw_response, re.DOTALL)
        if reply_matches:
            parsed = str(reply_matches[-1]).strip()
        parse_reply = parsed
        trace_matches = re.findall(r"```observe\s*\n(.*?)\n```", raw_response, re.DOTALL)
        observable_trace = []
        if trace_matches:
            observable_trace = [
                line.strip().removeprefix("-").strip()
                for line in trace_matches[-1].splitlines()
                if line.strip()
            ]
        return {
            "stage": stage,
            "stage_complete": bool(parse_reply or observable_trace),
            "reply": parse_reply,
            "observable_trace": observable_trace,
            "patch": {},
        }

    if not patch:
        return None
    return {
        "stage": stage,
        "stage_complete": True,
        "reply": "",
        "observable_trace": [],
        "patch": patch,
    }


def _validate_planner_submit_contract(
    submit: dict | None,
    action_name: str,
    dag: dict | None,
) -> list[dict]:
    """Validate that the Planner used the tool interface with usable panel data."""
    blockers: list[dict] = []

    if not isinstance(submit, dict):
        return [{
            "code": "planner_tool_missing",
            "message": "Planner 没有调用 planner_submit_turn 工具；系统不会从普通文本里猜测面板和 DAG。",
        }]

    action_requires_dag = action_name in {"update_dag", "set_ready"}
    if action_requires_dag and not (isinstance(dag, dict) and isinstance(dag.get("nodes"), list) and dag.get("nodes")):
        blockers.append({
            "code": "planner_dag_missing",
            "message": "planner_submit_turn 缺少可用 dag.nodes，无法更新画布。",
        })

    task_object = submit.get("task_object")
    if not isinstance(task_object, dict):
        blockers.append({
            "code": "task_object_missing",
            "message": "planner_submit_turn 缺少 task_object，左侧任务对象无法填充。",
        })
    else:
        required_text = ["title", "objective", "background"]
        missing_text = [key for key in required_text if not _has_text(task_object.get(key))]
        required_lists = ["constraints", "success_criteria"]
        missing_lists = [
            key for key in required_lists
            if not _has_string_list(task_object.get(key), allow_empty=False)
        ]
        for optional_key in ("assumptions", "open_questions"):
            if optional_key in task_object and not _has_string_list(task_object.get(optional_key), allow_empty=True):
                missing_lists.append(optional_key)
        if missing_text or missing_lists:
            blockers.append({
                "code": "task_object_incomplete",
                "message": f"task_object 字段不完整：缺少 {', '.join(missing_text + missing_lists)}。",
            })

    project_summary = submit.get("project_summary")
    project_summary_keys = [
        "project_type",
        "tech_stack",
        "startup",
        "build",
        "tests",
        "key_directories",
        "risk_points",
        "suggested_next_steps",
    ]
    if not isinstance(project_summary, dict):
        blockers.append({
            "code": "project_summary_missing",
            "message": "planner_submit_turn 缺少 project_summary，左侧项目摘要无法填充。",
        })
    else:
        missing_summary = []
        for key in project_summary_keys:
            value = project_summary.get(key)
            if key == "project_type":
                if not _has_text(value):
                    missing_summary.append(key)
            elif not _has_string_list(value, allow_empty=True):
                missing_summary.append(key)
        if missing_summary:
            blockers.append({
                "code": "project_summary_incomplete",
                "message": f"project_summary 字段不完整：缺少 {', '.join(missing_summary)}。",
            })

    if not _has_text(submit.get("shared_doc")):
        blockers.append({
            "code": "shared_doc_missing",
            "message": "planner_submit_turn 缺少 shared_doc，项目文档无法填充。",
        })

    task_board = submit.get("task_board")
    if not isinstance(task_board, list) or not task_board:
        blockers.append({
            "code": "task_board_missing",
            "message": "planner_submit_turn 缺少 task_board，任务面板无法填充。",
        })
    else:
        invalid = [
            str(index)
            for index, item in enumerate(task_board)
            if not isinstance(item, dict) or not _has_text(item.get("id")) or not _has_text(item.get("title"))
        ]
        if invalid:
            blockers.append({
                "code": "task_board_incomplete",
                "message": f"task_board 存在无效任务项：{', '.join(invalid)}。",
            })

    if blockers:
        logger.warning(
            "Planner submit contract invalid: action=%s blockers=%s submit_keys=%s",
            action_name,
            [item["code"] for item in blockers],
            sorted(submit.keys()),
        )
    return blockers


def _build_planner_retry_message(blockers: list[dict]) -> str:
    lines = [
        "你上一轮调用 `planner_submit_turn` 的参数不完整，系统没有接受更新。",
        "请立刻重新调用 `planner_submit_turn`，不要输出普通正文，只补齐完整工具参数。",
        "本轮只修正当前阶段需要的字段，并确保 `stage`、`stage_complete`、`patch` 正确。",
        "本轮缺失/无效项如下：",
    ]
    for blocker in blockers:
        code = str(blocker.get("code") or "unknown")
        message = str(blocker.get("message") or "").strip()
        lines.append(f"- {code}: {message}")
    lines.append("要求：沿用你刚才的规划意图，输出更小、更稳定、字段完整的结构化结果。")
    return "\n".join(lines)


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
        existing_ui_state = metadata.get("planner_ui_state") if isinstance(metadata.get("planner_ui_state"), dict) else {}
        existing_draft_state = metadata.get("planner_draft_state") if isinstance(metadata.get("planner_draft_state"), dict) else {}
        draft_state = {
            "current_stage": "plan_outline",
            "lifecycle_phase": workflow.lifecycle_phase,
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

        yield _sse({"type": "planner_status", "message": "Planner 请求已发送，开始分阶段规划。"})

        for stage in PLANNER_STAGE_SEQUENCE:
            stage_label = PLANNER_STAGE_LABELS[stage]
            stage_retry_feedback = ""
            stage_success = False
            draft_state["current_stage"] = stage
            draft_state["updated_at"] = datetime.now(timezone.utc).isoformat()

            yield _sse({"type": "planner_stage_status", "stage": stage, "message": stage_label})
            yield _sse({
                "type": "planner_observable_progress",
                "stage": stage,
                "status": "started",
                "attempt": 1,
                "received_fields": [field for field in PLANNER_STAGE_FIELDS.get(stage, []) if field not in _missing_stage_fields(stage, draft_state)],
                "missing_fields": _missing_stage_fields(stage, draft_state),
                "next_action": PLANNER_STAGE_TIMELINE_MESSAGES[stage],
                "draft_state": _draft_state_ui_payload(draft_state),
            })

            max_stage_attempts = 3 if stage == "fill_dag" else 2
            for attempt_index in range(max_stage_attempts):
                stage_messages = _build_stage_messages(
                    stage=stage,
                    latest_user_message=body.message,
                    workflow_goal=workflow_goal,
                    lifecycle_phase=workflow.lifecycle_phase,
                    draft_state=draft_state,
                    blockers=draft_state.get("blockers") or [],
                    recent_messages=messages,
                    retry_feedback=stage_retry_feedback,
                )
                tool_calls: list[dict] = []
                raw_response = ""
                if attempt_index > 0:
                    yield _sse({
                        "type": "planner_stage_status",
                        "stage": stage,
                        "message": f"{stage_label}（重试第 {attempt_index + 1} 次）",
                    })

                async for event in _call_llm_stream(
                    stage_messages,
                    PLANNER_CHAT_SYSTEM,
                    body.thinking_level,
                    tools=_planner_tools(stage),
                ):
                    event_type = event.get("type")
                    chunk = str(event.get("content") or "")
                    if event_type == "status":
                        yield _sse({"type": "planner_status", "message": chunk})
                        continue
                    if event_type == "thinking":
                        yield _sse({"type": "thinking_delta", "content": chunk})
                        continue
                    if event_type == "tool_call":
                        tool_calls.append(event)
                        yield _sse({
                            "type": "planner_tool_call",
                            "name": event.get("name"),
                            "id": event.get("id", ""),
                            "input_keys": sorted((event.get("input") or {}).keys()) if isinstance(event.get("input"), dict) else [],
                        })
                        continue
                    raw_response += chunk
                    if stage == "plan_outline":
                        yield _sse({"type": "text", "content": chunk})

                submit = _canonicalize_stage_submit(
                    _tool_input(tool_calls, "planner_submit_turn"),
                    expected_stage=stage,
                    fallback_goal=workflow_goal,
                    draft_state=draft_state,
                )
                if submit is None:
                    submit = _salvage_stage_submit_from_text(
                        stage=stage,
                        raw_response=raw_response,
                        fallback_goal=workflow_goal,
                        draft_state=draft_state,
                    )
                blockers = _validate_stage_submit(submit, stage)
                if not blockers and submit is not None:
                    draft_state = _merge_stage_patch(draft_state, submit)
                    if stage == "plan_outline":
                        trace = submit.get("observable_trace") if isinstance(submit.get("observable_trace"), list) else []
                        reply = str(submit.get("reply") or "").strip()
                        synthetic_response = ""
                        if trace:
                            synthetic_response += "```observe\n" + "\n".join(f"- {line}" for line in trace if line) + "\n```\n"
                        if reply:
                            synthetic_response += f"```reply\n{reply}\n```"
                        assistant_visible_response = synthetic_response or raw_response
                        if synthetic_response and synthetic_response.strip() != raw_response.strip():
                            yield _sse({"type": "text", "content": synthetic_response})
                    if stage == "fill_dag" and isinstance(draft_state.get("dag"), dict):
                        yield _sse({"type": "dag_update", "dag": draft_state["dag"], "draft": True})
                    yield _sse({
                        "type": "planner_stage_result",
                        "stage": stage,
                        "status": "completed",
                        "attempt": attempt_index + 1,
                        "applied_fields": sorted((submit.get("patch") or {}).keys()),
                        "summary": stage_label,
                        "draft_state": _draft_state_ui_payload(draft_state),
                    })
                    yield _sse({
                        "type": "planner_observable_progress",
                        "stage": stage,
                        "status": "completed",
                        "attempt": attempt_index + 1,
                        "received_fields": sorted((submit.get("patch") or {}).keys()),
                        "missing_fields": _missing_stage_fields(stage, draft_state),
                        "next_action": PLANNER_STAGE_TIMELINE_MESSAGES.get(
                            PLANNER_STAGE_SEQUENCE[PLANNER_STAGE_SEQUENCE.index(stage) + 1],
                            "准备结束本轮规划。",
                        ) if stage != "finalize_ready" else "本轮规划已完成。",
                        "draft_state": _draft_state_ui_payload(draft_state),
                    })
                    stage_history.append({
                        "stage": stage,
                        "status": "completed",
                        "attempt": attempt_index + 1,
                        "summary": stage_label,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    })
                    stage_success = True
                    break

                stage_retry_feedback = _build_planner_retry_message(blockers)
                retry_status = "retrying" if attempt_index < max_stage_attempts - 1 else "failed"
                stage_history.append({
                    "stage": stage,
                    "status": retry_status,
                    "attempt": attempt_index + 1,
                    "summary": "；".join(item.get("message", "") for item in blockers),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })
                yield _sse({
                    "type": "planner_observable_progress",
                    "stage": stage,
                    "status": retry_status,
                    "attempt": attempt_index + 1,
                    "received_fields": sorted(((submit or {}).get("patch") or {}).keys()) if isinstance(submit, dict) else [],
                    "missing_fields": [item.get("code") for item in blockers],
                    "next_action": "只重试当前阶段，不回退整轮。",
                    "draft_state": _draft_state_ui_payload(draft_state),
                })

            if stage_success:
                continue

            if stage == "fill_task_context":
                draft_state["task_object"] = _normalize_task_object(
                    draft_state.get("task_object"),
                    workflow_goal,
                    assistant_visible_response,
                )
                draft_state["project_summary"] = _normalize_project_summary(draft_state.get("project_summary"))
                draft_state["shared_doc"] = draft_state.get("shared_doc") or _default_shared_doc(
                    draft_state.get("task_object"),
                    assistant_visible_response,
                )
                yield _sse({
                    "type": "planner_stage_result",
                    "stage": stage,
                    "status": "fallback",
                    "attempt": 2,
                    "applied_fields": ["task_object", "project_summary", "shared_doc"],
                    "summary": "当前阶段由系统补默认摘要继续推进。",
                    "draft_state": _draft_state_ui_payload(draft_state),
                })
                continue

            if stage == "fill_dag":
                draft_state["dag"] = _build_minimal_planner_dag(
                    str((draft_state.get("task_object") or {}).get("title") or workflow_goal),
                    str((draft_state.get("task_object") or {}).get("objective") or workflow_goal),
                )
                draft_state["system_generated_dag"] = True
                draft_state["action"] = _normalize_action({
                    "action": "update_dag",
                    "message": "Planner 的 DAG 阶段连续失败，系统已补一个最小草案 DAG 以继续推进。",
                })
                yield _sse({
                    "type": "planner_stage_result",
                    "stage": stage,
                    "status": "fallback",
                    "attempt": max_stage_attempts,
                    "applied_fields": ["dag", "action"],
                    "summary": "DAG 阶段失败，系统已补最小草案 DAG。",
                    "draft_state": _draft_state_ui_payload(draft_state),
                })
                yield _sse({"type": "dag_update", "dag": draft_state["dag"], "draft": True})
                continue

            if stage == "fill_task_board":
                draft_state["task_board"] = _build_task_board_from_dag(draft_state.get("dag"))
                yield _sse({
                    "type": "planner_stage_result",
                    "stage": stage,
                    "status": "fallback",
                    "attempt": max_stage_attempts,
                    "applied_fields": ["task_board"],
                    "summary": "任务面板阶段失败，系统已从 DAG 自动派生任务卡。",
                    "draft_state": _draft_state_ui_payload(draft_state),
                })
                continue

            if stage == "finalize_ready":
                requested_run = any(token in body.message for token in ("运行", "执行", "开始", "run", "start"))
                if requested_run and isinstance(draft_state.get("dag"), dict) and (draft_state["dag"].get("nodes") or []):
                    draft_state["action"] = _normalize_action({
                        "action": "set_ready",
                        "message": "方案已就绪，请点击顶部运行按钮开始执行。",
                    })
                else:
                    draft_state["action"] = _normalize_action({
                        "action": "update_dag",
                        "message": "当前方案已进入 planning，可继续调整或直接准备运行。",
                    })
                continue

            draft_state["action"] = _normalize_action({
                "action": "report_blocker",
                "message": f"{stage_label}失败，且系统无法安全补全。",
                "blockers": [{
                    "code": f"{stage}_failed",
                    "message": f"{stage_label}失败，且系统无法安全补全。",
                }],
            }, default_action="report_blocker")
            break

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
