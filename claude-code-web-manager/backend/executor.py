import asyncio
import json
import os
import shutil
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
        base_repo: str = "/home/ubuntu/personal_coder",
        log_dir: str = "/home/ubuntu/task-logs",
        worktree_dir: str = "/home/ubuntu/personal_coder-worktrees",
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

    def _build_subprocess_env(self) -> dict[str, str]:
        """Build a clean environment for the claude subprocess."""
        env = {**os.environ, "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}
        # Remove CLAUDECODE to avoid nesting detection
        env.pop("CLAUDECODE", None)
        return env

    async def execute_task(
        self,
        task: Task,
        on_output: Callable[[int, str], Awaitable[None]],
        on_complete: Callable[..., Awaitable[None]],
    ):
        print(f"[executor] task {task.id}: starting execution (title={task.title!r})")

        # 1. Create worktree via worktree module
        branch, worktree_path = self._worktree_info(task)
        self._task_worktrees[task.id] = (branch, worktree_path)

        try:
            print(f"[executor] task {task.id}: creating worktree branch={branch} path={worktree_path}")
            await create_worktree(self.base_repo, branch, worktree_path)
            print(f"[executor] task {task.id}: worktree created successfully")
        except Exception as e:
            print(f"[executor] task {task.id}: worktree creation failed: {e}")
            self._task_worktrees.pop(task.id, None)
            await on_complete(task.id, exit_code=1, error=f"worktree creation failed: {e}")
            return

        try:
            # 2. Build prompt (Plan mode via prompt engineering, not --plan flag which doesn't exist)
            prompt = task.prompt
            if task.mode == TaskMode.PLAN:
                prompt = (
                    "IMPORTANT: Before writing any code, output a detailed implementation "
                    "plan as markdown. After the plan, write '---PLAN END---', then implement.\n\n"
                ) + prompt

            # Append git workflow instructions so claude handles commit/merge/push
            workflow_suffix = f"""

## Post-Implementation Workflow
IMPORTANT: You are being run by the Claude Code Web Manager, not the task orchestrator.
Ignore the "Task Lifecycle" and "Strict Rules" sections in CLAUDE.md — those steps
(claiming tasks, updating dev-tasks.json, PROGRESS.md, cleanup) are handled by the web manager.
Focus on implementing the requested changes, then follow the git steps below.

After completing your implementation, you MUST follow these git steps:
1. Stage and commit all changes:
   git add .
   git commit -m "[task-{task.id}] {task.title}"
2. Merge your branch into main from the base repo:
   cd {self.base_repo}
   git merge {branch}
3. Push to origin:
   git push origin main

If any git step fails, report the error clearly but do not retry more than once.
Your current branch is: {branch}
Your working directory is: {worktree_path}
The main repository is at: {self.base_repo}
"""
            prompt = prompt + workflow_suffix

            # 3. Build command — resolve claude to absolute path
            claude_path = shutil.which("claude")
            if not claude_path:
                raise FileNotFoundError("claude CLI not found in PATH")

            # Use stream-json for real-time NDJSON streaming (requires --verbose)
            cmd = [
                claude_path,
                "-p", prompt,
                "--dangerously-skip-permissions",
                "--output-format", "stream-json",
                "--verbose",
            ]

            # 4. Launch subprocess
            print(f"[executor] task {task.id}: launching subprocess: {claude_path}")
            process = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=worktree_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._build_subprocess_env(),
            )
            self.active_tasks[task.id] = process
            print(f"[executor] task {task.id}: subprocess started (pid={process.pid})")

            # 5. Stream NDJSON stdout line-by-line, parse each event
            log_path = self.log_dir / f"task-{task.id}.log"
            parsed: dict = {}  # populated by _process_ndjson_line

            with open(log_path, "w") as log_file:
                # Read raw chunks instead of lines to avoid asyncio's 64KB
                # StreamReader limit.  NDJSON lines can be arbitrarily large
                # (e.g. when Claude reads a big file, the JSON event for that
                # read can exceed 64KB).  We accumulate chunks in a buffer and
                # split on newlines ourselves — no upper bound on line length.
                buf = b""
                while True:
                    chunk = await process.stdout.read(65536)
                    if not chunk:
                        break
                    buf += chunk
                    while b"\n" in buf:
                        raw_line, buf = buf.split(b"\n", 1)
                        decoded = raw_line.decode().rstrip()
                        if not decoded:
                            continue
                        log_file.write(decoded + "\n")
                        log_file.flush()
                        await self._process_ndjson_line(
                            decoded, task.id, on_output, parsed,
                        )

                # Flush any trailing data without a final newline
                if buf.strip():
                    decoded = buf.decode().rstrip()
                    log_file.write(decoded + "\n")
                    log_file.flush()
                    await self._process_ndjson_line(
                        decoded, task.id, on_output, parsed,
                    )

            # 6. Wait for process and collect stderr
            await process.wait()
            stderr_bytes = await process.stderr.read()
            stderr = stderr_bytes.decode()

            result_text = parsed.get("result_text")
            input_tokens = parsed.get("input_tokens")
            output_tokens = parsed.get("output_tokens")
            cost_usd = parsed.get("cost_usd")
            plan_text = parsed.get("plan_text")

            if result_text is None:
                result_text = stderr or "(no output)"

            print(f"[executor] task {task.id}: subprocess exited (code={process.returncode}, result_len={len(result_text)}, stderr_len={len(stderr)})")

            # Extract plan section if present
            if "---PLAN END---" in result_text:
                plan_text = result_text.split("---PLAN END---", 1)[0].strip()

            self.active_tasks.pop(task.id, None)

            # 7. Cleanup worktree on failure
            exit_code = process.returncode
            if exit_code != 0:
                print(f"[executor] task {task.id}: cleaning up worktree after failure")
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
        except Exception as e:
            print(f"[executor] task {task.id}: unhandled exception: {e}")
            self.active_tasks.pop(task.id, None)
            await self._cleanup_worktree(task.id)
            await on_complete(task.id, exit_code=1, error=f"execution failed: {e}")

    async def _process_ndjson_line(
        self,
        decoded: str,
        task_id: int,
        on_output: Callable,
        parsed: dict,
    ) -> None:
        """Parse a single NDJSON line and dispatch to on_output / collect results."""
        try:
            event = json.loads(decoded)
        except json.JSONDecodeError:
            await on_output(task_id, decoded)
            return

        event_type = event.get("type")

        if event_type == "assistant":
            msg = event.get("message", {})
            for block in msg.get("content", []):
                if block.get("type") == "text":
                    await on_output(task_id, block["text"])
                elif block.get("type") == "tool_use":
                    tool_name = block.get("name", "tool")
                    tool_input = block.get("input", {})
                    summary = f"[Using {tool_name}]"
                    if tool_name == "Bash" and "command" in tool_input:
                        summary = f"[Running: {tool_input['command'][:100]}]"
                    elif tool_name in ("Edit", "Write") and "file_path" in tool_input:
                        summary = f"[{tool_name}: {tool_input['file_path']}]"
                    elif tool_name == "Read" and "file_path" in tool_input:
                        summary = f"[Reading: {tool_input['file_path']}]"
                    await on_output(task_id, summary)

        elif event_type == "result":
            parsed["result_text"] = event.get("result", "")
            parsed["cost_usd"] = event.get("total_cost_usd")
            usage = event.get("usage", {})
            parsed["input_tokens"] = usage.get("input_tokens")
            parsed["output_tokens"] = usage.get("output_tokens")

        elif event_type == "system":
            model = event.get("model", "unknown")
            await on_output(task_id, f"[Session started — model: {model}]")

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
