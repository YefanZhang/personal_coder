# How a Claude Code Session Gets Started

## Flow Overview

```
POST /api/tasks  →  Scheduler poll (2s)  →  Worktree created  →  claude subprocess launched
```

## Step-by-step

### 1. Task created via API

**`backend/main.py:115-126`** — `POST /api/tasks`

Inserts a row into SQLite with `status="pending"`. Nothing executes yet.

### 2. Scheduler picks it up

**`backend/scheduler.py:17-25`** — `TaskScheduler.start()`

A background loop runs every 2 seconds. Each tick calls `_dispatch_pending()` (line 27-38), which:

- Checks `active_count < max_concurrent` (default 3)
- Calls `db.get_next_pending_task()` — returns highest-priority pending task
- Checks `_dependencies_met()` — all `depends_on` tasks must be completed
- If all checks pass, calls `_dispatch()` (line 50-63)

`_dispatch()` sets the task to `IN_PROGRESS` in the DB, broadcasts via WebSocket, then fires off the executor:

```python
asyncio.create_task(self.executor.execute_task(task, self._on_output, self._on_complete))
```

### 3. Git worktree created

**`backend/executor.py:56-67`** → **`backend/worktree.py:27-67`**

Before claude runs, the executor creates an isolated git worktree:

```
branch: task-{id}-{title_slug}
path:   /home/ubuntu/personal_coder-worktrees/task-{id}-{title_slug}
```

Under the hood this runs `git worktree add -b {branch} {path}` in the base repo. If the branch already exists (stale retry), it prunes and force-deletes it first (`_cleanup_stale_branch`, worktree.py:70-89).

### 4. Subprocess launched

**`backend/executor.py:104-128`**

The actual claude CLI invocation:

```python
cmd = [
    claude_path,
    "-p", prompt,                        # task prompt + workflow suffix
    "--dangerously-skip-permissions",
    "--output-format", "stream-json",
    "--verbose",
]

process = await asyncio.create_subprocess_exec(
    *cmd,
    cwd=worktree_path,                   # runs inside the worktree
    stdout=asyncio.subprocess.PIPE,
    stderr=asyncio.subprocess.PIPE,
    env=self._build_subprocess_env(),    # strips CLAUDECODE, adds disable-traffic flag
)
```

Key details:
- **`-p`** runs claude non-interactively with the given prompt
- **`--output-format stream-json`** produces NDJSON (one JSON object per line) for real-time parsing
- **`cwd=worktree_path`** — claude operates in the isolated worktree, not the main repo
- **`_build_subprocess_env()`** (line 40-45) removes `CLAUDECODE` env var to avoid nesting detection

### 5. Output streamed in real time

**`backend/executor.py:130-184`**

The executor reads stdout line-by-line as NDJSON events:

| Event type | What happens |
|-----------|--------------|
| `system`  | Logs the model name |
| `assistant` | Extracts text blocks and tool-use summaries, calls `on_output()` |
| `result`  | Captures final output, token counts, and cost |

Each `on_output()` call flows through **`scheduler.py:90-96`** which writes a DB log entry and broadcasts the chunk to all connected WebSocket clients.

Raw NDJSON is also written to `/home/ubuntu/task-logs/task-{id}.log`.

### 6. Completion

**`backend/executor.py:187-218`** → **`backend/scheduler.py:98-124`**

After the process exits:
- Exit code 0 → status `COMPLETED`; worktree kept (contains committed changes)
- Exit code != 0 → status `FAILED`; worktree + branch cleaned up immediately
- Final DB update stores: output, error, input/output tokens, cost_usd, plan text, completed_at
- WebSocket broadcast notifies all clients of the final state
