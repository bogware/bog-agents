"""Tests for the CLI team executor (#21) — ledger construction + session wiring."""

from __future__ import annotations

from bog_agents.cost_ledger import RunawayCaps
from bog_agents.teams import DONE, Mailbox, TaskResult

from bog_agents_cli.team_executor import (
    build_ledger,
    parse_team_run_args,
    run_team_session,
)


class TestParseTeamRunArgs:
    def test_pipe_separated_titles(self) -> None:
        req = parse_team_run_args("do a |  do b | do c ")
        assert req.task_specs == ["do a", "do b", "do c"]
        assert req.members == []

    def test_members_override(self) -> None:
        req = parse_team_run_args("--members alice,bob write docs | write tests")
        assert req.members == ["alice", "bob"]
        assert req.task_specs == ["write docs", "write tests"]

    def test_chain_wires_linear_dependencies(self) -> None:
        req = parse_team_run_args("--chain design | build | ship")
        assert req.task_specs == ["design", ("build", ["design"]), ("ship", ["build"])]

    def test_blank_tasks_dropped(self) -> None:
        req = parse_team_run_args("only one ||  ")
        assert req.task_specs == ["only one"]


class TestBuildLedger:
    def test_titles_become_tasks(self) -> None:
        ledger = build_ledger(["a", "b", "c"])
        assert [t.title for t in ledger.tasks()] == ["a", "b", "c"]

    def test_dependencies_wired_by_title(self) -> None:
        ledger = build_ledger([("build", ["setup"]), "setup"])
        # "build" is not claimable until "setup" is done.
        assert ledger.claim_next("w").title == "setup"
        assert ledger.claim_next("w") is None


class TestRunTeamSession:
    async def test_runs_all_tasks_with_injected_runner(self, tmp_path) -> None:
        done_order: list[str] = []

        async def fake_runner(member: str, task, mailbox) -> TaskResult:
            done_order.append(task.title)
            return TaskResult(success=True, result=f"{member}:{task.title}")

        report = await run_team_session(
            [("build", ["setup"]), "setup", ("test", ["build"])],
            ["w1", "w2"],
            repo_dir=tmp_path,
            teammate_runner=fake_runner,
        )
        assert report.all_done is True
        assert all(t.status == DONE for t in report.ledger.tasks())
        # Dependency order respected across the shared ledger.
        assert (
            done_order.index("setup")
            < done_order.index("build")
            < done_order.index("test")
        )

    async def test_spawn_cap_enforced(self, tmp_path) -> None:
        async def fake_runner(member: str, task, mailbox) -> TaskResult:
            return TaskResult(success=True)

        report = await run_team_session(
            ["a", "b", "c", "d"],
            ["w1"],
            repo_dir=tmp_path,
            teammate_runner=fake_runner,
            caps=RunawayCaps(max_subagents=2),
        )
        assert report.stopped_reason == "spawn cap reached"
        assert sum(1 for t in report.ledger.tasks() if t.status == DONE) == 2

    async def test_mailbox_notes_reach_teammates(self, tmp_path) -> None:
        # A teammate can post to the shared mailbox; the executor passes one in.
        seen: dict[str, int] = {}

        async def fake_runner(member: str, task, mailbox: Mailbox) -> TaskResult:
            mailbox.send(member, Mailbox.ALL, f"{member} finished {task.title}")
            seen[task.title] = len(mailbox.inbox(member))
            return TaskResult(success=True)

        report = await run_team_session(
            ["a", "b"], ["w1"], repo_dir=tmp_path, teammate_runner=fake_runner
        )
        assert report.all_done is True
