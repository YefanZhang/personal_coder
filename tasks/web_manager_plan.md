# Claude Code Web Manager — 详细构建方案

> **版本说明：** 本文档已于 2026-02-26 完成技术审查，修正了已知 API 错误，新增测试策略、数据库接口设计、启动恢复逻辑、缺失端点和 WebSocket 健壮性改进。所有注释以 `[FIX]`、`[ADD]`、`[NOTE]` 标注。

---

## 1. 项目概述

构建一个基于 Web 的任务管理中心，用于在你的 EC2 + Tailscale 网络中管理多个 Claude Code 实例.通过 `claude -p [prompt] --dangerously-skip-permissions` 将 Claude Code 变成非交互式组件，用 Python 后端调度，通过手机浏览器随时派活。

---

## 2. 整体架构

```
┌─────────────────────────────────────────────────────────┐
│                    iPhone / Browser                      │
│              (Safari PWA / 任何浏览器)                    │
└──────────────────────┬──────────────────────────────────┘
                       │ HTTPS (Tailscale / Caddy)
                       ▼
┌─────────────────────────────────────────────────────────┐
│                  FastAPI Backend (EC2)                    │
│                                                          │
│  ┌──────────┐  ┌──────────────┐  ┌───────────────────┐  │
│  │ REST API  │  │  WebSocket   │  │  Task Scheduler   │  │
│  │ (CRUD)    │  │  (实时推送)   │  │  (任务队列)       │  │
│  └──────────┘  └──────────────┘  └───────────────────┘  │
│                        │                                  │
│  ┌─────────────────────┴────────────────────────────┐    │
│  │            Task Executor Engine                    │    │
│  │  ┌─────────┐ ┌─────────┐ ┌─────────┐            │    │
│  │  │Worker 1 │ │Worker 2 │ │Worker N │            │    │
│  │  │(worktree│ │(worktree│ │(worktree│            │    │
│  │  │  + CC)  │ │  + CC)  │ │  + CC)  │            │    │
│  │  └─────────┘ └─────────┘ └─────────┘            │    │
│  └──────────────────────────────────────────────────┘    │
│                                                          │
│  ┌──────────────┐  ┌────────────────┐                    │
│  │  SQLite DB   │  │  Git Worktrees │                    │
│  │  (任务状态)   │  │  (并行开发)     │                    │
│  └──────────────┘  └────────────────┘                    │
└─────────────────────────────────────────────────────────┘
```

---

## ⚠️ 已知 API 陷阱（Known API Gotchas）

**在开始实施前必读！以下是原始设计中存在的 API 错误及修复方案。**

### Gotcha 1：`claude-agent-sdk` Python 包不存在

| 项目 | 原始（错误） | 正确方案 |
|------|------------|---------|
| 导入 | `from claude_agent_sdk import query, ClaudeAgentOptions` | 不存在此包，pip 安装会失败 |
| **修复 A（推荐起步）** | — | 使用 `subprocess` 直接调用 `claude` CLI |
| **修复 B（高级）** | — | 使用 `claude-code-sdk`（npm 包）通过 Node.js 子进程调用，或用官方 `anthropic` Python 包直接调用 API |

```python
# [FIX] 正确的 subprocess 方式（方案 A）
import asyncio, os

cmd = [
    "claude",
    "-p", task.prompt,
    "--dangerously-skip-permissions",
    "--output-format", "json",
]
process = await asyncio.create_subprocess_exec(
    *cmd,
    cwd=worktree_path,
    stdout=asyncio.subprocess.PIPE,
    stderr=asyncio.subprocess.PIPE,
    env={**os.environ, "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}
)
```

```python
# [FIX] 如果用官方 anthropic Python SDK（方案 B）
from anthropic import Anthropic
client = Anthropic()
# 注意：官方 SDK 不支持 dangerously-skip-permissions，需用 subprocess
```

### Gotcha 2：`ClaudeAgentOptions(permission_mode="dangerously-skip-permissions")` 不存在

Python SDK 没有暴露此参数。**必须通过 subprocess + CLI flag 实现**：

```bash
claude -p "..." --dangerously-skip-permissions --output-format json
```

### Gotcha 3：`@app.on_event("startup")` 已弃用

FastAPI ≥ 0.93 中该装饰器已弃用。**必须使用 `asynccontextmanager` lifespan**：

```python
# [FIX] 正确的 FastAPI 启动方式
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动逻辑
    await db.init()
    await _recover_stuck_tasks()  # 见下方启动恢复
    task = asyncio.create_task(scheduler.start())
    yield
    # 关闭逻辑
    task.cancel()

app = FastAPI(title="Claude Code 任务管理中心", lifespan=lifespan)
```

### Gotcha 4：`--plan` CLI flag 不存在

Plan 模式是 Claude Code 的**交互式**功能，没有对应的 CLI flag。

```python
# [FIX] 用 prompt 工程模拟 Plan 模式
if task.mode == TaskMode.PLAN:
    # 方案一：在 prompt 前加指令，要求先输出方案
    plan_prefix = (
        "IMPORTANT: Before writing any code, output a detailed implementation plan "
        "as markdown. After the plan, write '---PLAN END---', then implement it.\n\n"
    )
    actual_prompt = plan_prefix + task.prompt
    # 后处理：解析输出中 ---PLAN END--- 之前的部分作为 plan 字段
```

### Gotcha 5：`--output-format json` 的输出结构

使用 JSON 输出时，成功检测应解析 `result` 字段：

```python
# [FIX] 正确解析 JSON 输出
try:
    parsed = json.loads(full_output)
    result_text = parsed.get("result", full_output)
    # parsed 还包含 token 用量（见 Gotcha 6）
except json.JSONDecodeError:
    result_text = full_output  # 降级到原始输出
```

### Gotcha 6：Token 用量在 JSON 输出中可获取

`--output-format json` 的返回包含 token 统计，**应存入数据库**（便于成本追踪）：

```python
# JSON 输出结构示例
{
    "result": "...",           # Claude 的完整回复
    "session_id": "...",
    "total_cost_usd": 0.0123,
    "usage": {
        "input_tokens": 1500,
        "output_tokens": 800
    }
}

# [ADD] 在 tasks 表中加字段
# input_tokens INTEGER, output_tokens INTEGER, cost_usd REAL
```

---

## 3. 技术选型

| 层级 | 选择 | 理由 |
|------|------|------|
| **后端框架** | FastAPI + uvicorn | 原生 async，WebSocket 支持好，你熟悉 Python |
| **Claude Code 调度** | `subprocess` 调用 `claude` CLI | [FIX] SDK 方式有坑，subprocess 最可靠 |
| **前端** | React (单文件 JSX) 或 纯 HTML + HTMX | 看你偏好；HTMX 更轻量，React 交互更丰富 |
| **数据库** | SQLite + aiosqlite | 轻量，单机足够，无需额外服务 |
| **实时通信** | WebSocket | 任务状态变更实时推送到前端 |
| **并行化** | Git worktrees | 每个任务一个 worktree，互不干扰 |
| **进程管理** | asyncio.create_subprocess_exec | [FIX] 放弃 claude-agent-sdk，直接用 subprocess |
| **认证** | Tailscale ACL + X-API-Key header | [ADD] 具体实现见第 10 节 |
| **反向代理/HTTPS** | Caddy 或 Tailscale Serve | 自动 HTTPS，手机 PWA 需要 |
| **包管理** | uv | [FIX] 全程用 `uv sync`，不用 pip |
| **E2E 测试** | MCP Playwright + pytest | [ADD] 见第 11 节测试策略 |

---

## 4. 数据模型

```python
# models.py
from enum import Enum
from datetime import datetime
from pydantic import BaseModel

class TaskStatus(str, Enum):
    PENDING = "pending"           # 待开发
    IN_PROGRESS = "in_progress"   # 开发中
    REVIEW = "review"             # 待 Review
    COMPLETED = "completed"       # 已完成
    FAILED = "failed"             # 失败
    CANCELLED = "cancelled"       # 已取消

class TaskMode(str, Enum):
    EXECUTE = "execute"           # 直接执行
    PLAN = "plan"                 # Plan 模式，先出方案再确认（用 prompt 工程实现）

class TaskPriority(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    URGENT = "urgent"

class Task(BaseModel):
    id: int
    title: str                    # 任务简述
    prompt: str                   # 完整 prompt
    status: TaskStatus = TaskStatus.PENDING
    mode: TaskMode = TaskMode.EXECUTE
    priority: TaskPriority = TaskPriority.MEDIUM

    # 执行相关
    worktree_branch: str | None = None
    working_directory: str | None = None
    worker_pid: int | None = None

    # 结果
    output: str | None = None     # Claude Code 完整输出
    plan: str | None = None       # Plan 模式下的方案
    error: str | None = None      # 错误信息
    exit_code: int | None = None

    # [ADD] Token 成本追踪
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None

    # 依赖
    depends_on: list[int] = []    # 前置任务 ID

    # 时间
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None

    # 元数据
    repo_path: str | None = None  # 指定仓库路径
    tags: list[str] = []
```

### SQLite Schema

```sql
CREATE TABLE tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    prompt TEXT NOT NULL,
    status TEXT DEFAULT 'pending',
    mode TEXT DEFAULT 'execute',
    priority TEXT DEFAULT 'medium',
    worktree_branch TEXT,
    working_directory TEXT,
    worker_pid INTEGER,
    output TEXT,
    plan TEXT,
    error TEXT,
    exit_code INTEGER,
    -- [ADD] Token 成本追踪字段
    input_tokens INTEGER,
    output_tokens INTEGER,
    cost_usd REAL,
    depends_on TEXT DEFAULT '[]',  -- JSON array
    repo_path TEXT,
    tags TEXT DEFAULT '[]',        -- JSON array
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    started_at TIMESTAMP,
    completed_at TIMESTAMP
);

CREATE TABLE task_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    level TEXT,       -- info, warn, error
    message TEXT,
    raw_output TEXT   -- Claude Code 的原始流式输出片段
);

-- [ADD] 索引，提升调度器查询性能
CREATE INDEX idx_tasks_status ON tasks(status);
CREATE INDEX idx_tasks_priority ON tasks(priority, created_at);
CREATE INDEX idx_task_logs_task_id ON task_logs(task_id);
```

---

## 5. 核心模块详细设计

### 5.0 Database 接口（[ADD] 原计划缺失此定义）

原始方案中大量引用 `db` 和 `Database` 类，但从未定义其接口。以下是完整接口规范：

```python
# database.py
import aiosqlite
import json
from datetime import datetime
from models import Task, TaskStatus, TaskLog

class Database:
    def __init__(self, db_path: str = "tasks.db"):
        self.db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def init(self):
        """创建数据库表（幂等，可重复调用）"""
        ...

    async def create_task(
        self,
        title: str,
        prompt: str,
        mode: str = "execute",
        priority: str = "medium",
        depends_on: list[int] = [],
        repo_path: str | None = None,
        tags: list[str] = [],
    ) -> Task:
        """插入新任务，返回含 id 的 Task 对象"""
        ...

    async def get_task(self, task_id: int) -> Task | None:
        """按 id 获取单个任务"""
        ...

    async def list_tasks(self, status: TaskStatus | None = None) -> list[Task]:
        """获取所有任务（可按 status 过滤）"""
        ...

    async def update_task(self, task_id: int, **fields) -> None:
        """动态更新任务字段"""
        ...

    async def count_tasks(self, status: TaskStatus) -> int:
        """统计某状态的任务数量（调度器用）"""
        ...

    async def get_next_pending_task(self) -> Task | None:
        """
        获取下一个待执行任务。
        排序规则：priority DESC（urgent > high > medium > low），然后 created_at ASC
        """
        ...

    async def add_log(
        self,
        task_id: int,
        level: str,
        message: str,
        raw_output: str | None = None,
    ) -> None:
        """追加任务日志"""
        ...

    async def get_task_logs(self, task_id: int) -> list[TaskLog]:
        """获取任务的所有日志条目"""
        ...

    async def delete_task(self, task_id: int) -> None:
        """删除任务及其日志（级联）"""
        ...
```

### 5.1 Task Executor（任务执行引擎）

**[FIX] 移除 `--plan` flag；[ADD] 文件日志；[ADD] Token 解析**

```python
# executor.py
import asyncio
import json
import os
from pathlib import Path
from models import Task, TaskMode

class ClaudeCodeExecutor:
    def __init__(self, max_workers: int = 3, base_repo: str = "/home/ubuntu/project"):
        self.max_workers = max_workers
        self.base_repo = base_repo
        self.active_tasks: dict[int, asyncio.subprocess.Process] = {}
        # [ADD] 文件日志目录
        self.log_dir = Path("/home/ubuntu/task-logs")
        self.log_dir.mkdir(exist_ok=True)

    async def execute_task(self, task: Task, on_output: callable, on_complete: callable):
        """在独立的 git worktree 中执行任务"""

        # 1. 创建 worktree
        branch = f"task-{task.id}-{task.title[:20].replace(' ', '-')}"
        worktree_path = f"/home/ubuntu/worktrees/{branch}"

        wt_proc = await asyncio.create_subprocess_exec(
            "git", "worktree", "add", "-b", branch, worktree_path,
            cwd=self.base_repo,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await wt_proc.wait()
        if wt_proc.returncode != 0:
            err = (await wt_proc.stderr.read()).decode()
            await on_complete(task.id, exit_code=1, error=f"worktree creation failed: {err}")
            return

        # 2. 构造 Claude Code 命令
        # [FIX] Plan 模式用 prompt 工程实现，不用 --plan flag
        prompt = task.prompt
        if task.mode == TaskMode.PLAN:
            prompt = (
                "IMPORTANT: Before writing any code, output a detailed implementation "
                "plan as markdown. After the plan, write '---PLAN END---', then implement.\n\n"
            ) + prompt

        cmd = [
            "claude",
            "-p", prompt,
            "--dangerously-skip-permissions",
            "--output-format", "json",
        ]

        # 3. 启动子进程
        process = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=worktree_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}
        )

        self.active_tasks[task.id] = process

        # 4. 流式读取输出（同时写文件）
        # [ADD] 文件日志
        log_path = self.log_dir / f"task-{task.id}.log"
        output_chunks = []
        with open(log_path, "w") as log_file:
            async for line in process.stdout:
                decoded = line.decode().strip()
                output_chunks.append(decoded)
                log_file.write(decoded + "\n")
                log_file.flush()
                await on_output(task.id, decoded)

        # 5. 等待完成
        await process.wait()
        stderr = (await process.stderr.read()).decode()

        full_output = "\n".join(output_chunks)

        # [FIX] 解析 JSON 输出，提取 result 和 token 用量
        result_text = full_output
        input_tokens = output_tokens = None
        cost_usd = None
        try:
            parsed = json.loads(full_output)
            result_text = parsed.get("result", full_output)
            usage = parsed.get("usage", {})
            input_tokens = usage.get("input_tokens")
            output_tokens = usage.get("output_tokens")
            cost_usd = parsed.get("total_cost_usd")
        except (json.JSONDecodeError, AttributeError):
            pass

        await on_complete(
            task.id,
            exit_code=process.returncode,
            output=result_text,
            error=stderr if process.returncode != 0 else None,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
        )

        self.active_tasks.pop(task.id, None)

    async def cancel_task(self, task_id: int):
        if task_id in self.active_tasks:
            self.active_tasks[task_id].terminate()
            self.active_tasks.pop(task_id, None)
```

### 5.2 Task Scheduler（任务调度器）

```python
# scheduler.py
class TaskScheduler:
    def __init__(self, executor: ClaudeCodeExecutor, db: Database, ws_manager, max_concurrent: int = 3):
        self.executor = executor
        self.db = db
        self.ws_manager = ws_manager
        self.max_concurrent = max_concurrent
        self._running = False

    async def start(self):
        """主调度循环"""
        self._running = True
        while self._running:
            active_count = await self.db.count_tasks(status=TaskStatus.IN_PROGRESS)

            if active_count < self.max_concurrent:
                next_task = await self.db.get_next_pending_task()
                if next_task and await self._dependencies_met(next_task):
                    await self._dispatch(next_task)

            await asyncio.sleep(2)

    async def _dependencies_met(self, task: Task) -> bool:
        """检查前置任务是否都已完成"""
        for dep_id in task.depends_on:
            dep = await self.db.get_task(dep_id)
            if dep is None or dep.status != TaskStatus.COMPLETED:
                return False
        return True

    async def _dispatch(self, task: Task):
        await self.db.update_task(task.id, status=TaskStatus.IN_PROGRESS, started_at=datetime.now())
        asyncio.create_task(
            self.executor.execute_task(task, self._on_output, self._on_complete)
        )

    async def _on_output(self, task_id: int, chunk: str):
        await self.ws_manager.broadcast(task_id, {"type": "output", "data": chunk})
        await self.db.add_log(task_id, "info", chunk)

    async def _on_complete(self, task_id: int, exit_code: int, output: str = "",
                           error: str = "", input_tokens=None, output_tokens=None, cost_usd=None):
        status = TaskStatus.COMPLETED if exit_code == 0 else TaskStatus.FAILED
        await self.db.update_task(
            task_id,
            status=status,
            exit_code=exit_code,
            output=output,
            error=error,
            completed_at=datetime.now(),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
        )
        await self.ws_manager.broadcast(task_id, {"type": "complete", "status": status})

    async def cancel_task(self, task_id: int):
        await self.executor.cancel_task(task_id)
```

### 5.3 FastAPI 后端

**[FIX] 使用 `asynccontextmanager` lifespan；[ADD] 启动恢复；[ADD] 新端点**

```python
# main.py
from contextlib import asynccontextmanager
import asyncio
import json
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Depends, HTTPException, Header
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse

# [FIX] 启动/关闭使用 lifespan，不用已弃用的 @app.on_event
@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init()
    # [ADD] 启动恢复：重置孤立的 in_progress 任务
    await _recover_stuck_tasks()
    scheduler_task = asyncio.create_task(scheduler.start())
    yield
    scheduler_task.cancel()

app = FastAPI(title="Claude Code 任务管理中心", lifespan=lifespan)

async def _recover_stuck_tasks():
    """服务重启时，将上次崩溃遗留的 in_progress 任务重置为 pending"""
    stuck = await db.list_tasks(status=TaskStatus.IN_PROGRESS)
    for task in stuck:
        await db.update_task(task.id, status=TaskStatus.PENDING, worker_pid=None)
    if stuck:
        print(f"[startup] recovered {len(stuck)} stuck tasks → pending")

# --- 认证 ---
# [ADD] Bearer token 认证（密钥从 .env 读取）
import os
API_KEY = os.getenv("API_KEY", "")

async def verify_api_key(x_api_key: str = Header(default="")):
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")

# --- REST API ---

# [ADD] 健康检查端点（systemd watchdog + Playwright 测试用）
@app.get("/api/health")
async def health():
    return {"status": "ok"}

@app.post("/api/tasks", dependencies=[Depends(verify_api_key)])
async def create_task(req: CreateTaskRequest):
    task = await db.create_task(
        title=req.title,
        prompt=req.prompt,
        mode=req.mode,
        priority=req.priority,
        depends_on=req.depends_on,
        repo_path=req.repo_path,
        tags=req.tags,
    )
    return task

# [ADD] 批量创建任务（launcher.sh 批量投递用）
@app.post("/api/tasks/batch", dependencies=[Depends(verify_api_key)])
async def create_tasks_batch(reqs: list[CreateTaskRequest]):
    tasks = []
    for req in reqs:
        task = await db.create_task(**req.model_dump())
        tasks.append(task)
    return tasks

@app.get("/api/tasks")
async def list_tasks(status: TaskStatus | None = None):
    return await db.list_tasks(status=status)

@app.get("/api/tasks/{task_id}")
async def get_task(task_id: int):
    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    logs = await db.get_task_logs(task_id)
    return {"task": task, "logs": logs}

# [ADD] 任务日志流（SSE）
@app.get("/api/tasks/{task_id}/logs")
async def get_task_logs(task_id: int):
    logs = await db.get_task_logs(task_id)
    return logs

@app.post("/api/tasks/{task_id}/cancel", dependencies=[Depends(verify_api_key)])
async def cancel_task(task_id: int):
    await scheduler.cancel_task(task_id)
    await db.update_task(task_id, status=TaskStatus.CANCELLED)

@app.post("/api/tasks/{task_id}/retry", dependencies=[Depends(verify_api_key)])
async def retry_task(task_id: int):
    await db.update_task(task_id, status=TaskStatus.PENDING, error=None)

@app.post("/api/tasks/{task_id}/approve-plan", dependencies=[Depends(verify_api_key)])
async def approve_plan(task_id: int):
    await db.update_task(task_id, status=TaskStatus.PENDING, mode=TaskMode.EXECUTE)

@app.delete("/api/tasks/{task_id}", dependencies=[Depends(verify_api_key)])
async def delete_task(task_id: int):
    await db.delete_task(task_id)

# --- WebSocket 实时推送 ---

class ConnectionManager:
    def __init__(self):
        self.connections: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.connections.append(ws)

    # [FIX] 清理死连接，避免向断开的连接发送导致异常
    async def broadcast(self, task_id: int, data: dict):
        msg = json.dumps({"task_id": task_id, **data})
        dead = []
        for conn in self.connections:
            try:
                await conn.send_text(msg)
            except Exception:
                dead.append(conn)
        for conn in dead:
            self.connections.remove(conn)

ws_manager = ConnectionManager()

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws_manager.connect(ws)
    try:
        while True:
            await ws.receive_text()  # keep alive
    except WebSocketDisconnect:
        if ws in ws_manager.connections:
            ws_manager.connections.remove(ws)

app.mount("/", StaticFiles(directory="frontend", html=True), name="static")
```

### 5.4 前端（Kanban Board）

参考截图中的看板式界面，建议用 React 单文件实现：

```
看板列布局:
┌──────────┬──────────┬──────────┬──────────┬──────────┬──────────┐
│ 待开发    │ 开发中    │ 待Review │ 已完成    │ 失败      │ 已取消    │
│ (pending) │(progress)│ (review) │(complete)│ (failed) │(canceled)│
│           │          │          │          │          │          │
│ [Task]    │ [Task]   │ [Task]   │ [Task]   │ [Task]   │          │
│ [Task]    │ [Task]   │          │ [Task]   │          │          │
│           │          │          │ ...189   │          │          │
└──────────┴──────────┴──────────┴──────────┴──────────┴──────────┘
```

关键前端功能：

- **任务创建表单**：大文本框输入 prompt，支持 Cmd+Enter 提交
- **Plan 模式复选框**：勾选后先出方案再执行（通过 prompt 工程实现）
- **前置任务下拉选择**
- **实时日志面板**：点击任务卡片展开，通过 WebSocket 流式显示输出
- **语音输入按钮**：用 Web Speech API 实现语音转文字（手机上方便）
- **中英文切换**
- **PWA manifest**：支持 Safari "添加到主屏幕"
- **[ADD] UI 底栏成本统计**：展示累计 token 用量和费用（从 tasks 表汇总）
- **[ADD] PROGRESS.md 侧边栏**：展示最近 N 条经验日志

---

## 6. Git Worktree 并行化策略

```bash
# 初始化：克隆主仓库
git clone <repo-url> /home/ubuntu/project

# 每个任务创建独立 worktree
git worktree add -b task-42-fix-login /home/ubuntu/worktrees/task-42 main

# 任务完成后合并
cd /home/ubuntu/project
git merge task-42-fix-login

# 清理
git worktree remove /home/ubuntu/worktrees/task-42
git branch -d task-42-fix-login
```

Executor 中自动管理 worktree 生命周期：
- 创建任务 → `git worktree add`
- 任务成功 → 可选自动 merge 或等待 review
- 任务失败/取消 → `git worktree remove` + `git branch -D`

---

## 7. 项目文件结构

```
claude-code-web-manager/
├── CLAUDE.md                  # Claude Code 的项目说明
├── README.md
├── pyproject.toml             # uv 项目配置
├── .env                       # API_KEY 等敏感配置（不入 git）
│
├── backend/
│   ├── __init__.py
│   ├── main.py                # FastAPI app 入口（含 lifespan）
│   ├── config.py              # 配置（端口、最大 worker 数等）
│   ├── database.py            # SQLite 数据层 (aiosqlite)
│   ├── models.py              # Pydantic 数据模型
│   ├── executor.py            # Claude Code 执行引擎
│   ├── scheduler.py           # 任务调度器
│   ├── worktree.py            # Git worktree 管理
│   └── ws_manager.py          # WebSocket 连接管理
│
├── frontend/
│   ├── index.html             # 主页面（单文件 SPA）
│   ├── manifest.json          # PWA manifest
│   ├── sw.js                  # Service Worker（离线支持）
│   └── favicon.ico
│
├── scripts/
│   ├── setup.sh               # 一键安装脚本
│   ├── start.sh               # 启动脚本
│   └── launcher.sh            # 批量投递任务脚本（调用 /api/tasks/batch）
│
└── tests/
    ├── test_executor.py       # mock subprocess，验证 worktree 创建
    ├── test_scheduler.py      # 依赖解析、优先级排序
    ├── test_api.py            # FastAPI TestClient，CRUD 端点
    ├── test_database.py       # SQLite 操作、启动恢复逻辑
    └── e2e/
        └── test_kanban.py     # Playwright E2E（使用 MCP Playwright 工具）
```

---

## 8. 分阶段实施路线（[FIX] 调整了 Phase 2/3 顺序）

> **重新排序理由**：Worktree 并行化是后端关注点，应在 Kanban UI 搭建之前完成。
> 早期暴露并发 race condition，避免 UI 构建在不稳定的并发层之上。

### Phase 1：MVP（1-2 天）
- [ ] SQLite 数据库初始化（含启动恢复逻辑）
- [ ] FastAPI 后端 CRUD API（含 /api/health + /api/tasks/batch）
- [ ] subprocess 执行 `claude -p` 命令（含 JSON 输出解析 + Token 追踪）
- [ ] 简单 HTML 前端（纯表格 + 任务创建表单）
- [ ] `GET /api/health` 可访问，`POST /api/tasks` 返回 201

### Phase 2：Git Worktree 并行化（[FIX] 原 Phase 3，提前）（第 3-4 天）
- [ ] Worktree 自动创建/清理
- [ ] 支持并发 3-5 个任务同时执行
- [ ] 调度器依赖检查（depends_on 字段）
- [ ] 任务完成后自动创建 PR 或等待 review

### Phase 3：看板 + 实时推送（[FIX] 原 Phase 2，延后）（第 5-6 天）
- [ ] WebSocket 实时输出推送（含死连接清理）
- [ ] React/HTMX 看板界面
- [ ] 任务详情侧边栏（展示完整日志）
- [ ] 任务取消、重试功能

### Phase 4：Plan 模式 + 高级功能（第 7-10 天）
- [ ] Plan 模式（prompt 工程实现，不依赖 --plan flag）
- [ ] 前置任务依赖 UI
- [ ] 语音输入（Web Speech API）
- [ ] PWA 支持（Safari 添加到主屏幕）
- [ ] 任务模板（常用 prompt 保存复用）

### Phase 5：可选增强
- [ ] 接入 MQTT（你已有经验）实现跨 EC2 实例的多 agent 协调
- [ ] CLAUDE.md / PROGRESS.md 自动管理 + UI 侧边栏展示
- [ ] Webhook 通知（完成/失败时推送到手机）

---

## 9. 部署方案

```bash
# 1. 在 EC2 上安装依赖（[FIX] 用 uv，不用 pip）
sudo apt update && sudo apt install -y python3.11 nodejs npm git
npm install -g @anthropic-ai/claude-code
curl -LsSf https://astral.sh/uv/install.sh | sh
cd claude-code-web-manager
uv sync  # 从 pyproject.toml 安装所有依赖

# 2. 配置环境变量
cp .env.example .env
# 编辑 .env，设置 API_KEY

# 3. 配置 Tailscale（你已有）
# 确保 EC2 在 Tailscale 网络中

# 4. 用 Tailscale Serve 暴露服务（自动 HTTPS）
tailscale serve https / http://localhost:8000

# 5. 启动
uvicorn backend.main:app --host 0.0.0.0 --port 8000

# 6. 或用 systemd 持久化
# /etc/systemd/system/claude-manager.service
[Unit]
Description=Claude Code Web Manager
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/claude-code-web-manager
EnvironmentFile=/home/ubuntu/claude-code-web-manager/.env
ExecStart=/home/ubuntu/.local/bin/uvicorn backend.main:app --host 0.0.0.0 --port 8000
Restart=always

[Install]
WantedBy=multi-user.target
```

---

## 10. 关键注意事项

1. **`--dangerously-skip-permissions` 安全性**：已在隔离 EC2 上运行，风险可控。但建议每个 worktree 限制文件系统范围。

2. **输出解析**：使用 `--output-format json` 可以获得结构化输出，更容易解析成功/失败和提取关键信息。**[FIX] 见 Gotcha 5/6 的解析代码。**

3. **上下文管理**：长任务用 `CLAUDE.md` + `PROGRESS.md` 让 Claude Code 保持上下文。在 worktree 根目录放好这些文件。

4. **并发限制**：Claude Code 有 API rate limit，建议 `max_concurrent` 设为 3-5，根据你的订阅计划调整。

5. **[FIX] 错误恢复（启动恢复）**：服务器启动时扫描 `status=in_progress` 的任务，通过 `_recover_stuck_tasks()` 重置为 pending，避免任务永久卡死。

6. **日志截断**：Claude Code 输出可能很长，数据库中只存摘要，完整日志写文件到 `/home/ubuntu/task-logs/task-{id}.log`。

7. **[ADD] Bearer Token 认证**：`X-API-Key` header，密钥存 `.env`。即使在私有 Tailscale 网络中，也建议启用，防止误访问。

8. **[ADD] 成本追踪**：所有任务的 token 用量和费用均存入数据库，可在 UI 底栏显示累计成本。

9. **Bootstrap 技巧**（胡渊鸣的经验）：用 Claude Code 来编写这个 manager 本身时，需要注意被管理的 Claude Code 返回值的解析。建议先手写核心 executor，再用 Claude Code 来完善 UI 和其他模块。

---

## 11. 测试策略（[ADD] 原计划缺失）

### 推荐方案：MCP Playwright（E2E）+ pytest（单元/集成）

| 方案 | 优点 | 缺点 |
|------|------|------|
| **MCP Playwright** | Claude Code 原生控制浏览器；可断言 UI 状态；自动集成到 CLAUDE.md Step 5 | 需要服务器在运行 |
| **pytest-only** | 快，无需浏览器 | 无法测试 WebSocket 实时 UI 或看板拖拽 |
| **自定义 /test 技能** | 一键调用 | 需自行维护；Claude 仍然需要浏览器 |

**最优组合**：MCP Playwright 做 E2E + pytest 做单元/集成测试。

### 11.1 启用 MCP Playwright

在 `~/.claude/settings.json` 中添加：

```json
{
  "mcpServers": {
    "playwright": {
      "command": "npx",
      "args": ["@playwright/mcp@latest"],
      "env": {}
    }
  }
}
```

这会给 Claude Code 提供 `browser_navigate`、`browser_click`、`browser_fill`、`browser_screenshot`、`browser_wait_for` 等工具，足以驱动完整的 E2E 流程。

### 11.2 E2E 测试工作流（在 CLAUDE.md Step 5 中执行）

```
合并后执行：
  1. pytest tests/ -v                        # 单元 + 集成
  2. 启动服务：uvicorn backend.main:app --port 8000 &
  3. 使用 Playwright MCP 工具：
     a. 访问 http://localhost:8000
     b. 通过表单创建测试任务
     c. 验证任务出现在 "pending" 列
     d. 等待状态变为 "in_progress"（通过 WebSocket 更新）
     e. 断言 GET /api/health 返回 200
  4. 关闭测试服务器
如有断言失败：修复后再标记任务为 done。
```

### 11.3 pytest 单元测试文件清单

```
tests/
├── test_executor.py      # mock subprocess，验证 worktree 创建逻辑
├── test_scheduler.py     # 依赖解析、优先级排序
├── test_api.py           # FastAPI TestClient，CRUD 端点
├── test_database.py      # SQLite 操作、启动恢复逻辑
└── e2e/
    └── test_kanban.py    # Playwright E2E
```

### 11.4 验收清单

实施完成后验证以下项目：

- [ ] `pytest tests/ -v` — 所有单元测试通过
- [ ] `uvicorn backend.main:app --port 8000` 无错误启动
- [ ] MCP Playwright：访问 UI，创建任务，验证看板列 WebSocket 更新
- [ ] `GET /api/health` 返回 `{"status": "ok"}`
- [ ] 关闭服务器，重启，确认孤立的 `in_progress` 任务重置为 `pending`
- [ ] 提交一个任务并检查 `/home/ubuntu/task-logs/task-{id}.log` 包含完整输出
