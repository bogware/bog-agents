"""QA harness — adaptive acceptance-criteria testing for deployed products.

The /qa package provides a complete loop:

1. **Ingest** acceptance criteria from Jira / files / JSON / stdin / wizard
   (:mod:`bog_agents_cli.qa.ac`).
2. **Plan** — an LLM-driven planner inspects the project, asks clarifying
   questions, declares variables (including secrets) and emits a typed
   plan (:mod:`bog_agents_cli.qa.plan`).
3. **Execute** the plan with a hybrid step model — agent / shell / http /
   mcp steps — capturing evidence per AC
   (:mod:`bog_agents_cli.qa.executor`).
4. **Report** — emit a verdict per AC plus an artifact in the chosen
   format (markdown / json / jira-comment / stdout)
   (:mod:`bog_agents_cli.qa.artifact`).

Plans are project-scoped: ``<project>/.bog-agents/qa-plans/<plan_id>.yaml``.
Run results land in ``<project>/.bog-agents/qa-results/<plan_id>/<run_id>.*``.
"""

from bog_agents_cli.qa.ac import (
    AcceptanceCriterion,
    load_acceptance_criteria,
    parse_ac_from_json,
    parse_ac_from_text,
)
from bog_agents_cli.qa.artifact import emit_artifact
from bog_agents_cli.qa.executor import (
    ACOutcome,
    ExecutionResult,
    StepResult,
    execute_plan,
)
from bog_agents_cli.qa.plan import (
    QAPlan,
    QAStep,
    StepKind,
    StepVerdict,
    find_plan,
    list_plans,
    load_plan,
    save_plan,
)

__all__ = [
    "ACOutcome",
    "AcceptanceCriterion",
    "ExecutionResult",
    "QAPlan",
    "QAStep",
    "StepKind",
    "StepResult",
    "StepVerdict",
    "emit_artifact",
    "execute_plan",
    "find_plan",
    "list_plans",
    "load_acceptance_criteria",
    "load_plan",
    "parse_ac_from_json",
    "parse_ac_from_text",
    "save_plan",
]
