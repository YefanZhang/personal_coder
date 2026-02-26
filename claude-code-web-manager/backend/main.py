import asyncio
import json
import os
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Depends, HTTPException, Header
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse

from backend.database import Database
from backend.executor import ClaudeCodeExecutor
from backend.scheduler import TaskScheduler
from backend.models import Task, TaskStatus, TaskMode, CreateTaskRequest

# ── Singletons ────────────────────────────────────────────────────────────────

db = Database(db_path=os.getenv("DB_PATH", "tasks.db"))
executor = ClaudeCodeExecutor(
    max_workers=int(os.getenv("MAX_WORKERS", "3")),
    base_repo=os.getenv("BASE_REPO", "/home/ubuntu/project"),
    log_dir=os.getenv("LOG_DIR", "/home/ubuntu/task-logs"),
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
scheduler = TaskScheduler(
    executor=executor,
    db=db,
    ws_manager=ws_manager,
    max_concurrent=int(os.getenv("MAX_CONCURRENT", "3")),
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
async def create_task(req: CreateTaskRequest):
    task = await db.create_task(
        title=req.title,
        prompt=req.prompt,
        mode=req.mode.value,
        priority=req.priority.value,
        depends_on=req.depends_on,
        repo_path=req.repo_path,
        tags=req.tags,
    )
    return task


@app.post("/api/tasks/batch", dependencies=[Depends(verify_api_key)], status_code=201)
async def create_tasks_batch(reqs: list[CreateTaskRequest]):
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
        )
        tasks.append(task)
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
    return {"status": "cancelled"}


@app.post("/api/tasks/{task_id}/retry", dependencies=[Depends(verify_api_key)])
async def retry_task(task_id: int):
    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    await db.update_task(task_id, status=TaskStatus.PENDING, error=None)
    return {"status": "pending"}


@app.post("/api/tasks/{task_id}/approve-plan", dependencies=[Depends(verify_api_key)])
async def approve_plan(task_id: int):
    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    await db.update_task(task_id, status=TaskStatus.PENDING, mode=TaskMode.EXECUTE.value)
    return {"status": "pending"}


@app.delete("/api/tasks/{task_id}", dependencies=[Depends(verify_api_key)])
async def delete_task(task_id: int):
    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    await db.delete_task(task_id)
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


# ── Static frontend (must be last) ───────────────────────────────────────────

import os as _os
_frontend_dir = _os.path.join(_os.path.dirname(__file__), "..", "frontend")
if _os.path.isdir(_frontend_dir):
    app.mount("/", StaticFiles(directory=_frontend_dir, html=True), name="static")
