"""Real-LLM integration test for the D-5 provenance loop (K3).

Opt-in test that uses a real Anthropic model to verify the LLM
actually invokes the citation / hallucination_detection / fact_check
tools when ``enable_provenance_loop=True``. Skips cleanly when no
API key is present so the suite stays green offline.

To run::

    cd libs/bog-agents
    ANTHROPIC_API_KEY=sk-ant-... uv run --group test pytest \
        tests/integration_tests/test_provenance_loop_integration.py -v

The test honors the user's note that the env var may be misspelled as
``ANTHROPIC_API_KEP`` in some setups — both names are accepted.
"""

from __future__ import annotations

import os

import pytest
from langchain_core.messages import HumanMessage

from bog_agents.feature_config import FeatureConfig
from bog_agents.graph import create_agent

# K3: support both the canonical env var AND the typo variant the
# user has flagged. First non-empty wins; we copy into the canonical
# name so downstream langchain code (which reads the canonical name
# directly) finds the key.
_KEY_CANDIDATES = ("ANTHROPIC_API_KEY", "ANTHROPIC_API_KEP")
_API_KEY = next(
    (os.environ[name] for name in _KEY_CANDIDATES if os.environ.get(name)),
    None,
)
if _API_KEY and not os.environ.get("ANTHROPIC_API_KEY"):
    os.environ["ANTHROPIC_API_KEY"] = _API_KEY

# Tool calls the LLM should make when the provenance loop is active.
# We don't insist on every one (the model has discretion), just at
# least one provenance-flavored call.
_PROVENANCE_TOOL_NAMES = frozenset(
    {
        "register_data_source",
        "add_citation",
        "register_fact",
        "verify_claim",
        "submit_claim",
        "generate_bibliography",
        "verification_report",
        "factcheck_report",
    }
)


@pytest.mark.skipif(
    _API_KEY is None,
    reason=("No Anthropic API key found in environment (ANTHROPIC_API_KEY or ANTHROPIC_API_KEP). Opt-in test."),
)
class TestProvenanceLoopWithRealModel:
    """End-to-end: ask a question that demands a sourced answer, assert
    the LLM at least *registered a source* or *added a citation* on
    its way to the response.
    """

    def test_llm_uses_provenance_tools_when_loop_active(self) -> None:
        agent = create_agent(
            model="claude-haiku-4-5-20251001",
            config=FeatureConfig(enable_provenance_loop=True),
        )
        # The question deliberately invites a verifiable factual claim
        # so the model has a reason to register + cite a source.
        result = agent.invoke(
            {
                "messages": [
                    HumanMessage(
                        content=(
                            "I need a short answer with a registered "
                            "source: in what year was Python first "
                            "released, and where did you get the answer? "
                            "Use the provenance tools you have access to."
                        )
                    )
                ]
            },
            config={"configurable": {"thread_id": "k3-provenance-1"}},
        )
        ai_messages = [m for m in result.get("messages", []) if m.type == "ai"]
        tool_calls = [call for m in ai_messages for call in getattr(m, "tool_calls", []) or []]
        called = {call.get("name", "") for call in tool_calls}
        assert called & _PROVENANCE_TOOL_NAMES, (
            "Expected the LLM to call at least one provenance tool when "
            "enable_provenance_loop=True is set. Tools the model actually "
            f"called: {sorted(called)}. Provenance vocabulary: "
            f"{sorted(_PROVENANCE_TOOL_NAMES)}"
        )

    def test_llm_does_not_call_provenance_tools_when_loop_inactive(self) -> None:
        """Negative control: same question, loop OFF — the LLM doesn't
        invoke the provenance tools (they aren't even bound).
        """
        agent = create_agent(
            model="claude-haiku-4-5-20251001",
            config=FeatureConfig(),  # all flags default off
        )
        result = agent.invoke(
            {"messages": [HumanMessage(content=("In what year was Python first released? Answer in one sentence."))]},
            config={"configurable": {"thread_id": "k3-provenance-2"}},
        )
        ai_messages = [m for m in result.get("messages", []) if m.type == "ai"]
        tool_calls = [call for m in ai_messages for call in getattr(m, "tool_calls", []) or []]
        called = {call.get("name", "") for call in tool_calls}
        assert not (called & _PROVENANCE_TOOL_NAMES), (
            "Expected the LLM to NOT call provenance tools when the loop "
            "is inactive (they shouldn't even be bound). The LLM called: "
            f"{sorted(called & _PROVENANCE_TOOL_NAMES)}"
        )
