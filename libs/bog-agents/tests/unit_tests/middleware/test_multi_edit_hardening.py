"""Hardening tests for the multi_edit_file tool (fixes S14).

A backend ``edit``/``aedit`` that raises must not crash the whole batch and
discard already-accumulated edits — the failure should be captured as an error
entry and the remaining edits should still be applied.
"""

from __future__ import annotations

from typing import Any

from langchain.tools import ToolRuntime
from langgraph.types import Command

from bog_agents.backends.protocol import EditResult
from bog_agents.middleware.multi_edit import create_multi_edit_file_tool


class _RaisingBackend:
    """Backend whose edit raises for one specific path, succeeds otherwise."""

    def __init__(self, *, raise_on: str) -> None:
        self._raise_on = raise_on

    def edit(self, path: str, old: str, new: str, *, replace_all: bool = False) -> EditResult:
        if path == self._raise_on:
            raise OSError("sandbox I/O failure")
        return EditResult(path=path, files_update={path: {"content": new}}, occurrences=1)

    async def aedit(self, path: str, old: str, new: str, *, replace_all: bool = False) -> EditResult:
        if path == self._raise_on:
            raise OSError("sandbox I/O failure")
        return EditResult(path=path, files_update={path: {"content": new}}, occurrences=1)


def _runtime() -> ToolRuntime:
    return ToolRuntime(state={}, context=None, tool_call_id="tc_1", store=None, stream_writer=lambda _: None, config={})


def _edits() -> list[dict[str, Any]]:
    return [
        {"file_path": "/a.txt", "old_string": "x", "new_string": "1"},
        {"file_path": "/boom.txt", "old_string": "y", "new_string": "2"},
        {"file_path": "/c.txt", "old_string": "z", "new_string": "3"},
    ]


def test_sync_edit_exception_does_not_crash_batch() -> None:
    backend = _RaisingBackend(raise_on="/boom.txt")
    tool = create_multi_edit_file_tool(backend, lambda _runtime: backend)

    result = tool.func(edits=_edits(), runtime=_runtime())

    assert isinstance(result, Command)
    summary = result.update["messages"][0].content
    # Surviving edits applied.
    assert "Replaced 1 instance(s) in '/a.txt'" in summary
    assert "Replaced 1 instance(s) in '/c.txt'" in summary
    # Failing edit captured as an error, not a traceback.
    assert "Edit 2 (/boom.txt): sandbox I/O failure" in summary
    # State update preserves the accumulated edits despite the mid-batch failure.
    assert set(result.update["files"]) == {"/a.txt", "/c.txt"}


async def test_async_edit_exception_does_not_crash_batch() -> None:
    backend = _RaisingBackend(raise_on="/boom.txt")
    tool = create_multi_edit_file_tool(backend, lambda _runtime: backend)

    result = await tool.coroutine(edits=_edits(), runtime=_runtime())

    assert isinstance(result, Command)
    summary = result.update["messages"][0].content
    assert "Replaced 1 instance(s) in '/a.txt'" in summary
    assert "Replaced 1 instance(s) in '/c.txt'" in summary
    assert "Edit 2 (/boom.txt): sandbox I/O failure" in summary
    assert set(result.update["files"]) == {"/a.txt", "/c.txt"}
