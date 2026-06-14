"""CI-aware self-fix support (ROADMAP killer #1).

"Ship it while you sleep" starts with the agent being able to read its own PR's
CI result, ingest the failing job logs, and diagnose a fix — instead of the
human having to notice red CI and re-prompt. This module wraps the GitHub CLI
(`gh`) to fetch run status + failing logs for the current branch and builds a
structured fix prompt for the agent.

Pure parsing/prompt logic is separated from the `gh` subprocess calls so it's
testable without network. The `/ci-fix` command composes these; a fully
autonomous "push fix until green" daemon loop is the larger follow-up (#7).
"""

from __future__ import annotations

import json
import logging
import subprocess  # noqa: S404 — invoking the trusted `gh` CLI
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

_RUN_FIELDS = "databaseId,status,conclusion,workflowName,headBranch,event,createdAt,url"


@dataclass(frozen=True)
class CIRun:
    """A single GitHub Actions run."""

    run_id: int
    workflow: str
    status: str  # queued | in_progress | completed
    conclusion: str  # success | failure | cancelled | "" (while running)
    branch: str
    event: str
    created_at: str
    url: str

    @property
    def is_complete(self) -> bool:
        """True when the run has finished (regardless of conclusion)."""
        return self.status == "completed"

    @property
    def is_failure(self) -> bool:
        """True when the run finished unsuccessfully."""
        return self.conclusion in {"failure", "timed_out", "startup_failure"}

    @property
    def is_pending(self) -> bool:
        """True when the run is still queued or running."""
        return self.status in {"queued", "in_progress", "waiting", "requested", "pending"}


class GhUnavailableError(RuntimeError):
    """Raised when the `gh` CLI is not installed or not authenticated."""


def parse_run_list(json_str: str) -> list[CIRun]:
    """Parse the JSON from ``gh run list --json`` into :class:`CIRun`s (pure)."""
    try:
        data = json.loads(json_str)
    except json.JSONDecodeError:
        return []
    runs: list[CIRun] = []
    for item in data if isinstance(data, list) else []:
        runs.append(
            CIRun(
                run_id=int(item.get("databaseId", 0)),
                workflow=str(item.get("workflowName", "")),
                status=str(item.get("status", "")),
                conclusion=str(item.get("conclusion") or ""),
                branch=str(item.get("headBranch", "")),
                event=str(item.get("event", "")),
                created_at=str(item.get("createdAt", "")),
                url=str(item.get("url", "")),
            )
        )
    return runs


def latest_run(runs: list[CIRun]) -> CIRun | None:
    """Return the most recently created run (the list is newest-first from gh)."""
    return runs[0] if runs else None


def _run_gh(args: list[str], *, cwd: Path | None, timeout: float = 30.0) -> str:
    """Invoke `gh` and return stdout.

    Raises:
        GhUnavailableError: if `gh` is missing, unauthenticated, times out, or
            exits non-zero.
    """
    try:
        proc = subprocess.run(  # noqa: S603 — fixed `gh` argv, no shell
            ["gh", *args],
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise GhUnavailableError(
            "GitHub CLI (`gh`) is not installed. Install it from https://cli.github.com"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise GhUnavailableError(f"`gh {' '.join(args)}` timed out") from exc
    if proc.returncode != 0:
        err = (proc.stderr or "").strip()
        if "not logged" in err.lower() or "authentication" in err.lower():
            raise GhUnavailableError("`gh` is not authenticated — run `gh auth login`.")
        raise GhUnavailableError(err or f"`gh {' '.join(args)}` failed")
    return proc.stdout


def get_ci_status(branch: str, *, cwd: Path | None = None, limit: int = 10) -> list[CIRun]:
    """Fetch recent CI runs for ``branch`` via `gh run list`.

    Propagates :class:`GhUnavailableError` from `_run_gh` when `gh` is missing,
    unauthenticated, or errors.
    """
    out = _run_gh(
        ["run", "list", "--branch", branch, "--limit", str(limit), "--json", _RUN_FIELDS],
        cwd=cwd,
    )
    return parse_run_list(out)


def get_failing_logs(run_id: int, *, cwd: Path | None = None, max_chars: int = 12000) -> str:
    """Fetch the failed-step logs for a run, truncated to ``max_chars`` (head+tail)."""
    out = _run_gh(["run", "view", str(run_id), "--log-failed"], cwd=cwd, timeout=60.0)
    out = out.strip()
    if len(out) <= max_chars:
        return out
    head = out[: max_chars // 2]
    tail = out[-max_chars // 2 :]
    return f"{head}\n\n...[{len(out) - max_chars} chars truncated]...\n\n{tail}"


def generate_ci_fix_prompt(branch: str, run: CIRun, logs: str) -> str:
    """Build the agent prompt for diagnosing and fixing a failing CI run (pure)."""
    return "\n".join(
        [
            "# CI Self-Fix",
            "",
            f"The latest CI run for branch `{branch}` **failed** "
            f"(workflow `{run.workflow}`, {run.url}).",
            "",
            "## Failing job logs",
            "```",
            logs or "(no failed-step logs returned)",
            "```",
            "",
            "## Your task",
            "1. Diagnose the root cause of the failure from the logs above "
            "(distinguish a real code/test defect from a flaky/infra failure).",
            "2. If it's a real defect, fix it in the code and add/adjust a test "
            "that would have caught it.",
            "3. Run the relevant lint/type/test locally to confirm the fix.",
            "4. Summarize the root cause and the fix. Do NOT push — the user "
            "will review and push, then re-run `/ci-fix` to confirm green.",
            "If the failure is flaky/infra (not caused by this branch), say so "
            "clearly instead of changing code.",
        ]
    )
