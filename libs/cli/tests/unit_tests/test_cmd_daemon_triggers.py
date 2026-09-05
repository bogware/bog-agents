"""`bog-agents daemon jobs create` trigger flags (v6 DMN-2).

The GitHub front door (`POST /webhooks/github`) parsed events and
dispatched `github`-trigger jobs, but no CLI flag could create such a job —
REST was the only way and no document mentioned it.
"""

from __future__ import annotations

import argparse
from types import SimpleNamespace

from bog_agents_cli import cmd_daemon


def _parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    cmd_daemon.setup_daemon_parser(sub)
    return parser.parse_args(argv)


def _args(**overrides: object) -> SimpleNamespace:
    base: dict[str, object] = {
        "cron": "",
        "interval": 0,
        "watch_dir": "",
        "watch_pattern": None,
        "debounce": 5.0,
        "webhook_path": "",
        "git_branch": "",
        "github": False,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_github_flag_parses() -> None:
    args = _parse(
        ["daemon", "jobs", "create", "--name", "gh", "--github", "--prompt", "x"]
    )
    assert args.github is True


def test_triggers_from_args_maps_github_flag() -> None:
    assert cmd_daemon._triggers_from_args(_args(github=True)) == [{"type": "github"}]


def test_triggers_from_args_keeps_existing_flags() -> None:
    triggers = cmd_daemon._triggers_from_args(
        _args(cron="0 9 * * 1-5", git_branch="main", github=True)
    )
    assert triggers == [
        {"type": "cron", "cron": "0 9 * * 1-5"},
        {"type": "git_push", "git_branch_pattern": "main"},
        {"type": "github"},
    ]


def test_trigger_summary_names_github() -> None:
    assert "GitHub" in cmd_daemon._trigger_summary({"type": "github"})


def test_output_github_issue_accepts_placeholder() -> None:
    args = _parse(
        [
            "daemon",
            "jobs",
            "create",
            "--name",
            "gh",
            "--github",
            "--prompt",
            "x",
            "--output",
            "github_comment",
            "--output-github-repo",
            "o/r",
            "--output-github-issue",
            "{issue_number}",
        ]
    )
    assert args.output_github_issue == "{issue_number}"


def test_budget_flags_parse_and_reach_the_payload() -> None:
    """ROADMAP #51: `--budget-usd` / `--daily-ceiling-usd` on create and edit, `jobs resume`."""
    args = _parse(
        [
            "daemon",
            "jobs",
            "create",
            "--name",
            "b",
            "--prompt",
            "x",
            "--budget-usd",
            "2.5",
            "--daily-ceiling-usd",
            "9",
        ]
    )
    assert (args.budget_usd, args.daily_ceiling_usd) == (2.5, 9.0)
    edit = _parse(["daemon", "jobs", "edit", "job1", "--budget-usd", "3"])
    assert edit.budget_usd == 3.0
    resume = _parse(["daemon", "jobs", "resume", "run1", "--budget-usd", "4"])
    assert (resume.jobs_command, resume.run_id, resume.budget_usd) == (
        "resume",
        "run1",
        4.0,
    )
