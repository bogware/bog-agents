"""Tests for remote execution providers."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

from bog_agents_cli.remote import (
    RemoteConfig,
    RemoteStatus,
    RemoteTask,
    cancel_remote_task,
    check_remote_task,
    find_remote_task,
    format_remote_config_summary,
    format_remote_recovery,
    format_remote_tasks,
    load_remote_config,
    load_remote_tasks,
    save_remote_tasks,
    submit_remote_task,
)


class TestLoadRemoteConfig:
    """Tests for remote config parsing."""

    def test_load_remote_config_parses_ssh_fields(self) -> None:
        """SSH-oriented remote config fields should deserialize cleanly."""
        config_dir = Path("E:/Code/bog-agents/.fake-bog-agents-config")
        payload = json.dumps(
            {
                "provider": "ssh",
                "host": "sandbox.example.com",
                "user": "bog",
                "port": 2222,
                "identity_file": "~/.ssh/id_ed25519",
                "ssh_options": ["StrictHostKeyChecking=accept-new"],
                "remote_root": "/srv/bog-remote",
                "repo_url": "git@github.com:bogware/bog-agents.git",
                "base_branch": "main",
                "bog_agents_command": "bog-agents",
                "shell_allow_list": "all",
                "publish_branch": True,
                "no_mcp": True,
            }
        )

        with (
            patch.object(Path, "exists", return_value=True),
            patch.object(Path, "read_text", return_value=payload),
        ):
            config = load_remote_config(config_dir)

        assert config.provider == "ssh"
        assert config.host == "sandbox.example.com"
        assert config.user == "bog"
        assert config.port == 2222
        assert config.remote_root == "/srv/bog-remote"
        assert config.publish_branch is True
        assert config.ssh_options == ["StrictHostKeyChecking=accept-new"]


class TestSshRemoteProvider:
    """Tests for the SSH-backed remote sandbox provider."""

    async def test_submit_remote_task_ssh_uses_remote_bootstrap(self) -> None:
        """SSH submission should emit running metadata from the bootstrap response."""
        config = RemoteConfig(
            provider="ssh",
            host="sandbox.example.com",
            user="bog",
            remote_root="/srv/bog-remote",
        )

        from bog_agents_cli.remote_sandbox import LocalGitContext

        git_context = LocalGitContext(
            repo_root=Path("E:/Code/bog-agents"),
            repo_name="bog-agents",
            repo_url="git@github.com:bogware/bog-agents.git",
            branch="main",
        )

        with (
            patch(
                "bog_agents_cli.remote_sandbox.detect_local_git_context",
                return_value=git_context,
            ),
            patch(
                "bog_agents_cli.remote_sandbox.run_remote_python",
                new=AsyncMock(
                    return_value=(
                        0,
                        json.dumps({"pid": 4242}),
                        "",
                    )
                ),
            ),
        ):
            task = await submit_remote_task(
                config,
                "Inspect the repo and prepare a patch.",
                model="gpt-5.4",
                label="scout",
                assistant_id="agent",
                branch_prefix="review",
                working_dir=git_context.repo_root,
            )

        assert task.status == RemoteStatus.RUNNING
        assert task.metadata["provider"] == "ssh"
        assert task.metadata["ssh_target"] == "bog@sandbox.example.com"
        assert str(task.metadata["branch"]).startswith("sandbox/review-")
        assert (
            task.working_dir == "/srv/bog-remote/workspaces/bog-agents/" + task.task_id
        )

    async def test_check_remote_task_updates_from_status_payload(self) -> None:
        """SSH status polling should map JSON state into the task object."""
        config = RemoteConfig(provider="ssh", host="sandbox.example.com", user="bog")
        task = RemoteTask(
            task_id="abc12345",
            prompt="ship it",
            metadata={"provider": "ssh", "status_file": "/srv/status.json"},
        )

        with patch(
            "bog_agents_cli.remote_sandbox.run_remote_python",
            new=AsyncMock(
                return_value=(
                    0,
                    json.dumps(
                        {
                            "status": "completed",
                            "workspace_dir": "/srv/bog-remote/workspaces/bog-agents/abc12345",
                            "output_preview": "done",
                            "files_changed": ["README.md"],
                            "branch": "sandbox/review-abc12345",
                            "repo_name": "bog-agents",
                            "ssh_target": "bog@sandbox.example.com",
                        }
                    ),
                    "",
                )
            ),
        ):
            updated = await check_remote_task(config, task)

        assert updated.status == RemoteStatus.COMPLETED
        assert updated.output == "done"
        assert updated.files_changed == ["README.md"]
        assert updated.metadata["branch"] == "sandbox/review-abc12345"

    async def test_cancel_remote_task_marks_task_cancelled(self) -> None:
        """SSH cancellation should update the tracked task state."""
        config = RemoteConfig(provider="ssh", host="sandbox.example.com", user="bog")
        task = RemoteTask(
            task_id="abc12345",
            prompt="ship it",
            metadata={"provider": "ssh", "status_file": "/srv/status.json"},
        )

        with patch(
            "bog_agents_cli.remote_sandbox.run_remote_python",
            new=AsyncMock(
                return_value=(
                    0,
                    json.dumps(
                        {
                            "status": "cancelled",
                            "workspace_dir": "/srv/bog-remote/workspaces/bog-agents/abc12345",
                            "ssh_target": "bog@sandbox.example.com",
                        }
                    ),
                    "",
                )
            ),
        ):
            updated = await cancel_remote_task(config, task)

        assert updated.status == RemoteStatus.CANCELLED


class TestRemoteFormatting:
    """Tests for remote formatting helpers."""

    def test_format_remote_config_summary_for_ssh(self) -> None:
        """SSH config summaries should expose sandbox-centric settings."""
        config = RemoteConfig(
            provider="ssh",
            host="sandbox.example.com",
            user="bog",
            remote_root="/srv/bog-remote",
            shell_allow_list="all",
        )

        summary = format_remote_config_summary(config)

        assert "SSH target: bog@sandbox.example.com" in summary
        assert "Remote root: /srv/bog-remote" in summary

    def test_format_remote_tasks_includes_sandbox_details(self) -> None:
        """Formatted task tables should surface SSH host, branch, and repo."""
        task = RemoteTask(
            task_id="abc12345",
            prompt="inspect repo",
            label="scout",
            status=RemoteStatus.RUNNING,
            model="gpt-5.4",
            working_dir="/srv/bog-remote/workspaces/bog-agents/abc12345",
            metadata={
                "ssh_target": "bog@sandbox.example.com",
                "branch": "sandbox/review-abc12345",
                "repo_name": "bog-agents",
            },
        )

        rendered = format_remote_tasks([task])

        assert "host=bog@sandbox.example.com" in rendered
        assert "branch=sandbox/review-abc12345" in rendered
        assert "repo=bog-agents" in rendered

    def test_format_remote_recovery_includes_recovery_steps(self) -> None:
        """Recovery formatting should surface SSH and branch follow-up details."""
        task = RemoteTask(
            task_id="abc12345",
            prompt="inspect repo",
            status=RemoteStatus.COMPLETED,
            working_dir="/srv/bog-remote/workspaces/bog-agents/abc12345",
            output="done",
            metadata={
                "ssh_target": "bog@sandbox.example.com",
                "branch": "sandbox/review-abc12345",
                "repo_url": "git@github.com:bogware/bog-agents.git",
            },
        )

        rendered = format_remote_recovery(task)

        assert "SSH target: bog@sandbox.example.com" in rendered
        assert "git status" in rendered
        assert "git log --oneline sandbox/review-abc12345 -n 5" in rendered


class TestRemotePersistence:
    """Tests for persisted remote task recovery."""

    def test_save_and_load_remote_tasks_round_trip(self, tmp_path: Path) -> None:
        """Persisted remote tasks should load back with stable metadata."""
        task = RemoteTask(
            task_id="run-123",
            prompt="ship it",
            label="scout",
            status=RemoteStatus.RUNNING,
            metadata={"provider": "ssh", "branch": "sandbox/review-run-123"},
        )

        save_remote_tasks(tmp_path, [task])
        loaded = load_remote_tasks(tmp_path)

        assert len(loaded) == 1
        assert loaded[0].task_id == "run-123"
        assert loaded[0].label == "scout"
        assert loaded[0].metadata["branch"] == "sandbox/review-run-123"

    def test_find_remote_task_returns_matching_task(self, tmp_path: Path) -> None:
        """Task lookup should find one persisted task by ID."""
        save_remote_tasks(
            tmp_path,
            [RemoteTask(task_id="run-999", prompt="recover me", label="worker")],
        )

        task = find_remote_task(tmp_path, "run-999")

        assert task is not None
        assert task.task_id == "run-999"
