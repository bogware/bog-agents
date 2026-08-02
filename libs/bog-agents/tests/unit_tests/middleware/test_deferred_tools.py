"""Unit tests for `bog_agents.middleware.deferred_tools`.

Covers request shaping (hiding deferred schemas from the model), the
`select`/`tool_search` metatools, the `select:<name>` shorthand, and the
`create_agent` wiring via `FeatureConfig`.
"""

from __future__ import annotations

from typing import Any

import pytest
from langchain.agents.middleware.types import ModelRequest, ToolCallRequest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.tools import tool

from bog_agents.middleware.deferred_tools import DeferredToolsMiddleware


@tool
def git_diff(staged: bool = False) -> str:
    """Show working-tree changes, optionally staged-only."""
    return "diff"


@tool
def git_log(count: int = 10) -> str:
    """Show recent commit history."""
    return "log"


@tool
def read_file(path: str) -> str:
    """Read a file's contents."""
    return "file"


class _FakeModel:
    """Minimal BaseChatModel stand-in: identifiable, never called."""

    _llm_type = "fake"

    def _get_ls_params(self, **_kwargs: Any) -> dict[str, str]:
        return {"ls_provider": "fake", "ls_model_name": "fake-model"}


def _request(tools: list[Any]) -> ModelRequest[Any]:
    return ModelRequest(
        model=_FakeModel(),
        messages=[HumanMessage(content="hi")],
        system_message=SystemMessage(content="base"),
        tools=tools,
        state={"messages": [HumanMessage(content="hi")]},
    )


def _full_tools(mw: DeferredToolsMiddleware) -> list[Any]:
    return [git_diff, git_log, read_file, *mw.tools]


def _handler(capture: dict[str, Any]):
    def handler(request: ModelRequest[Any]) -> AIMessage:
        capture["tools"] = list(request.tools)
        return AIMessage(content="ok")

    return handler


def _names(tools: list[Any]) -> list[str]:
    return [t.name for t in tools]


def _tool_call_request(name: str, args: dict[str, Any], tool_id: str = "tc1") -> ToolCallRequest:
    return ToolCallRequest(
        tool_call={"name": name, "args": args, "id": tool_id, "type": "tool_call"},
        tool=None,
        state={},
        runtime=None,
    )


def _populate(mw: DeferredToolsMiddleware) -> None:
    """Run one model request so the middleware indexes the tool registry."""
    mw.wrap_model_call(_request(_full_tools(mw)), _handler({}))


class TestRequestShaping:
    def test_deferred_tool_schema_hidden_from_model(self) -> None:
        mw = DeferredToolsMiddleware(deferred_names=frozenset({"git_diff"}))
        capture: dict[str, Any] = {}
        mw.wrap_model_call(_request(_full_tools(mw)), _handler(capture))
        visible = _names(capture["tools"])
        assert "git_diff" not in visible
        assert "git_log" in visible
        assert "read_file" in visible
        # The metatools must be present so the model can discover tools.
        assert "tool_search" in visible
        assert "select" in visible

    def test_activated_tool_reappears(self) -> None:
        mw = DeferredToolsMiddleware(deferred_names=frozenset({"git_diff", "git_log"}))
        _populate(mw)
        mw._activate("git_diff")
        capture: dict[str, Any] = {}
        mw.wrap_model_call(_request(_full_tools(mw)), _handler(capture))
        visible = _names(capture["tools"])
        assert "git_diff" in visible
        assert "git_log" not in visible

    def test_non_deferred_tools_unchanged(self) -> None:
        mw = DeferredToolsMiddleware(deferred_names=frozenset({"git_diff"}))
        capture: dict[str, Any] = {}
        mw.wrap_model_call(_request(_full_tools(mw)), _handler(capture))
        assert "read_file" in _names(capture["tools"])

    def test_noop_when_no_deferred_names(self) -> None:
        mw = DeferredToolsMiddleware()
        assert mw.tools == []
        capture: dict[str, Any] = {}
        mw.wrap_model_call(_request(_full_tools(mw)), _handler(capture))
        # Every tool (including the un-deferred git_diff) stays visible.
        assert "git_diff" in _names(capture["tools"])

    async def test_async_request_shaping(self) -> None:
        mw = DeferredToolsMiddleware(deferred_names=frozenset({"git_diff"}))

        async def handler(request: ModelRequest[Any]) -> AIMessage:
            return AIMessage(content="ok")

        await mw.awrap_model_call(_request(_full_tools(mw)), handler)


class TestSelectMetatool:
    def test_select_activates_and_returns_schema(self) -> None:
        mw = DeferredToolsMiddleware(deferred_names=frozenset({"git_diff", "git_log"}))
        mw.wrap_model_call(_request(_full_tools(mw)), _handler({}))
        select_tool = next(t for t in mw.tools if t.name == "select")
        result = select_tool.func(None, name="git_diff")  # type: ignore[arg-type]
        assert result.startswith("Tool 'git_diff' is active")
        assert "Show working-tree changes" in result
        assert '"staged"' in result
        assert "git_diff" in mw._activated

    def test_select_unknown_tool_returns_error(self) -> None:
        mw = DeferredToolsMiddleware(deferred_names=frozenset({"git_diff"}))
        mw.wrap_model_call(_request(_full_tools(mw)), _handler({}))
        select_tool = next(t for t in mw.tools if t.name == "select")
        result = select_tool.func(None, name="nope")  # type: ignore[arg-type]
        assert result.startswith("Error: no tool named")
        assert "git_diff" in result
        assert not mw._activated

    def test_select_shorthand_intercepted_in_wrap_tool_call(self) -> None:
        from langchain_core.messages import ToolMessage

        mw = DeferredToolsMiddleware(deferred_names=frozenset({"git_diff", "git_log"}))
        mw.wrap_model_call(_request(_full_tools(mw)), _handler({}))
        out = mw.wrap_tool_call(
            _tool_call_request("select:git_log", {}),
            lambda _request: "should-not-run",
        )
        assert isinstance(out, ToolMessage)
        assert "git_log" in out.content
        assert "git_log" in mw._activated
        assert out.name == "select:git_log"
        assert out.tool_call_id == "tc1"

    async def test_select_shorthand_async(self) -> None:
        mw = DeferredToolsMiddleware(deferred_names=frozenset({"git_diff"}))
        _populate(mw)

        async def handler(_request: ToolCallRequest) -> str:
            return "should-not-run"

        out = await mw.awrap_tool_call(_tool_call_request("select:git_diff", {}), handler)
        assert "git_diff" in out.content
        assert "git_diff" in mw._activated

    def test_non_select_calls_pass_through(self) -> None:
        mw = DeferredToolsMiddleware(deferred_names=frozenset({"git_diff"}))
        mw.wrap_model_call(_request(_full_tools(mw)), _handler({}))
        result = mw.wrap_tool_call(_tool_call_request("read_file", {"path": "x"}), lambda _request: "executed")
        assert result == "executed"

    def test_tool_call_gate_is_noop_when_idle(self) -> None:
        mw = DeferredToolsMiddleware()
        result = mw.wrap_tool_call(
            _tool_call_request("select:git_diff", {}),
            lambda _request: "executed",
        )
        assert result == "executed"


class TestToolSearch:
    def test_search_matches_deferred_and_visible_tools(self) -> None:
        mw = DeferredToolsMiddleware(deferred_names=frozenset({"git_diff", "git_log"}))
        mw.wrap_model_call(_request(_full_tools(mw)), _handler({}))
        search_tool = next(t for t in mw.tools if t.name == "tool_search")
        result = search_tool.func(None, query="git", limit=10)  # type: ignore[arg-type]
        assert "git_diff" in result
        assert "git_log" in result
        assert "deferred" in result

    def test_search_marks_activated_tools(self) -> None:
        mw = DeferredToolsMiddleware(deferred_names=frozenset({"git_diff", "git_log"}))
        _populate(mw)
        mw._activate("git_diff")
        mw.wrap_model_call(_request(_full_tools(mw)), _handler({}))
        search_tool = next(t for t in mw.tools if t.name == "tool_search")
        result = search_tool.func(None, query="diff", limit=10)  # type: ignore[arg-type]
        assert "[active]" in result

    def test_search_no_match_lists_available_tools(self) -> None:
        mw = DeferredToolsMiddleware(deferred_names=frozenset({"git_diff"}))
        mw.wrap_model_call(_request(_full_tools(mw)), _handler({}))
        search_tool = next(t for t in mw.tools if t.name == "tool_search")
        result = search_tool.func(None, query="zzz", limit=10)  # type: ignore[arg-type]
        assert "No tools matched" in result
        assert "git_diff" in result

    def test_search_uses_extra_keywords(self) -> None:
        mw = DeferredToolsMiddleware(
            deferred_names=frozenset({"git_diff"}),
            keywords={"git_diff": ["changes", "working tree"]},
        )
        mw.wrap_model_call(_request(_full_tools(mw)), _handler({}))
        search_tool = next(t for t in mw.tools if t.name == "tool_search")
        result = search_tool.func(None, query="working tree", limit=10)  # type: ignore[arg-type]
        assert "git_diff" in result

    def test_search_respects_limit(self) -> None:
        mw = DeferredToolsMiddleware(deferred_names=frozenset({"git_diff", "git_log"}))
        mw.wrap_model_call(_request(_full_tools(mw)), _handler({}))
        search_tool = next(t for t in mw.tools if t.name == "tool_search")
        result = search_tool.func(None, query="git", limit=1)  # type: ignore[arg-type]
        assert "and 1 more" in result

    def test_search_semantic_limit_coerced(self) -> None:
        mw = DeferredToolsMiddleware(deferred_names=frozenset({"git_diff", "git_log"}))
        mw.wrap_model_call(_request(_full_tools(mw)), _handler({}))
        search_tool = next(t for t in mw.tools if t.name == "tool_search")
        result = search_tool.func(None, query="git", limit="1")  # type: ignore[arg-type]
        assert "and 1 more" in result


class TestWiring:
    def test_middleware_is_constructed_by_create_agent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import warnings

        from bog_agents import create_agent
        from bog_agents.middleware import deferred_tools as deferred_module

        constructed: list[DeferredToolsMiddleware] = []

        class SpyDeferredToolsMiddleware(DeferredToolsMiddleware):
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                super().__init__(*args, **kwargs)
                constructed.append(self)

        monkeypatch.setattr(deferred_module, "DeferredToolsMiddleware", SpyDeferredToolsMiddleware)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            create_agent(
                model="anthropic:claude-sonnet-4-6",
                tools=[git_diff, git_log, read_file],
                config=_feature_config(),
            )
        assert len(constructed) == 1
        assert constructed[0]._deferred_names == frozenset({"git_diff", "git_log"})

    def test_flag_without_names_is_noop(self) -> None:
        import warnings

        from bog_agents import create_agent

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            agent = create_agent(
                model="anthropic:claude-sonnet-4-6",
                tools=[git_diff],
                config=_feature_config(deferred=[]),
            )
        assert agent is not None

    def test_create_agent_accepts_deferred_flag(self) -> None:
        import warnings

        from bog_agents import create_agent

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            agent = create_agent(
                model="anthropic:claude-sonnet-4-6",
                tools=[git_diff, git_log, read_file],
                config=_feature_config(),
            )
        assert agent is not None


def _feature_config(*, deferred: list[str] | None = None) -> Any:
    from bog_agents import FeatureConfig

    return FeatureConfig(
        enable_deferred_tools=True,
        deferred_tools=deferred if deferred is not None else ["git_diff", "git_log"],
    )
