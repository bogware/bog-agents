"""Tests for the keep-working Stop gate middleware (Tier-1 #3)."""

from __future__ import annotations

from dataclasses import dataclass

from langchain_core.messages import HumanMessage

from bog_agents.middleware.stop_gate import (
    StopContext,
    StopDecision,
    StopGateMiddleware,
    command_stop_check,
)


def _state(continuation: int = 0) -> dict:
    return {"messages": [HumanMessage(content="do the thing")], "_stop_gate_continuations": continuation}


class TestStopGateAfterAgent:
    def test_blocks_and_loops_back_to_model(self) -> None:
        mw = StopGateMiddleware([lambda ctx: StopDecision(block=True, reason="tests failing")])
        out = mw.after_agent(_state(0), runtime=None)  # type: ignore[arg-type]
        assert out is not None
        assert out["jump_to"] == "model"
        assert out["_stop_gate_continuations"] == 1
        msg = out["messages"][0]
        assert isinstance(msg, HumanMessage)
        assert "tests failing" in msg.content

    def test_passing_check_lets_turn_end(self) -> None:
        mw = StopGateMiddleware([lambda ctx: None])
        assert mw.after_agent(_state(0), runtime=None) is None  # type: ignore[arg-type]

    def test_non_blocking_decision_lets_turn_end(self) -> None:
        mw = StopGateMiddleware([lambda ctx: StopDecision(block=False, reason="all good")])
        assert mw.after_agent(_state(0), runtime=None) is None  # type: ignore[arg-type]

    def test_continuation_cap_gives_up(self) -> None:
        mw = StopGateMiddleware([lambda ctx: StopDecision(block=True, reason="x")], max_continuations=3)
        # At the cap, the gate must let the turn end even though the check blocks.
        assert mw.after_agent(_state(3), runtime=None) is None  # type: ignore[arg-type]
        # Just under the cap it still loops.
        assert mw.after_agent(_state(2), runtime=None) is not None  # type: ignore[arg-type]

    def test_raising_check_is_fail_open(self) -> None:
        def _boom(ctx: StopContext) -> StopDecision | None:
            raise RuntimeError("check exploded")

        mw = StopGateMiddleware([_boom])
        # A crashing check must not block the turn or raise.
        assert mw.after_agent(_state(0), runtime=None) is None  # type: ignore[arg-type]

    def test_multiple_reasons_combined(self) -> None:
        mw = StopGateMiddleware(
            [
                lambda ctx: StopDecision(block=True, reason="lint failed"),
                lambda ctx: StopDecision(block=True, reason="tests failed"),
            ]
        )
        out = mw.after_agent(_state(0), runtime=None)  # type: ignore[arg-type]
        body = out["messages"][0].content
        assert "lint failed" in body and "tests failed" in body

    def test_context_sees_continuation_count(self) -> None:
        seen: list[int] = []

        def _record(ctx: StopContext) -> StopDecision | None:
            seen.append(ctx.continuation)
            return None

        StopGateMiddleware([_record]).after_agent(_state(2), runtime=None)  # type: ignore[arg-type]
        assert seen == [2]


class TestCommandStopCheck:
    def test_passing_command_does_not_block(self) -> None:
        @dataclass
        class _Resp:
            exit_code: int
            output: str

        class _Backend:
            def execute(self, command: str, timeout: int = 0) -> _Resp:
                return _Resp(exit_code=0, output="ok")

        check = command_stop_check(_Backend(), "pytest", label="tests")
        assert check(StopContext(messages=[], continuation=0)) is None

    def test_failing_command_blocks_with_output(self) -> None:
        @dataclass
        class _Resp:
            exit_code: int
            output: str

        class _Backend:
            def execute(self, command: str, timeout: int = 0) -> _Resp:
                return _Resp(exit_code=1, output="E   assert 1 == 2")

        check = command_stop_check(_Backend(), "pytest", label="tests")
        decision = check(StopContext(messages=[], continuation=0))
        assert decision is not None
        assert decision.block is True
        assert "tests" in decision.reason
        assert "assert 1 == 2" in decision.reason


def test_middleware_is_lazily_exported() -> None:
    import bog_agents.middleware as mw

    assert mw.StopGateMiddleware is StopGateMiddleware


class TestContinuationBudgetResets:
    """The budget is per-turn, not per-thread."""

    def _gate(self) -> StopGateMiddleware:
        return StopGateMiddleware([lambda ctx: StopDecision(block=True, reason="run the tests")], max_continuations=2)

    def test_before_agent_clears_a_spent_budget(self) -> None:
        gate = self._gate()
        # A previous turn exhausted the budget and checkpointed the counter.
        assert gate.before_agent({"_stop_gate_continuations": 2}, None) == {"_stop_gate_continuations": 0}

    def test_before_agent_is_a_noop_when_unspent(self) -> None:
        gate = self._gate()
        assert gate.before_agent({}, None) is None
        assert gate.before_agent({"_stop_gate_continuations": 0}, None) is None

    def test_gate_enforces_again_on_the_next_turn(self) -> None:
        gate = self._gate()
        state: dict = {"messages": []}
        # Turn one: blocks until the cap, then lets the turn end.
        for _ in range(2):
            update = gate.after_agent(state, None)
            assert update is not None, "gate should still be blocking"
            state.update(update)
        assert gate.after_agent(state, None) is None, "cap reached, turn may end"
        # Turn two: the reset restores enforcement instead of staying disabled.
        state.update(gate.before_agent(state, None) or {})
        assert gate.after_agent(state, None) is not None
