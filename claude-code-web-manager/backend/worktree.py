import asyncio
import shutil
from pathlib import Path
from typing import Optional


class WorktreeError(Exception):
    """Raised when a git worktree operation fails."""
    pass


async def _run_git(
    *args: str,
    cwd: str,
) -> tuple[str, str, int]:
    """Run a git command and return (stdout, stderr, returncode)."""
    proc = await asyncio.create_subprocess_exec(
        "git", *args,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return stdout.decode(), stderr.decode(), proc.returncode


async def create_worktree(
    base_repo: str,
    branch: str,
    path: str,
) -> str:
    """Create a git worktree with a new branch.

    If the branch already exists (e.g. from a previous failed/retried task),
    cleans up the stale branch and worktree directory, then retries.

    Args:
        base_repo: Path to the main git repository.
        branch: Branch name to create.
        path: Filesystem path for the worktree.

    Returns:
        The worktree path on success.

    Raises:
        WorktreeError: If the git worktree command fails.
    """
    # Ensure parent directory exists
    Path(path).parent.mkdir(parents=True, exist_ok=True)

    stdout, stderr, rc = await _run_git(
        "worktree", "add", "-b", branch, path,
        cwd=base_repo,
    )
    if rc != 0:
        # Branch already exists — clean up stale state and retry
        if "already exists" in stderr:
            await _cleanup_stale_branch(base_repo, branch, path)
            stdout, stderr, rc = await _run_git(
                "worktree", "add", "-b", branch, path,
                cwd=base_repo,
            )
            if rc != 0:
                raise WorktreeError(f"worktree creation failed after cleanup: {stderr.strip()}")
            return path
        raise WorktreeError(f"worktree creation failed: {stderr.strip()}")
    return path


async def _cleanup_stale_branch(
    base_repo: str,
    branch: str,
    path: str,
) -> None:
    """Remove a stale branch and any associated worktree so it can be recreated.

    This handles the case where a previous task attempt left behind a branch
    (and possibly a worktree directory) that wasn't fully cleaned up.
    """
    # Remove the worktree directory if it still exists
    if Path(path).exists():
        shutil.rmtree(path, ignore_errors=True)

    # Prune worktree records that point to missing directories
    await _run_git("worktree", "prune", cwd=base_repo)

    # Force-delete the branch (may fail if still checked out — that's ok,
    # prune above should have fixed that)
    await _run_git("branch", "-D", branch, cwd=base_repo)


async def remove_worktree(
    base_repo: str,
    path: str,
) -> None:
    """Remove a git worktree and its directory.

    Uses --force to handle worktrees with untracked files (.venv, __pycache__).

    Args:
        base_repo: Path to the main git repository.
        path: Filesystem path of the worktree to remove.

    Raises:
        WorktreeError: If removal fails even with --force.
    """
    stdout, stderr, rc = await _run_git(
        "worktree", "remove", "--force", path,
        cwd=base_repo,
    )
    if rc != 0:
        # Fallback: prune + manual removal if the worktree is already gone
        if Path(path).exists():
            shutil.rmtree(path, ignore_errors=True)
        await _run_git("worktree", "prune", cwd=base_repo)


async def merge_worktree(
    base_repo: str,
    branch: str,
) -> tuple[bool, str]:
    """Merge a worktree branch into the current branch of base_repo.

    Args:
        base_repo: Path to the main git repository.
        branch: Branch name to merge.

    Returns:
        (success, output) tuple.
    """
    stdout, stderr, rc = await _run_git(
        "merge", branch,
        cwd=base_repo,
    )
    output = stdout + stderr
    return rc == 0, output.strip()


async def cleanup_branch(
    base_repo: str,
    branch: str,
) -> None:
    """Delete a local branch after worktree removal.

    Args:
        base_repo: Path to the main git repository.
        branch: Branch name to delete.
    """
    # Use -D (force delete) since the branch may not be fully merged
    await _run_git("branch", "-D", branch, cwd=base_repo)


async def list_worktrees(base_repo: str) -> list[str]:
    """List all worktree paths for a repository.

    Args:
        base_repo: Path to the main git repository.

    Returns:
        List of worktree paths (excluding the main worktree).
    """
    stdout, stderr, rc = await _run_git(
        "worktree", "list", "--porcelain",
        cwd=base_repo,
    )
    if rc != 0:
        return []

    paths = []
    for line in stdout.splitlines():
        if line.startswith("worktree "):
            wt_path = line[len("worktree "):]
            # Skip the main worktree (it's the base_repo itself)
            if wt_path != base_repo:
                paths.append(wt_path)
    return paths
