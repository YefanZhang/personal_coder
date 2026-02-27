import asyncio
import json
import os
import tempfile
from datetime import datetime
from typing import Optional

from backend.models import Task


class TaskRegistry:
    """Syncs web manager DB tasks to dev-tasks.json alongside CLI tasks."""

    def __init__(self, registry_path: str):
        self.registry_path = registry_path
        self._cli_tasks: list[dict] = []
        self._lock = asyncio.Lock()

    def load_cli_tasks(self) -> None:
        """Read dev-tasks.json and cache CLI task entries."""
        try:
            with open(self.registry_path, "r") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            self._cli_tasks = []
            return

        # Handle both old format ({"tasks": [...]}) and new format ({"meta": ..., "tasks": [...]})
        if isinstance(data, dict):
            tasks = data.get("tasks", [])
        elif isinstance(data, list):
            tasks = data
        else:
            tasks = []

        # Keep entries that are CLI tasks (no source field or source="cli")
        self._cli_tasks = []
        for t in tasks:
            if t.get("source", "cli") == "cli":
                t["source"] = "cli"  # Ensure legacy entries get the field
                self._cli_tasks.append(t)

    def _web_task_to_dict(self, task: Task) -> dict:
        """Convert a Pydantic Task to a registry dict with source=web."""
        return {
            "id": f"web-{task.id}",
            "title": task.title,
            "status": task.status.value,
            "source": "web",
            "created_by": task.created_by,
            "created_at": task.created_at.isoformat() if task.created_at else None,
            "started_at": task.started_at.isoformat() if task.started_at else None,
            "completed_at": task.completed_at.isoformat() if task.completed_at else None,
            "priority": task.priority.value,
            "mode": task.mode.value,
            "cost_usd": task.cost_usd,
            "input_tokens": task.input_tokens,
            "output_tokens": task.output_tokens,
            "exit_code": task.exit_code,
            "error": task.error,
            "tags": task.tags,
            "depends_on": [f"web-{d}" for d in task.depends_on],
        }

    async def sync(self, db_tasks: list[Task]) -> None:
        """Merge CLI tasks + web DB tasks and write to dev-tasks.json atomically."""
        async with self._lock:
            web_tasks = [self._web_task_to_dict(t) for t in db_tasks]
            all_tasks = self._cli_tasks + web_tasks

            # Compute meta summary
            total_cost = sum(
                t.get("cost_usd") or 0 for t in all_tasks
            )
            meta = {
                "last_synced_at": datetime.now().isoformat(),
                "total_cost_usd": round(total_cost, 6),
                "cli_tasks": len(self._cli_tasks),
                "web_tasks": len(web_tasks),
            }

            output = {"meta": meta, "tasks": all_tasks}

            # Atomic write: write to temp file then os.replace
            await asyncio.to_thread(self._atomic_write, output)

    def _atomic_write(self, data: dict) -> None:
        """Write JSON atomically using tmp file + os.replace."""
        dir_name = os.path.dirname(self.registry_path) or "."
        os.makedirs(dir_name, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=2)
                f.write("\n")
            os.replace(tmp_path, self.registry_path)
        except Exception:
            # Clean up temp file on failure
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
