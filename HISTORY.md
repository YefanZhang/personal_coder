# Historical Task List — personal_coder

## Completed Tasks

| # | Task ID | Name | Date | Commit | Abstract |
|---|---------|------|------|--------|----------|
| 1 | — | Initial commit | 2026-02-25 | `9fc330e` | Created the initial repository with base project structure. |
| 2 | — | Task automation infrastructure | 2026-02-25 | `7f1ad5a` | Added task automation scripts and infrastructure for orchestrating automated task execution. |
| 3 | — | Plan optimization & bootstrap | 2026-02-26 | `a21c45a` | Rewrote `web_manager_plan.md` fixing 5 categories of API errors (non-existent SDK, deprecated lifecycle hooks, missing CLI flags). Added database interface spec, startup recovery, WebSocket cleanup, auth, testing strategy. Populated `dev-tasks.json` with 11 implementation tasks across 4 phases. |
| 4 | p1-001 | Project scaffold and database layer | 2026-02-26 | `5e8eee6` | Created project structure with `pyproject.toml`, implemented `database.py` (SQLite + aiosqlite), Pydantic models, schema with indexes, and startup recovery logic. Full test suite in `test_database.py`. |
| 5 | p1-002 | Task executor engine (subprocess) | 2026-02-26 | `8b2a608` | Implemented `executor.py` using `asyncio.create_subprocess_exec` to run Claude CLI. Handles git worktree creation, stdout streaming, log file writing, JSON output parsing for token usage, and Plan mode via prompt prefix. |
| 6 | p1-003 | Task scheduler | 2026-02-26 | `e467d12` | Implemented `scheduler.py` with async polling loop, `max_concurrent` limit, dependency checking via `_dependencies_met()`, and task dispatch via `asyncio.create_task`. Tests cover priority ordering, dependency blocking, and concurrency limits. |
| 7 | p1-004 | FastAPI backend with all REST endpoints | 2026-02-26 | `11f39f6` | Implemented `main.py` with `asynccontextmanager` lifespan, 10 REST endpoints (health, CRUD, batch, cancel, retry, approve-plan), WebSocket `/ws` with dead-connection cleanup, and `X-API-Key` auth. Full API test suite. |
| 8 | p1-005 | Simple HTML MVP frontend (table view) | 2026-02-26 | `eae926d` | Created single-file SPA `frontend/index.html` with task creation form, HTML table view, WebSocket real-time updates, dark GitHub-style theme, Cmd+Enter shortcut, XSS-safe rendering, and cumulative cost footer. |
| 9 | p2-001 | Git worktree lifecycle management | 2026-02-26 | `799cb15` | Implemented `worktree.py` with `create_worktree`, `remove_worktree`, `merge_worktree`. Updated executor for worktree integration. Added cleanup on cancel/failure and `scripts/launcher.sh` for batch task submission. |
| 10 | p2-002 | Concurrency stress test & race condition fixes | 2026-02-26 | `32f5570` | Fixed two race conditions: scheduler dispatching only 1 task per iteration (now fills all slots), and `broadcast()` iterating over mutable connection list. Added 5 new tests including concurrency stress (5 tasks / max 3 concurrent). 68 tests passing. |
| 11 | p3-001 | React kanban board with WebSocket updates | 2026-02-26 | `951fbfa` | Replaced table frontend with full React 18 kanban board (6 columns). Task cards with priority badges, cost display, live elapsed time. Slide-in side panel for task details/logs. Real-time WebSocket card movement. Single-file SPA via CDN. |
| 12 | p3-002 | Enhanced task creation form | 2026-02-26 | `495e391` | Added Plan mode checkbox, `depends_on` multi-select dropdown, Web Speech API voice input with continuous recording, task template save/load via localStorage, and platform-aware keyboard shortcut hints. |
| 13 | p3-003 | PWA manifest and service worker | 2026-02-26 | `9a18f74` | Added `manifest.json`, `sw.js` (cache-first for shell, network-only for API/WS), SVG app icons (192/512/maskable), and mobile web app meta tags. Enables "Add to Home Screen" on mobile. |
| 14 | p4-001 | E2E Playwright test suite | 2026-02-26 | `65d8e6d` | Created `tests/e2e/test_kanban.py` with 7 Playwright tests: navigation, task creation, pending column verification, health endpoint, WebSocket connection, screenshot capture, and side panel interaction. |
| 15 | — | Executor error handling fix | 2026-02-26 | `660eb86` | Fixed executor to catch all exceptions during worktree creation, not just `WorktreeError`. |
| 16 | task-45 | Update README with project documentation | 2026-02-27 | `d7ebca4` | Comprehensive README update with full project documentation covering architecture, setup, usage, and API reference. |
| 17 | task-registry | Unified task registry | 2026-02-27 | `32bf138` | Implemented unified task registry that syncs web manager tasks to `dev-tasks.json`, ensuring consistency between the web UI and the file-based task queue. |
| 18 | — | E2E flow and session lifecycle docs | 2026-02-27 | `0b9b6fd` | Added documentation for E2E testing flow and session lifecycle management. |
| 19 | task-49 | Screenshot task | 2026-02-28 | `512dee7` | Implemented screenshot capture functionality for task output visualization. |
| 20 | — | Subprocess buffer fix | 2026-02-28 | `98be6d8` | Fixed `LimitOverrunError` by replacing line-based stdout reading with chunk-based approach and increasing subprocess buffer to 1MB. |
| 21 | task-63 | Refine README | 2026-02-28 | `4ceaf43` | Refined and polished the project README for clarity and completeness. |
