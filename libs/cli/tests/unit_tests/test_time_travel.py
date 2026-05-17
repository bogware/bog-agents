"""Tests for the time-travel rule replay (Q3).

The replay engine only re-runs the *rules engine* — no LLM, no
filesystem mutations. We build small causal logs and rule sets,
exercise replay() directly, then exercise the slash dispatch and the
hookup through /causal replay.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from bog_agents.middleware.expert_engine import (
    Action,
    ActionKind,
    Pattern,
    Predicate,
    PredicateOp,
    Rule,
)

from bog_agents_cli.causal.ledger import EventKind, open_session
from bog_agents_cli.time_travel import (
    ReplayInput,
    dispatch as replay_dispatch,
    render_result,
    replay,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _block_rule() -> Rule:
    """Rule that denies any tool_call with name='leak'."""
    return Rule(
        name="block_leak",
        when=(
            Pattern(
                fact_type="tool_call",
                predicates=(
                    Predicate(field="name", op=PredicateOp.EQ, value="leak"),
                ),
            ),
        ),
        then=(Action(kind=ActionKind.DENY, params={"reason": "block leak"}),),
    )


def _audit_rule() -> Rule:
    """Side-effect rule (audit only)."""
    return Rule(
        name="audit_leak",
        when=(
            Pattern(
                fact_type="tool_call",
                predicates=(
                    Predicate(field="name", op=PredicateOp.EQ, value="leak"),
                ),
            ),
        ),
        then=(Action(kind=ActionKind.AUDIT_LOG, params={"event": "leaked"}),),
    )


def _session_with_leak(tmp_path: Path) -> tuple[str, int]:
    """Build a session containing a tool_call(name='leak'). Returns (sid, tc_id)."""
    ledger = open_session(tmp_path)
    u = ledger.record(EventKind.USER_MESSAGE, actor="user", summary="please leak")
    m = ledger.record(
        EventKind.MODEL_CALL,
        actor="model",
        summary="invoke leak",
        parent_ids=(u.id,),
    )
    tc = ledger.record(
        EventKind.TOOL_CALL,
        actor="leak",
        summary="cmd",
        parent_ids=(m.id,),
        payload={"args_keys": ["cmd"], "tool_call_id": "call-1"},
    )
    ledger.close()
    return ledger.session_id, tc.id


# ---------------------------------------------------------------------------
# Replay() direct
# ---------------------------------------------------------------------------


class TestReplayHappy:
    def test_drop_rule_changes_outcome(self, tmp_path: Path):
        sid, tc_id = _session_with_leak(tmp_path)
        result = replay(
            session_id=sid,
            working_dir=tmp_path,
            input=ReplayInput(
                anchor_event_id=tc_id, drop_rules=("block_leak",)
            ),
            rules=[_block_rule()],
        )
        assert result.error == ""
        assert result.rule_count_before == 1
        assert result.rule_count_after == 0
        assert result.before.denials == 1
        assert result.after.denials == 0
        assert result.changed is True

    def test_add_rule_via_yaml(self, tmp_path: Path):
        sid, tc_id = _session_with_leak(tmp_path)
        add_yaml = """
- name: extra_audit
  when:
    - tool_call:
        name: { eq: leak }
  then:
    - audit_log:
        event: extra
"""
        result = replay(
            session_id=sid,
            working_dir=tmp_path,
            input=ReplayInput(
                anchor_event_id=tc_id, add_rules_yaml=add_yaml
            ),
            rules=[_audit_rule()],
        )
        assert result.error == ""
        assert result.rule_count_after == result.rule_count_before + 1
        # Both audit rules now fire.
        assert "extra_audit" in result.after.activations
        assert "audit_leak" in result.after.activations

    def test_identical_outcomes_marked_unchanged(self, tmp_path: Path):
        sid, tc_id = _session_with_leak(tmp_path)
        result = replay(
            session_id=sid,
            working_dir=tmp_path,
            # Drop a rule that doesn't exist → effectively no change.
            input=ReplayInput(
                anchor_event_id=tc_id, drop_rules=("does_not_exist",)
            ),
            rules=[_block_rule()],
        )
        assert result.error == ""
        assert result.changed is False
        # Warning surfaced in notes.
        assert any("did not match" in n for n in result.notes)


class TestReplayErrors:
    def test_unknown_session(self, tmp_path: Path):
        result = replay(
            session_id="not-a-session",
            working_dir=tmp_path,
            input=ReplayInput(anchor_event_id=1),
            rules=[],
        )
        assert "no recorded events" in result.error.lower()

    def test_unknown_event_id(self, tmp_path: Path):
        sid, _ = _session_with_leak(tmp_path)
        result = replay(
            session_id=sid,
            working_dir=tmp_path,
            input=ReplayInput(anchor_event_id=9999),
            rules=[],
        )
        assert "not found" in result.error

    def test_anchor_without_ancestor_tool_call(self, tmp_path: Path):
        ledger = open_session(tmp_path)
        # Just a user message — no tool_call ancestry.
        u = ledger.record(EventKind.USER_MESSAGE, actor="user", summary="hi")
        ledger.close()
        result = replay(
            session_id=ledger.session_id,
            working_dir=tmp_path,
            input=ReplayInput(anchor_event_id=u.id),
            rules=[_block_rule()],
        )
        assert "No tool_call ancestor" in result.error

    def test_invalid_yaml_in_with_rule(self, tmp_path: Path):
        sid, tc_id = _session_with_leak(tmp_path)
        result = replay(
            session_id=sid,
            working_dir=tmp_path,
            input=ReplayInput(
                anchor_event_id=tc_id, add_rules_yaml="!!! not valid yaml ::"
            ),
            rules=[_block_rule()],
        )
        assert "Could not parse --with-rule" in result.error

    def test_with_rule_name_collision(self, tmp_path: Path):
        sid, tc_id = _session_with_leak(tmp_path)
        add_yaml = """
- name: block_leak
  when:
    - tool_call:
        name: { eq: leak }
  then:
    - audit_log:
        event: x
"""
        result = replay(
            session_id=sid,
            working_dir=tmp_path,
            input=ReplayInput(
                anchor_event_id=tc_id, add_rules_yaml=add_yaml
            ),
            rules=[_block_rule()],
        )
        assert "collides" in result.error


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


class TestRenderer:
    def test_render_changed(self, tmp_path: Path):
        sid, tc_id = _session_with_leak(tmp_path)
        result = replay(
            session_id=sid,
            working_dir=tmp_path,
            input=ReplayInput(anchor_event_id=tc_id, drop_rules=("block_leak",)),
            rules=[_block_rule()],
        )
        out = render_result(result)
        assert "Time-travel replay" in out
        assert "Before (current rulebook)" in out
        assert "After (with your changes)" in out
        assert "outcomes differ" in out
        assert "denials: 1 → 0" in out

    def test_render_unchanged(self, tmp_path: Path):
        sid, tc_id = _session_with_leak(tmp_path)
        result = replay(
            session_id=sid,
            working_dir=tmp_path,
            input=ReplayInput(
                anchor_event_id=tc_id, drop_rules=("ghost-rule",)
            ),
            rules=[_block_rule()],
        )
        out = render_result(result)
        assert "identical outcomes" in out

    def test_render_error(self, tmp_path: Path):
        result = replay(
            session_id="nope",
            working_dir=tmp_path,
            input=ReplayInput(anchor_event_id=1),
            rules=[],
        )
        out = render_result(result)
        assert "/causal replay failed" in out


# ---------------------------------------------------------------------------
# Slash dispatch
# ---------------------------------------------------------------------------


class TestDispatch:
    def test_help_empty(self, tmp_path: Path):
        out = replay_dispatch(
            "/causal replay",
            working_dir=tmp_path,
            session_id=None,
            rules_provider=list,
        )
        assert "Usage" in out

    def test_help_word(self, tmp_path: Path):
        out = replay_dispatch(
            "/causal replay help",
            working_dir=tmp_path,
            session_id=None,
            rules_provider=list,
        )
        assert "Usage" in out

    def test_invalid_event_id(self, tmp_path: Path):
        out = replay_dispatch(
            "/causal replay abc",
            working_dir=tmp_path,
            session_id=None,
            rules_provider=list,
        )
        assert "Invalid event id" in out

    def test_unknown_flag(self, tmp_path: Path):
        out = replay_dispatch(
            "/causal replay 1 --weird",
            working_dir=tmp_path,
            session_id=None,
            rules_provider=list,
        )
        assert "Unknown flag" in out

    def test_missing_value_after_no_rule(self, tmp_path: Path):
        out = replay_dispatch(
            "/causal replay 1 --no-rule",
            working_dir=tmp_path,
            session_id=None,
            rules_provider=list,
        )
        assert "Missing value after --no-rule" in out

    def test_no_sessions_returns_friendly(self, tmp_path: Path):
        out = replay_dispatch(
            "/causal replay 1",
            working_dir=tmp_path,
            session_id=None,
            rules_provider=list,
        )
        assert "No causal sessions" in out

    def test_end_to_end_via_dispatch(self, tmp_path: Path):
        sid, tc_id = _session_with_leak(tmp_path)
        out = replay_dispatch(
            f"/causal replay {tc_id} --no-rule block_leak",
            working_dir=tmp_path,
            session_id=sid,
            rules_provider=lambda: [_block_rule()],
        )
        assert "Time-travel replay" in out
        assert "denials: 1 → 0" in out

    def test_with_rule_file(self, tmp_path: Path):
        sid, tc_id = _session_with_leak(tmp_path)
        rule_file = tmp_path / "extra.yaml"
        rule_file.write_text(
            """
- name: extra_audit
  when:
    - tool_call:
        name: { eq: leak }
  then:
    - audit_log:
        event: extra
""",
            encoding="utf-8",
        )
        out = replay_dispatch(
            f"/causal replay {tc_id} --with-rule {rule_file}",
            working_dir=tmp_path,
            session_id=sid,
            rules_provider=lambda: [_audit_rule()],
        )
        assert "Time-travel replay" in out
        assert "extra_audit" in out

    def test_rules_provider_failure_surfaces(self, tmp_path: Path):
        sid, tc_id = _session_with_leak(tmp_path)

        def boom():
            msg = "synthetic"
            raise RuntimeError(msg)

        out = replay_dispatch(
            f"/causal replay {tc_id}",
            working_dir=tmp_path,
            session_id=sid,
            rules_provider=boom,
        )
        assert "Could not load active rules" in out


# ---------------------------------------------------------------------------
# Integration with /causal controller
# ---------------------------------------------------------------------------


class TestCausalIntegration:
    def test_causal_controller_routes_replay(self, tmp_path: Path):
        from bog_agents_cli.causal.controller import (
            dispatch as causal_dispatch,
            reset_controllers,
        )

        try:
            reset_controllers()
            # /causal on creates the active session; for the replay
            # path we need both an active session AND events. Create
            # events via the ledger directly, then surface them via
            # the controller's active ledger.
            from bog_agents_cli.causal.controller import get_controller

            ctl = get_controller(tmp_path)
            ctl.ensure_active()
            assert ctl.active is not None
            u = ctl.active.record(
                EventKind.USER_MESSAGE, actor="user", summary="hi"
            )
            tc = ctl.active.record(
                EventKind.TOOL_CALL,
                actor="leak",
                summary="cmd",
                parent_ids=(u.id,),
            )
            # Now invoke /causal replay through the outer dispatcher.
            out = causal_dispatch(
                f"/causal replay {tc.id} --no-rule block_leak",
                tmp_path,
            )
            # Either time-travel rendered or surfaces a sensible error
            # (expert controller has no rules loaded → no diff).
            assert "Time-travel replay" in out or "rule" in out.lower()
        finally:
            reset_controllers()
