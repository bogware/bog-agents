"""`bog_agents.evals` — evaluation as a first-class SDK primitive (ROADMAP #9).

Bring a dataset of cases and a set of scorers, point the runner at any callable,
and gate releases on the pass rate::

    from bog_agents.evals import Dataset, Contains, run_evals

    data = Dataset.from_list(
        [
            {"input": "2+2", "expected": "4"},
            {"input": "capital of France", "expected": "Paris"},
        ]
    )

    report = await run_evals(my_agent_fn, data, [Contains()])
    report.assert_pass_rate(0.9)  # raises in CI if below 90%
    print(report.summary())
"""

from __future__ import annotations

from bog_agents.evals.core import (
    Case,
    CaseResult,
    Dataset,
    EvalReport,
    Score,
    Scorer,
    run_evals,
)
from bog_agents.evals.scorers import Contains, ExactMatch, LLMJudge, Regex

__all__ = [
    "Case",
    "CaseResult",
    "Contains",
    "Dataset",
    "EvalReport",
    "ExactMatch",
    "LLMJudge",
    "Regex",
    "Score",
    "Scorer",
    "run_evals",
]
