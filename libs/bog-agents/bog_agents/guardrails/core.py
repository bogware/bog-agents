"""Composable input/output guardrails with fail-fast tripwire semantics (#18).

A single declarative abstraction over what was scattered across the safety
middleware: validators that run on the inbound user message and the outbound
model response, each able to "trip a wire" that fails the turn fast (the OpenAI
Agents SDK pattern). Bring a list of :class:`Guardrail`s; the
:class:`GuardrailMiddleware` runs input guardrails before the model and output
guardrails after it.

Everything here is pure and synchronous-or-async friendly so guardrails are
trivially unit-testable in isolation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Awaitable, Sequence


@dataclass(frozen=True)
class GuardrailResult:
    """Outcome of one guardrail.

    Attributes:
        guardrail: Name of the guardrail that produced this result.
        tripped: True when the guardrail's tripwire fired (a violation).
        reason: Human-readable explanation when tripped.
    """

    guardrail: str
    tripped: bool
    reason: str = ""


@runtime_checkable
class Guardrail(Protocol):
    """Validates a piece of text. ``check`` may be sync or async."""

    name: str

    def check(self, text: str) -> GuardrailResult | Awaitable[GuardrailResult]: ...


class GuardrailTripwireError(RuntimeError):
    """Raised when a guardrail trips and the policy is to fail fast."""

    def __init__(self, result: GuardrailResult, *, stage: str) -> None:
        self.result = result
        self.stage = stage
        super().__init__(f"{stage} guardrail '{result.guardrail}' tripped: {result.reason}")


# ---------------------------------------------------------------------------
# Built-in guardrails
# ---------------------------------------------------------------------------


@dataclass
class BlocklistGuardrail:
    """Trip when the text matches any blocked pattern (regex, case-insensitive)."""

    patterns: Sequence[str]
    name: str = "blocklist"

    def check(self, text: str) -> GuardrailResult:
        for pat in self.patterns:
            if re.search(pat, text, re.IGNORECASE):
                return GuardrailResult(self.name, tripped=True, reason=f"matched /{pat}/")
        return GuardrailResult(self.name, tripped=False)


@dataclass
class MaxLengthGuardrail:
    """Trip when the text exceeds ``max_chars``."""

    max_chars: int
    name: str = "max_length"

    def check(self, text: str) -> GuardrailResult:
        if len(text) > self.max_chars:
            return GuardrailResult(
                self.name,
                tripped=True,
                reason=f"{len(text)} chars exceeds limit {self.max_chars}",
            )
        return GuardrailResult(self.name, tripped=False)


# Conservative secret shapes: AWS keys, GitHub PATs, Slack tokens, Anthropic/
# OpenAI keys, generic "API key"/private-key headers. Defence-in-depth only.
_SECRET_PATTERNS = (
    r"AKIA[0-9A-Z]{16}",  # AWS access key id
    r"ghp_[A-Za-z0-9]{36}",  # GitHub personal access token
    r"xox[baprs]-[A-Za-z0-9-]{10,}",  # Slack token
    r"sk-ant-[A-Za-z0-9_\-]{20,}",  # Anthropic key
    r"sk-[A-Za-z0-9]{32,}",  # OpenAI-style key
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",  # private key block
)


@dataclass
class NoSecretsGuardrail:
    """Trip when the text appears to contain a leaked secret/credential."""

    name: str = "no_secrets"

    def check(self, text: str) -> GuardrailResult:
        for pat in _SECRET_PATTERNS:
            if re.search(pat, text):
                return GuardrailResult(self.name, tripped=True, reason="possible secret/credential detected")
        return GuardrailResult(self.name, tripped=False)


@dataclass
class LLMGuardrail:
    """LLM-as-judge tripwire: trip when the text violates a stated policy.

    Asks the model to return ``{"violation": bool, "reason": str}``. A model/
    parse failure does NOT trip (fail-open for the judge itself, so an LLM
    outage can't block every turn) — use a rule-based guardrail when you need a
    hard guarantee.
    """

    model: Any
    policy: str
    name: str = "llm_guardrail"

    async def check(self, text: str) -> GuardrailResult:
        import json

        from langchain_core.messages import HumanMessage, SystemMessage

        system = (
            'You are a guardrail. Decide whether the text VIOLATES the policy. Respond ONLY with JSON: {"violation": <bool>, "reason": "<short>"}.'
        )
        user = f"# Policy\n{self.policy}\n\n# Text\n{text}"
        try:
            resp = await self.model.ainvoke([SystemMessage(content=system), HumanMessage(content=user)])
            raw = getattr(resp, "content", resp)
            raw = raw if isinstance(raw, str) else str(raw)
            start, end = raw.find("{"), raw.rfind("}")
            data = json.loads(raw[start : end + 1]) if start != -1 else {}
            tripped = bool(data.get("violation", False))
            return GuardrailResult(self.name, tripped=tripped, reason=str(data.get("reason", "")))
        except Exception:
            return GuardrailResult(self.name, tripped=False, reason="judge unavailable")


async def _await_maybe(value: Any) -> Any:
    import inspect

    if inspect.isawaitable(value):
        return await value
    return value


async def run_guardrails(text: str, guardrails: Sequence[Guardrail], *, stop_on_first: bool = True) -> list[GuardrailResult]:
    """Run guardrails over ``text`` and return their results.

    Args:
        text: The text to validate.
        guardrails: Guardrails to run, in order.
        stop_on_first: Return as soon as one trips (the fail-fast default).

    Returns:
        The collected :class:`GuardrailResult`s. With ``stop_on_first`` the last
        entry is the tripped one when any tripped.
    """
    results: list[GuardrailResult] = []
    for g in guardrails:
        result = await _await_maybe(g.check(text))
        results.append(result)
        if result.tripped and stop_on_first:
            break
    return results


def first_tripped(results: Sequence[GuardrailResult]) -> GuardrailResult | None:
    """Return the first tripped result, or None."""
    return next((r for r in results if r.tripped), None)
