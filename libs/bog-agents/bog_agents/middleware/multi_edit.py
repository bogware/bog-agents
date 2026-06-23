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


def _prior_file_data(all_files_updates: dict[str, Any], path: str) -> dict[str, Any] | None:
    """Return the working FileData for a path if an earlier edit already touched it.

    Chained edits to the same file must see earlier edits. State-backed stores
    are not mutated until the batch's Command is returned, so the only place an
    in-flight edit's result lives is the accumulated ``all_files_updates`` map.

    Args:
        all_files_updates: Accumulated ``{path: file_data}`` updates so far.
        path: The validated file path about to be edited.

    Returns:
        The prior FileData dict if present and dict-shaped, otherwise None
        (signalling the backend should read its own canonical copy).
    """
    prior = all_files_updates.get(path)
    return prior if isinstance(prior, dict) else None


def _backend_edit_sync(
    backend: BackendProtocol,
    path: str,
    old_string: str,
    new_string: str,
    replace_all: bool,
    base_content: dict[str, Any] | None,
) -> EditResult:
    """Call ``backend.edit`` threading ``base_content`` when the backend accepts it.

    Older/custom backends may not accept the ``base_content`` keyword. They are
    typically on-disk backends whose store already reflects prior edits, so we
    fall back to a plain call rather than failing the batch.
    """
    if base_content is None:
        return backend.edit(path, old_string, new_string, replace_all=replace_all)
    try:
        return backend.edit(path, old_string, new_string, replace_all=replace_all, base_content=base_content)
    except TypeError:
        return backend.edit(path, old_string, new_string, replace_all=replace_all)


async def _backend_edit_async(
    backend: BackendProtocol,
    path: str,
    old_string: str,
    new_string: str,
    replace_all: bool,
    base_content: dict[str, Any] | None,
) -> EditResult:
    """Async twin of `_backend_edit_sync`."""
    if base_content is None:
        return await backend.aedit(path, old_string, new_string, replace_all=replace_all)
    try:
        return await backend.aedit(path, old_string, new_string, replace_all=replace_all, base_content=base_content)
    except TypeError:
        return await backend.aedit(path, old_string, new_string, replace_all=replace_all)


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

            # Thread the result of any earlier edit to this same file forward so
            # chained edits compose (state-backed stores are not mutated until the
            # batch returns, so re-reading would discard intermediate edits).
            base_content = _prior_file_data(all_files_updates, validated_path)
            try:
                res: EditResult = _backend_edit_sync(resolved_backend, validated_path, old_string, new_string, replace_all, base_content)
            except Exception as e:
                errors.append(f"Edit {i + 1} ({validated_path}): {e}")
                continue
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
                            status="error" if errors else "success",
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

            # Thread the result of any earlier edit to this same file forward so
            # chained edits compose (state-backed stores are not mutated until the
            # batch returns, so re-reading would discard intermediate edits).
            base_content = _prior_file_data(all_files_updates, validated_path)
            try:
                res: EditResult = await _backend_edit_async(resolved_backend, validated_path, old_string, new_string, replace_all, base_content)
            except Exception as e:
                errors.append(f"Edit {i + 1} ({validated_path}): {e}")
                continue
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
                            status="error" if errors else "success",
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
