"""Policy invariant prover — ``/prove-invariant`` (Q1, moat wave).

Compiles expert-rule YAML and a user-supplied invariant into either a
proof (no fact sequence violates it) or a counterexample (here is how
it can be violated).

The invariant language is small on purpose. An invariant is a pair:

* **precondition** — a pattern describing a situation the user cares
  about (e.g. "after ``git push`` to main").
* **forbidden** — a pattern that must never fire when the precondition
  holds (e.g. "tool call to read PII data").

Sub-modules
-----------

* :mod:`.invariant` — data model + YAML loader.
* :mod:`.prover` — the heuristic-first prover (no extra deps), with an
  optional Z3 backend when ``z3-solver`` is installed.
* :mod:`.controller` — slash-command facade for ``/prove-invariant``.
"""

from __future__ import annotations

from bog_agents_cli.policy_prove.invariant import (
    Invariant,
    PatternSpec,
    PredicateSpec,
    load_invariant_from_dict,
    load_invariant_from_yaml,
)
from bog_agents_cli.policy_prove.prover import (
    InvariantProof,
    ProofVerdict,
    prove,
)

__all__ = [
    "Invariant",
    "InvariantProof",
    "PatternSpec",
    "PredicateSpec",
    "ProofVerdict",
    "load_invariant_from_dict",
    "load_invariant_from_yaml",
    "prove",
]
