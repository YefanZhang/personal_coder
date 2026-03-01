import asyncio
import pytest
import pytest_asyncio
import tempfile
import os
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime

from backend.scheduler import TaskScheduler
from backend.database import Database
from backend.models import Task, TaskStatus, TaskMode, TaskPriority


def make_task(
    task_id: int = 1,
    title: str = "Task",
    prompt: str = "Do it",
    status: TaskStatus = TaskStatus.PENDING,
    priority: TaskPriority = TaskPriority.MEDIUM,
    depends_on: list[int] = None,
) -> Task:
    return Task(
        id=task_id,
        title=title,
        prompt=prompt,
        status=status,
        priority=priority,
        depends_on=depends_on or [],
        created_at=datetime.now(),
    )


@pytest_asyncio.fixture
async def real_db():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    db = Database(db_path)
    await db.init()
    yield db
    await db.close()
    os.unlink(db_path)


def make_scheduler(db=None, max_concurrent=3):
    executor = MagicMock()
    executor.cancel_task = AsyncMock()
    executor.execute_task = AsyncMock()
    ws = MagicMock()
    ws.broadcast = AsyncMock()
    if db is None:
        db = MagicMock()
        db.count_tasks = AsyncMock(return_value=0)
        db.get_next_pending_task = AsyncMock(return_value=None)
        db.get_task = AsyncMock(return_value=None)
        db.update_task = AsyncMock()
        db.add_log = AsyncMock()
        db.list_tasks = AsyncMock(return_value=[])
    scheduler = TaskScheduler(executor=executor, db=db, ws_manager=ws, max_concurrent=max_concurrent)
    return scheduler, executor, ws


# ── Priority ordering test (uses real DB) ─────────────────────────────────────

async def test_priority_ordering(real_db):
    low = await real_db.create_task(title="low", prompt="p", priority="low")
    high = await real_db.create_task(title="high", prompt="p", priority="high")
    medium = await real_db.create_task(title="medium", prompt="p", priority="medium")
    urgent = await real_db.create_task(title="urgent", prompt="p", priority="urgent")

    dispatched_order = []
    scheduler, executor, ws = make_scheduler(db=real_db, max_concurrent=1)

    async def fake_execute(task, on_output, on_complete):
        dispatched_order.append(task.title)
        await on_complete(task.id, exit_code=0, output="done")

    executor.execute_task.side_effect = fake_execute

    # Run scheduler for just enough iterations to dispatch 4 tasks
    async def limited_start():
        for _ in range(8):
            active_count = await real_db.count_tasks(status=TaskStatus.IN_PROGRESS)
            if active_count < scheduler.max_concurrent:
                next_task = await real_db.get_next_pending_task()
                if next_task and await scheduler._dependencies_met(next_task):
                    await scheduler._dispatch(next_task)
            await asyncio.sleep(0)

    await limited_start()
    assert dispatched_order == ["urgent", "high", "medium", "low"]


# ── Dependency blocking test ──────────────────────────────────────────────────

async def test_dependency_blocking(real_db):
    t1 = await real_db.create_task(title="T1", prompt="p", priority="medium")
    t2 = await real_db.create_task(
        title="T2", prompt="p", priority="medium", depends_on=[t1.id]
    )
    scheduler, executor, ws = make_scheduler(db=real_db, max_concurrent=3)

    # T1 is pending, T2 depends on T1 → T2 should not be dispatched yet
    # get_next_pending_task returns T1 first (same priority, earlier created_at)
    next_task = await real_db.get_next_pending_task()
    assert next_task.id == t1.id

    # With T1 in_progress, T2 shouldn't dispatch
    await real_db.update_task(t1.id, status=TaskStatus.IN_PROGRESS)
    next_task = await real_db.get_next_pending_task()
    assert next_task.id == t2.id
    assert not await scheduler._dependencies_met(next_task)

    # After T1 completes, T2 should be eligible
    await real_db.update_task(t1.id, status=TaskStatus.COMPLETED)
    assert await scheduler._dependencies_met(next_task)


# ── Concurrent limit test ─────────────────────────────────────────────────────

async def test_concurrent_limit():
    scheduler, executor, ws = make_scheduler(max_concurrent=2)

    task1 = make_task(1, status=TaskStatus.PENDING)
    task2 = make_task(2, status=TaskStatus.PENDING)

    scheduler.db.count_tasks = AsyncMock(return_value=2)  # already at limit
    scheduler.db.get_next_pending_task = AsyncMock(return_value=task1)

    # Run one scheduler iteration
    active_count = await scheduler.db.count_tasks(status=TaskStatus.IN_PROGRESS)
    if active_count < scheduler.max_concurrent:
        next_task = await scheduler.db.get_next_pending_task()
        if next_task:
            await scheduler._dispatch(next_task)

    # Should NOT dispatch because active_count (2) == max_concurrent (2)
    scheduler.db.update_task.assert_not_called()


# ── _on_output broadcasts and logs ────────────────────────────────────────────

async def test_on_output_broadcasts_and_logs():
    scheduler, executor, ws = make_scheduler()
    await scheduler._on_output(task_id=5, chunk="hello output")
    scheduler.db.add_log.assert_called_once_with(5, "info", "hello output", raw_output="hello output")
    ws.broadcast.assert_called_once_with(5, {"type": "output", "data": "hello output"})


# ── _on_complete marks completed ──────────────────────────────────────────────

async def test_on_complete_success():
    scheduler, executor, ws = make_scheduler()
    await scheduler._on_complete(task_id=3, exit_code=0, output="result", input_tokens=10, output_tokens=5, cost_usd=0.001)
    call_kwargs = scheduler.db.update_task.call_args.kwargs
    assert call_kwargs["status"] == TaskStatus.COMPLETED
    assert call_kwargs["output"] == "result"
    assert call_kwargs["input_tokens"] == 10
    ws.broadcast.assert_called_once()
    broadcast_data = ws.broadcast.call_args.args[1]
    assert broadcast_data["status"] == TaskStatus.COMPLETED


async def test_on_complete_failure():
    scheduler, executor, ws = make_scheduler()
    await scheduler._on_complete(task_id=7, exit_code=1, error="boom")
    call_kwargs = scheduler.db.update_task.call_args.kwargs
    assert call_kwargs["status"] == TaskStatus.FAILED
    assert call_kwargs["error"] == "boom"


# ── Plan mode goes to REVIEW ────────────────────────────────────────────────

async def test_on_complete_plan_mode_sets_review_status():
    scheduler, executor, ws = make_scheduler()
    scheduler.db.add_plan = AsyncMock()
    await scheduler._on_complete(
        task_id=10, exit_code=0, output="plan text",
        plan="the plan", is_plan_mode=True,
    )
    call_kwargs = scheduler.db.update_task.call_args.kwargs
    assert call_kwargs["status"] == TaskStatus.REVIEW
    assert call_kwargs["plan"] == "the plan"
    # Plan version should be stored
    scheduler.db.add_plan.assert_called_once_with(10, "the plan")


async def test_on_complete_plan_mode_failure_still_fails():
    scheduler, executor, ws = make_scheduler()
    scheduler.db.add_plan = AsyncMock()
    await scheduler._on_complete(
        task_id=10, exit_code=1, error="fail",
        plan=None, is_plan_mode=True,
    )
    call_kwargs = scheduler.db.update_task.call_args.kwargs
    assert call_kwargs["status"] == TaskStatus.FAILED


# ── cancel_task delegates to executor ────────────────────────────────────────

async def test_cancel_task():
    scheduler, executor, ws = make_scheduler()
    await scheduler.cancel_task(42)
    executor.cancel_task.assert_called_once_with(42)
