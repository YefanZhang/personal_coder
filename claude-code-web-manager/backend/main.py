import asyncio
import json
import os
import shutil
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, Depends, HTTPException, Header
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse

from backend.database import Database
from backend.executor import ClaudeCodeExecutor
from backend.scheduler import TaskScheduler
from backend.task_registry import TaskRegistry
from backend.models import Task, TaskStatus, TaskMode, CreateTaskRequest
from backend.chat import ChatSession

# ── Singletons ────────────────────────────────────────────────────────────────

db = Database(db_path=os.getenv("DB_PATH", "tasks.db"))
executor = ClaudeCodeExecutor(
    max_workers=int(os.getenv("MAX_WORKERS", "3")),
    base_repo=os.getenv("BASE_REPO", "/home/ubuntu/personal_coder"),
    log_dir=os.getenv("LOG_DIR", "/home/ubuntu/task-logs"),
    worktree_dir=os.getenv("WORKTREE_DIR", "/home/ubuntu/personal_coder-worktrees"),
)
registry = TaskRegistry(
    registry_path=os.getenv("REGISTRY_PATH", os.path.join(
        os.path.dirname(__file__), "..", "..", "data", "dev-tasks.json"
    ))
)

API_KEY = os.getenv("API_KEY", "")


# ── WebSocket manager ─────────────────────────────────────────────────────────

class ConnectionManager:
    def __init__(self):
        self.connections: list[WebSocket] = []

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self.connections.append(ws)

    def disconnect(self, ws: WebSocket) -> None:
        if ws in self.connections:
            self.connections.remove(ws)

    async def broadcast(self, task_id: int, data: dict) -> None:
        msg = json.dumps({"task_id": task_id, **data})
        dead: list[WebSocket] = []
        # Iterate over a copy to avoid "list changed size during iteration"
        # when concurrent broadcasts or disconnects modify self.connections
        for conn in list(self.connections):
            try:
                await conn.send_text(msg)
            except Exception:
                dead.append(conn)
        for conn in dead:
            if conn in self.connections:
                self.connections.remove(conn)


ws_manager = ConnectionManager()


# ── Registry sync helper ─────────────────────────────────────────────────────

async def _sync_registry() -> None:
    """Read all DB tasks and sync to dev-tasks.json."""
    try:
        tasks = await db.list_tasks()
        await registry.sync(tasks)
    except Exception as e:
        print(f"[registry] sync error: {e}")


scheduler = TaskScheduler(
    executor=executor,
    db=db,
    ws_manager=ws_manager,
    max_concurrent=int(os.getenv("MAX_CONCURRENT", "3")),
    on_state_change=_sync_registry,
)


# ── Startup recovery ──────────────────────────────────────────────────────────

async def _recover_stuck_tasks() -> None:
    stuck = await db.list_tasks(status=TaskStatus.IN_PROGRESS)
    for task in stuck:
        await db.update_task(task.id, status=TaskStatus.PENDING, worker_pid=None)
    if stuck:
        print(f"[startup] recovered {len(stuck)} stuck task(s) → pending")


# ── Lifespan (NOT deprecated @app.on_event) ───────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init()
    await _recover_stuck_tasks()
    claude_path = shutil.which("claude")
    if claude_path:
        print(f"[startup] claude CLI found at {claude_path}")
    else:
        print("[startup] WARNING: claude CLI not found in PATH — tasks will fail")
    registry.load_cli_tasks()
    await _sync_registry()
    scheduler_task = asyncio.create_task(scheduler.start())
    yield
    scheduler.stop()
    scheduler_task.cancel()
    await db.close()


app = FastAPI(title="Claude Code Web Manager", lifespan=lifespan)


# ── Auth ──────────────────────────────────────────────────────────────────────

async def verify_api_key(x_api_key: str = Header(default="")) -> None:
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/api/health")
async def health():
    return {"status": "ok"}


# ── Tasks ─────────────────────────────────────────────────────────────────────

@app.post("/api/tasks", dependencies=[Depends(verify_api_key)], status_code=201)
async def create_task(req: CreateTaskRequest, request: Request):
    created_by = request.client.host if request.client else None
    task = await db.create_task(
        title=req.title,
        prompt=req.prompt,
        mode=req.mode.value,
        priority=req.priority.value,
        depends_on=req.depends_on,
        repo_path=req.repo_path,
        tags=req.tags,
        created_by=created_by,
    )
    await _sync_registry()
    return task


@app.post("/api/tasks/batch", dependencies=[Depends(verify_api_key)], status_code=201)
async def create_tasks_batch(reqs: list[CreateTaskRequest], request: Request):
    created_by = request.client.host if request.client else None
    tasks = []
    for req in reqs:
        task = await db.create_task(
            title=req.title,
            prompt=req.prompt,
            mode=req.mode.value,
            priority=req.priority.value,
            depends_on=req.depends_on,
            repo_path=req.repo_path,
            tags=req.tags,
            created_by=created_by,
        )
        tasks.append(task)
    await _sync_registry()
    return tasks


@app.get("/api/tasks")
async def list_tasks(status: Optional[TaskStatus] = None):
    return await db.list_tasks(status=status)


@app.get("/api/tasks/{task_id}")
async def get_task(task_id: int):
    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    logs = await db.get_task_logs(task_id)
    return {"task": task, "logs": logs}


@app.get("/api/tasks/{task_id}/logs")
async def get_task_logs(task_id: int):
    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return await db.get_task_logs(task_id)


@app.post("/api/tasks/{task_id}/cancel", dependencies=[Depends(verify_api_key)])
async def cancel_task(task_id: int):
    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    await scheduler.cancel_task(task_id)
    await db.update_task(task_id, status=TaskStatus.CANCELLED)
    await _sync_registry()
    return {"status": "cancelled"}


@app.post("/api/tasks/{task_id}/retry", dependencies=[Depends(verify_api_key)])
async def retry_task(task_id: int):
    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    await db.update_task(task_id, status=TaskStatus.PENDING, error=None)
    await _sync_registry()
    return {"status": "pending"}


@app.post("/api/tasks/{task_id}/approve-plan", dependencies=[Depends(verify_api_key)])
async def approve_plan(task_id: int):
    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    await db.update_task(task_id, status=TaskStatus.PENDING, mode=TaskMode.EXECUTE.value)
    await _sync_registry()
    return {"status": "pending"}


@app.delete("/api/tasks/{task_id}", dependencies=[Depends(verify_api_key)])
async def delete_task(task_id: int):
    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    await db.delete_task(task_id)
    await _sync_registry()
    return {"status": "deleted"}


# ── WebSocket ─────────────────────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws_manager.connect(ws)
    try:
        while True:
            await ws.receive_text()  # keep-alive ping/pong
    except WebSocketDisconnect:
        ws_manager.disconnect(ws)


# ── Chat WebSocket ───────────────────────────────────────────────────────────

_chat_sessions: dict[int, ChatSession] = {}  # keyed by id(ws)


@app.websocket("/ws/chat")
async def chat_endpoint(ws: WebSocket):
    await ws.accept()
    base_repo = os.getenv("BASE_REPO", "/home/ubuntu/personal_coder")
    session = ChatSession(working_dir=base_repo)
    ws_id = id(ws)
    _chat_sessions[ws_id] = session

    async def _send(data: dict) -> None:
        try:
            await ws.send_text(json.dumps(data))
        except Exception:
            pass

    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await _send({"type": "error", "message": "Invalid JSON"})
                continue

            msg_type = msg.get("type")

            if msg_type == "message":
                text = msg.get("text", "").strip()
                if not text:
                    await _send({"type": "error", "message": "Empty message"})
                    continue

                # Allow changing working directory
                wd = msg.get("working_dir")
                if wd and os.path.isdir(wd):
                    session.working_dir = wd

                await session.send_message(
                    text=text,
                    on_text=lambda t: _send({"type": "assistant_text", "text": t}),
                    on_tool=lambda s: _send({"type": "tool_use", "summary": s}),
                    on_session_info=lambda m: _send({"type": "session_start", "model": m}),
                    on_done=lambda d: _send({"type": "message_done", **d}),
                    on_error=lambda e: _send({"type": "error", "message": e}),
                )

            elif msg_type == "cancel":
                await session.cancel()
                await _send({"type": "cancelled"})

    except WebSocketDisconnect:
        await session.cleanup()
        _chat_sessions.pop(ws_id, None)


# ── Static frontend (must be last) ───────────────────────────────────────────

import os as _os
_frontend_dir = _os.path.join(_os.path.dirname(__file__), "..", "frontend")
if _os.path.isdir(_frontend_dir):
    app.mount("/", StaticFiles(directory=_frontend_dir, html=True), name="static")
