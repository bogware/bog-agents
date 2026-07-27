"""Tests for governed agent teams (#21) — ledger, mailbox, coordinator loop."""

from __future__ import annotations

from bog_agents.cost_ledger import CostLedger, RunawayCaps
from bog_agents.teams import (
    DONE,
    FAILED,
    Mailbox,
    TaskLedger,
    TaskResult,
    run_team,
)


class TestTaskLedger:
    def test_claim_is_atomic(self) -> None:
        ledger = TaskLedger()
        ledger.add("only task")
        first = ledger.claim_next("a")
        assert first is not None and first.owner == "a"
        # A second claim can't grab the same (now-claimed) task.
        assert ledger.claim_next("b") is None

    def test_dependency_gating(self) -> None:
        ledger = TaskLedger()
        t1 = ledger.add("setup")
        ledger.add("build", depends_on=[t1.id])
        # Only the dependency-free task is claimable.
        claimed = ledger.claim_next("a")
        assert claimed.title == "setup"
        assert ledger.claim_next("b") is None  # "build" blocked until setup done
        ledger.complete(t1.id, "a", "ok")
        assert ledger.claim_next("b").title == "build"

    def test_failed_dependency_blocks_forever(self) -> None:
        ledger = TaskLedger()
        t1 = ledger.add("setup")
        ledger.add("build", depends_on=[t1.id])
        ledger.claim_next("a")
        ledger.fail(t1.id, "a", "broke")
        assert ledger.has_claimable() is False
        assert ledger.is_done() is False  # "build" is stuck open, never terminal

    def test_is_done_when_all_terminal(self) -> None:
        ledger = TaskLedger()
        a = ledger.add("a")
        b = ledger.add("b")
        ledger.complete(a.id, "x", "")
        ledger.fail(b.id, "x", "")
        assert ledger.is_done() is True


class TestMailbox:
    def test_addressed_delivery(self) -> None:
        box = Mailbox()
        box.send("lead", "worker-1", "do the thing")
        assert [m.body for m in box.inbox("worker-1")] == ["do the thing"]
        assert box.inbox("worker-2") == []  # not addressed to worker-2

    def test_broadcast_visible_to_all_but_sender(self) -> None:
        box = Mailbox()
        box.send("lead", Mailbox.ALL, "standup")
        assert box.inbox("worker-1")[0].body == "standup"
        assert box.inbox("lead") == []  # sender doesn't see own broadcast

    def test_drain_consumes(self) -> None:
        box = Mailbox()
        box.send("lead", "w", "one")
        assert [m.body for m in box.drain("w")] == ["one"]
        assert box.drain("w") == []  # already consumed
        box.send("lead", "w", "two")
        assert [m.body for m in box.drain("w")] == ["two"]


def _make_ledger(*specs: tuple[str, list[str]]) -> tuple[TaskLedger, dict[str, str]]:
    """Build a ledger from (title, dep_titles) specs; return (ledger, title->id)."""
    ledger = TaskLedger()
    ids: dict[str, str] = {}
    for title, _deps in specs:
        ids[title] = ledger.add(title).id
    # Second pass to wire deps by title.
    for title, deps in specs:
        task = ledger.get(ids[title])
        task.depends_on = [ids[d] for d in deps]
    return ledger, ids


class TestRunTeam:
    async def test_all_tasks_completed(self) -> None:
        ledger, _ = _make_ledger(("a", []), ("b", []), ("c", []))

        async def runner(member: str, task, mailbox) -> TaskResult:
            return TaskResult(success=True, result=f"{member} did {task.title}")

        report = await run_team(ledger, ["w1", "w2"], teammate_runner=runner)
        assert report.all_done is True
        assert all(t.status == DONE for t in ledger.tasks())

    async def test_dependency_order_enforced(self) -> None:
        ledger, _ = _make_ledger(("setup", []), ("build", ["setup"]), ("test", ["build"]))
        order: list[str] = []

        async def runner(member: str, task, mailbox) -> TaskResult:
            order.append(task.title)
            return TaskResult(success=True)

        report = await run_team(ledger, ["w1", "w2", "w3"], teammate_runner=runner)
        assert report.all_done is True
        assert order.index("setup") < order.index("build") < order.index("test")

    async def test_failed_task_deadlocks_dependents(self) -> None:
        ledger, _ = _make_ledger(("setup", []), ("build", ["setup"]))

        async def runner(member: str, task, mailbox) -> TaskResult:
            if task.title == "setup":
                return TaskResult(success=False, error="setup broke")
            return TaskResult(success=True)

        report = await run_team(ledger, ["w1"], teammate_runner=runner)
        assert report.all_done is False
        assert "deadlock" in report.stopped_reason
        titles = {t.title: t.status for t in ledger.tasks()}
        assert titles["setup"] == FAILED

    async def test_teammate_exception_fails_only_its_task(self) -> None:
        ledger, _ = _make_ledger(("a", []), ("b", []))

        async def runner(member: str, task, mailbox) -> TaskResult:
            if task.title == "a":
                msg = "boom"
                raise RuntimeError(msg)
            return TaskResult(success=True)

        await run_team(ledger, ["w1", "w2"], teammate_runner=runner)
        titles = {t.title: t.status for t in ledger.tasks()}
        assert titles["a"] == FAILED
        assert titles["b"] == DONE

    async def test_spawn_cap_stops_the_team(self) -> None:
        ledger, _ = _make_ledger(("a", []), ("b", []), ("c", []))
        cost_ledger = CostLedger(caps=RunawayCaps(max_subagents=2))

        async def runner(member: str, task, mailbox) -> TaskResult:
            return TaskResult(success=True)

        report = await run_team(ledger, ["w1"], teammate_runner=runner, cost_ledger=cost_ledger)
        assert report.stopped_reason == "spawn cap reached"
        assert sum(1 for t in ledger.tasks() if t.status == DONE) == 2  # only 2 activations allowed
