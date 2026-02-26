import asyncio
import json
import os
from pathlib import Path
from typing import Callable, Awaitable, Optional

from backend.models import Task, TaskMode
from backend.worktree import (
    create_worktree,
    remove_worktree,
    cleanup_branch,
    WorktreeError,
)


class ClaudeCodeExecutor:
    def __init__(
        self,
        max_workers: int = 3,
        base_repo: str = "/home/ubuntu/project",
        log_dir: str = "/home/ubuntu/task-logs",
        worktree_dir: str = "/home/ubuntu/worktrees",
    ):
        self.max_workers = max_workers
        self.base_repo = base_repo
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.worktree_dir = worktree_dir
        self.active_tasks: dict[int, asyncio.subprocess.Process] = {}
        # Track worktree info per task for cleanup
        self._task_worktrees: dict[int, tuple[str, str]] = {}  # task_id -> (branch, path)

    def _worktree_info(self, task: Task) -> tuple[str, str]:
        """Return (branch, worktree_path) for a task."""
        branch = f"task-{task.id}-{task.title[:20].replace(' ', '-')}"
        path = os.path.join(self.worktree_dir, branch)
        return branch, path

    async def execute_task(
        self,
        task: Task,
        on_output: Callable[[int, str], Awaitable[None]],
        on_complete: Callable[..., Awaitable[None]],
    ):
        # 1. Create worktree via worktree module
        branch, worktree_path = self._worktree_info(task)
        self._task_worktrees[task.id] = (branch, worktree_path)

        try:
            await create_worktree(self.base_repo, branch, worktree_path)
        except WorktreeError as e:
            self._task_worktrees.pop(task.id, None)
            await on_complete(task.id, exit_code=1, error=str(e))
            return

        # 2. Build prompt (Plan mode via prompt engineering, not --plan flag which doesn't exist)
        prompt = task.prompt
        if task.mode == TaskMode.PLAN:
            prompt = (
                "IMPORTANT: Before writing any code, output a detailed implementation "
                "plan as markdown. After the plan, write '---PLAN END---', then implement.\n\n"
            ) + prompt

        # 3. Build command
        cmd = [
            "claude",
            "-p", prompt,
            "--dangerously-skip-permissions",
            "--output-format", "json",
        ]

        # 4. Launch subprocess
        process = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=worktree_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"},
        )
        self.active_tasks[task.id] = process

        # 5. Stream stdout line-by-line, write to file log
        log_path = self.log_dir / f"task-{task.id}.log"
        output_chunks: list[str] = []
        with open(log_path, "w") as log_file:
            async for raw_line in process.stdout:
                decoded = raw_line.decode().rstrip("\n")
                output_chunks.append(decoded)
                log_file.write(decoded + "\n")
                log_file.flush()
                await on_output(task.id, decoded)

        # 6. Wait for process and collect stderr
        await process.wait()
        stderr_bytes = await process.stderr.read()
        stderr = stderr_bytes.decode()

        full_output = "\n".join(output_chunks)

        # 7. Parse JSON output for result text + token usage (Gotcha 5/6)
        result_text = full_output
        input_tokens: Optional[int] = None
        output_tokens: Optional[int] = None
        cost_usd: Optional[float] = None
        plan_text: Optional[str] = None

        try:
            parsed = json.loads(full_output)
            result_text = parsed.get("result", full_output)
            usage = parsed.get("usage", {})
            input_tokens = usage.get("input_tokens")
            output_tokens = usage.get("output_tokens")
            cost_usd = parsed.get("total_cost_usd")
        except (json.JSONDecodeError, AttributeError):
            pass

        # Extract plan section if present
        if "---PLAN END---" in result_text:
            parts = result_text.split("---PLAN END---", 1)
            plan_text = parts[0].strip()

        self.active_tasks.pop(task.id, None)

        # 8. Cleanup worktree on failure
        exit_code = process.returncode
        if exit_code != 0:
            await self._cleanup_worktree(task.id)

        await on_complete(
            task.id,
            exit_code=exit_code,
            output=result_text,
            error=stderr if exit_code != 0 else None,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
            plan=plan_text,
        )

    async def cancel_task(self, task_id: int) -> None:
        proc = self.active_tasks.get(task_id)
        if proc is not None:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
            self.active_tasks.pop(task_id, None)
        # Cleanup worktree on cancel
        await self._cleanup_worktree(task_id)

    async def _cleanup_worktree(self, task_id: int) -> None:
        """Remove worktree and branch for a task if they exist."""
        info = self._task_worktrees.pop(task_id, None)
        if info is None:
            return
        branch, path = info
        try:
            await remove_worktree(self.base_repo, path)
        except Exception:
            pass
        try:
            await cleanup_branch(self.base_repo, branch)
        except Exception:
            pass

    async def cleanup_task_worktree(self, task_id: int) -> None:
        """Public method for explicit worktree cleanup (e.g., after successful merge)."""
        await self._cleanup_worktree(task_id)

    def get_task_worktree_info(self, task_id: int) -> Optional[tuple[str, str]]:
        """Get (branch, path) for an active task's worktree."""
        return self._task_worktrees.get(task_id)
