# CLAUDE.md — personal_coder

## Project Overview

**Name:** personal_coder
**Repo:** /home/ubuntu/personal_coder
**Worktrees base:** /home/ubuntu/personal_coder-worktrees/

### Tech Stack
<!-- Update this section as the project grows -->
- Language/Runtime: TBD
- Framework: TBD
- Database: TBD
- Package manager: TBD

### Key Files
- `data/dev-tasks.json` — task queue (source of truth for pending work)
- `PROGRESS.md` — running log of completed work and lessons learned
- `CLAUDE.md` — this file

### How to Run
```bash
# Update these commands when a dev server is added
# e.g.: npm run dev  |  python main.py  |  go run .
echo "No dev server configured yet"
```

### How to Run Tests
```bash
# Update this when a test suite is added
# e.g.: npm test  |  pytest  |  go test ./...
echo "No tests configured yet"
```

---

## Task Lifecycle

Follow these steps **in exact order** for every task. Do not skip, reorder, or combine steps.

### Step 1 — CLAIM TASK
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
Then run the project test command (see **How to Run Tests** above).
If tests fail: fix the failing code and re-run before proceeding.

### Step 6 — PUSH TO MAIN
```bash
git rebase origin/main
git checkout main
git merge task/[id]
git push origin main
```

### Step 7 — MARK DONE  ← must happen BEFORE step 8
- Open `data/dev-tasks.json`
- Set the task's `status` to `"done"`
- Save the file

### Step 8 — CLEANUP
```bash
git worktree remove ../personal_coder-worktrees/task-[id]
git branch -d task/[id]
git push origin --delete task/[id]
```
Then restart the dev server if one is running.

### Step 9 — LESSONS
Append a short summary to `PROGRESS.md`:
```
## [task-id] — [short title]
Date: YYYY-MM-DD
What was done: ...
Issues encountered: ...
```

### Step 10 — EXIT
```bash
exit 0
```

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
4. **ALWAYS** exit after completing or failing exactly one task
5. Step 7 **must** always happen before step 8, even if step 8 fails
