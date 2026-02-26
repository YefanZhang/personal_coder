# PROGRESS.md — personal_coder

## Experience Log

---

### Entry 001 — 2026-02-26: Plan optimization and project bootstrap

**Problem / Change:** `tasks/web_manager_plan.md` contained five categories of implementation-breaking errors: (1) non-existent `claude_agent_sdk` Python package, (2) `ClaudeAgentOptions(permission_mode=...)` parameter that doesn't exist in any SDK, (3) deprecated `@app.on_event("startup")` in FastAPI ≥0.93, (4) `--plan` CLI flag that does not exist in claude CLI, (5) `Database` class referenced throughout but never defined. Additionally the plan had no testing strategy, no startup crash-recovery, no WebSocket dead-connection cleanup, and Phase ordering put UI before parallel execution.

**Solution:** Rewrote `tasks/web_manager_plan.md` with a prominent "Known API Gotchas" section at the top documenting all five bugs with before/after code. Added: `database.py` full interface spec (Section 5.0), `_recover_stuck_tasks()` startup recovery, three new API endpoints (`/api/health`, `/api/tasks/batch`, `/api/tasks/{id}/logs`), WebSocket `broadcast()` with dead-connection cleanup, file-based log storage (`/home/ubuntu/task-logs/task-{id}.log`), token cost tracking fields in schema, bearer token auth via `X-API-Key` + `.env`, and a full testing strategy section (Section 11) covering MCP Playwright + pytest. Reordered Phase 2/3 so worktree parallelization comes before the kanban UI. Updated `CLAUDE.md` Tech Stack, How to Run, How to Run Tests, and Step 5 with Playwright E2E workflow. Added MCP Playwright to `~/.claude/settings.json`. Populated `data/dev-tasks.json` with 11 concrete Phase 1-4 implementation tasks.

**Prevention:**
- Before implementing any SDK usage, always verify the package exists on PyPI/npm with the exact import path.
- Check FastAPI changelog when using lifecycle hooks — `@app.on_event` was deprecated in 0.93 (2023).
- `--plan` is a Claude Code *interactive* feature only. Non-interactive (`-p`) mode has no equivalent flag. Use prompt engineering instead.
- Always define the `Database` interface explicitly before writing modules that depend on it.
- Phase ordering: backend parallelism before frontend polish — race conditions are harder to fix once UI is built on top.

**Commit:** a21c45a (pre-implementation baseline; implementation tasks now in dev-tasks.json)

---

### Entry 002 — 2026-02-26: p1-001 Project scaffold and database layer

**Problem / Change:** Setting up hatchling build backend for the first time — it cannot auto-detect packages when the package name (claude-code-web-manager, with hyphens) doesn't match any directory. Also, `uv sync` only installs base deps; dev extras like pytest are not installed unless `--all-extras` is passed.

**Solution:** Added `[tool.hatch.build.targets.wheel] packages = ["backend", "tests"]` to pyproject.toml. Use `uv sync --all-extras` to install pytest/pytest-asyncio. The `git checkout main` step fails inside a worktree because `main` is checked out in the parent repo — must run `git merge task/<id>` from the main repo directory instead.

**Prevention:**
- Always include `[tool.hatch.build.targets.wheel] packages = [...]` in pyproject.toml when the project name differs from the package directory.
- `uv sync --all-extras` for dev environments; CI can use bare `uv sync`.
- Merge task branches from the main repo directory (`/home/ubuntu/personal_coder`), not from inside the worktree.
- Pushing `--delete` of a remote branch that was never pushed gives a non-fatal error — safe to ignore.

**Commit:** 5e8eee6

---

### Entry 003 — 2026-02-26: p1-002 Task executor engine

**Problem / Change:** FakeProcess mock for `asyncio.subprocess.Process` was missing the `communicate()` method, causing 6/8 tests to fail with `AttributeError`. Also: `git worktree remove` fails with "contains modified or untracked files" when the worktree has a `.venv` directory.

**Solution:** Added `async def communicate(self)` to FakeProcess. Used `git worktree remove --force` for worktrees containing virtual environment directories.

**Prevention:**
- When mocking `asyncio.subprocess.Process`, always stub: `communicate()`, `wait()`, `terminate()`, `stdout` (async iterator), `stderr.read()`, and `returncode`.
- Always use `--force` when removing worktrees that contain `.venv` or other untracked build artifacts.

**Commit:** 8b2a608

---

### Entry 004 — 2026-02-26: p1-003 Task scheduler

**Problem / Change:** Implemented TaskScheduler with async polling loop. No issues — followed patterns established in p1-001/002.

**Solution:** Standard implementation per plan. Used real DB fixture in tests for priority/dependency tests; mock db for unit-level callback tests.

**Prevention:** Tests that exercise ordering must use a real DB; mock DBs can be used for callback behavior.

**Commit:** e467d12

---

### Entry 005 — 2026-02-26: p1-004 FastAPI backend with all REST endpoints

**Problem / Change:** Task was interrupted mid-session — code was implemented but never committed. Worktree existed with unstaged `__pycache__` files blocking `git rebase`. Added `.gitignore` for pycache/venv/db files.

**Solution:** Added `claude-code-web-manager/.gitignore`, committed the already-implemented main.py + test_api.py, used `git stash` to work around pycache blocking rebase, merged from main repo dir per previous lesson.

**Prevention:** Always add `.gitignore` early in project setup. When rebase fails due to untracked files, use `git stash --include-untracked` before rebase.

**Commit:** 11f39f6

---

### Entry 006 — 2026-02-26: p1-005 Simple HTML MVP frontend (table view)

**Problem / Change:** Created single-file SPA frontend with task creation form, task table, WebSocket real-time updates, and cumulative cost footer. No issues.

**Solution:** Single `frontend/index.html` with inline CSS and JS. Dark theme matching GitHub style. WebSocket auto-reconnects on disconnect. Cmd+Enter shortcut for form submission. XSS-safe rendering via `textContent`.

**Prevention:** Always escape user content via DOM `textContent`, never `innerHTML` with raw strings.

**Commit:** eae926d

---

### Entry 007 — 2026-02-26: p2-001 Git worktree lifecycle management

**Problem / Change:** When mocking functions imported with `from X import Y` in Python, patching at `X.Y` does NOT affect the importing module's reference. The importing module binds directly to the function object at import time. Tests that patched `backend.worktree.create_worktree` were accidentally passing because the real `create_worktree` ran (using the separately-mocked `asyncio.create_subprocess_exec`), not because the mock was correctly applied. Tests that needed the mock to raise or track calls failed.

**Solution:** Patch at the import location: `backend.executor.create_worktree`, `backend.executor.remove_worktree`, `backend.executor.cleanup_branch` — i.e., the module where `from ... import` binds the name. Also, when `create_worktree` calls `Path(path).parent.mkdir(parents=True, exist_ok=True)`, tests using `/fake/wt` fail with PermissionError because `/fake` cannot be created. Used `tmp_path` fixture for worktree paths in tests.

**Prevention:**
- When mocking a `from X import Y`-imported function, ALWAYS patch at the consuming module (`consumer_module.Y`), never at the source module (`X.Y`).
- In tests, never use hardcoded paths like `/fake/...` for operations that create directories — always use `tmp_path` to stay within the test's temp filesystem.
- When a test passes "accidentally" (real function runs but happens to work due to other mocks), the test is fragile. Verify mocks are actually intercepting calls by checking the mock was called.

**Commit:** 799cb15

---

### Entry 008 — 2026-02-26: p2-002 Concurrency stress test and race condition fixes

**Problem / Change:** Two race conditions found and fixed: (1) Scheduler only dispatched 1 task per iteration — with 3 available slots and 5 pending tasks, it took 3 loop cycles (6 seconds) to fill all slots. (2) `ConnectionManager.broadcast()` iterated over `self.connections` directly, which could raise `RuntimeError: list changed size during iteration` when concurrent broadcasts or disconnects modified the list.

**Solution:** (1) Extracted `_dispatch_pending()` method with inner `while` loop that dispatches as many tasks as available slots allow per iteration. Added configurable `poll_interval` parameter (default 2.0s) for faster test cycles. (2) Changed `broadcast()` to iterate over `list(self.connections)` (a copy) and guarded `remove()` with `if conn in self.connections` check. Added 5 new tests: concurrency stress test (5 tasks/max 3 concurrent with event-based synchronization), startup recovery (2 tests for crash simulation), DB corruption test under concurrent load, and concurrent count accuracy test. All 68 tests pass.

**Prevention:**
- When iterating over a mutable collection that async callbacks can modify, always iterate over a copy (`list(...)` or `[:]`).
- Scheduler loops should fill all available slots per iteration, not just one — use an inner loop.
- Use `asyncio.Event` for test synchronization instead of `asyncio.sleep` for deterministic, timing-independent tests.
- `poll_interval` should be configurable for testability.

**Commit:** 32f5570

---

### Entry 009 — 2026-02-26: p3-001 React kanban board with WebSocket real-time updates

**Problem / Change:** Replaced the Phase 1 table-based frontend with a full React kanban board. Used React 18, ReactDOM 18, and Babel standalone via CDN (single-file SPA, no build step). Implemented 6 columns (pending, in_progress, review, completed, failed, cancelled), task cards with priority badges, cost display, live elapsed time for in_progress tasks, a slide-in side panel for task details and logs, and real-time WebSocket updates that move cards between columns automatically. No issues encountered — the existing API endpoints and WebSocket protocol supported the kanban UI without changes.

**Solution:** Single `frontend/index.html` with inline `<script type="text/babel">` for JSX. React components: `App` (state management + WebSocket), `CreateForm` (collapsible), `KanbanColumn`, `TaskCard` (with `setInterval` for elapsed time), `SidePanel` (fetches `/api/tasks/{id}` for detail + logs). WebSocket `onmessage` triggers both `fetchTasks()` and, if the side panel is open for the affected task, `fetchTaskDetail()`. Escape key closes the panel.

**Prevention:**
- When using Babel standalone for JSX in a single-file SPA, use `<script type="text/babel">` — Babel picks this up automatically.
- `git stash --include-untracked` before rebase when pycache files block it (recurring pattern from p1-004).

**Commit:** 951fbfa

---

### Entry 010 — 2026-02-26: p3-002 Enhanced task creation form with Plan mode, voice input, templates

**Problem / Change:** Enhanced the CreateForm component with five new features: (1) Plan mode checkbox that sets `mode: "plan"` and prepends a plan-mode prompt prefix, (2) `depends_on` multi-select dropdown populated from existing tasks with chip-based removal, (3) voice input button using Web Speech API (`SpeechRecognition` / `webkitSpeechRecognition`) with continuous recording mode, (4) task template save/load system using localStorage, (5) platform-aware Cmd/Ctrl+Enter hint. Passed `tasks` prop from App to CreateForm so the dependency dropdown can be populated. No backend changes needed — the API already supported `mode`, `depends_on`, and all required fields.

**Solution:** All features implemented in the single-file `frontend/index.html`. Added CSS for plan toggle, dependency chips, voice button with pulse animation, and template row. Templates stored in localStorage under `claude-manager-templates` key. Voice input appends transcribed text to the prompt textarea. Plan mode prepends a structured prefix instructing Claude to analyze, plan, and present for review before executing. The dependency dropdown filters out completed/cancelled tasks and already-selected dependencies.

**Prevention:**
- Web Speech API requires `SpeechRecognition` or `webkitSpeechRecognition` — always feature-detect and hide the button if unavailable.
- When passing data between React components via CDN (no modules), prop drilling is the simplest pattern — `tasks` passed from App to CreateForm.
- localStorage operations should be wrapped in try/catch for private browsing modes that may throw.

**Commit:** 495e391

---

### Entry 011 — 2026-02-26: p3-003 PWA manifest and service worker

**Problem / Change:** Added PWA support: manifest.json (name, short_name, start_url, display: standalone, themed colors matching the dark UI), service worker (cache-first for app shell, network-only for API/WebSocket), SVG app icons (192, 512, maskable-512), and updated index.html with manifest link, apple-mobile-web-app meta tags, favicon link, and SW registration. No issues encountered — straightforward task.

**Solution:** Created `frontend/manifest.json`, `frontend/sw.js`, `frontend/icons/` with 3 SVG icons. Updated `<head>` in index.html with `<link rel="manifest">`, `<meta name="theme-color">`, `<meta name="apple-mobile-web-app-capable">`, `<meta name="apple-mobile-web-app-status-bar-style">`, `<link rel="icon">`, `<link rel="apple-touch-icon">`. Added SW registration script before `</body>`. Service worker uses stale-while-revalidate pattern for cached assets and skips `/api/` and `/ws` paths entirely.

**Prevention:**
- SVG icons work well for PWAs — no need for multiple PNG sizes. Just provide different viewBox dimensions.
- Service workers must explicitly skip API and WebSocket paths to avoid caching dynamic data.
- `apple-mobile-web-app-status-bar-style` should be `black-translucent` for dark-themed apps to avoid a white status bar on iOS.

**Commit:** 9a18f74
