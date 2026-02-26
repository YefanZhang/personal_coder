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
