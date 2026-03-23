"""PR-as-Output mode for non-interactive agent execution.

Run: ``bog-agents -n "fix issue #123" --pr``

Produces a ready-to-merge pull request with description, test plan,
and changelog entry. Designed for CI/CD and automation workflows.
"""

# Partial executable paths are intentional (git, gh, make, etc.)

from __future__ import annotations

import logging
import re
import subprocess  # noqa: S404
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

_MAX_TITLE_LEN = 70
_MAX_FILES_IN_BODY = 20


@dataclass
class PRConfig:
    """Configuration for PR generation."""

    base_branch: str = "main"
    branch_prefix: str = "bog-agents/"
    auto_push: bool = True
    draft: bool = False
    add_changelog: bool = True
    run_tests_before_pr: bool = True
    max_agent_turns: int = 100
    reviewers: list[str] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)


@dataclass
class PRResult:
    """Result of a PR generation run."""

    success: bool
    branch_name: str = ""
    pr_url: str = ""
    pr_number: int | None = None
    title: str = ""
    body: str = ""
    files_changed: list[str] = field(default_factory=list)
    commits: list[str] = field(default_factory=list)
    test_results: str | None = None
    error: str | None = None
    duration_seconds: float = 0.0


def _run_git(args: list[str], *, cwd: str | None = None) -> tuple[bool, str]:
    """Run a git command.

    Args:
        args: Git command arguments.
        cwd: Working directory.

    Returns:
        Tuple of (success, output).
    """
    try:
        result = subprocess.run(  # noqa: S603
            ["git", *args],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            cwd=cwd,
        )
        return result.returncode == 0, result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)


def _run_gh(args: list[str], *, cwd: str | None = None) -> tuple[bool, str]:
    """Run a GitHub CLI command.

    Args:
        args: gh command arguments.
        cwd: Working directory.

    Returns:
        Tuple of (success, output).
    """
    try:
        result = subprocess.run(  # noqa: S603
            ["gh", *args],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
            cwd=cwd,
        )
        return result.returncode == 0, result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)


def create_branch(
    task_description: str,
    config: PRConfig,
    *,
    cwd: str | None = None,
) -> str:
    """Create a new feature branch for the PR.

    Args:
        task_description: Task being performed (used in branch name).
        config: PR configuration.
        cwd: Working directory.

    Returns:
        Branch name.

    Raises:
        RuntimeError: If branch creation fails.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", task_description.lower())[:40].strip("-")
    timestamp = str(int(time.time()))[-6:]
    branch_name = f"{config.branch_prefix}{slug}-{timestamp}"

    _run_git(["checkout", config.base_branch], cwd=cwd)
    _run_git(["pull", "origin", config.base_branch], cwd=cwd)

    success, _ = _run_git(["checkout", "-b", branch_name], cwd=cwd)
    if not success:
        msg = f"Failed to create branch: {branch_name}"
        raise RuntimeError(msg)

    logger.info("Created branch: %s", branch_name)
    return branch_name


def get_changed_files(base_branch: str, *, cwd: str | None = None) -> list[str]:
    """Get list of files changed relative to base branch.

    Args:
        base_branch: Base branch to diff against.
        cwd: Working directory.

    Returns:
        List of changed file paths.
    """
    success, output = _run_git(
        ["diff", "--name-only", f"{base_branch}...HEAD"], cwd=cwd
    )
    if not success:
        _, output = _run_git(["diff", "--name-only", "HEAD"], cwd=cwd)
    return [f for f in output.split("\n") if f.strip()]


def generate_pr_body(
    task_description: str,
    files_changed: list[str],
    commits: list[str],
    *,
    test_results: str | None = None,
) -> str:
    """Generate a PR body with summary and test plan.

    Args:
        task_description: Original task description.
        files_changed: List of changed files.
        commits: Commit messages.
        test_results: Optional test results.

    Returns:
        Formatted PR body in markdown.
    """
    lines: list[str] = [
        "## Summary",
        "",
        f"Automated changes from bog-agents: {task_description}",
        "",
    ]

    if commits:
        lines.extend(("## Changes", ""))
        lines.extend(f"- {commit}" for commit in commits)
        lines.append("")

    if files_changed:
        lines.extend(("## Files Changed", ""))
        lines.extend(f"- `{f}`" for f in files_changed[:_MAX_FILES_IN_BODY])
        if len(files_changed) > _MAX_FILES_IN_BODY:
            extra = len(files_changed) - _MAX_FILES_IN_BODY
            lines.append(f"- ... and {extra} more")
        lines.append("")

    lines.extend(("## Test Plan", ""))
    if test_results:
        lines.extend(("```", test_results[:2000], "```"))
    else:
        lines.extend(
            (
                "- [ ] Review changes",
                "- [ ] Run test suite",
                "- [ ] Verify no regressions",
            )
        )

    lines.extend(
        (
            "",
            "---",
            ("*Generated by [bog-agents](https://github.com/bogware/bog-agents)*"),
        )
    )

    return "\n".join(lines)


def generate_pr_title(task_description: str) -> str:
    """Generate a concise PR title from the task description.

    Args:
        task_description: Full task description.

    Returns:
        PR title (max 70 chars).
    """
    title = task_description.strip()

    for prefix in ("fix ", "implement ", "add ", "update ", "resolve "):
        if title.lower().startswith(prefix):
            title = prefix.capitalize() + title[len(prefix) :]
            break

    if len(title) > _MAX_TITLE_LEN:
        title = title[: _MAX_TITLE_LEN - 3] + "..."

    return title


def run_tests(*, cwd: str | None = None) -> tuple[bool, str]:
    """Run the project's test suite.

    Tries common test commands in order until one works.

    Args:
        cwd: Working directory.

    Returns:
        Tuple of (success, output).
    """
    test_commands = [
        ["make", "test"],
        ["pytest", "--tb=short", "-q"],
        ["npm", "test", "--", "--ci"],
        ["cargo", "test"],
        ["go", "test", "./..."],
    ]

    for cmd in test_commands:
        try:
            result = subprocess.run(  # noqa: S603
                cmd,
                capture_output=True,
                text=True,
                timeout=300,
                check=False,
                cwd=cwd,
            )
            output = result.stdout + result.stderr
            if result.returncode == 0:
                return True, output[-2000:]
            if (
                "command not found" not in output.lower()
                and "not recognized" not in output.lower()
            ):
                return False, output[-2000:]
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue

    return True, "No test runner found"


def commit_and_push(
    message: str,
    branch_name: str,
    *,
    cwd: str | None = None,
) -> tuple[bool, list[str]]:
    """Stage all changes, commit, and push.

    Args:
        message: Commit message.
        branch_name: Branch to push to.
        cwd: Working directory.

    Returns:
        Tuple of (success, list of commit messages).
    """
    _run_git(["add", "-A"], cwd=cwd)

    _, status = _run_git(["status", "--porcelain"], cwd=cwd)
    if not status.strip():
        return True, []

    success, _ = _run_git(["commit", "-m", message], cwd=cwd)
    if not success:
        return False, []

    _, log_output = _run_git(
        ["log", "--oneline", f"origin/{branch_name}..HEAD"], cwd=cwd
    )
    commits = [line.strip() for line in log_output.split("\n") if line.strip()]

    success, _ = _run_git(["push", "-u", "origin", branch_name], cwd=cwd)
    return success, commits


def create_pull_request(
    title: str,
    body: str,
    branch_name: str,
    config: PRConfig,
    *,
    cwd: str | None = None,
) -> tuple[bool, str]:
    """Create a pull request using GitHub CLI.

    Args:
        title: PR title.
        body: PR body markdown.
        branch_name: Source branch.
        config: PR configuration.
        cwd: Working directory.

    Returns:
        Tuple of (success, pr_url_or_error).
    """
    args = [
        "pr",
        "create",
        "--title",
        title,
        "--body",
        body,
        "--base",
        config.base_branch,
        "--head",
        branch_name,
    ]

    if config.draft:
        args.append("--draft")

    for reviewer in config.reviewers:
        args.extend(["--reviewer", reviewer])

    for label in config.labels:
        args.extend(["--label", label])

    return _run_gh(args, cwd=cwd)


async def run_pr_mode(
    task_description: str,
    agent: Any,  # noqa: ANN401
    *,
    config: PRConfig | None = None,
    cwd: str | None = None,
) -> PRResult:
    """Run the agent in PR-output mode.

    1. Create a feature branch
    2. Run the agent with the task
    3. Run tests
    4. Commit and push
    5. Create a PR

    Args:
        task_description: What the agent should do.
        agent: Compiled LangGraph agent.
        config: PR configuration.
        cwd: Working directory.

    Returns:
        ``PRResult`` with the outcome.
    """
    start = time.time()
    cfg = config or PRConfig()
    result = PRResult(success=False)

    try:
        branch_name = create_branch(task_description, cfg, cwd=cwd)
        result.branch_name = branch_name

        logger.info("Running agent: %s", task_description)
        agent_config = {"configurable": {"thread_id": f"pr-{branch_name}"}}
        await agent.ainvoke(
            {"messages": [{"role": "user", "content": task_description}]},
            config=agent_config,
        )

        if cfg.run_tests_before_pr:
            test_success, test_output = run_tests(cwd=cwd)
            result.test_results = test_output
            if not test_success:
                logger.warning("Tests failed, continuing with PR creation")

        result.files_changed = get_changed_files(cfg.base_branch, cwd=cwd)

        if not result.files_changed:
            result.error = "No files changed by the agent"
            result.duration_seconds = time.time() - start
            return result

        commit_msg = f"feat: {task_description[:50]}\n\nAutomated by bog-agents"
        push_success, commits = commit_and_push(commit_msg, branch_name, cwd=cwd)
        result.commits = commits

        if not push_success:
            result.error = "Failed to push changes"
            result.duration_seconds = time.time() - start
            return result

        result.title = generate_pr_title(task_description)
        result.body = generate_pr_body(
            task_description,
            result.files_changed,
            result.commits,
            test_results=result.test_results,
        )

        pr_success, pr_output = create_pull_request(
            result.title,
            result.body,
            branch_name,
            cfg,
            cwd=cwd,
        )

        if pr_success:
            result.pr_url = pr_output
            result.success = True
            logger.info("PR created: %s", pr_output)
        else:
            result.error = f"Failed to create PR: {pr_output}"

    except Exception:
        logger.exception("PR mode failed")
        result.error = "PR mode encountered an error"

    result.duration_seconds = time.time() - start
    return result
