"""Automatic git commit after each agent turn.

When enabled via ``--auto-commit``, the CLI creates a conventional commit
after each completed agent turn if there are any file changes. The commit
message is tagged with ``(bog-agent)`` and follows Conventional Commits format:

  chore(bog-agent): auto-commit agent changes (bog-agent)

Only creates commits when:
  1. The working directory is inside a git repository.
  2. At least one file has been modified, added, or deleted (staged or unstaged).
  3. git is available on PATH.

When ``paths`` is supplied, only those specific files are staged. Otherwise
falls back to ``git add -A`` with a logged warning. Pre-commit hooks are
allowed to run (no ``--no-verify``).
"""

from __future__ import annotations

import asyncio
import logging
import subprocess  # noqa: S404
from pathlib import Path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Async helpers (used by the Textual TUI)
# ---------------------------------------------------------------------------


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
    """Return True when the working tree has any staged or unstaged changes."""
    code, output = await _git_async("status", "--porcelain", cwd=cwd)
    return code == 0 and bool(output.strip())


async def _is_git_repo(cwd: Path) -> bool:
    """Return True when *cwd* is inside a git repository."""
    code, _ = await _git_async("rev-parse", "--git-dir", cwd=cwd)
    return code == 0


async def run_auto_commit(
    cwd: Path | None = None,
    *,
    message: str | None = None,
    paths: list[str] | None = None,
) -> str | None:
    """Stage changes and create a conventional commit tagged '(bog-agent)'.

    A no-op when not in a git repo or when there are no changes to commit.
    Pre-commit hooks are allowed to run. Errors are logged at WARNING level
    and never raised.

    Args:
        cwd: Repository root; defaults to the current working directory.
        message: Override commit message (without the '(bog-agent)' tag,
            which is appended automatically).
        paths: Specific file paths to stage. When provided, only those files
            are staged. When None, falls back to ``git add -A`` with a warning.

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

        # Stage files — selective when paths provided, full fallback otherwise
        if paths:
            code, out = await _git_async("add", "--", *paths, cwd=repo_dir)
        else:
            logger.warning(
                "auto-commit: no paths specified — staging all changes via `git add -A`. "
                "Pass paths= to restrict staging to agent-modified files."
            )
            code, out = await _git_async("add", "-A", cwd=repo_dir)

        if code != 0:
            logger.warning("auto-commit: git add failed: %s", out)
            return None

        # Verify something is actually staged
        code, staged = await _git_async("diff", "--cached", "--name-only", cwd=repo_dir)
        if code != 0 or not staged.strip():
            logger.debug("auto-commit: nothing staged after git add")
            return None

        commit_msg = message or "chore(bog-agent): auto-commit agent changes"
        full_msg = f"{commit_msg} (bog-agent)"

        # Pre-commit hooks run (no --no-verify). On hook failure, surface the error.
        code, out = await _git_async("commit", "-m", full_msg, cwd=repo_dir)
        if code != 0:
            logger.warning(
                "auto-commit: git commit failed (hooks may have blocked it): %s", out
            )
            return None

        code, sha_out = await _git_async("rev-parse", "--short", "HEAD", cwd=repo_dir)
        sha = sha_out.strip() if code == 0 else None
        logger.info("auto-commit: created commit %s", sha)
        return sha

    except Exception:
        logger.warning("auto-commit: unexpected error", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Sync helper (for scripting / non-async contexts)
# ---------------------------------------------------------------------------


def auto_commit(
    message: str,
    *,
    cwd: str | Path | None = None,
    paths: list[str] | None = None,
) -> None:
    """Stage and commit files (synchronous version for scripting contexts).

    Args:
        message: Commit message.
        cwd: Working directory for git commands. Defaults to `Path.cwd()`.
        paths: File paths to stage. When provided, only those specific paths
            are staged. When None, falls back to ``git add -A`` with a logged
            warning.

    Raises:
        subprocess.CalledProcessError: If ``git add`` or ``git commit`` exits
            with a non-zero status (including pre-commit hook failures).
    """  # noqa: DOC502
    work_dir = Path(cwd) if cwd is not None else Path.cwd()

    if paths:
        add_cmd: list[str] = ["git", "add", "--", *list(paths)]
    else:
        logger.warning(
            "auto_commit called without an explicit paths list — falling back to "
            "`git add -A`. Pass paths= to restrict staging to agent-modified files."
        )
        add_cmd = ["git", "add", "-A"]

    _run(add_cmd, cwd=work_dir)
    _run(["git", "commit", "-m", message], cwd=work_dir)


def _run(cmd: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run a subprocess command, raising CalledProcessError on non-zero exit.

    Raises:
        subprocess.CalledProcessError: If the command exits with non-zero status.
    """
    # See ``_constants.GIT_WRITE_TIMEOUT_S`` for the rationale on the
    # 30s budget — long enough to forgive a slow disk + pre-commit hook,
    # short enough that a hung lockfile fails loud rather than hanging
    # the CLI.
    from bog_agents_cli._constants import GIT_WRITE_TIMEOUT_S

    result = subprocess.run(  # noqa: S603
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
        timeout=GIT_WRITE_TIMEOUT_S,
    )
    if result.returncode != 0:
        error_output = result.stderr.strip() or result.stdout.strip()
        logger.error(
            "Command %s failed (exit %d):\n%s",
            " ".join(cmd),
            result.returncode,
            error_output,
        )
        raise subprocess.CalledProcessError(
            result.returncode,
            cmd,
            output=result.stdout,
            stderr=result.stderr,
        )
    return result
