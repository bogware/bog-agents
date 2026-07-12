"""Tests for ``ExpertRulesMiddleware`` — the integration layer between
the rule engine and bog-agents tool-call interception.
"""

from __future__ import annotations

import asyncio
import json
import textwrap
from typing import TYPE_CHECKING, Any

from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest

if TYPE_CHECKING:
    from pathlib import Path

from bog_agents.middleware.expert_engine import (
    Action,
    ActionKind,
    Fact,
    Pattern,
    Predicate,
    PredicateOp,
    Rule,
)
from bog_agents.middleware.expert_rules import ExpertRulesMiddleware

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_request(name: str, args: dict[str, Any], call_id: str = "call-1") -> ToolCallRequest:
    return ToolCallRequest(
        tool_call={"name": name, "args": args, "id": call_id, "type": "tool_call"},
        tool=None,
        state={},
        runtime=None,
    )


def _write_rule_file(directory: Path, name: str, text: str) -> Path:
    path = directory / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Sync wrap_tool_call
# ---------------------------------------------------------------------------


class TestSyncInterception:
    def test_pass_through_when_no_rules(self, tmp_path: Path) -> None:
        mw = ExpertRulesMiddleware(working_dir=tmp_path)
        request = _make_request("shell", {"command": "ls"})
        called: list[ToolCallRequest] = []

        def handler(r: ToolCallRequest) -> str:
            called.append(r)
            return "ran"

        result = mw.wrap_tool_call(request, handler)
        assert result == "ran"
        assert called and called[0] is request

    def test_deny_blocks_handler(self, tmp_path: Path) -> None:
        rules_dir = tmp_path / ".bog-agents" / "expert_rules"
        _write_rule_file(
            rules_dir,
            "block.yaml",
            """
            - name: block_rm
              when:
                - tool_call:
                    name: shell
                    command:
                      matches: '^rm '
              then:
                - deny: "rm is disallowed"
            """,
        )
        mw = ExpertRulesMiddleware(working_dir=tmp_path, reload_interval=0)
        request = _make_request("shell", {"command": "rm -rf /tmp/x"})
        called: list[ToolCallRequest] = []

        def handler(r: ToolCallRequest) -> str:
            called.append(r)
            return "ran"

        result = mw.wrap_tool_call(request, handler)
        assert called == []  # handler must not run
        assert isinstance(result, ToolMessage)
        payload = json.loads(result.content)
        assert payload["expert_rules"] == "deny"
        assert "rm is disallowed" in payload["reasons"]
        assert mw.counters["denials"] == 1

    def test_modify_rewrites_args(self, tmp_path: Path) -> None:
        rules_dir = tmp_path / ".bog-agents" / "expert_rules"
        _write_rule_file(
            rules_dir,
            "modify.yaml",
            """
            - name: clamp_timeout
              when:
                - tool_call:
                    name: shell
              then:
                - modify:
                    timeout: 30
            """,
        )
        mw = ExpertRulesMiddleware(working_dir=tmp_path, reload_interval=0)
        request = _make_request("shell", {"command": "echo hi", "timeout": 9999})
        seen: list[dict[str, Any]] = []

        def handler(r: ToolCallRequest) -> str:
            seen.append(dict(r.tool_call["args"]))
            return "ok"

        result = mw.wrap_tool_call(request, handler)
        assert result == "ok"
        assert seen[0]["timeout"] == 30
        assert seen[0]["command"] == "echo hi"  # other args preserved
        assert mw.counters["modifications"] == 1

    def test_approval_required_blocks(self, tmp_path: Path) -> None:
        rules_dir = tmp_path / ".bog-agents" / "expert_rules"
        _write_rule_file(
            rules_dir,
            "approve.yaml",
            """
            - name: prod_gate
              when:
                - tool_call:
                    name: deploy
              then:
                - require_approval:
                    gate: "prod-deploy"
                    risk: high
            """,
        )
        captured: list[dict[str, Any]] = []
        mw = ExpertRulesMiddleware(
            working_dir=tmp_path,
            reload_interval=0,
            on_approval_required=captured.append,
        )
        request = _make_request("deploy", {"env": "prod"})

        def handler(_: ToolCallRequest) -> str:
            raise AssertionError("handler should not run")

        result = mw.wrap_tool_call(request, handler)
        assert isinstance(result, ToolMessage)
        payload = json.loads(result.content)
        assert payload["expert_rules"] == "approval_required"
        assert payload["gates"][0]["gate"] == "prod-deploy"
        assert captured and captured[0]["gate"] == "prod-deploy"
        assert mw.counters["approvals"] == 1

    def test_disabled_passes_through(self, tmp_path: Path) -> None:
        rules_dir = tmp_path / ".bog-agents" / "expert_rules"
        _write_rule_file(
            rules_dir,
            "block.yaml",
            """
            - name: block_all
              when:
                - tool_call: {}
              then:
                - deny: "blocked"
            """,
        )
        mw = ExpertRulesMiddleware(working_dir=tmp_path, reload_interval=0, enabled=False)
        result = mw.wrap_tool_call(_make_request("anything", {}), lambda _r: "ran")
        assert result == "ran"
        assert mw.counters["denials"] == 0
        # Toggle on dynamically
        mw.set_enabled(True)
        result2 = mw.wrap_tool_call(_make_request("anything", {}), lambda _r: "ran")
        assert isinstance(result2, ToolMessage)

    def test_request_does_not_persist_in_memory_after_call(self, tmp_path: Path) -> None:
        mw = ExpertRulesMiddleware(working_dir=tmp_path)
        request = _make_request("shell", {"command": "ls"})
        mw.wrap_tool_call(request, lambda _r: "ok")
        assert mw.engine.memory.stats().get("tool_call", 0) == 0

    def test_broken_yaml_keeps_old_rules_live(self, tmp_path: Path) -> None:
        rules_dir = tmp_path / ".bog-agents" / "expert_rules"
        good = _write_rule_file(
            rules_dir,
            "block.yaml",
            """
            - name: block_a
              when:
                - tool_call:
                    name: a
              then:
                - deny: "no a"
            """,
        )
        mw = ExpertRulesMiddleware(working_dir=tmp_path, reload_interval=0)
        assert len(mw.engine.rules) == 1
        # Now break it.
        good.write_text("this is :: not yaml [\n", encoding="utf-8")
        count, err = mw.reload()
        # Reload should report the error but engine keeps the old rule alive.
        assert err  # non-empty error
        assert count == 1  # still has the prior rule

    def test_extra_rules_are_additive(self, tmp_path: Path) -> None:
        rule = Rule(
            name="programmatic",
            when=(
                Pattern(
                    fact_type="tool_call",
                    predicates=(Predicate("name", PredicateOp.EQ, "shell"),),
                ),
            ),
            then=(Action(kind=ActionKind.DENY, params={"reason": "no shell"}),),
        )
        mw = ExpertRulesMiddleware(working_dir=tmp_path, extra_rules=[rule], reload_interval=0)
        result = mw.wrap_tool_call(_make_request("shell", {}), lambda _r: "ran")
        assert isinstance(result, ToolMessage)


# ---------------------------------------------------------------------------
# Async wrap_tool_call — pytest-asyncio is configured asyncio_mode=auto
# ---------------------------------------------------------------------------


class TestAsyncInterception:
    async def test_async_deny(self, tmp_path: Path) -> None:
        rules_dir = tmp_path / ".bog-agents" / "expert_rules"
        _write_rule_file(
            rules_dir,
            "block.yaml",
            """
            - name: block_x
              when:
                - tool_call:
                    name: x
              then:
                - deny: "blocked"
            """,
        )
        mw = ExpertRulesMiddleware(working_dir=tmp_path, reload_interval=0)
        request = _make_request("x", {})

        async def handler(_: ToolCallRequest) -> str:
            raise AssertionError("must not be called")

        result = await mw.awrap_tool_call(request, handler)
        assert isinstance(result, ToolMessage)

    async def test_async_modify(self, tmp_path: Path) -> None:
        rules_dir = tmp_path / ".bog-agents" / "expert_rules"
        _write_rule_file(
            rules_dir,
            "modify.yaml",
            """
            - name: clamp
              when:
                - tool_call: {}
              then:
                - modify:
                    safe_mode: true
            """,
        )
        mw = ExpertRulesMiddleware(working_dir=tmp_path, reload_interval=0)
        seen: list[dict[str, Any]] = []

        async def handler(r: ToolCallRequest) -> str:
            seen.append(dict(r.tool_call["args"]))
            return "ok"

        await mw.awrap_tool_call(_make_request("shell", {"command": "echo"}), handler)
        assert seen[0]["safe_mode"] is True
        assert seen[0]["command"] == "echo"

    async def test_concurrent_calls_do_not_corrupt_memory(self, tmp_path: Path) -> None:
        """A burst of concurrent tool calls must all run cleanly."""
        rules_dir = tmp_path / ".bog-agents" / "expert_rules"
        _write_rule_file(
            rules_dir,
            "tag.yaml",
            """
            - name: tag
              when:
                - tool_call: {}
              then:
                - modify:
                    audited: true
            """,
        )
        mw = ExpertRulesMiddleware(working_dir=tmp_path, reload_interval=0)
        seen: list[dict[str, Any]] = []

        async def handler(r: ToolCallRequest) -> str:
            seen.append(dict(r.tool_call["args"]))
            return "ok"

        await asyncio.gather(*(mw.awrap_tool_call(_make_request("x", {"i": i}, call_id=f"c{i}"), handler) for i in range(10)))
        # Engine retracts each tool_call fact after its run; memory should be clean.
        assert mw.engine.memory.stats().get("tool_call", 0) == 0
        assert len(seen) == 10
        assert all(s["audited"] is True for s in seen)


# ---------------------------------------------------------------------------
# Backward-chain helpers exposed by the middleware
# ---------------------------------------------------------------------------


class TestExplain:
    def test_explain_with_loaded_rule(self, tmp_path: Path) -> None:
        rules_dir = tmp_path / ".bog-agents" / "expert_rules"
        _write_rule_file(
            rules_dir,
            "block.yaml",
            """
            - name: block_rm
              when:
                - tool_call:
                    command:
                      matches: '^rm '
              then:
                - deny: "no rm"
            """,
        )
        mw = ExpertRulesMiddleware(working_dir=tmp_path, reload_interval=0)
        # Make the fact world look real so explain has something to walk.
        mw.engine.assert_fact(Fact(fact_type="tool_call", data={"name": "shell", "command": "rm -rf /tmp/x"}))
        tree = mw.explain(Pattern(fact_type="tool_call"))
        assert tree["root"]["proven"] is True

    def test_last_trace_after_run(self, tmp_path: Path) -> None:
        mw = ExpertRulesMiddleware(working_dir=tmp_path)
        mw.wrap_tool_call(_make_request("anything", {}), lambda _r: "ok")
        trace = mw.last_trace()
        # No rules loaded → trace may be empty, but list-shape must hold.
        assert isinstance(trace, list)


# ---------------------------------------------------------------------------
# Wave 5: ApprovalStore integration
# ---------------------------------------------------------------------------


class TestApprovalStoreIntegration:
    """When an :class:`ApprovalStore` is passed in, ``require_approval``
    actions create real submissions on the store (auto-creating gates).
    """

    def test_submission_created_on_approval_action(self, tmp_path: Path) -> None:
        from bog_agents.middleware.approval_gates import ApprovalStore

        rules_dir = tmp_path / ".bog-agents" / "expert_rules"
        _write_rule_file(
            rules_dir,
            "approve.yaml",
            """
            - name: prod_gate
              when:
                - tool_call:
                    name: deploy
              then:
                - require_approval:
                    gate: "prod-deploy"
                    risk: high
                    reason: "Deploying to prod requires sign-off"
            """,
        )
        store = ApprovalStore()
        mw = ExpertRulesMiddleware(
            working_dir=tmp_path,
            reload_interval=0,
            approval_store=store,
        )
        mw.wrap_tool_call(_make_request("deploy", {"env": "prod"}), lambda _r: "")
        assert "prod-deploy" in store.gates
        assert store.gates["prod-deploy"].description.startswith("Deploying to prod")
        assert len(store.submissions) == 1
        sub = store.submissions[0]
        assert sub.gate_name == "prod-deploy"
        assert sub.risk_level == "high"

    def test_existing_gate_is_reused(self, tmp_path: Path) -> None:
        from bog_agents.middleware.approval_gates import ApprovalStore

        rules_dir = tmp_path / ".bog-agents" / "expert_rules"
        _write_rule_file(
            rules_dir,
            "approve.yaml",
            """
            - name: gate
              when:
                - tool_call:
                    name: x
              then:
                - require_approval:
                    gate: "existing"
                    risk: low
            """,
        )
        store = ApprovalStore()
        store.create_gate("existing", required_approvers=2, description="manual")
        mw = ExpertRulesMiddleware(
            working_dir=tmp_path,
            reload_interval=0,
            approval_store=store,
        )
        mw.wrap_tool_call(_make_request("x", {}), lambda _r: "")
        # The existing gate is kept, not overwritten.
        assert store.gates["existing"].required_approvers == 2
        assert store.gates["existing"].description == "manual"
        assert len(store.submissions) == 1


# ---------------------------------------------------------------------------
# Working-memory soft cap wiring (P17)
# ---------------------------------------------------------------------------


class TestWorkingMemoryCapWiring:
    """The middleware bounds derived-fact growth without breaking semantics.

    The per-call ``tool_call`` fact must still be retracted each turn, and
    cross-call ``assert_fact``-derived facts must accumulate (so rate-limit /
    cumulative-cost rulebooks keep working) — but only up to the configured
    ``max_working_facts`` cap, past which the oldest derived facts are evicted.
    """

    def test_tool_call_fact_retracted_each_turn(self, tmp_path: Path) -> None:
        mw = ExpertRulesMiddleware(working_dir=tmp_path, reload_interval=0)
        for _ in range(5):
            mw.wrap_tool_call(_make_request("shell", {"command": "ls"}), lambda _r: "ok")
        # The structural tool_call fact is asserted then retracted in the
        # finally of _run_for_request, so memory holds none between calls.
        assert mw.engine.memory.stats().get("tool_call", 0) == 0

    def test_derived_facts_accumulate_then_bound(self, tmp_path: Path) -> None:
        rules_dir = tmp_path / ".bog-agents" / "expert_rules"
        _write_rule_file(
            rules_dir,
            "accumulate.yaml",
            """
            - name: record_call
              when:
                - tool_call:
                    name: shell
              then:
                - assert_fact:
                    fact_type: call_seen
                    data:
                      marker: 1
            """,
        )
        # cap=3, eviction ceiling=6: drive enough calls to exceed it.
        mw = ExpertRulesMiddleware(working_dir=tmp_path, reload_interval=0, max_working_facts=3)
        for _ in range(20):
            mw.wrap_tool_call(_make_request("shell", {"command": "ls"}), lambda _r: "ok")
        # Cross-call derived facts accumulated (semantics intact) but the
        # shared memory stayed bounded rather than leaking 20 facts.
        derived = mw.engine.memory.stats().get("call_seen", 0)
        assert 0 < derived <= 6
        # And tool_call is still never resident between calls.
        assert mw.engine.memory.stats().get("tool_call", 0) == 0
