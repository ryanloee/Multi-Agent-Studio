# 工作流统一架构设计

---

## 1. 核心洞察：工作流就是工作流，不分模式

**之前的错误**：把"自动规划"和"手动连线"分成两个完全独立的模式，两套执行逻辑。

**正确的理解**：工作流就是 DAG（节点 + 边），区别只是**谁定义了这个 DAG**：

```
                    谁定义 DAG？
                        │
            ┌───────────┴───────────┐
            │                       │
        用户在画布上拖拽          Planner 运行时生成
        (手动模式)               (自动模式)
            │                       │
            └───────────┬───────────┘
                        │
                  同一个执行引擎
                  (数据传递 + 文件继承)
```

**Planner 生成的计划也可以有依赖关系**，不一定是扁平的并行任务：

```
Planner 输出的计划:
  Task 1 [explore] ──→ Task 2 [coder] ──→ Task 3 [review] ──→ Task 4 [test]
                          │
                          └──→ Task 5 [coder] ──→ Task 3 [review]

这和用户手动画出来的 Coder→Reviewer→Tester 是同一种 DAG！
执行引擎应该用同一套逻辑处理。
```

---

## 2. 统一的工作流模型

### 2.1 工作流 = 节点 + 边

不管是手动还是自动，最终产物都是同一个东西：

```typescript
interface Workflow {
  nodes: Node[];          // 节点列表
  edges: Edge[];          // 边列表（数据依赖）
  // 元信息（不影响执行）
  author: "user" | "planner";  // 谁创建的
  goal?: string;               // 原始目标（自动模式才有）
}
```

### 2.2 边的含义统一

不管是用户画的边还是 Planner 生成的边，含义完全一样：

| 边的含义 | 说明 |
|---------|------|
| **执行顺序** | A→B 意味着 A 完成后才能执行 B |
| **数据传递** | A 的输出摘要注入 B 的 prompt |
| **文件继承** | B 复用 A 的 sandbox，能看到 A 的文件改动 |
| **升级通道** | B 遇到困难时可以向上游 A 求助 |

### 2.3 两个关键升级

当前引擎缺失的能力，**两种模式都需要**：

| 能力 | 自动模式需要 | 手动模式需要 |
|------|:----------:|:----------:|
| 上游输出注入下游 prompt | ✅ Planner→Worker 已硬编码 | ❌ 完全缺失 |
| 同链路复用 sandbox | ❌ Worker 并行所以各自独立 | ❌ 完全缺失 |
| 串行 Worker 复用 sandbox | ✅ Planner 可输出串行依赖 | ✅ 画布上的 A→B→C |
| 升级协议 (Worker→上游) | ✅ 已有 | ❌ 缺失（应支持 B→A） |

---

## 3. Planner 的进化：从"扁平列表"到"结构化 DAG"

### 3.1 当前 Planner 的输出（扁平）

```
Planner 输出 → parse_plan_output() → [task1, task2, task3, ...]
                                          │
                                          └──→ 全部并行执行
```

**问题**：所有子任务并行执行，没有依赖关系。实际上很多任务是有先后顺序的。

### 3.2 Planner 应该输出的（结构化 DAG）

```json
{
  "tasks": [
    {
      "id": "explore_1",
      "type": "explore",
      "prompt": "分析现有代码结构",
      "depends_on": []
    },
    {
      "id": "coder_register",
      "type": "coder",
      "prompt": "实现注册 API",
      "depends_on": ["explore_1"]
    },
    {
      "id": "coder_login",
      "type": "coder",
      "prompt": "实现登录 API",
      "depends_on": ["explore_1"]
    },
    {
      "id": "review_1",
      "type": "review",
      "prompt": "审查代码质量",
      "depends_on": ["coder_register", "coder_login"]
    },
    {
      "id": "test_1",
      "type": "shell",
      "prompt": "运行测试",
      "depends_on": ["review_1"]
    }
  ]
}
```

**产生的 DAG**：

```
        explore_1
       /         \
  coder_register  coder_login
       \         /
        review_1
           |
         test_1
```

这和用户手动画出来的 DAG 结构完全一样！

### 3.3 Planner Prompt 升级

当前 Planner 的 PLAN_SUFFIX：

```
Format your plan as:
## Plan
1. [agent_type: coder] <task description>
   Prompt: <detailed prompt>
```

升级后：

```
Format your plan as JSON:
```json
{
  "tasks": [
    {
      "id": "step_1",
      "type": "coder",
      "prompt": "...",
      "depends_on": []
    },
    {
      "id": "step_2",
      "type": "review",
      "prompt": "...",
      "depends_on": ["step_1"]
    }
  ]
}
```

Use depends_on to specify which tasks must complete before this one starts.
Tasks without dependencies run in parallel.
```

### 3.4 plan_parser.py 升级

当前解析策略只支持扁平列表。升级后：
- 新增第 5 种解析策略：**结构化 JSON DAG**（含 `depends_on` 字段）
- 解析后动态构建 edges：`depends_on` 中每个 ID → 当前 task ID
- 传入统一执行引擎

---

## 4. 统一执行引擎

### 4.1 核心原则

**一条边就是一条数据通道**，不管这条边是谁画的：

```
A ──→ B

执行时:
1. A 先执行
2. A 完成后:
   - A 的 result_summary 注入 B 的 prompt（数据传递）
   - B 复用 A 的 sandbox（文件继承）
   - B 执行前 git commit A 的改动（可回滚）
3. B 执行
```

### 4.2 执行流程

```python
async def _execute_workflow(self, run_id, nodes, edges, global_config, cancel_event):
    """统一工作流执行引擎 — 处理任何来源的 DAG。"""

    # 1. 拓扑排序（复用 compile_dag）
    layers = compile_dag({"nodes": nodes, "edges": edges})

    # 2. 逐层执行
    layer_results = {}
    sandbox_map = {}     # node_id → sandbox_id
    commit_map = {}      # node_id → git commit hash

    for layer in layers:
        # 同层节点可并行，但同层间也可能共享 sandbox
        # 简化策略：同层节点各自执行，按需共享
        for node in layer:
            node_id = node["id"]

            # --- Sandbox 策略 ---
            # 找上游：edges 中 target == node_id 的所有 source
            upstream_ids = [e["source"] for e in edges if e["target"] == node_id]

            if upstream_ids and len(upstream_ids) == 1:
                # 单上游：直接复用上游 sandbox
                sandbox_id = sandbox_map.get(upstream_ids[0])
                if not sandbox_id:
                    sandbox_id = await self._sandbox.create(f"ws-{node_id}")
            elif upstream_ids and len(upstream_ids) > 1:
                # 多上游：复用主上游 sandbox + 在 prompt 中注入所有上游摘要
                # 主上游 = edges 列表中最后一条边的 source
                primary = upstream_ids[-1]
                sandbox_id = sandbox_map.get(primary)
                if not sandbox_id:
                    sandbox_id = await self._sandbox.create(f"ws-{node_id}")
                # TODO: 可选 — 将其他上游的文件 git merge 进来
            else:
                # 无上游：创建新 sandbox
                sandbox_id = await self._sandbox.create(f"ws-{node_id}")

            sandbox_map[node_id] = sandbox_id

            # --- 数据传递 ---
            upstream_context = self._build_upstream_context(node_id, edges, layer_results)

            # --- 执行节点 ---
            result = await self._execute_node(
                run_id, node, layer_results, global_config, cancel_event,
                sandbox_id=sandbox_id,
                upstream_context=upstream_context,
            )
            layer_results[node_id] = result

            # --- Git checkpoint ---
            try:
                commit_hash = await self._checkpoint.auto_commit(
                    sandbox_id, f"after [{node_id}]"
                )
                commit_map[node_id] = commit_hash
            except Exception:
                pass

            # --- 检查是否是 Plan 节点 ---
            if node.get("agent_type") == "plan" and result.get("state") == "completed":
                # Plan 节点：解析输出，生成子 DAG，递归执行
                child_results = await self._execute_dynamic_plan(
                    run_id, node_id, result, global_config, cancel_event,
                    planner_node=node,
                )
                layer_results.update(child_results)
```

### 4.3 `_build_upstream_context()` 实现

```python
def _build_upstream_context(self, node_id, edges, layer_results):
    """构建上游输出摘要，注入当前节点的 prompt。"""
    upstream_edges = [e for e in edges if e["target"] == node_id]
    if not upstream_edges:
        return ""

    parts = ["\n\n## 上游节点输出\n"]
    for edge in upstream_edges:
        source_id = edge["source"]
        result = layer_results.get(source_id, {})
        if not result:
            continue

        # 优先用 result_summary，否则截取 raw_output
        summary = result.get("result_summary", "")
        if summary:
            parts.append(f"### {source_id}\n{summary}\n")
        else:
            raw = result.get("raw_output", "")
            if raw:
                # 提取 LLM 文本（最后 2000 字符）
                text = self._extract_llm_text(raw)
                parts.append(f"### {source_id}\n{text[-2000:]}\n")

    return "\n".join(parts)
```

---

## 5. Planner 生成 DAG 的例子

### 场景："实现用户注册登录系统"

**Planner 输入**：`实现一个用户注册登录系统`

**Planner 输出**（结构化 DAG）：

```json
{
  "tasks": [
    {
      "id": "explore_1",
      "type": "explore",
      "prompt": "分析项目现有代码结构，了解技术栈和目录组织",
      "depends_on": []
    },
    {
      "id": "coder_register",
      "type": "coder",
      "prompt": "基于探索结果实现用户注册 API，包含参数校验和密码加密",
      "depends_on": ["explore_1"]
    },
    {
      "id": "coder_login",
      "type": "coder",
      "prompt": "基于探索结果实现用户登录 API，包含 JWT token 生成",
      "depends_on": ["explore_1"]
    },
    {
      "id": "review_1",
      "type": "review",
      "prompt": "审查注册和登录的代码质量，检查安全性和错误处理",
      "depends_on": ["coder_register", "coder_login"]
    },
    {
      "id": "test_1",
      "type": "shell",
      "prompt": "运行 pytest 执行注册和登录的集成测试",
      "depends_on": ["review_1"]
    }
  ]
}
```

**生成的 DAG**（和手动画的一样）：

```
     explore_1
    /          \
coder_register  coder_login
    \          /
     review_1
        |
      test_1
```

**执行过程**：
1. `explore_1` 执行（新 sandbox）
2. `coder_register` 和 `coder_login` 并行执行（各自复用 explore_1 的 sandbox 副本？还是共享？）
   - **关键决策**：并行节点**不能共享 sandbox**（会冲突），各自独立 sandbox
   - 但 prompt 中都注入了 `explore_1` 的输出摘要
3. `review_1` 执行（复用 `coder_login` 的 sandbox，prompt 注入两个 coder 的摘要）
4. `test_1` 执行（复用 `review_1` 的 sandbox）

---

## 6. 并行节点的 Sandbox 策略

这是唯一需要特殊处理的地方：

| 场景 | Sandbox 策略 | 原因 |
|------|------------|------|
| A→B（串行） | B 复用 A 的 sandbox | B 需要 A 的文件改动 |
| A→B, A→C（一分多） | B 和 C 各自独立 sandbox，但 prompt 都注入 A 的摘要 | 并行写同一文件会冲突 |
| B→D, C→D（多合一） | D 复用主上游 sandbox，prompt 注入所有上游摘要 | 需要所有上游的上下文 |
| A→B→C（链式） | C 复用 B 复用 A 的 sandbox | 文件变更逐级累积 |

### 并行节点的文件同步

当 `explore_1` 同时被 `coder_register` 和 `coder_login` 依赖时：

```
方案 1: 独立 sandbox + prompt 注入摘要（当前选择，简单可靠）
  explore_1 sandbox ──(复制)──→ coder_register sandbox
  explore_1 sandbox ──(复制)──→ coder_login sandbox
  各自独立，不会冲突

方案 2: 共享 sandbox + 文件锁（未来优化，更高效）
  explore_1 sandbox ←── coder_register (锁文件写)
  explore_1 sandbox ←── coder_login   (锁文件写)
  需要 merge 机制，复杂度高
```

**当前选择方案 1**：并行节点独立 sandbox（从上游 sandbox 复制一份），串行节点复用同一 sandbox。

---

## 7. 两种"创作模式"的 UI 差异

虽然执行引擎统一了，但**创作体验**仍然不同：

### 7.1 手动创作模式

```
┌──────────────────────────────────────────────────────────┐
│  用户在画布上拖拽节点、连线                                 │
│                                                           │
│  🧩 节点栏: Planner | Coder | Reviewer | Tester | ...     │
│                                                           │
│  🗺️ 画布: 可编辑，拖拽、连线、配置                         │
│                                                           │
│  📐 连线 = 数据依赖 + 文件通道                              │
│     用户可配置: 是否传递文件、传递摘要还是完整输出             │
│                                                           │
│  🎯 适合: 固定流程、CI/CD、模板                            │
└──────────────────────────────────────────────────────────┘
```

### 7.2 自动创作模式

```
┌──────────────────────────────────────────────────────────┐
│  用户只输入目标，Planner 生成 DAG                           │
│                                                           │
│  💬 输入: "实现用户注册登录系统"                             │
│                                                           │
│  🗺️ 画布: 运行后自动展示 Planner 生成的 DAG（只读）         │
│     - 节点自动出现                                         │
│     - 边自动出现（Planner 定义了 depends_on）               │
│     - 用户可查看但不可编辑                                  │
│                                                           │
│  📋 任务板: 和手动模式共享同一个任务板组件                   │
│                                                           │
│  🎯 适合: 需求开发、探索性任务                              │
└──────────────────────────────────────────────────────────┘
```

### 7.3 混合模式（未来）

用户先画一个基本框架，Planner 在框架内填充细节：

```
用户画: Coder ──→ Reviewer ──→ Tester
          │
          └──→ Planner 补充: "Coder 内部应该拆成 3 个子任务"

最终: Coder_register ──→ Reviewer ──→ Tester
      Coder_login   ──↗
      Coder_ui      ──↗
```

---

## 8. 前端组件变化

### 8.1 统一组件，条件显示

| 组件 | 手动模式 | 自动模式 |
|------|---------|---------|
| `LeftPanel.tsx` | 节点栏 + 任务板 | **只显示任务板** |
| `FlowCanvas.tsx` | 可编辑 | 只读（运行后展示 Planner 生成的 DAG） |
| `Toolbar.tsx` | 保存 + 运行 | 运行（目标输入在画布区） |
| `ConfigPanel.tsx` | 完整配置 | 只展示（不可编辑） |
| `TaskBoard.tsx` | ✅ 共享 | ✅ 共享 |
| 新增: `GoalInput.tsx` | 不显示 | ✅ 核心组件 |

### 8.2 工作流列表页入口

```
┌──────────────────────────────────────────────┐
│  创建新工作流                                  │
│                                               │
│  ┌──────────────────────────────────────┐    │
│  │  🤖 自动规划                          │    │
│  │  描述目标，Planner 自动构建工作流      │    │
│  │  [开始]                              │    │
│  └──────────────────────────────────────┘    │
│                                               │
│  ┌──────────────────────────────────────┐    │
│  │  📐 手动工作流                        │    │
│  │  在画布上设计节点和连线               │    │
│  │  [创建空白画布]                      │    │
│  └──────────────────────────────────────┘    │
└──────────────────────────────────────────────┘
```

---

## 9. 数据模型

### 9.1 Workflow 表

```sql
ALTER TABLE workflows ADD COLUMN mode TEXT DEFAULT 'manual';
-- 'auto' | 'manual'
ALTER TABLE workflows ADD COLUMN goal TEXT DEFAULT '';
-- 自动模式的目标描述
```

### 9.2 Edge 新增属性

| 属性 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `transfer_files` | boolean | `true` | 是否传递文件（复用 sandbox） |
| `transfer_summary` | boolean | `true` | 是否注入上游输出摘要 |
| `transfer_format` | string | `"summary"` | `"summary"` / `"full"` / `"diff"` |

---

## 10. 实施步骤

### Phase 1: 统一执行引擎（3 天）

1. `_execute_node()` 支持 `sandbox_id` 和 `upstream_context` 参数
2. 新增 `_build_upstream_context()`
3. 修改 `_execute_dag()` 实现数据传递 + sandbox 复用
4. 每个节点执行后 `git commit`

### Phase 2: Planner 输出升级（2 天）

1. 修改 Planner prompt：支持 `depends_on` 字段
2. 修改 `plan_parser.py`：新增结构化 DAG 解析策略
3. `_execute_dynamic_plan()` 从 `depends_on` 构建 edges
4. 生成的子 DAG 走统一执行引擎

### Phase 3: 后端模式分流（1 天）

1. `compiler.py` 新增 `detect_mode()`
2. `runs.py` 传递 mode + edges + goal
3. 自动模式：先执行 Planner → 生成 DAG → 走统一引擎
4. 手动模式：直接走统一引擎

### Phase 4: 前端适配（2 天）

1. 工作流列表页：两种入口
2. 自动模式：隐藏节点栏，显示 GoalInput
3. 手动模式：正常显示
4. 画布根据模式切换可编辑/只读

---

## 11. 测试方案

| 测试项 | 方法 | 框架 |
|--------|------|------|
| 手动模式数据传递 | A→B，A 输出 "result_x"，B 的 prompt 含 "result_x" | pytest |
| 手动模式文件继承 | A 创建 `hello.py`，B 复用 sandbox 能读到 | pytest |
| 自动模式 Planner 生成 DAG | 传入 goal，验证 Planner 输出含 `depends_on` | pytest |
| 自动模式串行任务 | Planner 输出 A→B→C，验证 B 能看到 A 的文件 | pytest |
| 并行节点独立 sandbox | A→B, A→C，验证 B 和 C 不会文件冲突 | pytest |
| 多上游摘要注入 | A→C, B→C，C 的 prompt 含 A 和 B 的摘要 | pytest |
| git 回滚 | B 失败后 git revert 回到 A 的状态 | pytest |
| 升级协议 | 手动模式 B→A 求助，验证 A 能回复 | pytest |
| 前端自动模式 | 创建自动工作流，验证无节点栏 | 手动 |
| 前端手动模式 | 创建手动工作流，验证有节点栏和可编辑画布 | 手动 |
| 混合场景 | 手动画 A→B，B 是 Planner，验证 Planner 生成的子 DAG 也走统一引擎 | 手动 |

---

## 12. Agent 节点系统提示词与权限控制设计

### 12.1 设计原则

1. **系统提示词 = 不可编辑的身份定义**。每个节点类型有一段内置的系统级提示词，定义"你是谁、你能做什么、你不能做什么"。用户提供的 prompt 是**任务指令**，追加在系统提示词之后，不能覆盖系统提示词。
2. **权限 = 工具可用性 + 数据访问范围 + 操作约束**。每个节点类型只能看到和使用被授权的工具，且工具的参数和行为受权限边界约束。
3. **最小权限原则**。默认只授予完成任务所需的最小权限。例如 Explorer 只能读不能写，Reviewer 可以写审查意见但不能执行 shell。

### 12.2 Prompt 结构

```
┌─────────────────────────────────────────────┐
│ 系统提示词（不可编辑，内置）                    │
│   - 身份定义                                  │
│   - 核心职责                                  │
│   - 权限边界                                  │
│   - 行为约束                                  │
│   - 输出格式要求                               │
├─────────────────────────────────────────────┤
│ 上游上下文（自动注入）                          │
│   - 上游节点的输出摘要                          │
│   - 工作流元信息（任务 ID、节点 ID 等）          │
├─────────────────────────────────────────────┤
│ 用户指令（可编辑）                             │
│   - 用户在配置面板中输入的具体任务描述            │
└─────────────────────────────────────────────┘
```

### 12.3 各节点类型的系统提示词

---

#### 🤖 Planner（规划器）

**身份**：团队的项目经理，负责分析需求、拆解任务、分配资源。

**系统提示词**：

```
你是一个项目管理规划器（Planner）。你是团队的核心决策者。

## 你的职责
1. 分析用户需求或上游传来的任务
2. 将复杂任务拆解为可执行的子任务
3. 为每个子任务指定最合适的执行者类型（coder/review/shell/explore）
4. 定义子任务之间的执行依赖关系
5. 在 Worker 遇到困难时提供指导

## 你的权限边界
- 你可以读取项目文件，但不应该直接修改代码
- 你可以创建和编辑规划文件（如 TODO.md、plan.md）
- 你不能执行 shell 命令（除 git status 等只读命令）
- 你不能直接审查或测试代码

## 输出格式
你必须以结构化 JSON 输出你的计划：
```json
{
  "tasks": [
    {
      "id": "step_1",
      "type": "explore|coder|review|shell",
      "prompt": "具体的任务描述",
      "depends_on": ["上游任务ID"]
    }
  ]
}
```

## 重要约束
- 每个子任务的 prompt 必须足够具体，包含完整上下文
- depends_on 必须准确反映执行依赖，无依赖的任务会并行执行
- 不要创建超过 8 个子任务，保持计划聚焦
- 如果需求不明确，使用 ESCALATE_TO_PLANNER 向用户确认
- 优先创建串行依赖链，而非全并行，确保代码质量
```

**权限矩阵**：

| 工具 | 权限 | 约束 |
|------|------|------|
| glob | ✅ 允许 | 无 |
| grep | ✅ 允许 | 无 |
| read | ✅ 允许 | 无 |
| edit | ✅ 允许 | 只能编辑 `.md` / `TODO` / `plan` 文件 |
| write | ✅ 允许 | 只能写 `.md` / `.txt` / `plan.json` 文件 |
| shell | ✅ 允许 | 只允许只读命令：`git status`, `git log`, `ls`, `cat`, `find` |
| apply_patch | ❌ 禁止 | — |

---

#### 💻 Coder（编码器）

**身份**：团队的程序员，负责编写和修改代码。

**系统提示词**：

```
你是一个专业程序员（Coder）。你是团队的代码实现者。

## 你的职责
1. 根据任务描述或上游传来的需求编写代码
2. 修改现有代码实现功能变更或 bug 修复
3. 遵循项目已有的代码风格和架构约定
4. 编写清晰、可维护的代码，包含必要的注释
5. 确保代码可编译/运行，无语法错误

## 你的权限边界
- 你可以自由读写项目代码文件
- 你可以执行构建、测试、安装等 shell 命令
- 你不能删除 .git 目录或修改版本控制配置
- 你不能修改 CI/CD 配置文件（需由 Shell 节点处理）
- 你不能执行危险命令：rm -rf /、格式化磁盘等

## 代码编写规范
1. 修改代码前先阅读现有代码，理解上下文
2. 使用 edit 工具做精确修改，而非 write 覆写整个文件
3. 每次修改聚焦于一个明确的变更点
4. 如果任务涉及多个文件，按依赖顺序依次修改
5. 修改后运行简单验证（如语法检查）确保不出错

## 重要约束
- 不要重构不相关的代码，只做任务要求的变更
- 不要引入任务未要求的新依赖
- 遇到不确定的需求，使用 ESCALATE_TO_PLANNER 向上游确认
- 报告进度：使用 TASK_PROGRESS: <0-100> 标记
```

**权限矩阵**：

| 工具 | 权限 | 约束 |
|------|------|------|
| glob | ✅ 允许 | 无 |
| grep | ✅ 允许 | 无 |
| read | ✅ 允许 | 无 |
| edit | ✅ 允许 | 不能编辑 `.git/`、`.env`（需确认） |
| write | ✅ 允许 | 不能写 `.git/`、`.env`（需确认） |
| shell | ✅ 允许 | `rm -rf`、`format`、`del /s` 等危险命令需确认 |
| apply_patch | ✅ 允许 | 无 |

---

#### 🔍 Explorer（探索器）

**身份**：团队的调研员，负责搜索和收集信息，只读不写。

**系统提示词**：

```
你是一个代码调研员（Explorer）。你是团队的信息收集者。

## 你的职责
1. 搜索和阅读项目代码，理解架构和逻辑
2. 查找特定功能的实现位置
3. 收集相关文件和代码片段供下游节点参考
4. 分析依赖关系和调用链
5. 汇总信息并输出结构化的分析报告

## 你的权限边界 — 严格只读
- 你只能读取文件，不能修改任何文件
- 你只能搜索代码，不能执行任何 shell 命令
- 你不能创建、编辑、删除任何文件
- 你的输出是分析报告，不是代码改动

## 输出规范
1. 汇总关键发现，按文件/模块组织
2. 列出相关文件路径和关键代码行
3. 说明各模块之间的调用关系
4. 标出需要注意的技术债务或风险点

## 重要约束
- 绝对不要修改任何文件，即使你认为可以改进
- 不要执行 shell 命令，即使只是为了"快速验证"
- 如果发现需要修改代码的问题，在报告中标注，交给 Coder 处理
- 保持客观，只报告事实，不做主观评价
```

**权限矩阵**：

| 工具 | 权限 | 约束 |
|------|------|------|
| glob | ✅ 允许 | 无 |
| grep | ✅ 允许 | 无 |
| read | ✅ 允许 | 无 |
| edit | ❌ 禁止 | — |
| write | ❌ 禁止 | — |
| shell | ❌ 禁止 | — |
| apply_patch | ❌ 禁止 | — |

---

#### 📋 Reviewer（审查器）

**身份**：团队的代码审查员，负责审查代码质量和安全性。

**系统提示词**：

```
你是一个代码审查员（Reviewer）。你是团队的代码质量把关者。

## 你的职责
1. 审查上游节点的代码改动，评估质量
2. 检查代码是否符合项目规范和最佳实践
3. 发现潜在的 bug、安全漏洞和性能问题
4. 确认代码是否正确实现了需求
5. 提出修改建议并可直接修复小问题

## 你的权限边界
- 你可以读取所有项目代码
- 你可以编辑代码文件（修复审查中发现的小问题）
- 你不能执行 shell 命令（测试由 Tester 或 Shell 节点负责）
- 你不能大规模重写代码（只做审查建议和微调）
- 你不能修改构建配置、CI/CD 文件

## 审查标准
1. **正确性**：代码是否正确实现了需求？有没有逻辑错误？
2. **安全性**：有没有 SQL 注入、XSS、敏感信息泄露？
3. **可维护性**：代码是否清晰？命名是否合理？有没有过度复杂？
4. **性能**：有没有明显的性能问题？N+1 查询？内存泄漏？
5. **规范**：是否符合项目代码风格？有没有未处理的异常？

## 输出规范
1. 逐条列出发现的问题（严重/建议/风格三个等级）
2. 对每个问题给出具体的修改建议
3. 如果修改简单（<10 行），直接用 edit 工具修复
4. 如果修改复杂，在输出中描述修改方案，交由 Coder 实现

## 重要约束
- 审查要全面但不过度挑剔，区分严重问题和风格偏好
- 不要重写上游的代码，除非有明确的 bug
- 不要添加上游没有要求的功能
- 遇到需求不明确时，使用 ESCALATE_TO_PLANNER 向上游确认
```

**权限矩阵**：

| 工具 | 权限 | 约束 |
|------|------|------|
| glob | ✅ 允许 | 无 |
| grep | ✅ 允许 | 无 |
| read | ✅ 允许 | 无 |
| edit | ✅ 允许 | 每次编辑不超过 10 行变更（微调修复） |
| write | ❌ 禁止 | — |
| shell | ❌ 禁止 | — |
| apply_patch | ❌ 禁止 | — |

---

#### ⌨️ Shell（执行器）

**身份**：团队的运维/执行员，负责运行命令和脚本。

**系统提示词**：

```
你是一个命令执行员（Shell）。你是团队的运维执行者。

## 你的职责
1. 执行构建、测试、部署等 shell 命令
2. 安装依赖包和配置环境
3. 运行测试套件并报告结果
4. 执行 Git 操作（提交、推送等）
5. 处理文件系统操作（移动、重命名、权限等）

## 你的权限边界
- 你可以执行任意 shell 命令
- 你可以读写文件
- 你不能修改代码逻辑（这是 Coder 的职责）
- 你不能修改代码审查意见（这是 Reviewer 的职责）
- 危险命令（删除、格式化、强制推送）需要确认

## 执行规范
1. 先检查当前环境状态（pwd、git status）
2. 每次执行一个明确的命令，观察输出后再继续
3. 命令失败时分析错误原因，不要盲目重试
4. 记录所有执行的命令和关键输出
5. 长时间运行的命令设置合理的超时

## 重要约束
- 不要执行 `rm -rf /`、`git push --force` 等不可逆操作
- 不要在生产环境执行未经验证的命令
- 不要修改代码文件的逻辑内容（只做文件操作和命令执行）
- 遇到环境问题时，使用 ESCALATE_TO_PLANNER 报告
- 报告进度：使用 TASK_PROGRESS: <0-100> 标记
```

**权限矩阵**：

| 工具 | 权限 | 约束 |
|------|------|------|
| glob | ✅ 允许 | 无 |
| grep | ✅ 允许 | 无 |
| read | ✅ 允许 | 无 |
| edit | ✅ 允许 | 只能编辑配置文件（`.yml`、`.json`、`.toml`、`.env`） |
| write | ✅ 允许 | 不能写代码文件（`.py`、`.ts`、`.js` 等） |
| shell | ✅ 允许 | `rm -rf`、`git push --force` 等需确认 |
| apply_patch | ❌ 禁止 | — |

---

#### 👤 Human（人工审批）

**身份**：团队中的人类决策者，在关键节点进行审批和决策。

**系统提示词**：

```
你是一个人工审批节点（Human-in-the-Loop）。

## 你的职责
1. 等待上游节点完成任务
2. 审查上游的输出和文件变更
3. 做出批准/拒绝/修改的决策
4. 如有修改意见，反馈给上游节点

## 你的权限边界
- 你没有工具，不能直接操作代码
- 你只能查看上游传来的摘要信息
- 你的决策通过"批准"或"拒绝"按钮表达
- 拒绝时可以附上修改意见

## 决策指引
- 批准：如果上游输出符合预期
- 拒绝：如果上游输出不符合需求，附上具体修改意见
- 修改：如果只需小幅调整，可以直接提出修改指令
```

**权限矩阵**：

| 工具 | 权限 | 约束 |
|------|------|------|
| glob | ❌ 禁止 | — |
| grep | ❌ 禁止 | — |
| read | ❌ 禁止 | — |
| edit | ❌ 禁止 | — |
| write | ❌ 禁止 | — |
| shell | ❌ 禁止 | — |
| apply_patch | ❌ 禁止 | — |

> Human 节点通过前端 UI 的审批按钮交互，不需要工具。

---

### 12.4 权限总览矩阵

| 工具 | Planner | Coder | Explorer | Reviewer | Shell | Human |
|------|:-------:|:-----:|:--------:|:--------:|:-----:|:-----:|
| glob | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ |
| grep | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ |
| read | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ |
| edit | ✅ ¹ | ✅ | ❌ | ✅ ² | ✅ ³ | ❌ |
| write | ✅ ¹ | ✅ | ❌ | ❌ | ✅ ³ | ❌ |
| shell | ✅ ⁴ | ✅ | ❌ | ❌ | ✅ | ❌ |
| apply_patch | ❌ | ✅ | ❌ | ❌ | ❌ | ❌ |

**约束说明**：
- ¹ Planner 只能写/编辑 `.md`、`.txt`、`plan.json` 等规划文件
- ² Reviewer 的 edit 每次不超过 10 行变更
- ³ Shell 的 edit/write 只能操作配置文件，不能写代码文件
- ⁴ Planner 的 shell 只允许只读命令（git status、ls、cat 等）

### 12.5 升级协议权限

| 节点类型 | 可以升级到 | 升级场景 |
|---------|----------|---------|
| Coder | 直接上游（Planner 或另一个 Coder） | 需求不明确、技术选型不确定 |
| Reviewer | 直接上游（Coder 或 Planner） | 发现重大问题需要确认修复方向 |
| Shell | 直接上游（Coder 或 Planner） | 环境问题、命令执行失败需指导 |
| Explorer | 直接上游（Planner） | 找不到关键信息、需要更具体的搜索方向 |
| Planner | 无（Planner 是最高决策者） | — |
| Human | 无（Human 本身就是决策输出） | — |

### 12.6 数据访问范围

| 节点类型 | 可读上游数据 | 可见文件范围 |
|---------|------------|-----------|
| Planner | 所有上游节点的完整输出 | 全部（只读） |
| Coder | 所有上游节点的输出摘要 + Planner 的指导 | 全部（读写） |
| Explorer | 仅任务描述（不需要上游输出） | 全部（只读） |
| Reviewer | 直接上游的输出摘要 + 上游的 git diff | 全部（只读 + 微调） |
| Shell | 直接上游的输出摘要 | 全部（读写，但不写代码文件） |
| Human | 直接上游的输出摘要 + git diff | 通过前端 UI 查看文件变更 |

### 12.7 实现方式

**系统提示词与用户指令的拼接**（在 `load_prompt()` 中）：

```python
def load_prompt(agent_type: str, **kwargs: Any) -> str:
    """构建完整 prompt：系统提示词 + 工作目录 + 上游上下文 + 用户指令"""
    
    # 1. 系统提示词（不可编辑）
    system_prompt = SYSTEM_PROMPTS.get(agent_type, DEFAULT_SYSTEM)
    
    # 2. 工作目录信息
    workspace = kwargs.get("workspace", "/workspace")
    workspace_info = f"\n\nWorking directory: {workspace}"
    
    # 3. 上游上下文（由引擎注入）
    upstream_context = kwargs.get("upstream_context", "")
    
    # 4. 用户指令（可编辑）
    user_prompt = kwargs.get("user_prompt", "")
    
    parts = [system_prompt, workspace_info]
    if upstream_context:
        parts.append(upstream_context)
    if user_prompt:
        parts.append(f"\n\n## 任务指令\n{user_prompt}")
    
    return "\n".join(parts)
```

**权限强制执行**（在 `ToolRegistry.for_agent_type()` + 工具执行时）：

```python
class ToolRegistry:
    @classmethod
    def for_agent_type(cls, agent_type: str) -> list[dict]:
        """返回该节点类型可用的工具 API schemas。"""
        if agent_type == "human":
            return []
        
        schemas = []
        for tool in cls._tools.values():
            if tool.allowed_agent_types is None or agent_type in tool.allowed_agent_types:
                schemas.append(tool.to_api_schema())
        return schemas

    @classmethod
    def validate_execution(cls, agent_type: str, tool_name: str, arguments: dict) -> list[str]:
        """验证工具执行是否在权限范围内，返回警告列表。"""
        warnings = []
        tool = cls.get(tool_name)
        if not tool:
            return [f"未知工具: {tool_name}"]
        
        if tool.allowed_agent_types and agent_type not in tool.allowed_agent_types:
            return [f"权限拒绝: {agent_type} 无权使用 {tool_name}"]
        
        # 额外的参数级权限检查
        if tool_name == "edit" and agent_type == "reviewer":
            # Reviewer edit 限制：检查变更行数
            old_text = arguments.get("old_text", "")
            new_text = arguments.get("new_text", "")
            if abs(len(new_text) - len(old_text)) > 200:  # 约 10 行
                warnings.append("Reviewer 单次 edit 不应超过 10 行变更")
        
        if tool_name == "write" and agent_type == "planner":
            # Planner write 限制：只能写规划文件
            path = arguments.get("path", "")
            valid_exts = (".md", ".txt", ".json")
            if not any(path.endswith(ext) for ext in valid_exts):
                warnings.append(f"Planner 只能写规划文件 (.md/.txt/.json)，不能写: {path}")
        
        return warnings
```

### 12.8 测试方案

| 测试项 | 方法 | 框架 |
|--------|------|------|
| Explorer 无法使用 write | 调用 `for_agent_type("explore")`，验证不包含 write/edit/shell | pytest |
| Reviewer edit 行数限制 | Reviewer 尝试 edit 超过 10 行，验证返回警告 | pytest |
| Planner shell 只读限制 | Planner 尝试执行 `npm install`，验证返回警告 | pytest |
| Human 无工具 | 调用 `for_agent_type("human")`，验证返回空列表 | pytest |
| 系统提示词不可覆盖 | 验证 `load_prompt()` 输出始终以系统提示词开头 | pytest |
| 用户指令追加在系统提示词之后 | 传入 user_prompt，验证顺序正确 | pytest |
| 升级协议权限 | Coder 向非上游节点升级，验证被拒绝 | pytest |
| Shell 不能写代码文件 | Shell 尝试 write `main.py`，验证返回警告 | pytest |
