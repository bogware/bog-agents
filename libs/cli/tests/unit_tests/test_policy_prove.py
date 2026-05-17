"""Tests for the policy invariant prover (Q1).

Three layers:

* :mod:`bog_agents_cli.policy_prove.invariant` — YAML/dict parsing.
* :mod:`bog_agents_cli.policy_prove.prover` — heuristic prover +
  Z3 fallback path.
* :mod:`bog_agents_cli.policy_prove.controller` — slash-command
  dispatch + renderer + file lookup.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest
from bog_agents.middleware.expert_engine import (
    Action,
    ActionKind,
    Pattern,
    Predicate,
    PredicateOp,
    Rule,
)

from bog_agents_cli.policy_prove import (
    Invariant,
    InvariantProof,
    PatternSpec,
    PredicateSpec,
    ProofVerdict,
    load_invariant_from_dict,
    load_invariant_from_yaml,
    prove,
)
from bog_agents_cli.policy_prove.controller import (
    dispatch as prove_dispatch,
    render_proof,
)
from bog_agents_cli.policy_prove.invariant import InvariantParseError
from bog_agents_cli.policy_prove.prover import (
    _pattern_subsumes,
)

# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


_GOOD_YAML = dedent(
    """
    name: no_force_push_to_main
    description: Block any force-push to main/master.
    precondition:
      fact_type: tool_call
      predicates:
        - field: name
          op: eq
          value: shell_execute
    forbidden:
      fact_type: tool_call
      predicates:
        - field: command
          op: matches
          value: 'git push.*--force.*(main|master)'
    """
).strip()


class TestParser:
    def test_load_from_yaml_string(self):
        inv = load_invariant_from_yaml(_GOOD_YAML)
        assert inv.name == "no_force_push_to_main"
        assert inv.description.startswith("Block any force-push")
        assert inv.precondition.fact_type == "tool_call"
        assert inv.forbidden.predicates[0].op == PredicateOp.MATCHES

    def test_load_from_dict(self):
        inv = load_invariant_from_dict(
            {
                "name": "i1",
                "precondition": {"fact_type": "x"},
                "forbidden": {"fact_type": "y"},
            }
        )
        assert inv.name == "i1"

    def test_missing_name_raises(self):
        with pytest.raises(InvariantParseError, match="'name'"):
            load_invariant_from_dict(
                {"precondition": {"fact_type": "x"}, "forbidden": {"fact_type": "y"}}
            )

    def test_missing_precondition_raises(self):
        with pytest.raises(InvariantParseError, match="precondition"):
            load_invariant_from_dict(
                {"name": "i", "forbidden": {"fact_type": "y"}}
            )

    def test_pattern_missing_fact_type_raises(self):
        with pytest.raises(InvariantParseError, match="fact_type"):
            load_invariant_from_dict(
                {
                    "name": "i",
                    "precondition": {"predicates": []},
                    "forbidden": {"fact_type": "y"},
                }
            )

    def test_unknown_predicate_op_raises(self):
        with pytest.raises(InvariantParseError, match="Unknown predicate op"):
            load_invariant_from_dict(
                {
                    "name": "i",
                    "precondition": {
                        "fact_type": "x",
                        "predicates": [{"field": "a", "op": "wibble", "value": 1}],
                    },
                    "forbidden": {"fact_type": "y"},
                }
            )

    def test_empty_document_raises(self):
        with pytest.raises(InvariantParseError, match="empty"):
            load_invariant_from_yaml("")

    def test_malformed_yaml_raises(self):
        with pytest.raises(InvariantParseError, match="YAML parse error"):
            load_invariant_from_yaml("name: x: oops:\n  - bad: : :")

    def test_path_load(self, tmp_path: Path):
        p = tmp_path / "inv.yaml"
        p.write_text(_GOOD_YAML, encoding="utf-8")
        inv = load_invariant_from_yaml(p)
        assert inv.name == "no_force_push_to_main"


# ---------------------------------------------------------------------------
# Heuristic prover
# ---------------------------------------------------------------------------


def _mk_invariant(
    *,
    pre_preds=(),
    forbid_preds=(),
    name: str = "test",
) -> Invariant:
    """Tiny builder that yields a typical tool_call → tool_call invariant."""
    return Invariant(
        name=name,
        description="",
        precondition=PatternSpec(
            fact_type="tool_call",
            predicates=tuple(
                PredicateSpec(field=f, op=PredicateOp(op), value=v)
                for f, op, v in pre_preds
            ),
        ),
        forbidden=PatternSpec(
            fact_type="tool_call",
            predicates=tuple(
                PredicateSpec(field=f, op=PredicateOp(op), value=v)
                for f, op, v in forbid_preds
            ),
        ),
    )


def _mk_guard_rule(
    *,
    name: str,
    when_predicates: list[tuple[str, str, object]],
    action_kind: ActionKind = ActionKind.DENY,
) -> Rule:
    """Build a single-pattern rule with a blocking action."""
    return Rule(
        name=name,
        when=(
            Pattern(
                fact_type="tool_call",
                predicates=tuple(
                    Predicate(field=f, op=PredicateOp(op), value=v)
                    for f, op, v in when_predicates
                ),
            ),
        ),
        then=(Action(kind=action_kind, params={"reason": "test"}),),
    )


class TestProverHolds:
    def test_unconditional_guard_proves_invariant(self):
        invariant = _mk_invariant(
            forbid_preds=[("command", "matches", r"rm\s+-rf\s+/")],
        )
        # Rule that denies the forbidden shape unconditionally.
        guard = _mk_guard_rule(
            name="block_rm_rf",
            when_predicates=[("command", "matches", r"rm\s+-rf\s+/")],
        )
        proof = prove(invariant, [guard])
        assert proof.verdict == ProofVerdict.HOLDS
        assert "block_rm_rf" in proof.guards

    def test_require_approval_counts_as_blocking(self):
        invariant = _mk_invariant(
            forbid_preds=[("name", "eq", "deploy_prod")],
        )
        guard = _mk_guard_rule(
            name="approval_prod_deploy",
            when_predicates=[("name", "eq", "deploy_prod")],
            action_kind=ActionKind.REQUIRE_APPROVAL,
        )
        proof = prove(invariant, [guard])
        assert proof.verdict == ProofVerdict.HOLDS

    def test_multiple_guards_all_listed(self):
        invariant = _mk_invariant(
            forbid_preds=[("name", "eq", "x")],
        )
        g1 = _mk_guard_rule(name="g1", when_predicates=[("name", "eq", "x")])
        g2 = _mk_guard_rule(name="g2", when_predicates=[("name", "eq", "x")])
        proof = prove(invariant, [g1, g2])
        assert proof.verdict == ProofVerdict.HOLDS
        assert set(proof.guards) == {"g1", "g2"}


class TestProverCounterexample:
    def test_no_rules_at_all_produces_counterexample(self):
        invariant = _mk_invariant(
            pre_preds=[("name", "eq", "shell_execute")],
            forbid_preds=[("name", "eq", "leak_secrets")],
        )
        proof = prove(invariant, [])
        assert proof.verdict == ProofVerdict.COUNTEREXAMPLE
        assert "leak_secrets" in proof.counterexample

    def test_non_blocking_action_does_not_guard(self):
        invariant = _mk_invariant(
            forbid_preds=[("name", "eq", "x")],
        )
        # Audit-only rule isn't a guard.
        audit_rule = _mk_guard_rule(
            name="audit_x",
            when_predicates=[("name", "eq", "x")],
            action_kind=ActionKind.AUDIT_LOG,
        )
        proof = prove(invariant, [audit_rule])
        assert proof.verdict == ProofVerdict.COUNTEREXAMPLE

    def test_different_fact_type_does_not_guard(self):
        invariant = _mk_invariant(
            forbid_preds=[("name", "eq", "x")],
        )
        # Rule that matches a different fact_type.
        other_rule = Rule(
            name="session_guard",
            when=(Pattern(fact_type="session"),),
            then=(Action(kind=ActionKind.DENY),),
        )
        proof = prove(invariant, [other_rule])
        assert proof.verdict == ProofVerdict.COUNTEREXAMPLE

    def test_counterexample_unrenderable_falls_through(self):
        """INCONCLUSIVE when the prover can't sample the forbidden pattern.

        When predicates use ops we can't sample (NOT_IN/MISSING etc.)
        and there's no guard, the prover returns INCONCLUSIVE rather
        than an empty counterexample.
        """
        invariant = _mk_invariant(
            forbid_preds=[("name", "missing", None)],
        )
        proof = prove(invariant, [])
        assert proof.verdict == ProofVerdict.INCONCLUSIVE


# ---------------------------------------------------------------------------
# Subsumption helper
# ---------------------------------------------------------------------------


class TestSubsumption:
    def test_identical_pattern_subsumes(self):
        guard = Pattern(
            fact_type="tool_call",
            predicates=(Predicate(field="name", op=PredicateOp.EQ, value="x"),),
        )
        forbidden = PatternSpec(
            fact_type="tool_call",
            predicates=(PredicateSpec(field="name", op=PredicateOp.EQ, value="x"),),
        )
        assert _pattern_subsumes(guard, forbidden) is True

    def test_guard_pattern_strict_subset_of_forbidden_subsumes(self):
        # guard: name=x; forbidden: name=x AND target=y
        guard = Pattern(
            fact_type="tool_call",
            predicates=(Predicate(field="name", op=PredicateOp.EQ, value="x"),),
        )
        forbidden = PatternSpec(
            fact_type="tool_call",
            predicates=(
                PredicateSpec(field="name", op=PredicateOp.EQ, value="x"),
                PredicateSpec(field="target", op=PredicateOp.EQ, value="y"),
            ),
        )
        assert _pattern_subsumes(guard, forbidden) is True

    def test_extra_guard_predicate_does_not_subsume(self):
        # guard: name=x AND foo=z; forbidden: name=x (only)
        guard = Pattern(
            fact_type="tool_call",
            predicates=(
                Predicate(field="name", op=PredicateOp.EQ, value="x"),
                Predicate(field="foo", op=PredicateOp.EQ, value="z"),
            ),
        )
        forbidden = PatternSpec(
            fact_type="tool_call",
            predicates=(PredicateSpec(field="name", op=PredicateOp.EQ, value="x"),),
        )
        assert _pattern_subsumes(guard, forbidden) is False

    def test_different_fact_type_does_not_subsume(self):
        guard = Pattern(fact_type="session")
        forbidden = PatternSpec(fact_type="tool_call")
        assert _pattern_subsumes(guard, forbidden) is False

    def test_exists_op_subsumes_anything_touching_field(self):
        guard = Pattern(
            fact_type="tool_call",
            predicates=(Predicate(field="name", op=PredicateOp.EXISTS),),
        )
        forbidden = PatternSpec(
            fact_type="tool_call",
            predicates=(PredicateSpec(field="name", op=PredicateOp.EQ, value="x"),),
        )
        assert _pattern_subsumes(guard, forbidden) is True


# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------


class TestRender:
    def test_render_holds(self):
        inv = _mk_invariant(forbid_preds=[("name", "eq", "x")])
        proof = InvariantProof(
            invariant=inv,
            verdict=ProofVerdict.HOLDS,
            guards=("guard_x",),
            rationale="Invariant holds via 1 guard rule: guard_x.",
        )
        out = render_proof(proof)
        assert "✓ HOLDS" in out
        assert "guard_x" in out

    def test_render_counterexample(self):
        inv = _mk_invariant(forbid_preds=[("name", "eq", "x")])
        proof = InvariantProof(
            invariant=inv,
            verdict=ProofVerdict.COUNTEREXAMPLE,
            counterexample="example facts here",
            rationale="No guard rule blocks the forbidden pattern.",
        )
        out = render_proof(proof)
        assert "✗ COUNTEREXAMPLE" in out
        assert "example facts here" in out

    def test_render_inconclusive(self):
        inv = _mk_invariant(forbid_preds=[("name", "eq", "x")])
        proof = InvariantProof(
            invariant=inv,
            verdict=ProofVerdict.INCONCLUSIVE,
            rationale="Heuristic can't decide.",
            notes=("z3 backend unavailable",),
        )
        out = render_proof(proof)
        assert "? INCONCLUSIVE" in out
        assert "z3 backend unavailable" in out


# Wave V removed the Z3 backend entirely. Tests that exercised the
# parameter shape (use_z3=True / use_z3=None) live in git history
# rather than as dead code.


# ---------------------------------------------------------------------------
# Controller / slash dispatch
# ---------------------------------------------------------------------------


class TestController:
    @pytest.fixture(autouse=True)
    def _reset_expert_controllers(self):
        from bog_agents_cli.expert_controller import reset_controllers

        reset_controllers()
        yield
        reset_controllers()

    def test_help_when_empty(self, tmp_path: Path):
        out = prove_dispatch("/prove-invariant", tmp_path)
        assert "Usage" in out
        assert "/prove-invariant" in out

    def test_help_subcommand(self, tmp_path: Path):
        out = prove_dispatch("/prove-invariant help", tmp_path)
        assert "Invariant YAML shape" in out

    def test_list_empty(self, tmp_path: Path):
        out = prove_dispatch("/prove-invariant list", tmp_path)
        assert "No invariants/" in out

    def test_list_with_files(self, tmp_path: Path):
        d = tmp_path / "invariants"
        d.mkdir()
        (d / "no-force-push.yaml").write_text(_GOOD_YAML, encoding="utf-8")
        out = prove_dispatch("/prove-invariant list", tmp_path)
        assert "no-force-push.yaml" in out
        assert "Block any force-push" in out

    def test_list_handles_parse_error(self, tmp_path: Path):
        d = tmp_path / "invariants"
        d.mkdir()
        (d / "broken.yaml").write_text("not: a: valid: doc", encoding="utf-8")
        out = prove_dispatch("/prove-invariant list", tmp_path)
        assert "[parse error" in out or "broken.yaml" in out

    def test_inline_yaml_with_no_rules_returns_counterexample(
        self, tmp_path: Path
    ):
        out = prove_dispatch(f"/prove-invariant\n{_GOOD_YAML}", tmp_path)
        assert "COUNTEREXAMPLE" in out

    def test_file_path_argument_loads(self, tmp_path: Path):
        d = tmp_path / "invariants"
        d.mkdir()
        path = d / "x.yaml"
        path.write_text(_GOOD_YAML, encoding="utf-8")
        out = prove_dispatch(
            "/prove-invariant invariants/x.yaml", tmp_path
        )
        # No rules loaded → counterexample expected.
        assert "COUNTEREXAMPLE" in out

    def test_unparseable_body_reports_error(self, tmp_path: Path):
        # Not a path, not a valid YAML doc.
        out = prove_dispatch(
            "/prove-invariant name-only-no-precondition", tmp_path
        )
        assert "Could not parse invariant" in out

    def test_legacy_z3_flag_silently_ignored(self, tmp_path: Path):
        """Wave V removed --z3, but the controller still strips it.

        Users with muscle-memory from earlier waves shouldn't get
        a parse error when they include --z3 in a /prove-invariant
        invocation; the flag is silently dropped and the heuristic
        prover runs.
        """
        body = "\n".join([_GOOD_YAML, ""])
        out = prove_dispatch(
            f"/prove-invariant --z3\n{body}", tmp_path
        )
        assert "COUNTEREXAMPLE" in out or "INCONCLUSIVE" in out
