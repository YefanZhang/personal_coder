import aiosqlite
import json
from datetime import datetime
from typing import Optional
from pathlib import Path

from backend.models import Task, TaskStatus, TaskMode, TaskPriority, TaskLog

# Priority ordering for SQL queries (higher number = higher priority)
PRIORITY_ORDER = {
    "urgent": 4,
    "high": 3,
    "medium": 2,
    "low": 1,
}

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tasks (
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
    input_tokens INTEGER,
    output_tokens INTEGER,
    cost_usd REAL,
    depends_on TEXT DEFAULT '[]',
    repo_path TEXT,
    tags TEXT DEFAULT '[]',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    started_at TIMESTAMP,
    completed_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS task_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    level TEXT,
    message TEXT,
    raw_output TEXT
);

CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_priority ON tasks(priority, created_at);
CREATE INDEX IF NOT EXISTS idx_task_logs_task_id ON task_logs(task_id);
"""


def _row_to_task(row: aiosqlite.Row) -> Task:
    d = dict(row)
    d["depends_on"] = json.loads(d.get("depends_on") or "[]")
    d["tags"] = json.loads(d.get("tags") or "[]")
    return Task(**d)


def _row_to_log(row: aiosqlite.Row) -> TaskLog:
    return TaskLog(**dict(row))


class Database:
    def __init__(self, db_path: str = "tasks.db"):
        self.db_path = db_path
        self._conn: Optional[aiosqlite.Connection] = None

    async def init(self):
        self._conn = await aiosqlite.connect(self.db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(SCHEMA_SQL)
        await self._conn.execute("PRAGMA foreign_keys = ON")
        await self._conn.commit()

    async def close(self):
        if self._conn:
            await self._conn.close()
            self._conn = None

    async def create_task(
        self,
        title: str,
        prompt: str,
        mode: str = "execute",
        priority: str = "medium",
        depends_on: list[int] = [],
        repo_path: Optional[str] = None,
        tags: list[str] = [],
    ) -> Task:
        async with self._conn.execute(
            """
            INSERT INTO tasks (title, prompt, mode, priority, depends_on, repo_path, tags)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                title,
                prompt,
                mode,
                priority,
                json.dumps(depends_on),
                repo_path,
                json.dumps(tags),
            ),
        ) as cursor:
            task_id = cursor.lastrowid
        await self._conn.commit()
        return await self.get_task(task_id)

    async def get_task(self, task_id: int) -> Optional[Task]:
        async with self._conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (task_id,)
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return _row_to_task(row)

    async def list_tasks(self, status: Optional[TaskStatus] = None) -> list[Task]:
        if status is not None:
            async with self._conn.execute(
                "SELECT * FROM tasks WHERE status = ? ORDER BY created_at ASC",
                (status.value,),
            ) as cursor:
                rows = await cursor.fetchall()
        else:
            async with self._conn.execute(
                "SELECT * FROM tasks ORDER BY created_at ASC"
            ) as cursor:
                rows = await cursor.fetchall()
        return [_row_to_task(r) for r in rows]

    async def update_task(self, task_id: int, **fields) -> None:
        if not fields:
            return
        # Serialize special fields
        for key in ("depends_on", "tags"):
            if key in fields and isinstance(fields[key], list):
                fields[key] = json.dumps(fields[key])
        # Convert enums to their values
        for key, val in fields.items():
            if isinstance(val, (TaskStatus, TaskMode, TaskPriority)):
                fields[key] = val.value
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [task_id]
        await self._conn.execute(
            f"UPDATE tasks SET {set_clause} WHERE id = ?", values
        )
        await self._conn.commit()

    async def count_tasks(self, status: TaskStatus) -> int:
        async with self._conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE status = ?", (status.value,)
        ) as cursor:
            row = await cursor.fetchone()
        return row[0]

    async def get_next_pending_task(self) -> Optional[Task]:
        # Order: urgent > high > medium > low, then created_at ASC
        async with self._conn.execute(
            """
            SELECT * FROM tasks
            WHERE status = 'pending'
            ORDER BY
                CASE priority
                    WHEN 'urgent' THEN 4
                    WHEN 'high'   THEN 3
                    WHEN 'medium' THEN 2
                    WHEN 'low'    THEN 1
                    ELSE 0
                END DESC,
                created_at ASC
            LIMIT 1
            """
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return _row_to_task(row)

    async def add_log(
        self,
        task_id: int,
        level: str,
        message: str,
        raw_output: Optional[str] = None,
    ) -> None:
        await self._conn.execute(
            "INSERT INTO task_logs (task_id, level, message, raw_output) VALUES (?, ?, ?, ?)",
            (task_id, level, message, raw_output),
        )
        await self._conn.commit()

    async def get_task_logs(self, task_id: int) -> list[TaskLog]:
        async with self._conn.execute(
            "SELECT * FROM task_logs WHERE task_id = ? ORDER BY timestamp ASC",
            (task_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [_row_to_log(r) for r in rows]

    async def delete_task(self, task_id: int) -> None:
        await self._conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        await self._conn.commit()
