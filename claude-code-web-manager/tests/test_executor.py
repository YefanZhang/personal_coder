import asyncio
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from backend.executor import ClaudeCodeExecutor
from backend.models import Task, TaskStatus, TaskMode, TaskPriority
from backend.worktree import WorktreeError
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


# -- Helpers for building stream-json NDJSON lines --

def make_system_event(model="claude-test"):
    return json.dumps({"type": "system", "subtype": "init", "model": model, "tools": []})


def make_assistant_event(text):
    return json.dumps({
        "type": "assistant",
        "message": {
            "content": [{"type": "text", "text": text}],
            "usage": {"input_tokens": 10, "output_tokens": 5},
        },
    })


def make_tool_use_event(name, tool_input):
    return json.dumps({
        "type": "assistant",
        "message": {
            "content": [{"type": "tool_use", "name": name, "input": tool_input}],
        },
    })


def make_result_event(result="", input_tokens=100, output_tokens=50, cost=0.001):
    return json.dumps({
        "type": "result",
        "subtype": "success",
        "result": result,
        "total_cost_usd": cost,
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
    })


class FakeStdout:
    """Mock stdout that supports `.read(n)` returning chunks then b""."""

    def __init__(self, data: bytes):
        self._data = data
        self._pos = 0

    async def read(self, n: int = -1) -> bytes:
        if self._pos >= len(self._data):
            return b""
        end = len(self._data) if n < 0 else min(self._pos + n, len(self._data))
        chunk = self._data[self._pos:end]
        self._pos = end
        return chunk


class FakeProcess:
    """Minimal asyncio.subprocess.Process mock."""

    def __init__(self, stdout_lines: list[str], returncode: int = 0, stderr: str = ""):
        self.returncode = returncode
        self.pid = 12345
        self._stdout_lines = stdout_lines
        self._stderr = stderr.encode()
        # Build raw bytes: each line terminated by \n, as NDJSON would be
        self.stdout = FakeStdout(
            b"".join((line + "\n").encode() for line in stdout_lines)
        )
        self.stderr = AsyncMock()
        self.stderr.read = AsyncMock(return_value=self._stderr)

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
def executor(tmp_log_dir, tmp_path):
    return ClaudeCodeExecutor(
        max_workers=3,
        base_repo="/fake/repo",
        log_dir=tmp_log_dir,
        worktree_dir=str(tmp_path / "worktrees"),
    )


async def test_execute_task_calls_on_output_and_on_complete(executor, tmp_log_dir):
    """stream-json format: system + assistant + result events."""
    ndjson_lines = [
        make_system_event(),
        make_assistant_event("Task done"),
        make_result_event(result="Task done", input_tokens=100, output_tokens=50, cost=0.001),
    ]
    fake_proc = FakeProcess(stdout_lines=ndjson_lines)

    output_calls = []
    complete_kwargs = {}

    async def on_output(task_id, text):
        output_calls.append(text)

    async def on_complete(task_id, **kwargs):
        complete_kwargs.update(kwargs)
        complete_kwargs["task_id"] = task_id

    with patch("backend.executor.create_worktree", new_callable=AsyncMock) as mock_create_wt, \
         patch("asyncio.create_subprocess_exec", return_value=fake_proc), \
         patch("backend.executor.shutil.which", return_value="/usr/bin/claude"):
        mock_create_wt.return_value = "/fake/worktree"
        task = make_task()
        await executor.execute_task(task, on_output, on_complete)

    # System event produces a session message, assistant event produces text
    assert any("Task done" in c for c in output_calls)
    assert complete_kwargs["exit_code"] == 0
    assert complete_kwargs["output"] == "Task done"
    assert complete_kwargs["input_tokens"] == 100
    assert complete_kwargs["output_tokens"] == 50
    assert complete_kwargs["cost_usd"] == 0.001
    assert complete_kwargs["error"] is None


async def test_execute_task_writes_log_file(executor, tmp_log_dir):
    ndjson_lines = [
        make_system_event(),
        make_result_event(result="done"),
    ]
    fake_proc = FakeProcess(stdout_lines=ndjson_lines, returncode=0)

    with patch("backend.executor.create_worktree", new_callable=AsyncMock) as mock_create_wt, \
         patch("asyncio.create_subprocess_exec", return_value=fake_proc), \
         patch("backend.executor.shutil.which", return_value="/usr/bin/claude"):
        mock_create_wt.return_value = "/fake/worktree"
        task = make_task(task_id=42)
        await executor.execute_task(task, AsyncMock(), AsyncMock())

    log_file = Path(tmp_log_dir) / "task-42.log"
    assert log_file.exists()
    content = log_file.read_text()
    # Log file should contain the raw NDJSON lines
    assert '"type": "system"' in content or '"type":"system"' in content


async def test_worktree_failure_calls_on_complete_with_error(executor):
    on_complete = AsyncMock()

    with patch("backend.executor.create_worktree", new_callable=AsyncMock) as mock_create_wt:
        mock_create_wt.side_effect = WorktreeError("worktree creation failed: permission denied")
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
        return FakeProcess(stdout_lines=[make_result_event()], returncode=0)

    with patch("backend.executor.create_worktree", new_callable=AsyncMock) as mock_create_wt, \
         patch("asyncio.create_subprocess_exec", side_effect=fake_exec), \
         patch("backend.executor.shutil.which", return_value="/usr/bin/claude"):
        mock_create_wt.return_value = "/fake/worktree"
        task = make_task(prompt="Write a function", mode=TaskMode.PLAN)
        await executor.execute_task(task, AsyncMock(), AsyncMock())

    all_args = " ".join(str(a) for a in captured_cmd)
    # Plan mode uses structured plan prompt
    assert "PLAN MODE" in all_args
    assert "Do NOT execute any code changes" in all_args
    assert "Write a function" in all_args
    # Plan mode should NOT have workflow suffix (no implementation)
    assert "Post-Implementation Workflow" not in all_args
    # Plan mode should have --max-turns 1
    assert "--max-turns" in all_args
    assert "1" in captured_cmd


async def test_workflow_instructions_appended_to_prompt(executor):
    """Every task prompt includes git workflow instructions with correct branch/path/repo values."""
    captured_cmd = []

    async def fake_exec(*args, **kwargs):
        captured_cmd.extend(args)
        return FakeProcess(stdout_lines=[make_result_event()], returncode=0)

    with patch("backend.executor.create_worktree", new_callable=AsyncMock) as mock_create_wt, \
         patch("asyncio.create_subprocess_exec", side_effect=fake_exec), \
         patch("backend.executor.shutil.which", return_value="/usr/bin/claude"):
        mock_create_wt.return_value = "/fake/worktree"
        task = make_task(task_id=42, title="My cool task", prompt="Do something")
        await executor.execute_task(task, AsyncMock(), AsyncMock())

    # The prompt is passed via -p flag; reconstruct it from captured args
    all_args = " ".join(str(a) for a in captured_cmd)

    # Workflow section is present
    assert "Post-Implementation Workflow" in all_args
    assert "Claude Code Web Manager" in all_args

    # Git commands reference correct values
    assert 'git commit -m "[task-42] My cool task"' in all_args
    assert "git push origin main" in all_args

    # Branch and path values are interpolated
    branch, worktree_path = executor._worktree_info(task)
    assert f"Your current branch is: {branch}" in all_args
    assert f"The main repository is at: {executor.base_repo}" in all_args

    # CLAUDE.md override instruction is present
    assert 'Ignore the "Task Lifecycle"' in all_args


async def test_non_json_output_streams_raw_text(executor):
    """Non-JSON lines are streamed as raw output."""
    fake_proc = FakeProcess(stdout_lines=["plain text output"], returncode=0)
    output_calls = []
    complete_kwargs = {}

    async def on_output(task_id, text):
        output_calls.append(text)

    async def on_complete(task_id, **kwargs):
        complete_kwargs.update(kwargs)

    with patch("backend.executor.create_worktree", new_callable=AsyncMock) as mock_create_wt, \
         patch("asyncio.create_subprocess_exec", return_value=fake_proc), \
         patch("backend.executor.shutil.which", return_value="/usr/bin/claude"):
        mock_create_wt.return_value = "/fake/worktree"
        task = make_task()
        await executor.execute_task(task, on_output, on_complete)

    # Non-JSON lines are streamed as-is via on_output
    assert "plain text output" in output_calls
    # No result event means output is "(no output)" or stderr
    assert complete_kwargs["exit_code"] == 0


async def test_plan_section_extracted_with_delimiter(executor):
    """Backward compat: ---PLAN END--- delimiter still works in execute mode."""
    result_with_plan = "Here is my plan\n---PLAN END---\nHere is the implementation"
    ndjson_lines = [
        make_assistant_event("planning..."),
        make_result_event(result=result_with_plan, input_tokens=10, output_tokens=5, cost=0.0001),
    ]
    fake_proc = FakeProcess(stdout_lines=ndjson_lines, returncode=0)
    complete_kwargs = {}

    async def on_complete(task_id, **kwargs):
        complete_kwargs.update(kwargs)

    with patch("backend.executor.create_worktree", new_callable=AsyncMock) as mock_create_wt, \
         patch("asyncio.create_subprocess_exec", return_value=fake_proc), \
         patch("backend.executor.shutil.which", return_value="/usr/bin/claude"):
        mock_create_wt.return_value = "/fake/worktree"
        task = make_task()
        await executor.execute_task(task, AsyncMock(), on_complete)

    assert complete_kwargs["plan"] == "Here is my plan"


async def test_plan_mode_entire_output_is_plan(executor):
    """In plan mode, the entire result_text is treated as the plan."""
    plan_output = "## Files to Modify\n- src/main.py\n\n## Steps\n1. Do X\n2. Do Y"
    ndjson_lines = [
        make_result_event(result=plan_output, input_tokens=10, output_tokens=5, cost=0.0001),
    ]
    fake_proc = FakeProcess(stdout_lines=ndjson_lines, returncode=0)
    complete_kwargs = {}

    async def on_complete(task_id, **kwargs):
        complete_kwargs.update(kwargs)

    with patch("backend.executor.create_worktree", new_callable=AsyncMock) as mock_create_wt, \
         patch("asyncio.create_subprocess_exec", return_value=fake_proc), \
         patch("backend.executor.shutil.which", return_value="/usr/bin/claude"):
        mock_create_wt.return_value = "/fake/worktree"
        task = make_task(mode=TaskMode.PLAN)
        await executor.execute_task(task, AsyncMock(), on_complete)

    assert complete_kwargs["plan"] == plan_output
    assert complete_kwargs["is_plan_mode"] is True


async def test_execute_mode_with_approved_plan(executor):
    """Execute mode includes approved plan in the prompt."""
    captured_cmd = []

    async def fake_exec(*args, **kwargs):
        captured_cmd.extend(args)
        return FakeProcess(stdout_lines=[make_result_event()], returncode=0)

    with patch("backend.executor.create_worktree", new_callable=AsyncMock) as mock_create_wt, \
         patch("asyncio.create_subprocess_exec", side_effect=fake_exec), \
         patch("backend.executor.shutil.which", return_value="/usr/bin/claude"):
        mock_create_wt.return_value = "/fake/worktree"
        task = make_task(prompt="Build a widget")
        # Simulate approved plan
        task.plan = "Step 1: Create widget.py\nStep 2: Add tests"
        await executor.execute_task(task, AsyncMock(), AsyncMock())

    all_args = " ".join(str(a) for a in captured_cmd)
    assert "Execute the following approved plan exactly" in all_args
    assert "Step 1: Create widget.py" in all_args
    assert "Build a widget" in all_args
    # Should have workflow suffix in execute mode
    assert "Post-Implementation Workflow" in all_args


async def test_tool_use_events_stream_summaries(executor):
    """Tool use events produce human-readable summaries."""
    ndjson_lines = [
        make_tool_use_event("Bash", {"command": "npm test"}),
        make_tool_use_event("Edit", {"file_path": "/src/main.py"}),
        make_tool_use_event("Read", {"file_path": "/README.md"}),
        make_result_event(result="done"),
    ]
    fake_proc = FakeProcess(stdout_lines=ndjson_lines, returncode=0)
    output_calls = []

    async def on_output(task_id, text):
        output_calls.append(text)

    with patch("backend.executor.create_worktree", new_callable=AsyncMock) as mock_create_wt, \
         patch("asyncio.create_subprocess_exec", return_value=fake_proc), \
         patch("backend.executor.shutil.which", return_value="/usr/bin/claude"):
        mock_create_wt.return_value = "/fake/worktree"
        task = make_task()
        await executor.execute_task(task, on_output, AsyncMock())

    assert any("Running: npm test" in c for c in output_calls)
    assert any("Edit: /src/main.py" in c for c in output_calls)
    assert any("Reading: /README.md" in c for c in output_calls)


async def test_cancel_task_terminates_process(executor):
    task = make_task(task_id=99)
    mock_proc = MagicMock()
    executor.active_tasks[99] = mock_proc

    with patch("backend.executor.remove_worktree", new_callable=AsyncMock), \
         patch("backend.executor.cleanup_branch", new_callable=AsyncMock):
        await executor.cancel_task(99)

    mock_proc.terminate.assert_called_once()
    assert 99 not in executor.active_tasks


async def test_cancel_task_noop_for_unknown_id(executor):
    with patch("backend.executor.remove_worktree", new_callable=AsyncMock), \
         patch("backend.executor.cleanup_branch", new_callable=AsyncMock):
        await executor.cancel_task(99999)


async def test_failed_task_cleans_up_worktree(executor):
    """When a task fails (non-zero exit), worktree should be cleaned up."""
    fake_proc = FakeProcess(stdout_lines=["error output"], returncode=1, stderr="fatal error")

    complete_kwargs = {}

    async def on_complete(task_id, **kwargs):
        complete_kwargs.update(kwargs)

    with patch("backend.executor.create_worktree", new_callable=AsyncMock) as mock_create_wt, \
         patch("backend.executor.remove_worktree", new_callable=AsyncMock) as mock_remove_wt, \
         patch("backend.executor.cleanup_branch", new_callable=AsyncMock) as mock_cleanup_br, \
         patch("asyncio.create_subprocess_exec", return_value=fake_proc), \
         patch("backend.executor.shutil.which", return_value="/usr/bin/claude"):
        mock_create_wt.return_value = "/fake/worktree"
        task = make_task(task_id=7)
        await executor.execute_task(task, AsyncMock(), on_complete)

    assert complete_kwargs["exit_code"] == 1
    mock_remove_wt.assert_called_once()
    mock_cleanup_br.assert_called_once()


async def test_cancel_cleans_up_worktree(executor):
    """When a task is cancelled, its worktree should be cleaned up."""
    task = make_task(task_id=5)
    mock_proc = MagicMock()
    executor.active_tasks[5] = mock_proc
    executor._task_worktrees[5] = ("task-5-branch", "/fake/wt")

    with patch("backend.executor.remove_worktree", new_callable=AsyncMock) as mock_remove_wt, \
         patch("backend.executor.cleanup_branch", new_callable=AsyncMock) as mock_cleanup_br:
        await executor.cancel_task(5)

    mock_proc.terminate.assert_called_once()
    mock_remove_wt.assert_called_once_with("/fake/repo", "/fake/wt")
    mock_cleanup_br.assert_called_once_with("/fake/repo", "task-5-branch")


async def test_get_task_worktree_info(executor):
    executor._task_worktrees[10] = ("branch-10", "/path/to/wt")
    info = executor.get_task_worktree_info(10)
    assert info == ("branch-10", "/path/to/wt")
    assert executor.get_task_worktree_info(999) is None
