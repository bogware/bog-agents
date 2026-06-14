"""`bog_agents.guardrails` — composable input/output guardrails (ROADMAP #18).

A single declarative abstraction over the scattered safety capability: bring a
list of guardrails and the middleware runs them around each model call, failing
fast on a tripwire (the OpenAI Agents SDK pattern)::

    from bog_agents import create_agent
    from bog_agents.guardrails import (
        GuardrailMiddleware,
        NoSecretsGuardrail,
        MaxLengthGuardrail,
    )

    agent = create_agent(
        model="claude-sonnet-4-6",
        middleware=[
            GuardrailMiddleware(
                input_guardrails=[MaxLengthGuardrail(20000)],
                output_guardrails=[NoSecretsGuardrail()],
            )
        ],
    )
"""

from __future__ import annotations

from bog_agents.guardrails.core import (
    BlocklistGuardrail,
    Guardrail,
    GuardrailResult,
    GuardrailTripwireError,
    LLMGuardrail,
    MaxLengthGuardrail,
    NoSecretsGuardrail,
    first_tripped,
    run_guardrails,
)
from bog_agents.guardrails.middleware import GuardrailMiddleware

__all__ = [
    "BlocklistGuardrail",
    "Guardrail",
    "GuardrailMiddleware",
    "GuardrailResult",
    "GuardrailTripwireError",
    "LLMGuardrail",
    "MaxLengthGuardrail",
    "NoSecretsGuardrail",
    "first_tripped",
    "run_guardrails",
]
