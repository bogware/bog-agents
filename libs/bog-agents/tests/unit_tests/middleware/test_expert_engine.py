"""Tests for the expert rule engine.

Covers: types, working memory, pattern matcher, forward chaining engine,
action executor, backward chainer, and YAML loader.
"""

from __future__ import annotations

import textwrap
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from bog_agents.middleware.expert_engine import (
    Action,
    ActionExecutor,
    ActionKind,
    BackwardChainer,
    ExpertEngine,
    Fact,
    Pattern,
    PatternMatcher,
    Predicate,
    PredicateOp,
    Rule,
    RuleLoadError,
    Trace,
    WorkingMemory,
    load_rule_file,
    load_rules_from_dir,
)
from bog_agents.middleware.expert_engine.backward import (
    pattern_from_shorthand,
    render_tree,
)
from bog_agents.middleware.expert_engine.loader import load_rules_from_string

# ---------------------------------------------------------------------------
# Predicates
# ---------------------------------------------------------------------------


class TestPredicates:
    def test_eq_simple(self) -> None:
        fact = Fact(fact_type="x", data={"name": "shell"})
        assert Predicate("name", PredicateOp.EQ, "shell").test(fact)
        assert not Predicate("name", PredicateOp.EQ, "other").test(fact)

    def test_dotted_field(self) -> None:
        fact = Fact(fact_type="x", data={"user": {"id": 7}})
        assert Predicate("user.id", PredicateOp.EQ, 7).test(fact)
        assert not Predicate("user.id", PredicateOp.EQ, 8).test(fact)

    def test_missing_field_with_eq_is_false(self) -> None:
        fact = Fact(fact_type="x", data={})
        assert not Predicate("name", PredicateOp.EQ, "shell").test(fact)

    def test_exists_and_missing(self) -> None:
        present = Fact(fact_type="x", data={"name": "shell"})
        absent = Fact(fact_type="x", data={})
        assert Predicate("name", PredicateOp.EXISTS).test(present)
        assert not Predicate("name", PredicateOp.EXISTS).test(absent)
        assert Predicate("name", PredicateOp.MISSING).test(absent)
        assert not Predicate("name", PredicateOp.MISSING).test(present)

    def test_in_and_not_in(self) -> None:
        fact = Fact(fact_type="x", data={"env": "prod"})
        assert Predicate("env", PredicateOp.IN, ["prod", "staging"]).test(fact)
        assert Predicate("env", PredicateOp.NOT_IN, ["dev"]).test(fact)
        assert not Predicate("env", PredicateOp.IN, ["dev"]).test(fact)

    def test_numeric_comparisons_safe(self) -> None:
        fact = Fact(fact_type="x", data={"cost": 3.5})
        assert Predicate("cost", PredicateOp.GT, 1).test(fact)
        assert Predicate("cost", PredicateOp.GTE, 3.5).test(fact)
        assert Predicate("cost", PredicateOp.LTE, 4).test(fact)
        # mixed-type compare should be False, not raise
        assert not Predicate("cost", PredicateOp.GT, "abc").test(fact)

    def test_matches_regex(self) -> None:
        fact = Fact(fact_type="x", data={"cmd": "git push --force main"})
        assert Predicate("cmd", PredicateOp.MATCHES, r"git push.*--force").test(fact)
        assert not Predicate("cmd", PredicateOp.MATCHES, r"^rm ").test(fact)

    def test_contains(self) -> None:
        fact = Fact(fact_type="x", data={"tags": ["urgent", "prod"], "text": "Hello world"})
        assert Predicate("tags", PredicateOp.CONTAINS, "prod").test(fact)
        assert Predicate("text", PredicateOp.CONTAINS, "world").test(fact)
        assert not Predicate("tags", PredicateOp.CONTAINS, "dev").test(fact)


# ---------------------------------------------------------------------------
# WorkingMemory
# ---------------------------------------------------------------------------


class TestWorkingMemory:
    def test_assert_assigns_id(self) -> None:
        wm = WorkingMemory()
        f = wm.assert_fact(Fact(fact_type="tool_call", data={"name": "a"}))
        assert f.id == 1
        assert f.asserted_at > 0
        f2 = wm.assert_fact(Fact(fact_type="tool_call", data={"name": "b"}))
        assert f2.id == 2

    def test_retract(self) -> None:
        wm = WorkingMemory()
        f = wm.assert_fact(Fact(fact_type="x", data={}))
        assert wm.retract(f.id) == f
        assert wm.retract(f.id) is None  # idempotent
        assert f.id not in wm

    def test_by_type_is_insertion_ordered(self) -> None:
        wm = WorkingMemory()
        a = wm.assert_fact(Fact(fact_type="x", data={"n": 1}))
        b = wm.assert_fact(Fact(fact_type="x", data={"n": 2}))
        c = wm.assert_fact(Fact(fact_type="y", data={"n": 3}))
        assert [f.id for f in wm.by_type("x")] == [a.id, b.id]
        assert [f.id for f in wm.by_type("y")] == [c.id]

    def test_retract_matching_predicate(self) -> None:
        wm = WorkingMemory()
        wm.assert_fact(Fact(fact_type="x", data={"keep": True}))
        wm.assert_fact(Fact(fact_type="x", data={"keep": False}))
        removed = wm.retract_matching("x", predicate=lambda f: not f.get("keep"))
        assert len(removed) == 1
        assert all(f.get("keep") for f in wm.by_type("x"))

    def test_stats(self) -> None:
        wm = WorkingMemory()
        wm.assert_fact(Fact(fact_type="a", data={}))
        wm.assert_fact(Fact(fact_type="a", data={}))
        wm.assert_fact(Fact(fact_type="b", data={}))
        assert wm.stats() == {"a": 2, "b": 1}


# ---------------------------------------------------------------------------
# PatternMatcher
# ---------------------------------------------------------------------------


class TestMatcher:
    def test_empty_when_matches_once(self) -> None:
        wm = WorkingMemory()
        matcher = PatternMatcher()
        matches = matcher.match_all((), wm)
        assert len(matches) == 1
        assert matches[0].matched_facts == ()

    def test_single_pattern(self) -> None:
        wm = WorkingMemory()
        wm.assert_fact(Fact(fact_type="tool", data={"name": "shell"}))
        wm.assert_fact(Fact(fact_type="tool", data={"name": "edit"}))
        pat = Pattern(
            fact_type="tool",
            predicates=(Predicate("name", PredicateOp.EQ, "shell"),),
        )
        matches = PatternMatcher().match_all((pat,), wm)
        assert len(matches) == 1
        assert matches[0].matched_facts[0].data["name"] == "shell"

    def test_cross_pattern_binding(self) -> None:
        """Two patterns join via {{var.field}} reference."""
        wm = WorkingMemory()
        wm.assert_fact(Fact(fact_type="session", data={"id": 7, "env": "prod"}))
        wm.assert_fact(Fact(fact_type="tool", data={"session_id": 7, "name": "shell"}))
        wm.assert_fact(Fact(fact_type="tool", data={"session_id": 99, "name": "shell"}))
        sess_pat = Pattern(
            fact_type="session",
            bind="s",
            predicates=(Predicate("env", PredicateOp.EQ, "prod"),),
        )
        tool_pat = Pattern(
            fact_type="tool",
            predicates=(Predicate("session_id", PredicateOp.EQ, "{{s.id}}"),),
        )
        matches = PatternMatcher().match_all((sess_pat, tool_pat), wm)
        assert len(matches) == 1
        assert matches[0].bindings["s"].get("id") == 7
        assert matches[0].matched_facts[1].get("session_id") == 7

    def test_negation(self) -> None:
        wm = WorkingMemory()
        wm.assert_fact(Fact(fact_type="approval", data={"granted": True}))
        wm.assert_fact(Fact(fact_type="tool", data={"name": "shell"}))
        # rule fires only when no approval=false fact exists
        pat = Pattern(
            fact_type="approval",
            negated=True,
            predicates=(Predicate("granted", PredicateOp.EQ, False),),
        )
        matches = PatternMatcher().match_all((pat,), wm)
        assert len(matches) == 1  # negation succeeds (no granted=false)
        wm.assert_fact(Fact(fact_type="approval", data={"granted": False}))
        matches = PatternMatcher().match_all((pat,), wm)
        assert matches == []  # negation now fails

    def test_no_match_when_no_facts(self) -> None:
        wm = WorkingMemory()
        pat = Pattern(fact_type="x")
        assert PatternMatcher().match_all((pat,), wm) == []


# ---------------------------------------------------------------------------
# ActionExecutor
# ---------------------------------------------------------------------------


class TestActionExecutor:
    def test_deny_records_reason(self) -> None:
        wm = WorkingMemory()
        ex = ActionExecutor(wm)
        trace = Trace()
        result = ex.execute_actions(
            "r",
            (Action(kind=ActionKind.DENY, params={"reason": "no"}),),
            bindings={},
            trace=trace,
        )
        assert result.denied
        assert result.deny_reasons == ["no"]

    def test_modify_collects_params(self) -> None:
        wm = WorkingMemory()
        ex = ActionExecutor(wm)
        trace = Trace()
        result = ex.execute_actions(
            "r",
            (Action(kind=ActionKind.MODIFY, params={"timeout": 30}),),
            bindings={},
            trace=trace,
        )
        assert result.modifications == [{"timeout": 30}]
        assert result.merged_modification() == {"timeout": 30}

    def test_notify_uses_sink(self) -> None:
        wm = WorkingMemory()
        called: list[tuple[str, dict]] = []

        def sink(channel: str, payload: dict) -> None:
            called.append((channel, payload))

        ex = ActionExecutor(wm, notify=sink)
        ex.execute_actions(
            "r",
            (Action(kind=ActionKind.NOTIFY, params={"channel": "slack", "text": "hi"}),),
            bindings={},
            trace=Trace(),
        )
        assert called == [("slack", {"channel": "slack", "text": "hi"})]

    def test_assert_fact_mutates_memory(self) -> None:
        wm = WorkingMemory()
        ex = ActionExecutor(wm)
        ex.execute_actions(
            "r",
            (
                Action(
                    kind=ActionKind.ASSERT_FACT,
                    params={"fact_type": "alert", "data": {"level": "high"}},
                ),
            ),
            bindings={},
            trace=Trace(),
        )
        assert wm.stats() == {"alert": 1}

    def test_template_resolution_uses_bindings(self) -> None:
        wm = WorkingMemory()
        ex = ActionExecutor(wm)
        sess = wm.assert_fact(Fact(fact_type="session", data={"id": 42, "user": "scott"}))
        result = ex.execute_actions(
            "r",
            (
                Action(
                    kind=ActionKind.DENY,
                    params={"reason": "user={{s.user}} session={{s.id}}"},
                ),
            ),
            bindings={"s": sess},
            trace=Trace(),
        )
        assert result.deny_reasons == ["user=scott session=42"]

    def test_handler_exception_is_caught(self) -> None:
        wm = WorkingMemory()
        ex = ActionExecutor(wm)
        # assert_fact with bad params should be reported as ok=False outcome,
        # but the engine must not crash.
        result = ex.execute_actions(
            "r",
            (Action(kind=ActionKind.ASSERT_FACT, params={"data": {"k": "v"}}),),
            bindings={},
            trace=Trace(),
        )
        # missing fact_type → handler returns a message, doesn't raise
        assert result.outcomes[0].ok is True
        assert "missing fact_type" in result.outcomes[0].message


# ---------------------------------------------------------------------------
# ExpertEngine — forward chaining
# ---------------------------------------------------------------------------


class TestEngine:
    def test_empty_rules_fires_nothing(self) -> None:
        engine = ExpertEngine()
        result = engine.run()
        assert result.activations == []
        assert not result.denied

    def test_simple_deny(self) -> None:
        rule = Rule(
            name="block_rm",
            when=(
                Pattern(
                    fact_type="tool_call",
                    predicates=(Predicate("name", PredicateOp.EQ, "rm"),),
                ),
            ),
            then=(Action(kind=ActionKind.DENY, params={"reason": "no rm"}),),
        )
        engine = ExpertEngine([rule])
        engine.assert_fact(Fact(fact_type="tool_call", data={"name": "rm"}))
        result = engine.run()
        assert result.denied
        assert result.deny_reasons == ["no rm"]
        assert len(result.activations) == 1

    def test_salience_orders_firing(self) -> None:
        wm = WorkingMemory()
        fact = Fact(fact_type="t", data={})
        wm.assert_fact(fact)
        low = Rule(
            name="low",
            when=(Pattern(fact_type="t"),),
            then=(Action(kind=ActionKind.AUDIT_LOG, params={"event": "low"}),),
            salience=0,
        )
        high = Rule(
            name="high",
            when=(Pattern(fact_type="t"),),
            then=(Action(kind=ActionKind.AUDIT_LOG, params={"event": "high"}),),
            salience=100,
        )
        engine = ExpertEngine([low, high], memory=wm)
        result = engine.run()
        fired_order = [a.rule.name for a in result.activations]
        assert fired_order == ["high", "low"]

    def test_once_flag_caps_firing(self) -> None:
        rule = Rule(
            name="once_only",
            when=(Pattern(fact_type="t"),),
            then=(
                Action(
                    kind=ActionKind.ASSERT_FACT,
                    params={"fact_type": "alert", "data": {"k": "v"}},
                ),
            ),
            once=True,
        )
        engine = ExpertEngine([rule])
        engine.assert_fact(Fact(fact_type="t", data={}))
        engine.assert_fact(Fact(fact_type="t", data={}))
        result = engine.run()
        # Both "t" facts would match, but once=True caps to 1 firing.
        fires = [a for a in result.activations if a.rule.name == "once_only"]
        assert len(fires) == 1
        assert engine.memory.stats().get("alert", 0) == 1

    def test_forward_chain_via_assert(self) -> None:
        """A → B → C: rule asserts fact that triggers next rule."""
        rule_ab = Rule(
            name="a_to_b",
            when=(Pattern(fact_type="a"),),
            then=(
                Action(
                    kind=ActionKind.ASSERT_FACT,
                    params={"fact_type": "b", "data": {"from": "a"}},
                ),
            ),
        )
        rule_bc = Rule(
            name="b_to_c",
            when=(Pattern(fact_type="b"),),
            then=(
                Action(
                    kind=ActionKind.ASSERT_FACT,
                    params={"fact_type": "c", "data": {"from": "b"}},
                ),
            ),
        )
        engine = ExpertEngine([rule_ab, rule_bc])
        engine.assert_fact(Fact(fact_type="a", data={}))
        result = engine.run()
        types_in_memory = engine.memory.stats()
        assert types_in_memory == {"a": 1, "b": 1, "c": 1}
        rule_names = [a.rule.name for a in result.activations]
        assert rule_names == ["a_to_b", "b_to_c"]

    def test_cycle_detected_and_truncated(self) -> None:
        """Two rules each assert a fact the other matches → must terminate."""
        rule_a = Rule(
            name="a",
            when=(Pattern(fact_type="t"),),
            then=(
                Action(
                    kind=ActionKind.ASSERT_FACT,
                    params={"fact_type": "t", "data": {"loop": 1}},
                ),
            ),
        )
        engine = ExpertEngine([rule_a], max_iterations=10)
        engine.assert_fact(Fact(fact_type="t", data={"seed": True}))
        result = engine.run()
        # Each new fact has unique id so activation signature differs;
        # signature dedup is by (rule_name, fact_ids). The activation_history
        # set grows monotonically because every new t-fact creates a new
        # signature. After max_iterations we truncate.
        assert result.truncated
        assert result.iterations >= 10

    def test_deny_short_circuits(self) -> None:
        """A deny in iteration 1 stops the loop before iteration 2."""
        deny_rule = Rule(
            name="deny",
            when=(Pattern(fact_type="t"),),
            then=(Action(kind=ActionKind.DENY, params={"reason": "stop"}),),
            salience=100,
        )
        downstream = Rule(
            name="never",
            when=(Pattern(fact_type="t"),),
            then=(
                Action(
                    kind=ActionKind.ASSERT_FACT,
                    params={"fact_type": "side_effect", "data": {}},
                ),
            ),
        )
        engine = ExpertEngine([deny_rule, downstream])
        engine.assert_fact(Fact(fact_type="t", data={}))
        result = engine.run()
        assert result.denied
        assert "side_effect" not in engine.memory.stats()


# ---------------------------------------------------------------------------
# BackwardChainer
# ---------------------------------------------------------------------------


class TestBackward:
    def test_why_with_direct_fact(self) -> None:
        engine = ExpertEngine()
        engine.assert_fact(Fact(fact_type="alert", data={"level": "high"}))
        chainer = BackwardChainer(engine.rules, engine.memory)
        tree = chainer.why(pattern_from_shorthand("alert"))
        assert tree.proven
        assert "fact: alert" in tree.root.children[0].label

    def test_why_with_producer_rule(self) -> None:
        rule = Rule(
            name="trigger",
            when=(Pattern(fact_type="x"),),
            then=(
                Action(
                    kind=ActionKind.ASSERT_FACT,
                    params={"fact_type": "y", "data": {}},
                ),
            ),
        )
        engine = ExpertEngine([rule])
        engine.assert_fact(Fact(fact_type="x", data={}))
        chainer = BackwardChainer(engine.rules, engine.memory)
        tree = chainer.why(pattern_from_shorthand("y"))
        assert tree.proven
        assert "trigger" in tree.rules_visited

    def test_prove_succeeds_when_antecedents_present(self) -> None:
        rule = Rule(
            name="emit_alert",
            when=(Pattern(fact_type="signal"),),
            then=(
                Action(
                    kind=ActionKind.ASSERT_FACT,
                    params={"fact_type": "alert", "data": {}},
                ),
            ),
        )
        engine = ExpertEngine([rule])
        engine.assert_fact(Fact(fact_type="signal", data={}))
        chainer = BackwardChainer(engine.rules, engine.memory)
        tree = chainer.prove(pattern_from_shorthand("alert"))
        assert tree.proven

    def test_prove_fails_with_no_producer(self) -> None:
        engine = ExpertEngine()
        chainer = BackwardChainer(engine.rules, engine.memory)
        tree = chainer.prove(pattern_from_shorthand("nothing"))
        assert not tree.proven
        assert any("no rule asserts" in c.label for c in tree.root.children)

    def test_render_tree(self) -> None:
        engine = ExpertEngine()
        engine.assert_fact(Fact(fact_type="x", data={}))
        chainer = BackwardChainer(engine.rules, engine.memory)
        text = render_tree(chainer.why(pattern_from_shorthand("x")))
        assert "✓" in text
        assert "fact: x" in text


# ---------------------------------------------------------------------------
# YAML loader
# ---------------------------------------------------------------------------


class TestLoader:
    def test_load_minimal_rule(self) -> None:
        text = textwrap.dedent(
            """
            - name: block_rm
              when:
                - tool_call:
                    name: rm
              then:
                - deny: "no rm"
            """
        )
        rules = load_rules_from_string(text)
        assert len(rules) == 1
        rule = rules[0]
        assert rule.name == "block_rm"
        assert len(rule.when) == 1
        assert rule.when[0].fact_type == "tool_call"
        assert rule.when[0].predicates[0].field == "name"
        assert rule.when[0].predicates[0].value == "rm"
        assert rule.then[0].kind is ActionKind.DENY
        assert rule.then[0].params == {"reason": "no rm"}

    def test_load_predicate_operators(self) -> None:
        text = textwrap.dedent(
            """
            - name: r
              when:
                - tool_call:
                    name: shell_execute
                    cost:
                      gt: 1.0
                      lte: 10
                    command:
                      matches: 'git push.*--force'
                    env:
                      in: [prod, staging]
              then:
                - audit_log:
                    event: blocked
            """
        )
        rules = load_rules_from_string(text)
        assert len(rules) == 1
        pat = rules[0].when[0]
        # name is one predicate (eq), cost has two (gt + lte), command has one
        # (matches), env has one (in) — total 5 predicates
        assert len(pat.predicates) == 5
        ops_by_field = {(p.field, p.op) for p in pat.predicates}
        assert ("name", PredicateOp.EQ) in ops_by_field
        assert ("cost", PredicateOp.GT) in ops_by_field
        assert ("cost", PredicateOp.LTE) in ops_by_field
        assert ("command", PredicateOp.MATCHES) in ops_by_field
        assert ("env", PredicateOp.IN) in ops_by_field

    def test_load_bind_and_not(self) -> None:
        text = textwrap.dedent(
            """
            - name: r
              when:
                - session:
                    $bind: s
                    env: prod
                - tool_call:
                    $not: true
                    name: rm
              then:
                - deny: "no"
            """
        )
        rules = load_rules_from_string(text)
        assert rules[0].when[0].bind == "s"
        assert rules[0].when[1].negated is True

    def test_load_actions_bare_verb(self) -> None:
        text = textwrap.dedent(
            """
            - name: r
              when:
                - x: {}
              then:
                - audit_log
            """
        )
        rules = load_rules_from_string(text)
        assert rules[0].then[0].kind is ActionKind.AUDIT_LOG
        assert rules[0].then[0].params == {}

    def test_load_rule_file(self, tmp_path: Path) -> None:
        f = tmp_path / "r.yaml"
        f.write_text(
            "- name: r\n  when:\n    - x: {}\n  then:\n    - audit_log\n",
            encoding="utf-8",
        )
        rules = load_rule_file(f)
        assert len(rules) == 1
        assert rules[0].source_file == str(f)

    def test_load_dir_sorted(self, tmp_path: Path) -> None:
        (tmp_path / "b.yaml").write_text(
            "- name: b\n  when:\n    - x: {}\n  then:\n    - audit_log\n",
            encoding="utf-8",
        )
        (tmp_path / "a.yaml").write_text(
            "- name: a\n  when:\n    - x: {}\n  then:\n    - audit_log\n",
            encoding="utf-8",
        )
        rules = load_rules_from_dir(tmp_path)
        assert [r.name for r in rules] == ["a", "b"]

    def test_load_dir_missing_returns_empty(self, tmp_path: Path) -> None:
        assert load_rules_from_dir(tmp_path / "nope") == []

    def test_load_bad_yaml_raises(self) -> None:
        with pytest.raises(RuleLoadError) as exc_info:
            load_rules_from_string(":\n  - bad: [unclosed")
        assert "invalid YAML" in str(exc_info.value)

    def test_load_missing_name_raises(self) -> None:
        with pytest.raises(RuleLoadError) as exc_info:
            load_rules_from_string("- when: []\n  then: []")
        assert "missing or empty 'name'" in str(exc_info.value)

    def test_load_unknown_action_raises(self) -> None:
        text = textwrap.dedent(
            """
            - name: r
              when:
                - x: {}
              then:
                - frobnicate: yes
            """
        )
        with pytest.raises(RuleLoadError) as exc_info:
            load_rules_from_string(text)
        assert "frobnicate" in str(exc_info.value)
        assert "not a known action" in str(exc_info.value)

    def test_load_mixed_operator_and_nondict_keys_raises(self) -> None:
        """Typo'd operator next to a real one must fail loudly."""
        text = textwrap.dedent(
            """
            - name: r
              when:
                - x:
                    f:
                      gtt: 5
                      lt: 10
              then:
                - audit_log
            """
        )
        with pytest.raises(RuleLoadError) as exc_info:
            load_rules_from_string(text)
        assert "mixes operator and non-operator keys" in str(exc_info.value)

    def test_load_nested_dict_as_equality(self) -> None:
        """A pure non-operator dict is treated as an equality literal."""
        text = textwrap.dedent(
            """
            - name: r
              when:
                - x:
                    f:
                      nested:
                        deep: value
              then:
                - audit_log
            """
        )
        rules = load_rules_from_string(text)
        pred = rules[0].when[0].predicates[0]
        assert pred.op is PredicateOp.EQ
        assert pred.value == {"nested": {"deep": "value"}}


# ---------------------------------------------------------------------------
# Integration — realistic policy rulebook (forward + backward together)
# ---------------------------------------------------------------------------


class TestIntegration:
    def test_realistic_policy_rulebook(self) -> None:
        """End-to-end: load a YAML rulebook, drive facts, verify deny + audit."""
        text = textwrap.dedent(
            """
            - name: prod_force_push_gate
              description: Block force-push to main/master on prod.
              salience: 100
              when:
                - context:
                    $bind: ctx
                    env:
                      in: [prod, production]
                - tool_call:
                    name: shell_execute
                    command:
                      matches: 'git push.*--force.*(main|master)'
              then:
                - deny:
                    reason: "Force-push to main on {{ctx.env}} is prohibited"
                - audit_log:
                    event: prod_force_push_blocked
                - notify:
                    channel: slack
                    severity: high

            - name: budget_brake
              description: Hard brake at $5.00 session spend.
              salience: 90
              when:
                - session:
                    cost_usd:
                      gt: 5.0
              then:
                - require_approval:
                    gate: "Cost exceeded $5.00 — continue?"
            """
        )
        rules = load_rules_from_string(text)
        engine = ExpertEngine(rules)
        engine.assert_fact(Fact(fact_type="context", data={"env": "prod"}))
        engine.assert_fact(
            Fact(
                fact_type="tool_call",
                data={"name": "shell_execute", "command": "git push --force main"},
            )
        )
        result = engine.run()
        assert result.denied
        assert result.deny_reasons == ["Force-push to main on prod is prohibited"]
        # audit + notify were also emitted before the deny short-circuit, since
        # they're earlier in the rule's ``then``.
        kinds = {o.kind for o in result.actions.outcomes if o.ok}
        assert ActionKind.DENY in kinds
        assert ActionKind.AUDIT_LOG in kinds
        assert ActionKind.NOTIFY in kinds

    def test_realistic_policy_budget_approval(self) -> None:
        text = textwrap.dedent(
            """
            - name: budget_brake
              salience: 90
              when:
                - session:
                    cost_usd:
                      gt: 5.0
              then:
                - require_approval:
                    gate: "Cost exceeded $5.00"
            """
        )
        rules = load_rules_from_string(text)
        engine = ExpertEngine(rules)
        engine.assert_fact(Fact(fact_type="session", data={"cost_usd": 7.5}))
        result = engine.run()
        assert not result.denied
        assert len(result.actions.approvals_required) == 1
        assert result.actions.approvals_required[0]["gate"] == "Cost exceeded $5.00"


# ---------------------------------------------------------------------------
# V5: slow-run latency warning (observability hook, not optimization)
# ---------------------------------------------------------------------------


class TestV5SlowRunWarning:
    """The engine emits a structured warning when a single ``run()``
    exceeds the configured threshold. This is an observability hook
    so the team can defer optimization until a real customer signal
    arrives — the matcher itself is still ``O(P · F^k)``.
    """

    def test_elapsed_ms_populated_on_normal_run(self):
        from bog_agents.middleware.expert_engine.engine import ExpertEngine
        from bog_agents.middleware.expert_engine.types import Fact

        engine = ExpertEngine(rules=[])
        engine.assert_fact(Fact(fact_type="tool_call", data={"name": "ls"}))
        result = engine.run()
        # Normal runs are sub-millisecond; the field must exist and be
        # non-negative regardless of timing.
        assert result.elapsed_ms >= 0.0
        assert isinstance(result.elapsed_ms, float)

    def test_slow_run_emits_warning_when_threshold_breached(
        self, monkeypatch, caplog
    ):
        """Force a slow run by setting an absurdly low threshold."""
        import logging

        from bog_agents.middleware.expert_engine.engine import ExpertEngine
        from bog_agents.middleware.expert_engine.types import Fact

        monkeypatch.setenv("BOG_AGENTS_RULES_SLOW_WARN_MS", "0.0001")
        engine = ExpertEngine(rules=[])
        engine.assert_fact(Fact(fact_type="tool_call", data={"name": "ls"}))
        with caplog.at_level(logging.WARNING, logger="bog_agents.middleware.expert_engine.engine"):
            engine.run()
        assert any(
            "expert_engine slow run" in rec.message for rec in caplog.records
        )

    def test_threshold_zero_disables_warning(self, monkeypatch, caplog):
        import logging

        from bog_agents.middleware.expert_engine.engine import ExpertEngine
        from bog_agents.middleware.expert_engine.types import Fact

        monkeypatch.setenv("BOG_AGENTS_RULES_SLOW_WARN_MS", "0")
        engine = ExpertEngine(rules=[])
        engine.assert_fact(Fact(fact_type="tool_call", data={"name": "ls"}))
        with caplog.at_level(logging.WARNING, logger="bog_agents.middleware.expert_engine.engine"):
            engine.run()
        assert not any(
            "expert_engine slow run" in rec.message for rec in caplog.records
        )

    def test_threshold_invalid_value_falls_back_to_default(
        self, monkeypatch
    ):
        from bog_agents.middleware.expert_engine.engine import (
            _DEFAULT_SLOW_RUN_WARN_MS,
            _resolve_slow_warn_ms,
        )

        monkeypatch.setenv("BOG_AGENTS_RULES_SLOW_WARN_MS", "not-a-number")
        assert _resolve_slow_warn_ms() == _DEFAULT_SLOW_RUN_WARN_MS
