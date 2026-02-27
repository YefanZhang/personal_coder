# End-to-End Task Flow: Web UI to Merged Code

What happens from the moment you hit "Create" to code landing on main.

## 1. User submits the form

**`frontend/index.html:709-738`** — `handleSubmit()`

The React form collects title, prompt, priority, plan mode toggle, and dependencies. On submit it `POST`s to `/api/tasks`:

```json
{
  "title": "add hello world script",
  "prompt": "Create scripts/hello.sh that prints hello world",
  "priority": "medium",
  "mode": "execute",
  "depends_on": []
}
```

If Plan mode is checked, the frontend prepends its own plan prefix to the prompt and sets `mode: "plan"` (line 713-718).

## 2. API persists the task

**`backend/main.py:115-126`** — `POST /api/tasks`

Inserts a row into SQLite with `status="pending"`. Returns the task object. Nothing runs yet.

## 3. Scheduler picks it up

**`backend/scheduler.py:17-38`** — `start()` → `_dispatch_pending()`

A background loop polls every 2 seconds. Each tick:
- Checks `active_count < max_concurrent` (line 30-31)
- Fetches next pending task by priority (line 33)
- Verifies all `depends_on` tasks are completed (line 36, `_dependencies_met()` at line 43-48)

If all checks pass, `_dispatch()` (line 50-63) sets the task to `IN_PROGRESS` and fires:

```python
asyncio.create_task(self.executor.execute_task(task, self._on_output, self._on_complete))
```

## 4. Worktree created

**`backend/executor.py:56-67`** → **`backend/worktree.py:27-67`**

Before claude runs, the executor creates a git worktree so each task works in isolation:

```
branch:  task-{id}-{title_slug}
path:    /home/ubuntu/personal_coder-worktrees/task-{id}-{title_slug}
```

Runs `git worktree add -b {branch} {path}` in the base repo. If a stale branch exists from a previous attempt, it prunes and force-deletes first.

## 5. Prompt assembled with workflow instructions

**`backend/executor.py:70-102`**

The executor builds the final prompt in layers:

```
[Plan mode prefix, if mode=plan]     (line 72-76)
[User's original prompt]              (line 71)
[Git workflow suffix]                 (line 78-102)
```

### The git workflow suffix

This is the key to automation. The executor appends instructions that tell claude to commit, merge, and push after implementing changes:

```
## Post-Implementation Workflow
IMPORTANT: You are being run by the Claude Code Web Manager, not the task orchestrator.
Ignore the "Task Lifecycle" and "Strict Rules" sections in CLAUDE.md — those steps
(claiming tasks, updating dev-tasks.json, PROGRESS.md, cleanup) are handled by the web manager.

After completing your implementation, you MUST follow these git steps:
1. git add . && git commit -m "[task-{id}] {title}"
2. cd /home/ubuntu/personal_coder && git merge {branch}
3. git push origin main
```

### Why this override is needed

The repo has a `CLAUDE.md` with a "Task Lifecycle" section designed for the CLI task orchestrator (`scripts/run-tasks.sh`). That workflow claims tasks from `dev-tasks.json`, updates `PROGRESS.md`, does cleanup — none of which applies to web manager tasks. The workflow suffix explicitly tells claude to ignore those sections and follow the web manager's git steps instead.

### Why prompt-based, not code-based

The git workflow is injected via prompt rather than as post-execution shell commands in the executor. This is intentional:
- Claude can handle errors intelligently (merge conflicts, push failures) rather than blindly running commands
- The workflow can evolve by editing a string template, not by writing new async subprocess code
- Claude already has `--dangerously-skip-permissions` and can run any git command

## 6. Claude subprocess launched

**`backend/executor.py:104-128`**

```python
cmd = [
    claude_path,
    "-p", prompt,                        # full assembled prompt
    "--dangerously-skip-permissions",    # no human approval needed
    "--output-format", "stream-json",   # NDJSON for real-time parsing
    "--verbose",
]
process = await asyncio.create_subprocess_exec(
    *cmd,
    cwd=worktree_path,                  # isolated worktree
    env=...                             # CLAUDECODE removed, telemetry disabled
)
```

Claude reads the worktree's `CLAUDE.md` automatically (it's a copy of the repo), but the prompt suffix overrides the irrelevant sections.

## 7. Output streamed to browser in real time

**`backend/executor.py:130-184`** → **`backend/scheduler.py:90-96`** → **`backend/main.py:44-56`**

```
claude stdout (NDJSON)
  → executor parses each line (assistant text, tool use summaries, result)
  → on_output() callback
  → scheduler writes DB log + broadcasts via WebSocket
  → browser receives JSON and appends to side panel
```

The kanban card moves to `in_progress` and shows live elapsed time. Clicking the card opens a side panel with streaming logs.

## 8. Claude does the work + git workflow

Inside the worktree, claude:
1. Reads the codebase, implements the requested changes
2. Follows the workflow suffix instructions:
   - `git add .` + `git commit -m "[task-{id}] {title}"`
   - `cd /home/ubuntu/personal_coder` + `git merge {branch}`
   - `git push origin main`

This all happens within the same claude session — no separate scripts or post-hooks.

## 9. Task completes

**`backend/executor.py:187-218`** → **`backend/scheduler.py:98-124`**

After claude exits:

| Exit code | Status | Worktree |
|-----------|--------|----------|
| 0 | `COMPLETED` | Kept (contains committed work) |
| non-zero | `FAILED` | Cleaned up immediately |

The scheduler updates the DB with final status, token counts, cost, and any plan text. WebSocket broadcasts the completion — the kanban card moves to the `completed` or `failed` column.

## 10. What the user sees

```
 Kanban board
 ┌──────────┐  ┌────────────┐  ┌───────────┐
 │ Pending  │  │ In Progress│  │ Completed │
 │          │  │            │  │           │
 │          │→ │ ■ my task  │→ │ ■ my task │
 │          │  │  2m 14s... │  │  $0.03    │
 └──────────┘  └────────────┘  └───────────┘
```

1. Card appears in **Pending** after form submit
2. Moves to **In Progress** when scheduler dispatches (within 2s)
3. Side panel shows live logs (streamed via WebSocket)
4. Moves to **Completed** when claude finishes and git workflow succeeds
5. `git log` on main shows the commit: `[task-{id}] {title}`

## Summary: where automation lives

| Concern | Where it's handled | How |
|---------|-------------------|-----|
| Task scheduling | `scheduler.py` | Async polling loop, priority queue, dependency checks |
| Git isolation | `worktree.py` + `executor.py` | `git worktree add` before each task |
| Code implementation | Claude CLI subprocess | `-p` flag with user's prompt |
| Commit / merge / push | Prompt engineering in `executor.py:78-102` | Workflow suffix appended to every prompt |
| CLAUDE.md conflict | Prompt engineering in `executor.py:82-85` | Explicit "ignore Task Lifecycle" instruction |
| Real-time feedback | WebSocket in `main.py` + NDJSON parsing in `executor.py` | Streamed line-by-line to all connected browsers |
| Failure handling | `executor.py:179-181` + `scheduler.py:109` | Worktree cleanup on failure, status set to FAILED |
