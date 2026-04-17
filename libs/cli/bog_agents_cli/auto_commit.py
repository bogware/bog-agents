"""Automatic git commit after each agent turn.

When enabled via ``--auto-commit``, the CLI creates a conventional commit
after each completed agent turn if there are any file changes. The commit
message is tagged with ``(bog-agent)`` and follows Conventional Commits format:

  chore(bog-agent): auto-commit agent changes (bog-agent)

Only creates commits when:
  1. The working directory is inside a git repository.
  2. At least one file has been modified, added, or deleted (staged or unstaged).
  3. git is available on PATH.

Unstaged changes are staged automatically (``git add -A``) before committing.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


async def _git_async(*args: str, cwd: Path) -> tuple[int, str]:
    """Run a git command and return (returncode, output).

    Args:
        *args: Git command arguments.
        cwd: Working directory.

    Returns:
        Tuple of (return code, combined stdout+stderr).
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        output = (stdout + stderr).decode(errors="replace").strip()
        return proc.returncode or 0, output
    except TimeoutError:
        return 1, "Error: git command timed out"
    except (OSError, FileNotFoundError) as exc:
        return 1, f"Error: {exc}"


async def _has_changes(cwd: Path) -> bool:
    """Return True when the working tree has any staged or unstaged changes.

    Args:
        cwd: Repository root directory.

    Returns:
        True if there are any changes to commit.
    """
    code, output = await _git_async("status", "--porcelain", cwd=cwd)
    return code == 0 and bool(output.strip())


async def _is_git_repo(cwd: Path) -> bool:
    """Return True when *cwd* is inside a git repository.

    Args:
        cwd: Directory to check.

    Returns:
        True if inside a git repo.
    """
    code, _ = await _git_async("rev-parse", "--git-dir", cwd=cwd)
    return code == 0


async def run_auto_commit(
    cwd: Path | None = None,
    *,
    message: str | None = None,
) -> str | None:
    """Stage all changes and create a conventional commit tagged '(bog-agent)'.

    A no-op when not in a git repo or when there are no changes to commit.
    Errors are logged at WARNING level and never raised.

    Args:
        cwd: Repository root; defaults to the current working directory.
        message: Override commit message (without the '(bog-agent)' tag,
            which is appended automatically).

    Returns:
        The commit SHA if a commit was created, else None.
    """
    repo_dir = cwd or Path.cwd()

    try:
        if not await _is_git_repo(repo_dir):
            return None

        if not await _has_changes(repo_dir):
            logger.debug("auto-commit: no changes to commit")
            return None

        # Stage all changes
        code, out = await _git_async("add", "-A", cwd=repo_dir)
        if code != 0:
            logger.warning("auto-commit: git add failed: %s", out)
            return None

        # Double-check there's actually something staged now
        code, staged = await _git_async("diff", "--cached", "--name-only", cwd=repo_dir)
        if code != 0 or not staged.strip():
            logger.debug("auto-commit: nothing staged after git add")
            return None

        commit_msg = message or "chore(bog-agent): auto-commit agent changes"
        full_msg = f"{commit_msg} (bog-agent)"

        code, out = await _git_async(
            "commit",
            "-m",
            full_msg,
            "--no-verify",  # skip hooks for auto-commits
            cwd=repo_dir,
        )
        if code != 0:
            logger.warning("auto-commit: git commit failed: %s", out)
            return None

        # Extract SHA from "master abc1234 message" output
        code, sha_out = await _git_async("rev-parse", "--short", "HEAD", cwd=repo_dir)
        sha = sha_out.strip() if code == 0 else None
        logger.info("auto-commit: created commit %s", sha)
        return sha

    except Exception:
        logger.warning("auto-commit: unexpected error", exc_info=True)
        return None
