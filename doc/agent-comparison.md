# MAS Agent vs OpenCode 对比分析

> 对比对象：本项目 `apps/agent/mas_agent/` vs 开源项目 [OpenCode](https://github.com/opencode-ai/opencode)（v1.14.41）
>
> 目的：识别功能差距和潜在风险，指导后续改进优先级

---

## 1. 架构概览

| 维度 | MAS Agent | OpenCode |
|------|-----------|----------|
| 语言 | Python (~500 行) | TypeScript (~50,000+ 行) |
| 运行时 | 子进程 (subprocess.Popen) | Bun/Node.js |
| 核心循环 | 手写 while 循环 | Vercel AI SDK `streamText()` |
| 依赖 | 纯 stdlib + httpx | Effect + AI SDK + 30+ 包 |
| 工具数量 | 4 个 | 20+ 个 |
| LLM Provider | 1 种 (Anthropic 兼容) | 20+ 种 |
| Agent 类型 | 6 种 (仅提示词区分) | 8 种 (提示词 + 权限 + 工具集区分) |
| 编排方式 | 外部 DAG 编排器 | 内部 task 工具 |

### MAS 架构

```
编排器 (FastAPI)                    Agent 进程
┌──────────────┐                   ┌─────────────────┐
│ compile_dag  │── start_workflow ─→│ mas_agent CLI    │
│ _execute_dag │←─ stream.jsonl ───│ AgentLoop        │
│ TaskScheduler│   (每秒轮询)       │  ├ LLM (httpx)   │
└──────────────┘                   │  ├ Tools (4个)   │
                                   │  └ StreamWriter  │
                                   └─────────────────┘
```

### OpenCode 架构

```
TUI / HTTP API / ACP / MCP
┌──────────────────────────────────────────────────┐
│ Session → Prompt → Processor → LLM (AI SDK)      │
│   ├ 20+ Tools (edit/grep/shell/task/...)          │
│   ├ Permission (allow/deny/ask)                   │
│   ├ Compaction (自动上下文压缩)                     │
│   ├ Snapshot (每步 git commit)                     │
│   ├ LSP (编辑后诊断)                               │
│   ├ MCP (外部工具协议)                             │
│   └ Plugin (钩子系统)                              │
└──────────────────────────────────────────────────┘
```

---

## 2. 工具系统对比

### 工具清单

| 工具 | MAS | OpenCode | 差距说明 |
|------|:---:|:--------:|----------|
| 文件名搜索 `glob` | ✅ | ✅ | 功能相当 |
| 文件读取 `read` | ✅ | ✅ | MAS 无分页提示，OpenCode 有 |
| 文件写入 `write` | ✅ | ✅ | MAS 整文件覆写，OpenCode 有 diff 显示 |
| Shell 执行 `shell` | ✅ | ✅ | MAS 无路径安全检查，无超时保护 |
| **搜索替换 `edit`** | ❌ | ✅ | **最大差距** — MAS 只能整文件覆写 |
| **内容搜索 `grep`** | ❌ | ✅ | **关键缺失** — 无法搜索代码内容 |
| Unified Diff `apply_patch` | ❌ | ✅ | 适用于 GPT 系列模型 |
| 子 Agent `task` | ❌ | ✅ | MAS 通过编排器外部实现 |
| Web 获取 `fetch` | ❌ | ✅ | 获取 URL 内容 |
| Web 搜索 `search` | ❌ | ✅ | Web 搜索 |
| 待办管理 `todo` | ❌ | ✅ | 任务进度追踪 |
| 代码搜索 `codesearch` | ❌ | ✅ | 语义代码搜索 (实验性) |
| 仓库克隆 `repo_clone` | ❌ | ✅ | 克隆外部仓库 (实验性) |
| LSP 操作 `lsp` | ❌ | ✅ | 语言服务器协议 (实验性) |
| MCP 工具 | ❌ | ✅ | 动态加载外部工具 |
| 技能加载 `skill` | ❌ | ✅ | 运行时注入专业指令 |

### 核心差距详解

#### (a) 没有 `edit` 搜索替换工具 — 🔴 紧急

**现状**：MAS 的 `write` 工具每次都覆写整个文件。

**问题**：
- LLM 必须输出完整文件内容才能改一行，token 浪费严重
- 大文件容易截断或遗漏内容
- LLM 输出的代码可能有格式偏差，整文件覆写无法容错

**OpenCode 做法**：
- `edit` 工具支持搜索替换模式，只输出需要修改的部分
- 10 层降级容错策略：精确匹配 → 行级模糊 → 块锚点 → 空白归一化 → 缩进灵活 → 转义归一化 → 边界修剪 → 上下文感知 → Levenshtein 相似度
- 文件级信号量锁防止并发冲突
- 编辑后自动 LSP 诊断 + 自动格式化

#### (b) 没有 `grep` 内容搜索 — 🔴 紧急

**现状**：Agent 无法搜索文件内容。

**问题**：
- 编码任务的核心操作就是"找到相关代码 → 修改"，缺少搜索等于半残
- Agent 只能先 `glob` 找文件名，再 `read` 逐个读文件，效率极低
- 对于"修改所有调用 XXX 函数的地方"这类任务，当前工具完全无法完成

**OpenCode 做法**：
- `grep` 工具基于 ripgrep，支持正则搜索
- 输出带上下文行、文件名、行号
- 结果数量限制避免过大输出

---

## 3. 权限与安全

| 安全机制 | MAS | OpenCode |
|----------|:---:|:--------:|
| 操作确认 | ❌ | ✅ allow/deny/ask 三级 |
| Shell 命令审查 | ❌ | ✅ tree-sitter 解析 + 路径提取 |
| 外部目录保护 | ❌ | ✅ 工作树外操作需额外权限 |
| 敏感文件保护 | ❌ | ✅ .env 等文件编辑需确认 |
| Doom Loop 防护 | ❌ | ✅ 连续 3 次相同调用触发确认 |
| 权限持久化 | ❌ | ✅ "always allow" 持久化到 DB |

**风险**：当前 MAS Agent 可以无限制执行任何 shell 命令、写入任何文件。一个错误的 LLM 输出可能导致 `rm -rf /` 或覆盖关键系统文件。

---

## 4. LLM Provider

| 能力 | MAS | OpenCode |
|------|:---:|:--------:|
| Provider 数量 | 1 (Anthropic 兼容) | 20+ |
| SSE 流式 | ✅ 手写解析 | ✅ AI SDK 标准化 |
| 工具调用修复 | ❌ | ✅ 自动修大小写错误 |
| 自动重试 | ❌ | ✅ 内置重试 |
| Extended Thinking | ❌ | ✅ Provider 变换支持 |
| 模型发现 | 硬编码 | 从 models.dev 动态加载 |
| 流式解析健壮性 | 一般 | 完善的 content_block 处理 |

**说明**：MAS 的单 Provider 设计对当前场景够用（内部统一走 Anthropic 兼容接口），但手写的 SSE 解析器健壮性不如 AI SDK。

---

## 5. 上下文管理

| 机制 | MAS | OpenCode |
|------|:---:|:--------:|
| 上下文压缩 | ❌ | ✅ 自动 compaction |
| 压缩策略 | 无 | 保留最近 2 轮 + 历史摘要 (Goal/Constraints/Progress) |
| 工具输出截断 | 固定 5000 字符 | 智能：大输出存文件 + 返回尾部 |
| 消息截断 | 超过 max_tokens 直接失败 | 自动压缩后继续 |
| 子 Agent 上下文 | 独立进程，无共享 | 继承父会话权限，可恢复 |

**风险**：长任务中 Agent 的上下文会不断膨胀（每次工具调用都追加结果），超过模型上下文窗口后直接失败，没有自动压缩机制。对于复杂的多步骤编码任务，这几乎是必然发生的。

---

## 6. Agent 编排

| 方面 | MAS | OpenCode |
|------|:---:|:--------:|
| 编排方式 | **外部 DAG 编排器** | 内部 task 工具 |
| 多 Agent 协作 | ✅ DAG 层级 + 动态 Plan→Worker | 仅 task 子 agent（扁平） |
| 升级协议 | ✅ Worker↔Planner 双向对话 | ❌ 无对等升级机制 |
| 可视化编排 | ✅ 画布拖拽 + 实时节点状态 | ❌ 纯 CLI/TUI |
| 任务管理 UI | ✅ 任务板 CRUD + 分配 + 重启 | ❌ 无 |
| 运行状态监控 | ✅ 节点级状态 + 进度条 | ❌ 仅消息流 |
| Plan 模式 | ✅ Planner 生成结构化子任务 | 5 阶段固定工作流 |
| 动态节点 | ✅ 运行时添加子节点到画布 | ❌ 无可视化 |

**说明**：编排和可视化是 MAS 的核心优势，OpenCode 没有对标功能。

---

## 7. 其他功能差距

| 功能 | MAS | OpenCode | 影响 |
|------|:---:|:--------:|------|
| MCP 协议 | ❌ | ✅ | 无法接入外部工具生态 |
| LSP 集成 | ❌ | ✅ | 编辑后无类型检查反馈 |
| Git 快照 | 简单 auto-commit | 每步 git commit + diff + revert | 错误难以精确回滚 |
| 插件系统 | ❌ | ✅ | 不可扩展 |
| 技能系统 | ❌ | ✅ | 无法注入领域知识 |
| 配置系统 | 环境变量 | 多级 JSON 配置 + Schema 校验 | 难以定制 |
| ACP 协议 | ❌ | ✅ | 无法被外部编辑器驱动 |
| 会话持久化 | ❌ | ✅ SQLite | 重启后丢失上下文 |
| 文件监控 | ❌ | ✅ @parcel/watcher | Agent 不知道外部文件变更 |

---

## 8. MAS 的独特优势

| 优势 | 说明 |
|------|------|
| **可视化 DAG 编排** | 拖拽式画布构建工作流，OpenCode 完全没有 |
| **实时节点状态** | 每个节点的运行/完成/失败状态可视化 |
| **Worker↔Planner 升级协议** | Worker 遇到困难可向 Planner 求助，OpenCode 无此机制 |
| **任务板** | 任务的创建/分配/重启/过滤，OpenCode 无任务管理 UI |
| **动态子节点** | Planner 运行时生成的子节点实时出现在画布上 |
| **架构简洁** | ~500 行核心代码，易于理解和修改 |

---

## 9. 改进优先级

### 🔴 P0 — 不修会出问题

| # | 改进 | 原因 | 预估工作量 |
|---|------|------|-----------|
| 1 | **添加 `grep` 工具** | 没有内容搜索，Agent 编码效率极低 | 0.5 天 |
| 2 | **添加 `edit` 搜索替换工具** | 整文件覆写浪费 token 且容易出错 | 1 天 |

### 🟡 P1 — 不修会有限制

| # | 改进 | 原因 | 预估工作量 |
|---|------|------|-----------|
| 3 | **添加权限系统** | 至少对 shell 删除命令和关键文件写入增加确认 | 2 天 |
| 4 | **添加上下文压缩** | 长任务必然超出上下文窗口 | 2 天 |
| 5 | **工具输出智能截断** | 大输出存文件返回引用，而非硬截断丢失信息 | 0.5 天 |
| 6 | **Doom Loop 检测** | 防止 LLM 陷入重复工具调用循环 | 0.5 天 |

### 🟢 P2 — 提升体验

| # | 改进 | 原因 | 预估工作量 |
|---|------|------|-----------|
| 7 | 工具调用自动修复 | 修复常见的大小写、格式错误 | 1 天 |
| 8 | `read` 工具增加分页提示 | 告诉 LLM 还有更多内容可读 | 0.5 天 |
| 9 | LSP 诊断反馈 | 编辑后自动检查类型错误 | 3 天 |
| 10 | MCP 协议支持 | 接入外部工具生态 | 5 天 |

---

## 10. 关键文件索引

### MAS Agent

| 文件 | 路径 | 职责 |
|------|------|------|
| CLI 入口 | `apps/agent/mas_agent/cli.py` | 参数解析 → 构造 LoopConfig |
| Agent 循环 | `apps/agent/mas_agent/loop.py` | LLM ↔ Tool 核心循环 |
| 事件写入 | `apps/agent/mas_agent/events.py` | StreamWriter → stream.jsonl |
| 工具注册 | `apps/agent/mas_agent/tools/__init__.py` | 4 工具注册 |
| Provider | `apps/agent/mas_agent/providers/anthropic_provider.py` | Anthropic SSE 解析 |
| 提示词 | `apps/agent/mas_agent/prompts/__init__.py` | 6 种 Agent 提示词 |

### OpenCode

| 文件 | 路径 | 职责 |
|------|------|------|
| 入口 | `packages/opencode/src/index.ts` | CLI + 多命令 |
| Agent 定义 | `packages/opencode/src/agent/agent.ts` | 8 种 Agent 定义 |
| 核心循环 | `packages/opencode/src/session/prompt.ts` | 提示处理 + 子任务 |
| 流处理 | `packages/opencode/src/session/processor.ts` | LLM 流事件处理 |
| 工具注册 | `packages/opencode/src/tool/registry.ts` | 20+ 工具 |
| edit 工具 | `packages/opencode/src/tool/edit.ts` | 搜索替换 + 10 层容错 |
| grep 工具 | `packages/opencode/src/tool/grep.ts` | ripgrep 正则搜索 |
| shell 工具 | `packages/opencode/src/tool/shell.ts` | tree-sitter 解析 + 安全检查 |
| 权限 | `packages/opencode/src/permission/index.ts` | allow/deny/ask |
| 上下文压缩 | `packages/opencode/src/session/compaction.ts` | 自动 compaction |
| Provider | `packages/opencode/src/provider/provider.ts` | 20+ Provider |
