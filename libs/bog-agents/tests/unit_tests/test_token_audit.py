"""ROADMAP #54: harness overhead audit, per-middleware attribution, and the `lean` profile."""

from __future__ import annotations

from pathlib import Path

import pytest
from langchain_core.messages import HumanMessage

from bog_agents.backends import LocalShellBackend
from bog_agents.feature_config import FeatureConfig
from bog_agents.graph import create_agent
from bog_agents.profiles.harness._lean import LEAN_BASE_PROMPT, LEAN_TOOL_DESCRIPTIONS
from bog_agents.profiles.harness.harness_profiles import named_harness_profile
from bog_agents.token_audit import (
    AgentAssembly,
    RecordingChatModel,
    TokenAudit,
    approx_tokens,
    audit_agent,
    audit_create_agent,
    capture_assembly,
    count_tokens,
    notify_assembly,
)


def _backend() -> LocalShellBackend:
    return LocalShellBackend(root_dir=Path.cwd(), virtual_mode=True)


def _default_audit() -> TokenAudit:
    return audit_create_agent(method="approx", backend=_backend())


def _lean_audit() -> TokenAudit:
    return audit_create_agent(method="approx", backend=_backend(), config=FeatureConfig(harness_profile="lean"))


class TestCounting:
    def test_approx_is_deterministic_and_monotone(self) -> None:
        text = "def read_file(path: str, offset: int = 0) -> str:\n    return open(path).read()\n"
        assert approx_tokens(text) == approx_tokens(text) > 10
        assert approx_tokens(text * 2) > approx_tokens(text)
        assert approx_tokens("") == 0
        assert count_tokens("", method="approx") == 0

    def test_unknown_method_rejected(self) -> None:
        with pytest.raises(ValueError, match="unknown token counting method"):
            count_tokens("x", method="nope")


class TestRecordingModel:
    def test_records_tools_and_messages(self) -> None:
        model = RecordingChatModel()
        bound = model.bind_tools([{"type": "function", "function": {"name": "t", "description": "d", "parameters": {"type": "object"}}}])
        assert bound is model
        result = bound.invoke([HumanMessage(content="hi")])
        assert result.content == "ok"
        assert model.calls[0]["tools"][0]["function"]["name"] == "t"
        assert model.calls[0]["messages"][0].content == "hi"


class TestAssemblyHook:
    def test_hook_sees_final_stack_and_is_noop_outside(self) -> None:
        seen: list[AgentAssembly] = []
        notify_assembly([], [], "unused")  # no hook installed → nothing happens
        with capture_assembly(seen.append):
            create_agent(model=RecordingChatModel(), backend=_backend())
        assert seen, "create_agent must notify the audit hook"
        names = [type(m).__name__ for m in seen[0].middleware]
        assert "FilesystemMiddleware" in names
        assert isinstance(seen[0].system_prompt, str) and seen[0].system_prompt


class TestAudit:
    def test_default_agent_is_measured_and_attributed(self) -> None:
        audit = _default_audit()
        assert audit.tokenizer == "approx"
        assert audit.per_turn_overhead > 3000
        assert audit.system_prompt_tokens > 0 and audit.tool_schema_tokens > 0
        tool_names = {t.name for t in audit.tools}
        assert {"task", "write_todos", "read_file", "execute"} <= tool_names
        by_name = {m.name: m for m in audit.middleware}
        assert by_name["FilesystemMiddleware"].prompt_tokens > 0
        assert by_name["SubAgentMiddleware"].prompt_tokens > 0
        assert audit.unattributed_prompt_tokens == 0, audit.render()
        assert "Harness overhead:" in audit.render()
        assert audit.to_dict()["per_turn_overhead"] == audit.per_turn_overhead

    def test_lean_profile_cuts_overhead_by_more_than_half(self) -> None:
        default = _default_audit()
        lean = _lean_audit()
        assert lean.per_turn_overhead * 2 < default.per_turn_overhead, (lean.per_turn_overhead, default.per_turn_overhead)
        assert "write_todos" not in {t.name for t in lean.tools}
        assert "TodoListMiddleware" not in {m.name for m in lean.middleware}
        assert lean.assembled_prompt_tokens == count_tokens(LEAN_BASE_PROMPT, method="approx")
        lean_tools = {t.name: t for t in lean.tools}
        default_tools = {t.name: t for t in default.tools}
        for name in LEAN_TOOL_DESCRIPTIONS:
            if name in lean_tools:
                assert lean_tools[name].description_tokens < default_tools[name].description_tokens, name

    def test_deferred_allowlist_hides_every_other_schema(self) -> None:
        audit = audit_create_agent(
            method="approx",
            backend=_backend(),
            config=FeatureConfig(enable_deferred_tools=True, deferred_keep_tools=["read_file", "execute"]),
        )
        assert {t.name for t in audit.tools} == {"read_file", "execute", "tool_search", "select"}
        by_name = {m.name: m for m in audit.middleware}
        assert by_name["DeferredToolsMiddleware"].tool_tokens < 0  # it removes schemas from the request

    def test_unknown_named_profile_is_an_error(self) -> None:
        with pytest.raises(ValueError, match="Unknown harness profile"):
            create_agent(model=RecordingChatModel(), backend=_backend(), config=FeatureConfig(harness_profile="no-such-profile"))
        assert named_harness_profile("lean").base_system_prompt == LEAN_BASE_PROMPT

    async def test_sync_audit_works_inside_a_running_loop(self) -> None:
        audit = audit_agent(lambda model: create_agent(model=model, backend=_backend()), method="approx")
        assert audit.per_turn_overhead > 0
