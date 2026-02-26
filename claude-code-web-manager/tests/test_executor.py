import asyncio
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from backend.executor import ClaudeCodeExecutor
from backend.models import Task, TaskStatus, TaskMode, TaskPriority
from datetime import datetime


def make_task(
    task_id: int = 1,
    title: str = "Test task",
    prompt: str = "Do something",
    mode: TaskMode = TaskMode.EXECUTE,
) -> Task:
    return Task(
        id=task_id,
        title=title,
        prompt=prompt,
        status=TaskStatus.PENDING,
        mode=mode,
        priority=TaskPriority.MEDIUM,
        created_at=datetime.now(),
    )


class FakeProcess:
    """Minimal asyncio.subprocess.Process mock."""

    def __init__(self, stdout_lines: list[str], returncode: int = 0, stderr: str = ""):
        self.returncode = returncode
        self._stdout_lines = stdout_lines
        self._stderr = stderr.encode()
        self.stdout = self._make_reader()
        self.stderr = AsyncMock()
        self.stderr.read = AsyncMock(return_value=self._stderr)

    def _make_reader(self):
        async def _aiter():
            for line in self._stdout_lines:
                yield (line + "\n").encode()

        reader = MagicMock()
        reader.__aiter__ = lambda self: _aiter()
        return reader

    async def communicate(self):
        stdout = b"\n".join(l.encode() for l in self._stdout_lines)
        return stdout, self._stderr

    async def wait(self):
        return self.returncode

    def terminate(self):
        pass


@pytest.fixture
def tmp_log_dir(tmp_path):
    return str(tmp_path / "task-logs")


@pytest.fixture
def executor(tmp_log_dir):
    return ClaudeCodeExecutor(
        max_workers=3,
        base_repo="/fake/repo",
        log_dir=tmp_log_dir,
    )


async def test_execute_task_calls_on_output_and_on_complete(executor, tmp_log_dir):
    output_data = json.dumps({
        "result": "Task done",
        "usage": {"input_tokens": 100, "output_tokens": 50},
        "total_cost_usd": 0.001,
    })
    fake_proc = FakeProcess(stdout_lines=[output_data])
    wt_proc = FakeProcess(stdout_lines=[], returncode=0)

    on_output = AsyncMock()
    complete_kwargs = {}

    async def on_complete(task_id, **kwargs):
        complete_kwargs.update(kwargs)
        complete_kwargs["task_id"] = task_id

    with patch("asyncio.create_subprocess_exec") as mock_exec:
        mock_exec.side_effect = [wt_proc, fake_proc]
        task = make_task()
        await executor.execute_task(task, on_output, on_complete)

    on_output.assert_called_once_with(1, output_data)
    assert complete_kwargs["exit_code"] == 0
    assert complete_kwargs["output"] == "Task done"
    assert complete_kwargs["input_tokens"] == 100
    assert complete_kwargs["output_tokens"] == 50
    assert complete_kwargs["cost_usd"] == 0.001
    assert complete_kwargs["error"] is None


async def test_execute_task_writes_log_file(executor, tmp_log_dir):
    output_data = "line1"
    fake_proc = FakeProcess(stdout_lines=[output_data], returncode=0)
    wt_proc = FakeProcess(stdout_lines=[], returncode=0)

    with patch("asyncio.create_subprocess_exec") as mock_exec:
        mock_exec.side_effect = [wt_proc, fake_proc]
        task = make_task(task_id=42)
        await executor.execute_task(task, AsyncMock(), AsyncMock())

    log_file = Path(tmp_log_dir) / "task-42.log"
    assert log_file.exists()
    assert log_file.read_text() == "line1\n"


async def test_worktree_failure_calls_on_complete_with_error(executor):
    wt_proc = FakeProcess(stdout_lines=[], returncode=1, stderr="permission denied")
    on_complete = AsyncMock()

    with patch("asyncio.create_subprocess_exec") as mock_exec:
        mock_exec.side_effect = [wt_proc]
        task = make_task()
        await executor.execute_task(task, AsyncMock(), on_complete)

    on_complete.assert_called_once()
    call_kwargs = on_complete.call_args
    assert call_kwargs.kwargs["exit_code"] == 1
    assert "worktree creation failed" in call_kwargs.kwargs["error"]


async def test_plan_mode_prepends_prefix_to_prompt(executor):
    captured_cmd = []

    async def fake_exec(*args, **kwargs):
        captured_cmd.extend(args)
        if "worktree" in args:
            return FakeProcess(stdout_lines=[], returncode=0)
        return FakeProcess(stdout_lines=[], returncode=0)

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        task = make_task(prompt="Write a function", mode=TaskMode.PLAN)
        await executor.execute_task(task, AsyncMock(), AsyncMock())

    # The second call is the claude invocation; check -p argument
    all_args = " ".join(str(a) for a in captured_cmd)
    assert "IMPORTANT: Before writing any code" in all_args


async def test_non_json_output_uses_raw_text(executor):
    fake_proc = FakeProcess(stdout_lines=["plain text output"], returncode=0)
    wt_proc = FakeProcess(stdout_lines=[], returncode=0)
    complete_kwargs = {}

    async def on_complete(task_id, **kwargs):
        complete_kwargs.update(kwargs)

    with patch("asyncio.create_subprocess_exec") as mock_exec:
        mock_exec.side_effect = [wt_proc, fake_proc]
        task = make_task()
        await executor.execute_task(task, AsyncMock(), on_complete)

    assert complete_kwargs["output"] == "plain text output"
    assert complete_kwargs["input_tokens"] is None
    assert complete_kwargs["cost_usd"] is None


async def test_plan_section_extracted(executor):
    result_with_plan = "Here is my plan\n---PLAN END---\nHere is the implementation"
    output_data = json.dumps({
        "result": result_with_plan,
        "usage": {"input_tokens": 10, "output_tokens": 5},
        "total_cost_usd": 0.0001,
    })
    fake_proc = FakeProcess(stdout_lines=[output_data], returncode=0)
    wt_proc = FakeProcess(stdout_lines=[], returncode=0)
    complete_kwargs = {}

    async def on_complete(task_id, **kwargs):
        complete_kwargs.update(kwargs)

    with patch("asyncio.create_subprocess_exec") as mock_exec:
        mock_exec.side_effect = [wt_proc, fake_proc]
        task = make_task()
        await executor.execute_task(task, AsyncMock(), on_complete)

    assert complete_kwargs["plan"] == "Here is my plan"


async def test_cancel_task_terminates_process(executor):
    task = make_task(task_id=99)
    mock_proc = MagicMock()
    executor.active_tasks[99] = mock_proc
    await executor.cancel_task(99)
    mock_proc.terminate.assert_called_once()
    assert 99 not in executor.active_tasks


async def test_cancel_task_noop_for_unknown_id(executor):
    # Should not raise
    await executor.cancel_task(99999)
