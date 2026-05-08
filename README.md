# Multi-Agent Studio

**面向复杂工程的 AI Agent 可视化编排平台**

Multi-Agent Studio 是一个开源的 AI 多智能体工作流编排系统。用户通过拖拽式可视化画布构建多 Agent 协作流程，系统在 Docker 沙盒中隔离执行，并实时流式呈现 Agent 的每一步操作。核心 Agent 引擎基于 [OpenCode CLI](https://github.com/opencode-ai/opencode)（MIT 协议），复用其 6 种内置 Agent、20+ 编程工具和 75+ 模型提供商能力。

## 核心特性

- **可视化编排** -- 基于 React Flow v12 的拖拽画布，支持 6 种节点类型（Coder / Plan / Explore / General / Shell / Review）和 Human 决策节点，通过连线定义 DAG 执行拓扑
- **Docker 沙盒隔离** -- 每个 Agent 节点在独立 Docker 容器中执行，Git 目录与工作空间物理分离（防自杀设计），支持自动 Checkpoint 与回滚
- **实时流式输出** -- 完整 Streaming Pipeline：OpenCode JSONL 文件写入 → Python 解析 → Redis Pub/Sub → WebSocket 推送 → 前端 Monaco Editor 打字机效果 / Xterm.js 终端渲染
- **Human-in-the-Loop** -- 基于 Temporal Signal 的人工审批机制，支持 Git Diff 展示、批准/拒绝操作，工作流可在审批节点暂停等待
- **安全防爆** -- 50MB Log Bomb 防御、`run_id` 命名空间隔离、Agent 权限分级（allow/deny/ask）、Worker 崩溃后 Temporal 自动恢复

## 系统架构

```
┌──────────────────────────────────────────────────────────────────┐
│                  Web Frontend (Next.js 14)                       │
│   [React Flow 画布] [Monaco Editor] [Xterm.js] [Zustand Store]  │
└───────────┬──────────────────────────────────┬───────────────────┘
            │ REST API                          │ WebSocket (实时流)
┌───────────▼──────────────────────────────────▼───────────────────┐
│            All-in-One Server (Python / FastAPI)                   │
│                                                                   │
│  ┌────────────────┐  ┌────────────────┐  ┌───────────────────┐  │
│  │  REST Handler  │  │ WebSocket Hub  │  │  Temporal Client  │  │
│  │ (工作流 / 运行) │  │ (asyncio 推流) │  │                   │  │
│  └────────────────┘  └────────────────┘  └───────────────────┘  │
│  ┌────────────────┐  ┌────────────────┐  ┌───────────────────┐  │
│  │  MCP Server    │  │ OpenCode Agent │  │  Sandbox Manager  │  │
│  │ (工作流级工具)  │  │  Wrapper       │  │  (Docker SDK)     │  │
│  └────────────────┘  └────────────────┘  └───────────────────┘  │
│  ┌─────────────────────────────────────────────────────────────┐ │
│  │  Streaming Pipeline                                         │ │
│  │  文件 JSONL → 解析 → 节流合并 → Redis Pub/Sub → WS 推送     │ │
│  └─────────────────────────────────────────────────────────────┘ │
└───────────┬──────────────────────────────────────────────────────┘
            │ 任务分发
┌───────────▼──────────────────────────────────────────────────────┐
│              Agent Runtime Fleet (OpenCode 实例)                  │
│                                                                   │
│  ┌───────────────────┐  ┌───────────────────┐                    │
│  │   沙盒容器 A       │  │   沙盒容器 B       │                    │
│  │   opencode build  │  │   opencode plan   │                    │
│  │   共享 Git 仓库   │  │   共享 Git 仓库   │                    │
│  │   stream.jsonl    │  │   stream.jsonl    │                    │
│  └───────────────────┘  └───────────────────┘                    │
└───────────┬──────────────────────────────────────────────────────┘
            │
┌───────────▼──────────────────────────────────────────────────────┐
│         Data Layer (PostgreSQL + Redis + MinIO)                   │
└──────────────────────────────────────────────────────────────────┘
```

## 技术栈

| 层级 | 技术 | 说明 |
|------|------|------|
| **前端** | Next.js 14 (App Router) | React 框架，SSR 支持 |
| | React Flow v12 (`@xyflow/react`) | 节点编辑器画布 |
| | Zustand v5 | 轻量状态管理 |
| | TailwindCSS v3 | 样式系统 |
| | Monaco Editor | 代码编辑 / Prompt 编写 |
| | Xterm.js | 终端输出渲染 |
| **后端** | Python 3.11+ / FastAPI | REST + WebSocket 一体化 |
| | Temporal.io (Python SDK) | 工作流编排引擎 |
| | Docker SDK | 沙盒容器管理 |
| | Redis 7 | Pub/Sub 消息总线 |
| | PostgreSQL 15 + JSONB | 持久化存储 |
| | SQLAlchemy 2 (async) | 异步 ORM |
| | Pydantic v2 | 数据校验与配置 |
| **Agent 引擎** | OpenCode CLI v1.14.41 | 封装为子 Agent，MIT 协议 |
| **基础设施** | Docker | 沙盒隔离与容器化 |
| | Temporal Server | 持久化工作流引擎 |
| | MinIO | S3 兼容对象存储 |

## 快速开始

### 前置条件

| 依赖 | 最低版本 | 检查命令 |
|------|----------|----------|
| Node.js | 18+ | `node --version` |
| Python | 3.11+ | `python3 --version` |
| Docker Desktop | 运行中 | `docker ps` |
| pnpm (推荐) 或 npm | 任意 | `pnpm --version` |
| Poetry | 最新 | `poetry --version` |

### 一键初始化

项目提供了安装脚本，克隆后运行即可完成所有依赖安装和基础设施启动：

```bash
# 1. 克隆项目
git clone <repo-url>
cd multi-agent-studio

# 2. 一键安装（检查前置条件、安装依赖、构建沙盒镜像、启动基础设施）

# Windows (PowerShell):
.\scripts\setup.ps1

# Linux / macOS:
bash scripts/setup.sh
```

脚本会依次执行以下步骤：
1. 检查 Docker、Python、Node 是否已安装
2. 安装前端依赖（`apps/web`）
3. 安装 Python 依赖（`apps/orchestrator`）
4. 构建沙盒 Docker 镜像（首次约 5-10 分钟）
5. 启动 PostgreSQL、Redis、Temporal、MinIO 容器

### 手动安装

如果需要手动控制每一步：

```bash
# 1. 克隆项目
git clone <repo-url>
cd multi-agent-studio

# 2. 安装前端依赖
cd apps/web && pnpm install && cd ../..

# 3. 安装后端依赖
cd apps/orchestrator && pip install poetry && poetry install --no-root && cd ../..

# 4. 构建沙盒镜像
docker build -t multi-agent-studio/sandbox-base:latest infra/sandbox-images/base/

# 5. 启动基础设施
docker compose -f infra/docker-compose.yml up -d

# 6. 启动后端 API（终端 1）
cd apps/orchestrator && poetry run python -m app.main

# 7. 启动 Temporal Worker（终端 2）
cd apps/orchestrator && poetry run python -m app.workflows.worker

# 8. 启动前端（终端 3）
cd apps/web && pnpm dev
```

> **Windows 注意**：`poetry run` 不支持 `&` 后台运行，每个服务需要单独开一个终端窗口。

### 使用开发脚本

脚本会自动检测并启动所有服务：

```bash
# Windows (PowerShell):
.\scripts\dev.ps1

# Linux / macOS:
bash scripts/dev.sh
```

输出如下：

```
=== Starting Multi-Agent Studio Dev Environment ===
Starting infrastructure...
Starting services:
  1. Python Orchestrator (FastAPI) on :8000
  2. Temporal Worker
  3. Frontend (Next.js) on :3000

All services started. Press Ctrl+C to stop.
  Orchestrator: http://localhost:8000
  Frontend:     http://localhost:3000
  Temporal UI:  http://localhost:8088
```

### 验证

```bash
# 检查后端 API
curl http://localhost:8000/health
# 返回: {"status":"ok"}

# 检查可用模型
curl http://localhost:8000/api/models

# 打开浏览器访问前端
# http://localhost:3000

# 查看 Temporal 管理界面
# http://localhost:8088
```

## 项目结构

```
multi-agent-studio/
├── apps/
│   ├── web/                            # Next.js 14 前端应用
│   │   ├── src/
│   │   │   ├── components/
│   │   │   │   ├── canvas/             # React Flow 画布组件
│   │   │   │   │   ├── FlowCanvas.tsx  # 画布主组件
│   │   │   │   │   ├── nodes/          # 自定义节点（Coder/Plan/Shell 等）
│   │   │   │   │   └── edges/          # 自定义连线
│   │   │   │   ├── panels/             # 配置面板
│   │   │   │   └── common/             # 通用组件
│   │   │   ├── stores/
│   │   │   │   ├── workflowStore.ts    # 工作流状态管理
│   │   │   │   └── runStore.ts         # 运行状态管理
│   │   │   ├── hooks/
│   │   │   │   └── useWebSocket.ts     # WebSocket 连接 Hook
│   │   │   └── lib/
│   │   │       └── api.ts              # API 客户端
│   │   └── package.json
│   │
│   ├── gateway/                        # Go API 网关（Phase 2 启用）
│   │
│   └── orchestrator/                   # Python 编排引擎（核心）
│       ├── pyproject.toml              # Poetry 依赖配置
│       ├── .env                        # 环境变量
│       ├── app/
│       │   ├── main.py                 # FastAPI 入口（REST + WebSocket + Lifespan）
│       │   ├── config.py               # 配置项（Pydantic Settings）
│       │   ├── api/
│       │   │   ├── workflows.py         # 工作流 CRUD
│       │   │   ├── runs.py             # 运行管理（触发/取消/审批）
│       │   │   └── models.py           # 可用模型列表
│       │   ├── ws/
│       │   │   └── hub.py              # WebSocket Hub（asyncio）
│       │   ├── core/
│       │   │   └── database.py         # 数据库连接（SQLAlchemy async）
│       │   ├── models/
│       │   │   ├── db.py               # ORM 模型
│       │   │   └── schemas.py          # Pydantic 请求/响应模型
│       │   ├── workflows/
│       │   │   ├── compiler.py         # DAG 编译器（React Flow JSON → 拓扑层）
│       │   │   ├── dag_workflow.py      # Temporal DAG Workflow 定义
│       │   │   ├── activities.py       # Temporal Activities
│       │   │   └── worker.py           # Temporal Worker 入口
│       │   ├── agents/
│       │   │   ├── base.py             # Agent 抽象基类
│       │   │   ├── opencode.py         # OpenCode CLI Wrapper
│       │   │   ├── factory.py          # Agent 工厂
│       │   │   └── config.py           # OpenCode 配置动态生成
│       │   ├── sandbox/
│       │   │   ├── manager.py          # Docker 沙盒管理器
│       │   │   ├── provision.py        # 沙盒初始化
│       │   │   └── checkpoint.py       # Git Checkpoint 管理
│       │   ├── streaming/
│       │   │   ├── file_watcher.py     # stream.jsonl 文件监听
│       │   │   ├── parser.py           # JSONL 解析器
│       │   │   ├── throttler.py        # 节流合并器（100ms 窗口）
│       │   │   └── publisher.py        # Redis Publisher
│       │   ├── mcp_server/
│       │   │   ├── server.py           # MCP Server 主入口
│       │   │   └── tools.py            # 工作流级 MCP 工具定义
│       │   └── memory/
│       │       ├── context.py          # 上下文窗口管理
│       │       └── workspace_share.py  # Workspace 共享 + 文件传递
│       └── tests/                      # pytest 测试
│
├── packages/
│   └── shared-types/                   # 共享类型定义
│       └── schemas/
│           ├── workflow.json
│           ├── events.json
│           └── node-config.json
│
├── infra/
│   ├── docker-compose.yml              # 基础设施编排（PG/Redis/Temporal/MinIO）
│   ├── postgres/
│   │   └── init.sql                    # 数据库初始化 SQL
│   ├── sandbox-images/
│   │   └── base/
│   │       └── Dockerfile              # 沙盒基础镜像（Ubuntu + OpenCode + Git + GCC）
│   └── temporal/
│       └── config.yaml                 # Temporal 配置
│
├── scripts/
│   ├── setup.sh                        # 一键安装脚本
│   └── dev.sh                          # 开发环境启动脚本
│
├── doc/
│   └── plan.md                         # 技术规划蓝图
│
└── README.md
```

## 配置说明

所有配置通过环境变量管理，统一以 `MAS_` 为前缀。配置文件位于 `apps/orchestrator/.env`，由 `pydantic-settings` 自动加载。

### 环境变量一览

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `MAS_HOST` | `0.0.0.0` | API 服务监听地址 |
| `MAS_PORT` | `8000` | API 服务监听端口 |
| `MAS_DATABASE_URL` | `postgresql+asyncpg://postgres:postgres@localhost:5432/multi_agent_studio` | PostgreSQL 连接串（异步驱动） |
| `MAS_REDIS_URL` | `redis://localhost:6379/0` | Redis 连接串 |
| `MAS_TEMPORAL_HOST` | `localhost:7233` | Temporal Server 地址 |
| `MAS_TEMPORAL_NAMESPACE` | `default` | Temporal 命名空间 |
| `MAS_TEMPORAL_TASK_QUEUE` | `agent-workflow` | Temporal 任务队列名 |
| `MAS_DOCKER_SOCKET` | Linux: `unix:///var/run/docker.sock` / Windows: `npipe:////./pipe/docker_engine` | Docker Socket 路径（自动检测） |
| `MAS_SANDBOX_IMAGE` | `multi-agent-studio/sandbox-base:latest` | 沙盒 Docker 镜像名 |
| `MAS_OPENCODE_STREAM_DIR` | `/workspace/.opencode` | OpenCode 流式输出目录（沙盒内路径） |
| `MAS_OPENCODE_STREAM_FILE` | `stream.jsonl` | OpenCode 流式输出文件名 |
| `MAS_OPENCODE_LOG_MAX_BYTES` | `52428800` (50MB) | 单节点日志文件大小硬上限（Log Bomb 防御） |
| `MAS_STREAM_THROTTLE_WINDOW_MS` | `100` | shell_stdout 节流窗口（毫秒） |

### .env 示例

```bash
# 数据库
MAS_DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/multi_agent_studio

# Redis
MAS_REDIS_URL=redis://localhost:6379/0

# Temporal
MAS_TEMPORAL_HOST=localhost:7233
MAS_TEMPORAL_NAMESPACE=default
MAS_TEMPORAL_TASK_QUEUE=agent-workflow

# 服务
MAS_HOST=0.0.0.0
MAS_PORT=8000
```

## API 文档

后端 FastAPI 启动后访问 `http://localhost:8000/docs` 可查看自动生成的 Swagger 文档。

### REST API

#### 工作流管理

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/api/workflows` | 工作流列表（按创建时间倒序） |
| `POST` | `/api/workflows` | 创建工作流（接收 React Flow JSON） |
| `GET` | `/api/workflows/:id` | 工作流详情 |
| `PUT` | `/api/workflows/:id` | 更新工作流（保存 DAG 定义） |
| `DELETE` | `/api/workflows/:id` | 删除工作流及关联运行 |

#### 运行管理

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/api/runs/:workflowId/run` | 触发工作流执行（通过 Temporal DAGWorkflow） |
| `GET` | `/api/runs` | 运行记录列表（支持 `workflow_id` / `limit` / `offset` 过滤） |
| `GET` | `/api/runs/:id` | 运行详情（含 Temporal 状态同步） |
| `POST` | `/api/runs/:id/cancel` | 取消运行中工作流（通过 Temporal cancel） |
| `GET` | `/api/runs/:id/diff` | 获取 Git Diff（Human-in-the-Loop 审批用） |
| `GET` | `/api/runs/:id/nodes` | 获取节点执行详情 |
| `POST` | `/api/runs/:id/approve` | 批准（Human-in-the-Loop） |
| `POST` | `/api/runs/:id/reject` | 拒绝（Human-in-the-Loop） |

#### 模型管理

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/api/models` | 可用模型列表 |

#### 健康检查

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/health` | 服务健康检查，返回 `{"status": "ok"}` |

### WebSocket

| 协议 | 路径 | 说明 |
|------|------|------|
| `WS` | `/ws/runs/:runId/stream` | 实时运行事件流 |

**服务端推送消息格式**：

```json
{
  "type": "llm_token",
  "node_id": "coder1",
  "content": "def ",
  "timestamp": 1715049600000
}
```

事件类型（`type`）：

| 类型 | 说明 | 前端渲染目标 |
|------|------|-------------|
| `llm_token` | LLM 逐 Token 输出 | Monaco Editor 打字机效果 |
| `tool_call` | 工具调用（edit/bash/grep 等） | 节点详情面板 |
| `tool_result` | 工具调用结果 | 节点详情面板 |
| `shell_stdout` | Shell 标准输出（100ms 节流合并） | Xterm.js 终端 |
| `status` | 节点状态变更（running/completed/failed） | 节点颜色状态 |
| `error` | 错误信息 | 错误提示 |

服务端每 30 秒发送 `{"type": "ping"}` 心跳。

## 可用模型

系统内置两类模型，每个 Agent 节点可独立选择模型。

### 免费模型（无需 API Key）

| 模型 ID | 名称 | 说明 |
|---------|------|------|
| `minimax-m2.5-free` | MiniMax M2.5 Free | OpenCode 内置免费模型 |
| `nemotron-3-super-free` | Nemotron 3 Super Free | NVIDIA 免费模型 |
| `hy3-preview-free` | HY3 Preview Free | 免费预览版 |
| `big-pickle` | Big Pickle | OpenCode 社区免费模型 |

### 付费模型（需要 API Key）

| 提供商 | 模型 ID | 名称 |
|--------|---------|------|
| Anthropic | `claude-sonnet-4-20250514` | Claude Sonnet 4 |
| Anthropic | `claude-opus-4-20250514` | Claude Opus 4 |
| OpenAI | `gpt-4o` | GPT-4o |
| OpenAI | `o1` | o1 |
| Google | `gemini-2.0-flash` | Gemini 2.0 Flash |

> OpenCode 支持 75+ 模型提供商（通过 Vercel AI SDK v5），以上为系统预配置列表。可通过编辑 `apps/orchestrator/app/api/models.py` 添加更多模型。

## 使用指南

### 创建工作流

1. 打开浏览器访问 `http://localhost:3000`
2. 点击 **"新建工作流"** 按钮
3. 输入工作流名称和描述

### 拖拽节点和连线

从左侧节点面板拖拽节点到画布：

| 节点类型 | 对应 Agent | 权限 | 用途 |
|----------|-----------|------|------|
| Coder Node | `build` | 完全（读写 + 执行） | 代码生成、修改、重构 |
| Plan Node | `plan` | 只读 | 代码分析、架构评审、方案规划 |
| Explore Node | `@explore` | 只读 | 快速代码搜索、依赖分析 |
| General Node | `@general` | 完全 | 复杂多步自定义任务 |
| Shell Node | 自定义 Agent + bash only | 仅执行 | 编译 / 测试 / 部署 |
| Review Node | `plan` + 自定义 prompt | 只读 | 代码审查、质量检查 |
| Human Node | 无 Agent | -- | 人工审批 / 决策点 |

通过拖拽节点端口创建连线，连线方向即为数据流向（DAG 拓扑）。

### 配置 Agent

点击节点打开右侧配置面板：

- **模型选择** -- 从下拉列表选择该节点使用的 LLM 模型
- **Prompt 编辑** -- 在 Monaco Editor 中编写 Prompt 模板，支持 `{variable}` 变量插值
- **权限设置** -- 配置工具权限（bash: allow/deny/ask，write: allow/deny，edit: allow/deny）
- **Agent 类型** -- 选择 OpenCode 内置 Agent（build / plan / explore / general）

### 运行工作流

1. 点击画布右上角 **"Run"** 按钮
2. 系统将 DAG 提交到 Temporal 编排引擎
3. 前端自动建立 WebSocket 连接接收实时事件

### 查看实时输出

工作流运行后，点击节点查看三种实时输出：

- **LLM 输出** -- Monaco Editor 中以打字机效果逐字呈现代码生成过程
- **Shell 终端** -- Xterm.js 渲染编译/测试输出（已做 100ms 节流，不会卡顿）
- **工具调用** -- 面板中显示 Agent 正在使用的工具（edit、read、bash、grep 等）及参数

### Human-in-the-Loop 审批

当工作流执行到 Human Node 时：

1. 工作流自动暂停，等待人工操作
2. 审批面板展示当前节点的 Git Diff（执行前后的代码变更）
3. 选择 **"Approve"** 继续，或 **"Reject"** 回滚并终止
4. 如果选择 Reject，系统自动 `git reset --hard` 回滚到该节点执行前的 Checkpoint

### 语言切换

前端支持中英文界面切换，通过顶栏语言选择器切换。

## 开发指南

### 启动开发环境

```bash
# 方式 1: 使用开发脚本
bash scripts/dev.sh

# 方式 2: 手动启动各服务
# 终端 1: 后端 API
cd apps/orchestrator && poetry run python -m app.main

# 终端 2: Temporal Worker
cd apps/orchestrator && poetry run python -m app.workflows.worker

# 终端 3: 前端
cd apps/web && pnpm dev
```

### 前端开发（apps/web）

```bash
cd apps/web

# 安装依赖
pnpm install

# 启动开发服务器（热更新）
pnpm dev

# 类型检查
pnpm type-check

# 代码检查
pnpm lint

# 生产构建
pnpm build
```

关键目录：

- `src/components/canvas/` -- 画布与节点组件
- `src/stores/` -- Zustand 状态管理
- `src/hooks/useWebSocket.ts` -- WebSocket 连接管理

### 后端开发（apps/orchestrator）

```bash
cd apps/orchestrator

# 安装依赖
poetry install

# 启动 API 服务（带热更新）
poetry run python -m app.main

# 启动 Temporal Worker
poetry run python -m app.workflows.worker

# 运行测试
poetry run pytest

# 代码检查
poetry run ruff check app/

# 类型检查
poetry run mypy app/
```

关键目录：

- `app/api/` -- REST 接口（FastAPI Router）
- `app/workflows/` -- Temporal 工作流定义与 Activities
- `app/agents/` -- OpenCode Agent 封装层
- `app/sandbox/` -- Docker 沙盒管理
- `app/streaming/` -- 流式输出处理管线
- `app/mcp_server/` -- MCP Server（工作流级工具）

### 添加新的 Agent 类型

1. 在 `apps/orchestrator/app/agents/` 下创建新的 Agent 类，继承 `BaseAgentRuntime`
2. 实现 `run()` 方法，定义 Agent 的执行逻辑
3. 在 `apps/orchestrator/app/agents/factory.py` 中注册新类型
4. 在前端 `apps/web/src/components/canvas/nodes/` 下添加对应的节点组件
5. 更新 `models.py` 中的模型列表（如需要特殊模型）

### 添加新的模型

编辑 `apps/orchestrator/app/api/models.py`，在 `models` 列表中添加新条目：

```python
{
    "provider": "your-provider",      # 模型提供商
    "id": "model-id",                 # 模型 ID
    "name": "Display Name",           # 显示名称
    "free": False,                    # 是否免费
}
```

OpenCode 支持 75+ 模型提供商，几乎所有主流模型均可直接使用。

## 故障排除

### Docker Desktop 未启动

**症状**：`Cannot connect to the Docker daemon`

**解决**：启动 Docker Desktop 应用程序，等待其完全就绪后重试。可通过 `docker ps` 验证。

### 端口被占用

**症状**：`Address already in use` 或 `bind: address already in use`

**解决**：

```bash
# 查看端口占用（以 8000 为例）
# Linux / macOS
lsof -i :8000
# Windows
netstat -ano | findstr :8000

# 终止占用进程或修改 .env 中的端口号
MAS_PORT=8001
```

### Temporal 连接失败

**症状**：`temporalio.client.WorkflowServiceError` 或连接超时

**解决**：

```bash
# 确认 Temporal 容器正在运行
docker ps | grep temporal

# 如果未运行，启动基础设施
docker compose -f infra/docker-compose.yml up -d temporal

# 等待 Temporal 就绪（首次启动需要初始化）
docker compose -f infra/docker-compose.yml logs -f temporal
```

### Poetry 安装失败

**症状**：`Poetry could not find a pyproject.toml`

**解决**：

```bash
# 确保在正确的目录下执行
cd apps/orchestrator

# 如果 Poetry 未安装
pip install poetry

# 清除缓存重新安装
poetry cache clear pypi --all
poetry install
```

### 前端类型错误

**症状**：`Type error: ...` 或 TypeScript 编译失败

**解决**：

```bash
cd apps/web

# 清除缓存
rm -rf .next node_modules
pnpm install

# 检查类型
pnpm type-check
```

### Windows Docker Socket 路径

**症状**：`FileNotFoundError: [WinError 2]` 或 Docker 操作失败

**解决**：Windows 系统下 Docker Socket 默认路径为 `npipe:////./pipe/docker_engine`。`config.py` 已内置自动检测逻辑，无需手动配置。如果仍有问题，在 `.env` 中显式设置：

```bash
MAS_DOCKER_SOCKET=npipe:////./pipe/docker_engine
```

### Temporal Worker 崩溃恢复

**症状**：Worker 进程意外终止，工作流卡在 running 状态

**解决**：这是正常场景。Temporal Server 持久化了工作流状态，只需重启 Worker 即可自动恢复：

```bash
# 重启 Worker
cd apps/orchestrator && poetry run python -m app.workflows.worker

# Temporal 会自动将未完成的 Task 重新分配给新 Worker
```

## 端口一览

| 端口 | 服务 | 地址 |
|------|------|------|
| 3000 | 前端 (Next.js) | http://localhost:3000 |
| 8000 | 后端 API (FastAPI) | http://localhost:8000 |
| 8000 | API 文档 (Swagger) | http://localhost:8000/docs |
| 5432 | PostgreSQL | localhost:5432 |
| 6379 | Redis | localhost:6379 |
| 7233 | Temporal Server (gRPC) | localhost:7233 |
| 8088 | Temporal Web UI | http://localhost:8088 |
| 19000 | MinIO API | http://localhost:19000 |
| 19001 | MinIO Console | http://localhost:19001 |

## License

MIT
