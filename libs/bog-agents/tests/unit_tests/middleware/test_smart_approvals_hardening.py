"""Hardening tests for `SmartApprovalsMiddleware` tool-call gating (P2).

The middleware previously only had a passthrough `awrap_model_call`, so its
guardian evaluation logic had zero callers and every tool ran unimpeded
(fail-open). These tests prove the wrap_tool_call / awrap_tool_call hooks now
fail CLOSED: denied / escalated calls never execute the tool body, while
auto-approved calls pass straight through to the handler.
"""

from __future__ import annotations

from typing import Any

from langchain.tools.tool_node import ToolCallRequest
from langchain_core.messages import ToolMessage

from bog_agents.middleware.smart_approvals import (
    ApprovalPolicy,
    RiskLevel,
    SmartApprovalsMiddleware,
)


def _make_request(tool_name: str, args: dict[str, Any], tool_call_id: str = "call_1") -> ToolCallRequest:
    """Build a real `ToolCallRequest` carrying just the tool_call payload.

    The middleware only reads `request.tool_call`, so `tool`, `state`, and
    `runtime` are left as None.

    Args:
        tool_name: Name of the tool being called.
        args: Tool-call arguments.
        tool_call_id: Identifier for the tool call.

    Returns:
        A `ToolCallRequest` instance.
    """
    return ToolCallRequest(
        tool_call={"name": tool_name, "args": args, "id": tool_call_id, "type": "tool_call"},
        tool=None,
        state=None,
        runtime=None,  # type: ignore[arg-type]
    )


class _Spy:
    """Records whether the downstream handler was invoked."""

    def __init__(self) -> None:
        self.called = False

    def sync(self, request: ToolCallRequest) -> ToolMessage:
        self.called = True
        tool_call = request.tool_call or {}
        return ToolMessage(content="ran", tool_call_id=str(tool_call.get("id", "")), name=str(tool_call.get("name", "")))

    async def asnyc(self, request: ToolCallRequest) -> ToolMessage:
        self.called = True
        tool_call = request.tool_call or {}
        return ToolMessage(content="ran", tool_call_id=str(tool_call.get("id", "")), name=str(tool_call.get("name", "")))


# ---------------------------------------------------------------------------
# Auto-approved calls pass through (handler runs)
# ---------------------------------------------------------------------------


def test_auto_approved_safe_tool_passes_through_sync() -> None:
    mw = SmartApprovalsMiddleware()
    spy = _Spy()
    result = mw.wrap_tool_call(_make_request("read_file", {"path": "/a"}), spy.sync)
    assert spy.called is True
    assert isinstance(result, ToolMessage)
    assert result.content == "ran"
    assert result.status != "error"


async def test_auto_approved_safe_tool_passes_through_async() -> None:
    mw = SmartApprovalsMiddleware()
    spy = _Spy()
    result = await mw.awrap_tool_call(_make_request("grep", {"pattern": "x"}), spy.asnyc)
    assert spy.called is True
    assert isinstance(result, ToolMessage)
    assert result.content == "ran"


# ---------------------------------------------------------------------------
# Escalated calls are BLOCKED (handler never runs, fail CLOSED)
# ---------------------------------------------------------------------------


def test_escalated_medium_tool_is_blocked_sync() -> None:
    mw = SmartApprovalsMiddleware()
    spy = _Spy()
    # git_push is MEDIUM with no auto_approve and no history -> guardian escalates.
    result = mw.wrap_tool_call(_make_request("git_push", {}, tool_call_id="gp"), spy.sync)
    assert spy.called is False  # tool body must NOT run
    assert isinstance(result, ToolMessage)
    assert result.status == "error"
    assert result.tool_call_id == "gp"
    assert result.name == "git_push"


async def test_escalated_medium_tool_is_blocked_async() -> None:
    mw = SmartApprovalsMiddleware()
    spy = _Spy()
    result = await mw.awrap_tool_call(_make_request("git_push", {}), spy.asnyc)
    assert spy.called is False
    assert isinstance(result, ToolMessage)
    assert result.status == "error"


def test_dangerous_shell_arg_escalates_to_high_and_blocks() -> None:
    """`execute` is MEDIUM, but a destructive command arg escalates to HIGH,
    crossing `require_human_above` and forcing a fail-closed block."""
    mw = SmartApprovalsMiddleware()
    spy = _Spy()
    result = mw.wrap_tool_call(_make_request("execute", {"command": "rm -rf /"}), spy.sync)
    assert spy.called is False
    assert isinstance(result, ToolMessage)
    assert result.status == "error"
    assert "human approval" in result.content.lower()


def test_high_risk_custom_policy_is_blocked() -> None:
    mw = SmartApprovalsMiddleware(
        policies=[ApprovalPolicy(tool_pattern=r"^danger$", risk_level=RiskLevel.CRITICAL)],
    )
    spy = _Spy()
    result = mw.wrap_tool_call(_make_request("danger", {}), spy.sync)
    assert spy.called is False
    assert isinstance(result, ToolMessage)
    assert result.status == "error"


def test_unknown_tool_defaults_to_blocked() -> None:
    """An unmatched tool classifies as MEDIUM with no history -> guardian review
    -> blocked. Fail CLOSED for tools nobody whitelisted."""
    mw = SmartApprovalsMiddleware()
    spy = _Spy()
    result = mw.wrap_tool_call(_make_request("totally_unknown_tool", {}), spy.sync)
    assert spy.called is False
    assert isinstance(result, ToolMessage)
    assert result.status == "error"


# ---------------------------------------------------------------------------
# Blocked decisions are recorded in history
# ---------------------------------------------------------------------------


def test_block_is_recorded_in_history() -> None:
    mw = SmartApprovalsMiddleware()
    mw.wrap_tool_call(_make_request("git_push", {}), _Spy().sync)
    assert len(mw.history.decisions) >= 1
    last = mw.history.decisions[-1]
    assert last.approved is False
    assert last.tool_name == "git_push"
