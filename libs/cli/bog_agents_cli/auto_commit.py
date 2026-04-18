"""Auto-commit helper: stage and commit agent-modified files.

This module provides utilities to commit files changed by an agent session,
using selective staging to avoid accidentally committing unrelated changes.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


def auto_commit(
    message: str,
    *,
    cwd: str | Path | None = None,
    paths: list[str] | None = None,
) -> None:
    """Stage and commit files modified during an agent session.

    Args:
        message: Commit message.
        cwd: Working directory for git commands. Defaults to `Path.cwd()`.
        paths: List of file paths to stage. When provided, only those specific
            paths are staged via `git add -- <paths>`. When `None`, falls back
            to `git add -A` with a logged warning (all changes staged).

    Raises:
        subprocess.CalledProcessError: If `git add` or `git commit` exits with a
            non-zero status (including pre-commit hook failures). The exception
            carries the original stderr output so callers can surface it.
    """
    work_dir = Path(cwd) if cwd is not None else Path.cwd()

    # --- Stage files ---
    if paths:
        add_cmd: list[str] = ["git", "add", "--"] + list(paths)
    else:
        logger.warning(
            "auto_commit called without an explicit paths list — falling back to "
            "`git add -A`. This may stage unintended changes. Pass paths= to "
            "restrict staging to agent-modified files."
        )
        add_cmd = ["git", "add", "-A"]

    _run(add_cmd, cwd=work_dir)

    # --- Commit (pre-commit hooks are allowed to run) ---
    commit_cmd = ["git", "commit", "-m", message]
    _run(commit_cmd, cwd=work_dir)


def _run(cmd: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run a subprocess command, raising on non-zero exit.

    Args:
        cmd: Command and arguments.
        cwd: Working directory.

    Returns:
        Completed process result.

    Raises:
        subprocess.CalledProcessError: On non-zero exit code. The `stderr`
            attribute of the exception contains the command's error output.
    """
    result = subprocess.run(
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
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
