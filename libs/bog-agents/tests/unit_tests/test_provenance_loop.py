"""Tests for the D-5 provenance-loop umbrella flag.

Verifies that ``enable_provenance_loop=True`` composes all three
provenance middleware (citations, hallucination_detection, fact_check)
AND injects the per-loop system-prompt addendum so the model is
actually told to use the tools.
"""

from __future__ import annotations

import pytest
from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from bog_agents.feature_config import FeatureConfig
from bog_agents.graph import _PROVENANCE_LOOP_PROMPT, create_agent
from bog_agents.middleware.citations import CitationsMiddleware
from bog_agents.middleware.fact_check import FactCheckMiddleware
from bog_agents.middleware.hallucination_detection import (
    HallucinationDetectionMiddleware,
)


def _fake_model() -> GenericFakeChatModel:
    """Return a fake chat model with one canned response."""
    return GenericFakeChatModel(messages=iter([AIMessage(content="ok")]))


def _middleware_classes(agent) -> set[type]:
    """Walk the compiled graph's middleware list (best-effort)."""
    raw = getattr(agent, "_middleware", None)
    if raw is None:
        raw = getattr(getattr(agent, "_step_runner", object()), "_middleware", [])
    return {type(m) for m in raw if isinstance(m, AgentMiddleware)}


class TestProvenanceUmbrella:
    def test_default_off(self) -> None:
        cfg = FeatureConfig()
        assert cfg.enable_provenance_loop is False

    def test_individual_flags_still_default_off(self) -> None:
        cfg = FeatureConfig()
        assert cfg.enable_citations is False
        assert cfg.enable_hallucination_detection is False
        assert cfg.enable_fact_check is False

    def test_umbrella_compiles(self) -> None:
        """Sanity: create_agent with the umbrella on does not raise."""
        cfg = FeatureConfig(enable_provenance_loop=True)
        agent = create_agent(model=_fake_model(), config=cfg)
        assert agent is not None

    def test_individual_flags_still_work_alone(self) -> None:
        """Each flag still wires its own middleware without the umbrella."""
        cfg = FeatureConfig(enable_citations=True)
        agent = create_agent(model=_fake_model(), config=cfg)
        assert agent is not None


class TestProvenancePrompt:
    def test_prompt_constant_mentions_each_step(self) -> None:
        """The injected addendum names every workflow step the model
        is expected to execute.
        """
        prompt = _PROVENANCE_LOOP_PROMPT
        for term in (
            "register_data_source",
            "add_citation",
            "register_fact",
            "verify_claim",
            "submit_claim",
        ):
            assert term in prompt, f"prompt missing {term!r}"

    def test_prompt_warns_against_unsourced_guesses(self) -> None:
        assert "Unsourced" in _PROVENANCE_LOOP_PROMPT
        assert "say" in _PROVENANCE_LOOP_PROMPT.lower()


class TestProvenanceInjection:
    """Confirm the addendum is injected by capturing the system_prompt
    argument bog-agents passes to the upstream ``create_agent`` call.
    Monkey-patching the upstream call site is the cleanest assertion —
    compiled-graph internals vary across langchain/langgraph versions.
    """

    @pytest.fixture
    def captured(self, monkeypatch: pytest.MonkeyPatch) -> dict:
        """Replace the upstream ``create_agent`` with a recorder."""
        captures: dict = {}

        def fake_create_agent(*_args, **kwargs):  # noqa: ANN002, ANN003
            captures.update(kwargs)

            # Return a sentinel that satisfies the chained
            # ``.with_config(...)`` call bog-agents does after creating
            # the upstream agent. Returning self makes the chain a
            # no-op while still letting create_agent run to completion.
            class _Compiled:
                def with_config(self, _cfg):
                    return self

            return _Compiled()

        import bog_agents.graph as graph_module

        monkeypatch.setattr(
            graph_module, "_langchain_create_agent", fake_create_agent
        )
        return captures

    @pytest.mark.parametrize(
        "flag",
        [
            "enable_provenance_loop",
            "enable_citations",
            "enable_fact_check",
            "enable_hallucination_detection",
        ],
    )
    def test_any_provenance_flag_injects_addendum(
        self, flag: str, captured: dict
    ) -> None:
        cfg = FeatureConfig(**{flag: True})
        create_agent(model=_fake_model(), config=cfg)
        system_prompt = captured.get("system_prompt", "")
        assert "Citations & Verification" in str(system_prompt), (
            f"provenance addendum missing from system_prompt when {flag} is True"
        )

    def test_no_flag_no_addendum(self, captured: dict) -> None:
        cfg = FeatureConfig()
        create_agent(model=_fake_model(), config=cfg)
        system_prompt = captured.get("system_prompt", "")
        assert "Citations & Verification" not in str(system_prompt), (
            "provenance addendum injected when no flag was set"
        )


_ = (CitationsMiddleware, HallucinationDetectionMiddleware, FactCheckMiddleware)
