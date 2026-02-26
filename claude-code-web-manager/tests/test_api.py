import asyncio
import os
import tempfile
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from httpx import AsyncClient, ASGITransport

# We need to patch the global singletons before importing main
import backend.main as main_module
from backend.database import Database
from backend.models import TaskStatus


@pytest_asyncio.fixture
async def app_with_db():
    """Set up a test FastAPI app instance with a temporary DB."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    test_db = Database(db_path)
    await test_db.init()

    # Patch the global db and scheduler in main module
    mock_scheduler = MagicMock()
    mock_scheduler.start = AsyncMock(return_value=None)
    mock_scheduler.stop = MagicMock()
    mock_scheduler.cancel_task = AsyncMock()

    original_db = main_module.db
    original_scheduler = main_module.scheduler

    main_module.db = test_db
    main_module.scheduler = mock_scheduler

    # Temporarily clear API_KEY for auth-skipping
    original_api_key = main_module.API_KEY
    main_module.API_KEY = ""

    # We need the app to run lifespan but without the real scheduler loop
    # Patch asyncio.create_task to avoid background tasks in tests
    with patch("backend.main.asyncio.create_task", return_value=MagicMock()):
        from httpx import AsyncClient, ASGITransport
        transport = ASGITransport(app=main_module.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # Manually init db (lifespan won't run in TestClient)
            await test_db.init()
            yield client, test_db

    main_module.db = original_db
    main_module.scheduler = original_scheduler
    main_module.API_KEY = original_api_key
    await test_db.close()
    os.unlink(db_path)


@pytest.fixture
def client_and_db(app_with_db):
    return app_with_db


# ── Health ────────────────────────────────────────────────────────────────────

async def test_health(app_with_db):
    client, db = app_with_db
    resp = await client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


# ── Create task ───────────────────────────────────────────────────────────────

async def test_create_task(app_with_db):
    client, db = app_with_db
    resp = await client.post("/api/tasks", json={"title": "T1", "prompt": "Do it"})
    assert resp.status_code == 201
    data = resp.json()
    assert data["title"] == "T1"
    assert data["status"] == "pending"
    assert data["id"] is not None


async def test_create_task_with_priority(app_with_db):
    client, db = app_with_db
    resp = await client.post("/api/tasks", json={"title": "High", "prompt": "p", "priority": "high"})
    assert resp.status_code == 201
    assert resp.json()["priority"] == "high"


# ── Batch create ──────────────────────────────────────────────────────────────

async def test_create_tasks_batch(app_with_db):
    client, db = app_with_db
    resp = await client.post("/api/tasks/batch", json=[
        {"title": "B1", "prompt": "p1"},
        {"title": "B2", "prompt": "p2"},
    ])
    assert resp.status_code == 201
    data = resp.json()
    assert len(data) == 2
    assert data[0]["title"] == "B1"
    assert data[1]["title"] == "B2"


# ── List tasks ────────────────────────────────────────────────────────────────

async def test_list_tasks(app_with_db):
    client, db = app_with_db
    await client.post("/api/tasks", json={"title": "T1", "prompt": "p"})
    await client.post("/api/tasks", json={"title": "T2", "prompt": "p"})
    resp = await client.get("/api/tasks")
    assert resp.status_code == 200
    assert len(resp.json()) == 2


async def test_list_tasks_filter_by_status(app_with_db):
    client, db = app_with_db
    r = await client.post("/api/tasks", json={"title": "T1", "prompt": "p"})
    task_id = r.json()["id"]
    await db.update_task(task_id, status=TaskStatus.COMPLETED)

    resp = await client.get("/api/tasks?status=completed")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["status"] == "completed"


# ── Get task ──────────────────────────────────────────────────────────────────

async def test_get_task(app_with_db):
    client, db = app_with_db
    r = await client.post("/api/tasks", json={"title": "Fetch me", "prompt": "p"})
    task_id = r.json()["id"]
    resp = await client.get(f"/api/tasks/{task_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["task"]["id"] == task_id
    assert data["logs"] == []


async def test_get_task_not_found(app_with_db):
    client, db = app_with_db
    resp = await client.get("/api/tasks/99999")
    assert resp.status_code == 404


# ── Get task logs ─────────────────────────────────────────────────────────────

async def test_get_task_logs(app_with_db):
    client, db = app_with_db
    r = await client.post("/api/tasks", json={"title": "Log test", "prompt": "p"})
    task_id = r.json()["id"]
    await db.add_log(task_id, "info", "step 1", raw_output="raw1")
    resp = await client.get(f"/api/tasks/{task_id}/logs")
    assert resp.status_code == 200
    logs = resp.json()
    assert len(logs) == 1
    assert logs[0]["message"] == "step 1"


async def test_get_task_logs_not_found(app_with_db):
    client, db = app_with_db
    resp = await client.get("/api/tasks/99999/logs")
    assert resp.status_code == 404


# ── Cancel task ───────────────────────────────────────────────────────────────

async def test_cancel_task(app_with_db):
    client, db = app_with_db
    r = await client.post("/api/tasks", json={"title": "Cancel me", "prompt": "p"})
    task_id = r.json()["id"]
    resp = await client.post(f"/api/tasks/{task_id}/cancel")
    assert resp.status_code == 200
    updated = await db.get_task(task_id)
    assert updated.status == TaskStatus.CANCELLED


async def test_cancel_task_not_found(app_with_db):
    client, db = app_with_db
    resp = await client.post("/api/tasks/99999/cancel")
    assert resp.status_code == 404


# ── Retry task ────────────────────────────────────────────────────────────────

async def test_retry_task(app_with_db):
    client, db = app_with_db
    r = await client.post("/api/tasks", json={"title": "Retry", "prompt": "p"})
    task_id = r.json()["id"]
    await db.update_task(task_id, status=TaskStatus.FAILED, error="oops")
    resp = await client.post(f"/api/tasks/{task_id}/retry")
    assert resp.status_code == 200
    updated = await db.get_task(task_id)
    assert updated.status == TaskStatus.PENDING
    assert updated.error is None


async def test_retry_task_not_found(app_with_db):
    client, db = app_with_db
    resp = await client.post("/api/tasks/99999/retry")
    assert resp.status_code == 404


# ── Approve plan ──────────────────────────────────────────────────────────────

async def test_approve_plan(app_with_db):
    client, db = app_with_db
    r = await client.post("/api/tasks", json={"title": "Plan", "prompt": "p", "mode": "plan"})
    task_id = r.json()["id"]
    resp = await client.post(f"/api/tasks/{task_id}/approve-plan")
    assert resp.status_code == 200
    updated = await db.get_task(task_id)
    assert updated.status == TaskStatus.PENDING
    from backend.models import TaskMode
    assert updated.mode == TaskMode.EXECUTE


# ── Delete task ───────────────────────────────────────────────────────────────

async def test_delete_task(app_with_db):
    client, db = app_with_db
    r = await client.post("/api/tasks", json={"title": "Delete me", "prompt": "p"})
    task_id = r.json()["id"]
    resp = await client.delete(f"/api/tasks/{task_id}")
    assert resp.status_code == 200
    assert await db.get_task(task_id) is None


async def test_delete_task_not_found(app_with_db):
    client, db = app_with_db
    resp = await client.delete("/api/tasks/99999")
    assert resp.status_code == 404


# ── API Key auth ──────────────────────────────────────────────────────────────

async def test_auth_required_when_api_key_set(app_with_db):
    client, db = app_with_db
    original_key = main_module.API_KEY
    main_module.API_KEY = "secret-key"
    try:
        resp = await client.post("/api/tasks", json={"title": "T", "prompt": "p"})
        assert resp.status_code == 401

        resp2 = await client.post(
            "/api/tasks",
            json={"title": "T", "prompt": "p"},
            headers={"x-api-key": "secret-key"},
        )
        assert resp2.status_code == 201
    finally:
        main_module.API_KEY = original_key
