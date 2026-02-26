#!/usr/bin/env bash
# run-tasks.sh — External orchestrator for personal_coder task queue
#
# Invokes one fresh `claude` session per task so each gets:
#   - A clean context window (no accumulated bloat)
#   - Fresh rate-limit budget
#   - Crash isolation (if one task dies, the next picks up)
#
# Usage:
#   ./scripts/run-tasks.sh              # run all pending tasks
#   ./scripts/run-tasks.sh --once       # run exactly one task then exit
#   ./scripts/run-tasks.sh --dry-run    # show what would run without executing

set -euo pipefail

# ── Configuration ────────────────────────────────────────────────────
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
TASKS_FILE="$REPO_DIR/data/dev-tasks.json"
LOG_DIR="$REPO_DIR/logs/orchestrator"
COOLDOWN_SECS="${COOLDOWN_SECS:-15}"          # pause between tasks
FAIL_COOLDOWN_SECS="${FAIL_COOLDOWN_SECS:-60}" # pause after a failure
MAX_CONSECUTIVE_FAILURES="${MAX_CONSECUTIVE_FAILURES:-3}"
CLAUDE_PROMPT="Do the next task."

# ── Flags ────────────────────────────────────────────────────────────
RUN_ONCE=false
DRY_RUN=false
for arg in "$@"; do
  case "$arg" in
    --once)    RUN_ONCE=true ;;
    --dry-run) DRY_RUN=true ;;
    --help|-h)
      echo "Usage: $0 [--once] [--dry-run]"
      echo "  --once     Run exactly one task then exit"
      echo "  --dry-run  Show pending tasks without executing"
      echo ""
      echo "Environment variables:"
      echo "  COOLDOWN_SECS              Seconds between tasks (default: 15)"
      echo "  FAIL_COOLDOWN_SECS         Seconds after failure  (default: 60)"
      echo "  MAX_CONSECUTIVE_FAILURES   Bail after N failures  (default: 3)"
      exit 0
      ;;
    *) echo "Unknown flag: $arg"; exit 1 ;;
  esac
done

# ── Helpers ──────────────────────────────────────────────────────────
mkdir -p "$LOG_DIR"

timestamp() { date '+%Y-%m-%d %H:%M:%S'; }

log() { echo "[$(timestamp)] $*"; }

count_pending() {
  python3 -c "
import json, sys
with open('$TASKS_FILE') as f:
    tasks = json.load(f).get('tasks', json.load(open('$TASKS_FILE')))
if isinstance(tasks, dict):
    tasks = tasks.get('tasks', [])
print(sum(1 for t in tasks if t.get('status') == 'pending'))
"
}

count_in_progress() {
  python3 -c "
import json
with open('$TASKS_FILE') as f:
    data = json.load(f)
tasks = data.get('tasks', data) if isinstance(data, dict) else data
print(sum(1 for t in tasks if t.get('status') == 'in_progress'))
"
}

recover_stale_tasks() {
  # Reset any in_progress tasks back to pending (crash recovery)
  local stale
  stale=$(count_in_progress)
  if [ "$stale" -gt 0 ]; then
    log "RECOVERY: Found $stale stale in_progress task(s), resetting to pending..."
    python3 -c "
import json
with open('$TASKS_FILE', 'r') as f:
    data = json.load(f)
tasks = data.get('tasks', data)
for t in tasks:
    if t.get('status') == 'in_progress':
        t['status'] = 'pending'
with open('$TASKS_FILE', 'w') as f:
    json.dump(data, f, indent=2)
    f.write('\n')
"
    log "RECOVERY: Reset complete."
  fi
}

next_task_id() {
  python3 -c "
import json
with open('$TASKS_FILE') as f:
    data = json.load(f)
tasks = data.get('tasks', data) if isinstance(data, dict) else data
for t in tasks:
    if t.get('status') == 'pending':
        print(t['id'])
        break
"
}

cleanup_orphan_worktrees() {
  local worktree_base="$REPO_DIR/../personal_coder-worktrees"
  if [ -d "$worktree_base" ]; then
    # Prune worktrees whose backing directories are gone
    cd "$REPO_DIR" && git worktree prune 2>/dev/null || true
  fi
}

# ── Main Loop ────────────────────────────────────────────────────────
log "=== Task Orchestrator Started ==="
log "Repo:       $REPO_DIR"
log "Tasks file: $TASKS_FILE"
log "Cooldown:   ${COOLDOWN_SECS}s (normal), ${FAIL_COOLDOWN_SECS}s (failure)"
log "Max consecutive failures: $MAX_CONSECUTIVE_FAILURES"
echo ""

# Recover from previous crashes
recover_stale_tasks
cleanup_orphan_worktrees

consecutive_failures=0
tasks_completed=0

while true; do
  pending=$(count_pending)

  if [ "$pending" -eq 0 ]; then
    log "No pending tasks remaining. Completed $tasks_completed task(s) this run."
    break
  fi

  task_id=$(next_task_id)
  log "── Next task: $task_id ($pending pending) ──"

  if [ "$DRY_RUN" = true ]; then
    log "[DRY RUN] Would invoke: claude -p \"$CLAUDE_PROMPT\" --dangerously-skip-permissions"
    if [ "$RUN_ONCE" = true ]; then break; fi
    # In dry-run, just show all pending and exit
    break
  fi

  # Run claude in the repo directory, log output
  task_log="$LOG_DIR/${task_id}-$(date '+%Y%m%d-%H%M%S').log"
  log "Log file: $task_log"

  set +e
  (
    cd "$REPO_DIR"
    claude -p "$CLAUDE_PROMPT" --dangerously-skip-permissions 2>&1 | tee "$task_log"
  )
  exit_code=$?
  set -e

  if [ $exit_code -eq 0 ]; then
    log "Task $task_id session exited successfully (code 0)."
    consecutive_failures=0
    tasks_completed=$((tasks_completed + 1))

    if [ "$RUN_ONCE" = true ]; then
      log "--once flag set. Exiting after 1 task."
      break
    fi

    log "Cooling down ${COOLDOWN_SECS}s before next task..."
    sleep "$COOLDOWN_SECS"
  else
    consecutive_failures=$((consecutive_failures + 1))
    log "WARNING: Task $task_id session exited with code $exit_code (failure $consecutive_failures/$MAX_CONSECUTIVE_FAILURES)"

    if [ "$consecutive_failures" -ge "$MAX_CONSECUTIVE_FAILURES" ]; then
      log "ERROR: $MAX_CONSECUTIVE_FAILURES consecutive failures. Stopping orchestrator."
      log "Check logs in $LOG_DIR for details."
      exit 1
    fi

    log "Cooling down ${FAIL_COOLDOWN_SECS}s before retry..."
    sleep "$FAIL_COOLDOWN_SECS"
  fi
done

log "=== Task Orchestrator Finished ($tasks_completed tasks completed) ==="
