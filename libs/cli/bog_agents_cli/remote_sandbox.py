"""SSH-backed remote sandbox helpers for the CLI remote provider."""

from __future__ import annotations

import asyncio
import base64
import subprocess  # noqa: S404 - required for local git discovery
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


@dataclass(frozen=True)
class LocalGitContext:
    """Local repository context used to seed a remote sandbox task."""

    repo_root: Path
    repo_name: str
    repo_url: str
    branch: str


def asset_text(name: str) -> str:
    """Load a bundled remote sandbox helper script."""
    asset_path = Path(__file__).with_name("remote_assets") / name
    return asset_path.read_text(encoding="utf-8")


def repo_name_from_url(repo_url: str) -> str:
    """Derive a repository name from a git URL."""
    name = repo_url.rstrip("/").split("/")[-1]
    name = name.removesuffix(".git")
    return name or "repo"


def slugify(value: str) -> str:
    """Convert arbitrary text into a branch-safe slug."""
    chars: list[str] = []
    previous_dash = False
    for char in value.lower():
        if char.isalnum():
            chars.append(char)
            previous_dash = False
            continue
        if not previous_dash:
            chars.append("-")
            previous_dash = True
    slug = "".join(chars).strip("-")
    return slug or "task"


def build_ssh_target(*, host: str, user: str) -> str:
    """Build the SSH target string from host/user config."""
    clean_host = host.strip()
    if not clean_host:
        return ""
    if "@" in clean_host or not user.strip():
        return clean_host
    return f"{user.strip()}@{clean_host}"


def ssh_base_args(
    *, port: int, identity_file: str, ssh_options: list[str]
) -> list[str]:
    """Build stable SSH CLI arguments."""
    args: list[str] = []
    if port:
        args.extend(["-p", str(port)])
    if identity_file:
        args.extend(["-i", identity_file])
    for option in ssh_options:
        args.extend(["-o", option])
    return args


def run_local_git(working_dir: Path, *args: str) -> str | None:
    """Run a local git command for repo discovery."""
    try:
        result = subprocess.run(  # noqa: S603 - git executable and args are controlled here
            ["git", *args],
            cwd=working_dir,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def detect_local_git_context(
    working_dir: Path | None,
    *,
    repo_url_override: str = "",
    base_branch_override: str = "",
) -> LocalGitContext | None:
    """Detect repo root, origin URL, and branch from a local working directory."""
    if working_dir is None:
        return None
    start_dir = working_dir if working_dir.is_dir() else working_dir.parent
    repo_root_raw = run_local_git(start_dir, "rev-parse", "--show-toplevel")
    if repo_root_raw is None:
        return None
    repo_root = Path(repo_root_raw)
    repo_url = repo_url_override or (
        run_local_git(repo_root, "remote", "get-url", "origin") or ""
    )
    if not repo_url:
        return None
    branch = base_branch_override or (
        run_local_git(repo_root, "rev-parse", "--abbrev-ref", "HEAD") or "main"
    )
    return LocalGitContext(
        repo_root=repo_root,
        repo_name=repo_root.name or repo_name_from_url(repo_url),
        repo_url=repo_url,
        branch=branch,
    )


async def run_remote_python(
    *,
    host: str,
    user: str,
    port: int,
    identity_file: str,
    ssh_options: list[str],
    python_command: str,
    script_name: str,
    args: list[str],
) -> tuple[int, str, str]:
    """Run a bundled Python helper script on the remote SSH target."""
    target = build_ssh_target(host=host, user=user)
    if not target:
        return 1, "", "SSH remote host is not configured."
    try:
        process = await asyncio.create_subprocess_exec(
            "ssh",
            *ssh_base_args(
                port=port,
                identity_file=identity_file,
                ssh_options=ssh_options,
            ),
            target,
            python_command,
            "-",
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        return 1, "", f"Failed to start ssh client: {exc}"
    stdout_bytes, stderr_bytes = await process.communicate(
        asset_text(script_name).encode("utf-8")
    )
    return (
        process.returncode or 0,
        stdout_bytes.decode("utf-8", errors="replace").strip(),
        stderr_bytes.decode("utf-8", errors="replace").strip(),
    )


def encode_script_arg(value: str) -> str:
    """Encode arbitrary text as a compact script argument."""
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


def build_task_payload(
    *,
    host: str,
    user: str,
    remote_root: str,
    repo_url: str,
    base_branch: str,
    bog_agents_command: str,
    shell_allow_list: str,
    git_user_name: str,
    git_user_email: str,
    publish_branch: bool,
    no_mcp: bool,
    prompt: str,
    task_id: str,
    model: str,
    label: str,
    assistant_id: str,
    branch_prefix: str,
    working_dir: Path | None,
    base_branch_override: str | None,
) -> dict[str, object] | str:
    """Build the payload sent to the remote sandbox bootstrap script."""
    git_context = detect_local_git_context(
        working_dir,
        repo_url_override=repo_url,
        base_branch_override=base_branch_override or base_branch,
    )
    resolved_repo_url = repo_url
    repo_name = repo_name_from_url(repo_url) if repo_url else ""
    resolved_base_branch = base_branch_override or base_branch or "main"
    if git_context is not None:
        resolved_repo_url = repo_url or git_context.repo_url
        repo_name = git_context.repo_name
        resolved_base_branch = base_branch_override or base_branch or git_context.branch
    if not resolved_repo_url:
        return (
            "SSH remote sandbox requires a git origin URL. "
            "Set `repo_url` in ~/.bog-agents/remote.json or run from a git repo "
            "with `origin` configured."
        )

    branch_seed = branch_prefix or label or prompt
    sandbox_branch = f"sandbox/{slugify(branch_seed)}-{task_id}"
    posix_root = PurePosixPath(remote_root)
    task_dir = str(posix_root / "tasks" / task_id)
    workspace_dir = str(posix_root / "workspaces" / repo_name / task_id)

    return {
        "task_id": task_id,
        "prompt": prompt,
        "label": label,
        "model": model,
        "assistant_id": assistant_id,
        "task_dir": task_dir,
        "workspace_dir": workspace_dir,
        "status_file": str(PurePosixPath(task_dir) / "status.json"),
        "stdout_file": str(PurePosixPath(task_dir) / "stdout.log"),
        "stderr_file": str(PurePosixPath(task_dir) / "stderr.log"),
        "prompt_file": str(PurePosixPath(task_dir) / "prompt.txt"),
        "repo_url": resolved_repo_url,
        "repo_name": repo_name or repo_name_from_url(resolved_repo_url),
        "base_branch": resolved_base_branch or "main",
        "sandbox_branch": sandbox_branch,
        "bog_agents_command": bog_agents_command,
        "shell_allow_list": shell_allow_list,
        "git_user_name": git_user_name,
        "git_user_email": git_user_email,
        "publish_branch": publish_branch,
        "no_mcp": no_mcp,
        "ssh_target": build_ssh_target(host=host, user=user),
    }
