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


# ── Test 7b: Side panel shows status and priority badges ─────────────────

def test_side_panel_shows_status_and_priority(page: Page):
    """Verify the side panel displays status badge, priority badge, and created date."""
    # Create a task with unsatisfied dep so it stays pending
    page.request.post(
        f"{BASE_URL}/api/tasks",
        data={
            "title": "Badge display test",
            "prompt": "Testing badge visibility in side panel",
            "priority": "urgent",
            "mode": "execute",
            "depends_on": [999999],
        },
    )

    page.goto(BASE_URL)
    expect(page.locator(".task-card")).to_have_count(1, timeout=5000)
    page.click(".task-card")

    panel = page.locator(".side-panel")
    expect(panel).to_be_visible(timeout=3000)

    # Status badge should show "pending"
    expect(panel.locator(".badge-pending")).to_be_visible()

    # Priority badge should show "urgent"
    expect(panel.locator(".badge-urgent")).to_be_visible()

    # Created date should be present
    expect(panel.locator("label:has-text('Created')")).to_be_visible()


# ── Test 7c: Side panel close via X button ───────────────────────────────

def test_side_panel_close_button(page: Page):
    """Click the X button to close the side panel."""
    page.request.post(
        f"{BASE_URL}/api/tasks",
        data={
            "title": "Close button test",
            "prompt": "Testing close button",
            "priority": "medium",
            "mode": "execute",
            "depends_on": [999999],
        },
    )

    page.goto(BASE_URL)
    expect(page.locator(".task-card")).to_have_count(1, timeout=5000)
    page.click(".task-card")
    expect(page.locator(".side-panel")).to_be_visible(timeout=3000)

    # Click the close button
    page.click(".panel-close")
    expect(page.locator(".side-panel")).not_to_be_visible()


# ── Test 7d: Side panel close via overlay click ──────────────────────────

def test_side_panel_overlay_close(page: Page):
    """Click the overlay behind the side panel to close it."""
    page.request.post(
        f"{BASE_URL}/api/tasks",
        data={
            "title": "Overlay close test",
            "prompt": "Testing overlay click close",
            "priority": "medium",
            "mode": "execute",
            "depends_on": [999999],
        },
    )

    page.goto(BASE_URL)
    expect(page.locator(".task-card")).to_have_count(1, timeout=5000)
    page.click(".task-card")
    expect(page.locator(".side-panel")).to_be_visible(timeout=3000)

    # Click the overlay (outside the panel)
    page.click(".side-panel-overlay")
    expect(page.locator(".side-panel")).not_to_be_visible()


# ── Test 7e: Side panel cancel button for pending task ───────────────────

def test_side_panel_cancel_button(page: Page):
    """Cancel button should be visible for pending tasks and cancel on click."""
    resp = page.request.post(
        f"{BASE_URL}/api/tasks",
        data={
            "title": "Cancel button test",
            "prompt": "Testing cancel in side panel",
            "priority": "high",
            "mode": "execute",
            "depends_on": [999999],
        },
    )
    assert resp.ok

    page.goto(BASE_URL)
    expect(page.locator(".task-card")).to_have_count(1, timeout=5000)
    page.click(".task-card")

    panel = page.locator(".side-panel")
    expect(panel).to_be_visible(timeout=3000)

    # Cancel button should be visible for pending tasks
    cancel_btn = panel.locator("button.danger:has-text('Cancel')")
    expect(cancel_btn).to_be_visible()

    # Click cancel
    cancel_btn.click()

    # Status should update to cancelled
    expect(panel.locator(".badge-cancelled")).to_be_visible(timeout=5000)


# ── Test 7f: Side panel retry button for cancelled task ──────────────────

def test_side_panel_retry_button(page: Page):
    """Retry button should appear for cancelled tasks and reset to pending."""
    # Create and then cancel a task via API
    resp = page.request.post(
        f"{BASE_URL}/api/tasks",
        data={
            "title": "Retry button test",
            "prompt": "Testing retry in side panel",
            "priority": "medium",
            "mode": "execute",
            "depends_on": [999999],
        },
    )
    task_id = resp.json()["id"]
    page.request.post(f"{BASE_URL}/api/tasks/{task_id}/cancel")

    page.goto(BASE_URL)
    # Task should be in cancelled column
    cancelled_column = page.locator(".kanban-column").nth(5)
    expect(cancelled_column.locator(".task-card")).to_have_count(1, timeout=5000)

    # Click the card in cancelled column
    cancelled_column.locator(".task-card").click()

    panel = page.locator(".side-panel")
    expect(panel).to_be_visible(timeout=3000)

    # Retry button should be visible for cancelled tasks
    retry_btn = panel.locator("button.secondary:has-text('Retry')")
    expect(retry_btn).to_be_visible()

    # Click retry
    retry_btn.click()

    # Status should update to pending
    expect(panel.locator(".badge-pending")).to_be_visible(timeout=5000)


# ── Test 7g: Side panel delete button ────────────────────────────────────

def test_side_panel_delete_button(page: Page):
    """Delete button removes the task and closes the side panel."""
    page.request.post(
        f"{BASE_URL}/api/tasks",
        data={
            "title": "Delete button test",
            "prompt": "Testing delete in side panel",
            "priority": "low",
            "mode": "execute",
            "depends_on": [999999],
        },
    )

    page.goto(BASE_URL)
    expect(page.locator(".task-card")).to_have_count(1, timeout=5000)
    page.click(".task-card")

    panel = page.locator(".side-panel")
    expect(panel).to_be_visible(timeout=3000)

    # Delete button should be visible (task is pending, not in_progress)
    delete_btn = panel.locator("button.danger:has-text('Delete')")
    expect(delete_btn).to_be_visible()

    # Click delete
    delete_btn.click()

    # Panel should close
    expect(page.locator(".side-panel")).not_to_be_visible(timeout=3000)

    # Task card should be gone
    expect(page.locator(".task-card")).to_have_count(0, timeout=5000)


# ── Test 7h: Side panel shows logs section ───────────────────────────────

def test_side_panel_logs_section(page: Page):
    """Verify the logs section header and 'No logs yet' message appear."""
    page.request.post(
        f"{BASE_URL}/api/tasks",
        data={
            "title": "Logs section test",
            "prompt": "Testing logs display in side panel",
            "priority": "medium",
            "mode": "execute",
            "depends_on": [999999],
        },
    )

    page.goto(BASE_URL)
    expect(page.locator(".task-card")).to_have_count(1, timeout=5000)
    page.click(".task-card")

    panel = page.locator(".side-panel")
    expect(panel).to_be_visible(timeout=3000)

    # Logs section header should be visible
    expect(panel.locator(".log-section h3")).to_have_text("Logs")

    # With no execution, should show empty message
    expect(panel.locator(".log-empty")).to_have_text("No logs yet.")


# ── Test 7i: Side panel prompt display ───────────────────────────────────

def test_side_panel_prompt_display(page: Page):
    """Verify the full prompt text is displayed in the prompt section."""
    long_prompt = "This is a detailed prompt with multiple lines.\nLine 2 of the prompt.\nLine 3 with special chars: <>&"
    page.request.post(
        f"{BASE_URL}/api/tasks",
        data={
            "title": "Prompt display test",
            "prompt": long_prompt,
            "priority": "medium",
            "mode": "execute",
            "depends_on": [999999],
        },
    )

    page.goto(BASE_URL)
    expect(page.locator(".task-card")).to_have_count(1, timeout=5000)
    page.click(".task-card")

    panel = page.locator(".side-panel")
    expect(panel).to_be_visible(timeout=3000)

    # Prompt label should be present
    expect(panel.locator("label:has-text('Prompt')")).to_be_visible()

    # Prompt text should contain the first line
    expect(panel.locator(".prompt-text")).to_contain_text(
        "This is a detailed prompt with multiple lines.",
    )


# ── Test 7j: Side panel shows task ID in header ─────────────────────────

def test_side_panel_header_shows_id(page: Page):
    """Verify the panel header shows the task ID and title."""
    resp = page.request.post(
        f"{BASE_URL}/api/tasks",
        data={
            "title": "Header ID test",
            "prompt": "Testing header ID display",
            "priority": "medium",
            "mode": "execute",
            "depends_on": [999999],
        },
    )
    task_id = resp.json()["id"]

    page.goto(BASE_URL)
    expect(page.locator(".task-card")).to_have_count(1, timeout=5000)
    page.click(".task-card")

    panel = page.locator(".side-panel")
    expect(panel).to_be_visible(timeout=3000)

    # Header should show "#<id> <title>"
    expect(panel.locator(".panel-header h2")).to_contain_text(f"#{task_id}")
    expect(panel.locator(".panel-header h2")).to_contain_text("Header ID test")


# ── Test 8: Full task execution end-to-end ──────────────────────────────

def test_task_executes_end_to_end(page: Page):
    """Create a simple task and verify it executes through the full pipeline:
    pending → in_progress → completed, with output captured."""
    # Create a very simple task that Claude can complete quickly
    resp = page.request.post(
        f"{BASE_URL}/api/tasks",
        data={
            "title": "E2E execution test",
            "prompt": "Reply with exactly: e2e-success-marker. Nothing else.",
            "priority": "high",
            "mode": "execute",
        },
    )
    assert resp.ok, f"Failed to create task: {resp.status}"
    task_id = resp.json()["id"]

    # Navigate to the app
    page.goto(BASE_URL)

    # Wait for the task to reach the completed column (may skip pending/in_progress
    # if the scheduler is fast). The completed column is the 4th column.
    completed_column = page.locator(".kanban-column").nth(3)
    expect(completed_column.locator(".column-title")).to_have_text("Completed")
    expect(
        completed_column.locator(".card-title:has-text('E2E execution test')")
    ).to_be_visible(timeout=120_000)  # 2 min max for claude to respond

    # Verify the task has output via API
    detail_resp = page.request.get(f"{BASE_URL}/api/tasks/{task_id}")
    assert detail_resp.ok
    task_data = detail_resp.json()["task"]
    assert task_data["status"] == "completed"
    assert task_data["exit_code"] == 0
    assert task_data["output"] is not None
    assert len(task_data["output"]) > 0
