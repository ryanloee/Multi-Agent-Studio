# 前端平台组 — Phase 0-1 详细实施计划

> 负责人：前端平台组
> 审查状态：待总指挥审查
> 预估总工作量：约 14 工作日（3 周含联调）

---

## 现有代码盘点

| 文件 | 完成度 | 问题 |
|------|--------|------|
| FlowCanvas.tsx | 30% | 仅默认节点，无自定义节点，未接入 Zustand |
| workflowStore.ts | 40% | 缺 onNodesChange/onEdgesChange，缺 node data 编辑 |
| runStore.ts | 60% | 缺 nodeStatuses 按 node_id 跟踪，缺 selector |
| useWebSocket.ts | 50% | 无条件重连，status 事件未同步 store，无手动断开 |
| api.ts | 70% | 全用 any 类型，缺错误处理 |

---

## 实施步骤

### Step 1: TypeScript 类型 + 常量与节点元数据（0.5 天）

**类型文件**：新建 `src/types/workflow.ts`、`src/types/events.ts`、`src/types/api.ts`

核心类型：
- `AgentNodeType`: 6 种节点类型联合
- `NodeData`: label/agentType/modelProvider/modelId/prompt/permissions/command
- `WorkflowNode = Node<NodeData, AgentNodeType>`
- `StreamEvent`: 从 events.json 对齐
- `ModelInfo`、`RunInfo` 等 API 响应类型

**常量文件**：新建 `src/lib/constants.ts`

- `NODE_META`: 6 种节点的图标/颜色/默认数据/描述
- `VALID_CONNECTIONS`: 连线规则白名单
- `STATUS_COLORS`: 运行状态 → CSS class 映射

验收：`pnpm type-check` 通过

---

### Step 2: Zustand Store 重构（1 天）

**workflowStore.ts 重构**：
- 接入 `applyNodeChanges`/`applyEdgeChanges`
- 添加 `addNode(type, position)` — 从 NODE_META 填充默认数据
- 添加 `updateNodeData(id, data)` — 配置面板唯一写入入口
- 添加 `removeNode`、`loadWorkflow`、`clearCanvas`
- `onConnect` 内置连线规则校验

**runStore.ts 扩展**：
- 新增 `nodeStatuses: Record<string, RunStatus>` — 每个节点独立状态
- `addEvent` 内部：status 事件自动同步到 `setNodeStatus`
- 新增 selector：`useEventsByType`、`useEventsByNode`、`useNodeStatus`

---

### Step 3: BaseNode + 6 种自定义节点（2 天）

**BaseNode.tsx** — 共享节点外壳：
- 宽 200px，顶部色条，左侧图标 + 标题
- Handle：顶部 TargetHandle，底部 SourceHandle
- 运行状态覆盖：从 runStore.nodeStatuses 读取，叠加 STATUS_COLORS
- 选中态：蓝色 ring
- 必须用 `memo()` 包裹

**6 种具体节点**：每种扩展 BaseNode，添加独有内容
- CoderNode：显示 modelId 小字
- PlanNode：显示 "Read-only" 标签
- ShellNode：显示 command 前 30 字符预览
- ReviewNode：显示状态文字
- ExploreNode / HumanNode：基础内容

注册 `nodeTypes` 映射。

---

### Step 4: FlowCanvas 重构（1 天）

- 删除内部 useNodesState/useEdgesState，改用 workflowStore
- 注册 6 种 nodeTypes
- 接入 onDragOver/onDrop 实现拖拽创建
- 接入 onNodeClick → setSelectedNode
- 接入 onConnect 连线校验
- snapToGrid + MiniMap

依赖：Step 1, 2, 3

---

### Step 5: 左侧侧边栏 Sidebar（0.5 天）

- 宽 240px，6 个可拖拽卡片
- 每个：图标 + 名称 + 描述
- `draggable` + `onDragStart` → `dataTransfer`

可与 Step 3 并行。

---

### Step 6: 右侧配置面板 ConfigPanel（1.5 天）

**面板容器**：selectedNodeId 非空时展示

**配置项按节点类型分发**：

| 配置项 | coder | plan | explore | shell | review | human |
|--------|-------|------|---------|-------|--------|-------|
| Label | Y | Y | Y | Y | Y | Y |
| Agent Type | Y | Y | Y | - | Y | - |
| Model 选择器 | Y | Y | Y | - | Y | - |
| Prompt 编辑器 | Y | Y | Y | - | Y | - |
| Permissions | Y | Y | - | - | - | - |
| Command | - | - | - | Y | - | - |
| Description | - | - | - | - | - | Y |

**子组件**：
- ModelSelector：调 /api/models 按 provider 分组
- PromptEditor：textarea + 字符计数 + 变量提示
- PermissionsEditor：radio 矩阵 (allow/deny/ask × tool)
- CommandEditor：单行 input

---

### Step 7: WebSocket Hook 重构（0.5 天）

- status 事件同步到 runStore.setStatus + setNodeStatus
- 手动断开控制（run 结束后停止重连）
- 连接失败 → setStatus("failed")
- onclose 区分手动/异常关闭

---

### Step 8: 底部输出面板 — OutputPanel 容器 + XtermStream + ToolCallList（1.5 天）

**容器**：可折叠，三个 Tab（LLM/Shell/Tools）+ 节点过滤下拉

**XtermStream.tsx**（优先实现，Shell 输出是核心能力）：
- @xterm/xterm + FitAddon
- shell_stdout 事件 → terminal.write()
- ResizeObserver 自动 fit()
- 组件卸载时 term.dispose()

**ToolCallList.tsx**：
- tool_call + tool_result 配对显示
- 时间戳 + 工具名 + 节点名 + 状态图标
- 可展开详情

---

### Step 9: MonacoStream / 简易 LLM 输出（0.5 天）

**MVP 方案**：先用简单 div 打字机效果替代 Monaco
- contentRef 追加 llm_token，requestAnimationFrame 批量更新
- 自动滚动到底部
- 预渲染 Markdown 渲染（可选）

**完整方案**（可后补至 post-MVP）：
- MonacoStream.tsx：@monaco-editor/react 只读模式
- 完整语法高亮与性能优化

---

### Step 10: 工具栏 + 运行控制（0.5 天）

- Toolbar：Logo + 工作流名称 + Save + Run/Cancel + 状态徽章
- RunControls：Run 触发 api.triggerRun，Cancel 触发 api.cancelRun

---

### Step 11: 主布局编排（1 天）

三栏布局：
```
┌──────────────────────────────────────────────┐
│ Toolbar (h-12)                               │
├──────┬────────────────────┬──────────────────┤
│ Side │  FlowCanvas        │  ConfigPanel     │
│ 240px│  (flex-1)          │  (w-320px)       │
│      ├────────────────────┤                  │
│      │  OutputPanel       │                  │
│      │  (h-300px, 折36px) │                  │
└──────┴────────────────────┴──────────────────┘
```

页面路由：`/workflows/[id]`
- useEffect 加载工作流数据
- useWebSocket 连接
- 2 秒 debounce 自动保存

---

### Step 12: 工作流列表页（1 天）

- `/workflows` 路由
- 网格卡片布局
- New Workflow → createWorkflow → 跳转编辑页
- 删除确认 → deleteWorkflow

---

### Step 13: Human-in-the-Loop 审批面板（1 天）

- runStore.status === "paused" 时弹出模态
- DiffViewer：调 getRunDiff，逐行着色（+绿/-红）
- Approve/Reject 按钮

---

### Step 14: API 类型化 + 错误处理（0.5 天）

- 所有 any 替换为具体类型
- 统一错误处理（401/404/500）
- 准备 Mock 模式（前端独立开发）

---

## 依赖关系与并行机会

```
Step 1 (Types+Constants) → Step 2 (Store) → Step 3 (节点) → Step 4 (Canvas) → Step 11 (主布局) → Step 12/13
                                                  ↓                ↓
                                            Step 5 (Sidebar)  Step 6 (Config)
                                                ↓                  ↓
                                            Step 7 (WebSocket) → Step 8 (Output+Xterm) → Step 9 (Monaco)
                                                ↓
                                            Step 10 (Toolbar)
```

**可并行**：
- Step 5 与 Step 3
- Step 8 与 Step 6
- Step 12 与 Step 13

---

## Step 15: 前后端联调（2 天）

**1. 何时从 Mock 模式切换到真实后端**

- 前端 Step 1–11 完成并通过自测后，开始切真实后端
- 切换方式：`api.ts` 中 `MOCK_MODE = false`，所有接口指向后端真实地址
- 建议分两轮联调：第一轮核心流程（Step 1–11），第二轮完整功能（Step 12–14）

**2. 联调接口优先级**

| 优先级 | 接口 | 说明 |
|--------|------|------|
| P0 | `GET /api/workflows` | 工作流列表，验证基础连通性 |
| P0 | `GET /api/workflows/:id` | 加载工作流详情 |
| P0 | `POST /api/workflows/:id/run` | 触发运行 |
| P0 | `WebSocket /ws/run/:run_id` | 实时事件流 |
| P1 | `PUT /api/workflows/:id` | 保存工作流 |
| P1 | `POST /api/workflows` | 创建新工作流 |
| P1 | `DELETE /api/workflows/:id` | 删除工作流 |
| P1 | `GET /api/models` | 模型列表 |
| P2 | `POST /api/runs/:id/cancel` | 取消运行 |
| P2 | `GET /api/runs/:id/diff` | Human-in-the-Loop diff |
| P2 | `POST /api/runs/:id/approve` / `reject` | 审批操作 |

**3. WebSocket 联调特殊注意事项**

- **事件格式对齐**：前端 `StreamEvent` 类型必须与后端实际推送的 JSON 结构逐一比对，特别注意 `node_id`、`timestamp` 字段是否一致
- **重连测试**：模拟网络断开（DevTools Offline），验证前端是否能正确重连并恢复状态
- **事件顺序**：验证 `run_started` → 节点事件 → `run_completed/failed` 的完整生命周期
- **并发多节点**：多节点同时运行时，验证 `node_id` 映射是否正确，事件是否交错正确
- **手动断开**：运行结束后前端应主动断开 WebSocket，不再重连

**4. 预估联调工作量**

- 核心流程联调（P0 接口）：1 天
- 完整功能联调（P1–P2 接口）：1 天
- 总计：2 天

---

## Mock 模式（前端独立开发）

在 api.ts 中添加 MOCK_MODE 开关：
- mockWorkflows：3 个示例
- mockModels：10 个模型
- mockStreamEvents：模拟一次完整运行的 50 个事件序列

---

## UI/UX 要点

1. 节点组件必须 `memo()` 包裹
2. Monaco 懒加载，折叠时不挂载
3. Xterm 卸载时 dispose() 防内存泄漏
4. 运行中节点用 pulse 动画
5. 空画布显示引导文字
6. 最小支持 1280px 宽度
7. Ctrl+S 保存，Ctrl+Enter 运行

---

*文档版本：v1.1*
*创建时间：2026-05-07*
*状态：已按总指挥审查意见修改*
