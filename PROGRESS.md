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
