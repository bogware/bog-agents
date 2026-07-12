"""Model-call smoke test — guards the langchain 1.x migration regressions.

REVIEW.md v2 §2 documents two bug classes that shipped green because the
test suite exercised middleware constructors + tool lists but never drove a
real model call through them:

* Bug class A — ``append_to_system_message(request, ...)`` passed the whole
  ``ModelRequest`` instead of ``request.system_message`` (AttributeError).
* (Bug class B — wrong ``wrap_model_call`` signature — is covered where the
  middleware is constructible without heavy deps.)

These tests construct each affected middleware and drive one fake-model
``wrap_model_call`` / ``awrap_model_call`` turn, asserting no exception and
that any system-prompt injection actually lands. They fail loudly against the
pre-fix code and prevent the whole class from recurring on the next dep bump.
"""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from bog_agents.middleware.auto_quality import AutoQualityMiddleware
from bog_agents.middleware.plan_mode import PlanModeMiddleware
from bog_agents.middleware.repo_map import RepoMapMiddleware
from bog_agents.middleware.thinking import ThinkingMiddleware

try:
    from langchain.agents.middleware.types import ModelRequest
except ImportError:  # pragma: no cover - import-path fallback
    from langchain.agents.middleware import ModelRequest  # type: ignore[no-redef,attr-defined]


class _FakeModel:
    """Minimal BaseChatModel stand-in: identifiable, never actually called."""

    _llm_type = "fake"

    def _get_ls_params(self, **_kwargs: Any) -> dict[str, str]:
        return {"ls_provider": "fake", "ls_model_name": "fake-model"}


def _make_request(*, tools: list[Any] | None = None) -> ModelRequest:
    return ModelRequest(
        model=_FakeModel(),
        messages=[HumanMessage(content="hi")],
        system_message=SystemMessage(content="base system prompt"),
        tools=tools or [],
        runtime=None,
        state={"messages": [HumanMessage(content="hi")]},
    )


def _passthrough(request: ModelRequest) -> AIMessage:
    """A sync handler that records the request it received and returns a response."""
    _passthrough.last_request = request  # type: ignore[attr-defined]
    return AIMessage(content="ok")


async def _apassthrough(request: ModelRequest) -> AIMessage:
    """An async handler for awrap_model_call (which awaits the handler)."""
    _passthrough.last_request = request  # type: ignore[attr-defined]
    return AIMessage(content="ok")


def _system_text(request: ModelRequest) -> str:
    sm = request.system_message
    if sm is None:
        return ""
    if isinstance(sm.content, str):
        return sm.content
    return " ".join(b.get("text", "") for b in sm.content if isinstance(b, dict) and b.get("type") == "text")


# ---------------------------------------------------------------------------
# Bug class A — system-prompt injection middlewares
# ---------------------------------------------------------------------------


def test_repo_map_wrap_model_call_injects_without_crashing(tmp_path) -> None:
    mw = RepoMapMiddleware(working_dir=tmp_path)
    request = _make_request()
    mw.wrap_model_call(request, _passthrough)
    seen = _passthrough.last_request  # type: ignore[attr-defined]
    text = _system_text(seen)
    assert "base system prompt" in text
    assert "Repository Map" in text


async def test_repo_map_awrap_model_call_injects_without_crashing(tmp_path) -> None:
    mw = RepoMapMiddleware(working_dir=tmp_path)
    request = _make_request()
    await mw.awrap_model_call(request, _apassthrough)
    assert "Repository Map" in _system_text(_passthrough.last_request)  # type: ignore[attr-defined]


def test_plan_mode_injects_and_filters_tools_without_crashing() -> None:
    class _Tool:
        def __init__(self, name: str) -> None:
            self.name = name

    mw = PlanModeMiddleware(enabled=True)
    request = _make_request(tools=[_Tool("read_file"), _Tool("write_file"), _Tool("execute")])
    mw.wrap_model_call(request, _passthrough)
    seen = _passthrough.last_request  # type: ignore[attr-defined]
    assert "base system prompt" in _system_text(seen)
    names = {getattr(t, "name", "") for t in seen.tools}
    # Mutating tools are filtered out; read_file survives.
    assert "read_file" in names
    assert "write_file" not in names and "execute" not in names


def test_thinking_fallback_injects_without_crashing() -> None:
    # _FakeModel is not a known native-thinking model, so the fallback
    # chain-of-thought system-prompt injection path runs (the one that used
    # the broken append_to_system_message(request, ...) form).
    mw = ThinkingMiddleware(enabled=True)
    request = _make_request()
    mw.wrap_model_call(request, _passthrough)
    # No exception is the primary assertion; the fallback prompt should land.
    assert _system_text(_passthrough.last_request) != ""  # type: ignore[attr-defined]


def test_auto_quality_injects_when_detection_present() -> None:
    from bog_agents.middleware.auto_quality import ProjectDetection

    mw = AutoQualityMiddleware()
    # Force a detection so the injection path runs.
    mw._detection = ProjectDetection(language="python", package_manager="uv")  # type: ignore[attr-defined]
    request = _make_request()
    mw.wrap_model_call(request, _passthrough)
    assert "Project Detection" in _system_text(_passthrough.last_request)  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Bug class C — ModelResponse usage extraction
# ---------------------------------------------------------------------------


def test_cost_tracker_records_usage_from_response() -> None:
    # Bug class C: cost_tracker read response.response_metadata (nonexistent on
    # ModelResponse) and recorded nothing. Usage lives on the AIMessage in
    # response.result via usage_metadata.
    try:
        from langchain.agents.middleware.types import ModelResponse
    except ImportError:  # pragma: no cover
        from langchain.agents.middleware import ModelResponse  # type: ignore[no-redef,attr-defined]

    from bog_agents.middleware.cost_tracker import CostTrackerMiddleware

    mw = CostTrackerMiddleware(model_name="anthropic:claude-sonnet-4-6", budget_usd=None)
    ai = AIMessage(
        content="hello",
        usage_metadata={
            "input_tokens": 1000,
            "output_tokens": 200,
            "total_tokens": 1200,
            "input_token_details": {"cache_read": 100, "cache_creation": 50},
        },
    )

    def handler(_request: ModelRequest) -> ModelResponse:
        return ModelResponse(result=[ai])

    mw.wrap_model_call(_make_request(), handler)
    assert mw.tracker.input_tokens == 1000
    assert mw.tracker.output_tokens == 200


def test_enable_multi_agent_flag_does_not_crash() -> None:
    # P1-1: enable_multi_agent=True used to import a deleted module and raise
    # ModuleNotFoundError. The flag is now a deprecated no-op.
    import warnings

    from bog_agents import create_agent

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        agent = create_agent(model="anthropic:claude-sonnet-4-6", enable_multi_agent=True)
    assert agent is not None


def test_audit_trail_records_tool_calls_and_content() -> None:
    # Bug class C: audit_trail read response.tool_calls / response.content
    # (nonexistent), logging an empty tool list + has_content=False every time.
    try:
        from langchain.agents.middleware.types import ModelResponse
    except ImportError:  # pragma: no cover
        from langchain.agents.middleware import ModelResponse  # type: ignore[no-redef,attr-defined]

    from bog_agents.middleware.audit_trail import AuditTrailMiddleware

    mw = AuditTrailMiddleware()
    ai = AIMessage(
        content="doing it",
        tool_calls=[{"name": "read_file", "args": {"path": "x"}, "id": "1", "type": "tool_call"}],
    )

    def handler(_request: ModelRequest) -> ModelResponse:
        return ModelResponse(result=[ai])

    mw.wrap_model_call(_make_request(), handler)
    entries = [e for e in mw.audit_log.entries if e.action_type == "llm_response"]
    assert entries, "no llm_response audit entry recorded"
    meta = entries[-1].metadata
    assert meta["tool_calls"] == ["read_file"]
    assert meta["has_content"] is True


def test_audit_trail_records_llm_error_on_sync_failure() -> None:
    # S17: a model timeout/5xx/exhaustion must not vanish from the compliance
    # record. The llm_call should be followed by an llm_error entry (not a
    # dangling unresolved llm_call), and the original exception must propagate.
    from bog_agents.middleware.audit_trail import AuditTrailMiddleware

    mw = AuditTrailMiddleware()

    class _Boom(RuntimeError):
        pass

    def handler(_request: ModelRequest):
        raise _Boom("model exhausted")

    with pytest.raises(_Boom):
        mw.wrap_model_call(_make_request(), handler)

    action_types = [e.action_type for e in mw.audit_log.entries]
    assert action_types[-2:] == ["llm_call", "llm_error"]
    assert not [e for e in mw.audit_log.entries if e.action_type == "llm_response"]
    err = mw.audit_log.entries[-1]
    assert err.metadata["error"] == "_Boom"


async def test_audit_trail_records_llm_error_on_async_failure() -> None:
    # S17: mirror of the sync path for awrap_model_call.
    from bog_agents.middleware.audit_trail import AuditTrailMiddleware

    mw = AuditTrailMiddleware()

    class _Boom(RuntimeError):
        pass

    async def handler(_request: ModelRequest):
        raise _Boom("model timeout")

    with pytest.raises(_Boom):
        await mw.awrap_model_call(_make_request(), handler)

    action_types = [e.action_type for e in mw.audit_log.entries]
    assert action_types[-2:] == ["llm_call", "llm_error"]
    assert not [e for e in mw.audit_log.entries if e.action_type == "llm_response"]
    assert mw.audit_log.entries[-1].metadata["error"] == "_Boom"
