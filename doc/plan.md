# Multi-Agent Workflow OS — 技术规划与实施蓝图 v4

> **产品定位**：面向复杂工程的 AI Agent Orchestration OS（编排操作系统）
> **核心壁垒**：基于 OpenCode 的 Agent 引擎 + Git 原生 Checkpoint（防自杀隔离）+ 流式反馈 + 安全沙盒
> **技术哲学**：绝不重造轮子 — 调度用 Temporal，Agent 用 OpenCode，隔离用 Docker/gVisor
> **MVP 原则**：单语言（Python）先行，Go 网关延后引入，避免过早微服务化
> **防爆原则**：每一个外部输入都可能是恶意的或失控的，系统必须在边界处设防

---

## 一、OpenCode 集成策略（核心决策）

### 1.1 为什么选 OpenCode 作为 Agent 引擎

OpenCode 是一个完全开源（MIT）的 AI 编程代理，具备完整的 Agent 系统，我们**直接复用其能力层，而非从零构建 Agent**：

| OpenCode 能力 | 我们如何复用 |
|--------------|-------------|
| 6 个内置 Agent（build/plan/@general/@explore） | 映射为工作流节点类型 |
| 20+ 编程工具（read/edit/bash/grep 等） | 直接使用，无需自建 Tool 层 |
| MCP 原生支持 | 与 Orchestrator 通过 MCP 通信（见 1.5 MCP 拓扑） |
| 75+ 模型提供商（Vercel AI SDK v5） | 每个节点独立选模型 |
| LSP 集成（40+ 语言） | 代码智能开箱即用 |
| 权限系统（allow/deny/ask） | 控制每个节点的安全边界 |
| Session 管理 + 上下文压缩 | 复用会话持久化和压缩逻辑 |
| Oh My OpenCode (OMO) 插件 | 增强多模型协作、Skills 系统 |

### 1.2 集成方式演进路线

**Phase 0-1：文件通道 + CLI 模式**
- 每个 Agent 节点在独立 Docker 沙盒中启动一个 OpenCode 实例
- OpenCode 将结构化日志（JSONL）输出到沙盒内文件 `/workspace/.opencode/stream.jsonl`
- Python 守护进程通过 Docker API 挂载卷读取该文件（避免 stdout 污染）
- OpenCode 退出后通过 exit code 判断成功/失败

```
Orchestrator (Python)
  │
  ├─ 1. 创建沙盒容器 + 挂载 workspace volume
  ├─ 2. 注入 OpenCode 配置 (模型/权限/MCP，含 run_id)
  ├─ 3. 在容器内执行: stdbuf -o0 opencode task --agent build --prompt "..."
  │     (OpenCode 将结构化日志写入 /workspace/.opencode/stream.jsonl)
  │     (stdbuf -o0 强制关闭缓冲，保证实时写入)
  │
  ├─ 4. FileWatcher tail -f 读取 stream.jsonl → 解析 → Redis Pub/Sub
  │     (FileWatcher 带 50MB 上限保护，防 Log Bomb)
  │
  └─ 5. OpenCode 进程退出 → exit code → 触发下游节点
```

> **为什么不直接读 stdout？** 真实工程中 npm/pip/gcc 会向 stdout 输出非标准 JSON 警告，
> 甚至不可见的控制字符，导致 JSON 解析频繁崩溃。文件通道是更可靠的方案。
>
> **文件缓冲对策**：Linux 非 tty 环境下默认 Block Buffering（4KB），会导致打字机效果
> 变成"一段一段蹦出来"。使用 `stdbuf -o0` 或 Bun 的 `NODE_OPTIONS=--no-buffering`
> 强制无缓冲写入，确保流式体验顺滑。
>
> **Log Bomb 防御**：若 Shell 节点执行 `cat /dev/urandom` 等无限输出脚本，
> stream.jsonl 会在几秒内膨胀到几十 GB。FileWatcher 设置 50MB 硬上限，
> 超出立即截断并 kill OpenCode 进程，抛出 `LogLimitExceeded` 错误。

**Phase 2：MCP Server 模式**
- Orchestrator 启动一个 MCP Server（Workflow OS MCP），提供工作流级工具
- OpenCode 作为 MCP Client 连接到 Orchestrator
- 支持暂停、恢复、查询上游状态、请求人工干预等高级控制

**Phase 3：SDK 深度集成**
- 导入 OpenCode 核心模块作为库
- 自定义 Agent 注册到 OpenCode 的插件系统
- 构建垂直领域专用 Agent（BSP/嵌入式/测试链）

### 1.3 OpenCode Agent → 工作流节点映射

| 工作流节点类型 | OpenCode Agent | 权限 | 用途 |
|--------------|---------------|------|------|
| **Coder Node** | `build` | 完全（读写+执行） | 代码生成、修改、重构 |
| **Plan Node** | `plan` | 只读 | 代码分析、架构评审、方案规划 |
| **Explore Node** | `@explore` | 只读 | 快速代码搜索、依赖分析 |
| **General Node** | `@general` | 完全 | 复杂多步任务（自定义） |
| **Shell Node** | 自定义 Agent + `bash` only | 仅执行 | 命令行执行（编译/测试/部署） |
| **Review Node** | `plan` + 自定义 prompt | 只读 | 代码审查、质量检查 |
| **Human Node** | 无 Agent | — | 人工审批/决策点 |

### 1.4 OpenCode 配置注入（含 run_id 命名空间隔离）

每个工作流节点会动态生成 OpenCode 配置。**关键**：`run_id` 强制注入到 MCP Server URL，
确保每个 Agent 的 KV 操作自动限定在当前 Run 的作用域内，防止 Agent A 修改 Agent B 的数据。

```json
{
  "model": {
    "provider": "anthropic",
    "id": "claude-sonnet-4-20250514"
  },
  "agents": {
    "build": {
      "tools": ["read", "edit", "write", "bash", "glob", "grep"],
      "permissions": {
        "bash": "allow",
        "write": "allow",
        "edit": "allow"
      }
    },
    "plan": {
      "tools": ["read", "glob", "grep", "codesearch"],
      "permissions": {
        "read": "allow",
        "bash": "deny",
        "write": "deny"
      }
    }
  },
  "mcp": {
    "servers": {
      "workflow-os": {
        "url": "http://host.docker.internal:8765/mcp?run_id={{RUN_ID}}&node_id={{NODE_ID}}",
        "tools": ["query_upstream_status", "request_human_approval", "read_shared_kv", "write_shared_kv"]
      }
    }
  }
}
```

> **命名空间隔离原理**：Python 生成 config 时将 `run_id` 和 `node_id` 拼入 URL。
> MCP Server 收到请求后，在底层自动将所有 KV 操作限定在 `WHERE run_id = ?` 的作用域内。
> Agent 完全无感知，它只知道调用 `write_shared_kv("key", "value")`，
> 但数据实际上被隔离在当前 Run 的命名空间中。

### 1.5 MCP 拓扑结构（关键设计）

```
┌─────────────────────────────────────────────────────────┐
│  Orchestrator (Python)                                  │
│  ┌───────────────────────────────────────────────────┐  │
│  │  Workflow OS MCP Server                           │  │
│  │  提供 tools:                                      │  │
│  │  - query_upstream(node_id) → 查询上游节点结果     │  │
│  │  - request_human_approval(reason) → 触发人工审批  │  │
│  │  - read_shared_kv(key) → 读取全局共享数据         │  │
│  │  - write_shared_kv(key, value) → 写入共享数据     │  │
│  │  - report_progress(percent) → 报告进度            │  │
│  │  - block_dangerous_ops(policy) → 拦截危险操作     │  │
│  └───────────────────────┬───────────────────────────┘  │
│                          │ MCP 协议                      │
│              ┌───────────┴───────────┐                   │
│              │                       │                   │
│         ┌────▼─────┐          ┌─────▼────┐              │
│         │ OpenCode  │          │ OpenCode  │              │
│         │ (沙盒 A)  │          │ (沙盒 B)  │              │
│         │ MCP Client│          │ MCP Client│              │
│         └──────────┘          └──────────┘              │
└─────────────────────────────────────────────────────────┘
```

**优势**：
- Python 层可拦截 OpenCode 的危险操作（比直接禁掉 bash tool 更细粒度）
- OpenCode 能通过 MCP 工具"查询上游节点状态"、"请求人工干预"——不需要硬编码
- 跨节点数据传递通过 `read_shared_kv`，天然解耦

---

## 二、系统架构

### 2.1 MVP 架构（Phase 0-1：单语言 Python）

```
┌──────────────────────────────────────────────────────────────┐
│                     Web Frontend (Next.js 14)                │
│     [React Flow 画布] [Monaco] [Xterm.js] [Zustand Store]   │
└────────────┬──────────────────────────────────┬──────────────┘
             │ REST API                          │ WebSocket (实时流)
┌────────────▼──────────────────────────────────▼──────────────┐
│           All-in-One Server (Python / FastAPI)                │
│                                                               │
│  ┌─────────────────┐  ┌──────────────────┐  ┌─────────────┐ │
│  │  REST Handler   │  │  WebSocket Hub   │  │  Temporal   │ │
│  │  (工作流/运行)   │  │  (asyncio 推流)  │  │  Client     │ │
│  └─────────────────┘  └──────────────────┘  └─────────────┘ │
│                                                               │
│  ┌─────────────────┐  ┌──────────────────┐  ┌─────────────┐ │
│  │  MCP Server     │  │  OpenCode Agent  │  │  Sandbox    │ │
│  │  (工作流级工具)  │  │  Wrapper         │  │  Manager    │ │
│  └─────────────────┘  └──────────────────┘  └─────────────┘ │
│                                                               │
│  ┌─────────────────────────────────────────────────────────┐ │
│  │  Streaming Pipeline                                     │ │
│  │  文件 JSONL 读取 → 解析 → 节流合并 → Redis → WS 推送    │ │
│  └─────────────────────────────────────────────────────────┘ │
└────────────┬─────────────────────────────────────────────────┘
             │ 任务分发
┌────────────▼─────────────────────────────────────────────────┐
│              Agent Runtime Fleet (OpenCode 实例)              │
│                                                               │
│  ┌──────────────────┐  ┌──────────────────┐                  │
│  │   沙盒容器 A      │  │   沙盒容器 B      │                  │
│  │   opencode build │  │   opencode plan  │                  │
│  │   共享 Git 仓库  │  │   共享 Git 仓库  │                  │
│  │   stream.jsonl   │  │   stream.jsonl   │                  │
│  └──────────────────┘  └──────────────────┘                  │
└──────────────────────────────────────────────────────────────┘
             │
┌────────────▼─────────────────────────────────────────────────┐
│        Data Layer (PostgreSQL + Redis + MinIO)               │
└──────────────────────────────────────────────────────────────┘
```

> **为什么 MVP 去掉 Go 网关？**
> Python FastAPI + asyncio 完全能扛住几千并发的 WebSocket 流式下发。
> MVP 阶段引入 Go 仅为了做路由，会带来跨语言联调、接口对齐、部署复杂性。
> 等 Phase 2 真正遇到 WebSocket 性能瓶颈时再引入 Go。

### 2.2 生产架构（Phase 2-3：Go 网关拆分）

```
              Frontend
                 │
    ┌────────────▼────────────┐
    │   Go API Gateway (Gin)  │  ← Phase 2 引入，专注 WebSocket + 路由 + 鉴权
    └────────────┬────────────┘
                 │
    ┌────────────▼────────────┐
    │  Python Orchestrator    │  ← 保留核心编排逻辑
    │  (Temporal + Agent +    │
    │   Sandbox + MCP Server) │
    └─────────────────────────┘
```

### 2.3 数据流（Streaming 全链路）

```
OpenCode 实例 (Docker 沙盒内)
  │ opencode task --agent build --prompt "..."
  │ 结构化日志写入: /workspace/.opencode/stream.jsonl
  │
  ▼
Python 守护进程 (tail -f 读挂载卷)
  │ 文件 JSONL → 逐行解析
  │ 标准化为事件:
  │ {"run_id":"123", "node":"coder1", "type":"llm_token", "content":"def "}
  │ {"run_id":"123", "node":"coder1", "type":"tool_call", "tool":"edit", "file":"main.py"}
  │ {"run_id":"123", "node":"coder1", "type":"shell_stdout", "content":"npm WARN"}
  │
  ▼ 节流合并 (shell_stdout 每 100ms 合并为一个 chunk)
Redis Pub/Sub
  │
  ▼ 订阅频道 run:123:stream
FastAPI WebSocket Handler
  │ 找到对应用户的 WebSocket 连接
  ▼ push (shell_stdout 已合并，避免 Xterm.js 卡死)
前端
  ├── "llm_token"    → Monaco Editor 打字机效果
  ├── "tool_call"    → 节点详情面板（显示正在编辑的文件）
  ├── "shell_stdout" → Xterm.js 终端渲染（节流后，不卡顿）
  └── "status"       → 节点状态更新（绿/红/黄/蓝）
```

> **Xterm.js 节流策略**：编译日志等大量 shell 输出会在 Python 端按 100ms 窗口合并为单个 chunk，
> 避免逐行推送导致浏览器渲染压力过大甚至卡死。

---

## 三、技术栈决策

### 3.1 MVP 技术栈（Phase 0-1）

| 层 | 技术 | 选型理由 |
|----|------|----------|
| **Agent 引擎** | **OpenCode (MIT)** | 内置 Agent/Tool/MCP/LSP，75+ 模型，开箱即用 |
| 前端画布 | React Flow v12 | 最成熟的节点编辑器 |
| 前端框架 | Next.js 14 (App Router) | SSR + TailwindCSS + Zustand |
| **后端（All-in-One）** | **Python / FastAPI** | REST + WebSocket + Temporal Client + MCP Server 一体化 |
| 编排引擎 | Temporal.io (Python SDK) | 原生重试/暂停/恢复/事件历史 |
| 沙盒 | Docker | MVP 够用 |
| 消息总线 | Redis Pub/Sub | Python 客户端成熟 |
| 数据库 | PostgreSQL 15 + JSONB | DAG 天然适配 JSON |
| 对象存储 | MinIO (本地) | 产物、快照 |
| Agent 通信 | MCP | OpenCode 原生支持 |

### 3.2 生产技术栈（Phase 2-3 扩展）

| 新增层 | 技术 | 引入时机 |
|--------|------|----------|
| API 网关 | Go / Gin | Phase 2（WS 并发瓶颈时） |
| 安全沙盒 | gVisor | Phase 2 |
| 向量存储 | PGVector | Phase 2 |
| 云存储 | S3 | Phase 3 |

---

## 四、Monorepo 目录结构

```
multi-agent-studio/
├── apps/
│   ├── web/                        # Next.js 14 前端
│   │   ├── src/
│   │   │   ├── components/
│   │   │   │   ├── canvas/         # React Flow 画布
│   │   │   │   │   ├── FlowCanvas.tsx
│   │   │   │   │   ├── nodes/
│   │   │   │   │   │   ├── CoderNode.tsx
│   │   │   │   │   │   ├── PlanNode.tsx
│   │   │   │   │   │   ├── ExploreNode.tsx
│   │   │   │   │   │   ├── ShellNode.tsx
│   │   │   │   │   │   ├── ReviewNode.tsx
│   │   │   │   │   │   └── HumanNode.tsx
│   │   │   │   │   └── edges/
│   │   │   │   ├── panels/
│   │   │   │   └── common/
│   │   │   ├── stores/
│   │   │   │   ├── workflowStore.ts
│   │   │   │   └── runStore.ts
│   │   │   ├── hooks/
│   │   │   │   └── useWebSocket.ts
│   │   │   └── lib/
│   │   │       └── api.ts
│   │   ├── package.json
│   │   └── next.config.js
│   │
│   ├── gateway/                    # Go API 网关 (Phase 2 才启用)
│   │   └── ...
│   │
│   └── orchestrator/              # Python 编排引擎 (MVP 核心)
│       ├── pyproject.toml
│       ├── app/
│       │   ├── main.py            # FastAPI 入口 (REST + WebSocket + Temporal)
│       │   ├── ws/                # WebSocket Hub (asyncio)
│       │   │   └── hub.py
│       │   ├── api/               # REST 接口
│       │   │   ├── workflows.py
│       │   │   ├── runs.py
│       │   │   └── models.py
│       │   ├── workflows/         # Temporal workflow 定义
│       │   │   ├── compiler.py    # DAG Compiler
│       │   │   ├── code_workflow.py
│       │   │   └── activities.py  # Temporal Activities (异步轮询模式)
│       │   ├── agents/            # OpenCode Agent 封装层
│       │   │   ├── base.py        # BaseAgent 抽象类
│       │   │   ├── opencode.py    # OpenCode Wrapper (文件通道模式)
│       │   │   ├── factory.py     # Agent 工厂
│       │   │   └── config.py      # OpenCode 配置动态生成器
│       │   ├── sandbox/           # Docker 沙盒管理
│       │   │   ├── manager.py     # SandboxManager
│       │   │   ├── provision.py   # 沙盒初始化
│       │   │   └── checkpoint.py  # Git Checkpoint 管理
│       │   ├── streaming/         # 流式输出处理
│       │   │   ├── file_watcher.py  # stream.jsonl 文件监听
│       │   │   ├── parser.py        # JSONL 解析器
│       │   │   ├── throttler.py     # 节流合并器 (100ms 窗口)
│       │   │   └── publisher.py     # Redis publisher
│       │   ├── mcp_server/        # Workflow OS MCP Server
│       │   │   ├── server.py      # MCP Server 主入口
│       │   │   └── tools.py       # 工作流级 MCP 工具定义
│       │   └── memory/            # 上下文/记忆管理
│       │       ├── context.py     # 上下文窗口管理
│       │       └── workspace_share.py  # Workspace 共享 + 文件传递
│       └── Dockerfile
│
├── packages/
│   └── shared-types/
│       └── schemas/
│           ├── workflow.json
│           ├── events.json
│           └── node-config.json
│
├── infra/
│   ├── docker-compose.yml
│   ├── sandbox-images/
│   │   └── base/
│   │       └── Dockerfile         # Ubuntu + opencode + git + python3 + gcc
│   └── temporal/
│       └── config.yaml
│
├── scripts/
│   ├── setup.sh
│   └── dev.sh
│
└── README.md
```

---

## 五、核心模块设计

### 5.1 Workflow Engine（系统大脑）

**核心原则：绝不手写状态机和 DAG 调度。强依赖 Temporal.io。**

- **DAG 解析**：前端传递 React Flow JSON → `DAGCompiler` 转化为 Temporal Workflow
  - 串行：A → B → C
  - 并行：A → [B, C] fork-join → D
  - 条件：A → (if success) B / (if fail) C
- **状态机与容错**：Temporal 原生记录所有 Event History
- **Human-in-the-Loop**：Temporal Signal 机制

#### 5.1.1 Temporal Activity 异步轮询模式（关键设计）

> **风险**：Temporal Activity 倾向于"无状态"且有 Timeout。如果 Coder Agent 在沙盒里执行了 20 分钟，
> Worker 重启或心跳丢失会触发 Activity 重试，导致同一节点被重复执行。

**解决方案**：Activity 不阻塞等待 Docker 执行完毕，而是拆分为"启动"和"轮询"两个阶段。

```python
@activity.defn
async def start_agent_task(node_config: dict) -> str:
    """Activity A: 仅负责启动任务，立即返回执行 ID"""
    sandbox_id = node_config["sandbox_id"]
    execution_id = str(uuid4())

    # 在沙盒中启动 OpenCode（后台执行）
    await sandbox_manager.exec_async(
        sandbox_id,
        cmd=f"opencode task --agent {node_config['agent_type']} "
            f"--prompt '{node_config['prompt']}' "
            f"> /workspace/.opencode/output.jsonl 2>&1",
        exec_id=execution_id
    )
    return execution_id


@workflow.defn
class AgentNodeWorkflow:
    """Workflow: 编排"启动 → 轮询 → 完成/失败"的全流程"""

    @workflow.run
    async def run(self, node_config: dict) -> dict:
        # Step 1: 启动任务（Activity，可重试）
        exec_id = await workflow.execute_activity(
            start_agent_task, node_config,
            start_to_close_timeout=timedelta(minutes=5),
            retry_policy=RetryPolicy(maximum_attempts=3)
        )

        # Step 2: 轮询任务状态（Workflow 级别，不怕 Worker 重启）
        while True:
            status = await workflow.execute_activity(
                check_agent_status, exec_id,
                start_to_close_timeout=timedelta(seconds=30)
            )
            if status["state"] in ("completed", "failed"):
                return status
            await workflow.sleep(timedelta(seconds=5))  # 5 秒轮询一次


@activity.defn
async def check_agent_status(exec_id: str) -> dict:
    """Activity B: 检查任务执行状态"""
    process = await sandbox_manager.get_process(exec_id)
    if process.running:
        return {"state": "running"}
    return {"state": "completed" if process.exit_code == 0 else "failed",
            "exit_code": process.exit_code}
```

**优势**：
- Worker 重启不影响 Workflow（状态在 Temporal Server 端持久化）
- Activity 超时不会导致重复执行（轮询 Activity 是幂等的）
- 天然支持 Kill & Resume（见 Phase 0 验证）

#### 5.1.2 Temporal Signal 替代轮询（Phase 1 升级路径）

> **隐患**：轮询模式每 5 秒产生一个 Activity Event。如果节点执行 1 小时 = 720 次 Activity
> = 几千个 Event History。Temporal 单 Workflow 有 50,000 Event 硬限，长时任务会逼近上限。
>
> **升级方案**：用 `workflow.wait_condition` + Signal 替代轮询，Event 从几千压缩到个位数。

```python
@workflow.defn
class AgentNodeWorkflowV2:
    """Phase 1 升级版：Signal 驱动，零轮询"""

    def __init__(self):
        self._task_completed = False
        self._task_result = None

    @workflow.run
    async def run(self, node_config: dict) -> dict:
        # Step 1: 启动任务（Activity，可重试）
        exec_id = await workflow.execute_activity(
            start_agent_task, node_config,
            start_to_close_timeout=timedelta(minutes=5),
            retry_policy=RetryPolicy(maximum_attempts=3)
        )

        # Step 2: 零轮询等待 — FileWatcher 检测到进程退出时发 Signal
        await workflow.wait_condition(lambda: self._task_completed)

        return self._task_result

    @workflow.signal
    async def on_task_completed(self, result: dict):
        """由 Python FileWatcher 在检测到 OpenCode 退出时调用"""
        self._task_result = result
        self._task_completed = True
```

```python
# Python 端 FileWatcher 检测到 OpenCode 进程退出后：
async def on_process_exit(exec_id: str, exit_code: int):
    temporal_client = await get_temporal_client()
    handle = temporal_client.get_workflow_handle(workflow_id)
    await handle.signal(
        "on_task_completed",
        {"state": "completed" if exit_code == 0 else "failed", "exit_code": exit_code}
    )
```

**Signal 模式优势**：
- Event History 从几千压缩到个位数（只有 start + signal 两个关键 Event）
- 毫秒级响应（进程退出 → FileWatcher → Signal → Workflow 唤醒，而非等 5 秒轮询）
- Temporal 吞吐量大幅提升，可支撑更多并行 Workflow

### 5.2 OpenCode Agent Wrapper（核心适配层）

```python
class OpenCodeAgent(BaseAgentRuntime):
    """封装 OpenCode CLI，通过文件通道获取结构化输出"""

    STREAM_FILE = "/workspace/.opencode/stream.jsonl"

    def __init__(self, node_id: str, sandbox_id: str, config: NodeConfig):
        super().__init__(node_id, sandbox_id, config)
        self.agent_type = config.agent_type      # build | plan | explore | general
        self.model = config.model
        self.permissions = config.permissions
        self.mcp_servers = config.mcp_servers
        self.prompt_template = config.prompt

    async def run(self, task_input: dict) -> dict:
        """在沙盒中启动 OpenCode 实例并执行任务"""

        # 1. Git Checkpoint: 执行前自动提交
        await self.checkpoint_manager.auto_commit(
            self.sandbox_id,
            message=f"before node [{self.node_id}]"
        )

        # 2. 生成 OpenCode 配置文件
        config = self._generate_config()
        await self.sandbox.write_file(
            self.sandbox_id, "/root/.opencode/config.json", config
        )

        # 3. 构建 opencode 命令 (后台执行，输出到文件)
        cmd = self._build_command(task_input)
        exec_id = await self.sandbox.exec_async(self.sandbox_id, cmd)

        # 4. 启动文件监听，读取 stream.jsonl
        watcher = FileWatcher(
            sandbox_id=self.sandbox_id,
            file_path=self.STREAM_FILE,
            on_event=self._on_stream_event
        )
        await watcher.start()

        # 5. 等待 OpenCode 进程退出
        exit_code = await self.sandbox.wait_process(exec_id)
        await watcher.stop()

        return {"exit_code": exit_code,
                "status": "completed" if exit_code == 0 else "failed"}

    async def _on_stream_event(self, event: StreamEvent):
        """解析到流式事件后，推送到 Redis"""
        await self.stream_publisher.publish(event)

    def _build_command(self, task_input: dict) -> str:
        prompt = self.prompt_template.format(**task_input)
        return (
            f"cd /workspace && "
            f"stdbuf -o0 opencode task "                  # 强制无缓冲输出
            f"--agent {self.agent_type} "
            f"--model {self.model.provider}/{self.model.id} "
            f"--prompt '{prompt}' "
            f"--log-format jsonl "                       # 结构化日志
            f"--log-file /workspace/.opencode/stream.jsonl"  # 输出到文件
        )
```

> **注意**：`--log-format jsonl` 和 `--log-file` 参数需在 P0-3 阶段验证 OpenCode 是否支持。
> 若不支持，备选方案为：`stdbuf -o0 opencode task ... > /workspace/.opencode/stream.jsonl 2>&1`
> + Python 端容错解析（跳过非 JSON 行）。

### 5.3 Git Checkpoint 管理（防自杀隔离设计）

> **核心理念**：针对代码工程，最好的 Checkpoint 机制是 Git 本身。
> Docker 快照沉重且不可 diff，Git commit 天然支持回滚和对比。
>
> **防自杀设计**：OpenCode build Agent 拥有 bash 完全执行权限。大模型在遇到代码冲突时，
> 有概率执行 `rm -rf .git` 或 `git reset --hard`，直接摧毁 Checkpoint 机制。
> **对策**：将 `.git` 目录与 Agent 工作空间物理隔离。

**沙盒内目录结构**：
```
/workspace        → Agent 可读写，无 .git 目录（Agent 不知道有 Git）
/sandbox-meta/.git → 仅 Python 控制层可操作（只读挂载或独立 volume）
```

```python
class GitCheckpointManager:
    """用 Git 替代 Docker 快照做 Checkpoint（Git 目录隔离版）"""

    # Git 元数据与工作树分离，Agent 无法触及 .git
    GIT_DIR = "/sandbox-meta/.git"
    WORK_TREE = "/workspace"

    def _git(self, sandbox_id: str, *args: str) -> str:
        """构建隔离 git 命令"""
        cmd = f"git --git-dir={self.GIT_DIR} --work-tree={self.WORK_TREE} {' '.join(args)}"
        return cmd

    async def init_repo(self, sandbox_id: str) -> None:
        """沙盒初始化时创建分离式 Git 仓库"""
        await self.sandbox.exec(sandbox_id, f"mkdir -p {self.GIT_DIR}")
        await self.sandbox.exec(sandbox_id, self._git(sandbox_id, "init"))

    async def auto_commit(self, sandbox_id: str, message: str) -> str:
        """节点执行前自动 git commit（Agent 完全无感知）"""
        cmd = self._git
        await self.sandbox.exec(sandbox_id, cmd(sandbox_id, "add -A"))
        await self.sandbox.exec(sandbox_id, cmd(sandbox_id, f'commit -m "{message}" --allow-empty'))
        commit_hash = await self.sandbox.exec(sandbox_id, cmd(sandbox_id, "rev-parse HEAD"))
        return commit_hash.strip()

    async def rollback(self, sandbox_id: str, commit_hash: str) -> None:
        """节点失败或 Review Reject 时，回滚到上一个 Checkpoint"""
        await self.sandbox.exec(sandbox_id, cmd(sandbox_id, f"reset --hard {commit_hash}"))

    async def get_diff(self, sandbox_id: str, from_hash: str) -> str:
        """获取节点执行前后的 diff（用于前端审批面板展示）"""
        return await self.sandbox.exec(sandbox_id, cmd(sandbox_id, f"diff {from_hash} HEAD"))
```

**Checkpoint 策略**：
- **自动 Checkpoint**：每个节点执行前自动 `git commit`（Agent 完全无感知）
- **防自杀**：`.git` 在 `/sandbox-meta/` 下，Agent 无法触及，即使执行 `rm -rf /workspace` 也不影响
- **失败回滚**：节点失败或 Review Reject 时 `git reset --hard`
- **Diff 展示**：Human-in-the-Loop 审批时展示 `git diff`
- **Docker 快照保留**：仅在 Phase 2+ 用于非代码场景（如环境配置快照）

### 5.4 Workspace 共享与上下文传递（防爆炸）

> **核心理念**：节点间不直接传大块文本，而是通过文件系统间接传递。
> 上游输出写入文件，下游通过 OpenCode 的 read 工具按需读取。

```
共享 Workspace (Docker Volume)
├── src/                  # 代码
├── .workflow/
│   ├── plan.md           # Plan Node 输出 → Coder Node 读取
│   ├── requirements.txt  # Explore Node 输出 → Shell Node 读取
│   ├── review.json       # Review Node 输出 → Human Node 读取
│   └── shared_kv.json    # 全局共享键值对
└── .opencode/
    └── stream.jsonl      # 当前节点的流式输出
```

**节点间传递模式**：
- Plan Node 输出 500 行架构文档 → 写入 `.workflow/plan.md`
- Coder Node 的 prompt 只有：**"请阅读 .workflow/plan.md，并据此实现代码"**
- OpenCode 通过 read 工具按需读取，而非把 500 行全塞进 prompt

**优势**：
- 大模型 Context 转化为 Agent 的本地文件检索能力
- 天然利用 OpenCode 内置的 read 工具
- Prompt 始终简洁，Token 消耗可控

### 5.5 Streaming Pipeline（含节流）

```python
class StreamThrottler:
    """shell_stdout 节流合并器，防止 Xterm.js 卡死"""

    def __init__(self, window_ms: int = 100):
        self.window_ms = window_ms
        self._buffer: list[str] = []
        self._last_flush = time.monotonic()

    def add(self, content: str) -> Optional[str]:
        """添加内容，达到窗口时间后返回合并结果"""
        self._buffer.append(content)
        now = time.monotonic()
        if (now - self._last_flush) * 1000 >= self.window_ms:
            merged = "".join(self._buffer)
            self._buffer.clear()
            self._last_flush = now
            return merged
        return None

    def flush(self) -> Optional[str]:
        """强制 flush 剩余内容"""
        if self._buffer:
            merged = "".join(self._buffer)
            self._buffer.clear()
            return merged
        return None
```

**节流规则**：
- `llm_token`：不节流，逐个推送（打字机效果）
- `shell_stdout`：100ms 窗口合并为单个 chunk
- `tool_call` / `tool_result`：不节流，即时推送
- `status` / `error`：不节流，即时推送

### 5.6 Memory 与上下文系统

| 层级 | 策略 | 实现方式 |
|------|------|----------|
| 节点内上下文 | OpenCode Session 管理 | 复用 OpenCode 原生 Session + 上下文压缩 |
| 节点间传递 | 文件系统共享 | 上游写入 `.workflow/` 目录，下游通过 read 工具读取 |
| 全局共享 | KV 键值对 | 通过 MCP Server 的 `read_shared_kv` / `write_shared_kv` |
| Git Checkpoint | 每节点自动 commit | 执行前 commit，失败时 reset |
| 产物隔离 | 工具获取 | OpenCode 的 read 工具天然支持按需读取 |

---

## 六、API 设计

### 6.1 REST API（FastAPI）

```
# 工作流管理
POST   /api/workflows              # 创建工作流（接收 React Flow JSON）
GET    /api/workflows              # 列出工作流
GET    /api/workflows/:id          # 获取工作流详情
PUT    /api/workflows/:id          # 更新工作流
DELETE /api/workflows/:id          # 删除工作流

# 运行管理
POST   /api/workflows/:id/run      # 触发执行
GET    /api/runs                   # 列出运行记录
GET    /api/runs/:id               # 查询运行状态
POST   /api/runs/:id/cancel        # 取消运行
POST   /api/runs/:id/approve       # 人工审批
GET    /api/runs/:id/nodes         # 获取节点执行详情
GET    /api/runs/:id/diff          # 获取 Git Diff（审批用）

# 沙盒管理
GET    /api/sandboxes/:id/files    # 浏览沙盒文件
GET    /api/sandboxes/:id/git-log  # 查看 Git 历史

# 模型管理
GET    /api/models                 # 可用模型列表
```

### 6.2 WebSocket（FastAPI）

```
连接: ws://server/runs/{run_id}/stream

消息格式 (Server -> Client):
{
  "type": "llm_token" | "tool_call" | "tool_result" | "shell_stdout" | "status" | "error",
  "node_id": "coder1",
  "content": "...",
  "tool_name": "edit",
  "metadata": {...},
  "timestamp": 1234567890
}
```

---

## 七、数据库设计

```sql
-- 工作流模板
CREATE TABLE workflows (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        VARCHAR(255) NOT NULL,
    description TEXT,
    dag_json    JSONB NOT NULL,
    version     INT DEFAULT 1,
    created_by  UUID REFERENCES users(id),
    created_at  TIMESTAMP DEFAULT NOW(),
    updated_at  TIMESTAMP DEFAULT NOW()
);

-- 运行记录
CREATE TABLE runs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workflow_id     UUID REFERENCES workflows(id),
    status          VARCHAR(50) DEFAULT 'pending',
    input           JSONB,
    output          JSONB,
    event_history   JSONB,
    started_at      TIMESTAMP,
    completed_at    TIMESTAMP,
    created_at      TIMESTAMP DEFAULT NOW()
);

-- 节点执行日志
CREATE TABLE node_executions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id          UUID REFERENCES runs(id),
    node_id         VARCHAR(100),
    agent_type      VARCHAR(50),
    opencode_config JSONB,
    status          VARCHAR(50),
    input           JSONB,
    output          JSONB,
    tokens_used     INT,
    duration_ms     INT,
    sandbox_id      VARCHAR(100),
    git_commit_before VARCHAR(40),  -- 节点执行前的 Git commit hash
    git_commit_after  VARCHAR(40),  -- 节点执行后的 Git commit hash
    created_at      TIMESTAMP DEFAULT NOW()
);

-- 全局共享 KV
CREATE TABLE shared_kv (
    run_id      UUID REFERENCES runs(id),
    key         VARCHAR(255),
    value       JSONB,
    updated_at  TIMESTAMP DEFAULT NOW(),
    PRIMARY KEY (run_id, key)
);

-- 用户
CREATE TABLE users (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email       VARCHAR(255) UNIQUE NOT NULL,
    name        VARCHAR(255),
    role        VARCHAR(50) DEFAULT 'user',
    created_at  TIMESTAMP DEFAULT NOW()
);
```

---

## 八、分阶段实施路线

### Phase 0：引擎验证（第 1-3 周）

> **不写前端，不写网关。纯 Python 验证核心逻辑 + OpenCode CLI 集成。**

| 序号 | 任务 | 交付物 |
|------|------|--------|
| P0-1 | 项目脚手架 + 基础设施 | monorepo + docker-compose.yml（PG, Redis, Temporal, MinIO） |
| P0-2 | Docker 沙盒 + OpenCode 预装 | `SandboxManager` + 沙盒基础镜像（含 opencode CLI + git） |
| P0-3 | OpenCode 输出通道验证 | 验证 OpenCode 的结构化日志能力（文件 vs stdout），确定最终通道方案 |
| P0-4 | OpenCode CLI Wrapper | `OpenCodeAgent` 类：配置注入 → 后台执行 → 文件监听 → 流式解析 |
| P0-5 | Git Checkpoint | `GitCheckpointManager`：执行前 auto commit，失败时 rollback |
| P0-6 | Temporal Workflow（异步模式） | `AgentNodeWorkflow`：start_activity → poll_activity → 完成/失败 |
| P0-7 | Streaming Pipeline | JSONL 文件读取 → 解析 → Redis Pub/Sub → 验证数据格式 |
| **P0-8** | **Kill & Resume 验证** | **故意 kill -9 Python Worker，验证 Temporal 重启后能否平滑接管** |

**Phase 0 验收命令**：
```bash
# 1. 启动基础设施
docker-compose up -d

# 2. 启动 FastAPI + Temporal Worker
cd apps/orchestrator && poetry run python -m app.main

# 3. 触发测试 Workflow
curl -X POST http://localhost:8000/api/test/run \
  -d '{"prompt": "实现冒泡排序并用 gcc 编译", "agent": "build"}'

# 4. 验证 Git Checkpoint
docker exec <sandbox_id> git log --oneline
# 应看到: "before node [coder1]" commit

# 5. Kill & Resume 测试
# 在 Agent 执行到一半时 kill Python Worker
kill -9 <worker_pid>
# 重启 Worker，观察 Temporal 是否自动恢复执行
poetry run python -m app.main
```

---

### Phase 1：可视化平台（第 4-8 周）

| 序号 | 任务 | 交付物 |
|------|------|--------|
| P1-1 | FastAPI WebSocket Hub | asyncio WebSocket 管理 + Redis 订阅 + 推流 |
| P1-2 | React Flow 前端画布 | 6 种自定义节点 + 拖拽连线 |
| P1-3 | 节点配置面板 | Agent 类型、模型选择、Prompt 模板、权限配置 |
| P1-4 | Streaming 全链路 | JSONL → 解析 → 节流 → Redis → WS → 前端 |
| P1-5 | DAG Compiler | React Flow JSON → 拓扑排序 → Temporal Workflow |
| P1-6 | Workspace 共享 | 节点间通过 `.workflow/` 目录传递上下文 |
| P1-7 | MCP Server (初版) | `query_upstream` + `read_shared_kv` + `write_shared_kv` |

**Phase 1 验收**：
```
1. 打开 http://localhost:3000
2. 拖拽 Plan Node → prompt: "分析项目结构并生成实现方案"
3. 拖拽 Coder Node → prompt: "阅读 .workflow/plan.md 并实现代码"
4. 拖拽 Shell Node → command: "gcc main.c -o main && ./main"
5. 连线：Plan → Coder → Shell
6. 点击 Run
7. 实时看到：
   - Plan Node 生成架构文档（写入 .workflow/plan.md）
   - Coder Node 读取 plan.md 生成代码（打字机效果）
   - Shell Node 编译执行（Xterm.js，节流后不卡顿）
   - 每个 Node 执行前 Git Checkpoint
```

---

### Phase 2：工业级特性 + Go 网关拆分（第 9-16 周）

| 序号 | 任务 | 交付物 |
|------|------|--------|
| P2-1 | **Go API Gateway 引入** | Gin 框架，WebSocket + 路由 + 鉴权从 Python 拆出 |
| P2-2 | Human-in-the-Loop 完善 | Diff 审批面板 + Temporal Signal 暂停/恢复 |
| P2-3 | Context 压缩增强 | OpenCode 原生压缩 + 自定义跨节点摘要 |
| P2-4 | Oh My OpenCode 集成 | OMO 插件：多模型协作、Skills、后台任务 |
| P2-5 | MCP Server 增强 | `request_human_approval` + `block_dangerous_ops` |
| P2-6 | 沙盒安全升级 | gVisor 替换 runc |
| P2-7 | OpenCode Server 模式 | 从 CLI 升级为 Headless Server API 交互 |

### Phase 3：护城河（第 17+ 周）

| 序号 | 任务 | 交付物 |
|------|------|--------|
| P3-1 | 自定义 Agent 注册 | 通过 OpenCode 插件系统注册垂直领域 Agent |
| P3-2 | 多节点并行 | Planner → 并行拉起多个 OpenCode build Agent |
| P3-3 | 高级仿真器 | QEMU 仿真 + OpenCode 读 Kernel dump |
| P3-4 | 垂直领域镜像 | BSP/Yocto/GCC 交叉编译链 |
| P3-5 | LSP 深度集成 | OpenCode 40+ 语言 LSP 代码智能审查 |

---

## 九、风险与对策

| 风险 | 严重度 | 对策 |
|------|--------|------|
| OpenCode 不支持 `--log-file` 参数 | 高 | P0-3 第一时间验证；备选：`stdbuf -o0` 重定向 stdout 到文件 + Python 容错解析 |
| 文件通道 Block Buffering | 中 | `stdbuf -o0` 或 `NODE_OPTIONS=--no-buffering` 强制无缓冲写入 |
| Log Bomb（日志炸弹） | 高 | FileWatcher 50MB 硬上限 + Docker `log-opt` 限制 + 超限 kill 进程 |
| Agent 执行 `rm -rf .git` 摧毁 Checkpoint | 高 | Git 目录分离：`.git` 在 `/sandbox-meta/` 下，Agent 无法触及 |
| Temporal Activity 超时导致重复执行 | 高 | 异步轮询模式（Phase 0）→ Signal 驱动模式（Phase 1） |
| Temporal Event History 爆炸（50k 上限） | 中 | Phase 1 升级为 Signal 驱动，Event 从几千压缩到个位数 |
| MCP Shared KV 跨 Run 污染 | 中 | `run_id` 强制注入 MCP URL，Server 端自动作用域隔离 |
| Xterm.js 大量输出卡死前端 | 中 | Python 端 100ms 窗口节流合并 shell_stdout |
| 节点间上下文传递导致 Token 爆炸 | 高 | Workspace 文件共享模式，下游 read 按需读取 |
| 沙盒逃逸 | 中 | Phase 2 升级 gVisor |
| Go 网关拆分成本 | 低 | Python 端保持 REST + WS 接口稳定，Go 做透明代理 |

---

## 十、架构演进摘要

### v2 → v3

| 维度 | v2 方案 | v3 方案 |
|------|---------|---------|
| CLI 输出通道 | stdout 捕获 | **文件通道 (stream.jsonl)** |
| MVP 后端 | Go + Python 双栈 | **Python All-in-One** |
| Temporal Activity | 阻塞等待 | **异步轮询模式** |
| Checkpoint | Docker 快照 | **Git commit + rollback** |
| 节点间传递 | 直接塞 prompt | **Workspace 文件共享** |
| MCP 拓扑 | 未明确 | **Python MCP Server + OpenCode Client** |
| Streaming 节流 | 无 | **100ms 窗口合并** |
| Kill & Resume | 未验证 | **P0-8 强制验证** |

### v3 → v4（本次防爆加固）

| 维度 | v3 方案 | v4 方案 |
|------|---------|---------|
| Git 安全 | `.git` 在 `/workspace` 下 | **Git 目录分离** `/sandbox-meta/.git`，Agent 无法触及 |
| 文件缓冲 | 未处理 | **`stdbuf -o0` 强制无缓冲**，保证打字机效果 |
| Log Bomb | 未防御 | **FileWatcher 50MB 硬上限** + 超限 kill 进程 |
| Temporal 轮询 | 5 秒轮询（Event 爆炸风险） | **Signal 驱动**（Phase 1 升级），Event 压缩到个位数 |
| MCP KV 隔离 | 无隔离 | **`run_id` 注入 URL**，Server 端自动作用域隔离 |

---

## 十一、Phase 0 执行策略（硬仗顺序）

> **核心原则**：先消除最大技术风险，再做集成。

**第一场硬仗（P0-2 + P0-3）**：
不要写数据库，不要写 Temporal。直接用 Python 裸写一段代码：
1. 启动一个装有 OpenCode 的 Docker 容器
2. 下发一条"生成代码并编译"的指令
3. 跑通文件通道 JSONL 的实时捕获 + 解析
4. 验证 `stdbuf -o0` 能否解决 Block Buffering
5. 验证 50MB Log Bomb 防御是否生效

如果这一步顺滑，整个项目的技术风险就消除了 80%。

**第二场硬仗（P0-5 + P0-6 + P0-8）**：
在跑通单次调用后，立刻套上 Temporal + Git Checkpoint：
1. 实现分离式 Git Checkpoint（`.git` 在 `/sandbox-meta/` 下）
2. 实现 Temporal 异步轮询 Workflow
3. 故意在中途 kill -9 Python Worker
4. 看着它重启后，依靠 Git Checkpoint 和 Temporal 状态机，精准地从断点继续把代码写完

**当你亲眼看到这一幕时，你的核心壁垒就已经建立起来了。**

---

*文档版本：v4.0*
*更新时间：2026-05-07*
*v4 防爆加固：Git 目录分离、无缓冲写入、Log Bomb 防御、Signal 驱动、MCP 命名空间隔离*
*基于 Edge Case Review 终极加固*
