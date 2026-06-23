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
from bog_agents.backends.state import StateBackend
from bog_agents.backends.utils import create_file_data, file_data_to_string
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


# --- P10: a partially-failed batch must surface status="error" ---


def test_sync_error_status_set_when_any_edit_fails() -> None:
    backend = _RaisingBackend(raise_on="/boom.txt")
    tool = create_multi_edit_file_tool(backend, lambda _runtime: backend)

    result = tool.func(edits=_edits(), runtime=_runtime())

    assert isinstance(result, Command)
    # The model must not believe a partially-failed batch succeeded.
    assert result.update["messages"][0].status == "error"


async def test_async_error_status_set_when_any_edit_fails() -> None:
    backend = _RaisingBackend(raise_on="/boom.txt")
    tool = create_multi_edit_file_tool(backend, lambda _runtime: backend)

    result = await tool.coroutine(edits=_edits(), runtime=_runtime())

    assert isinstance(result, Command)
    assert result.update["messages"][0].status == "error"


def test_all_success_keeps_status_success() -> None:
    backend = _RaisingBackend(raise_on="/never")
    tool = create_multi_edit_file_tool(backend, lambda _runtime: backend)

    result = tool.func(edits=_edits(), runtime=_runtime())

    assert isinstance(result, Command)
    assert result.update["messages"][0].status == "success"


# --- P9: chained edits to the SAME file must see earlier edits (StateBackend) ---


def _state_runtime(files: dict | None = None) -> ToolRuntime:
    return ToolRuntime(
        state={"messages": [], "files": files or {}},
        context=None,
        tool_call_id="tc_chain",
        store=None,
        stream_writer=lambda _: None,
        config={},
    )


def test_state_backend_chained_edits_same_file_sync() -> None:
    """Two edits to one file: edit #2 must see edit #1's result.

    Regression for the silent lost-write where StateBackend re-read the
    original content for every edit (state is not mutated mid-batch) and the
    dict-keyed update overwrote edit #1 with edit #2 computed from the original.
    """
    runtime = _state_runtime({"/m.py": create_file_data("def foo():\n    foo()\n")})
    backend = StateBackend(runtime)
    tool = create_multi_edit_file_tool(backend, lambda _rt: backend)

    edits = [
        {"file_path": "/m.py", "old_string": "def foo():", "new_string": "def bar():"},
        {"file_path": "/m.py", "old_string": "    foo()", "new_string": "    bar()"},
    ]
    result = tool.func(edits=edits, runtime=runtime)

    assert isinstance(result, Command)
    final = file_data_to_string(result.update["files"]["/m.py"])
    # BOTH edits applied — not just the last-writer.
    assert "def bar():" in final
    assert "    bar()" in final
    assert "foo" not in final
    assert result.update["messages"][0].status == "success"


async def test_state_backend_chained_edits_same_file_async() -> None:
    runtime = _state_runtime({"/m.py": create_file_data("def foo():\n    foo()\n")})
    backend = StateBackend(runtime)
    tool = create_multi_edit_file_tool(backend, lambda _rt: backend)

    edits = [
        {"file_path": "/m.py", "old_string": "def foo():", "new_string": "def bar():"},
        {"file_path": "/m.py", "old_string": "    foo()", "new_string": "    bar()"},
    ]
    result = await tool.coroutine(edits=edits, runtime=runtime)

    assert isinstance(result, Command)
    final = file_data_to_string(result.update["files"]["/m.py"])
    assert "def bar():" in final
    assert "    bar()" in final
    assert "foo" not in final


def test_state_backend_three_chained_edits_same_file() -> None:
    """Three sequential dependent edits all compose."""
    runtime = _state_runtime({"/x.txt": create_file_data("a")})
    backend = StateBackend(runtime)
    tool = create_multi_edit_file_tool(backend, lambda _rt: backend)

    edits = [
        {"file_path": "/x.txt", "old_string": "a", "new_string": "b"},
        {"file_path": "/x.txt", "old_string": "b", "new_string": "c"},
        {"file_path": "/x.txt", "old_string": "c", "new_string": "d"},
    ]
    result = tool.func(edits=edits, runtime=runtime)

    assert isinstance(result, Command)
    assert file_data_to_string(result.update["files"]["/x.txt"]) == "d"
