import asyncio
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock
import pytest

from backend.worktree import (
    create_worktree,
    remove_worktree,
    merge_worktree,
    cleanup_branch,
    list_worktrees,
    WorktreeError,
    _run_git,
)


# ── Helpers ──────────────────────────────────────────────────────────────────


def make_git_result(stdout: str = "", stderr: str = "", returncode: int = 0):
    """Create a mock for _run_git return value."""
    async def _mock(*args, **kwargs):
        return stdout, stderr, returncode
    return _mock


# ── Tests with mocked git ────────────────────────────────────────────────────


async def test_create_worktree_success(tmp_path):
    with patch("backend.worktree._run_git") as mock:
        mock.return_value = ("Preparing worktree", "", 0)
        path = await create_worktree(
            base_repo="/fake/repo",
            branch="task-1-test",
            path=str(tmp_path / "wt"),
        )
    assert path == str(tmp_path / "wt")
    mock.assert_called_once_with(
        "worktree", "add", "-b", "task-1-test", str(tmp_path / "wt"),
        cwd="/fake/repo",
    )


async def test_create_worktree_failure_raises(tmp_path):
    with patch("backend.worktree._run_git") as mock:
        mock.return_value = ("", "fatal: branch already exists", 128)
        with pytest.raises(WorktreeError, match="branch already exists"):
            await create_worktree("/fake/repo", "task-1", str(tmp_path / "wt"))


async def test_remove_worktree_success():
    with patch("backend.worktree._run_git") as mock:
        mock.return_value = ("", "", 0)
        await remove_worktree("/fake/repo", "/fake/wt")
    mock.assert_called_once_with(
        "worktree", "remove", "--force", "/fake/wt",
        cwd="/fake/repo",
    )


async def test_remove_worktree_fallback_on_failure(tmp_path):
    """If git worktree remove fails, fallback to shutil + prune."""
    wt_dir = tmp_path / "wt"
    wt_dir.mkdir()
    (wt_dir / "file.txt").write_text("data")

    call_count = 0

    async def fake_run_git(*args, cwd=None):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # First call: worktree remove fails
            return ("", "error: not a worktree", 1)
        else:
            # Second call: worktree prune
            return ("", "", 0)

    with patch("backend.worktree._run_git", side_effect=fake_run_git):
        await remove_worktree("/fake/repo", str(wt_dir))

    # The directory should be cleaned up by shutil.rmtree
    assert not wt_dir.exists()
    assert call_count == 2  # remove + prune


async def test_merge_worktree_success():
    with patch("backend.worktree._run_git") as mock:
        mock.return_value = ("Merge made by the 'ort' strategy.", "", 0)
        success, output = await merge_worktree("/fake/repo", "task-1-test")
    assert success is True
    assert "Merge made" in output


async def test_merge_worktree_conflict():
    with patch("backend.worktree._run_git") as mock:
        mock.return_value = ("", "CONFLICT (content): merge conflict in file.py", 1)
        success, output = await merge_worktree("/fake/repo", "task-1-test")
    assert success is False
    assert "CONFLICT" in output


async def test_cleanup_branch():
    with patch("backend.worktree._run_git") as mock:
        mock.return_value = ("Deleted branch task-1-test", "", 0)
        await cleanup_branch("/fake/repo", "task-1-test")
    mock.assert_called_once_with("branch", "-D", "task-1-test", cwd="/fake/repo")


async def test_list_worktrees():
    porcelain_output = (
        "worktree /fake/repo\n"
        "HEAD abc123\n"
        "branch refs/heads/main\n"
        "\n"
        "worktree /home/ubuntu/worktrees/task-1\n"
        "HEAD def456\n"
        "branch refs/heads/task-1\n"
        "\n"
        "worktree /home/ubuntu/worktrees/task-2\n"
        "HEAD 789abc\n"
        "branch refs/heads/task-2\n"
    )
    with patch("backend.worktree._run_git") as mock:
        mock.return_value = (porcelain_output, "", 0)
        paths = await list_worktrees("/fake/repo")
    assert paths == [
        "/home/ubuntu/worktrees/task-1",
        "/home/ubuntu/worktrees/task-2",
    ]


async def test_list_worktrees_empty_on_error():
    with patch("backend.worktree._run_git") as mock:
        mock.return_value = ("", "not a git repository", 128)
        paths = await list_worktrees("/fake/repo")
    assert paths == []


# ── Integration test with real git (uses tmp_path) ───────────────────────────


@pytest.fixture
def git_repo(tmp_path):
    """Create a minimal real git repo for integration testing."""
    import subprocess
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=str(repo), check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=str(repo), check=True, capture_output=True,
    )
    # Create initial commit so branches can be created
    (repo / "README.md").write_text("# Test")
    subprocess.run(["git", "add", "."], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "initial"],
        cwd=str(repo), check=True, capture_output=True,
    )
    return repo


async def test_integration_create_and_remove_worktree(git_repo, tmp_path):
    """End-to-end test: create a worktree, verify it exists, remove it."""
    wt_path = str(tmp_path / "wt-integration")
    branch = "test-branch-integration"

    # Create
    result = await create_worktree(str(git_repo), branch, wt_path)
    assert result == wt_path
    assert Path(wt_path).exists()

    # Verify it shows up in list
    paths = await list_worktrees(str(git_repo))
    assert wt_path in paths

    # Remove
    await remove_worktree(str(git_repo), wt_path)
    assert not Path(wt_path).exists()


async def test_integration_parallel_worktrees(git_repo, tmp_path):
    """Create 3 worktrees in parallel, verify all exist, then clean up."""
    branches = [f"parallel-{i}" for i in range(3)]
    paths = [str(tmp_path / f"wt-parallel-{i}") for i in range(3)]

    # Create all 3 in parallel
    results = await asyncio.gather(
        create_worktree(str(git_repo), branches[0], paths[0]),
        create_worktree(str(git_repo), branches[1], paths[1]),
        create_worktree(str(git_repo), branches[2], paths[2]),
    )

    # All 3 should exist
    for p in paths:
        assert Path(p).exists(), f"Worktree {p} should exist"

    # List should show all 3
    listed = await list_worktrees(str(git_repo))
    for p in paths:
        assert p in listed, f"Worktree {p} should be in list"

    # Clean up all 3
    await asyncio.gather(
        remove_worktree(str(git_repo), paths[0]),
        remove_worktree(str(git_repo), paths[1]),
        remove_worktree(str(git_repo), paths[2]),
    )

    for p in paths:
        assert not Path(p).exists(), f"Worktree {p} should be removed"


async def test_integration_merge_worktree(git_repo, tmp_path):
    """Create a worktree, make a commit in it, merge back."""
    import subprocess

    wt_path = str(tmp_path / "wt-merge")
    branch = "merge-test-branch"

    await create_worktree(str(git_repo), branch, wt_path)

    # Make a change in the worktree
    (Path(wt_path) / "new_file.txt").write_text("hello from worktree")
    subprocess.run(["git", "add", "."], cwd=wt_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "add file from worktree"],
        cwd=wt_path, check=True, capture_output=True,
    )

    # Merge back into main repo
    success, output = await merge_worktree(str(git_repo), branch)
    assert success is True

    # Verify the file exists in the main repo
    assert (git_repo / "new_file.txt").exists()

    # Clean up
    await remove_worktree(str(git_repo), wt_path)
    await cleanup_branch(str(git_repo), branch)
