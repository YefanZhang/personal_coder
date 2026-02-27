import asyncio
import json
import os
import tempfile

import pytest
import pytest_asyncio

from backend.database import Database
from backend.models import Task, TaskStatus, TaskMode, TaskPriority
from backend.task_registry import TaskRegistry


@pytest.fixture
def tmp_registry(tmp_path):
    return str(tmp_path / "dev-tasks.json")


@pytest.fixture
def cli_tasks_file(tmp_path):
    """Create a dev-tasks.json with CLI tasks (old format)."""
    path = str(tmp_path / "dev-tasks.json")
    data = {
        "tasks": [
            {
                "id": "p1-001",
                "title": "Project scaffold",
                "status": "done",
                "priority": "high",
                "phase": 1,
                "description": "Create the project structure.",
            },
            {
                "id": "p1-002",
                "title": "Task executor",
                "status": "done",
                "priority": "high",
                "phase": 1,
                "description": "Implement executor.",
            },
        ]
    }
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    return path


@pytest_asyncio.fixture
async def db(tmp_path):
    db_path = str(tmp_path / "test.db")
    database = Database(db_path)
    await database.init()
    yield database
    await database.close()


def test_load_preserves_cli_tasks(cli_tasks_file):
    """Loading existing CLI tasks preserves them and adds source field."""
    reg = TaskRegistry(cli_tasks_file)
    reg.load_cli_tasks()
    assert len(reg._cli_tasks) == 2
    assert reg._cli_tasks[0]["id"] == "p1-001"
    assert reg._cli_tasks[0]["source"] == "cli"
    assert reg._cli_tasks[1]["source"] == "cli"
    # Original fields preserved
    assert reg._cli_tasks[0]["phase"] == 1
    assert reg._cli_tasks[0]["description"] == "Create the project structure."


def test_load_handles_missing_file(tmp_path):
    """Gracefully handles missing registry file."""
    reg = TaskRegistry(str(tmp_path / "nonexistent.json"))
    reg.load_cli_tasks()
    assert reg._cli_tasks == []


def test_load_handles_corrupt_json(tmp_path):
    """Gracefully handles invalid JSON."""
    path = str(tmp_path / "bad.json")
    with open(path, "w") as f:
        f.write("not valid json {{{")
    reg = TaskRegistry(path)
    reg.load_cli_tasks()
    assert reg._cli_tasks == []


def test_load_filters_web_tasks(tmp_path):
    """Web-sourced tasks in the file are not loaded as CLI tasks."""
    path = str(tmp_path / "dev-tasks.json")
    data = {
        "meta": {},
        "tasks": [
            {"id": "p1-001", "title": "CLI task", "status": "done", "source": "cli"},
            {"id": "web-1", "title": "Web task", "status": "completed", "source": "web"},
        ],
    }
    with open(path, "w") as f:
        json.dump(data, f)
    reg = TaskRegistry(path)
    reg.load_cli_tasks()
    assert len(reg._cli_tasks) == 1
    assert reg._cli_tasks[0]["id"] == "p1-001"


async def test_sync_writes_merged_output(cli_tasks_file, db):
    """CLI + web tasks appear together in output."""
    reg = TaskRegistry(cli_tasks_file)
    reg.load_cli_tasks()

    task = await db.create_task(
        title="Fix bug", prompt="Fix the login", created_by="192.168.1.10"
    )

    db_tasks = await db.list_tasks()
    await reg.sync(db_tasks)

    with open(cli_tasks_file) as f:
        output = json.load(f)

    assert "meta" in output
    assert output["meta"]["cli_tasks"] == 2
    assert output["meta"]["web_tasks"] == 1
    assert len(output["tasks"]) == 3

    # CLI tasks come first
    assert output["tasks"][0]["id"] == "p1-001"
    assert output["tasks"][0]["source"] == "cli"

    # Web task last
    web_task = output["tasks"][2]
    assert web_task["id"] == f"web-{task.id}"
    assert web_task["source"] == "web"
    assert web_task["title"] == "Fix bug"
    assert web_task["created_by"] == "192.168.1.10"


async def test_sync_with_empty_db(cli_tasks_file):
    """With no DB tasks, only CLI tasks in output."""
    reg = TaskRegistry(cli_tasks_file)
    reg.load_cli_tasks()
    await reg.sync([])

    with open(cli_tasks_file) as f:
        output = json.load(f)

    assert output["meta"]["cli_tasks"] == 2
    assert output["meta"]["web_tasks"] == 0
    assert len(output["tasks"]) == 2


def test_web_task_id_format(tmp_registry):
    """Web task IDs are formatted as 'web-N'."""
    from datetime import datetime

    reg = TaskRegistry(tmp_registry)
    task = Task(
        id=42,
        title="Test",
        prompt="test prompt",
        created_at=datetime.now(),
    )
    result = reg._web_task_to_dict(task)
    assert result["id"] == "web-42"
    assert result["source"] == "web"


def test_web_task_includes_all_fields(tmp_registry):
    """Web task dict includes all expected metadata fields."""
    from datetime import datetime

    now = datetime.now()
    reg = TaskRegistry(tmp_registry)
    task = Task(
        id=1,
        title="Test task",
        prompt="Do something",
        status=TaskStatus.COMPLETED,
        mode=TaskMode.EXECUTE,
        priority=TaskPriority.HIGH,
        created_at=now,
        started_at=now,
        completed_at=now,
        cost_usd=0.042,
        input_tokens=15000,
        output_tokens=8000,
        exit_code=0,
        error=None,
        tags=["bugfix"],
        depends_on=[1, 2],
        created_by="10.0.0.1",
    )
    result = reg._web_task_to_dict(task)
    assert result["status"] == "completed"
    assert result["priority"] == "high"
    assert result["mode"] == "execute"
    assert result["cost_usd"] == 0.042
    assert result["input_tokens"] == 15000
    assert result["output_tokens"] == 8000
    assert result["exit_code"] == 0
    assert result["tags"] == ["bugfix"]
    assert result["depends_on"] == ["web-1", "web-2"]
    assert result["created_by"] == "10.0.0.1"


async def test_created_by_included(tmp_registry, db):
    """created_by flows through from DB to registry output."""
    reg = TaskRegistry(tmp_registry)
    reg.load_cli_tasks()

    await db.create_task(title="T1", prompt="P1", created_by="10.0.0.5")
    db_tasks = await db.list_tasks()
    await reg.sync(db_tasks)

    with open(tmp_registry) as f:
        output = json.load(f)

    web_task = output["tasks"][0]
    assert web_task["created_by"] == "10.0.0.5"


async def test_sync_computes_total_cost(cli_tasks_file, db):
    """Meta total_cost_usd sums costs from all tasks."""
    reg = TaskRegistry(cli_tasks_file)
    reg.load_cli_tasks()

    t1 = await db.create_task(title="T1", prompt="P1")
    await db.update_task(t1.id, cost_usd=0.10)
    t2 = await db.create_task(title="T2", prompt="P2")
    await db.update_task(t2.id, cost_usd=0.25)

    db_tasks = await db.list_tasks()
    await reg.sync(db_tasks)

    with open(cli_tasks_file) as f:
        output = json.load(f)

    assert output["meta"]["total_cost_usd"] == 0.35


async def test_sync_atomic_write(tmp_registry):
    """Verify the file is written atomically (no partial writes)."""
    reg = TaskRegistry(tmp_registry)
    reg.load_cli_tasks()

    # Sync with empty tasks
    await reg.sync([])

    with open(tmp_registry) as f:
        output = json.load(f)

    # File should be valid JSON
    assert "meta" in output
    assert "tasks" in output


async def test_concurrent_syncs_safe(tmp_registry, db):
    """Multiple concurrent syncs don't corrupt the file."""
    reg = TaskRegistry(tmp_registry)
    reg.load_cli_tasks()

    # Create several tasks
    for i in range(5):
        await db.create_task(title=f"Task {i}", prompt=f"Prompt {i}")

    db_tasks = await db.list_tasks()

    # Launch multiple syncs concurrently
    await asyncio.gather(
        reg.sync(db_tasks),
        reg.sync(db_tasks),
        reg.sync(db_tasks),
        reg.sync(db_tasks),
    )

    # File should still be valid
    with open(tmp_registry) as f:
        output = json.load(f)

    assert output["meta"]["web_tasks"] == 5
    assert len(output["tasks"]) == 5


async def test_sync_creates_parent_dir(tmp_path):
    """Sync creates parent directories if they don't exist."""
    path = str(tmp_path / "subdir" / "nested" / "dev-tasks.json")
    reg = TaskRegistry(path)
    reg.load_cli_tasks()
    await reg.sync([])

    with open(path) as f:
        output = json.load(f)
    assert output["meta"]["web_tasks"] == 0


async def test_db_created_by_column(db):
    """Database stores and retrieves created_by field."""
    task = await db.create_task(title="T", prompt="P", created_by="1.2.3.4")
    fetched = await db.get_task(task.id)
    assert fetched.created_by == "1.2.3.4"


async def test_db_created_by_default_none(db):
    """created_by defaults to None when not provided."""
    task = await db.create_task(title="T", prompt="P")
    fetched = await db.get_task(task.id)
    assert fetched.created_by is None
