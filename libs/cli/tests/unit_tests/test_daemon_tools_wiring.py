"""ROADMAP #55 (CLI): daemon tools ride on the interactive agent only while the daemon runs; job create flags."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

from bog_agents_cli import cmd_daemon
from bog_agents_cli.tokens_audit_controller import audit_cli_agent

if TYPE_CHECKING:
    import pytest


def test_daemon_tools_only_when_daemon_is_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("bog_agents_cli.daemon_client.is_daemon_running", lambda: False)
    off = {
        t.name
        for t in audit_cli_agent(
            harness_profile=None, cwd=tmp_path, method="approx"
        ).tools
    }
    assert "schedule" not in off and "subscribe" not in off
    monkeypatch.setattr("bog_agents_cli.daemon_client.is_daemon_running", lambda: True)
    on = {
        t.name
        for t in audit_cli_agent(
            harness_profile=None, cwd=tmp_path, method="approx"
        ).tools
    }
    assert {"schedule", "subscribe", "list_subscriptions", "unsubscribe"} <= on


def test_jobs_create_flags_flow_into_the_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: dict[str, object] = {}

    def _post(
        path: str, payload: dict[str, object], *, port: int = 0
    ) -> dict[str, object]:
        sent["path"] = path
        sent["payload"] = payload
        return {"job_id": "abc", **payload}

    monkeypatch.setattr(cmd_daemon, "_api_post", _post)
    args = SimpleNamespace(
        port=7391,
        name="pr-42",
        prompt="address the review",
        description="",
        model="",
        working_dir="",
        pipeline="",
        skill="",
        output="log",
        output_file="",
        output_slack="",
        output_webhook="",
        disabled=False,
        cron="",
        interval=0,
        watch_dir="",
        watch_pattern=[],
        debounce=5.0,
        webhook_path="",
        webhook_secret="",
        git_branch="",
        github=False,
        github_number=42,
        max_runs=3,
        thread="thread-xyz",
        budget_usd=None,
        daily_ceiling_usd=None,
    )
    cmd_daemon.cmd_jobs_create(args)
    payload = sent["payload"]
    assert isinstance(payload, dict)
    assert payload["max_runs"] == 3 and payload["thread_id"] == "thread-xyz"
    assert payload["triggers"] == [{"type": "github", "github_number": 42}]
