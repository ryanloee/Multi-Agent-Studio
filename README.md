# Multi-Agent Studio

**面向复杂工程的 AI Agent 可视化编排平台**

Multi-Agent Studio 是一个开源的 AI 多智能体工作流编排系统。用户通过拖拽式可视化画布构建多 Agent 协作流程，系统在本地沙盒中隔离执行，并实时流式呈现 Agent 的每一步操作。核心 Agent 引擎基于 [OpenCode CLI](https://github.com/opencode-ai/opencode)（MIT 协议），复用其内置 Agent、编程工具和 75+ 模型提供商能力。

## 核心特性

- **可视化编排** -- 基于 React Flow v12 的拖拽画布，支持 8 种节点类型（Coder / Planner / Designer / Explorer / Merger / Shell / Reviewer / Human），通过连线定义 DAG 执行拓扑
- **本地沙盒隔离** -- 每个 Agent 节点在独立工作目录中执行，Git Checkpoint 与回滚，无需 Docker
- **实时流式输出** -- OpenCode JSONL 文件写入 → Python 解析 → 事件总线 → WebSocket 推送 → 前端 Monaco Editor 打字机效果 / Xterm.js 终端渲染
- **自动规划模式** -- Planner Agent 从目标描述自动生成 DAG 工作流，支持 Chat 交互式调整
- **任务看板** -- 运行时任务管理，支持拓扑视图、任务分配、消息交互
- **Human-in-the-Loop** -- 人工审批机制，支持 Git Diff 展示、批准/拒绝操作

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
│  │  REST Handler  │  │ WebSocket Hub  │  │  Local DAG        │  │
│  │ (工作流 / 运行) │  │ (asyncio 推流) │  │  Executor         │  │
│  └────────────────┘  └────────────────┘  └───────────────────┘  │
│  ┌────────────────┐  ┌────────────────┐  ┌───────────────────┐  │
│  │  Planner Chat  │  │  Task Board    │  │  Local Sandbox    │  │
│  │ (SSE 交互式)   │  │ (任务看板)     │  │  (文件系统沙盒)   │  │
│  └────────────────┘  └────────────────┘  └───────────────────┘  │
│  ┌─────────────────────────────────────────────────────────────┐ │
│  │  Streaming Pipeline                                         │ │
│  │  文件 JSONL → 解析 → 事件总线 → WebSocket 推送              │ │
│  └─────────────────────────────────────────────────────────────┘ │
└───────────┬──────────────────────────────────────────────────────┘
            │ 子进程调用
┌───────────▼──────────────────────────────────────────────────────┐
│         opencode-runner (Bun / TypeScript)                        │
│                                                                   │
│  ┌───────────────────┐  ┌───────────────────┐                    │
│  │   SSE Server      │  │   OpenCode CLI    │                    │
│  │   (事件流式推送)   │──│   (子进程执行)    │                    │
│  │   :0 随机端口      │  │   stream.jsonl    │                    │
│  └───────────────────┘  └───────────────────┘                    │
└──────────────────────────────────────────────────────────────────┘
```

### MVP 本地模式

当前版本使用本地组件替代生产基础设施，零外部依赖即可运行：

| 生产架构 | MVP 本地替代 | 说明 |
|----------|-------------|------|
| PostgreSQL | SQLite (aiosqlite) | 数据库文件在 `apps/orchestrator/data/` |
| Redis Pub/Sub | InProcessEventBus | `app/core/local_bus.py`，进程内 asyncio 事件总线 |
| Temporal.io | LocalDAGExecutor | `app/core/local_engine.py`，本地 DAG 拓扑执行器 |
| Docker 沙盒 | LocalSandbox | `app/core/local_sandbox.py`，文件系统级隔离 |

## 技术栈

| 层级 | 技术 | 说明 |
|------|------|------|
| **前端** | Next.js 14 (App Router) | React 框架 |
| | React Flow v12 (`@xyflow/react`) | 节点编辑器画布 |
| | Zustand v5 | 轻量状态管理（6 个 Store） |
| | TailwindCSS v3 | 样式系统 |
| | Monaco Editor | 代码编辑 / Prompt 编写 |
| | Xterm.js | 终端输出渲染 |
| **后端** | Python 3.11+ / FastAPI | REST + WebSocket 一体化 |
| | SQLite + aiosqlite | 本地持久化存储 |
| | SQLAlchemy 2 (async) | 异步 ORM |
| | Pydantic v2 | 数据校验与配置 |
| | httpx | 异步 HTTP 客户端（SSE 订阅） |
| **Agent 引擎** | OpenCode CLI | 封装为子 Agent，MIT 协议 |
| | opencode-runner (Bun/TS) | Node 执行器，SSE 事件推送 |
| | Bun | TypeScript 运行时，启动 opencode 子进程 |

## 快速开始

### 前置条件

| 依赖 | 最低版本 | 检查命令 | 说明 |
|------|----------|----------|------|
| Node.js | 18+ | `node --version` | 前端构建 |
| Bun | 最新 | `bun --version` | opencode-runner 运行时 |
| Python | 3.11+ | `python3 --version` | 后端运行 |
| pnpm | 任意 | `pnpm --version` | 前端包管理 |
| Poetry | 最新 | `poetry --version` | 后端包管理 |
| Git | 任意 | `git --version` | Checkpoint / 回滚 |

> **Windows 用户**：推荐安装 [Git for Windows](https://git-scm.com/download/win)，Shell 节点依赖 Git Bash。

### 安装 Bun

opencode-runner 使用 Bun 运行时执行 OpenCode CLI：

```bash
# 通过 npm 安装
npm install -g bun

# 或通过官方安装脚本
# Windows (PowerShell):
powershell -c "irm bun.sh/install.ps1 | iex"
# Linux / macOS:
curl -fsSL https://bun.sh/install | bash

# 验证
bun --version
```

### 安装依赖

```bash
# 1. 克隆项目
git clone <repo-url>
cd multi-agent-studio

# 2. 安装前端依赖
cd apps/web && pnpm install && cd ../..

# 3. 安装后端依赖
cd apps/orchestrator && poetry install --no-root && cd ../..
```

### 配置模型提供商

系统通过 `apps/orchestrator/app/api/models.json` 配置模型提供商。每个提供商需要对应的 API Key，配置在 `apps/orchestrator/.env` 中。

#### models.json 结构

```json
{
  "providers": [
    {
      "id": "glm",
      "label": "GLM",
      "url": "https://open.bigmodel.cn/api/anthropic",
      "key": "GLM_API_KEY",
      "models": [
        {"id": "glm-5.1", "label": "glm-5.1"},
        {"id": "glm-4.7", "label": "glm-4.7"}
      ]
    },
    {
      "id": "mimo",
      "label": "MiMo",
      "url": "https://token-plan-cn.xiaomimimo.com/anthropic",
      "key": "MIMO_API_KEY",
      "models": [
        {"id": "mimo-v2.5", "label": "mimo-v2.5"}
      ]
    }
  ]
}
```

#### .env 配置

```bash
# apps/orchestrator/.env

# 数据库（SQLite，无需额外安装）
MAS_DATABASE_URL=sqlite+aiosqlite:///./data/mas.db

# 服务
MAS_HOST=0.0.0.0
MAS_PORT=8000

# 可选：访问密码（LAN 部署时使用，设置后 API 和 WebSocket 需携带）
# MAS_ACCESS_PASSWORD=your_password

# 模型 API Key（至少配置一个）
GLM_API_KEY=your_glm_api_key_here
MIMO_API_KEY=your_mimo_api_key_here
```

#### API Key 解析优先级

当节点执行时，系统按以下顺序查找 API Key：

1. **节点模型配置** -- 节点上配置的模型对应的 provider key
2. **models.json provider 配置** -- `key` 字段指向的环境变量
3. **全量扫描** -- 扫描所有 provider，使用第一个有 key 的
4. **环境变量兜底** -- `MIMO_API_KEY` 环境变量

### 启动服务

```bash
# 终端 1: 启动后端 API
cd apps/orchestrator && poetry run python -m app.main

# 终端 2: 启动前端
cd apps/web && pnpm dev
```

或使用启动脚本：

```bash
# Windows (PowerShell):
.\scripts\start.ps1

# Linux / macOS:
bash scripts/start.sh
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
```

## 项目结构

```
multi-agent-studio/
├── apps/
│   ├── web/                            # Next.js 14 前端应用
│   │   ├── src/
│   │   │   ├── app/                    # App Router 页面
│   │   │   │   └── workflows/
│   │   │   │       └── [id]/
│   │   │   │           └── WorkflowEditor.tsx  # 工作流编辑器主页面
│   │   │   ├── components/
│   │   │   │   ├── canvas/             # React Flow 画布组件
│   │   │   │   │   ├── FlowCanvas.tsx  # 画布主组件
│   │   │   │   │   ├── BaseNode.tsx    # 节点基类组件
│   │   │   │   │   ├── GoalInput.tsx   # 自动规划目标输入
│   │   │   │   │   ├── PlannerChat.tsx # Planner 聊天组件
│   │   │   │   │   ├── nodeTypes.ts    # 节点类型注册映射
│   │   │   │   │   ├── edges/          # 自定义连线
│   │   │   │   │   └── nodes/          # 自定义节点组件
│   │   │   │   │       ├── CoderNode.tsx
│   │   │   │   │       ├── PlanNode.tsx    # Planner + Designer 共用
│   │   │   │   │       ├── ExploreNode.tsx
│   │   │   │   │       ├── MergeNode.tsx
│   │   │   │   │       ├── ShellNode.tsx
│   │   │   │   │       ├── ReviewNode.tsx
│   │   │   │   │       ├── ChildNode.tsx   # 子任务节点
│   │   │   │   │       └── HumanNode.tsx
│   │   │   │   ├── panels/             # 右侧面板
│   │   │   │   │   ├── ConfigPanel.tsx     # 节点配置面板
│   │   │   │   │   ├── OutputPanel.tsx     # 输出面板（Agent/Shell/Events/Chat Tab）
│   │   │   │   │   ├── LLMOutput.tsx       # LLM 输出渲染（thinking/响应分离）
│   │   │   │   │   ├── XtermStream.tsx     # 终端输出渲染
│   │   │   │   │   ├── EventsTab.tsx       # 事件流面板
│   │   │   │   │   ├── PlannerChatTab.tsx  # Planner 交互式聊天
│   │   │   │   │   ├── TaskBoard.tsx       # 任务看板
│   │   │   │   │   ├── TaskTopology.tsx    # 任务拓扑视图
│   │   │   │   │   ├── ApprovalModal.tsx   # 审批弹窗
│   │   │   │   │   ├── CommandEditor.tsx   # Shell 命令编辑器
│   │   │   │   │   ├── ModelSelector.tsx   # 模型选择器
│   │   │   │   │   ├── PermissionsEditor.tsx # 权限编辑器
│   │   │   │   │   └── PromptEditor.tsx    # Prompt 编辑器
│   │   │   │   ├── sidebar/
│   │   │   │   │   ├── LeftPanel.tsx       # 左侧面板（节点库 + 工作流列表）
│   │   │   │   │   └── Sidebar.tsx         # 侧边栏容器
│   │   │   │   ├── toolbar/
│   │   │   │   │   └── Toolbar.tsx         # 顶部工具栏
│   │   │   │   ├── auth/
│   │   │   │   │   └── AuthGate.tsx        # 认证网关
│   │   │   │   ├── settings/
│   │   │   │   │   └── SettingsModal.tsx   # 全局设置弹窗
│   │   │   │   └── common/
│   │   │   │       ├── DirectoryPicker.tsx # 目录选择器
│   │   │   │       └── MarkdownMessage.tsx # Markdown 消息渲染
│   │   │   ├── stores/
│   │   │   │   ├── workflowStore.ts    # 工作流状态管理
│   │   │   │   ├── runStore.ts         # 运行状态管理 + 事件查询
│   │   │   │   ├── taskStore.ts        # 任务看板状态
│   │   │   │   ├── plannerChatStore.ts # Planner 聊天状态
│   │   │   │   ├── settingsStore.ts    # 全局设置
│   │   │   │   └── localeStore.ts      # 语言切换（中/英）
│   │   │   ├── types/
│   │   │   │   ├── workflow.ts         # 工作流 / 节点类型定义
│   │   │   │   ├── events.ts           # 事件类型定义
│   │   │   │   ├── task.ts             # 任务类型定义
│   │   │   │   ├── settings.ts         # 设置类型定义
│   │   │   │   ├── api.ts              # API 响应类型
│   │   │   │   └── css.d.ts            # CSS 模块声明
│   │   │   ├── hooks/
│   │   │   │   └── useWebSocket.ts     # WebSocket 连接 Hook
│   │   │   └── lib/
│   │   │       ├── api.ts              # API 客户端
│   │   │       ├── auth.ts             # 认证工具
│   │   │       ├── constants.ts        # 节点元数据、连接规则、状态颜色
│   │   │       ├── i18n.ts             # 中英文翻译（270+ 键/语言）
│   │   │       └── plannerObservable.ts # Planner 事件流 Observable
│   │   └── package.json
│   │
│   ├── orchestrator/                   # Python 编排引擎（核心）
│   │   ├── pyproject.toml              # Poetry 依赖配置
│   │   ├── .env                        # 环境变量（API Key 等）
│   │   ├── app/
│   │   │   ├── main.py                 # FastAPI 入口（REST + WebSocket + Lifespan）
│   │   │   ├── config.py               # 配置项（Pydantic Settings，MAS_ 前缀）
│   │   │   ├── launcher.py             # 启动器
│   │   │   ├── api/
│   │   │   │   ├── workflows.py        # 工作流 CRUD + 评估
│   │   │   │   ├── runs.py             # 运行管理（触发/取消/审批/回滚）
│   │   │   │   ├── models.py           # 可用模型列表
│   │   │   │   ├── models.json         # 模型提供商配置（URL / Key / 模型列表）
│   │   │   │   ├── planner_chat.py     # Planner 交互式聊天（SSE）
│   │   │   │   ├── settings.py         # 全局设置（路径验证/目录浏览/模型测试）
│   │   │   │   ├── tasks.py            # 任务看板 CRUD + 分配/消息/重启
│   │   │   │   ├── artifacts.py        # 运行产物管理
│   │   │   │   └── shared_doc.py       # 工作流共享文档
│   │   │   ├── ws/
│   │   │   │   └── hub.py              # WebSocket Hub（asyncio）
│   │   │   ├── core/
│   │   │   │   ├── local_engine.py     # 本地 DAG 执行器（核心）
│   │   │   │   ├── local_bus.py        # 进程内事件总线
│   │   │   │   ├── local_sandbox.py    # 文件系统沙盒
│   │   │   │   ├── database.py         # SQLite 连接
│   │   │   │   └── task_scheduler.py   # 任务调度器（死循环检测）
│   │   │   ├── workflows/
│   │   │   │   ├── compiler.py         # DAG 编译器（React Flow JSON → 拓扑层）
│   │   │   │   ├── plan_parser.py      # Plan 节点输出 → 子节点创建
│   │   │   │   └── task_compiler.py    # Task Board → DAG 编译
│   │   │   ├── sandbox/
│   │   │   │   ├── provision.py        # 沙盒工作目录初始化
│   │   │   │   └── checkpoint.py       # Git Checkpoint 管理
│   │   │   └── models/
│   │   │       ├── db.py               # ORM 模型
│   │   │       ├── schemas.py          # Pydantic 请求/响应模型
│   │   │       └── task.py             # 任务 ORM 模型
│   │   ├── data/                       # SQLite 数据库文件
│   │   └── .sandboxes/                 # 沙盒工作目录
│   │
│   └── opencode-runner/                # Node 执行器（Bun / TypeScript）
│       ├── run-node.ts                 # 主入口：SSE Server + OpenCode 子进程
│       └── vendor/
│           └── opencode/               # OpenCode CLI 源码（vendored）
│
├── packages/
│   └── shared-types/                   # 共享 JSON Schema
│       └── schemas/
│           ├── workflow.json           # 工作流数据结构
│           ├── events.json             # 事件数据结构
│           └── node-config.json        # 节点配置结构
│
├── scripts/
│   ├── setup.sh / setup.ps1           # 一键安装脚本
│   ├── start.sh / start.ps1           # 开发环境启动脚本
│   └── build.ps1                      # 构建脚本
│
└── README.md
```

## opencode-runner 架构

opencode-runner 是节点执行的核心组件，负责启动 OpenCode CLI 并通过 SSE 实时推送执行事件。

### 执行流程

```
LocalDAGExecutor (Python)
    │
    ├─ 1. 构建 CLI 参数（--model, --provider-url, --provider-key, --prompt 等）
    │
    ├─ 2. 通过 LocalSandbox.exec_async() 启动子进程
    │      └─ bun run-node.ts [args...]
    │
    ├─ 3. run-node.ts 启动 Bun.serve() SSE Server（随机端口）
    │      └─ 读取 stream.jsonl，实时推送事件到 SSE 连接
    │
    ├─ 4. run-node.ts 启动 opencode 子进程
    │      └─ opencode run --format json --model [model] --prompt [prompt]
    │
    ├─ 5. Engine 通过 httpx 订阅 SSE（trust_env=False，绕过系统代理）
    │      └─ 解析事件：llm_token, tool_call, shell_stdout, node_completed 等
    │
    └─ 6. 事件 → InProcessEventBus → WebSocketHub → 前端实时渲染
```

### SSE 事件类型

| 事件 | 说明 | 前端渲染 |
|------|------|----------|
| `llm_token` | LLM 逐 Token 输出 | Monaco Editor 打字机效果 |
| `tool_call` | 工具调用（edit/bash/grep 等） | 工具调用面板 |
| `tool_result` | 工具调用结果 | 工具调用面板 |
| `shell_stdout` | Shell 标准输出 | Xterm.js 终端 |
| `node_completed` | 节点执行完成 | 节点状态变更 |
| `node_failed` | 节点执行失败 | 错误提示 |

### 关键设计

- **代理绕过**：Engine 使用 `trust_env=False` 避免系统 HTTP_PROXY 干扰 localhost SSE 连接
- **进程等待**：Engine 在检查 exit code 前先 `wait_process()` 等待进程退出
- **终端事件检查**：SSE 断连时检查是否已收到 `node_completed`，避免误判为失败
- **Windows 兼容**：`process.execPath` 替代硬编码 `"bun"`，支持全局或项目级 bun 安装

## 节点类型

| 节点类型 | Agent 类型 | 说明 | 可连接目标 |
|----------|-----------|------|-----------|
| **Coder** | `coder` | 编写和修改代码文件 | 所有节点 |
| **Planner** | `plan` | 分析任务，制定执行计划 | 所有节点 |
| **Designer** | `design` | 产生架构/设计指导文档 | Coder, Explore, Shell, Review, Merge, Human |
| **Explorer** | `explore` | 搜索代码库，收集信息 | 所有节点 |
| **Merger** | `merge` | 合并并行代码变更，解决冲突 | Coder, Plan, Design, Explore, Shell, Review, Human |
| **Shell** | `shell` | 执行 Shell 命令 | 除 Review 外的所有节点 |
| **Reviewer** | `review` | 审查代码变更，提供反馈 | 除 Shell 外的所有节点 |
| **Human** | `human` | 暂停等待人工审批或输入 | 仅作为目标（sink），无出向连接 |

连接规则：Shell ↔ Review 互相不可连接，Human 只能作为终点。

## 配置说明

所有配置通过环境变量管理，统一以 `MAS_` 为前缀。配置文件位于 `apps/orchestrator/.env`，由 `pydantic-settings` 自动加载。

### 环境变量一览

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `MAS_HOST` | `0.0.0.0` | API 服务监听地址 |
| `MAS_PORT` | `8000` | API 服务监听端口 |
| `MAS_DATABASE_URL` | `sqlite+aiosqlite:///./data/mas.db` | SQLite 数据库路径 |
| `MAS_ACCESS_PASSWORD` | _(空)_ | 可选的访问密码，设置后 API 和 WebSocket 请求需携带 |
| `GLM_API_KEY` | _(空)_ | GLM 平台 API Key |
| `MIMO_API_KEY` | _(空)_ | MiMo 平台 API Key |

### 前端环境变量

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `BACKEND_URL` | `http://localhost:8000` | 后端 URL（Next.js rewrites 代理目标） |
| `NEXT_PUBLIC_API_URL` | `/api` | API 基础路径 |
| `NEXT_PUBLIC_WS_URL` | `ws://localhost:8000` | WebSocket 地址 |

## API 文档

后端 FastAPI 启动后访问 `http://localhost:8000/docs` 可查看自动生成的 Swagger 文档。

### 认证

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/api/auth/status` | 认证状态查询 |
| `POST` | `/api/auth/verify` | 验证访问密码 |

### 工作流管理

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/api/workflows` | 创建工作流 |
| `GET` | `/api/workflows` | 工作流列表 |
| `GET` | `/api/workflows/:id` | 工作流详情 |
| `PUT` | `/api/workflows/:id` | 更新工作流 |
| `DELETE` | `/api/workflows/:id` | 删除工作流及关联运行 |
| `POST` | `/api/workflows/:id/assess` | 工作流评估 |

### 运行管理

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/api/runs/:workflowId/run` | 触发工作流执行 |
| `GET` | `/api/runs` | 运行记录列表 |
| `GET` | `/api/runs/:id` | 运行详情 |
| `GET` | `/api/runs/:id/events` | 运行事件历史 |
| `GET` | `/api/runs/:id/nodes` | 节点执行详情 |
| `GET` | `/api/runs/:id/diff` | Git Diff（Human-in-the-Loop） |
| `POST` | `/api/runs/:id/cancel` | 取消运行 |
| `POST` | `/api/runs/:id/approve` | 批准暂停的运行 |
| `POST` | `/api/runs/:id/reject` | 拒绝暂停的运行 |
| `POST` | `/api/runs/:id/rollback` | 回滚到上一个 Checkpoint |

### 运行产物

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/api/runs/:run_id/artifacts` | 产物列表 |
| `GET` | `/api/runs/:run_id/artifacts/:id` | 产物详情 |
| `POST` | `/api/runs/:run_id/artifacts` | 创建产物 |
| `PATCH` | `/api/runs/:run_id/artifacts/:id` | 更新产物 |

### 任务管理

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/api/runs/:run_id/tasks` | 任务列表 |
| `GET` | `/api/runs/:run_id/tasks/:task_id` | 任务详情 |
| `POST` | `/api/runs/:run_id/tasks` | 创建任务 |
| `PATCH` | `/api/runs/:run_id/tasks/:task_id` | 更新任务 |
| `POST` | `/api/runs/:run_id/tasks/:task_id/restart` | 重启任务 |
| `POST` | `/api/runs/:run_id/tasks/:task_id/assign` | 分配任务到节点 |
| `POST` | `/api/runs/:run_id/tasks/:task_id/messages` | 发送任务消息 |
| `GET` | `/api/runs/:run_id/tasks/:task_id/messages` | 任务消息列表 |

### Planner 聊天

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/api/planner/chat` | Planner 交互式聊天（SSE 流式响应） |
| `GET` | `/api/planner/history/:workflow_id` | 聊天历史 |

### 共享文档

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/api/workflows/:id/shared-doc` | 获取共享文档 |
| `PUT` | `/api/workflows/:id/shared-doc` | 更新共享文档 |

### 全局设置

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/api/settings` | 获取设置 |
| `PUT` | `/api/settings` | 更新设置 |
| `POST` | `/api/settings/validate-path` | 验证文件路径 |
| `POST` | `/api/settings/test-model-url` | 测试模型 URL 连通性 |
| `POST` | `/api/settings/browse-dir` | 浏览目录内容 |
| `POST` | `/api/settings/list-dir` | 列出目录条目 |

### 其他

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/api/models` | 可用模型列表 |
| `GET` | `/health` | 健康检查 |

### WebSocket

| 协议 | 路径 | 说明 |
|------|------|------|
| `WS` | `/ws/runs/:runId/stream` | 实时运行事件流 |

服务端每 30 秒发送 `{"type": "ping"}` 心跳，最大缓冲 500 条事件（供晚连接的客户端回放），自动重连间隔 3 秒。

## 使用指南

### 创建工作流

1. 打开浏览器访问 `http://localhost:3000`
2. 点击 **"新建工作流"** 按钮
3. 输入工作流名称和描述

### 手动编排模式

从左侧节点面板拖拽节点到画布，通过拖拽节点端口创建连线，连线方向即为数据流向（DAG 拓扑）。

### 自动规划模式

1. 在 Planner Chat 面板中描述目标（如 "我要开发一个游戏CDK销售网站"）
2. Planner Agent 自动生成任务列表
3. 确认后系统自动编译为 DAG 并创建节点
4. 点击 Run 执行

### 运行工作流

1. 点击画布右上角 **"Run"** 按钮
2. 系统将 DAG 提交到 LocalDAGExecutor
3. 前端自动建立 WebSocket 连接接收实时事件

### 查看实时输出

工作流运行后，点击节点查看输出：

- **Agent 面板** -- LLM 输出（thinking 和响应分离展示）
- **Shell 面板** -- 终端输出，Xterm.js 渲染
- **Events 面板** -- 工具调用事件流
- **Chat 面板** -- Planner 交互式聊天

### 语言切换

前端支持中英文界面切换（270+ 翻译键），通过顶栏语言选择器切换。

## 开发指南

### 前端开发（apps/web）

```bash
cd apps/web

pnpm install              # 安装依赖
pnpm dev                  # 开发服务器（热更新）:3000
pnpm type-check           # TypeScript 类型检查
pnpm lint                 # ESLint 代码检查
pnpm build                # 生产构建
```

### 后端开发（apps/orchestrator）

```bash
cd apps/orchestrator

poetry install                            # 安装依赖
poetry run python -m app.main             # 启动 API 服务（:8000，带热更新）
poetry run pytest                         # 运行测试
poetry run pytest tests/test_x.py -v      # 运行单个测试文件
poetry run ruff check app/                # 代码检查
poetry run mypy app/                      # 类型检查
```

### 添加新的模型提供商

编辑 `apps/orchestrator/app/api/models.json`：

```json
{
  "providers": [
    {
      "id": "your-provider",
      "label": "Your Provider",
      "url": "https://api.your-provider.com/anthropic",
      "key": "YOUR_PROVIDER_API_KEY",
      "models": [
        {"id": "model-id", "label": "Model Name"}
      ]
    }
  ]
}
```

然后在 `apps/orchestrator/.env` 中添加对应的 API Key。

## 故障排除

### Bun 未安装

**症状**：`bun: command not found` 或节点执行立即失败

**解决**：`npm install -g bun`

### API Key 未配置

**症状**：节点执行失败，日志显示 `401 Unauthorized` 或 `missing api key`

**解决**：确保 `apps/orchestrator/.env` 中至少配置了一个有效的 API Key。

### 系统代理干扰

**症状**：节点启动后立即失败（exit_code=-1），日志显示 SSE 连接错误

**解决**：Engine 已使用 `trust_env=False` 绕过系统代理。如果仍有问题，临时取消代理：

```bash
# Windows
set http_proxy=
set https_proxy=
# Linux/macOS
unset http_proxy https_proxy
```

### 端口被占用

**解决**：

```bash
# Windows
netstat -ano | findstr :8000
# Linux / macOS
lsof -i :8000
# 修改 .env 中的端口号
MAS_PORT=8001
```

### Windows Shell 节点问题

**解决**：确保安装了 Git for Windows，Shell 节点使用 Git Bash 执行命令。

## 端口一览

| 端口 | 服务 | 地址 |
|------|------|------|
| 3000 | 前端 (Next.js) | http://localhost:3000 |
| 8000 | 后端 API (FastAPI) | http://localhost:8000 |
| 8000 | API 文档 (Swagger) | http://localhost:8000/docs |

## License

MIT
