# Claude Code Web Manager

A web-based task management system for orchestrating [Claude Code](https://docs.anthropic.com/en/docs/claude-code) instances. Submit tasks through a kanban board UI, and the system executes them in isolated git worktrees with real-time progress updates via WebSocket.

## Features

- **Kanban board** — 6-column board (Pending, In Progress, Review, Completed, Failed, Cancelled) with drag-free auto-updating via WebSocket
- **Task execution** — Spawns Claude Code CLI in isolated git worktrees, one branch per task
- **Concurrency** — Configurable parallel task limit with dependency-aware scheduling
- **Plan mode** — Tasks can be submitted in plan-only mode for review before execution
- **Real-time updates** — WebSocket broadcasts move task cards between columns as status changes
- **Voice input** — Web Speech API integration for dictating task prompts
- **Templates** — Save and load task templates via localStorage
- **PWA** — Installable as a standalone app with offline shell caching
- **Task orchestrator** — Shell script that runs one task per fresh Claude session with crash recovery

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Backend | Python 3.11, FastAPI, Uvicorn |
| Database | SQLite + aiosqlite |
| Frontend | React 18 (CDN, single-file SPA, no build step) |
| Real-time | WebSocket |
| Task runner | Claude Code CLI (`claude -p`) |
| Isolation | Git worktrees (one branch per task) |
| Testing | pytest, pytest-asyncio, Playwright (E2E) |
| Package manager | uv |

## Project Structure

```
├── claude-code-web-manager/       # Main application
│   ├── backend/
│   │   ├── main.py                # FastAPI app, REST + WebSocket endpoints
│   │   ├── models.py              # Pydantic models (Task, TaskLog, enums)
│   │   ├── database.py            # SQLite layer with async CRUD
│   │   ├── executor.py            # Subprocess executor (runs Claude CLI)
│   │   ├── scheduler.py           # Async polling scheduler with concurrency
│   │   └── worktree.py            # Git worktree create/remove/merge
│   ├── frontend/
│   │   ├── index.html             # React SPA (kanban board, forms, side panel)
│   │   ├── manifest.json          # PWA manifest
│   │   ├── sw.js                  # Service worker
│   │   └── icons/                 # SVG app icons
│   ├── scripts/
│   │   └── launcher.sh            # Batch task submission from file
│   ├── tests/                     # Unit, integration, and E2E tests
│   └── pyproject.toml
├── scripts/
│   └── run-tasks.sh               # Task orchestrator (one session per task)
├── data/
│   └── dev-tasks.json             # Task queue (source of truth)
├── tasks/
│   └── web_manager_plan.md        # Implementation plan
├── CLAUDE.md                      # Developer guidelines and task lifecycle
└── PROGRESS.md                    # Experience log
```

## Quick Start

```bash
# Install dependencies
cd claude-code-web-manager
uv sync --all-extras

# Start the server
uvicorn backend.main:app --reload --port 8000
```

Open http://localhost:8000 in your browser.

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/health` | Health check |
| `POST` | `/api/tasks` | Create a task |
| `POST` | `/api/tasks/batch` | Create multiple tasks |
| `GET` | `/api/tasks` | List tasks (filterable by `?status=`) |
| `GET` | `/api/tasks/{id}` | Task detail with logs |
| `GET` | `/api/tasks/{id}/logs` | Task logs only |
| `POST` | `/api/tasks/{id}/cancel` | Cancel a task |
| `POST` | `/api/tasks/{id}/retry` | Retry a failed task |
| `POST` | `/api/tasks/{id}/approve-plan` | Approve plan and execute |
| `DELETE` | `/api/tasks/{id}` | Delete a task |
| `WebSocket` | `/ws` | Real-time task updates |

## Configuration

Environment variables (set in `.env` or export directly):

| Variable | Default | Description |
|----------|---------|-------------|
| `DB_PATH` | `tasks.db` | SQLite database path |
| `MAX_CONCURRENT` | `3` | Max tasks running simultaneously |
| `BASE_REPO` | `/home/ubuntu/project` | Git repo for worktree creation |
| `LOG_DIR` | `/home/ubuntu/task-logs` | Task execution log directory |
| `API_KEY` | *(empty)* | Bearer token for auth (disabled if empty) |

## Running Tests

```bash
cd claude-code-web-manager

# Unit + integration tests
pytest tests/ -v --tb=short

# E2E tests (requires server running on port 8000)
uvicorn backend.main:app --port 8000 &
pytest tests/e2e/ -v --tb=short
```

## Task Orchestrator

The shell-based orchestrator (`scripts/run-tasks.sh`) runs tasks from `data/dev-tasks.json` one at a time, each in a fresh Claude session:

```bash
./scripts/run-tasks.sh           # Run all pending tasks
./scripts/run-tasks.sh --once    # Run exactly one task
./scripts/run-tasks.sh --dry-run # Preview what would run
```

On startup it resets stale `in_progress` tasks to `pending` and prunes orphaned worktrees.

| Variable | Default | Description |
|----------|---------|-------------|
| `COOLDOWN_SECS` | `15` | Pause between tasks |
| `FAIL_COOLDOWN_SECS` | `60` | Pause after a failure |
| `MAX_CONSECUTIVE_FAILURES` | `3` | Stop after N consecutive failures |

## How It Works

1. **Submit** a task via the web UI or API with a title and prompt
2. **Scheduler** picks up pending tasks respecting priority, dependencies, and concurrency limits
3. **Executor** creates an isolated git worktree, runs `claude -p "<prompt>"` as a subprocess
4. **Output** streams back through the database and WebSocket to the kanban board in real-time
5. **Completion** records token usage, cost, and exit code; worktree is cleaned up

## License

[Boost Software License 1.0](LICENSE)
