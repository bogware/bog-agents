"""Tests for the expert-rule linter (`/expert lint`)."""

from __future__ import annotations

import textwrap

from bog_agents.middleware.expert_engine import (
    Action,
    ActionKind,
    Pattern,
    Predicate,
    PredicateOp,
    Rule,
    lint,
    render_report,
)
from bog_agents.middleware.expert_engine.loader import load_rules_from_string

_SENTINEL = object()


def _rule(
    name: str,
    *,
    when: object = None,
    then: object = _SENTINEL,
    salience: int = 0,
    once: bool = False,
) -> Rule:
    """Helper that lets tests pass ``then=[]`` to mean *truly empty*.

    The previous implementation used ``then or default`` which conflated
    "not passed" with "passed empty" and broke the no-actions lint test.
    """
    then_value = (
        (Action(kind=ActionKind.AUDIT_LOG, params={}),) if then is _SENTINEL else tuple(then)  # type: ignore[arg-type]
    )
    return Rule(
        name=name,
        when=tuple(when or ()),  # type: ignore[arg-type]
        then=then_value,
        salience=salience,
        once=once,
    )


class TestNoIssues:
    def test_clean_rulebook(self) -> None:
        rules = [
            _rule(
                "block_rm",
                when=[
                    Pattern(
                        fact_type="tool_call",
                        predicates=(Predicate("name", PredicateOp.EQ, "rm"),),
                    )
                ],
                then=[Action(kind=ActionKind.DENY, params={"reason": "no rm"})],
            ),
        ]
        report = lint(rules)
        assert report.ok
        assert report.findings == []


class TestDuplicateName:
    def test_two_rules_same_name(self) -> None:
        rules = [
            _rule("twin", when=[Pattern(fact_type="tool_call")]),
            _rule("twin", when=[Pattern(fact_type="tool_call")]),
        ]
        report = lint(rules)
        codes = [f.code for f in report.findings]
        assert "duplicate-name" in codes


class TestDeadRule:
    def test_unknown_fact_type_flagged(self) -> None:
        rules = [_rule("dead_one", when=[Pattern(fact_type="totally_unknown")])]
        report = lint(rules)
        assert any(f.code == "dead-rule" for f in report.findings)

    def test_engine_fact_type_not_flagged(self) -> None:
        rules = [_rule("alive", when=[Pattern(fact_type="tool_call")])]
        assert all(f.code != "dead-rule" for f in lint(rules).findings)

    def test_produced_fact_type_not_flagged(self) -> None:
        rules = [
            _rule(
                "producer",
                when=[Pattern(fact_type="tool_call")],
                then=[
                    Action(
                        kind=ActionKind.ASSERT_FACT,
                        params={"fact_type": "alert", "data": {}},
                    )
                ],
            ),
            _rule("consumer", when=[Pattern(fact_type="alert")]),
        ]
        assert all(f.code != "dead-rule" for f in lint(rules).findings)


class TestNoActions:
    def test_empty_then_warns(self) -> None:
        rules = [_rule("limp", when=[Pattern(fact_type="tool_call")], then=[])]
        report = lint(rules)
        assert any(f.code == "no-actions" for f in report.findings)


class TestAlwaysFires:
    def test_empty_when_info(self) -> None:
        rules = [_rule("bootstrap")]
        report = lint(rules)
        assert any(f.code == "always-fires" for f in report.findings)


class TestRedundantPredicate:
    def test_two_eq_on_same_field(self) -> None:
        rules = [
            _rule(
                "double_eq",
                when=[
                    Pattern(
                        fact_type="tool_call",
                        predicates=(
                            Predicate("name", PredicateOp.EQ, "shell"),
                            Predicate("name", PredicateOp.EQ, "edit"),
                        ),
                    ),
                ],
            )
        ]
        report = lint(rules)
        assert any(f.code == "redundant-predicate" for f in report.findings)


class TestConflictingActions:
    def test_deny_plus_modify_on_same_fact_types(self) -> None:
        deny_rule = _rule(
            "deny_shell",
            when=[Pattern(fact_type="tool_call")],
            then=[Action(kind=ActionKind.DENY, params={"reason": "no"})],
        )
        modify_rule = _rule(
            "tag_shell",
            when=[Pattern(fact_type="tool_call")],
            then=[Action(kind=ActionKind.MODIFY, params={"timeout": 30})],
        )
        report = lint([deny_rule, modify_rule])
        assert any(f.code == "conflicting-actions" for f in report.findings)


class TestRender:
    def test_render_clean(self) -> None:
        text = render_report(lint([]))
        assert "no issues" in text.lower()

    def test_render_with_findings(self) -> None:
        rules = [_rule("dead_one", when=[Pattern(fact_type="totally_unknown")])]
        text = render_report(lint(rules))
        assert "dead-rule" in text


class TestStarterRulebook:
    """Smoke test: the shipped starter.yaml should lint clean (or at most info)."""

    def test_starter_lints_without_errors(self) -> None:
        starter_yaml = textwrap.dedent(
            """
            - name: block_force_push_to_main
              salience: 100
              when:
                - tool_call:
                    name: shell_execute
                    command:
                      matches: 'git push.*--force.*(main|master)\\b'
              then:
                - deny: "blocked"

            - name: budget_brake_warn
              salience: 90
              once: true
              when:
                - session:
                    cost_usd:
                      gt: 5.0
              then:
                - notify:
                    channel: tui
            """
        )
        rules = load_rules_from_string(starter_yaml)
        report = lint(rules)
        # Starter rules should have no errors. Warnings/infos are OK
        # (e.g. always-fires for bootstrap rules).
        assert report.errors == [], "starter rulebook has lint errors:\n" + render_report(report)
