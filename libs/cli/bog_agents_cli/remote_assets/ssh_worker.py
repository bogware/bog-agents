"""Run a full Bog Agents headless task inside the remote SSH sandbox workspace."""

from __future__ import annotations

import json
import os
import shlex
import subprocess  # noqa: S404 - required for git and CLI execution
import sys
import time
from pathlib import Path


def _write_json(path: Path, data: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


def _run(
    args: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - command inputs are prepared by trusted bootstrap code
        args,
        cwd=str(cwd) if cwd is not None else None,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def _ensure_success(result: subprocess.CompletedProcess[str], fallback: str) -> None:
    if result.returncode != 0:
        msg = result.stderr or result.stdout or fallback
        raise RuntimeError(msg)


def _excerpt(path: Path, limit: int = 4000) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")[:limit]


def _status_files_changed(workspace_dir: Path) -> list[str]:
    result = _run(["git", "status", "--porcelain"], cwd=workspace_dir)
    if result.returncode != 0:
        return []
    files: list[str] = []
    for line in result.stdout.splitlines():
        if len(line) >= 4:
            files.append(line[3:])
    return files


def main() -> int:
    """Execute the remote sandbox task from its serialized payload."""
    payload_path = Path(sys.argv[1]).expanduser()
    payload = json.loads(payload_path.read_text(encoding="utf-8"))

    task_dir = Path(str(payload["task_dir"])).expanduser()
    status_file = Path(str(payload["status_file"])).expanduser()
    stdout_file = Path(str(payload["stdout_file"])).expanduser()
    stderr_file = Path(str(payload["stderr_file"])).expanduser()
    prompt_file = Path(str(payload["prompt_file"])).expanduser()
    workspace_dir = Path(str(payload["workspace_dir"])).expanduser()

    status: dict[str, object] = {
        "task_id": payload["task_id"],
        "label": payload["label"],
        "prompt": payload["prompt"],
        "repo_name": payload["repo_name"],
        "repo_url": payload["repo_url"],
        "branch": payload["sandbox_branch"],
        "base_branch": payload["base_branch"],
        "workspace_dir": str(workspace_dir),
        "task_dir": str(task_dir),
        "output_file": str(stdout_file),
        "stderr_file": str(stderr_file),
        "pid": os.getpid(),
        "started_at": time.time(),
        "status": "running",
        "provider": "ssh",
        "ssh_target": payload["ssh_target"],
        "publish_branch": payload["publish_branch"],
    }
    _write_json(status_file, status)

    try:
        workspace_dir.parent.mkdir(parents=True, exist_ok=True)
        clone_result = _run(
            ["git", "clone", str(payload["repo_url"]), str(workspace_dir)]
        )
        _ensure_success(clone_result, "git clone failed")

        fetch_result = _run(["git", "fetch", "origin", "--prune"], cwd=workspace_dir)
        _ensure_success(fetch_result, "git fetch failed")

        git_user_name = str(payload.get("git_user_name", "") or "")
        git_user_email = str(payload.get("git_user_email", "") or "")
        if git_user_name:
            _run(["git", "config", "user.name", git_user_name], cwd=workspace_dir)
        if git_user_email:
            _run(["git", "config", "user.email", git_user_email], cwd=workspace_dir)

        base_branch = str(payload["base_branch"])
        sandbox_branch = str(payload["sandbox_branch"])
        head_check = _run(
            ["git", "ls-remote", "--heads", "origin", base_branch],
            cwd=workspace_dir,
        )
        if head_check.returncode == 0 and head_check.stdout.strip():
            checkout_result = _run(
                ["git", "checkout", "-B", sandbox_branch, f"origin/{base_branch}"],
                cwd=workspace_dir,
            )
        else:
            checkout_result = _run(
                ["git", "checkout", "-B", sandbox_branch],
                cwd=workspace_dir,
            )
        _ensure_success(checkout_result, "git checkout failed")

        if bool(payload.get("publish_branch", False)):
            _run(["git", "push", "-u", "origin", sandbox_branch], cwd=workspace_dir)

        env = os.environ.copy()
        env["BOG_REMOTE_SANDBOX"] = "1"

        command = shlex.split(str(payload["bog_agents_command"]))
        command.extend(
            [
                "--agent",
                str(payload["assistant_id"]),
                "--output",
                "json",
                "--no-stream",
                "--auto-approve",
            ]
        )
        shell_allow_list = str(payload.get("shell_allow_list", "") or "")
        if shell_allow_list:
            command.extend(["--shell-allow-list", shell_allow_list])
        if bool(payload.get("no_mcp", True)):
            command.append("--no-mcp")
        model = str(payload.get("model", "") or "")
        if model:
            command.extend(["--model", model])

        with (
            prompt_file.open("r", encoding="utf-8") as prompt_stream,
            stdout_file.open("w", encoding="utf-8") as stdout_stream,
            stderr_file.open("w", encoding="utf-8") as stderr_stream,
        ):
            exec_result = subprocess.run(  # noqa: S603 - generated command is controlled by payload/config
                command,
                cwd=str(workspace_dir),
                env=env,
                stdin=prompt_stream,
                stdout=stdout_stream,
                stderr=stderr_stream,
                text=True,
                check=False,
            )

        branch_result = _run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=workspace_dir
        )
        head_result = _run(["git", "rev-parse", "HEAD"], cwd=workspace_dir)
        status.update(
            {
                "status": "completed" if exec_result.returncode == 0 else "failed",
                "completed_at": time.time(),
                "exit_code": exec_result.returncode,
                "branch": branch_result.stdout.strip() or sandbox_branch,
                "head_sha": head_result.stdout.strip(),
                "files_changed": _status_files_changed(workspace_dir),
                "output_preview": _excerpt(stdout_file),
                "error_preview": _excerpt(stderr_file),
            }
        )
        _write_json(status_file, status)
        return 0
    except Exception as exc:
        status.update(
            {
                "status": "failed",
                "completed_at": time.time(),
                "error": str(exc),
                "output_preview": _excerpt(stdout_file),
                "error_preview": _excerpt(stderr_file),
            }
        )
        _write_json(status_file, status)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
