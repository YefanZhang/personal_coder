"""
E2E Playwright tests for the Claude Code Manager kanban board.

How to run:
    1. Start the backend server:
        cd claude-code-web-manager
        uvicorn backend.main:app --port 8000

    2. Run the E2E tests (in a separate terminal):
        cd claude-code-web-manager
        pytest tests/e2e/ -v --tb=short

    Prerequisites:
        uv sync --all-extras        # install dev deps (includes pytest-playwright)
        playwright install chromium  # install headless browser
"""
import re

import pytest
from playwright.sync_api import Page, expect

BASE_URL = "http://localhost:8000"


@pytest.fixture(autouse=True)
def _cleanup_tasks(page: Page):
    """Delete all tasks after each test to keep runs independent."""
    yield
    # Fetch all tasks via API, then delete each one
    resp = page.request.get(f"{BASE_URL}/api/tasks")
    if resp.ok:
        for task in resp.json():
            page.request.delete(f"{BASE_URL}/api/tasks/{task['id']}")


# ── Test 1: Navigate to the app ──────────────────────────────────────────────

def test_navigate_to_app(page: Page):
    """Navigate to http://localhost:8000 and verify the page loads."""
    page.goto(BASE_URL)
    expect(page.locator("header h1")).to_have_text("Claude Code Manager")
    # Kanban columns should be visible
    expect(page.locator(".kanban-column")).to_have_count(6)


# ── Test 2: Create a task via the form ───────────────────────────────────────

def test_create_task_via_form(page: Page):
    """Fill in the task creation form and submit, verify task appears."""
    page.goto(BASE_URL)

    # Open the form
    page.click("button.form-toggle")

    # Fill in title and prompt
    page.fill('input[placeholder="Task title"]', "E2E test task")
    page.fill(
        'textarea',
        "This is an automated E2E test task created by Playwright",
    )

    # Select priority
    page.select_option('select', value='high', strict=False)

    # Submit
    page.click("button:has-text('Create Task')")

    # Wait for the task card to appear in the pending column
    pending_column = page.locator(".kanban-column").first
    expect(pending_column.locator(".card-title")).to_contain_text(
        "E2E test task", timeout=5000,
    )


# ── Test 3: Assert task card appears in the pending column ───────────────────

def test_task_appears_in_pending_column(page: Page):
    """Create a task via API, verify it shows up in the pending kanban column."""
    # Create task with an unsatisfied dependency so the scheduler won't
    # dispatch it (depends_on=[999999] — a non-existent task keeps it pending)
    resp = page.request.post(
        f"{BASE_URL}/api/tasks",
        data={
            "title": "Pending column test",
            "prompt": "Test prompt for pending column verification",
            "priority": "medium",
            "mode": "execute",
            "depends_on": [999999],
        },
    )
    assert resp.ok, f"Failed to create task: {resp.status}"

    # Navigate and verify
    page.goto(BASE_URL)
    pending_column = page.locator(".kanban-column").first
    expect(pending_column.locator(".column-title")).to_have_text("Pending")
    expect(pending_column.locator(".card-title")).to_contain_text(
        "Pending column test", timeout=5000,
    )
    # Count badge should show at least 1
    expect(pending_column.locator(".column-count")).not_to_have_text("0")


# ── Test 4: Assert GET /api/health returns 200 ──────────────────────────────

def test_health_endpoint(page: Page):
    """Verify the /api/health endpoint returns 200 with correct body."""
    resp = page.request.get(f"{BASE_URL}/api/health")
    assert resp.status == 200
    body = resp.json()
    assert body == {"status": "ok"}


# ── Test 5: Assert WebSocket connection is established ───────────────────────

def test_websocket_connection(page: Page):
    """Verify the frontend establishes a WebSocket connection."""
    ws_urls: list[str] = []

    # Listen for WebSocket connections before navigating
    page.on("websocket", lambda ws: ws_urls.append(ws.url))

    page.goto(BASE_URL)

    # Wait for the WS status indicator to show "Connected"
    expect(page.locator(".ws-dot.connected")).to_be_visible(timeout=5000)

    # Verify a ws:// URL was captured
    assert len(ws_urls) > 0, "No WebSocket connections detected"
    assert any(re.search(r"wss?://", url) for url in ws_urls), (
        f"No ws:// or wss:// URL found in {ws_urls}"
    )


# ── Test 6: Screenshot the kanban board ──────────────────────────────────────

def test_screenshot_kanban_board(page: Page):
    """Take a screenshot of the kanban board for visual review."""
    # Create a couple of tasks for a more interesting screenshot
    for i, (title, priority) in enumerate([
        ("Screenshot task A", "high"),
        ("Screenshot task B", "low"),
        ("Screenshot task C", "urgent"),
    ]):
        page.request.post(
            f"{BASE_URL}/api/tasks",
            data={
                "title": title,
                "prompt": f"Test prompt #{i+1}",
                "priority": priority,
                "mode": "execute",
            },
        )

    page.goto(BASE_URL)
    # Wait for tasks to render
    expect(page.locator(".task-card")).to_have_count(3, timeout=5000)

    # Take the screenshot
    page.screenshot(path="tests/e2e/kanban-screenshot.png", full_page=True)


# ── Test 7: Side panel opens on card click ───────────────────────────────────

def test_side_panel_opens(page: Page):
    """Click a task card and verify the side panel opens with details."""
    # Create a task
    page.request.post(
        f"{BASE_URL}/api/tasks",
        data={
            "title": "Panel test task",
            "prompt": "Prompt for side panel test",
            "priority": "high",
            "mode": "execute",
        },
    )

    page.goto(BASE_URL)
    expect(page.locator(".task-card")).to_have_count(1, timeout=5000)

    # Click the card
    page.click(".task-card")

    # Side panel should be visible
    expect(page.locator(".side-panel")).to_be_visible(timeout=3000)
    expect(page.locator(".panel-header h2")).to_contain_text("Panel test task")

    # Prompt should be displayed
    expect(page.locator(".prompt-text")).to_contain_text(
        "Prompt for side panel test",
    )

    # Close via Escape
    page.keyboard.press("Escape")
    expect(page.locator(".side-panel")).not_to_be_visible()
