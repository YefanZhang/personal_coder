import pytest
import pytest_asyncio
import asyncio
import os
import tempfile
from backend.database import Database
from backend.models import TaskStatus, TaskPriority


@pytest_asyncio.fixture
async def db():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    database = Database(db_path)
    await database.init()
    yield database
    await database.close()
    os.unlink(db_path)


async def test_init_idempotent(db):
    # Calling init twice should not raise
    await db.init()


async def test_create_task(db):
    task = await db.create_task(title="Test", prompt="Do something")
    assert task.id is not None
    assert task.title == "Test"
    assert task.prompt == "Do something"
    assert task.status == TaskStatus.PENDING


async def test_get_task(db):
    created = await db.create_task(title="T1", prompt="P1")
    fetched = await db.get_task(created.id)
    assert fetched is not None
    assert fetched.id == created.id
    assert fetched.title == "T1"


async def test_get_task_not_found(db):
    result = await db.get_task(99999)
    assert result is None


async def test_list_tasks(db):
    await db.create_task(title="A", prompt="pa")
    await db.create_task(title="B", prompt="pb")
    tasks = await db.list_tasks()
    assert len(tasks) == 2


async def test_list_tasks_by_status(db):
    t1 = await db.create_task(title="A", prompt="pa")
    await db.create_task(title="B", prompt="pb")
    await db.update_task(t1.id, status=TaskStatus.COMPLETED)
    pending = await db.list_tasks(status=TaskStatus.PENDING)
    completed = await db.list_tasks(status=TaskStatus.COMPLETED)
    assert len(pending) == 1
    assert len(completed) == 1


async def test_update_task(db):
    task = await db.create_task(title="T", prompt="P")
    await db.update_task(task.id, status=TaskStatus.IN_PROGRESS, worker_pid=1234)
    updated = await db.get_task(task.id)
    assert updated.status == TaskStatus.IN_PROGRESS
    assert updated.worker_pid == 1234


async def test_count_tasks(db):
    await db.create_task(title="A", prompt="pa")
    await db.create_task(title="B", prompt="pb")
    count = await db.count_tasks(TaskStatus.PENDING)
    assert count == 2
    count_in_progress = await db.count_tasks(TaskStatus.IN_PROGRESS)
    assert count_in_progress == 0


async def test_get_next_pending_task_priority_ordering(db):
    low = await db.create_task(title="low", prompt="p", priority="low")
    urgent = await db.create_task(title="urgent", prompt="p", priority="urgent")
    medium = await db.create_task(title="medium", prompt="p", priority="medium")
    high = await db.create_task(title="high", prompt="p", priority="high")

    next_task = await db.get_next_pending_task()
    assert next_task.id == urgent.id

    await db.update_task(urgent.id, status=TaskStatus.IN_PROGRESS)
    next_task = await db.get_next_pending_task()
    assert next_task.id == high.id

    await db.update_task(high.id, status=TaskStatus.IN_PROGRESS)
    next_task = await db.get_next_pending_task()
    assert next_task.id == medium.id

    await db.update_task(medium.id, status=TaskStatus.IN_PROGRESS)
    next_task = await db.get_next_pending_task()
    assert next_task.id == low.id


async def test_get_next_pending_task_none_when_empty(db):
    result = await db.get_next_pending_task()
    assert result is None


async def test_add_log(db):
    task = await db.create_task(title="T", prompt="P")
    await db.add_log(task.id, "info", "step 1 done", raw_output="raw1")
    await db.add_log(task.id, "error", "something broke")
    logs = await db.get_task_logs(task.id)
    assert len(logs) == 2
    assert logs[0].level == "info"
    assert logs[0].message == "step 1 done"
    assert logs[0].raw_output == "raw1"
    assert logs[1].level == "error"


async def test_get_task_logs(db):
    task = await db.create_task(title="T", prompt="P")
    logs = await db.get_task_logs(task.id)
    assert logs == []


async def test_delete_task_cascade(db):
    task = await db.create_task(title="T", prompt="P")
    await db.add_log(task.id, "info", "log entry")
    await db.delete_task(task.id)
    deleted = await db.get_task(task.id)
    assert deleted is None
    # logs should also be gone (cascade)
    logs = await db.get_task_logs(task.id)
    assert logs == []


async def test_startup_recovery_resets_in_progress_to_pending(db):
    # Simulate crash: tasks stuck in_progress
    t1 = await db.create_task(title="T1", prompt="P1")
    t2 = await db.create_task(title="T2", prompt="P2")
    await db.update_task(t1.id, status=TaskStatus.IN_PROGRESS)
    await db.update_task(t2.id, status=TaskStatus.IN_PROGRESS)

    # Recovery logic (same as _recover_stuck_tasks in main.py)
    stuck = await db.list_tasks(status=TaskStatus.IN_PROGRESS)
    for task in stuck:
        await db.update_task(task.id, status=TaskStatus.PENDING, worker_pid=None)

    after = await db.list_tasks(status=TaskStatus.IN_PROGRESS)
    assert len(after) == 0
    pending = await db.list_tasks(status=TaskStatus.PENDING)
    assert len(pending) == 2


async def test_create_task_with_depends_on_and_tags(db):
    t1 = await db.create_task(title="T1", prompt="P1")
    t2 = await db.create_task(
        title="T2", prompt="P2", depends_on=[t1.id], tags=["feature", "backend"]
    )
    fetched = await db.get_task(t2.id)
    assert fetched.depends_on == [t1.id]
    assert fetched.tags == ["feature", "backend"]
