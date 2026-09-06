"""ROADMAP #55: the schedule / subscribe tool bundle and its parsers."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest

from bog_agents.tools.daemon_tools import (
    DaemonUnavailableError,
    build_job_payload,
    daemon_tools_bundle,
    parse_source,
    parse_when,
    thread_id_from_runtime,
)

NOW = datetime(2026, 9, 5, 10, 0, 0)


class _FakeDaemon:
    def __init__(self, *, down: bool = False) -> None:
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []
        self.jobs: list[dict[str, Any]] = []
        self.down = down

    def request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        if self.down:
            msg = "daemon not reachable at http://127.0.0.1:7391; start it with `bog-agents daemon start`"
            raise DaemonUnavailableError(msg)
        self.calls.append((method, path, payload))
        if method == "POST":
            job = {"job_id": f"job{len(self.jobs) + 1}", **(payload or {}), "run_count": 0}
            self.jobs.append(job)
            return job
        if method == "GET":
            return list(self.jobs)
        if method == "DELETE":
            self.jobs = [j for j in self.jobs if not path.endswith(j["job_id"])]
            return None
        return None


def _runtime(thread_id: str | None = "thread-abc") -> SimpleNamespace:
    return SimpleNamespace(config={"configurable": {"thread_id": thread_id}} if thread_id else {})


class TestParsing:
    def test_when_forms(self) -> None:
        once = parse_when("in 2 hours", now=NOW)
        assert once["trigger"] == {"type": "cron", "cron": "0 12 5 9 *"} and once["max_runs"] == 1
        assert parse_when("in 45m", now=NOW)["trigger"]["cron"] == "45 10 5 9 *"
        every = parse_when("every 30 minutes", now=NOW)
        assert every["trigger"] == {"type": "interval", "interval_seconds": 1800} and every["max_runs"] == 0
        assert parse_when("daily", now=NOW)["trigger"]["interval_seconds"] == 86400
        assert parse_when("at 09:30", now=NOW)["trigger"]["cron"] == "30 9 6 9 *"  # already past today → tomorrow
        assert parse_when("2026-12-01T08:15", now=NOW)["trigger"]["cron"] == "15 8 1 12 *"
        cron = parse_when("0 9 * * 1-5", now=NOW)
        assert cron["trigger"] == {"type": "cron", "cron": "0 9 * * 1-5"} and cron["max_runs"] == 0
        with pytest.raises(ValueError, match="could not parse"):
            parse_when("whenever", now=NOW)

    def test_source_forms(self) -> None:
        pr = parse_source("github:pr:123")
        assert pr["type"] == "github" and pr["github_number"] == 123 and "ci_failure" in pr["github_kinds"]
        assert parse_source("github:issue:7")["github_kinds"][0].startswith("issue")
        assert parse_source("github") == {"type": "github"}
        assert parse_source("webhook:/deploys") == {"type": "webhook", "webhook_path": "deploys"}
        assert parse_source("file:src:*.py") == {"type": "file_change", "watch_dir": "src", "watch_patterns": ["*.py"]}
        assert parse_source("cron:0 * * * *") == {"type": "cron", "cron": "0 * * * *"}
        with pytest.raises(ValueError, match="unknown source"):
            parse_source("slack:channel")

    def test_payload_and_runtime_thread(self) -> None:
        payload = build_job_payload(
            name="n", prompt="p", trigger={"type": "github"}, max_runs=2, thread_id="t1", goal_ref="/g.json", working_dir="/w"
        )
        assert payload["thread_id"] == "t1" and payload["max_runs"] == 2 and payload["triggers"] == [{"type": "github"}]
        assert thread_id_from_runtime(_runtime("t9")) == "t9"
        assert thread_id_from_runtime(_runtime(None)) == ""
        assert thread_id_from_runtime(object()) == ""


class TestBundle:
    def test_schedule_subscribe_list_unsubscribe(self) -> None:
        daemon = _FakeDaemon()
        tools = {
            t.name: t for t in daemon_tools_bundle(client=daemon, working_dir="/repo", goal_ref="/repo/.bog-agents/goal.json", clock=lambda: NOW)
        }
        assert set(tools) == {"schedule", "subscribe", "list_subscriptions", "unsubscribe"}
        out = tools["schedule"].func(_runtime(), prompt="run the nightly checks", when="in 2 hours")
        assert out.startswith("Scheduled job job1 (once at 2026-09-05 12:00) on thread thread-abc")
        method, path, payload = daemon.calls[0]
        assert (method, path) == ("POST", "/jobs")
        assert payload is not None and payload["thread_id"] == "thread-abc" and payload["max_runs"] == 1
        assert payload["goal_ref"].endswith("goal.json") and payload["working_dir"] == "/repo"
        out = tools["subscribe"].func(_runtime(), source="github:pr:42", prompt="address review comments", until_runs=3)
        assert "Subscribed job job2 to github:pr:42 (up to 3 run(s))" in out
        assert daemon.jobs[1]["triggers"][0]["github_number"] == 42
        listing = tools["list_subscriptions"].func(_runtime())
        assert "job1" in listing and "job2" in listing and "0/1 runs" in listing
        assert tools["list_subscriptions"].func(_runtime("other")) == "No daemon jobs for this thread."
        assert tools["unsubscribe"].func(_runtime(), job_id="job1") == "Deleted daemon job job1."
        assert [j["job_id"] for j in daemon.jobs] == ["job2"]

    def test_errors_are_strings_not_exceptions(self) -> None:
        tools = {t.name: t for t in daemon_tools_bundle(client=_FakeDaemon(down=True))}
        assert tools["schedule"].func(_runtime(), prompt="x", when="in 1h").startswith("Error: daemon not reachable")
        assert tools["schedule"].func(_runtime(), prompt="x", when="whenever").startswith("Error: could not parse")
        assert tools["subscribe"].func(_runtime(), source="nope", prompt="x").startswith("Error: unknown source")
        assert tools["list_subscriptions"].func(_runtime()).startswith("Error:")
