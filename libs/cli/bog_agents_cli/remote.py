"""Remote/cloud execution mode for running agents on remote infrastructure.

Feature #21: Remote/cloud execution — push agent tasks to cloud instances.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import cast

logger = logging.getLogger(__name__)
_REMOTE_TASKS_FILE = "remote-tasks.json"


def _coerce_string_list(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


class RemoteStatus(StrEnum):
    """Status of a remote agent task."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class RemoteTask:
    """A task running on remote infrastructure."""

    task_id: str
    """Unique task identifier."""

    prompt: str
    """The task prompt/instructions."""

    label: str = ""
    """Optional human-readable task label."""

    status: RemoteStatus = RemoteStatus.PENDING
    """Current task status."""

    model: str = ""
    """Model being used."""

    working_dir: str = ""
    """Workspace associated with the task, when known."""

    output: str = ""
    """Task output when completed."""

    files_changed: list[str] = field(default_factory=list)
    """Files modified by the remote agent."""

    error: str = ""
    """Error message if failed."""

    metadata: dict[str, object] = field(default_factory=dict)
    """Additional metadata."""


def _remote_tasks_path(config_dir: Path) -> Path:
    """Return the persisted remote task registry path."""
    return config_dir / _REMOTE_TASKS_FILE


def remote_task_to_dict(task: RemoteTask) -> dict[str, object]:
    """Serialize a remote task for persistence."""
    return {
        "task_id": task.task_id,
        "prompt": task.prompt,
        "label": task.label,
        "status": str(task.status),
        "model": task.model,
        "working_dir": task.working_dir,
        "output": task.output,
        "files_changed": list(task.files_changed),
        "error": task.error,
        "metadata": dict(task.metadata),
    }


def remote_task_from_dict(data: object) -> RemoteTask | None:
    """Deserialize a persisted remote task record."""
    if not isinstance(data, dict):
        return None
    item = cast("dict[str, object]", data)
    metadata = item.get("metadata")
    return RemoteTask(
        task_id=str(item.get("task_id", "")).strip(),
        prompt=str(item.get("prompt", "")),
        label=str(item.get("label", "")),
        status=_coerce_remote_status(item.get("status")),
        model=str(item.get("model", "")),
        working_dir=str(item.get("working_dir", "")),
        output=str(item.get("output", "")),
        files_changed=_coerce_string_list(item.get("files_changed", [])),
        error=str(item.get("error", "")),
        metadata=cast("dict[str, object]", metadata).copy()
        if isinstance(metadata, dict)
        else {},
    )


def load_remote_tasks(config_dir: Path) -> list[RemoteTask]:
    """Load persisted remote tasks from disk."""
    tasks_file = _remote_tasks_path(config_dir)
    if not tasks_file.exists():
        return []
    try:
        payload = json.loads(tasks_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, TypeError, ValueError) as exc:
        logger.warning("Failed to load remote task registry: %s", exc)
        return []
    if not isinstance(payload, list):
        return []
    tasks: list[RemoteTask] = []
    for item in payload:
        task = remote_task_from_dict(item)
        if task is None or not task.task_id:
            continue
        tasks.append(task)
    return tasks


def save_remote_tasks(config_dir: Path, tasks: list[RemoteTask]) -> None:
    """Persist remote task state for restart-safe recovery."""
    tasks_file = _remote_tasks_path(config_dir)
    tasks_file.parent.mkdir(parents=True, exist_ok=True)
    serialized = [remote_task_to_dict(task) for task in tasks]
    tasks_file.write_text(json.dumps(serialized, indent=2), encoding="utf-8")


def find_remote_task(config_dir: Path, task_id: str) -> RemoteTask | None:
    """Find one persisted remote task by ID."""
    for task in load_remote_tasks(config_dir):
        if task.task_id == task_id:
            return task
    return None


@dataclass
class RemoteConfig:
    """Configuration for remote execution."""

    provider: str = "langgraph-cloud"
    """Remote execution provider."""

    api_url: str = ""
    """API endpoint URL."""

    api_key: str = ""
    """API authentication key."""

    workspace_sync: bool = True
    """Whether to sync local workspace to remote."""

    auto_apply: bool = False
    """Whether to auto-apply remote changes locally."""

    host: str = ""
    """SSH host or `user@host` target for the sandbox provider."""

    user: str = ""
    """Optional SSH username when `host` does not already include one."""

    port: int = 22
    """SSH port for the sandbox provider."""

    identity_file: str = ""
    """Optional SSH identity file path."""

    ssh_options: list[str] = field(default_factory=list)
    """Additional `ssh -o ...` options."""

    python_command: str = "python3"
    """Remote Python executable used to bootstrap sandbox tasks."""

    remote_root: str = "~/bog-agents-remote"
    """Base directory on the remote host for sandbox task state."""

    repo_url: str = ""
    """Optional git URL override used instead of the local `origin` URL."""

    base_branch: str = ""
    """Optional default branch to branch from on the remote sandbox."""

    workspace_mode: str = "clone"
    """Remote workspace strategy. Currently `clone` is supported."""

    bog_agents_command: str = "bog-agents"
    """Command used to invoke Bog Agents on the remote sandbox host."""

    shell_allow_list: str = "all"
    """Shell approval mode used for the remote headless CLI run."""

    git_user_name: str = ""
    """Optional git author name configured in each sandbox workspace."""

    git_user_email: str = ""
    """Optional git author email configured in each sandbox workspace."""

    publish_branch: bool = False
    """Whether to publish the sandbox branch to `origin` before agent work."""

    no_mcp: bool = True
    """Whether remote headless runs should disable MCP loading."""


def load_remote_config(config_dir: Path) -> RemoteConfig:
    """Load remote execution configuration.

    Args:
        config_dir: Config directory path.

    Returns:
        RemoteConfig instance.
    """
    config_file = config_dir / "remote.json"
    if not config_file.exists():
        return RemoteConfig()

    try:
        data = json.loads(config_file.read_text(encoding="utf-8"))
        return RemoteConfig(
            provider=str(data.get("provider", "langgraph-cloud")),
            api_url=str(data.get("api_url", "")),
            api_key=str(data.get("api_key", "")),
            workspace_sync=bool(data.get("workspace_sync", True)),
            auto_apply=bool(data.get("auto_apply", False)),
            host=str(data.get("host", "")),
            user=str(data.get("user", "")),
            port=int(data.get("port", 22) or 22),
            identity_file=str(data.get("identity_file", "")),
            ssh_options=_coerce_string_list(data.get("ssh_options", [])),
            python_command=str(data.get("python_command", "python3")),
            remote_root=str(data.get("remote_root", "~/bog-agents-remote")),
            repo_url=str(data.get("repo_url", "")),
            base_branch=str(data.get("base_branch", "")),
            workspace_mode=str(data.get("workspace_mode", "clone")),
            bog_agents_command=str(data.get("bog_agents_command", "bog-agents")),
            shell_allow_list=str(data.get("shell_allow_list", "all")),
            git_user_name=str(data.get("git_user_name", "")),
            git_user_email=str(data.get("git_user_email", "")),
            publish_branch=bool(data.get("publish_branch", False)),
            no_mcp=bool(data.get("no_mcp", True)),
        )
    except (json.JSONDecodeError, OSError, TypeError, ValueError) as exc:
        logger.warning("Failed to load remote config: %s", exc)
        return RemoteConfig()


def _coerce_remote_status(value: object) -> RemoteStatus:
    if isinstance(value, str):
        try:
            return RemoteStatus(value)
        except ValueError:
            return RemoteStatus.PENDING
    return RemoteStatus.PENDING


def _update_task_from_ssh_state(
    task: RemoteTask, data: dict[str, object]
) -> RemoteTask:
    task.status = _coerce_remote_status(data.get("status"))
    task.working_dir = str(data.get("workspace_dir", task.working_dir))
    task.output = str(data.get("output_preview", task.output) or "")
    error_preview = str(data.get("error_preview", "") or "")
    task.error = str(data.get("error", task.error) or error_preview or task.error)
    files_changed = data.get("files_changed")
    if isinstance(files_changed, list):
        task.files_changed = [str(item) for item in files_changed]
    task.metadata.update(
        {
            "provider": "ssh",
            "ssh_target": data.get("ssh_target", task.metadata.get("ssh_target", "")),
            "branch": data.get("branch", task.metadata.get("branch", "")),
            "repo_name": data.get("repo_name", task.metadata.get("repo_name", "")),
            "repo_url": data.get("repo_url", task.metadata.get("repo_url", "")),
            "head_sha": data.get("head_sha", task.metadata.get("head_sha", "")),
            "status_file": task.metadata.get("status_file", ""),
            "task_dir": data.get("task_dir", task.metadata.get("task_dir", "")),
            "publish_branch": data.get(
                "publish_branch", task.metadata.get("publish_branch", False)
            ),
        }
    )
    return task


def format_remote_config_summary(config: RemoteConfig) -> str:
    """Render a provider-aware summary of remote configuration."""
    lines = [
        "Remote execution",
        f"Provider: {config.provider}",
        f"Workspace sync: {'on' if config.workspace_sync else 'off'}",
        f"Auto-apply: {'on' if config.auto_apply else 'off'}",
    ]
    if config.provider == "langgraph-cloud":
        lines.extend(
            [
                f"API URL: {config.api_url or '(not configured)'}",
                f"API key: {'configured' if config.api_key else 'not configured'}",
            ]
        )
    elif config.provider == "ssh":
        from bog_agents_cli.remote_sandbox import build_ssh_target

        ssh_target = build_ssh_target(host=config.host, user=config.user) or (
            "(not configured)"
        )
        lines.extend(
            [
                f"SSH target: {ssh_target}",
                f"Remote root: {config.remote_root}",
                f"Repo URL override: {config.repo_url or '(use local origin)'}",
                f"Base branch: {config.base_branch or '(use local current branch)'}",
                f"Workspace mode: {config.workspace_mode}",
                f"Bog command: {config.bog_agents_command}",
                f"Shell allow-list: {config.shell_allow_list or '(disabled)'}",
                f"Publish branch: {'on' if config.publish_branch else 'off'}",
                f"MCP disabled: {'yes' if config.no_mcp else 'no'}",
            ]
        )
    return "\n".join(lines)


async def submit_remote_task(
    config: RemoteConfig,
    prompt: str,
    *,
    model: str = "",
    label: str = "",
    working_dir: Path | None = None,
    assistant_id: str = "agent",
    branch_prefix: str = "",
    base_branch: str | None = None,
) -> RemoteTask:
    """Submit a task for remote execution.

    Args:
        config: Remote execution configuration.
        prompt: Task instructions.
        model: Model to use.
        label: Optional task label.
        working_dir: Local working directory for git context discovery.
        assistant_id: Agent name to run in the remote sandbox.
        branch_prefix: Optional seed for the remote branch name.
        base_branch: Optional branch override for the remote checkout.

    Returns:
        RemoteTask with initial status.
    """
    task_id = str(uuid.uuid4())[:8]

    if config.provider == "langgraph-cloud" and config.api_url:
        try:
            from langgraph_sdk import get_client

            client = get_client(url=config.api_url)
            thread = await client.threads.create()
            run = await client.runs.create(
                thread["thread_id"],
                "agent",
                input={"messages": [{"role": "user", "content": prompt}]},
            )
            return RemoteTask(
                task_id=run["run_id"],
                prompt=prompt,
                label=label,
                status=RemoteStatus.RUNNING,
                model=model,
                working_dir=str(working_dir) if working_dir is not None else "",
                metadata={"thread_id": thread["thread_id"]},
            )
        except Exception as exc:
            return RemoteTask(
                task_id=task_id,
                prompt=prompt,
                label=label,
                status=RemoteStatus.FAILED,
                model=model,
                working_dir=str(working_dir) if working_dir is not None else "",
                error=f"Failed to submit to LangGraph Cloud: {exc}",
            )

    if config.provider == "ssh":
        from bog_agents_cli.remote_sandbox import (
            asset_text,
            build_task_payload,
            encode_script_arg,
            run_remote_python,
        )

        payload_or_error = build_task_payload(
            host=config.host,
            user=config.user,
            remote_root=config.remote_root,
            repo_url=config.repo_url,
            base_branch=config.base_branch,
            bog_agents_command=config.bog_agents_command,
            shell_allow_list=config.shell_allow_list,
            git_user_name=config.git_user_name,
            git_user_email=config.git_user_email,
            publish_branch=config.publish_branch,
            no_mcp=config.no_mcp,
            prompt=prompt,
            task_id=task_id,
            model=model,
            label=label,
            assistant_id=assistant_id,
            branch_prefix=branch_prefix,
            working_dir=working_dir,
            base_branch_override=base_branch,
        )
        if isinstance(payload_or_error, str):
            return RemoteTask(
                task_id=task_id,
                prompt=prompt,
                label=label,
                status=RemoteStatus.FAILED,
                model=model,
                working_dir=str(working_dir) if working_dir is not None else "",
                error=payload_or_error,
                metadata={"provider": "ssh"},
            )

        payload = payload_or_error
        rc, stdout, stderr = await run_remote_python(
            host=config.host,
            user=config.user,
            port=config.port,
            identity_file=config.identity_file,
            ssh_options=config.ssh_options,
            python_command=config.python_command,
            script_name="ssh_submit.py",
            args=[
                encode_script_arg(json.dumps(payload)),
                encode_script_arg(asset_text("ssh_worker.py")),
            ],
        )
        if rc != 0:
            msg = stderr or stdout or "Unknown SSH submission failure."
            return RemoteTask(
                task_id=task_id,
                prompt=prompt,
                label=label,
                status=RemoteStatus.FAILED,
                model=model,
                working_dir=str(payload["workspace_dir"]),
                error=msg,
                metadata={
                    "provider": "ssh",
                    "ssh_target": payload["ssh_target"],
                    "branch": payload["sandbox_branch"],
                    "repo_name": payload["repo_name"],
                    "status_file": payload["status_file"],
                },
            )
        try:
            response = json.loads(stdout or "{}")
        except json.JSONDecodeError:
            response = {}

        return RemoteTask(
            task_id=task_id,
            prompt=prompt,
            label=label,
            status=RemoteStatus.RUNNING,
            model=model,
            working_dir=str(payload["workspace_dir"]),
            metadata={
                "provider": "ssh",
                "ssh_target": payload["ssh_target"],
                "status_file": payload["status_file"],
                "task_dir": payload["task_dir"],
                "stdout_file": payload["stdout_file"],
                "stderr_file": payload["stderr_file"],
                "branch": payload["sandbox_branch"],
                "repo_name": payload["repo_name"],
                "repo_url": payload["repo_url"],
                "base_branch": payload["base_branch"],
                "publish_branch": payload["publish_branch"],
                "pid": response.get("pid"),
            },
        )

    return RemoteTask(
        task_id=task_id,
        prompt=prompt,
        label=label,
        status=RemoteStatus.FAILED,
        model=model,
        working_dir=str(working_dir) if working_dir is not None else "",
        error=f"Remote provider '{config.provider}' not configured. Set api_url in ~/.bog-agents/remote.json",
    )


async def check_remote_task(config: RemoteConfig, task: RemoteTask) -> RemoteTask:
    """Check the status of a remote task.

    Args:
        config: Remote execution configuration.
        task: The task to check.

    Returns:
        Updated RemoteTask.
    """
    provider = str(task.metadata.get("provider", config.provider))
    if provider == "langgraph-cloud" and config.api_url:
        try:
            from langgraph_sdk import get_client

            client = get_client(url=config.api_url)
            thread_id = str(task.metadata.get("thread_id", ""))
            run = await client.runs.get(thread_id, task.task_id)

            status_map = {
                "pending": RemoteStatus.PENDING,
                "running": RemoteStatus.RUNNING,
                "success": RemoteStatus.COMPLETED,
                "error": RemoteStatus.FAILED,
            }
            task.status = status_map.get(run.get("status", ""), RemoteStatus.PENDING)

            if task.status == RemoteStatus.COMPLETED:
                state = await client.threads.get_state(thread_id)
                messages = state.get("values", {}).get("messages", [])
                if messages:
                    last = messages[-1]
                    task.output = str(last.get("content", ""))
        except Exception as exc:
            logger.warning("Failed to check remote task: %s", exc)

    if provider == "ssh":
        from bog_agents_cli.remote_sandbox import run_remote_python

        status_file = str(task.metadata.get("status_file", "") or "")
        if not status_file:
            task.status = RemoteStatus.FAILED
            task.error = "Remote SSH task is missing a status file reference."
            return task
        rc, stdout, stderr = await run_remote_python(
            host=config.host,
            user=config.user,
            port=config.port,
            identity_file=config.identity_file,
            ssh_options=config.ssh_options,
            python_command=config.python_command,
            script_name="ssh_status.py",
            args=[status_file],
        )
        if rc != 0:
            task.status = RemoteStatus.FAILED
            task.error = stderr or stdout or "SSH status check failed."
            return task
        try:
            data = json.loads(stdout or "{}")
        except json.JSONDecodeError:
            task.status = RemoteStatus.FAILED
            task.error = stdout or "Could not decode SSH task status."
            return task
        return _update_task_from_ssh_state(task, data)

    return task


async def cancel_remote_task(config: RemoteConfig, task: RemoteTask) -> RemoteTask:
    """Cancel a remote task when the provider supports it.

    Args:
        config: Remote execution configuration.
        task: Task to cancel.

    Returns:
        Updated remote task after the cancellation attempt.
    """
    provider = str(task.metadata.get("provider", config.provider))
    if provider == "ssh":
        from bog_agents_cli.remote_sandbox import run_remote_python

        status_file = str(task.metadata.get("status_file", "") or "")
        if not status_file:
            task.status = RemoteStatus.FAILED
            task.error = "Remote SSH task is missing a status file reference."
            return task
        rc, stdout, stderr = await run_remote_python(
            host=config.host,
            user=config.user,
            port=config.port,
            identity_file=config.identity_file,
            ssh_options=config.ssh_options,
            python_command=config.python_command,
            script_name="ssh_cancel.py",
            args=[status_file],
        )
        if rc != 0:
            task.status = RemoteStatus.FAILED
            task.error = stderr or stdout or "SSH task cancellation failed."
            return task
        try:
            data = json.loads(stdout or "{}")
        except json.JSONDecodeError:
            task.status = RemoteStatus.CANCELLED
            return task
        return _update_task_from_ssh_state(task, data)

    task.error = (
        "Remote task cancellation is not supported by the current provider integration."
    )
    return task


def format_remote_tasks(tasks: list[RemoteTask]) -> str:
    """Format remote tasks for display.

    Args:
        tasks: List of remote tasks.

    Returns:
        Formatted string.
    """
    if not tasks:
        return "No remote tasks."

    lines = ["## Remote Tasks\n"]
    for task in tasks:
        status_icon = {
            RemoteStatus.PENDING: "...",
            RemoteStatus.RUNNING: ">>>",
            RemoteStatus.COMPLETED: " OK",
            RemoteStatus.FAILED: "ERR",
            RemoteStatus.CANCELLED: "---",
        }.get(task.status, "???")

        prompt_preview = (
            task.prompt[:60] + "..." if len(task.prompt) > 60 else task.prompt
        )
        label = f" {task.label}" if task.label else ""
        lines.append(f"[{status_icon}] {task.task_id}{label}: {prompt_preview}")

        detail_parts: list[str] = []
        if task.model:
            detail_parts.append(f"model={task.model}")
        if task.working_dir:
            detail_parts.append(f"cwd={task.working_dir}")
        if ssh_target := str(task.metadata.get("ssh_target", "") or ""):
            detail_parts.append(f"host={ssh_target}")
        if branch := str(task.metadata.get("branch", "") or ""):
            detail_parts.append(f"branch={branch}")
        if repo_name := str(task.metadata.get("repo_name", "") or ""):
            detail_parts.append(f"repo={repo_name}")
        if detail_parts:
            lines.append(f"  {' | '.join(detail_parts)}")

        if task.files_changed:
            lines.append(f"  Files changed: {', '.join(task.files_changed[:8])}")
        if task.error:
            lines.append(f"  Error: {task.error}")
        if task.output:
            output_preview = (
                task.output[:100] + "..." if len(task.output) > 100 else task.output
            )
            lines.append(f"  Output: {output_preview}")

    return "\n".join(lines)


def format_remote_recovery(task: RemoteTask) -> str:
    """Render recovery details for a remote sandbox task."""
    lines = [
        f"Remote recovery for {task.task_id}",
        f"Status: {task.status}",
        f"Label: {task.label or '(none)'}",
        f"Model: {task.model or '(default)'}",
    ]
    if task.working_dir:
        lines.append(f"Workspace: {task.working_dir}")
    if ssh_target := str(task.metadata.get("ssh_target", "") or ""):
        lines.append(f"SSH target: {ssh_target}")
    if branch := str(task.metadata.get("branch", "") or ""):
        lines.append(f"Branch: {branch}")
    if repo_url := str(task.metadata.get("repo_url", "") or ""):
        lines.append(f"Repo URL: {repo_url}")
    if stdout_file := str(task.metadata.get("stdout_file", "") or ""):
        lines.append(f"Stdout log: {stdout_file}")
    if stderr_file := str(task.metadata.get("stderr_file", "") or ""):
        lines.append(f"Stderr log: {stderr_file}")
    if task.files_changed:
        lines.append(f"Files changed: {', '.join(task.files_changed[:12])}")
    if task.output:
        lines.extend(["", "Output preview:", task.output])
    if task.error:
        lines.extend(["", "Error:", task.error])

    commands: list[str] = []
    if ssh_target and task.working_dir:
        commands.extend(
            [
                f"  ssh {ssh_target}",
                f"  cd {task.working_dir}",
                "  git status",
            ]
        )
    if branch:
        commands.append(f"  git log --oneline {branch} -n 5")
    if commands:
        lines.extend(["", "Suggested recovery steps:", *commands])
    return "\n".join(lines)
