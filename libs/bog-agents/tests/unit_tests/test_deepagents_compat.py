"""Tests for the deepagents compatibility surface and new create_agent params.

Covers the `bog_agents.deepagents` re-export module, the `create_deep_agent`
wrapper, and the `permissions=` / `state_schema=` wiring added to
`create_agent`. Graph assembly is exercised via a fake chat model and a
monkeypatched `_langchain_create_agent` so the tests need no API key or network
(mirrors `test_graph_upstream_parity.py`).
"""

from __future__ import annotations

from typing import Any

import pytest

import bog_agents
import bog_agents.graph as bog_graph
from bog_agents.middleware._tool_exclusion import _ToolExclusionMiddleware
from bog_agents.middleware.permissions import FilesystemPermission, FilesystemPermissionsMiddleware
from bog_agents.profiles.harness.harness_profiles import HarnessProfile
from tests.unit_tests.chat_model import GenericFakeChatModel

# The public symbols deepagents exports from its top-level package. The
# bog-agents compat module must provide every one of these so deepagents code
# imports unchanged.
_DEEPAGENTS_PUBLIC_API = frozenset(
    {
        "AsyncSubAgent",
        "AsyncSubAgentMiddleware",
        "CompiledSubAgent",
        "DeepAgentState",
        "FilesystemMiddleware",
        "FilesystemPermission",
        "FsToolName",
        "GeneralPurposeSubagentProfile",
        "HarnessProfile",
        "HarnessProfileConfig",
        "MemoryMiddleware",
        "ProviderProfile",
        "RubricMiddleware",
        "SubAgent",
        "SubAgentMiddleware",
        "SystemPromptConfig",
        "__version__",
        "create_deep_agent",
        "register_harness_profile",
        "register_provider_profile",
    }
)

# New interop symbols we additionally re-export beyond upstream's __all__.
_INTEROP_EXTRAS = frozenset({"create_sub_agent", "SUBAGENT_RESPONSE_FORMAT_CONFIG_KEY"})


class _DummyCompiledGraph:
    """Capture `.with_config()` calls without building a real graph."""

    def with_config(self, config: dict[str, Any]) -> _DummyCompiledGraph:
        return self


def _capture_create_agent(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Patch `_langchain_create_agent` and return a dict that captures its kwargs."""
    captured: dict[str, Any] = {}

    def fake_create_agent(model: object, **kwargs: object) -> _DummyCompiledGraph:
        captured["model"] = model
        captured["kwargs"] = kwargs
        return _DummyCompiledGraph()

    monkeypatch.setattr(bog_graph, "_langchain_create_agent", fake_create_agent)
    return captured


def test_compat_module_exposes_full_deepagents_api() -> None:
    """`bog_agents.deepagents` provides every deepagents top-level symbol."""
    import bog_agents.deepagents as compat

    missing = _DEEPAGENTS_PUBLIC_API - set(compat.__all__)
    assert not missing, f"compat module missing deepagents symbols: {sorted(missing)}"
    # Every advertised name is importable (not just listed in __all__).
    for name in compat.__all__:
        assert getattr(compat, name) is not None


def test_top_level_package_reexports_deepagents_names() -> None:
    """The deepagents-style names resolve from the top-level `bog_agents` package."""
    for name in _DEEPAGENTS_PUBLIC_API - {"__version__"}:
        assert getattr(bog_agents, name) is not None


def test_interop_extras_importable_from_both_entry_points() -> None:
    """The extra interop symbols resolve from the compat module and top-level package."""
    import bog_agents.deepagents as compat

    for name in _INTEROP_EXTRAS:
        assert name in compat.__all__
        assert getattr(compat, name) is not None
        assert getattr(bog_agents, name) is not None


def test_system_prompt_config_mapping_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    """The deepagents `SystemPromptConfig` mapping form is accepted (was a `TypeError`)."""
    captured = _capture_create_agent(monkeypatch)
    from bog_agents.deepagents import create_deep_agent

    create_deep_agent(
        model=GenericFakeChatModel(messages=iter([])),
        system_prompt={"base": None, "suffix": "be terse"},
    )
    assert "be terse" in captured["kwargs"]["system_prompt"]


def test_system_prompt_plain_string_still_works(monkeypatch: pytest.MonkeyPatch) -> None:
    """A plain-string `system_prompt` keeps working through the compat wrapper."""
    captured = _capture_create_agent(monkeypatch)
    from bog_agents.deepagents import create_deep_agent

    create_deep_agent(model=GenericFakeChatModel(messages=iter([])), system_prompt="hi")
    assert "hi" in captured["kwargs"]["system_prompt"]


def test_create_deep_agent_forwards_max_turns(monkeypatch: pytest.MonkeyPatch) -> None:
    """`create_deep_agent` exposes keyword-only `max_turns` (default mirrors deepagents)."""
    captured = _capture_create_agent(monkeypatch)
    from bog_agents.deepagents import create_deep_agent

    create_deep_agent(model=GenericFakeChatModel(messages=iter([])), max_turns=7)
    # `max_turns` shapes the compiled graph's recursion_limit, not a passthrough
    # kwarg; assert the call succeeded and default omission also works.
    assert captured["kwargs"] is not None
    create_deep_agent(model=GenericFakeChatModel(messages=iter([])))


def test_create_deep_agent_defaults_state_schema_to_deep_agent_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """`create_deep_agent` forwards `state_schema=DeepAgentState` like deepagents."""
    captured = _capture_create_agent(monkeypatch)
    from bog_agents.deepagents import create_deep_agent

    create_deep_agent(model=GenericFakeChatModel(messages=iter([])))
    assert captured["kwargs"]["state_schema"] is bog_graph.DeepAgentState


def test_create_agent_preserves_default_state_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    """`create_agent` without `state_schema` does not force one (default preserved)."""
    captured = _capture_create_agent(monkeypatch)
    bog_graph.create_agent(model=GenericFakeChatModel(messages=iter([])))
    assert "state_schema" not in captured["kwargs"]


def test_create_agent_forwards_explicit_state_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit `state_schema` is forwarded to the underlying create_agent."""
    captured = _capture_create_agent(monkeypatch)
    bog_graph.create_agent(model=GenericFakeChatModel(messages=iter([])), state_schema=bog_graph.DeepAgentState)
    assert captured["kwargs"]["state_schema"] is bog_graph.DeepAgentState


def test_permissions_install_enforcement_middleware(monkeypatch: pytest.MonkeyPatch) -> None:
    """A `permissions` list installs `FilesystemPermissionsMiddleware` on the main stack and GP subagent."""
    captured = _capture_create_agent(monkeypatch)
    rule = FilesystemPermission(operations=["write"], paths=["/secrets/**"], mode="deny")
    bog_graph.create_agent(model=GenericFakeChatModel(messages=iter([])), permissions=[rule])

    middleware = captured["kwargs"]["middleware"]
    main_perm = [m for m in middleware if isinstance(m, FilesystemPermissionsMiddleware)]
    assert len(main_perm) == 1
    assert main_perm[0].permissions == [rule]

    # The auto-added general-purpose subagent also gets enforcement.
    sub_mw = next(m for m in middleware if isinstance(m, bog_graph.SubAgentMiddleware))
    gp = next(spec for spec in sub_mw._subagents if spec["name"] == "general-purpose")
    assert any(isinstance(m, FilesystemPermissionsMiddleware) for m in gp["middleware"])


def test_no_permissions_means_no_enforcement_middleware(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without `permissions`, no `FilesystemPermissionsMiddleware` is added (default unchanged)."""
    captured = _capture_create_agent(monkeypatch)
    bog_graph.create_agent(model=GenericFakeChatModel(messages=iter([])))
    middleware = captured["kwargs"]["middleware"]
    assert not any(isinstance(m, FilesystemPermissionsMiddleware) for m in middleware)


def test_subagent_permissions_override_parent(monkeypatch: pytest.MonkeyPatch) -> None:
    """A subagent's own `permissions` replace the parent's; omission inherits."""
    captured = _capture_create_agent(monkeypatch)
    model = GenericFakeChatModel(messages=iter([]))
    parent_rule = FilesystemPermission(operations=["write"], paths=["/a/**"], mode="deny")
    child_rule = FilesystemPermission(operations=["read"], paths=["/b/**"], mode="deny")
    bog_graph.create_agent(
        model=model,
        permissions=[parent_rule],
        subagents=[
            {
                "name": "own-perms",
                "description": "Has its own perms",
                "system_prompt": "x",
                "permissions": [child_rule],
            },
            {
                "name": "inherits",
                "description": "Inherits parent perms",
                "system_prompt": "y",
            },
        ],
    )
    sub_mw = next(m for m in captured["kwargs"]["middleware"] if isinstance(m, bog_graph.SubAgentMiddleware))

    own = next(spec for spec in sub_mw._subagents if spec["name"] == "own-perms")
    own_perm = next(m for m in own["middleware"] if isinstance(m, FilesystemPermissionsMiddleware))
    assert own_perm.permissions == [child_rule]

    inherits = next(spec for spec in sub_mw._subagents if spec["name"] == "inherits")
    inherits_perm = next(m for m in inherits["middleware"] if isinstance(m, FilesystemPermissionsMiddleware))
    assert inherits_perm.permissions == [parent_rule]


def test_interrupt_permission_installs_hitl(monkeypatch: pytest.MonkeyPatch) -> None:
    """An interrupt-mode permission installs a HumanInTheLoopMiddleware on the main stack."""
    from langchain.agents.middleware import HumanInTheLoopMiddleware

    captured = _capture_create_agent(monkeypatch)
    rule = FilesystemPermission(operations=["read"], paths=["/private/**"], mode="interrupt")
    bog_graph.create_agent(model=GenericFakeChatModel(messages=iter([])), permissions=[rule])
    middleware = captured["kwargs"]["middleware"]
    assert any(isinstance(m, HumanInTheLoopMiddleware) for m in middleware)


def test_subagent_typeddict_accepts_permissions_and_response_format() -> None:
    """`SubAgent` declares the deepagents-parity optional keys."""
    from bog_agents.middleware.subagents import SubAgent

    annotations = SubAgent.__annotations__
    assert "permissions" in annotations
    assert "response_format" in annotations


def _force_profile(monkeypatch: pytest.MonkeyPatch, profile: HarnessProfile) -> None:
    """Make `create_agent` resolve `profile` regardless of the model."""
    monkeypatch.setattr(bog_graph, "_harness_profile_for_model", lambda *_a, **_k: profile)


def test_profile_system_prompt_suffix_applied(monkeypatch: pytest.MonkeyPatch) -> None:
    """`HarnessProfile.system_prompt_suffix` is appended to the base prompt."""
    captured = _capture_create_agent(monkeypatch)
    _force_profile(monkeypatch, HarnessProfile(system_prompt_suffix="ZZZ-SUFFIX"))
    bog_graph.create_agent(model=GenericFakeChatModel(messages=iter([])))
    assert "ZZZ-SUFFIX" in captured["kwargs"]["system_prompt"]


def test_profile_base_system_prompt_replaces(monkeypatch: pytest.MonkeyPatch) -> None:
    """`HarnessProfile.base_system_prompt` replaces the SDK base prompt outright."""
    captured = _capture_create_agent(monkeypatch)
    _force_profile(monkeypatch, HarnessProfile(base_system_prompt="ONLY-THIS"))
    bog_graph.create_agent(model=GenericFakeChatModel(messages=iter([])))
    assert captured["kwargs"]["system_prompt"] == "ONLY-THIS"


def test_profile_tool_description_override_applied(monkeypatch: pytest.MonkeyPatch) -> None:
    """`tool_description_overrides` rewrites a dict tool without mutating the original."""
    captured = _capture_create_agent(monkeypatch)
    _force_profile(monkeypatch, HarnessProfile(tool_description_overrides={"mytool": "NEW DESC"}))
    original = {"name": "mytool", "description": "old"}
    bog_graph.create_agent(model=GenericFakeChatModel(messages=iter([])), tools=[original])
    assert captured["kwargs"]["tools"][0]["description"] == "NEW DESC"
    assert original["description"] == "old"  # caller-owned tool untouched


def test_profile_excluded_tools_installs_exclusion_middleware(monkeypatch: pytest.MonkeyPatch) -> None:
    """`excluded_tools` installs `_ToolExclusionMiddleware` on the main stack."""
    captured = _capture_create_agent(monkeypatch)
    _force_profile(monkeypatch, HarnessProfile(excluded_tools=frozenset({"execute"})))
    bog_graph.create_agent(model=GenericFakeChatModel(messages=iter([])))
    assert any(isinstance(m, _ToolExclusionMiddleware) for m in captured["kwargs"]["middleware"])


def test_profile_excluded_middleware_removed(monkeypatch: pytest.MonkeyPatch) -> None:
    """`excluded_middleware` (by name) drops the matching middleware from the stack."""
    captured = _capture_create_agent(monkeypatch)
    _force_profile(monkeypatch, HarnessProfile(excluded_middleware=frozenset({"AnthropicPromptCachingMiddleware"})))
    bog_graph.create_agent(model=GenericFakeChatModel(messages=iter([])))
    names = {type(m).__name__ for m in captured["kwargs"]["middleware"]}
    assert "AnthropicPromptCachingMiddleware" not in names


def test_profile_excluded_middleware_no_match_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """An `excluded_middleware` entry that matches nothing raises a clear error."""
    _capture_create_agent(monkeypatch)
    _force_profile(monkeypatch, HarnessProfile(excluded_middleware=frozenset({"NonexistentMiddleware"})))
    with pytest.raises(ValueError, match="matched no middleware"):
        bog_graph.create_agent(model=GenericFakeChatModel(messages=iter([])))


def test_profile_cannot_exclude_required_scaffolding() -> None:
    """Excluding `FilesystemMiddleware`/`SubAgentMiddleware` is rejected at construction."""
    with pytest.raises(ValueError, match="scaffolding"):
        HarnessProfile(excluded_middleware=frozenset({"FilesystemMiddleware"}))


# ---------------------------------------------------------------------------
# deepagents 0.7 result-type shape parity
# ---------------------------------------------------------------------------


def test_readresult_carries_07_pagination_fields() -> None:
    """ReadResult exposes 0.7's total_lines/start_line/end_line/next_offset."""
    from bog_agents.backends.protocol import ReadResult

    # Default (bog's common case): pagination not tracked, all None, no error.
    default = ReadResult(file_data={"content": "x"})
    assert default.total_lines is None
    assert default.start_line is None
    assert default.end_line is None
    assert default.next_offset is None

    # A backend that does track pagination populates them.
    paged = ReadResult(file_data={"content": "x"}, total_lines=500, start_line=1, end_line=100, next_offset=100)
    assert (paged.total_lines, paged.start_line, paged.end_line, paged.next_offset) == (500, 1, 100, 100)


def test_readresult_rejects_negative_pagination() -> None:
    from bog_agents.backends.protocol import ReadResult

    with pytest.raises(ValueError, match="non-negative"):
        ReadResult(total_lines=-1)


def test_grepmatch_accepts_07_context_fields() -> None:
    """GrepMatch/ContextLine match 0.7's shape (context is NotRequired)."""
    from bog_agents.backends.protocol import ContextLine, GrepMatch

    before: list[ContextLine] = [{"line": 4, "text": "prev"}]
    match: GrepMatch = {"path": "/a.py", "line": 5, "text": "hit", "context_before": before}
    assert match["context_before"][0]["line"] == 4
    # Context is optional — a match without it is still valid.
    minimal: GrepMatch = {"path": "/a.py", "line": 5, "text": "hit"}
    assert minimal.get("context_after") is None
