# MAS Agent 对标 OpenCode 改进计划

> 基于与 OpenCode v1.14.41 的对比分析，制定分阶段实施计划。
> 每个子任务包含：功能描述、实现方案、测试方法、测试框架、验收标准。

---

## 阶段总览

| 阶段 | 目标 | 子任务数 | 预估总工时 |
|------|------|---------|-----------|
| P0 — 紧急 | 补齐核心工具，解决基本可用性 | 3 | 3 天 |
| P1 — 重要 | 安全与健壮性，解决生产可用性 | 5 | 7 天 |
| P2 — 增强 | 体验与扩展性，对齐 OpenCode 主要能力 | 5 | 10 天 |

---

## P0 — 紧急：核心工具补齐

### T01: `grep` 内容搜索工具

**功能**：在 workspace 中按正则表达式搜索文件内容，对标 OpenCode 的 `grep` 工具。

**实现方案**：
- 新建 `apps/agent/mas_agent/tools/grep_tool.py`
- 使用 Python `subprocess` 调用 `rg` (ripgrep)，如果不可用则回退到 `grep -rn`
- 参数：`pattern`(必填)、`path`(可选，默认 workspace 根)、`glob`(可选，文件过滤)、`context`(可选，上下文行数，默认 2)
- 输出格式：`{file}:{line}:{content}`，带行号和文件名
- 结果限制：最多 50 个匹配、每个匹配最多 3 行上下文
- 超时保护：5 秒超时

**参考**：OpenCode `packages/opencode/src/tool/grep.ts` — 基于 ripgrep，输出带行号

**测试**：
| 测试项 | 方法 | 框架 |
|--------|------|------|
| 基本正则搜索 | 创建含已知内容的临时文件，搜索验证 | pytest |
| 无匹配结果 | 搜索不存在的模式，验证返回友好提示 | pytest |
| glob 过滤 | 搜索时限定 `*.py`，验证只返回 Python 文件 | pytest |
| 上下文行数 | 验证 context 参数控制输出行数 | pytest |
| 结果数量限制 | 创建 100+ 匹配的文件，验证截断提示 | pytest |
| ripgrep 不可用回退 | mock `shutil.which` 返回 None，验证回退到 grep | pytest |
| 超长行截断 | 创建单行 10000 字符的文件，验证输出截断 | pytest |
| 集成测试 | 在真实项目中运行 `python -m mas_agent`，验证 grep 工具可被 LLM 调用 | 手动 |

**验收标准**：
- `grep "def main" --glob="*.py"` 能正确搜索 Python 文件中的函数定义
- 工具注册到 ToolRegistry，LLM 可自动调用
- 无 ripgrep 时自动回退到 grep

---

### T02: `edit` 搜索替换工具

**功能**：搜索文件中的文本片段并替换为新内容，避免整文件覆写。对标 OpenCode 的 `edit` 工具。

**实现方案**：
- 新建 `apps/agent/mas_agent/tools/edit_tool.py`
- 参数：`path`(必填)、`old_text`(必填)、`new_text`(必填)
- 替换策略（多层降级，参考 OpenCode 的 10 层容错）：
  1. **精确匹配**：`old_text` 在文件中完全匹配
  2. **空白归一化**：将连续空白压缩为单个空格后再匹配
  3. **缩进灵活**：忽略行首空白差异（4空格 vs tab）
  4. **模糊匹配**：使用 `difflib.SequenceMatcher` 计算相似度，阈值 0.8
- 匹配到多个位置时：全部替换，并在返回中提示替换了几处
- 匹配失败时：返回当前文件内容的前 20 行，帮助 LLM 重新定位
- 并发保护：使用文件级锁（`threading.Lock` 字典）
- 替换后自动去除末尾多余空行

**参考**：OpenCode `packages/opencode/src/tool/edit.ts` — 10 层降级 + Levenshtein + 文件锁

**测试**：
| 测试项 | 方法 | 框架 |
|--------|------|------|
| 精确替换 | 创建文件，精确 old→new 替换，验证结果 | pytest |
| 多处替换 | 同一 old_text 出现多次，验证全部替换 | pytest |
| 空白归一化 | old_text 有多余空格，验证仍可匹配 | pytest |
| 缩进灵活 | old_text 用 tab，文件用 4 空格，验证匹配 | pytest |
| 模糊匹配 | old_text 有轻微拼写差异，验证相似度阈值匹配 | pytest |
| 匹配失败 | old_text 不在文件中，验证返回友好错误 + 文件前 20 行 | pytest |
| 文件不存在 | 编辑不存在的文件，验证报错 | pytest |
| 并发保护 | 两个协程同时编辑同一文件，验证不会交错 | pytest |
| 行号信息 | 验证返回中包含被替换的行号范围 | pytest |
| 集成测试 | LLM 调用 edit 工具修改代码，验证结果正确 | 手动 |

**验收标准**：
- `edit("main.py", "def hello():", "def hello(name):")` 能精确替换
- old_text 有轻微格式差异时仍能匹配（空白/缩进）
- 匹配失败时返回文件前 20 行辅助 LLM 定位
- 不会出现文件内容丢失或交错写入

---

### T03: 工具注册表按 Agent 类型过滤

**功能**：不同 Agent 类型只能使用对应的工具集，对标 OpenCode 的 `ToolRegistry.for_agent_type()` 细粒度过滤。

**实现方案**：
- 修改 `apps/agent/mas_agent/tools/__init__.py`
- 为每个 Tool 添加 `allowed_agent_types` 属性（`None` 表示所有类型可用）
- 工具分配：
  - `plan`: glob, read, grep, edit, write, shell
  - `coder`: glob, read, grep, edit, write, shell
  - `explore`: glob, read, grep（只读）
  - `review`: glob, read, grep, edit（仅写评论文件）
  - `shell`: glob, read, grep, shell
  - `human`: 无工具
- 修改 `ToolRegistry.for_agent_type()` 实现

**测试**：
| 测试项 | 方法 | 框架 |
|--------|------|------|
| explore 只读 | 验证 explore agent 不返回 write/shell/edit 工具 | pytest |
| human 无工具 | 验证 human agent 返回空工具列表 | pytest |
| coder 全量 | 验证 coder agent 返回所有工具 | pytest |
| 默认行为 | 未指定 allowed_agent_types 的工具对所有类型可用 | pytest |

**验收标准**：
- `ToolRegistry.for_agent_type("explore")` 不包含 write/shell
- LLM 收到的工具列表与 agent 类型一致

---

## P1 — 重要：安全与健壮性

### T04: 权限系统

**功能**：工具执行前检查权限，高危操作需确认。对标 OpenCode 的 allow/deny/ask 三级权限。

**实现方案**：
- 新建 `apps/agent/mas_agent/permission.py`
- 权限模型：
  ```python
  class PermissionAction(Enum):
      ALLOW = "allow"
      DENY = "deny"
      ASK = "ask"    # 需要外部确认

  class PermissionRule:
      permission: str    # "shell", "edit", "write", "read"
      pattern: str       # glob 模式，如 "*.env", "rm *"
      action: PermissionAction
  ```
- 默认规则集（内置，可通过配置文件覆盖）：
  - `shell` + `rm *` → ASK
  - `shell` + `*:outside_workspace` → DENY
  - `write` + `.env` → ASK
  - `write` + `*.lock` → DENY
  - 其余 → ALLOW
- 权限检查接口：`check(permission, target) -> PermissionAction`
- ASK 处理：写入 `stream.jsonl` 发出 `permission_request` 事件，等待编排器回复
- 编排器收到后通过 WebSocket 通知前端，用户确认后写入 `permission_response` 文件
- Agent 轮询等待响应（超时 5 分钟默认 DENY）

**参考**：OpenCode `packages/opencode/src/permission/index.ts` — 规则评估 + Deferred 等待

**测试**：
| 测试项 | 方法 | 框架 |
|--------|------|------|
| ALLOW 规则 | 普通文件写入，验证直接通过 | pytest |
| DENY 规则 | 写入 .lock 文件，验证被拒绝 | pytest |
| ASK 规则 | 写入 .env 文件，验证等待确认 | pytest |
| 通配符匹配 | 验证 `rm *` 匹配 `rm -rf /tmp/test` | pytest |
| 默认 ALLOW | 无匹配规则时默认允许 | pytest |
| ASK 超时 | 模拟无人响应，5 分钟后默认 DENY | pytest |
| 集成测试 | 在编排器中触发 ASK，前端弹确认框，验证流程 | 手动 |

**验收标准**：
- `shell "rm -rf /"` 触发 ASK 确认
- `.env` 文件写入触发 ASK 确认
- `.lock` 文件写入被直接 DENY
- 普通操作无感知，不影响正常流程

---

### T05: Doom Loop 检测

**功能**：检测 Agent 陷入重复工具调用循环，防止 token 浪费。对标 OpenCode 的连续 3 次相同调用检测。

**实现方案**：
- 修改 `apps/agent/mas_agent/loop.py` 的 `AgentLoop.run()`
- 维护最近 N 次工具调用的历史 `list[tuple[name, args_hash]]`
- 每次工具调用前检查：如果最近 3 次调用完全相同（工具名 + 参数哈希），则：
  - 发出 `doom_loop_detected` 事件
  - 注入系统消息提醒 LLM："你已连续 3 次调用相同工具，请尝试不同方法"
  - 如果第 5 次仍然相同，终止循环

**测试**：
| 测试项 | 方法 | 框架 |
|--------|------|------|
| 正常循环 | 3 次不同工具调用，验证无触发 | pytest |
| 3 次相同 | 模拟 3 次相同 grep 调用，验证警告注入 | pytest |
| 5 次相同 | 模拟 5 次相同调用，验证循环终止 | pytest |
| 参数略有不同 | 3 次 grep 但 pattern 不同，验证不触发 | pytest |

**验收标准**：
- 连续 3 次相同工具调用时注入警告消息
- 连续 5 次时终止循环，返回 exit code 1

---

### T06: 上下文压缩 (Compaction)

**功能**：当消息历史接近上下文窗口时自动压缩，保留关键信息。对标 OpenCode 的自动 compaction。

**实现方案**：
- 新建 `apps/agent/mas_agent/compaction.py`
- 压缩策略：
  1. 估算当前 messages 的 token 数（简单启发式：字符数 / 4）
  2. 当超过 `max_tokens * 0.7` 时触发压缩
  3. 保留最近 2 轮完整对话
  4. 早期对话压缩为结构化摘要，由 LLM 生成：
     ```
     ## Context Summary
     - Goal: {原始任务}
     - Progress: {已完成的步骤}
     - In Progress: {当前正在做的事}
     - Blocked: {遇到的障碍}
     ```
  5. 工具输出截断为 2000 字符（保留前 500 + 后 500 + 中间省略标记）
- 修改 `AgentLoop.run()`：每轮工具调用后检查是否需要压缩

**参考**：OpenCode `packages/opencode/src/session/compaction.ts`

**测试**：
| 测试项 | 方法 | 框架 |
|--------|------|------|
| 压缩触发 | 构造超长消息列表，验证自动压缩 | pytest |
| 最近轮次保留 | 验证最近 2 轮对话完整保留 | pytest |
| 摘要生成 | 验证摘要包含 Goal/Progress/Blocked 结构 | pytest |
| 工具输出截断 | 10000 字符的工具输出被截断为 2000 字符 | pytest |
| 未超限不压缩 | 消息较少时验证不触发压缩 | pytest |
| 集成测试 | 长对话中触发压缩，验证后续 LLM 调用仍正常 | 手动 |

**验收标准**：
- 长对话不再因超 token 限制而失败
- 压缩后 Agent 仍能理解上下文继续工作
- 最近 2 轮对话完整保留

---

### T07: 工具输出智能截断

**功能**：大输出存文件返回引用，而非硬截断丢失信息。对标 OpenCode 的智能输出处理。

**实现方案**：
- 新建 `apps/agent/mas_agent/tools/output_utils.py`
- `truncate_output(content, max_chars=5000, workspace=None, label="output")` 函数：
  - 如果 `len(content) <= max_chars`，直接返回
  - 否则：将完整内容写入 `{workspace}/.agent/outputs/{label}_{timestamp}.txt`
  - 返回：前 500 字符 + `\n... (truncated, full output saved to {path})\n` + 后 500 字符
- 修改所有工具的返回值处理：`read`、`shell`、`grep` 的输出都通过此函数

**参考**：OpenCode `packages/opencode/src/tool/shell.ts` — 大输出存文件 + 返回尾部

**测试**：
| 测试项 | 方法 | 框架 |
|--------|------|------|
| 短输出直通 | 100 字符输出，验证原样返回 | pytest |
| 长输出截断 | 10000 字符输出，验证截断 + 保存到文件 | pytest |
| 文件可读 | 验证保存的文件内容完整可读 | pytest |
| 返回格式 | 验证包含前 500 + 路径提示 + 后 500 | pytest |
| 多次调用不覆盖 | 验证时间戳命名不覆盖之前的输出 | pytest |

**验收标准**：
- 工具输出 > 5000 字符时自动存文件
- LLM 仍能看到输出的头尾关键部分
- 完整输出可通过路径重新读取

---

### T08: 工具调用自动修复

**功能**：修复 LLM 常见的工具调用格式错误，提高成功率。对标 OpenCode 的 `experimental_repairToolCall`。

**实现方案**：
- 新建 `apps/agent/mas_agent/tool_repair.py`
- 修复策略：
  1. **工具名大小写修复**：LLM 输出 `Grep` → 修正为 `grep`；`ReadFile` → `read`
  2. **参数名修复**：`file_path` → `path`；`query` → `pattern`；`content` → `new_text`
  3. **JSON 修复**：截断的 JSON 尝试补全；多余逗号删除；单引号→双引号
  4. **类型转换**：字符串数字 → 实际数字（offset: "10" → 10）
- 修改 `AgentLoop._execute_tool()`：执行前调用修复函数
- 修复日志通过 `stream.emit_tool_call()` 记录原始和修复后的参数

**参考**：OpenCode `packages/opencode/src/session/llm.ts` — `experimental_repairToolCall`

**测试**：
| 测试项 | 方法 | 框架 |
|--------|------|------|
| 工具名大小写 | 输入 `Grep`，验证修正为 `grep` | pytest |
| 参数名别名 | 输入 `file_path`，验证修正为 `path` | pytest |
| 截断 JSON | 输入 `{"path": "main.py",`，验证尝试补全 | pytest |
| 多余逗号 | 输入 `{"path": "a",}`，验证删除尾逗号 | pytest |
| 无效工具名 | 输入 `nonexistent_tool`，验证返回错误 | pytest |
| 无需修复 | 正常参数，验证不做修改 | pytest |

**验收标准**：
- 常见的大小写、参数名错误能自动修复
- 修复记录写入事件流，可追溯
- 无需修复时不影响正常流程

---

## P2 — 增强：体验与扩展性

### T09: `read` 工具增强

**功能**：增强文件读取工具，添加分页提示、行范围显示、编码检测。对标 OpenCode 的 `read` 工具。

**实现方案**：
- 修改 `apps/agent/mas_agent/tools/read_tool.py`
- 增强：
  1. **分页提示**：底部显示 `Lines {start}-{end} of {total}. Use offset={end+1} to read more.`
  2. **行范围参数**：`start_line` / `end_line`（替代 offset/limit，更直观）
  3. **编码检测**：尝试 UTF-8，失败后尝试 GBK/Shift-JIS/Latin-1
  4. **BOM 处理**：自动跳过 UTF-8 BOM
  5. **大文件预警**：超过 1000 行时提示 `Large file, showing first 500 lines`
  6. **二进制文件检测**：检测到 \0 字符时返回 `Binary file, cannot display`

**参考**：OpenCode `packages/opencode/src/tool/read.ts`

**测试**：
| 测试项 | 方法 | 框架 |
|--------|------|------|
| 分页提示 | 读取 1000 行文件的前 500 行，验证底部提示 | pytest |
| 编码检测 | 创建 GBK 编码文件，验证自动检测 | pytest |
| BOM 处理 | 创建含 UTF-8 BOM 的文件，验证不显示 BOM 字符 | pytest |
| 二进制检测 | 读取 .exe 文件前几字节，验证二进制提示 | pytest |
| 行范围 | 使用 start_line=10, end_line=20，验证只返回对应行 | pytest |

**验收标准**：
- 大文件读取时 LLM 知道还有更多内容
- 中文 GBK 文件能正常读取
- 二进制文件不会输出乱码

---

### T10: `apply_patch` 工具

**功能**：应用 unified diff 格式补丁，适用于 GPT 系列模型。对标 OpenCode 的 `apply_patch` 工具。

**实现方案**：
- 新建 `apps/agent/mas_agent/tools/apply_patch_tool.py`
- 参数：`patch`(必填，unified diff 格式文本)
- 解析 unified diff：
  - 提取文件名（`--- a/file.py` / `+++ b/file.py`）
  - 提取 hunk 块（`@@ -start,count +start,count @@`）
  - 逐个 hunk 应用到目标文件
- 容错：hunk 行号不匹配时搜索上下文定位
- 修改 `ToolRegistry.for_agent_type()`：当 `agent_type` 使用 GPT 模型时，只提供 `apply_patch`，禁用 `edit`/`write`

**参考**：OpenCode `packages/opencode/src/tool/apply_patch.ts`

**测试**：
| 测试项 | 方法 | 框架 |
|--------|------|------|
| 基本补丁 | 创建文件，应用单 hunk 补丁 | pytest |
| 多 hunk | 一个补丁中多个 hunk，验证全部应用 | pytest |
| 多文件 | 一个补丁包含多文件修改 | pytest |
| 行号偏移 | hunk 行号与实际不匹配但上下文正确，验证仍可应用 | pytest |
| 无效补丁 | 格式错误的补丁，验证返回错误 | pytest |
| 新建文件 | `--- /dev/null` + `+++ b/new.py`，验证创建新文件 | pytest |

**验收标准**：
- 标准 unified diff 能正确应用
- GPT 模型自动使用 `apply_patch` 替代 `edit`

---

### T11: 配置系统

**功能**：支持 JSON 配置文件自定义 Agent 行为，对标 OpenCode 的多级配置。

**实现方案**：
- 新建 `apps/agent/mas_agent/config.py`
- 配置来源优先级：
  1. 项目级 `mas.json`（当前目录）
  2. 用户级 `~/.mas/config.json`
  3. 环境变量 `MAS_` 前缀
  4. CLI 参数
  5. 内置默认值
- 可配置项：
  ```json
  {
    "max_turns": 50,
    "max_tokens": 4096,
    "shell_timeout": 120,
    "permissions": [
      {"permission": "shell", "pattern": "rm *", "action": "ask"},
      {"permission": "write", "pattern": ".env", "action": "ask"}
    ],
    "agents": {
      "coder": {"model": "claude-3.5-sonnet", "steps": 30},
      "explore": {"model": "claude-3.5-haiku"}
    },
    "tools": {
      "disabled": ["apply_patch"],
      "custom": []
    }
  }
  ```
- 修改 `cli.py`：启动时加载配置，合并到 LoopConfig

**参考**：OpenCode `packages/opencode/src/config/config.ts` — 多级配置 + JSON Schema

**测试**：
| 测试项 | 方法 | 框架 |
|--------|------|------|
| 项目配置 | 创建 mas.json，验证配置被加载 | pytest |
| 用户配置 | mock home 目录，验证用户级配置加载 | pytest |
| 优先级 | 项目和用户都配置 max_turns，验证项目级优先 | pytest |
| 环境变量 | 设置 MAS_MAX_TURNS=30，验证覆盖默认值 | pytest |
| 无配置 | 无任何配置文件，验证使用内置默认值 | pytest |
| 无效 JSON | 损坏的 mas.json，验证优雅降级到默认值 | pytest |

**验收标准**：
- `mas.json` 能覆盖默认 Agent 行为
- 配置错误时不崩溃，使用默认值
- CLI 参数优先级最高

---

### T12: LLM Provider 增强

**功能**：修复增量 JSON 解析的健壮性问题，添加重试机制。对标 OpenCode 的 LLM 层。

**实现方案**：
- 修改 `apps/agent/mas_agent/providers/anthropic_provider.py`
- 增量 JSON 解析修复：
  - 当前实现用字符串拼接解析 `input_json_delta`，对复杂嵌套 JSON 会失败
  - 改用缓冲策略：先累积所有 partial_json 字符串，在 `content_block_stop` 时一次性 `json.loads()`
- 重试机制：
  - 网络错误自动重试最多 3 次，指数退避（1s, 2s, 4s）
  - 5xx 服务端错误重试，4xx 不重试
  - 每次重试前检查是否已被取消
- 超时改进：
  - 连接超时 15s，读取超时 120s，总超时 300s
  - 使用 `httpx.Timeout(connect=15, read=120, write=30, pool=15)`

**参考**：OpenCode `packages/opencode/src/session/llm.ts` — SessionRetry + ProviderTransform

**测试**：
| 测试项 | 方法 | 框架 |
|--------|------|------|
| 增量 JSON | 模拟多段 partial_json，验证累积后正确解析 | pytest |
| 嵌套 JSON | 工具参数含嵌套对象，验证正确解析 | pytest |
| 网络重试 | mock httpx 抛出 ConnectError，验证重试 3 次 | pytest |
| 5xx 重试 | mock 503 响应，验证自动重试 | pytest |
| 4xx 不重试 | mock 400 响应，验证不重试直接报错 | pytest |
| 超时 | mock 长时间无响应，验证超时触发 | pytest |

**验收标准**：
- 复杂工具参数（含嵌套 JSON）不再解析失败
- 网络抖动时自动重试，不直接崩溃
- 超时后优雅退出，不挂死

---

### T13: 快照与回滚增强

**功能**：每步操作前自动 git commit，支持精确回滚。对标 OpenCode 的 Snapshot 系统。

**实现方案**：
- 修改编排器 `apps/orchestrator/app/sandbox/checkpoint.py`
- 当前 `auto_commit` 在节点执行前提交一次，粒度太粗
- 增强：在 Agent 每次执行 `edit`/`write`/`shell` 工具前也自动提交
- 实现方式：工具执行前通过 stream.jsonl 发出 `pre_tool_commit` 事件，编排器收到后执行 git commit
- 或更简单：在 `edit_tool.py` / `write_tool.py` 中直接调用 `git add -A && git commit`
- 回滚 API：`POST /runs/{run_id}/rollback` — 回滚到指定步骤
- diff API：`GET /runs/{run_id}/diff` — 获取指定步骤的文件变更

**参考**：OpenCode `packages/opencode/src/snapshot/index.ts` — 每步 git commit + diff + revert

**测试**：
| 测试项 | 方法 | 框架 |
|--------|------|------|
| 自动提交 | Agent 修改文件，验证 git log 有新提交 | pytest |
| 提交信息 | 验证提交信息包含工具名和文件路径 | pytest |
| 回滚 | 执行 rollback API，验证文件恢复到之前状态 | pytest |
| diff | 执行 diff API，验证返回正确的文件差异 | pytest |
| 多步回滚 | 3 次修改后回滚到第 1 次，验证正确恢复 | pytest |

**验收标准**：
- 每次文件修改都有对应的 git commit
- 可通过 API 回滚到任意步骤
- diff API 返回精确的文件变更

---

## 测试基础设施

### 框架选型

| 层级 | 框架 | 用途 |
|------|------|------|
| Agent 单元测试 | **pytest** | 工具、权限、压缩等模块测试 |
| Agent 集成测试 | **pytest** + 手动 | 完整 LLM 循环测试 |
| 编排器测试 | **pytest** + **httpx.AsyncClient** | API 端点测试 |
| 前端 E2E | **Playwright** | 用户交互流程测试（已有 e2e_test.py 可参考） |
| Mock LLM | **pytest-httpx** 或自定义 Mock | 模拟 LLM 响应 |

### 测试目录结构

```
apps/agent/tests/
├── test_grep_tool.py
├── test_edit_tool.py
├── test_apply_patch_tool.py
├── test_read_tool.py
├── test_permission.py
├── test_compaction.py
├── test_output_utils.py
├── test_tool_repair.py
├── test_config.py
├── test_provider.py
├── test_doom_loop.py
└── conftest.py          # 共享 fixtures (临时 workspace, mock provider)
```

### conftest.py 核心内容

```python
import pytest
import tempfile
import os

@pytest.fixture
def workspace(tmp_path):
    """创建临时 workspace 目录。"""
    return str(tmp_path)

@pytest.fixture
def sample_file(workspace):
    """在 workspace 中创建示例文件。"""
    path = os.path.join(workspace, "sample.py")
    with open(path, "w") as f:
        f.write("def hello():\n    print('hello')\n")
    return path
```

---

## 实施依赖图

```
T01 grep ─────────┐
T02 edit ──────────┤
T03 工具过滤 ──────┤──→ T04 权限系统 ──→ T13 快照回滚
                   │
T05 Doom Loop ────┤
T07 输出截断 ─────┤
T08 工具修复 ─────┤
                   ├──→ T06 上下文压缩
T09 read 增强 ────┤
T10 apply_patch ──┤
T12 Provider ─────┤
T11 配置系统 ─────┘
```

- T01/T02/T03 是前置依赖，必须先完成
- T04 权限系统需要 T01/T02/T03 中的工具就绪后才能测试
- T06 上下文压缩依赖 T07 输出截断
- T13 快照回滚依赖 T04 权限系统

---

## 文件变更总览

| 新建文件 | 对应任务 |
|---------|---------|
| `apps/agent/mas_agent/tools/grep_tool.py` | T01 |
| `apps/agent/mas_agent/tools/edit_tool.py` | T02 |
| `apps/agent/mas_agent/permission.py` | T04 |
| `apps/agent/mas_agent/compaction.py` | T06 |
| `apps/agent/mas_agent/tools/output_utils.py` | T07 |
| `apps/agent/mas_agent/tool_repair.py` | T08 |
| `apps/agent/mas_agent/tools/apply_patch_tool.py` | T10 |
| `apps/agent/mas_agent/config.py` | T11 |
| `apps/agent/tests/conftest.py` | 全部 |
| `apps/agent/tests/test_*.py` (12 个) | 对应任务 |

| 修改文件 | 对应任务 |
|---------|---------|
| `apps/agent/mas_agent/tools/__init__.py` | T01, T02, T03 |
| `apps/agent/mas_agent/loop.py` | T05, T06, T07, T08 |
| `apps/agent/mas_agent/tools/read_tool.py` | T07, T09 |
| `apps/agent/mas_agent/tools/shell_tool.py` | T07 |
| `apps/agent/mas_agent/tools/write_tool.py` | T07 |
| `apps/agent/mas_agent/providers/anthropic_provider.py` | T12 |
| `apps/agent/mas_agent/cli.py` | T11 |
| `apps/agent/mas_agent/prompts/__init__.py` | T02 (edit 工具说明) |
