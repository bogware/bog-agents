"""Built-in scorers for `bog_agents.evals` (ROADMAP #9).

Rule-based scorers (ExactMatch, Contains, Regex) are pure and need no model.
LLMJudge grades an output against a rubric using any LangChain chat model —
the "LLM-as-judge" pattern teams use for open-ended outputs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from bog_agents.evals.core import Case, Score

if TYPE_CHECKING:
    from langchain_core.language_models import BaseChatModel


def _as_text(value: Any) -> str:
    """Best-effort stringify of a task output (handles message-like objects)."""
    content = getattr(value, "content", value)
    if isinstance(content, list):
        parts = [
            b.get("text", "") if isinstance(b, dict) else str(b) for b in content
        ]
        return "\n".join(p for p in parts if p)
    return content if isinstance(content, str) else str(content)


@dataclass
class ExactMatch:
    """Pass when the output equals ``case.expected`` (string-compared, trimmed)."""

    name: str = "exact_match"
    case_sensitive: bool = False

    def score(self, case: Case, output: Any) -> Score:
        out = _as_text(output).strip()
        exp = _as_text(case.expected).strip()
        if not self.case_sensitive:
            out, exp = out.lower(), exp.lower()
        ok = out == exp
        return Score(self.name, 1.0 if ok else 0.0, ok, "" if ok else f"expected {exp!r}")


@dataclass
class Contains:
    """Pass when the output contains ``case.expected`` (or a provided substring)."""

    name: str = "contains"
    case_sensitive: bool = False
    substring: str | None = None

    def score(self, case: Case, output: Any) -> Score:
        needle = self.substring if self.substring is not None else _as_text(case.expected)
        hay = _as_text(output)
        if not self.case_sensitive:
            needle, hay = needle.lower(), hay.lower()
        ok = needle in hay
        return Score(self.name, 1.0 if ok else 0.0, ok, "" if ok else f"missing {needle!r}")


@dataclass
class Regex:
    """Pass when the output matches a regular expression."""

    pattern: str
    name: str = "regex"
    flags: int = 0

    def score(self, case: Case, output: Any) -> Score:  # noqa: ARG002 — case unused
        ok = re.search(self.pattern, _as_text(output), self.flags) is not None
        return Score(self.name, 1.0 if ok else 0.0, ok, "" if ok else "no match")


@dataclass
class LLMJudge:
    """LLM-as-judge: grade the output against a rubric on a 0-1 scale.

    The model is asked to return a JSON object ``{"score": float, "reason": str}``.
    Pass threshold is configurable. Any parsing/model failure scores 0 (fail)
    with the error in ``detail`` rather than raising.
    """

    model: BaseChatModel
    rubric: str
    name: str = "llm_judge"
    threshold: float = 0.7

    async def score(self, case: Case, output: Any) -> Score:
        import json

        from langchain_core.messages import HumanMessage, SystemMessage

        system = (
            "You are a strict evaluation judge. Grade the candidate output "
            "against the rubric on a scale from 0.0 (fails) to 1.0 (fully meets). "
            'Respond ONLY with JSON: {"score": <float>, "reason": "<short>"}.'
        )
        user = (
            f"# Rubric\n{self.rubric}\n\n"
            f"# Task input\n{_as_text(case.input)}\n\n"
            + (f"# Reference / expected\n{_as_text(case.expected)}\n\n" if case.expected is not None else "")
            + f"# Candidate output\n{_as_text(output)}\n"
        )
        try:
            resp = await self.model.ainvoke(
                [SystemMessage(content=system), HumanMessage(content=user)]
            )
            text = _as_text(resp).strip()
            start, end = text.find("{"), text.rfind("}")
            data = json.loads(text[start : end + 1]) if start != -1 else {}
            value = float(data.get("score", 0.0))
            value = max(0.0, min(1.0, value))
            reason = str(data.get("reason", ""))[:300]
        except Exception as exc:
            return Score(self.name, 0.0, False, f"judge error: {type(exc).__name__}: {exc}")
        return Score(self.name, value, value >= self.threshold, reason)
