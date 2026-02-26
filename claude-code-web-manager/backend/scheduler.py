import asyncio
from datetime import datetime
from typing import Optional

from backend.models import TaskStatus, TaskMode


class TaskScheduler:
    def __init__(self, executor, db, ws_manager, max_concurrent: int = 3):
        self.executor = executor
        self.db = db
        self.ws_manager = ws_manager
        self.max_concurrent = max_concurrent
        self._running = False

    async def start(self):
        self._running = True
        while self._running:
            try:
                active_count = await self.db.count_tasks(status=TaskStatus.IN_PROGRESS)
                if active_count < self.max_concurrent:
                    next_task = await self.db.get_next_pending_task()
                    if next_task and await self._dependencies_met(next_task):
                        await self._dispatch(next_task)
            except Exception as e:
                # Log but don't crash the scheduler loop
                print(f"[scheduler] error in loop: {e}")
            await asyncio.sleep(2)

    def stop(self):
        self._running = False

    async def _dependencies_met(self, task) -> bool:
        for dep_id in task.depends_on:
            dep = await self.db.get_task(dep_id)
            if dep is None or dep.status != TaskStatus.COMPLETED:
                return False
        return True

    async def _dispatch(self, task) -> None:
        await self.db.update_task(
            task.id,
            status=TaskStatus.IN_PROGRESS,
            started_at=datetime.now(),
        )
        await self.ws_manager.broadcast(
            task.id, {"type": "status", "status": TaskStatus.IN_PROGRESS}
        )
        asyncio.create_task(
            self.executor.execute_task(task, self._on_output, self._on_complete)
        )

    async def _on_output(self, task_id: int, chunk: str) -> None:
        await self.db.add_log(task_id, "info", chunk, raw_output=chunk)
        await self.ws_manager.broadcast(task_id, {"type": "output", "data": chunk})

    async def _on_complete(
        self,
        task_id: int,
        exit_code: int,
        output: str = "",
        error: Optional[str] = None,
        input_tokens: Optional[int] = None,
        output_tokens: Optional[int] = None,
        cost_usd: Optional[float] = None,
        plan: Optional[str] = None,
    ) -> None:
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
            plan=plan,
        )
        await self.ws_manager.broadcast(
            task_id, {"type": "complete", "status": status}
        )

    async def cancel_task(self, task_id: int) -> None:
        await self.executor.cancel_task(task_id)
