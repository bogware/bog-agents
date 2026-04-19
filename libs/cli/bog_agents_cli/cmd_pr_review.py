"""PR review integration for GitHub and Azure DevOps.

Fetches PR metadata and diffs from GitHub (via the ``gh`` CLI) or Azure DevOps
(via the ``az`` CLI / REST API) and builds a structured prompt for the LLM
agent to review the pull request.
"""

from __future__ import annotations

import json
import logging
import subprocess  # noqa: S404
from pathlib import Path

logger = logging.getLogger(__name__)


def detect_pr_platform(cwd: Path) -> str | None:
    """Detect platform: 'github', 'azure', or None.

    Checks git remote URL for github.com or dev.azure.com/visualstudio.com.

    Args:
        cwd: Working directory from which to run git commands.

    Returns:
        'github', 'azure', or None when the platform cannot be determined.
    """
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            check=False,
            cwd=cwd,
        )
    except (FileNotFoundError, OSError):
        return None

    if result.returncode != 0:
        return None

    url = result.stdout.strip().lower()
    if "github.com" in url:
        return "github"
    if "dev.azure.com" in url or "visualstudio.com" in url:
        return "azure"
    return None


def get_github_pr_diff(pr_number: str | None = None, *, cwd: Path) -> dict[str, str]:
    """Fetch PR info and diff from GitHub using the gh CLI.

    Args:
        pr_number: PR number as string, or None to auto-detect from current branch.
        cwd: Working directory from which to run gh commands.

    Returns:
        Dict with keys: 'title', 'body', 'author', 'diff', 'files_changed', 'url'.

    Raises:
        RuntimeError: If the gh CLI is not found or not authenticated.
    """
    # Build base command args
    pr_arg: list[str] = [pr_number] if pr_number else []

    # Fetch PR metadata
    view_cmd = ["gh", "pr", "view", *pr_arg, "--json", "number,title,body,author,url"]
    try:
        view_result = subprocess.run(  # noqa: S603
            view_cmd,
            capture_output=True,
            text=True,
            check=False,
            cwd=cwd,
        )
    except FileNotFoundError:
        msg = "gh CLI not found. Install it from https://cli.github.com/ and run `gh auth login`."
        raise RuntimeError(msg) from None

    if view_result.returncode != 0:
        stderr = view_result.stderr.strip()
        if "not logged in" in stderr.lower() or "authentication" in stderr.lower():
            msg = f"gh CLI not authenticated. Run `gh auth login` to authenticate.\n{stderr}"
            raise RuntimeError(msg)
        msg = f"gh pr view failed: {stderr or view_result.stdout.strip()}"
        raise RuntimeError(msg)

    try:
        pr_meta = json.loads(view_result.stdout)
    except json.JSONDecodeError as exc:
        msg = f"Could not parse gh pr view output: {exc}"
        raise RuntimeError(msg) from exc

    author_login = pr_meta.get("author", {})
    if isinstance(author_login, dict):
        author_login = author_login.get("login", "")

    # Fetch the diff
    diff_cmd = ["gh", "pr", "diff", *pr_arg]
    try:
        diff_result = subprocess.run(  # noqa: S603
            diff_cmd,
            capture_output=True,
            text=True,
            check=False,
            cwd=cwd,
        )
    except FileNotFoundError:
        msg = "gh CLI not found. Install it from https://cli.github.com/."
        raise RuntimeError(msg) from None

    diff_text = diff_result.stdout if diff_result.returncode == 0 else ""

    # Count changed files from the diff header lines
    files_changed_set: set[str] = set()
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            parts = line.split(" ")
            if len(parts) >= 3:
                files_changed_set.add(parts[2].lstrip("a/"))
    files_changed = ", ".join(sorted(files_changed_set)) if files_changed_set else ""

    return {
        "title": pr_meta.get("title", ""),
        "body": pr_meta.get("body", "") or "",
        "author": author_login,
        "diff": diff_text,
        "files_changed": files_changed,
        "url": pr_meta.get("url", ""),
    }


def get_azure_pr_diff(pr_number: str | None = None, *, cwd: Path) -> dict[str, str]:
    """Fetch PR info and diff from Azure DevOps using az CLI or REST API.

    Tries ``az repos pr show`` and ``az repos pr diff``.
    Falls back to a git diff against the target branch when az CLI is unavailable.

    Args:
        pr_number: PR number as string, or None to try auto-detection.
        cwd: Working directory from which to run az/git commands.

    Returns:
        Dict with keys: 'title', 'body', 'author', 'diff', 'files_changed', 'url'.
    """
    pr_id_args: list[str] = ["--id", pr_number] if pr_number else []

    # Attempt az CLI
    az_available = False
    try:
        probe = subprocess.run(
            ["az", "--version"], capture_output=True, text=True, check=False
        )
        az_available = probe.returncode == 0
    except FileNotFoundError:
        pass

    if az_available:
        show_cmd = ["az", "repos", "pr", "show", *pr_id_args, "--output", "json"]
        show_result = subprocess.run(  # noqa: S603
            show_cmd, capture_output=True, text=True, check=False, cwd=cwd
        )
        if show_result.returncode == 0:
            try:
                pr_meta = json.loads(show_result.stdout)
            except json.JSONDecodeError:
                pr_meta = {}

            title = pr_meta.get("title", "")
            body = pr_meta.get("description", "") or ""
            author = pr_meta.get("createdBy", {}).get("uniqueName", "")
            url = pr_meta.get("url", "") or pr_meta.get("remoteUrl", "")
            target_branch = pr_meta.get("targetRefName", "refs/heads/main").replace(
                "refs/heads/", ""
            )

            # Try az repos pr diff
            diff_cmd = ["az", "repos", "pr", "diff", *pr_id_args, "--output", "json"]
            diff_result = subprocess.run(  # noqa: S603
                diff_cmd, capture_output=True, text=True, check=False, cwd=cwd
            )
            diff_text = ""
            files_changed = ""
            if diff_result.returncode == 0:
                diff_text = diff_result.stdout
            else:
                # Fallback: git diff against target branch
                git_diff = subprocess.run(  # noqa: S603
                    ["git", "diff", f"origin/{target_branch}...HEAD"],
                    capture_output=True,
                    text=True,
                    check=False,
                    cwd=cwd,
                )
                diff_text = git_diff.stdout if git_diff.returncode == 0 else ""

            files_changed_set: set[str] = set()
            for line in diff_text.splitlines():
                if line.startswith("diff --git "):
                    parts = line.split(" ")
                    if len(parts) >= 3:
                        files_changed_set.add(parts[2].lstrip("a/"))
            files_changed = ", ".join(sorted(files_changed_set))

            return {
                "title": title,
                "body": body,
                "author": author,
                "diff": diff_text,
                "files_changed": files_changed,
                "url": url,
            }

    # Final fallback: git diff against main/master
    logger.warning("az CLI unavailable or PR lookup failed; falling back to git diff")
    for base in ("origin/main", "origin/master"):
        git_diff = subprocess.run(  # noqa: S603
            ["git", "diff", f"{base}...HEAD"],
            capture_output=True,
            text=True,
            check=False,
            cwd=cwd,
        )
        if git_diff.returncode == 0 and git_diff.stdout.strip():
            diff_text = git_diff.stdout
            break
    else:
        diff_text = ""

    files_changed_set = set()
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            parts = line.split(" ")
            if len(parts) >= 3:
                files_changed_set.add(parts[2].lstrip("a/"))

    return {
        "title": f"PR {pr_number}" if pr_number else "Pull Request",
        "body": "",
        "author": "",
        "diff": diff_text,
        "files_changed": ", ".join(sorted(files_changed_set)),
        "url": "",
    }


def build_pr_review_prompt(pr_data: dict[str, str], *, focus: str = "all") -> str:
    """Build a structured review prompt for the LLM agent.

    Includes PR title, description, diff, and instructions to review for logic
    errors, security issues, performance concerns, test coverage, and code style.

    Args:
        pr_data: Dict with keys 'title', 'body', 'author', 'diff', 'files_changed', 'url'.
        focus: Review focus — 'security', 'performance', or 'all'.

    Returns:
        Structured prompt string ready to send to the LLM agent.
    """
    title = pr_data.get("title", "(no title)")
    body = pr_data.get("body", "").strip()
    author = pr_data.get("author", "unknown")
    diff = pr_data.get("diff", "")
    files_changed = pr_data.get("files_changed", "")
    url = pr_data.get("url", "")

    lines: list[str] = [
        "# Pull Request Review\n",
    ]

    if url:
        lines.append(f"**URL**: {url}")
    lines.append(f"**Title**: {title}")
    lines.append(f"**Author**: {author}")
    if files_changed:
        lines.append(f"**Files changed**: {files_changed}")
    lines.append("")

    if body:
        lines += ["## PR Description\n", body, ""]

    # Focus-specific instructions
    if focus == "security":
        lines += [
            "## Review Instructions\n",
            "Focus specifically on **security issues** in this PR:",
            "- Authentication and authorization flaws",
            "- Injection vulnerabilities (SQL, shell, path traversal)",
            "- Secrets or credentials accidentally committed",
            "- Insecure deserialization or cryptography",
            "- Unvalidated user input reaching sensitive sinks",
            "",
        ]
    elif focus == "performance":
        lines += [
            "## Review Instructions\n",
            "Focus specifically on **performance concerns** in this PR:",
            "- Unnecessary memory allocations or copies",
            "- N+1 query patterns or missing database indexes",
            "- Blocking calls in async contexts",
            "- Algorithmic complexity regressions",
            "- Missing caching opportunities",
            "",
        ]
    else:
        lines += [
            "## Review Instructions\n",
            "Please perform a thorough review of this PR covering all of the following:\n",
            "1. **Logic errors and bugs** — incorrect conditions, off-by-one errors, unhandled edge cases",
            "2. **Security issues** — injection flaws, auth bypasses, secrets in code, unvalidated input",
            "3. **Performance concerns** — unnecessary allocations, N+1 queries, blocking async calls",
            "4. **Test coverage** — missing tests for new logic, edge cases not covered",
            "5. **Code style / maintainability** — naming clarity, duplication, missing docstrings",
            "",
        ]

    lines += [
        "## Diff\n",
        "```diff",
        diff.strip() or "(no diff available)",
        "```",
        "",
        "## Output Format\n",
        "For each issue found provide:",
        "- **File and line** (if applicable)",
        "- **Severity**: critical / warning / suggestion",
        "- **Description**: what the issue is and why it matters",
        "- **Fix**: suggested fix or approach\n",
        "End with an overall summary and a quality score (0-100).",
    ]

    return "\n".join(lines)


def format_pr_review_not_found(platform: str | None) -> str:
    """Return helpful error when PR can't be found.

    Args:
        platform: Detected platform string ('github', 'azure', or None).

    Returns:
        Rich-formatted error message with remediation hints.
    """
    if platform == "github":
        return (
            "[red]Could not find a GitHub PR for the current branch.[/red]\n\n"
            "Make sure:\n"
            "  • You are on a branch that has an open PR\n"
            "  • The [cyan]gh[/cyan] CLI is installed and authenticated ([cyan]gh auth login[/cyan])\n"
            "  • Or provide a PR number: [cyan]/pr review 42[/cyan]"
        )
    if platform == "azure":
        return (
            "[red]Could not find an Azure DevOps PR for the current branch.[/red]\n\n"
            "Make sure:\n"
            "  • The [cyan]az[/cyan] CLI is installed ([cyan]az repos pr list[/cyan])\n"
            "  • You are authenticated ([cyan]az login[/cyan])\n"
            "  • Or provide a PR number: [cyan]/pr review 42[/cyan]"
        )
    return (
        "[red]No PR found.[/red] Could not detect a supported git platform (GitHub or Azure DevOps).\n\n"
        "Make sure the git remote URL points to github.com or dev.azure.com."
    )


def format_pr_help() -> str:
    """Return usage help for /pr command.

    Returns:
        Rich markup usage text.
    """
    return """\
[bold]PR review commands[/bold]

  [cyan]/pr review[/cyan]                    Review the PR for the current branch (auto-detect)
  [cyan]/pr review <number>[/cyan]           Review a specific PR by number
  [cyan]/pr review --focus security[/cyan]   Review with a security focus
  [cyan]/pr review --focus performance[/cyan] Review with a performance focus

[bold]Supported platforms[/bold]
  • GitHub — requires [dim]gh[/dim] CLI ([dim]gh auth login[/dim])
  • Azure DevOps — requires [dim]az[/dim] CLI ([dim]az login[/dim])

[bold]Platform detection[/bold]  Reads the [dim]origin[/dim] remote URL from git."""
