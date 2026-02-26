import asyncio
import os
import tempfile

import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from backend.database import Database
from backend.scheduler import TaskScheduler
from backend.models import TaskStatus


@pytest_asyncio.fixture
async def real_db():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    db = Database(db_path)
    await db.init()
    yield db
    await db.close()
    os.unlink(db_path)


# ── Concurrency stress test ──────────────────────────────────────────────────


async def test_concurrent_stress_5_tasks_max_3(real_db):
    """Fire 5 tasks simultaneously with max_concurrent=3.

    Verify:
      (a) never more than 3 tasks in_progress at once
      (b) tasks 4 and 5 stay pending until a slot opens
      (c) all 5 complete eventually
      (d) no DB corruption
    """
    # Create 5 tasks
    tasks = []
    for i in range(5):
        t = await real_db.create_task(title=f"Task {i+1}", prompt=f"Do task {i+1}")
        tasks.append(t)
    task_ids = [t.id for t in tasks]

    # Concurrency tracking
    concurrent_count = 0
    max_concurrent_seen = 0

    # Per-task synchronization
    started_events = {tid: asyncio.Event() for tid in task_ids}
    complete_events = {tid: asyncio.Event() for tid in task_ids}

    # Mock executor
    mock_executor = MagicMock()
    mock_executor.cancel_task = AsyncMock()

    async def fake_execute(task, on_output, on_complete):
        nonlocal concurrent_count, max_concurrent_seen
        concurrent_count += 1
        max_concurrent_seen = max(max_concurrent_seen, concurrent_count)
        started_events[task.id].set()
        await complete_events[task.id].wait()
        concurrent_count -= 1
        await on_complete(task.id, exit_code=0, output=f"done-{task.id}")

    mock_executor.execute_task = AsyncMock(side_effect=fake_execute)

    ws = MagicMock()
    ws.broadcast = AsyncMock()

    scheduler = TaskScheduler(
        executor=mock_executor, db=real_db, ws_manager=ws,
        max_concurrent=3, poll_interval=0.05,
    )

    scheduler_task = asyncio.create_task(scheduler.start())

    try:
        # Wait for first 3 tasks to start
        await asyncio.wait_for(
            asyncio.gather(*(started_events[tid].wait() for tid in task_ids[:3])),
            timeout=5,
        )

        # (a) Verify max 3 in_progress
        in_progress = await real_db.count_tasks(status=TaskStatus.IN_PROGRESS)
        assert in_progress == 3

        # (b) Tasks 4 and 5 should still be pending
        assert not started_events[task_ids[3]].is_set()
        assert not started_events[task_ids[4]].is_set()
        pending = await real_db.count_tasks(status=TaskStatus.PENDING)
        assert pending == 2

        # Complete task 1 to free a slot
        complete_events[task_ids[0]].set()

        # Wait for task 4 to start
        await asyncio.wait_for(started_events[task_ids[3]].wait(), timeout=5)

        # Still at most 3 concurrent
        in_progress = await real_db.count_tasks(status=TaskStatus.IN_PROGRESS)
        assert in_progress == 3

        # Complete task 2 to free another slot
        complete_events[task_ids[1]].set()

        # Wait for task 5 to start
        await asyncio.wait_for(started_events[task_ids[4]].wait(), timeout=5)

        # Complete all remaining tasks
        for tid in task_ids:
            complete_events[tid].set()

        # Wait for all to complete
        for _ in range(100):
            completed = await real_db.count_tasks(status=TaskStatus.COMPLETED)
            if completed == 5:
                break
            await asyncio.sleep(0.05)

        # (c) All 5 complete
        completed = await real_db.count_tasks(status=TaskStatus.COMPLETED)
        assert completed == 5

        # (a) Never exceeded concurrent limit
        assert max_concurrent_seen <= 3

        # (d) No DB corruption — all tasks have valid state
        all_tasks = await real_db.list_tasks()
        assert len(all_tasks) == 5
        for t in all_tasks:
            assert t.status == TaskStatus.COMPLETED
            assert t.output is not None

    finally:
        scheduler.stop()
        scheduler_task.cancel()
        try:
            await scheduler_task
        except asyncio.CancelledError:
            pass


# ── Startup recovery test ────────────────────────────────────────────────────


async def test_startup_recovery_resets_in_progress(real_db):
    """Server crash simulation: set 2 tasks to in_progress, verify recovery
    resets them to pending."""
    t1 = await real_db.create_task(title="Stuck 1", prompt="p")
    t2 = await real_db.create_task(title="Stuck 2", prompt="p")
    t3 = await real_db.create_task(title="Pending", prompt="p")

    # Simulate crash: tasks stuck in in_progress
    await real_db.update_task(t1.id, status=TaskStatus.IN_PROGRESS)
    await real_db.update_task(t2.id, status=TaskStatus.IN_PROGRESS)

    assert await real_db.count_tasks(status=TaskStatus.IN_PROGRESS) == 2

    # Call the actual recovery function with patched db
    from backend.main import _recover_stuck_tasks
    with patch("backend.main.db", real_db):
        await _recover_stuck_tasks()

    # Verify all reset to pending
    assert await real_db.count_tasks(status=TaskStatus.IN_PROGRESS) == 0
    assert await real_db.count_tasks(status=TaskStatus.PENDING) == 3

    # Verify individual tasks
    t1_after = await real_db.get_task(t1.id)
    t2_after = await real_db.get_task(t2.id)
    t3_after = await real_db.get_task(t3.id)
    assert t1_after.status == TaskStatus.PENDING
    assert t2_after.status == TaskStatus.PENDING
    assert t3_after.status == TaskStatus.PENDING


async def test_startup_recovery_ignores_other_statuses(real_db):
    """Recovery should only reset in_progress tasks, not completed/failed/pending."""
    t_pending = await real_db.create_task(title="Pending", prompt="p")
    t_done = await real_db.create_task(title="Done", prompt="p")
    t_failed = await real_db.create_task(title="Failed", prompt="p")
    t_stuck = await real_db.create_task(title="Stuck", prompt="p")

    await real_db.update_task(t_done.id, status=TaskStatus.COMPLETED)
    await real_db.update_task(t_failed.id, status=TaskStatus.FAILED)
    await real_db.update_task(t_stuck.id, status=TaskStatus.IN_PROGRESS)

    from backend.main import _recover_stuck_tasks
    with patch("backend.main.db", real_db):
        await _recover_stuck_tasks()

    assert (await real_db.get_task(t_pending.id)).status == TaskStatus.PENDING
    assert (await real_db.get_task(t_done.id)).status == TaskStatus.COMPLETED
    assert (await real_db.get_task(t_failed.id)).status == TaskStatus.FAILED
    assert (await real_db.get_task(t_stuck.id)).status == TaskStatus.PENDING


# ── DB corruption test under concurrent load ─────────────────────────────────


async def test_no_db_corruption_under_concurrent_load(real_db):
    """Concurrent creates, updates, and reads should not corrupt the DB."""
    created_ids = []

    async def create_and_update(i):
        t = await real_db.create_task(title=f"Concurrent {i}", prompt=f"prompt {i}")
        created_ids.append(t.id)
        await real_db.update_task(t.id, status=TaskStatus.IN_PROGRESS)
        await asyncio.sleep(0)  # yield to event loop
        await real_db.update_task(t.id, status=TaskStatus.COMPLETED, output=f"result-{i}")

    async def read_loop():
        for _ in range(20):
            await real_db.list_tasks()
            await asyncio.sleep(0)

    # Run concurrent creates/updates with concurrent reads
    await asyncio.gather(
        *(create_and_update(i) for i in range(10)),
        read_loop(),
    )

    # Verify integrity
    all_tasks = await real_db.list_tasks()
    assert len(all_tasks) == 10
    for t in all_tasks:
        assert t.status == TaskStatus.COMPLETED
        assert t.output is not None


async def test_concurrent_count_accuracy(real_db):
    """count_tasks should remain accurate under concurrent status changes."""
    tasks = []
    for i in range(6):
        t = await real_db.create_task(title=f"Task {i}", prompt=f"p{i}")
        tasks.append(t)

    # Set half to in_progress concurrently
    await asyncio.gather(
        *(real_db.update_task(t.id, status=TaskStatus.IN_PROGRESS) for t in tasks[:3])
    )

    assert await real_db.count_tasks(status=TaskStatus.IN_PROGRESS) == 3
    assert await real_db.count_tasks(status=TaskStatus.PENDING) == 3

    # Complete some, fail others concurrently
    await asyncio.gather(
        real_db.update_task(tasks[0].id, status=TaskStatus.COMPLETED),
        real_db.update_task(tasks[1].id, status=TaskStatus.FAILED),
        real_db.update_task(tasks[3].id, status=TaskStatus.IN_PROGRESS),
    )

    assert await real_db.count_tasks(status=TaskStatus.COMPLETED) == 1
    assert await real_db.count_tasks(status=TaskStatus.FAILED) == 1
    assert await real_db.count_tasks(status=TaskStatus.IN_PROGRESS) == 2
    assert await real_db.count_tasks(status=TaskStatus.PENDING) == 2
