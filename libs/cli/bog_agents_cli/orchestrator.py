"""Orchestrator — T-8 /orchestrate (Roo Code "Boomerang Tasks" parity).

The user states a goal. The orchestrator asks the LLM to decompose
it into mode-typed subtasks (code / test / review / doc / research),
runs each subtask in its own one-shot subagent with the appropriate
tool surface, and returns a tree-shaped summary into the parent
transcript. No competitor packages this exact UX end-to-end today —
bog-agents already has all the building blocks (subagents, sidecar's
read-only tool builder, the same model factory pattern), this module
just composes them.

Designed mirror-image of :mod:`bog_agents_cli.sidecar` so tests look
similar and operators see one consistent shape: pure-logic module
(model + tools injected) + CLI controller + thin app.py handler.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bog_agents_cli.sidecar import (
    SIDECAR_SYSTEM_PROMPT,
    build_readonly_tools,
    run_sidecar_query,
)

if TYPE_CHECKING:
    from langchain_core.language_models import BaseChatModel
    from langchain_core.tools import BaseTool

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Subtask modes
# ---------------------------------------------------------------------------


class SubtaskMode(StrEnum):
    """The vocabulary of subtask types the planner can pick from."""

    CODE = "code"
    TEST = "test"
    REVIEW = "review"
    DOC = "doc"
    RESEARCH = "research"


@dataclass(frozen=True)
class ModeProfile:
    """Per-mode system prompt + tool-set builder.

    Attributes:
        mode: Which :class:`SubtaskMode` this profile covers.
        system_prompt: Sent to the model for subtasks of this mode.
        tool_builder: Zero-arg callable returning the tool list. Each
            subtask gets a fresh tool list so per-invocation state
            (e.g. a search session) doesn't leak between subtasks.
        max_iterations: Hard cap on the subtask's model→tool loop.
    """

    mode: SubtaskMode
    system_prompt: str
    tool_builder: Callable[[], list[BaseTool]]
    max_iterations: int = 12


_CODE_PROMPT = """You are the **code** worker in a multi-agent
orchestration. You receive ONE precise subtask. Read the relevant
files, propose code, and ask the human to apply it via the parent
agent — DO NOT make edits yourself in this subagent. Output:

1. A short summary of what you'd change and why.
2. The minimal-but-complete diff or new file content.
3. Any caveats or follow-ups the parent agent needs to consider.

You have read-only tools. The parent agent owns the write surface.
"""

_TEST_PROMPT = """You are the **test** worker. Your subtask is to
analyse what tests are needed, identify gaps, and propose specific
test code. Output:

1. A list of test cases (one line each).
2. Test code for each (or pointers to the file each should live in).
3. Risk classification: which ones MUST land before shipping vs which
   are nice-to-have.

You have read-only tools. The parent agent owns the write surface.
"""

_REVIEW_PROMPT = """You are the **review** worker. Read the code or
plan provided and produce a focused critique. Output:

1. Top 3 concerns in priority order, each with a file:line citation
   when possible.
2. Anything the parent agent missed.
3. A short go / no-go recommendation for the broader goal.

You have read-only tools. Be specific; vague reviews are worse than
no review.
"""

_DOC_PROMPT = """You are the **doc** worker. Read the code or change
and produce documentation. Output:

1. A short summary of what changed and why.
2. Suggested docstring(s) or README section(s).
3. Where they should live.

You have read-only tools. The parent agent owns the write surface.
"""

_RESEARCH_PROMPT = """You are the **research** worker. Investigate
the question, search the web when helpful, and produce a concise
findings report. Output:

1. Key findings, each with a source citation.
2. Open questions worth following up.
3. A recommendation for next action.

You have read-only tools + web_search.
"""


def _make_default_profiles(working_dir: Path) -> dict[SubtaskMode, ModeProfile]:
    """Build the default mode → :class:`ModeProfile` map."""
    def _readonly(*, web: bool = False) -> Callable[[], list[BaseTool]]:
        def _builder() -> list[BaseTool]:
            return build_readonly_tools(working_dir=working_dir, web_search=web)

        return _builder

    return {
        SubtaskMode.CODE: ModeProfile(
            mode=SubtaskMode.CODE,
            system_prompt=_CODE_PROMPT,
            tool_builder=_readonly(web=False),
        ),
        SubtaskMode.TEST: ModeProfile(
            mode=SubtaskMode.TEST,
            system_prompt=_TEST_PROMPT,
            tool_builder=_readonly(web=False),
        ),
        SubtaskMode.REVIEW: ModeProfile(
            mode=SubtaskMode.REVIEW,
            system_prompt=_REVIEW_PROMPT,
            tool_builder=_readonly(web=False),
        ),
        SubtaskMode.DOC: ModeProfile(
            mode=SubtaskMode.DOC,
            system_prompt=_DOC_PROMPT,
            tool_builder=_readonly(web=False),
        ),
        SubtaskMode.RESEARCH: ModeProfile(
            mode=SubtaskMode.RESEARCH,
            system_prompt=_RESEARCH_PROMPT,
            tool_builder=_readonly(web=True),
        ),
    }


# ---------------------------------------------------------------------------
# Plan + result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Subtask:
    """One subtask in an orchestrator plan."""

    id: str
    mode: SubtaskMode
    description: str


@dataclass
class SubtaskResult:
    """Outcome of running one subtask."""

    subtask: Subtask
    answer: str = ""
    ok: bool = True
    error: str = ""
    tool_calls_made: list[str] = field(default_factory=list)
    duration_seconds: float = 0.0


@dataclass
class OrchestrationResult:
    """Final return from :func:`run_orchestration`."""

    goal: str
    plan: list[Subtask] = field(default_factory=list)
    subtasks: list[SubtaskResult] = field(default_factory=list)
    plan_raw: str = ""  # the LLM's pre-parse output (for debugging)
    parse_error: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        """True iff every subtask succeeded and the plan parsed cleanly."""
        if self.error or self.parse_error:
            return False
        return all(r.ok for r in self.subtasks)


# ---------------------------------------------------------------------------
# Planner system prompt
# ---------------------------------------------------------------------------


_PLANNER_SYSTEM_PROMPT = """You are the planning step of a multi-agent
orchestrator. The user states a goal. You decompose it into the
SMALLEST useful set of subtasks (typically 2-5; never more than 8)
and pick a mode for each.

Modes available:

* ``code``      — read the project, propose code changes (read-only).
* ``test``      — analyse test coverage, propose test cases.
* ``review``    — critique an existing plan, design, or code section.
* ``doc``       — produce documentation / explanation.
* ``research``  — investigate something external (web search ok).

Rules:

1. Output **ONLY** valid JSON. No commentary, no markdown fences.
2. Top-level shape: ``{"plan": [{"id": "t1", "mode": "code",
   "description": "..."}, ...]}``. Ids are short kebab-strings like
   ``t1``, ``t2``.
3. Subtask descriptions are concrete and self-contained — assume the
   worker that runs them has only the description and read-only file
   access, NOT the parent conversation.
4. Never produce a plan that requires the worker to write code or
   run shell commands — the parent agent owns the write surface. The
   worker's job is to *propose*, not *do*.
5. If the goal is small enough for ONE subtask, emit one. Don't
   manufacture extra subtasks for the sake of it.
"""


# ---------------------------------------------------------------------------
# Decompose
# ---------------------------------------------------------------------------


def decompose_goal(
    goal: str,
    *,
    model: BaseChatModel,
    system_prompt: str = _PLANNER_SYSTEM_PROMPT,
) -> tuple[list[Subtask], str, str]:
    """Ask the model to decompose *goal* into a list of subtasks.

    Returns:
        ``(subtasks, raw_text, error)``. When parsing fails ``error``
        is non-empty and ``subtasks`` is the empty list. ``raw_text``
        is always the model's literal output so the caller can show
        it to the user for debugging.
    """
    from langchain_core.messages import HumanMessage, SystemMessage

    messages = [
        SystemMessage(content=system_prompt),
        HumanMessage(
            content=(
                "Decompose this goal into a plan. Output JSON only.\n\n"
                f"Goal: {goal.strip()}"
            )
        ),
    ]
    try:
        response = model.invoke(messages)
    except Exception as exc:
        return [], "", f"model call failed: {exc}"
    raw = getattr(response, "content", response)
    if isinstance(raw, list):
        text_parts = [
            b.get("text", "")
            for b in raw
            if isinstance(b, dict) and b.get("type") == "text"
        ]
        text = "".join(text_parts)
    else:
        text = str(raw)
    subtasks, err = _parse_plan(text)
    return subtasks, text, err


def _parse_plan(text: str) -> tuple[list[Subtask], str]:
    """Parse the planner's output into :class:`Subtask` instances."""
    body = _strip_json_fences(text)
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        return [], f"planner did not emit valid JSON: {exc}"
    if not isinstance(payload, dict):
        return [], f"planner JSON top-level must be an object, got {type(payload).__name__}"
    plan = payload.get("plan")
    if not isinstance(plan, list):
        return [], "planner JSON missing 'plan' list"
    out: list[Subtask] = []
    for idx, item in enumerate(plan, start=1):
        if not isinstance(item, dict):
            return [], f"plan[{idx}] must be an object"
        sid = str(item.get("id") or f"t{idx}")
        mode_str = str(item.get("mode") or "").lower()
        try:
            mode = SubtaskMode(mode_str)
        except ValueError:
            return [], (
                f"plan[{idx}].mode {mode_str!r} is not a known mode "
                f"({', '.join(m.value for m in SubtaskMode)})"
            )
        desc = str(item.get("description") or "").strip()
        if not desc:
            return [], f"plan[{idx}].description is empty"
        out.append(Subtask(id=sid, mode=mode, description=desc))
    if not out:
        return [], "plan is empty"
    if len(out) > 8:
        return [], f"plan has too many subtasks ({len(out)}); keep it under 8"
    return out, ""


_JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


def _strip_json_fences(text: str) -> str:
    """Remove ```json … ``` markdown fences from *text*."""
    text = text.strip()
    if text.startswith("```"):
        text = _JSON_FENCE_RE.sub("", text)
    return text.strip()


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


def run_orchestration(
    *,
    goal: str,
    model: BaseChatModel,
    working_dir: Path,
    profiles: dict[SubtaskMode, ModeProfile] | None = None,
    max_iterations_per_subtask: int = 12,
) -> OrchestrationResult:
    """End-to-end orchestration: decompose, run subtasks, return results.

    Sequential v1 — runs subtasks in plan order, no parallelism.
    Parallel execution is a follow-up; ordered run keeps the subtask
    output deterministic and matches the way humans expect to read
    boomerang results.

    Args:
        goal: User's plain-English goal.
        model: Chat model. Used for BOTH the planner and the per-
            subtask workers. Tests inject a stub.
        working_dir: Project root for the read-only tools.
        profiles: Override the default mode → profile map (mostly
            for tests).
        max_iterations_per_subtask: Per-subtask loop cap.

    Returns:
        :class:`OrchestrationResult`. Never raises.
    """
    result = OrchestrationResult(goal=goal.strip())
    if not goal.strip():
        result.error = "empty goal — pass /orchestrate <your goal>"
        return result

    subtasks, raw, err = decompose_goal(goal, model=model)
    result.plan_raw = raw
    if err:
        result.parse_error = err
        return result
    result.plan = subtasks
    if profiles is None:
        profiles = _make_default_profiles(working_dir)

    for st in subtasks:
        st_result = SubtaskResult(subtask=st)
        profile = profiles.get(st.mode)
        if profile is None:
            st_result.ok = False
            st_result.error = f"no profile registered for mode {st.mode.value!r}"
            result.subtasks.append(st_result)
            continue
        try:
            tools = profile.tool_builder()
        except Exception as exc:
            st_result.ok = False
            st_result.error = f"tool builder failed: {exc}"
            result.subtasks.append(st_result)
            continue
        started = time.monotonic()
        # Reuse sidecar's runner — same one-shot read-only semantics.
        sidecar_run = run_sidecar_query(
            question=st.description,
            model=model,
            tools=tools,
            context_summary=(
                f"You are subtask {st.id} of an orchestrator plan for goal: "
                f"{goal.strip()}\nMode: {st.mode.value}."
            ),
            system_prompt=profile.system_prompt or SIDECAR_SYSTEM_PROMPT,
            max_iterations=min(profile.max_iterations, max_iterations_per_subtask),
        )
        st_result.duration_seconds = time.monotonic() - started
        st_result.answer = sidecar_run.answer
        st_result.ok = sidecar_run.ok
        st_result.error = sidecar_run.error
        st_result.tool_calls_made = list(sidecar_run.tool_calls_made)
        result.subtasks.append(st_result)
    return result


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_result(result: OrchestrationResult) -> str:
    """Markdown summary of the orchestration for the parent transcript."""
    lines: list[str] = []
    lines.append(f"## /orchestrate plan for: {result.goal[:120]}")
    if result.error:
        lines.append(f"> ⚠ orchestrator failed: {result.error}")
        return "\n".join(lines)
    if result.parse_error:
        lines.append(f"> ⚠ planner output could not be parsed: {result.parse_error}")
        if result.plan_raw:
            lines.append("\n```")
            lines.append(result.plan_raw[:600])
            lines.append("```")
        return "\n".join(lines)
    if not result.plan:
        lines.append("> _(no plan)_")
        return "\n".join(lines)
    lines.append(f"_{len(result.plan)} subtask(s); status: {'ok' if result.ok else 'partial'}_")
    lines.append("")
    for st_result in result.subtasks:
        st = st_result.subtask
        mark = "✓" if st_result.ok else "✗"
        lines.append(
            f"### {mark} {st.id} [{st.mode.value}] — {st.description[:100]}"
        )
        if st_result.duration_seconds:
            lines.append(f"_(took {st_result.duration_seconds:.1f}s)_")
        if not st_result.ok:
            lines.append(f"> ⚠ {st_result.error}")
            continue
        if st_result.tool_calls_made:
            lines.append(
                f"_(consulted: {', '.join(st_result.tool_calls_made)})_"
            )
        lines.append("")
        lines.append(st_result.answer.strip() or "_(no output)_")
        lines.append("")
    return "\n".join(lines)


__all__ = [
    "ModeProfile",
    "OrchestrationResult",
    "Subtask",
    "SubtaskMode",
    "SubtaskResult",
    "decompose_goal",
    "render_result",
    "run_orchestration",
]


# Silence unused-import lints when the tests don't drive Any path.
_ = Any
