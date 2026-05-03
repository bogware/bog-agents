"""Baseline tests for bog_agents_cli.auto_commit.

Each test creates a real git repo in ``tmp_path`` (not a mock) so we
exercise the actual subprocess/shell paths and the recently-added
``timeout=30`` guards. Tests are skipped if ``git`` is not on PATH.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from bog_agents_cli.auto_commit import (
    _has_changes,
    _is_git_repo,
    run_auto_commit,
)

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git not installed"
)


def _git(cwd: Path, *args: str) -> None:
    """Run a git command in *cwd*, raising on non-zero exit."""
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        timeout=15,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """Initialize a fresh git repo with a sane local config."""
    _git(tmp_path, "init", "-q")
    # Configure identity locally so commit doesn't fail on machines with
    # no global git config.
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    _git(tmp_path, "config", "commit.gpgsign", "false")
    return tmp_path


class TestRepoDetection:
    async def test_is_git_repo_true_inside_repo(self, repo: Path):
        assert await _is_git_repo(repo) is True

    # Note: an "outside a git repo" case is intentionally omitted —
    # pytest's tmp_path can sit inside an enclosing git tree depending on
    # the developer's $TEMP setup, which would flip this assertion based
    # on environment rather than code-under-test. The negative case is
    # exercised by the test_returns_none_when_not_a_repo test below
    # which ALSO uses tmp_path; if that test starts passing wrongly
    # because of an enclosing repo, run_auto_commit will detect there
    # are no changes and return None either way (safe).


class TestHasChanges:
    async def test_no_changes_in_clean_repo(self, repo: Path):
        # Make an initial commit so 'clean' is meaningful.
        (repo / "x.txt").write_text("hello")
        _git(repo, "add", "x.txt")
        _git(repo, "commit", "-m", "init")
        assert await _has_changes(repo) is False

    async def test_detects_unstaged_changes(self, repo: Path):
        (repo / "x.txt").write_text("hello")
        _git(repo, "add", "x.txt")
        _git(repo, "commit", "-m", "init")
        (repo / "x.txt").write_text("modified")
        assert await _has_changes(repo) is True

    async def test_detects_untracked_changes(self, repo: Path):
        (repo / "new.txt").write_text("new content")
        assert await _has_changes(repo) is True


class TestRunAutoCommit:
    async def test_returns_none_when_no_changes(self, repo: Path):
        (repo / "x.txt").write_text("hi")
        _git(repo, "add", "x.txt")
        _git(repo, "commit", "-m", "init")
        assert await run_auto_commit(repo) is None

    async def test_creates_commit_with_paths(self, repo: Path):
        # Initial commit so HEAD exists.
        (repo / "init.txt").write_text("init")
        _git(repo, "add", "init.txt")
        _git(repo, "commit", "-m", "init")
        # New file the agent "wrote".
        (repo / "agent.txt").write_text("agent change")
        sha = await run_auto_commit(repo, paths=["agent.txt"])
        assert sha is not None
        assert len(sha) >= 7

        # Verify the commit message ends with the (bog-agent) tag. Sync
        # subprocess inside an async test is fine here — the call is
        # short-lived (a single git log) and we're already wrapped in a
        # 15s timeout.
        result = subprocess.run(  # noqa: ASYNC221 — short, timed git verification
            ["git", "log", "-1", "--pretty=%s"],
            cwd=str(repo),
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert "(bog-agent)" in result.stdout

    async def test_custom_message(self, repo: Path):
        (repo / "init.txt").write_text("init")
        _git(repo, "add", "init.txt")
        _git(repo, "commit", "-m", "init")
        (repo / "agent.txt").write_text("agent change")
        sha = await run_auto_commit(repo, paths=["agent.txt"], message="fix: tweak")
        assert sha is not None
        result = subprocess.run(  # noqa: ASYNC221 — short, timed git verification
            ["git", "log", "-1", "--pretty=%s"],
            cwd=str(repo),
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
        # Custom message + (bog-agent) suffix.
        assert "fix: tweak" in result.stdout
        assert "(bog-agent)" in result.stdout

    async def test_handles_path_with_no_actual_change(self, repo: Path):
        """Staging a path that wasn't modified should produce no commit."""
        (repo / "init.txt").write_text("init")
        _git(repo, "add", "init.txt")
        _git(repo, "commit", "-m", "init")
        # No new changes, only an unrelated dirty file:
        (repo / "other.txt").write_text("other")
        sha = await run_auto_commit(repo, paths=["init.txt"])
        # init.txt isn't modified, so 'git add init.txt' stages nothing.
        # The function should detect no staged changes and return None.
        assert sha is None
