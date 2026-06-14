"""Tests for the CI self-fix support module (ROADMAP #1)."""

from __future__ import annotations

import json

import pytest

from bog_agents_cli import ci_tools
from bog_agents_cli.ci_tools import (
    CIRun,
    GhUnavailableError,
    generate_ci_fix_prompt,
    get_ci_status,
    latest_run,
    parse_run_list,
)

_SAMPLE = json.dumps(
    [
        {
            "databaseId": 27322372365,
            "status": "completed",
            "conclusion": "failure",
            "workflowName": "CI",
            "headBranch": "feature-x",
            "event": "pull_request",
            "createdAt": "2026-06-11T03:45:37Z",
            "url": "https://github.com/o/r/actions/runs/27322372365",
        },
        {
            "databaseId": 27298710958,
            "status": "in_progress",
            "conclusion": None,
            "workflowName": "CI",
            "headBranch": "feature-x",
            "event": "pull_request",
            "createdAt": "2026-06-10T18:51:56Z",
            "url": "https://github.com/o/r/actions/runs/27298710958",
        },
    ]
)


class TestParseRunList:
    def test_parses_fields(self) -> None:
        runs = parse_run_list(_SAMPLE)
        assert len(runs) == 2
        assert runs[0].run_id == 27322372365
        assert runs[0].workflow == "CI"
        assert runs[0].is_failure
        assert runs[0].is_complete
        assert runs[1].conclusion == ""  # None -> ""
        assert runs[1].is_pending

    def test_invalid_json_is_empty(self) -> None:
        assert parse_run_list("not json") == []
        assert parse_run_list("{}") == []

    def test_latest_run(self) -> None:
        runs = parse_run_list(_SAMPLE)
        assert latest_run(runs) is runs[0]
        assert latest_run([]) is None


class TestCIRunFlags:
    def test_success_not_failure(self) -> None:
        run = CIRun(1, "CI", "completed", "success", "b", "push", "", "")
        assert not run.is_failure
        assert run.is_complete
        assert not run.is_pending

    def test_pending_states(self) -> None:
        for status in ("queued", "in_progress", "waiting"):
            run = CIRun(1, "CI", status, "", "b", "push", "", "")
            assert run.is_pending


class TestPrompt:
    def test_prompt_contains_logs_and_task(self) -> None:
        run = parse_run_list(_SAMPLE)[0]
        prompt = generate_ci_fix_prompt("feature-x", run, "AssertionError: boom")
        assert "feature-x" in prompt
        assert "CI" in prompt
        assert "AssertionError: boom" in prompt
        assert "Diagnose" in prompt or "diagnose" in prompt
        assert "flaky" in prompt.lower()


class TestGhWrappers:
    def test_get_ci_status_parses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(ci_tools, "_run_gh", lambda *a, **k: _SAMPLE)
        runs = get_ci_status("feature-x")
        assert len(runs) == 2

    def test_get_ci_status_propagates_gh_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(*_a: object, **_k: object) -> str:
            raise GhUnavailableError("gh not installed")

        monkeypatch.setattr(ci_tools, "_run_gh", _boom)
        with pytest.raises(GhUnavailableError):
            get_ci_status("feature-x")

    def test_get_failing_logs_truncates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        big = "x" * 50000
        monkeypatch.setattr(ci_tools, "_run_gh", lambda *a, **k: big)
        out = ci_tools.get_failing_logs(123, max_chars=1000)
        assert len(out) < len(big)
        assert "truncated" in out

    def test_missing_gh_binary_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _no_gh(*_a: object, **_k: object) -> object:
            raise FileNotFoundError("gh")

        monkeypatch.setattr(ci_tools.subprocess, "run", _no_gh)
        with pytest.raises(GhUnavailableError):
            get_ci_status("b")
