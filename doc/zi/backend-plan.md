# 后端引擎组 — Phase 0 详细实施计划

> 负责人：后端引擎组
> 审查状态：待总指挥审查
> 预估总工作量：约 24 小时（3 个工作日）

---

## 现有代码 Bug 清单（实施前必须修复）

### BUG-1: checkpoint.py 调用约定错误（严重）
- **文件**：`apps/orchestrator/app/sandbox/checkpoint.py`
- **问题**：`auto_commit`、`rollback`、`get_diff` 中使用 `cmd(sandbox_id, ...)` 但 `cmd` 是 `_git_cmd` 的引用，而 `_git_cmd` 不接受 `sandbox_id` 参数
- **修复**：所有调用改为 `self.sandbox.exec(sandbox_id, self._git_cmd("..."))`

### BUG-2: Dockerfile 中 stdbuf 不是独立包（严重）
- **文件**：`infra/sandbox-images/base/Dockerfile`
- **问题**：`apt-get install -y stdbuf` — stdbuf 是 coreutils 的一部分，Ubuntu 22.04 上不是独立包，构建会失败
- **修复**：从 apt-get install 列表中移除 stdbuf

### BUG-3: docker-compose.yml Temporal 配置卷为空（严重）
- **文件**：`infra/docker-compose.yml`
- **问题**：`temporal_data:/etc/temporal/config/dynamicconfig` 使用命名卷，内容为空
- **修复**：改为绑定挂载 `./temporal/config:/etc/temporal/config/dynamicconfig`，并创建 `development.yaml`

### BUG-4: file_watcher.py 每次读取整个文件（性能）
- **文件**：`apps/orchestrator/app/streaming/file_watcher.py`
- **问题**：每次迭代通过 `read_file` 读取整个 stream.jsonl，文件增长到 50MB 时极其低效
- **修复**：改用 `exec("tail -n +{line_offset} {file_path}")` 只读新增行

### BUG-5: FileWatcher 未集成 Throttler（功能缺失）
- **文件**：`apps/orchestrator/app/streaming/file_watcher.py`
- **问题**：shell_stdout 事件直接发布，未经过 100ms 节流
- **修复**：在 FileWatcher 中集成 StreamThrottler

---

## 实施模块与依赖关系

```
模块 0 (OpenCode CLI 手动验证)  [1h]  ← 最优先，确认 CLI 能力边界
    │
    ▼
模块 1 (基础设施验证)           [2h]
    │
    ▼
模块 2 (SandboxManager)        [4h]  ← 关键路径
    │
    ▼
模块 3 (Checkpoint + Provision) [2h]
    │
    ├──▶ 模块 4 (CLI Wrapper + Parser) [3h]  ← 基于模块 0 发现实现
    │         │
    │         ▼
    │     模块 5 (Streaming Pipeline) [3h]
    │         │
    └─────────┤
              ▼
         模块 6 (Temporal 工作流)   [4h]
              │
              ▼
         模块 7 (REST API + DB)     [3h]
              │
              ▼
         模块 8 (WebSocket Hub)     [2h]
```

---

## 模块 0：OpenCode CLI 手动验证（1 小时）— 最优先

> **目的**：在编写任何自动化代码之前，先手动确认 OpenCode CLI 的实际能力和输出格式。
> 如果 `--log-format jsonl`、`--log-file` 等参数不存在，后续 FileWatcher、Parser、命令构建全部要重写。
> 因此本模块必须最先执行，结果决定模块 4 的实现方向。

### 步骤

1. **构建沙盒镜像**
   ```bash
   docker build -t multi-agent-studio/sandbox-base:latest infra/sandbox-images/base/
   ```

2. **进入容器手动测试 opencode 命令**
   ```bash
   docker run -it multi-agent-studio/sandbox-base:latest bash
   # 容器内执行：
   opencode --help
   opencode task --help
   opencode task --agent build --prompt "say hello"
   ```

3. **验证输出通道参数**
   - 测试 `--log-format jsonl` 是否被识别
   - 测试 `--log-file <path>` 是否被识别
   - 记录支持的参数列表，作为模块 4 命令构建的依据

4. **验证输出格式**
   - 确认输出是 JSONL 还是纯文本
   - 如果是 JSONL，记录 `type` 字段的所有可能值
   - 如果不是 JSONL，记录原始输出格式，确定解析策略

5. **验证缓冲行为**
   - 直接运行 vs `stdbuf -o0` 运行，对比输出实时性
   - 确认 stdout 是行缓冲还是块缓冲

6. **输出验证报告**
   - 将发现记录为简短文档，供模块 4 使用
   - 明确：CLI 支持的参数 → 命令模板；输出格式 → Parser 实现；缓冲方式 → FileWatcher 策略

### 验收标准
- 明确知道 opencode CLI 支持哪些参数
- 明确知道输出格式（JSONL / 纯文本 / 混合）
- 有明确的缓冲方案（是否需要 stdbuf）
- 产出的验证报告足以指导模块 4 的实现

---

## 模块 1：基础设施验证（2 小时）

### 步骤

1. **修复 BUG-3**：docker-compose.yml Temporal 配置
   - 创建 `infra/temporal/config/development.yaml`
   - 将 `temporal_data:` 改为 `./temporal/config:`

2. **修复 BUG-2**：Dockerfile 移除 stdbuf

3. **构建沙盒基础镜像**
   ```bash
   docker build -t multi-agent-studio/sandbox-base:latest infra/sandbox-images/base/
   ```
   - 验证 opencode 安装成功：`docker run --rm multi-agent-studio/sandbox-base:latest opencode --help`

4. **启动全套基础设施**
   ```bash
   docker-compose -f infra/docker-compose.yml up -d
   ```

5. **逐项验证**
   ```bash
   docker exec mas-postgres pg_isready           # PostgreSQL
   docker exec mas-redis redis-cli ping           # Redis
   curl http://localhost:8088                     # Temporal UI
   curl http://localhost:9001                     # MinIO Console
   ```

6. **创建 .env 文件**（`apps/orchestrator/.env`）

### 验收标准
- 5 个容器全部 running
- Temporal UI 可访问
- 沙盒镜像构建成功，opencode --help 有输出

---

## 模块 2：SandboxManager（4 小时）— 关键路径

### 步骤

1. **引入 Docker SDK**：使用同步 `docker` SDK + `asyncio.to_thread()` 包装
   - MVP 阶段并发量小（< 50 沙盒），同步 SDK + 线程池足够
   - 后续可升级为 aiohttp 直调 Docker Engine API

2. **实现 `create(workspace_id, template)`**
   - 创建两个命名卷：`{ws_id}-workspace`（/workspace）和 `{ws_id}-meta`（/sandbox-meta）
   - 环境变量 `TERM=dumb`（防 ANSI 转义码）
   - `detach=True` 启动容器，返回 container.id

3. **实现 `exec(container_id, cmd)`**
   - `container.exec_run(cmd, workdir="/workspace")`
   - 返回 `(stdout.decode(), stderr.decode())`

4. **实现 `exec_async(container_id, cmd)`**
   - `container.exec_run(cmd, detach=True, workdir="/workspace")`
   - 返回 Docker exec ID

5. **实现 `wait_process(exec_id)`**
   - 轮询 `self.client.api.exec_inspect(exec_id)`
   - `Running == False` 时返回 `ExitCode`

6. **实现 `get_process(exec_id)`**
   - 返回 `ProcessInfo(running=..., exit_code=...)`

7. **实现 `write_file(container_id, path, content)`**
   - 使用 `tarfile` + `io.BytesIO` 创建 tar archive
   - `container.put_archive()` 写入

8. **实现 `read_file(container_id, path)`**
   - `container.exec_run(f"cat {path}")` 简单实现

9. **实现 `snapshot(container_id)`**
   - `container.commit()` 生成镜像

10. **实现 `destroy(container_id)`**
    - `container.stop(timeout=5)` + `container.remove(force=True)`

### 验收标准
```python
mgr = SandboxManager("unix:///var/run/docker.sock", "multi-agent-studio/sandbox-base:latest")
cid = await mgr.create("test-ws-1")
stdout, _ = await mgr.exec(cid, "echo hello")         # "hello"
await mgr.write_file(cid, "/workspace/test.txt", "hi")
content = await mgr.read_file(cid, "/workspace/test.txt")  # "hi"
await mgr.destroy(cid)
```

---

## 模块 3：Checkpoint + Provisioner（2 小时）

### 步骤

1. **修复 BUG-1**：checkpoint.py 所有 `_git_cmd` 调用
   - `auto_commit`：`self.sandbox.exec(sandbox_id, self._git_cmd("add -A"))` 等
   - `rollback`：同上
   - `get_diff`：同上，修复返回值 `stdout, _ = await self.sandbox.exec(...)`

2. **init_repo 添加 git config**
   - `git config user.email 'orchestrator@mas.local'`
   - `git config user.name 'MAS Orchestrator'`
   - 不加这个 git commit 会失败

3. **实现 Provisioner.provision()**
   - 调用 `generate_opencode_config()` 生成配置
   - 创建目录、初始化 Git、注入配置

### 验收标准
```
创建沙盒 → init_repo → 写文件 → auto_commit → 修改文件 → get_diff → rollback → 验证恢复
```

---

## 模块 4：CLI Wrapper + Parser 实现（3 小时）— 基于模块 0 发现

> **前置条件**：模块 0 已完成，已知 CLI 支持的参数、输出格式、缓冲行为。
> 本模块根据模块 0 的发现，实现正式的 CLI Wrapper 和 Parser。

### 步骤

1. **实现 CLI Wrapper（命令构建器）**
   - 根据模块 0 确认的参数支持情况，构建命令模板
   - 如果支持 `--log-format jsonl --log-file`：
     ```python
     cmd = f"opencode task --agent {agent} --prompt '{prompt}' --log-format jsonl --log-file {log_path}"
     ```
   - 如果不支持上述参数（备选方案）：
     ```python
     cmd = f"stdbuf -o0 opencode task --agent {agent} --prompt '{prompt}' > {log_path} 2>&1"
     ```
   - 将选择逻辑封装为 `build_opencode_command(agent, prompt, log_path)` 函数

2. **实现 Parser**
   - 根据模块 0 确认的输出格式实现解析：
   - JSONL 格式 → 逐行 `json.loads()`，根据实际 `type` 字段映射
   - 纯文本格式 → 按行封装为 `StreamEvent(type="shell_stdout", content=line)`
   - 更新 parser.py 的 `_map_type()` 映射为模块 0 发现的实际值
   - 添加 `strip_ansi()` 正则（无论哪种格式都需要）

3. **实现 50MB Log Bomb 防御**
   - FileWatcher 中检查文件大小
   - 超过限制时发布 `LogLimitExceeded` 事件

### 风险应对
- 不支持 `--log-file` → stdout 重定向 + parser 容错
- 输出含 ANSI 码 → parser 中加 `strip_ansi()` 正则
- bun 安装的 opencode 二进制名不同 → 模块 0 已确认，此处使用正确名称

### 验收标准
- 沙盒中 opencode 任务成功执行并产生输出
- stream.jsonl 实时增长（非块增长）
- parser 正确解析所有行
- 50MB 限制正确触发

---

## 模块 5：Streaming Pipeline（3 小时）

### 步骤

1. **修复 BUG-4 + BUG-5**：FileWatcher
   - 改用 `exec("tail -n +{line_offset} {file_path}")` 只读新增行
   - 集成 StreamThrottler：shell_stdout 事件走 100ms 节流
   - stop() 时调用 throttler.flush()

2. **Parser 加固**
   - 添加 ANSI 转义码剥离
   - 添加最大行长度检查（> 1MB 跳过）

3. **Publisher 容错**
   - Redis 连接失败时 retry + exponential backoff
   - 添加重连逻辑

4. **StreamEvent 添加 timestamp**

### 验收标准
- FileWatcher 逐行读取并发布到 Redis
- 100 条快速 shell_stdout → 合并为约 10 个 chunk
- 50MB 限制触发 LogLimitExceeded
- 非 JSON 行静默跳过

---

## 模块 6：Temporal 工作流（4 小时）

### 步骤

1. **实现 `start_agent_task` Activity**
   - 使用模块级 SandboxManager 单例
   - 调用 Provisioner 注入配置
   - 调用 CheckpointManager.auto_commit() 执行前检查点
   - 调用 SandboxManager.exec_async() 启动 OpenCode
   - exec_id 存入模块级字典 `_exec_registry`

2. **实现 `check_agent_status` Activity**
   - 查找 _exec_registry
   - 检查进程状态
   - 完成时执行后 auto_commit

3. **创建 DAGWorkflow（多层 DAG 执行）**
   - 接收 compiler.compile_dag() 的分层结果
   - 每个节点封装为 `AgentTaskWorkflow`（Child Workflow），由 DAGWorkflow 按层调度
   - **层内并发**：同一层的多个节点通过多个并行 `workflow.execute_child_workflow` 调用实现并发。示例：

     ```python
     @workflow.defn
     class DAGWorkflow:
         @workflow.run
         async def run(self, params: DAGParams) -> DAGResult:
             layer_results: dict[str, Any] = {}
             for layer in params.layers:
                 # 同一层内并行启动所有 child workflow
                 child_handles: list[WorkflowHandle] = []
                 for node in layer.nodes:
                     # 将上游结果注入节点参数
                     node_input = self._inject_upstream(node, layer_results)
                     handle = await workflow.start_child_workflow(
                         AgentTaskWorkflow.run,
                         node_input,
                         id=f"agent-{params.run_id}-{node.id}",
                     )
                     child_handles.append((node.id, handle))

                 # 等待本层所有 child workflow 完成
                 for node_id, handle in child_handles:
                     result = await handle
                     layer_results[node_id] = result

             return DAGResult(results=layer_results)
     ```

   - **不使用 `asyncio.gather`**：Temporal Workflow 内不能使用原生 asyncio 并发原语（Temporal 的确定性要求），必须使用 `workflow.start_child_workflow` 或 `workflow.execute_child_workflow` 的多并行调用来实现并发
   - **层间串行**：一层全部完成后再启动下一层，保证上游结果可用

4. **连接 REST API → Temporal Client**
   - runs.py 中 trigger_run：编译 DAG → 启动 DAGWorkflow

5. **Kill & Resume 验证（P0-8）**
   - kill -9 worker → 重启 → 验证恢复

### 验收标准
- 单节点工作流完成
- 多层 DAG（Plan → Coder → Shell）按层执行
- Kill & Resume 成功恢复

---

## 模块 7：REST API + 数据库（3 小时）

### 步骤

1. **创建 SQLAlchemy 模型**（`app/models/db.py`）
   - Workflow, Run, NodeExecution, SharedKV, User
   - 使用 asyncpg + async sessionmaker

2. **建表方案（MVP 阶段）**
   - 使用 FastAPI lifespan 事件 + SQLAlchemy `create_all()` 自动建表：
     ```python
     # app/main.py
     from contextlib import asynccontextmanager
     from app.models.db import Base
     from app.core.database import engine

     @asynccontextmanager
     async def lifespan(app: FastAPI):
         # 启动时建表
         async with engine.begin() as conn:
             await conn.run_sync(Base.metadata.create_all)
         yield
         # 关闭时清理
         await engine.dispose()

     app = FastAPI(lifespan=lifespan)
     ```
   - **选型理由**：
     - MVP 阶段表结构频繁变动，`create_all()` 只创建不存在的表，开发体验好
     - 无需额外安装 Alembic，减少初期复杂度
     - 后续稳定后迁移到 Alembic 做版本化 migration 只需 `alembic init` + 生成初始 migration，平滑过渡
     - 注意：`create_all()` 不会修改已存在的表结构（如加列），生产环境必须用 Alembic

3. **添加 Pydantic 请求/响应模型**（`app/models/schemas.py`）
   - CreateWorkflowRequest, TriggerRunRequest 等

4. **实现 workflows.py**：CRUD + DAG 验证
5. **实现 runs.py**：trigger_run 连接 Temporal
6. **实现 models.py**：静态模型列表（已基本完成）

### 验收标准
- curl POST /api/workflows → 创建成功
- curl POST /api/workflows/{id}/run → Temporal 工作流启动
- 无效 DAG（含环）→ 400 错误

---

## 模块 8：WebSocket Hub（2 小时）

### 步骤

1. **添加 Redis 订阅循环**
   - 首个客户端连接时启动 pubsub
   - 最后一个断开时取消订阅

2. **添加 WebSocket 端点到 main.py**
   - `@app.websocket("/ws/runs/{run_id}/stream")`

3. **添加心跳机制**（30 秒 ping）

### 验收标准
- WS 客户端连接 → Redis publish → 客户端收到消息

---

*文档版本：v1.1*
*创建时间：2026-05-07*
*更新时间：2026-05-07*
*状态：已按总指挥审查意见修订*
