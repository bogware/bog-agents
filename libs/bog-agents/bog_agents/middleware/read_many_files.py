"""Tool for reading multiple files in a single call.

Feature #35: ReadManyFiles tool — reads and concatenates multiple files
or glob patterns in one invocation, reducing round-trips.
"""

from __future__ import annotations

from typing import Annotated, Any

from langchain.tools import ToolRuntime
from langchain_core.tools import BaseTool, StructuredTool

from bog_agents.backends.protocol import BACKEND_TYPES, BackendProtocol
from bog_agents.backends.utils import validate_path
from bog_agents.middleware.filesystem import FilesystemState

READ_MANY_FILES_TOOL_DESCRIPTION = """Read multiple files in a single tool call.

More efficient than calling read_file multiple times. Accepts a list of file paths
and/or glob patterns. Returns concatenated contents with clear file separators.

Each file is read with default pagination (first 100 lines). For large files,
use read_file with explicit offset/limit instead.

Example:
  read_many_files(paths=["/app/main.py", "/app/utils.py", "/app/tests/*.py"])
"""


def create_read_many_files_tool(
    backend: BACKEND_TYPES,
    get_backend: Any,
) -> BaseTool:
    """Create the read_many_files tool.

    Args:
        backend: Backend for file storage.
        get_backend: Callable to resolve the backend from a ToolRuntime.

    Returns:
        A StructuredTool for batch file reading.
    """

    def sync_read_many(
        paths: Annotated[
            list[str],
            "List of absolute file paths or glob patterns to read.",
        ],
        runtime: ToolRuntime[None, FilesystemState],
        limit: Annotated[int, "Maximum lines per file. Default 100."] = 100,
    ) -> str:
        """Read multiple files and concatenate results."""
        resolved_backend: BackendProtocol = get_backend(runtime)
        results: list[str] = []

        resolved_paths: list[str] = []
        for path in paths:
            if "*" in path or "?" in path:
                # Treat as glob pattern
                try:
                    infos = resolved_backend.glob_info(path)
                    resolved_paths.extend(fi.get("path", "") for fi in infos)
                except Exception as e:
                    results.append(f"--- Error expanding glob '{path}': {e} ---")
            else:
                resolved_paths.append(path)

        for file_path in resolved_paths[:50]:  # Cap at 50 files
            try:
                validated = validate_path(file_path)
            except ValueError as e:
                results.append(f"--- {file_path} ---\nError: {e}")
                continue

            try:
                content = resolved_backend.read(validated, offset=0, limit=limit)
                results.append(f"--- {validated} ---\n{content}")
            except Exception as e:
                results.append(f"--- {validated} ---\nError reading file: {e}")

        if not results:
            return "No files matched the provided paths/patterns."

        return "\n\n".join(results)

    async def async_read_many(
        paths: Annotated[
            list[str],
            "List of absolute file paths or glob patterns to read.",
        ],
        runtime: ToolRuntime[None, FilesystemState],
        limit: Annotated[int, "Maximum lines per file. Default 100."] = 100,
    ) -> str:
        """Read multiple files and concatenate results (async)."""
        resolved_backend: BackendProtocol = get_backend(runtime)
        results: list[str] = []

        resolved_paths: list[str] = []
        for path in paths:
            if "*" in path or "?" in path:
                try:
                    infos = await resolved_backend.aglob_info(path)
                    resolved_paths.extend(fi.get("path", "") for fi in infos)
                except Exception as e:
                    results.append(f"--- Error expanding glob '{path}': {e} ---")
            else:
                resolved_paths.append(path)

        for file_path in resolved_paths[:50]:
            try:
                validated = validate_path(file_path)
            except ValueError as e:
                results.append(f"--- {file_path} ---\nError: {e}")
                continue

            try:
                content = await resolved_backend.aread(validated, offset=0, limit=limit)
                results.append(f"--- {validated} ---\n{content}")
            except Exception as e:
                results.append(f"--- {validated} ---\nError reading file: {e}")

        if not results:
            return "No files matched the provided paths/patterns."

        return "\n\n".join(results)

    return StructuredTool.from_function(
        name="read_many_files",
        description=READ_MANY_FILES_TOOL_DESCRIPTION,
        func=sync_read_many,
        coroutine=async_read_many,
    )
