"""Tests for PreToolUse hook enforcement middleware (hook-bus completion)."""

from __future__ import annotations

import sys
from pathlib import Path

from langchain.tools.tool_node import ToolCallRequest
from langchain_core.messages import ToolMessage

from bog_agents_cli.hook_middleware import PreToolUseHookMiddleware

PY = sys.executable


def _req(name: str = "execute", command: str = "rm -rf /") -> ToolCallRequest:
    return ToolCallRequest(
        tool_call={"name": name, "args": {"command": command}, "id": "t1"},
        tool=None,
        state={},
        runtime=None,  # type: ignore[arg-type]
    )


def _deny_hook(tmp_path: Path, *, matcher: str = "execute") -> dict:
    script = tmp_path / "deny.py"
    script.write_text(
        'print(\'{"decision":"deny","reason":"policy X"}\')', encoding="utf-8"
    )
    return {"command": [PY, str(script)], "events": ["PreToolUse"], "matcher": matcher}


def _handler_ok(_request: ToolCallRequest) -> ToolMessage:
    return ToolMessage(content="ran", tool_call_id="t1", name="execute")


class TestPreToolUseHookMiddleware:
    def test_deny_blocks_and_does_not_run_handler(self, tmp_path: Path) -> None:
        called = {"handler": False}

        def handler(_r: ToolCallRequest) -> ToolMessage:
            called["handler"] = True
            return ToolMessage(content="ran", tool_call_id="t1", name="execute")

        mw = PreToolUseHookMiddleware([_deny_hook(tmp_path)])
        result = mw.wrap_tool_call(_req(), handler)
        assert isinstance(result, ToolMessage)
        assert result.status == "error"
        assert "PreToolUse hook" in result.content and "policy X" in result.content
        assert called["handler"] is False  # handler never ran

    def test_allow_passes_through_to_handler(self, tmp_path: Path) -> None:
        # A hook matched to a different tool → this call is allowed.
        mw = PreToolUseHookMiddleware([_deny_hook(tmp_path, matcher="read_file")])
        result = mw.wrap_tool_call(_req(name="execute"), _handler_ok)
        assert result.content == "ran"

    def test_no_hooks_passes_through(self) -> None:
        mw = PreToolUseHookMiddleware([])
        assert mw.wrap_tool_call(_req(), _handler_ok).content == "ran"

    async def test_async_deny_blocks(self, tmp_path: Path) -> None:
        async def ahandler(_r: ToolCallRequest) -> ToolMessage:
            return ToolMessage(content="ran", tool_call_id="t1", name="execute")

        mw = PreToolUseHookMiddleware([_deny_hook(tmp_path)])
        result = await mw.awrap_tool_call(_req(), ahandler)
        assert result.status == "error"
        assert "policy X" in result.content

    async def test_async_allow_passes_through(self, tmp_path: Path) -> None:
        async def ahandler(_r: ToolCallRequest) -> ToolMessage:
            return ToolMessage(content="ran-async", tool_call_id="t1", name="execute")

        mw = PreToolUseHookMiddleware([_deny_hook(tmp_path, matcher="read_file")])
        result = await mw.awrap_tool_call(_req(name="execute"), ahandler)
        assert result.content == "ran-async"
