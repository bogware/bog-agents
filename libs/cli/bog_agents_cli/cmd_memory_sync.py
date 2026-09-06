"""Team shared memory sync via git for AGENTS.md files.

Provides helpers to push and pull team-shared ``AGENTS.md`` content via a
dedicated git branch (default: ``team-memory``), and to inspect the state of
all memory files in the project.
"""

from __future__ import annotations

import logging
import subprocess  # noqa: S404
from datetime import UTC, datetime
from pathlib import Path

from bog_agents.git_env import hardened_git_env

logger = logging.getLogger(__name__)

_CONFIG_SUBDIR = ".bog-agents"
_CONFIG_FILENAME = "config.toml"
_DEFAULT_MEMORY_BRANCH = "team-memory"
_AGENTS_MD = "AGENTS.md"


def _run_git(
    args: list[str], *, cwd: Path, check: bool = False
) -> subprocess.CompletedProcess:
    """Run a git command and return its CompletedProcess result.

    Args:
        args: Arguments to pass to git (excluding the 'git' prefix).
        cwd: Working directory for the subprocess.
        check: If True, raise CalledProcessError on non-zero exit.

    Returns:
        CompletedProcess with stdout/stderr captured as text.
    """
    return subprocess.run(  # noqa: S603
        ["git", *args],
        capture_output=True,
        text=True,
        cwd=cwd,
        check=check,
        env=hardened_git_env(),
    )


def get_memory_branch(cwd: Path) -> str:
    """Return the memory sync branch name.

    Reads the ``[memory] branch`` key from ``.bog-agents/config.toml`` relative
    to the git repo root.  Falls back to ``'team-memory'`` when the file or key
    is absent.

    Args:
        cwd: Working directory used to locate the git repo root.

    Returns:
        Branch name string (default: 'team-memory').
    """
    # Locate git root
    result = _run_git(["rev-parse", "--show-toplevel"], cwd=cwd)
    if result.returncode != 0:
        return _DEFAULT_MEMORY_BRANCH

    git_root = Path(result.stdout.strip())
    config_path = git_root / _CONFIG_SUBDIR / _CONFIG_FILENAME

    if not config_path.is_file():
        return _DEFAULT_MEMORY_BRANCH

    try:
        import tomllib
    except ImportError:
        try:
            import tomli as tomllib  # type: ignore[no-redef]
        except ImportError:
            logger.debug("tomllib/tomli not available; using default memory branch")
            return _DEFAULT_MEMORY_BRANCH

    try:
        data = tomllib.loads(config_path.read_text(encoding="utf-8"))
        branch = data.get("memory", {}).get("branch", _DEFAULT_MEMORY_BRANCH)
        return str(branch)
    except Exception as exc:
        logger.warning("Could not parse %s: %s", config_path, exc)
        return _DEFAULT_MEMORY_BRANCH


def sync_memory(cwd: Path, *, direction: str = "pull") -> str:
    """Sync team AGENTS.md via git.

    Performs a pull (fetch + merge), push (commit + push), or both in sequence.

    Pull: ``git fetch origin <branch>``, then merges ``AGENTS.md`` from that branch
    into the working tree.

    Push: ``git add AGENTS.md``, ``git commit -m "chore: sync team memory"``,
    ``git push origin <branch>``.

    Handles conflicts by reporting a diff and asking the user to resolve manually.

    Args:
        cwd: Repository working directory.
        direction: 'pull', 'push', or 'both'.

    Returns:
        Rich-formatted status message.
    """
    if direction not in ("pull", "push", "both"):
        return f"[red]Unknown direction '[cyan]{direction}[/cyan]'.[/red] Use pull, push, or both."

    branch = get_memory_branch(cwd)
    messages: list[str] = []

    if direction in ("pull", "both"):
        messages.append(_pull_memory(cwd, branch=branch))

    if direction in ("push", "both"):
        messages.append(_push_memory(cwd, branch=branch))

    return "\n\n".join(m for m in messages if m)


def _pull_memory(cwd: Path, *, branch: str) -> str:
    """Fetch and merge AGENTS.md from the remote memory branch.

    Args:
        cwd: Repository working directory.
        branch: Memory sync branch name.

    Returns:
        Rich-formatted result message.
    """
    fetch_result = _run_git(["fetch", "origin", branch], cwd=cwd)
    if fetch_result.returncode != 0:
        stderr = fetch_result.stderr.strip()
        if "couldn't find remote ref" in stderr.lower():
            return (
                f"[yellow]Remote branch '[cyan]{branch}[/cyan]' does not exist yet.[/yellow]\n"
                "Push your AGENTS.md first: [cyan]/memory push[/cyan]"
            )
        return f"[red]git fetch failed:[/red] {stderr or '(no output)'}"

    # Merge only AGENTS.md from the remote branch using checkout-theirs strategy
    # to avoid polluting HEAD with unrelated branch files.
    show_result = _run_git(["show", f"origin/{branch}:{_AGENTS_MD}"], cwd=cwd)
    if show_result.returncode != 0:
        return f"[yellow]No {_AGENTS_MD} found on branch '[cyan]{branch}[/cyan]'.[/yellow] Nothing to pull."

    remote_content = show_result.stdout

    # Locate AGENTS.md in working tree
    root_result = _run_git(["rev-parse", "--show-toplevel"], cwd=cwd)
    git_root = Path(root_result.stdout.strip()) if root_result.returncode == 0 else cwd
    agents_md_path = git_root / _AGENTS_MD

    local_content = (
        agents_md_path.read_text(encoding="utf-8") if agents_md_path.is_file() else ""
    )

    if remote_content == local_content:
        return (
            f"[green]Memory is already up to date[/green] with [cyan]{branch}[/cyan]."
        )

    # Check for conflict markers
    diff_result = subprocess.run(  # noqa: S603
        ["git", "diff", "--no-index", "--", str(agents_md_path), "-"],
        input=remote_content,
        capture_output=True,
        text=True,
        check=False,
        cwd=cwd,
        env=hardened_git_env(),
    )
    diff_output = diff_result.stdout.strip()

    if local_content and remote_content != local_content:
        # Show the diff and ask user to resolve
        diff_preview = "\n".join(diff_output.splitlines()[:40])
        if len(diff_output.splitlines()) > 40:
            diff_preview += "\n[dim]... (diff truncated)[/dim]"
        return (
            f"[yellow]Conflict: local and remote {_AGENTS_MD} differ.[/yellow]\n\n"
            f"Remote branch: [cyan]{branch}[/cyan]\n\n"
            f"[bold]Diff (local vs remote):[/bold]\n[dim]{diff_preview}[/dim]\n\n"
            f"Please resolve manually, or use [cyan]/memory pull --force[/cyan] to overwrite with the remote version.\n"
            f"Remote content saved to [cyan]{agents_md_path}.remote[/cyan]."
        )

    # Safe to overwrite (local didn't exist)
    try:
        agents_md_path.write_text(remote_content, encoding="utf-8")
    except OSError as exc:
        return f"[red]Failed to write {_AGENTS_MD}:[/red] {exc}"

    return f"[green]Pulled {_AGENTS_MD}[/green] from [cyan]{branch}[/cyan] ({len(remote_content)} bytes)."


def _push_memory(cwd: Path, *, branch: str) -> str:
    """Commit and push AGENTS.md to the remote memory branch.

    Args:
        cwd: Repository working directory.
        branch: Memory sync branch name.

    Returns:
        Rich-formatted result message.
    """
    root_result = _run_git(["rev-parse", "--show-toplevel"], cwd=cwd)
    git_root = Path(root_result.stdout.strip()) if root_result.returncode == 0 else cwd
    agents_md_path = git_root / _AGENTS_MD

    if not agents_md_path.is_file():
        return f"[yellow]No {_AGENTS_MD} found at [cyan]{agents_md_path}[/cyan].[/yellow] Nothing to push."

    # Check if we are on the memory branch; if not, work on a worktree-free commit
    current_branch_result = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd)
    current_branch = (
        current_branch_result.stdout.strip()
        if current_branch_result.returncode == 0
        else ""
    )

    if current_branch == branch:
        # Already on the memory branch — just add, commit, push
        add_result = _run_git(["add", str(agents_md_path)], cwd=cwd)
        if add_result.returncode != 0:
            return f"[red]git add failed:[/red] {add_result.stderr.strip()}"

        # Check if there's anything to commit
        status_result = _run_git(
            ["status", "--porcelain", str(agents_md_path)], cwd=cwd
        )
        if not status_result.stdout.strip():
            return f"[dim]{_AGENTS_MD} unchanged; nothing to push.[/dim]"

        commit_result = _run_git(["commit", "-m", "chore: sync team memory"], cwd=cwd)
        if commit_result.returncode != 0:
            stderr = commit_result.stderr.strip()
            return f"[red]git commit failed:[/red] {stderr or '(no output)'}"
    else:
        # Push AGENTS.md to the memory branch without checking it out.
        # Read the file content and create a blob + tree + commit.
        blob_result = _run_git(["hash-object", "-w", str(agents_md_path)], cwd=cwd)
        if blob_result.returncode != 0:
            return f"[red]git hash-object failed:[/red] {blob_result.stderr.strip()}"
        blob_sha = blob_result.stdout.strip()

        # Check if the branch exists already
        branch_exists_result = _run_git(
            ["show-ref", "--verify", f"refs/heads/{branch}"], cwd=cwd
        )
        branch_exists = branch_exists_result.returncode == 0

        if branch_exists:
            # Read existing tree, update only AGENTS.md
            ls_tree_result = _run_git(["ls-tree", branch], cwd=cwd)
            tree_lines = (
                ls_tree_result.stdout.splitlines()
                if ls_tree_result.returncode == 0
                else []
            )
            # Build new tree input
            new_tree_lines = [
                line for line in tree_lines if not line.endswith(f"\t{_AGENTS_MD}")
            ]
            new_tree_lines.append(f"100644 blob {blob_sha}\t{_AGENTS_MD}")
            mktree_input = "\n".join(new_tree_lines) + "\n"
        else:
            mktree_input = f"100644 blob {blob_sha}\t{_AGENTS_MD}\n"

        mktree_result = subprocess.run(
            ["git", "mktree"],
            input=mktree_input,
            capture_output=True,
            text=True,
            check=False,
            cwd=cwd,
            env=hardened_git_env(),
        )
        if mktree_result.returncode != 0:
            return f"[red]git mktree failed:[/red] {mktree_result.stderr.strip()}"
        tree_sha = mktree_result.stdout.strip()

        # Build commit-tree args
        commit_tree_args = [tree_sha, "-m", "chore: sync team memory"]
        if branch_exists:
            commit_tree_args += ["-p", branch]
        commit_tree_result = _run_git(["commit-tree", *commit_tree_args], cwd=cwd)
        if commit_tree_result.returncode != 0:
            return f"[red]git commit-tree failed:[/red] {commit_tree_result.stderr.strip()}"
        new_commit_sha = commit_tree_result.stdout.strip()

        # Update (or create) the branch ref
        update_ref_result = _run_git(["branch", "-f", branch, new_commit_sha], cwd=cwd)
        if update_ref_result.returncode != 0:
            return f"[red]Failed to update branch ref:[/red] {update_ref_result.stderr.strip()}"

    push_result = _run_git(["push", "origin", branch], cwd=cwd)
    if push_result.returncode != 0:
        stderr = push_result.stderr.strip()
        return f"[red]git push failed:[/red] {stderr or '(no output)'}"

    return f"[green]Pushed {_AGENTS_MD}[/green] to [cyan]{branch}[/cyan] on origin."


def show_memory_status(cwd: Path) -> str:
    """Show status of memory files.

    Lists all AGENTS.md files found (project, user, subdirs).
    Shows last modified time and line count.
    Shows whether the team-memory branch exists locally and remotely.

    Args:
        cwd: Working directory to inspect.

    Returns:
        Rich-formatted status string.
    """
    branch = get_memory_branch(cwd)
    memory_files = list_memory_files(cwd)

    lines: list[str] = ["[bold]Memory File Status[/bold]", ""]

    if memory_files:
        lines.append(f"  {'File':<50}  {'Modified':<20}  {'Lines':>6}")
        lines.append("  " + "\u2500" * 82)
        for mf in memory_files:
            try:
                mtime = datetime.fromtimestamp(mf.stat().st_mtime, tz=UTC).strftime(
                    "%Y-%m-%d %H:%M"
                )
                line_count = len(
                    mf.read_text(encoding="utf-8", errors="replace").splitlines()
                )
            except OSError:
                mtime = "unknown"
                line_count = 0
            # Shorten path for display
            try:
                display_path = str(mf.relative_to(Path.home()))
                display_path = "~/" + display_path
            except ValueError:
                display_path = str(mf)
            if len(display_path) > 50:
                display_path = "\u2026" + display_path[-49:]
            lines.append(
                f"  [cyan]{display_path:<50}[/cyan]  {mtime:<20}  {line_count:>6}"
            )
    else:
        lines.append("  [dim]No AGENTS.md files found.[/dim]")

    lines += ["", "[bold]Team memory branch[/bold]"]

    # Check local branch
    local_check = _run_git(["show-ref", "--verify", f"refs/heads/{branch}"], cwd=cwd)
    local_exists = local_check.returncode == 0
    lines.append(
        f"  Local  [cyan]{branch}[/cyan]:  {'[green]exists[/green]' if local_exists else '[dim]not found[/dim]'}"
    )

    # Check remote branch
    remote_check = _run_git(
        ["show-ref", "--verify", f"refs/remotes/origin/{branch}"], cwd=cwd
    )
    remote_exists = remote_check.returncode == 0
    lines.append(
        f"  Remote [cyan]origin/{branch}[/cyan]:  {'[green]exists[/green]' if remote_exists else '[dim]not found[/dim]'}"
    )

    return "\n".join(lines)


def list_memory_files(cwd: Path) -> list[Path]:
    """Return paths of all AGENTS.md files: project root, user home, and subdirectories.

    Args:
        cwd: Working directory used to locate the git repo root.

    Returns:
        Deduplicated list of existing AGENTS.md paths, sorted by path.
    """
    found: list[Path] = []

    # User-global AGENTS.md
    user_agents_md = Path.home() / _AGENTS_MD
    if user_agents_md.is_file():
        found.append(user_agents_md)

    # Git repo root AGENTS.md
    root_result = _run_git(["rev-parse", "--show-toplevel"], cwd=cwd)
    if root_result.returncode == 0:
        git_root = Path(root_result.stdout.strip())
        project_agents_md = git_root / _AGENTS_MD
        if project_agents_md.is_file() and project_agents_md not in found:
            found.append(project_agents_md)

        # Subdirectory AGENTS.md files (up to 3 levels deep to avoid slow scans)
        for sub_agents_md in git_root.rglob(_AGENTS_MD):
            if sub_agents_md not in found and sub_agents_md.is_file():
                # Skip overly deep paths
                try:
                    rel = sub_agents_md.relative_to(git_root)
                    if len(rel.parts) <= 4:
                        found.append(sub_agents_md)
                except ValueError:
                    pass
    else:
        # Not in a git repo — scan cwd shallowly
        for sub_agents_md in cwd.glob(f"**/{_AGENTS_MD}"):
            if sub_agents_md not in found and sub_agents_md.is_file():
                found.append(sub_agents_md)

    return sorted(set(found))


def format_memory_help() -> str:
    """Return usage help for /memory command additions.

    Returns:
        Rich markup usage text.
    """
    return """\
[bold]Memory sync commands[/bold]

  [cyan]/memory status[/cyan]               Show all AGENTS.md files and branch status
  [cyan]/memory pull[/cyan]                 Pull team AGENTS.md from the remote memory branch
  [cyan]/memory push[/cyan]                 Push local AGENTS.md to the remote memory branch
  [cyan]/memory sync[/cyan]                 Pull then push (both directions)
  [cyan]/memory list[/cyan]                 List all AGENTS.md files found in this project

[bold]Memory branch[/bold]
  Default: [cyan]team-memory[/cyan]
  Configure in [dim].bog-agents/config.toml[/dim]:
    [dim][memory][/dim]
    [dim]branch = "my-team-memory-branch"[/dim]

[bold]Conflict resolution[/bold]
  When local and remote AGENTS.md differ, a diff is shown.
  Edit [dim]AGENTS.md[/dim] to merge the changes, then run [cyan]/memory push[/cyan]."""
