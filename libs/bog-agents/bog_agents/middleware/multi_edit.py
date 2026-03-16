"""Middleware providing a multi_edit_file tool for batch edits in one call.

Feature #1: MultiEdit tool — allows multiple edits to one or more files
in a single tool invocation, reducing token waste from sequential edit_file calls.
"""

from __future__ import annotations

from typing import Annotated, Any

from langchain.tools import ToolRuntime
from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool, StructuredTool
from langgraph.types import Command
from typing_extensions import TypedDict

from bog_agents.backends.protocol import BACKEND_TYPES, BackendProtocol, EditResult
from bog_agents.backends.utils import validate_path
from bog_agents.middleware.filesystem import FilesystemState

MULTI_EDIT_FILE_TOOL_DESCRIPTION = """Perform multiple edits to one or more files in a single tool call.

This is more efficient than calling edit_file multiple times. Each edit is an object with:
- file_path: Absolute path to the file
- old_string: The exact text to find
- new_string: The replacement text
- replace_all (optional): If True, replace all occurrences (default False)

Edits are applied sequentially in the order provided. Later edits see the results
of earlier edits, so you can chain dependent replacements within the same file.

You must read each file before editing it. All file paths must be absolute.

Example:
  multi_edit_file(edits=[
    {"file_path": "/app/main.py", "old_string": "def foo():", "new_string": "def bar():"},
    {"file_path": "/app/main.py", "old_string": "foo()", "new_string": "bar()"},
    {"file_path": "/app/utils.py", "old_string": "import foo", "new_string": "import bar"},
  ])
"""


class EditOperation(TypedDict):
    """A single edit operation within a multi-edit batch."""

    file_path: str
    old_string: str
    new_string: str
    replace_all: bool


def create_multi_edit_file_tool(
    backend: BACKEND_TYPES,
    get_backend: Any,
) -> BaseTool:
    """Create the multi_edit_file tool.

    Args:
        backend: Backend for file storage.
        get_backend: Callable to resolve the backend from a ToolRuntime.

    Returns:
        A StructuredTool for multi-file editing.
    """

    def sync_multi_edit(
        edits: Annotated[
            list[dict[str, Any]],
            "List of edit operations. Each has: file_path, old_string, new_string, and optional replace_all (bool).",
        ],
        runtime: ToolRuntime[None, FilesystemState],
    ) -> Command | str:
        """Apply multiple edits sequentially."""
        resolved_backend: BackendProtocol = get_backend(runtime)
        results: list[str] = []
        all_files_updates: dict[str, Any] = {}
        errors: list[str] = []

        for i, edit in enumerate(edits):
            file_path = edit.get("file_path", "")
            old_string = edit.get("old_string", "")
            new_string = edit.get("new_string", "")
            replace_all = bool(edit.get("replace_all", False))

            try:
                validated_path = validate_path(file_path)
            except ValueError as e:
                errors.append(f"Edit {i + 1}: Error validating path '{file_path}': {e}")
                continue

            res: EditResult = resolved_backend.edit(validated_path, old_string, new_string, replace_all=replace_all)
            if res.error:
                errors.append(f"Edit {i + 1} ({validated_path}): {res.error}")
                continue

            results.append(f"Edit {i + 1}: Replaced {res.occurrences} instance(s) in '{res.path}'")
            if res.files_update is not None:
                all_files_updates.update(res.files_update)

        summary_parts = results + errors
        summary = "\n".join(summary_parts) if summary_parts else "No edits performed."

        if all_files_updates:
            return Command(
                update={
                    "files": all_files_updates,
                    "messages": [
                        ToolMessage(
                            content=summary,
                            tool_call_id=runtime.tool_call_id,
                        )
                    ],
                }
            )
        return summary

    async def async_multi_edit(
        edits: Annotated[
            list[dict[str, Any]],
            "List of edit operations. Each has: file_path, old_string, new_string, and optional replace_all (bool).",
        ],
        runtime: ToolRuntime[None, FilesystemState],
    ) -> Command | str:
        """Apply multiple edits sequentially (async)."""
        resolved_backend: BackendProtocol = get_backend(runtime)
        results: list[str] = []
        all_files_updates: dict[str, Any] = {}
        errors: list[str] = []

        for i, edit in enumerate(edits):
            file_path = edit.get("file_path", "")
            old_string = edit.get("old_string", "")
            new_string = edit.get("new_string", "")
            replace_all = bool(edit.get("replace_all", False))

            try:
                validated_path = validate_path(file_path)
            except ValueError as e:
                errors.append(f"Edit {i + 1}: Error validating path '{file_path}': {e}")
                continue

            res: EditResult = await resolved_backend.aedit(validated_path, old_string, new_string, replace_all=replace_all)
            if res.error:
                errors.append(f"Edit {i + 1} ({validated_path}): {res.error}")
                continue

            results.append(f"Edit {i + 1}: Replaced {res.occurrences} instance(s) in '{res.path}'")
            if res.files_update is not None:
                all_files_updates.update(res.files_update)

        summary_parts = results + errors
        summary = "\n".join(summary_parts) if summary_parts else "No edits performed."

        if all_files_updates:
            return Command(
                update={
                    "files": all_files_updates,
                    "messages": [
                        ToolMessage(
                            content=summary,
                            tool_call_id=runtime.tool_call_id,
                        )
                    ],
                }
            )
        return summary

    return StructuredTool.from_function(
        name="multi_edit_file",
        description=MULTI_EDIT_FILE_TOOL_DESCRIPTION,
        func=sync_multi_edit,
        coroutine=async_multi_edit,
    )
