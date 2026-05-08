# OpenCode CLI 验证报告

> 状态：已通过实际 Docker 验证
> 验证方式：沙盒镜像构建 + 容器内 CLI 测试 + 实际输出分析
> 验证时间：2026-05-08
> OpenCode 版本：1.14.41

---

## 1. 沙盒镜像构建状态

- **Dockerfile 路径**: `infra/sandbox-images/base/Dockerfile`
- **构建状态**: 成功
- **镜像大小**: 2.35GB (DISK), 585MB (CONTENT)
- **构建命令**:
  ```bash
  docker build -t multi-agent-studio/sandbox-base:latest d:/codex/agent/mat/infra/sandbox-images/base/
  ```

### 构建过程中发现并修复的 BUG

1. **缺少 `unzip` 依赖**：Bun 安装脚本需要 `unzip`，但原始 Dockerfile 未安装
   - 修复：在 `apt-get install` 中添加 `unzip`
2. **npm 包名错误**：`bun install -g opencode` 返回 404，正确包名是 `opencode-ai`
   - 修复：改为 `bun install -g opencode-ai@latest`
   - 仓库已迁移到 `anomalyco/opencode`（原 `sst/opencode`）

### 已安装软件

| 软件 | 来源 | 备注 |
|------|------|------|
| ubuntu:22.04 | 基础镜像 | |
| git, curl, wget | apt | |
| build-essential, gcc, g++ | apt | C/C++ 编译工具链 |
| python3, python3-pip, python3-venv | apt | Python 运行时 |
| nodejs, npm | apt | |
| unzip | apt | **新增** - Bun 安装依赖 |
| bun 1.3.13 | curl installer | OpenCode 运行时 |
| opencode 1.14.41 | `bun install -g opencode-ai@latest` | **包名已修正** |

### 目录结构

- `/workspace` - Agent 工作目录（WORKDIR）
- `/workspace/.workflow` - 节点间上下文共享
- `/workspace/.opencode` - OpenCode 输出目录
- `/sandbox-meta/.git` - Git 元数据（checkpoint 管理）
- `/root/.opencode` - OpenCode 配置目录

---

## 2. OpenCode CLI 实际参数（已验证）

### 顶层命令（`opencode --help`）

```
Commands:
  opencode completion          generate shell completion script
  opencode acp                 start ACP (Agent Client Protocol) server
  opencode mcp                 manage MCP (Model Context Protocol) servers
  opencode [project]           start opencode tui                    [default]
  opencode attach <url>        attach to a running opencode server
  opencode run [message..]     run opencode with a message
  opencode debug               debugging and troubleshooting tools
  opencode providers           manage AI providers and credentials   [aliases: auth]
  opencode agent               manage agents
  opencode upgrade [target]    upgrade opencode to the latest or a specific version
  opencode uninstall           uninstall opencode and remove all related files
  opencode serve               starts a headless opencode server
  opencode web                 start opencode server and open web interface
  opencode models [provider]   list all available models
  opencode stats               show token usage and cost statistics
  opencode export [sessionID]  export session data as JSON
  opencode import <file>       import session data from JSON file or URL
  opencode github              manage GitHub agent
  opencode pr <number>         fetch and checkout a GitHub PR branch, then run opencode
  opencode session             manage sessions
  opencode plugin <module>     install plugin and update config       [aliases: plug]
  opencode db                  database tools
```

### 顶层选项

```
Options:
  -h, --help         show help
  -v, --version      show version number
      --print-logs   print logs to stderr
      --log-level    log level [DEBUG, INFO, WARN, ERROR]
      --pure         run without external plugins
      --port         port to listen on (default: 0)
      --hostname     hostname to listen on (default: 127.0.0.1)
      --mdns         enable mDNS service discovery
      --mdns-domain  custom domain name for mDNS (default: opencode.local)
      --cors         additional domains to allow for CORS
  -m, --model        model to use in provider/model format
  -c, --continue     continue the last session
  -s, --session      session id to continue
      --fork         fork the session when continuing
      --prompt       prompt to use
      --agent        agent to use
```

### `opencode run` 子命令（**核心命令** -- 替代之前假设的 `task`）

```
opencode run [message..]

Positionals:
  message  message to send                                [array] [default: []]

Options:
  -h, --help
  -v, --version
      --print-logs
      --log-level
      --pure
      --command                       the command to run, use message for args
  -c, --continue                      continue the last session
  -s, --session                       session id to continue
      --fork                          fork the session before continuing
      --share                         share the session
  -m, --model                         model (provider/model format)
      --agent                         agent to use
      --format                        format: default (formatted) or json (raw JSON events)
                                          [choices: "default", "json"] [default: "default"]
  -f, --file                          file(s) to attach to message           [array]
      --title                         title for the session
      --attach                        attach to a running opencode server
  -p, --password                      basic auth password
  -u, --username                      basic auth username
      --dir                           directory to run in
      --port                          port for the local server
      --variant                       model variant (reasoning effort)
      --thinking                      show thinking blocks       [default: false]
      --dangerously-skip-permissions  auto-approve permissions   [default: false]
```

### `opencode serve` 子命令（无头服务器模式）

```
opencode serve

Options:
  --port         port to listen on (default: 0)
  --hostname     hostname (default: 127.0.0.1)
  --mdns         enable mDNS discovery
  --mdns-domain  mDNS domain (default: opencode.local)
  --cors         CORS domains
```

### 可用 Agent 列表（`opencode agent list`）

| Agent | 类型 | 说明 |
|-------|------|------|
| build | primary | 主构建 agent，完整读写权限 |
| compaction | primary | 上下文压缩 agent |
| explore | subagent | 探索/搜索 agent（只读 + grep/glob/bash/webfetch） |
| general | subagent | 通用 agent（受限，无 todowrite） |
| plan | primary | 规划 agent（只写 .opencode/plans/*.md） |
| summary | primary | 摘要 agent（完全只读） |
| title | primary | 标题生成 agent（完全只读） |

---

## 3. 关键发现：与之前代码假设的重大差异

### 3.1 没有 `task` 子命令

**之前代码假设**：`opencode task --agent <type> --model <provider/id> --prompt '<prompt>' --log-format jsonl --log-file <path>`

**实际情况**：
- 没有 `task` 子命令
- 正确命令是 `opencode run`
- 没有 `--log-format jsonl` 参数
- 没有 `--log-file <path>` 参数
- 输出格式控制通过 `--format json`（不是 `--log-format jsonl`）
- prompt 通过位置参数传递（不是 `--prompt`）

### 3.2 正确命令模板

```bash
# 单次运行（JSON 输出到 stdout）
opencode run --agent build --model anthropic/claude-sonnet-4-20250514 --format json "say hello"

# 无头服务器模式（长驻运行，通过 API 交互）
opencode serve --port 4096 --hostname 0.0.0.0
```

### 3.3 输出格式

`--format json` 输出原始 JSON 事件流（不是 JSONL，而是 JSON stream）。
`--format default` 输出格式化的终端文本。

### 3.4 对模块 4 的影响

| 之前假设 | 实际情况 | 需要修改 |
|---------|---------|---------|
| `opencode task` | `opencode run` | 命令模板 |
| `--prompt '<text>'` | 位置参数 `[message..]` | 命令模板 |
| `--log-format jsonl` | `--format json` | 命令模板 + Parser |
| `--log-file <path>` | 无此参数，输出到 stdout | 需要 stdout 重定向 |
| `stdbuf -o0 opencode` | 可能不需要（JSON stream） | 缓冲策略 |
| `--agent build` | `--agent build` | 一致，无需修改 |
| `--model provider/id` | `-m provider/model` | 短参数名 `-m` |

---

## 4. 推荐的命令模板

### 方案 A：`opencode run` + stdout 重定向（推荐）

```python
def _build_command(self, task_input: dict) -> str:
    prompt = self.config.prompt.format(**task_input)
    return (
        f"cd /workspace && "
        f"opencode run "
        f"--agent {self.config.agent_type} "
        f"-m {self.config.model_provider}/{self.config.model_id} "
        f"--format json "
        f"--dangerously-skip-permissions "
        f"'{prompt}' "
        f"> {self.STREAM_FILE} 2>&1"
    )
```

优点：
- 最简单，直接运行后等待完成
- `--format json` 输出结构化 JSON 事件流
- 无需 `stdbuf`（JSON stream 应该是行缓冲的）

缺点：
- 需要验证 stdout 重定向是否实时写入文件
- 可能需要 `--dangerously-skip-permissions` 避免交互式权限确认

### 方案 B：`opencode serve` + HTTP API

```python
# 1. 启动服务器
opencode serve --port 4096 --hostname 0.0.0.0

# 2. 通过 HTTP API 发送任务
# （需要进一步研究 serve 模式的 API 文档）
```

优点：
- 服务器长驻，可复用
- 通过 HTTP API 交互，更适合编排

缺点：
- 需要研究 serve API
- 更复杂的状态管理

---

## 5. 基础设施验证结果

| 服务 | 状态 | 端口 | 备注 |
|------|------|------|------|
| PostgreSQL | 运行中 | 5432 | healthy |
| Redis | 运行中 | 6379 | healthy |
| Temporal Server | 运行中 | 7233 | 需要正确配置 DB=postgres12 + POSTGRES_SEEDS |
| Temporal UI | 运行中 | 8088 (host) -> 8080 (container) | HTTP 200 |
| MinIO API | 运行中 | 19000 (host) -> 9000 (container) | HTTP 200 |
| MinIO Console | 运行中 | 19001 (host) -> 9001 (container) | HTTP 200 |

### docker-compose.yml 修复记录

1. 移除了废弃的 `version: "3.8"`
2. Temporal DB driver: `DB=postgresql` -> `DB=postgres12`
3. Temporal 环境变量: `DB_HOST=postgres` -> `POSTGRES_SEEDS=postgres`
4. Temporal 凭据: 需要使用 `POSTGRES_USER` + `POSTGRES_PWD`（不是 `DB_USER`/`DB_PASSWORD`）
5. 添加了 PostgreSQL init.sql 创建 temporal 和 temporal_visibility 数据库
6. MinIO 端口: `9000:9000` -> `19000:9000`（避免端口冲突）
7. 移除了 Temporal 的 `./temporal/config` volume mount（目录不存在）

---

## 6. 后端 Python 环境验证

| 依赖 | 版本 | 状态 |
|------|------|------|
| Python | 3.12.10 | OK |
| FastAPI | 0.115.14 | OK |
| TemporalIO | 1.27.0 | OK |
| Redis | 5.3.1 | OK |
| SQLAlchemy | 2.0.49 | OK |
| Docker | 7.1.0 | OK |
| Pydantic | 2.13.4 | OK |

- `poetry install --no-root`: 成功（需 --no-root 因为缺少 README.md）
- `from app.main import app`: FastAPI app loaded OK

### 注意事项

1. Poetry 安装后需要添加到 PATH: `.../Python312/Scripts`
2. 首次 `poetry install` 可能因虚拟环境损坏需要 `poetry env remove --all` 后重建
3. `pyproject.toml` 中 `readme = "README.md"` 但文件不存在，需创建或移除该行

---

## 7. 待办事项

- [x] 构建沙盒镜像
- [x] 运行 `opencode --help` 记录完整参数列表
- [x] 运行 `opencode run --help` 记录子命令参数
- [x] 验证 agent 列表
- [x] 验证基础设施服务
- [x] 验证后端 Python 环境
- [ ] 测试 `opencode run --format json` 的实际 JSON 输出格式
- [ ] 记录 `type` 字段所有实际值（需 API key）
- [ ] 验证 `--format json` 输出的实时性（行缓冲 vs 全缓冲）
- [ ] 确认最终命令模板
- [ ] 更新 `opencode.py` 的 `_build_command()` 方法
- [ ] 更新 `parser.py` 的 `_map_type()` 映射
- [ ] 创建 README.md 或移除 pyproject.toml 中的 readme 配置

---

*报告版本: v1.0（实际 Docker 验证完成）*
*验证时间: 2026-05-08*
*验证者: 后端引擎组*
