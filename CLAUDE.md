# CLAUDE.md — personal_coder

## Project Overview

**Name:** personal_coder
**Repo:** /home/ubuntu/personal_coder
**Worktrees base:** /home/ubuntu/personal_coder-worktrees/

### Tech Stack
- Language/Runtime: Python 3.11
- Framework: FastAPI + uvicorn
- Database: SQLite + aiosqlite
- Package manager: uv
- Frontend: React (single-file SPA) or HTMX
- Real-time: WebSocket
- Testing: pytest + MCP Playwright (E2E)

### Key Files
- `data/dev-tasks.json` — task queue (source of truth for pending work)
- `PROGRESS.md` — running log of completed work and lessons learned
- `CLAUDE.md` — this file

### How to Run
```bash
# Install dependencies (first time)
cd claude-code-web-manager && uv sync

# Start the backend
uvicorn backend.main:app --reload --port 8000

# Or via systemd (production)
sudo systemctl start claude-manager
```

### How to Run Tests
```bash
# Unit + integration tests
pytest tests/ -v --tb=short

# Full E2E suite (requires server running on port 8000)
uvicorn backend.main:app --port 8000 &
# Then use MCP Playwright tools: browser_navigate http://localhost:8000
# See tasks/web_manager_plan.md Section 11 for full E2E workflow
```

---

## Task Lifecycle

Follow these steps **in exact order** for every task. Do not skip, reorder, or combine steps.

### Step 1 — CLAIM TASK
- Read `PROGRESS.md` in full before doing anything else — apply all past lessons to this task
- Read `data/dev-tasks.json`
- Pick the **first** task whose `status` is `"pending"`
- Immediately set its `status` to `"in_progress"` and save the file
- Do **nothing else** until the file is saved

### Step 2 — CREATE WORKSPACE
```bash
git worktree add -b task/[id] ../personal_coder-worktrees/task-[id]
```
Replace `[id]` with the task's `id` field.

### Step 3 — IMPLEMENT
- `cd` into `../personal_coder-worktrees/task-[id]`
- Do **all** work inside the worktree directory
- Do not touch the main repo directory during implementation

### Step 4 — COMMIT
```bash
git add .
git commit -m "[task-id] description of what was done"
```

### Step 5 — MERGE AND TEST
```bash
git fetch origin
git merge origin/main
```
Then run the full test suite in this order:

1. **Unit tests:** `pytest tests/ -v --tb=short`
2. **E2E tests (if backend exists):**
   ```bash
   uvicorn backend.main:app --port 8000 &
   SERVER_PID=$!
   # Use MCP Playwright tools to:
   #   a. browser_navigate http://localhost:8000
   #   b. browser_fill the task creation form and submit
   #   c. browser_wait_for the task card to appear in "pending" column
   #   d. Assert GET /api/health returns {"status": "ok"}
   kill $SERVER_PID
   ```
3. If any test fails: fix the code and re-run before proceeding to Step 6.

### Step 6 — AUTO-MERGE TO MAIN
```bash
git fetch origin main
git rebase origin/main
```
- If rebase fails, follow the **Conflict Resolution** table to resolve, then `git rebase --continue`
- If rebase succeeds:
```bash
git checkout main
git merge task/[id]
git push origin main
```
- If any part of this step fails, go back to Step 5 (merge, test, and retry)

### Step 7 — MARK DONE  ← must happen BEFORE step 8
- Open `data/dev-tasks.json`
- Set the task's `status` to `"done"`
- Save the file

> This MUST happen before Step 8. If the process is killed during cleanup, task state must not be lost.

### Step 8 — CLEANUP
```bash
git worktree remove ../personal_coder-worktrees/task-[id]
git branch -d task/[id]
git push origin --delete task/[id]
```
Then restart the dev server if one is running.

### Step 9 — LESSONS
Append an entry to `PROGRESS.md` using the Experience Log Rules below.

### Step 10 — END SESSION
- Stop responding. Do **not** pick up another task.
- The external orchestrator (`scripts/run-tasks.sh`) will start a fresh `claude` session for the next pending task.
- This ensures each task gets a clean context window and fresh rate-limit budget.

---

## Experience Log Rules

After every task completion **OR** whenever you encounter any problem during a task, append an entry to `PROGRESS.md` with these exact fields:

- **Problem / Change:** What problem was encountered, or what important change was made
- **Solution:** How it was solved
- **Prevention:** How to avoid it in the future
- **Commit:** The git commit ID related to this change (mandatory)

**Never make the same mistake twice. Always read PROGRESS.md at the start of each task before doing any work.**

---

## Conflict Resolution

| Situation | Action |
|-----------|--------|
| `git rebase` fails | Resolve conflicts keeping **both** changes, run `git rebase --continue`, retry the rebase |
| Tests fail | Fix the failing code before marking done — never skip tests |
| Any single step fails 3× in a row | Set task `status` to `"failed"` with an `"error"` note in `dev-tasks.json`, write the error to `PROGRESS.md`, then `exit 1` |

---

## Strict Rules

1. **NEVER** ask for permission on anything
2. **NEVER** mark a task as done before step 7
3. **NEVER** skip cleanup in step 8
4. **ALWAYS** complete or fail exactly one task per session, then stop
5. Step 7 **must** always happen before step 8, even if step 8 fails
6. **NEVER** pick up a second task in the same session — the orchestrator handles this

---

## Task Orchestrator

Tasks are run via `scripts/run-tasks.sh`, which invokes one fresh `claude` session per task.

### Why
- **Fresh context per task** — no accumulated bloat from prior tasks
- **Rate-limit isolation** — each session starts with a clean token budget
- **Crash recovery** — if a session dies, the orchestrator resets stale `in_progress` tasks to `pending` and retries
- **Clean separation** — no stale assumptions carried between tasks

### Usage
```bash
# Run all pending tasks (one at a time, fresh session each)
./scripts/run-tasks.sh

# Run exactly one task then stop
./scripts/run-tasks.sh --once

# Preview what would run
./scripts/run-tasks.sh --dry-run
```

### Configuration (environment variables)
| Variable | Default | Description |
|----------|---------|-------------|
| `COOLDOWN_SECS` | 15 | Pause between successful tasks |
| `FAIL_COOLDOWN_SECS` | 60 | Pause after a failed task |
| `MAX_CONSECUTIVE_FAILURES` | 3 | Stop orchestrator after N consecutive failures |

### Recovery
On startup, the orchestrator:
1. Resets any `in_progress` tasks back to `pending` (crash recovery)
2. Prunes orphaned git worktrees
3. Then begins the task loop
