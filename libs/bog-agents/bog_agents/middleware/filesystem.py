"""Middleware for providing filesystem tools to an agent."""

import asyncio
import base64
import concurrent.futures
import hashlib
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Annotated, Any, Literal, NotRequired, cast

from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    ContextT,
    ModelRequest,
    ModelResponse,
    ResponseT,
)
from langchain.tools import ToolRuntime
from langchain.tools.tool_node import ToolCallRequest
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, ToolMessage
from langchain_core.messages.content import ContentBlock, create_image_block
from langchain_core.tools import BaseTool, StructuredTool
from langgraph.runtime import Runtime
from langgraph.types import Command

from bog_agents.backends import StateBackend
from bog_agents.backends.composite import CompositeBackend
from bog_agents.backends.filesystem import FilesystemBackend
from bog_agents.backends.local_shell import LocalShellBackend
from bog_agents.backends.protocol import (
    BACKEND_TYPES as BACKEND_TYPES,  # Re-export type here for backwards compatibility
    BackendProtocol,
    DeleteResult,
    EditResult,
    FileData as FileData,  # Re-export the canonical (v2) FileData for backwards compatibility
    GlobResult,
    GrepResult,
    LsResult,
    SandboxBackendProtocol,
    WriteResult,
    execute_accepts_timeout,
    supports_delete,
)
from bog_agents.backends.utils import (
    format_grep_matches,
    regex_literal_hint,
    sanitize_tool_call_id,
    truncate_if_too_long,
    validate_path,
)
from bog_agents.middleware._message_eviction import (
    TOO_LARGE_TOOL_MSG,
    _build_evicted_content,
    _create_content_preview,
    _extract_text_from_message,
)
from bog_agents.middleware._utils import append_to_system_message
from bog_agents.middleware.permissions import (
    FilesystemPermission,
    _check_fs_permission,
    _find_delete_deny_patterns,
    apply_permissions_to_glob_result,
    apply_permissions_to_grep_result,
    apply_permissions_to_ls_result,
)

EMPTY_CONTENT_WARNING = "System reminder: File exists but has empty contents"
GLOB_TIMEOUT = 20.0  # seconds
LINE_NUMBER_WIDTH = 6
DEFAULT_READ_OFFSET = 0
DEFAULT_READ_LIMIT = 100
IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp"})
IMAGE_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

# Video container extensions the read_file tool samples into frames when the
# optional `[video]` extra is installed. Mirrors upstream deepagents' combined
# `_EXTENSION_TO_FILE_TYPE` video set plus `_VIDEO_EXTRA_EXTENSIONS` (`.mkv`).
VIDEO_EXTENSIONS = frozenset(
    {
        ".mp4",
        ".mpeg",
        ".mpg",
        ".mov",
        ".avi",
        ".flv",
        ".webm",
        ".wmv",
        ".3gpp",
        ".mkv",
    }
)

# Seconds between sampled frames when extracting stills from a video. The
# sampling rate is fixed by the middleware; agents control the window they
# inspect through read_file's existing offset/limit arguments (interpreted as
# seconds for video reads). Mirrors upstream deepagents' `_VIDEO_SAMPLING_RATE`.
_VIDEO_SAMPLING_RATE = 0.5

# Maximum raw video payload size accepted by read_file frame extraction.
# Mirrors upstream deepagents' `MAX_VIDEO_INPUT_BYTES`.
MAX_VIDEO_INPUT_BYTES = 1024 * 1024 * 1024


# Template for truncation message in read_file
# {file_path} will be filled in at runtime
READ_FILE_TRUNCATION_MSG = (
    "\n\n[Output was truncated due to size limits. "
    "The file content is very large. "
    "Consider reformatting the file to make it easier to navigate. "
    "For example, if this is JSON, use execute(command='jq . {file_path}') to pretty-print it with line breaks. "
    "For other formats, you can use appropriate formatting tools to split long lines.]"
)

# Approximate number of characters per token for truncation calculations.
# Using 4 chars per token as a conservative approximation (actual ratio varies by content)
# This errs on the high side to avoid premature eviction of content that might fit
NUM_CHARS_PER_TOKEN = 4

# Tool names that mutate file contents. Two of these targeting the same resolved
# file_path within a single AIMessage race each other: each reads the same base
# version and the last-writer-wins reducer silently clobbers the others. The
# after_model guard below rejects all-but-one such call and asks the model to
# sequence them across turns instead.
#
# `delete` is included so HITL / SafeToolsMiddleware gate it alongside the other
# mutating tools, and so a `delete` racing a `write_file` on the same path in one
# turn is rejected rather than resolved by whichever reducer write lands last.
_WRITE_CLASS_TOOL_NAMES = frozenset({"write_file", "edit_file", "multi_edit_file", "delete"})

# Message returned to the model for a conflicting write-class tool call that was
# rejected because another write-class call in the same turn already targets the
# same file.
_PARALLEL_WRITE_CONFLICT_MSG = (
    "Error: Multiple file-mutating tool calls in this turn target the same file "
    "'{file_path}'. Parallel edits/writes to one file race each other and the last "
    "writer silently overwrites the others, so this call was not executed. Re-issue "
    "this change in a follow-up turn, after the first edit to '{file_path}' has been "
    "applied, so the edits are sequenced correctly."
)


def _video_window_header(path: str, offset_seconds: float, duration_seconds: float, rate: float) -> str:
    """Render the model-facing text header introducing a sampled frame window.

    Args:
        path: Path of the video being read (for display).
        offset_seconds: Seconds into the source at which sampling starts.
        duration_seconds: Seconds of source sampled.
        rate: Sampling rate in frames per second.

    Returns:
        A human-readable one-line description of the sampled window.
    """
    end = offset_seconds + duration_seconds
    if offset_seconds <= 0.0:
        return f"Reading first {int(duration_seconds)}s of {path} at {rate} fps."
    return f"Reading [{offset_seconds:.3f}s, {end:.3f}s) of {path} at {rate} fps."


def _handle_video_read(
    validated_path: str,
    content: bytes,
    tool_call_id: str | None,
    offset: int,
    limit: int,
) -> ToolMessage | str:
    """Slice a video byte payload into a sampled frame window for the model.

    `offset` is reinterpreted as seconds into the source to skip and `limit` as
    seconds of source to sample, mirroring upstream deepagents' read_file video
    window. The agent's supplied `limit` is authoritative (no per-call upper
    clamp); a non-positive value is rejected as a tool error. Output volume is
    bounded by the layered caps on the extractor (`MAX_VIDEO_DECODE_SECONDS`,
    `MAX_VIDEO_SAMPLED_FRAMES`, `MAX_VIDEO_EMITTED_BYTES`, `MAX_VIDEO_FRAME_PIXELS`,
    `MAX_VIDEO_FRAME_SIDE`).

    The sampled frames are returned as image content blocks preceded by a text
    window header on a single success `ToolMessage`, matching the read_file image
    branch's shape. Errors are returned as plain error strings so the turn still
    completes and the agent can recover (e.g. by retrying with a smaller window).

    Args:
        validated_path: The validated path of the video being read.
        content: Raw bytes of the video payload (from the backend download).
        tool_call_id: The tool call id to stamp on the result message.
        offset: Seconds into the source to skip before sampling.
        limit: Seconds of source to sample.

    Returns:
        A success `ToolMessage` carrying the header and sampled frame blocks, or
        an error string when the window is invalid or extraction fails.
    """
    # Lazy import keeps `av` / Pillow optional and the lazy-import graph clean.
    from bog_agents.middleware.video_reader import VideoExtractionError, extract_video_frames

    if limit <= 0:
        return f"Error reading video {validated_path}: limit must be > 0, got {limit!r}"

    rate = _VIDEO_SAMPLING_RATE
    offset_seconds = max(0.0, float(offset))
    duration_seconds = float(limit)
    header = _video_window_header(validated_path, offset_seconds, duration_seconds, rate)

    if len(content) > MAX_VIDEO_INPUT_BYTES:
        return f"Error reading video {validated_path}: video payload exceeds maximum input size of {MAX_VIDEO_INPUT_BYTES} bytes\n{header}"

    try:
        blocks = extract_video_frames(
            content,
            offset_seconds=offset_seconds,
            duration_seconds=duration_seconds,
            sampling_rate=rate,
        )
    except VideoExtractionError as exc:
        return f"Error reading video {validated_path}: {exc}\n{header}"

    frame_count = sum(1 for block in blocks if isinstance(block, dict) and block.get("type") == "image")
    content_blocks: list[ContentBlock] = [cast("ContentBlock", {"type": "text", "text": header}), *blocks]
    return ToolMessage(
        content_blocks=content_blocks,
        name="read_file",
        tool_call_id=tool_call_id,
        additional_kwargs={
            "read_file_path": validated_path,
            "read_file_frame_count": frame_count,
        },
    )


def _write_target_paths(tool_call: dict[str, Any]) -> set[str]:
    """Resolve the file paths a write-class tool call would mutate.

    Args:
        tool_call: A tool-call dict from `AIMessage.tool_calls` (has `name`, `args`).

    Returns:
        The set of resolved (normalized) file paths the call targets. Paths that
        fail validation fall back to their raw string so the guard never crashes
        on a malformed path; an empty set is returned when no path is present.
    """
    args = tool_call.get("args") or {}
    raw_paths: list[str] = []

    if tool_call.get("name") == "multi_edit_file":
        edits = args.get("edits")
        if isinstance(edits, list):
            for edit in edits:
                if isinstance(edit, dict):
                    path = edit.get("file_path")
                    if isinstance(path, str) and path:
                        raw_paths.append(path)
    else:
        path = args.get("file_path")
        if isinstance(path, str) and path:
            raw_paths.append(path)

    resolved: set[str] = set()
    for raw in raw_paths:
        try:
            resolved.add(validate_path(raw))
        except ValueError:
            # Keep the raw spelling so a bad path still participates in conflict
            # detection (and is left to the tool itself to reject).
            resolved.add(raw)
    return resolved


def _detect_parallel_write_conflicts(message: AIMessage) -> tuple[list[dict[str, Any]], list[ToolMessage]]:
    """Find write-class tool calls in one AIMessage that race on the same file.

    Within a single AIMessage, multiple `write_file`/`edit_file`/`multi_edit_file`
    calls targeting the same resolved file path each read the same base version and
    the last-writer-wins reducer silently clobbers the others. This keeps the first
    such call per file and rejects every later conflicting one.

    Reads, globs, greps, executes, and writes to *distinct* files are never affected.

    Args:
        message: The AIMessage to inspect.

    Returns:
        A tuple `(kept_tool_calls, conflict_tool_messages)`:
        - `kept_tool_calls`: the tool calls to keep on the AIMessage (non-write
          calls, plus the first write-class call per file).
        - `conflict_tool_messages`: one error ToolMessage per rejected call.

        When there is no conflict, `conflict_tool_messages` is empty and
        `kept_tool_calls` is the original list (by value).
    """
    seen_paths: set[str] = set()
    kept: list[dict[str, Any]] = []
    conflicts: list[ToolMessage] = []

    for tool_call in message.tool_calls:
        if tool_call.get("name") not in _WRITE_CLASS_TOOL_NAMES:
            kept.append(tool_call)
            continue

        targets = _write_target_paths(tool_call)
        # Only reject when this call overlaps a path already claimed this turn.
        conflicting = targets & seen_paths
        if conflicting:
            conflict_path = sorted(conflicting)[0]
            conflicts.append(
                ToolMessage(
                    content=_PARALLEL_WRITE_CONFLICT_MSG.format(file_path=conflict_path),
                    tool_call_id=tool_call["id"],
                    name=tool_call.get("name"),
                    status="error",
                )
            )
        else:
            seen_paths |= targets
            kept.append(tool_call)

    return kept, conflicts


__all__ = [
    "FileData",
    "FilesystemMiddleware",
    "FilesystemState",
    "FsToolName",
]


def _file_data_reducer(left: dict[str, FileData] | None, right: dict[str, FileData | None]) -> dict[str, FileData]:
    """Merge file updates with support for deletions.

    This reducer enables file deletion by treating `None` values in the right
    dictionary as deletion markers. It's designed to work with LangGraph's
    state management where annotated reducers control how state updates merge.

    Args:
        left: Existing files dictionary. May be `None` during initialization.
        right: New files dictionary to merge. Files with `None` values are
            treated as deletion markers and removed from the result.

    Returns:
        Merged dictionary where right overwrites left for matching keys,
        and `None` values in right trigger deletions.

    Example:
        ```python
        existing = {"/file1.txt": FileData(...), "/file2.txt": FileData(...)}
        updates = {"/file2.txt": None, "/file3.txt": FileData(...)}
        result = file_data_reducer(existing, updates)
        # Result: {"/file1.txt": FileData(...), "/file3.txt": FileData(...)}
        ```
    """
    if left is None:
        return {k: v for k, v in right.items() if v is not None}

    result = {**left}
    for key, value in right.items():
        if value is None:
            result.pop(key, None)
        else:
            result[key] = value
    return result


class FilesystemState(AgentState):
    """State for the filesystem middleware."""

    files: Annotated[NotRequired[dict[str, FileData]], _file_data_reducer]
    """Files in the filesystem."""


LIST_FILES_TOOL_DESCRIPTION = """Lists all files in a directory.

This is useful for exploring the filesystem and finding the right file to read or edit.
You should almost ALWAYS use this tool before using the read_file or edit_file tools."""

READ_FILE_TOOL_DESCRIPTION = """Reads a file from the filesystem.

Assume this tool is able to read all files. If the User provides a path to a file assume that path is valid. It is okay to read a file that does not exist; an error will be returned.

Usage:
- By default, it reads up to 100 lines starting from the beginning of the file
- **IMPORTANT for large files and codebase exploration**: Use pagination with offset and limit parameters to avoid context overflow
  - First scan: read_file(file_path="...", limit=100) to see file structure
  - Read more sections: read_file(file_path="...", offset=100, limit=200) for next 200 lines
  - Only omit limit (read full file) when necessary for editing
- Specify offset and limit: read_file(file_path="...", offset=0, limit=100) reads first 100 lines
- Results are returned using cat -n format, with line numbers starting at 1
- Lines longer than 5,000 characters will be split into multiple lines with continuation markers (e.g., 5.1, 5.2, etc.). When you specify a limit, these continuation lines count towards the limit.
- You have the capability to call multiple tools in a single response. It is always better to speculatively read multiple files as a batch that are potentially useful.
- If you read a file that exists but has empty contents you will receive a system reminder warning in place of file contents.
- Image files (`.png`, `.jpg`, `.jpeg`, `.gif`, `.webp`) are returned as multimodal image content blocks (see https://docs.langchain.com/oss/python/langchain/messages#multimodal).
- PDF files (`.pdf`) are returned as extracted text with page markers (requires the `pdf` extra: `pip install 'bog-agents[pdf]'`). Use `offset` as the 0-indexed start page to read further into a long PDF.

For image tasks:
- Use `read_file(file_path=...)` for `.png/.jpg/.jpeg/.gif/.webp`
- Do NOT use `offset`/`limit` for images (pagination is text-only)
- If image details were compacted from history, call `read_file` again on the same path

- You should ALWAYS make sure a file has been read before editing it."""

EDIT_FILE_TOOL_DESCRIPTION = """Performs exact string replacements in files.

Usage:
- You must read the file before editing. This tool will error if you attempt an edit without reading the file first.
- When editing, preserve the exact indentation (tabs/spaces) from the read output. Never include line number prefixes in old_string or new_string.
- ALWAYS prefer editing existing files over creating new ones.
- Only use emojis if the user explicitly requests it."""


WRITE_FILE_TOOL_DESCRIPTION = """Writes content to a file. Creates the file if it does not exist; replaces it entirely if it does.

Usage:
- Use this tool when you intend to create a new file or replace the whole file. You do not need to read the file first.
- Prefer to edit existing files (with the edit_file tool) over creating new ones when possible.
"""

DELETE_TOOL_DESCRIPTION = """Deletes a file or directory from the filesystem.

Usage:
- Permanently removes the file or directory at the given absolute path.
- Deleting a directory removes it and everything inside it, recursively. Prefer
  deleting a directory in one call over deleting each file individually.
- This cannot be undone, so only delete paths you are sure are no longer needed.
"""

GLOB_TOOL_DESCRIPTION = """Find files matching a glob pattern.

Supports standard glob patterns: `*` (any characters), `**` (any directories), `?` (single character).
Returns a list of absolute file paths that match the pattern.

Examples:
- `**/*.py` - Find all Python files
- `*.txt` - Find all text files in root
- `/subdir/**/*.md` - Find all markdown files under /subdir"""

# Carries its own leading newline so substituting the empty string drops the whole
# line cleanly, with no blank line left behind.
_GREP_REGEX_EXECUTE_FALLBACK = "\n- If you genuinely need regex, use the execute tool with `rg '<regex>'` instead."

_GREP_TOOL_DESCRIPTION_TEMPLATE = """Search for a LITERAL text pattern across files (NOT regex).

Returns matching files or content based on output_mode. The pattern is matched
verbatim: regex metacharacters are treated as ordinary characters, NOT operators.

Do NOT pass a regex. In particular:
- To match any of several strings, run a SEPARATE grep for each one. There is no
  `|` alternation: `grep(pattern="foo|bar")` looks for the literal text "foo|bar".
- Do not use wildcards (`.*`) or escapes (`\\.`); they match those characters literally.{execute_fallback}

Examples:
- Search all files: `grep(pattern="TODO")`
- Search Python files only: `grep(pattern="import", glob="*.py")`
- Show matching lines: `grep(pattern="error", output_mode="content")`
- Literal special chars are fine: `grep(pattern="def __init__(self):")`"""

GREP_TOOL_DESCRIPTION = _GREP_TOOL_DESCRIPTION_TEMPLATE.format(execute_fallback=_GREP_REGEX_EXECUTE_FALLBACK)
"""Grep description used when the backend supports `execute` (so `rg` is reachable)."""

GREP_TOOL_DESCRIPTION_WITHOUT_EXECUTE = _GREP_TOOL_DESCRIPTION_TEMPLATE.format(execute_fallback="")
"""Grep description used when no `execute` tool is available to fall back to."""

SEARCH_TRUNCATION_NOTE = (
    "Note: the search stopped early because it hit its time limit. The matches above are valid but incomplete. "
    "Narrow the search (a more specific pattern or a narrower path) to see the rest."
)

EXECUTE_TOOL_DESCRIPTION = """Executes a shell command in an isolated sandbox environment.

Usage:
Executes a given command in the sandbox environment with proper handling and security measures.
Before executing the command, please follow these steps:
1. Directory Verification:
   - If the command will create new directories or files, first use the ls tool to verify the parent directory exists and is the correct location
   - For example, before running "mkdir foo/bar", first use ls to check that "foo" exists and is the intended parent directory
2. Command Execution:
   - Always quote file paths that contain spaces with double quotes (e.g., cd "path with spaces/file.txt")
   - Examples of proper quoting:
     - cd "/Users/name/My Documents" (correct)
     - cd /Users/name/My Documents (incorrect - will fail)
     - python "/path/with spaces/script.py" (correct)
     - python /path/with spaces/script.py (incorrect - will fail)
   - After ensuring proper quoting, execute the command
   - Capture the output of the command
Usage notes:
  - Commands run in an isolated sandbox environment
  - Returns combined stdout/stderr output with exit code
  - If the output is very large, it may be truncated
  - For long-running commands, use the optional timeout parameter to override the default timeout (e.g., execute(command="make build", timeout=300))
  - A timeout of 0 may disable timeouts on backends that support no-timeout execution
  - VERY IMPORTANT: You MUST avoid using search commands like find and grep. Instead use the grep, glob tools to search. You MUST avoid read tools like cat, head, tail, and use read_file to read files.
  - When issuing multiple commands, use the ';' or '&&' operator to separate them. DO NOT use newlines (newlines are ok in quoted strings)
    - Use '&&' when commands depend on each other (e.g., "mkdir dir && cd dir")
    - Use ';' only when you need to run commands sequentially but don't care if earlier commands fail
  - Try to maintain your current working directory throughout the session by using absolute paths and avoiding usage of cd

Examples:
  Good examples:
    - execute(command="pytest /foo/bar/tests")
    - execute(command="python /path/to/script.py")
    - execute(command="npm install && npm test")
    - execute(command="make build", timeout=300)

  Bad examples (avoid these):
    - execute(command="cd /foo/bar && pytest tests")  # Use absolute path instead
    - execute(command="cat file.txt")  # Use read_file tool instead
    - execute(command="find . -name '*.py'")  # Use glob tool instead
    - execute(command="grep -r 'pattern' .")  # Use grep tool instead

Note: This tool is only available if the backend supports execution (SandboxBackendProtocol).
If execution is not supported, the tool will return an error message."""

DEFAULT_ARTIFACTS_ROOT = "/large_tool_results"
"""Fallback root for offloaded tool results when nothing else configures one."""

FsToolName = Literal[
    "ls",
    "read_file",
    "write_file",
    "edit_file",
    "delete",
    "glob",
    "grep",
    "execute",
    "multi_edit_file",
    "read_many_files",
]
"""Names of the built-in filesystem tools accepted by `FilesystemMiddleware(tools=...)`.

A **superset** of upstream deepagents' seven filesystem tools: bog additionally
ships `multi_edit_file` and `read_many_files`, so a tool list typed against
upstream still type-checks here.
"""

_FS_TOOL_ORDER: tuple[str, ...] = (
    "ls",
    "read_file",
    "write_file",
    "edit_file",
    "delete",
    "glob",
    "grep",
    "multi_edit_file",
    "read_many_files",
)
"""Prompt ordering for the non-`execute` filesystem tools."""

_ALL_FS_TOOL_NAMES: frozenset[str] = frozenset(_FS_TOOL_ORDER) | {"execute"}
"""Every tool name `FilesystemMiddleware` owns (`execute` is prompted separately)."""

_FS_TOOL_DESCRIPTION_LINES: dict[str, str] = {
    "ls": "ls: list files in a directory (requires absolute path)",
    "read_file": "read_file: read a file from the filesystem",
    "write_file": "write_file: write to a file in the filesystem",
    "edit_file": "edit_file: edit a file in the filesystem",
    "delete": "delete: delete a file or directory (recursively) from the filesystem",
    "glob": 'glob: find files matching a pattern (e.g., "**/*.py")',
    "grep": "grep: search for text within files",
    "multi_edit_file": "multi_edit_file: apply several edits in one call",
    "read_many_files": "read_many_files: read several files in one call",
}


def _build_fs_tools_section(visible: set[str]) -> tuple[str, str]:
    """Render the filesystem tool header and bullet list for a visible tool set.

    Args:
        visible: Names of the filesystem tools the model can actually see.
            Names outside `_FS_TOOL_ORDER` (including `execute`, which has its
            own prompt section) are ignored.

    Returns:
        A `(tool_header, tool_descriptions)` pair: a backtick-quoted, comma-joined
            tool list and a newline-joined bullet list, both in `_FS_TOOL_ORDER`.
    """
    ordered = [name for name in _FS_TOOL_ORDER if name in visible]
    header = ", ".join(f"`{name}`" for name in ordered)
    descriptions = "\n".join(f"- {_FS_TOOL_DESCRIPTION_LINES[name]}" for name in ordered)
    return header, descriptions


_FILESYSTEM_SYSTEM_PROMPT_TEMPLATE = """## Following Conventions

- Read files before editing — understand existing content before making changes
- Mimic existing style, naming conventions, and patterns

## Filesystem Tools {tool_header}

You have access to a filesystem which you can interact with using these tools.
All file paths must start with a /. Follow the tool docs for the available tools, and use pagination (offset/limit) when reading large files.

{tool_descriptions}

## Large Tool Results

When a tool result is too large, it may be offloaded into the filesystem instead of being returned inline. In those cases, use `read_file` to inspect the saved result in chunks, or use `grep` within `{large_tool_results_prefix}/` if you need to search across offloaded tool results and do not know the exact file path. Offloaded tool results are stored under `{large_tool_results_prefix}/<tool_call_id>`."""

_DEFAULT_TOOL_HEADER, _DEFAULT_TOOL_DESCRIPTIONS = _build_fs_tools_section(set(_FS_TOOL_ORDER))

FILESYSTEM_SYSTEM_PROMPT = _FILESYSTEM_SYSTEM_PROMPT_TEMPLATE.format(
    tool_header=_DEFAULT_TOOL_HEADER,
    tool_descriptions=_DEFAULT_TOOL_DESCRIPTIONS,
    large_tool_results_prefix=DEFAULT_ARTIFACTS_ROOT,
)
"""Pre-rendered prompt for the default (all tools visible, default artifacts root).

Kept as a module-level constant so existing importers keep working. The middleware
itself renders `_FILESYSTEM_SYSTEM_PROMPT_TEMPLATE` per request from the *visible*
tool set and the *resolved* artifacts root, so this constant is only the default.
"""

EXECUTION_SYSTEM_PROMPT = """## Execute Tool `execute`

You have access to an `execute` tool for running shell commands in a sandboxed environment.
Use this tool to run commands, scripts, tests, builds, and other shell operations.

- execute: run a shell command in the sandbox (returns output and exit code)"""


def _route_host_path_prompt(backend: BackendProtocol) -> str:
    """Build a prompt section mapping virtual route paths to host shell paths.

    `execute` runs on the default backend's shell, so virtual paths (e.g.
    `/common/`) may not exist there. Rather than rewriting the model's shell
    commands, hand it the prefix substitutions so it can write correct commands
    directly.

    A route exposes a usable host path only when its files live on the same
    filesystem the default backend's shell runs in, which requires the default to
    be a `LocalShellBackend`. For such a default, a `FilesystemBackend` route maps
    to a host path based on its mode:

    - virtual mode: the prefix maps to the backend's host root, `route.cwd`
        (e.g. `/common/` -> `/data/`, so `/common/x` is `/data/x` on the host).
    - non-virtual mode: the prefix is stripped and the remaining absolute path is
        used as-is, i.e. the prefix maps to the filesystem root `/`.

    A remote/sandbox default runs its shell in a separate filesystem, so a local
    `FilesystemBackend` route is not reachable from it. Those routes, along with
    store-backed routes, have no host path mapping and must be reached through the
    file tools instead.

    Args:
        backend: The resolved backend for the current request.

    Returns:
        The prompt section, or the empty string when there are no routes to
            describe (i.e. the backend is not a `CompositeBackend`, or it has no
            routes).
    """
    if not isinstance(backend, CompositeBackend):
        return ""

    # Host mappings are only meaningful when the default's shell shares the local
    # filesystem with the routes.
    default_uses_local_shell = isinstance(backend.default, LocalShellBackend)

    host_mappings: list[tuple[str, str]] = []
    no_host_routes: list[str] = []
    for route_prefix, route_backend in backend.sorted_routes:
        if not (default_uses_local_shell and isinstance(route_backend, FilesystemBackend)):
            no_host_routes.append(route_prefix)
        elif route_backend.virtual_mode:
            host_mappings.append((route_prefix, str(route_backend.cwd)))
        else:
            host_mappings.append((route_prefix, "/"))

    if not host_mappings and not no_host_routes:
        return ""

    def _norm(prefix: str) -> str:
        """Ensure a trailing slash so prefix substitution composes for subpaths."""
        return prefix if prefix.endswith("/") else f"{prefix}/"

    def _mapping_line(virtual_prefix: str, host_prefix: str) -> str:
        virtual = _norm(virtual_prefix)
        host = _norm(host_prefix)
        return f"- `{virtual}` -> `{host}` (e.g. `{virtual}dir/x.py` -> `{host}dir/x.py`)"

    lines = [
        "## Shell paths vs. virtual paths",
        "",
        "The `execute` tool runs commands in the host shell and can only access files that exist on the host filesystem.",
        "",
        "Some paths returned by the file tools are virtual mounts:",
        "",
        "- If a virtual mount has a host path mapping, replace its virtual prefix with the host prefix when running shell commands.",
        "- If a virtual mount does not have a host path mapping, it is not accessible from the shell. Use the file tools listed above to interact with those files.",
        "",
        "Do not assume that a path returned by a file tool can be used directly in a shell command.",
    ]

    if host_mappings:
        lines.extend(("", "Host path mappings:"))
        lines.extend(_mapping_line(virtual_prefix, host_prefix) for virtual_prefix, host_prefix in host_mappings)

    if no_host_routes:
        lines.extend(("", "Virtual mounts without a host path mapping (not accessible from the shell):"))
        lines.extend(f"- `{prefix}`" for prefix in no_host_routes)

    return "\n".join(lines)


def _supports_execution(backend: BackendProtocol) -> bool:
    """Check if a backend supports command execution.

    For CompositeBackend, checks if the default backend supports execution.
    For other backends, checks if they implement SandboxBackendProtocol.

    Args:
        backend: The backend to check.

    Returns:
        True if the backend supports execution, False otherwise.
    """
    # For CompositeBackend, check the default backend
    if isinstance(backend, CompositeBackend):
        return isinstance(backend.default, SandboxBackendProtocol)

    # For other backends, use isinstance check
    return isinstance(backend, SandboxBackendProtocol)


# Tools that should be excluded from the large result eviction logic.
#
# This tuple contains tools that should NOT have their results evicted to the filesystem
# when they exceed token limits. Tools are excluded for different reasons:
#
# 1. Tools with built-in truncation (ls, glob, grep):
#    These tools truncate their own output when it becomes too large. When these tools
#    produce truncated output due to many matches, it typically indicates the query
#    needs refinement rather than full result preservation. In such cases, the truncated
#    matches are potentially more like noise and the LLM should be prompted to narrow
#    its search criteria instead.
#
# 2. Tools with problematic truncation behavior (read_file):
#    read_file is tricky to handle as the failure mode here is single long lines
#    (e.g., imagine a jsonl file with very long payloads on each line). If we try to
#    truncate the result of read_file, the agent may then attempt to re-read the
#    truncated file using read_file again, which won't help.
#
# 3. Tools that never exceed limits (edit_file, write_file):
#    These tools return minimal confirmation messages and are never expected to produce
#    output large enough to exceed token limits, so checking them would be unnecessary.
TOOLS_EXCLUDED_FROM_EVICTION = (
    "ls",
    "glob",
    "grep",
    "read_file",
    "edit_file",
    "write_file",
    "delete",
)


TOO_LARGE_HUMAN_MSG = """Message content too large and was saved to the filesystem at: {file_path}

You can read the full content using the read_file tool with pagination (offset and limit parameters).

Here is a preview showing the head and tail of the content:

{content_sample}
"""

CONVERSATION_HISTORY_DIRNAME = "conversation_history"
"""Sub-directory of the artifacts root that oversized human messages are offloaded to."""


def _human_message_digest(content_str: str) -> str:
    """Return a stable short digest identifying an evicted human message's content.

    A content digest — rather than a random UUID — is what makes eviction
    idempotent: the same oversized message always maps to the same backend path,
    so re-rendering the request on a later turn rewrites the same file instead of
    accumulating one artifact per model call. It also means no state update (and
    therefore no message-count change) is needed to remember where the content
    went, which is what keeps the Street Sweeper invariant intact.

    Args:
        content_str: The message's joined text content.

    Returns:
        A 16-character hex digest of the content.
    """
    return hashlib.sha256(content_str.encode("utf-8")).hexdigest()[:16]


def _build_evicted_human_content(message: HumanMessage, replacement_text: str) -> str | list[ContentBlock]:
    """Build replacement content for an evicted `HumanMessage`, preserving non-text blocks.

    Args:
        message: The original `HumanMessage` being evicted.
        replacement_text: The truncation notice and preview text.

    Returns:
        The replacement content: a plain string, or a block list whose leading text
            block is `replacement_text`, followed by the original's media blocks.
    """
    if isinstance(message.content, str):
        return replacement_text
    media_blocks = [block for block in message.content_blocks if block["type"] != "text"]
    if not media_blocks:
        return replacement_text
    return [cast("ContentBlock", {"type": "text", "text": replacement_text}), *media_blocks]


def _build_truncated_human_message(message: HumanMessage, file_path: str, content_str: str) -> HumanMessage:
    """Build the truncated stand-in for an oversized `HumanMessage`.

    Pure string computation — no backend I/O. The result is a `model_copy` of the
    original, so it keeps the same `id` and the request's message count and order
    are unchanged; only the message *text* differs. That is what keeps this rewrite
    composable with `SummarizationMiddleware` (cutoff indices stay aligned) and
    `AnthropicPromptCachingMiddleware` (stable prefix).

    Args:
        message: The original `HumanMessage` (full content stays in state).
        file_path: The backend path the full content was written to.
        content_str: The message's joined text content.

    Returns:
        A copy of `message` whose text is a truncation notice plus a head/tail
            preview. Non-text blocks (images, audio) are preserved.
    """
    content_sample = _create_content_preview(content_str)
    replacement_text = TOO_LARGE_HUMAN_MSG.format(file_path=file_path, content_sample=content_sample)
    evicted = _build_evicted_human_content(message, replacement_text)
    return message.model_copy(update={"content": evicted})


class FilesystemMiddleware(AgentMiddleware[FilesystemState, ContextT, ResponseT]):
    """Middleware for providing filesystem and optional execution tools to an agent.

    This middleware adds filesystem tools to the agent: `ls`, `read_file`, `write_file`,
    `edit_file`, `glob`, and `grep`.

    Files can be stored using any backend that implements the `BackendProtocol`.

    If the backend implements `SandboxBackendProtocol`, an `execute` tool is also added
    for running shell commands.

    This middleware also automatically evicts large tool results to the file system when
    they exceed a token threshold, preventing context window saturation.

    Args:
        backend: Backend for file storage and optional execution.

            If not provided, defaults to `StateBackend` (ephemeral storage in agent state).

            For persistent storage or hybrid setups, use `CompositeBackend` with custom routes.

            For execution support, use a backend that implements `SandboxBackendProtocol`.
        system_prompt: Optional custom system prompt override.
        custom_tool_descriptions: Optional custom tool descriptions override.
        tool_token_limit_before_evict: Token limit before evicting a tool result to the
            filesystem.

            When exceeded, writes the result using the configured backend and replaces it
            with a truncated preview and file reference.

    Example:
        ```python
        from bog_agents.middleware.filesystem import FilesystemMiddleware
        from bog_agents.backends import StateBackend, StoreBackend, CompositeBackend
        from langchain.agents import create_agent

        # Ephemeral storage only (default, no execution)
        agent = create_agent(middleware=[FilesystemMiddleware()])

        # With hybrid storage (ephemeral + persistent /memories/)
        backend = CompositeBackend(default=StateBackend(), routes={"/memories/": StoreBackend()})
        agent = create_agent(middleware=[FilesystemMiddleware(backend=backend)])

        # With sandbox backend (supports execution)
        from my_sandbox import DockerSandboxBackend

        sandbox = DockerSandboxBackend(container_id="my-container")
        agent = create_agent(middleware=[FilesystemMiddleware(backend=sandbox)])
        ```
    """

    state_schema = FilesystemState

    def __init__(
        self,
        *,
        backend: BACKEND_TYPES | None = None,
        system_prompt: str | None = None,
        custom_tool_descriptions: Mapping[str, str] | None = None,
        tool_token_limit_before_evict: int | None = 20000,
        max_execute_timeout: int = 7200,
        artifacts_root: str | None = None,
        human_message_token_limit_before_evict: int | None = 50000,
        tools: list[FsToolName] | Literal["all"] | None = None,
        _permissions: list[FilesystemPermission] | None = None,
    ) -> None:
        """Initialize the filesystem middleware.

        Args:
            backend: Backend for file storage and optional execution, or a factory callable.
                Defaults to StateBackend if not provided.
            system_prompt: Optional custom system prompt override.
            custom_tool_descriptions: Optional custom tool descriptions override.
            tool_token_limit_before_evict: Optional token limit before evicting a tool result to the filesystem.
            max_execute_timeout: Maximum allowed value in seconds for per-command timeout
                overrides on the execute tool.

                Defaults to 7200 seconds (2 hours). Any per-command timeout
                exceeding this value will be rejected with an error message.
            artifacts_root: Optional override for where large tool results are
                offloaded. When omitted, composite backends may provide their
                own `artifacts_root`, otherwise `/large_tool_results` is used.
            human_message_token_limit_before_evict: Optional token limit before an
                oversized `HumanMessage` (e.g. a giant pasted payload) is offloaded
                to the backend and replaced, *in the model request only*, with a
                head/tail preview pointing at the saved file. Set to `None` to
                disable. Defaults to 50000 tokens.
            tools: Allowlist of tool names to expose to the model. `"all"` (or the
                default `None`) exposes every tool. Pass a list of `FsToolName`
                values to restrict the model to only those tools; all others are
                hidden. `read_file` must be included in any list. Backend
                capability checks for `execute` and `delete` still apply, so
                listing them when the backend cannot serve them is a no-op.
            _permissions: Optional filesystem permission rules enforced directly by
                this middleware's tool implementations — a defense-in-depth layer
                under `FilesystemPermissionsMiddleware`, and the *only* layer that
                can filter denied entries out of `ls`/`glob`/`grep` *results* (a
                pathless bulk call has no path argument for the boundary check to
                match against).

                Marked private because it is an internal implementation detail and
                may move to the backend layer in a future change.

        Raises:
            ValueError: If `max_execute_timeout` is not positive, or if `tools` is
                a list that omits `read_file`.
        """
        if max_execute_timeout <= 0:
            msg = f"max_execute_timeout must be positive, got {max_execute_timeout}"
            raise ValueError(msg)
        if isinstance(tools, list) and "read_file" not in tools:
            msg = "read_file must be included in tools; it is required by FilesystemMiddleware"
            raise ValueError(msg)
        # Use provided backend or default to StateBackend factory
        self.backend = backend if backend is not None else (StateBackend)

        # Store configuration (private - internal implementation details)
        self._custom_system_prompt = system_prompt
        self._custom_tool_descriptions: Mapping[str, str] = custom_tool_descriptions or {}
        self._tool_token_limit_before_evict = tool_token_limit_before_evict
        self._human_message_token_limit_before_evict = human_message_token_limit_before_evict
        self._max_execute_timeout = max_execute_timeout
        self._artifacts_root = artifacts_root
        self._permissions: list[FilesystemPermission] = list(_permissions or [])
        if isinstance(tools, list):
            self._enabled_tools: frozenset[str] | None = frozenset(tools)
        elif tools == "all":
            self._enabled_tools = frozenset(_ALL_FS_TOOL_NAMES)
        else:  # None — user did not restrict the set, so every tool is opted in.
            self._enabled_tools = None

        # Paths whose oversized human-message content has already been offloaded in
        # this process. Purely a write-amplification guard: the digest-derived path
        # makes the write idempotent, so a cold start simply rewrites the same file.
        self._evicted_human_paths: set[str] = set()

        all_tools: list[BaseTool] = [
            self._create_ls_tool(),
            self._create_read_file_tool(),
            self._create_write_file_tool(),
            self._create_edit_file_tool(),
            self._create_delete_tool(),
            self._create_glob_tool(),
            self._create_grep_tool(),
            self._create_execute_tool(),
        ]

        # Feature #1: MultiEdit tool — batch edits in one call
        from bog_agents.middleware.multi_edit import create_multi_edit_file_tool

        all_tools.append(create_multi_edit_file_tool(self.backend, self._get_backend))

        # Feature #35: ReadManyFiles tool — read multiple files in one call
        from bog_agents.middleware.read_many_files import create_read_many_files_tool

        all_tools.append(create_read_many_files_tool(self.backend, self._get_backend))

        # The bundled tools build their own descriptions; let a harness profile's
        # `tool_description_overrides` reach them too (ROADMAP #54 lean profile).
        all_tools = [
            tool.model_copy(update={"description": self._custom_tool_descriptions[tool.name]})
            if tool.name in ("multi_edit_file", "read_many_files") and tool.name in self._custom_tool_descriptions
            else tool
            for tool in all_tools
        ]

        self.tools = [tool for tool in all_tools if self._enabled_tools is None or tool.name in self._enabled_tools]

    def _get_backend(self, runtime: ToolRuntime[Any, Any]) -> BackendProtocol:
        """Get the resolved backend instance from backend or factory.

        Args:
            runtime: The tool runtime context.

        Returns:
            Resolved backend instance.
        """
        if callable(self.backend):
            return self.backend(runtime)  # ty: ignore[call-top-callable]
        return self.backend

    def _resolve_artifacts_root(self, resolved_backend: BackendProtocol | None) -> str:
        """Resolve the root under which offloaded artifacts are stored.

        Precedence: the explicit `artifacts_root` constructor argument, then a
        `CompositeBackend`'s own `artifacts_root`, then `DEFAULT_ARTIFACTS_ROOT`.

        Args:
            resolved_backend: The resolved backend, or `None` when it could not be
                resolved (in which case only the constructor argument is consulted).

        Returns:
            The artifacts root with any trailing slash stripped (`"/"` is preserved).
        """
        root = self._artifacts_root
        if root is None and isinstance(resolved_backend, CompositeBackend):
            root = resolved_backend.artifacts_root
        return (root or DEFAULT_ARTIFACTS_ROOT).rstrip("/") or "/"

    def _artifact_path(self, resolved_backend: BackendProtocol, tool_call_id: str) -> str:
        """Build the storage path for an offloaded tool result."""
        artifact_root = self._resolve_artifacts_root(resolved_backend)
        sanitized_id = sanitize_tool_call_id(tool_call_id)
        if artifact_root == "/":
            return f"/{sanitized_id}"
        return f"{artifact_root}/{sanitized_id}"

    def _denied(self, operation: Literal["read", "write"], path: str) -> str | None:
        """Return a permission-denied error string when `operation` on `path` is denied.

        Defense in depth: `FilesystemPermissionsMiddleware` already blocks denied
        calls at the tool-call boundary, but that middleware is wired separately and
        may be absent. When `_permissions` is configured on this middleware, every
        tool re-checks its own path here so a denied path can never be reached even
        if the boundary middleware is not installed.

        Args:
            operation: The operation the tool performs on `path`.
            path: The normalized (validated) path the tool targets.

        Returns:
            The error string to return from the tool, or `None` when the call may
                proceed.
        """
        if not self._permissions:
            return None
        if _check_fs_permission(self._permissions, operation, path) == "deny":
            return f"Error: permission denied for {operation} on {path}"
        return None

    def _create_ls_tool(self) -> BaseTool:
        """Create the ls (list files) tool."""
        tool_description = self._custom_tool_descriptions.get("ls") or LIST_FILES_TOOL_DESCRIPTION

        def _format(result: LsResult) -> str:
            """Filter denied entries out of an ls result, then render it."""
            if result.error:
                return f"Error: {result.error}"
            filtered = apply_permissions_to_ls_result(self._permissions, result)
            paths = [fi.get("path", "") for fi in filtered.entries or []]
            return str(truncate_if_too_long(paths))

        def sync_ls(
            runtime: ToolRuntime[None, FilesystemState],
            path: Annotated[str, "Absolute path to the directory to list. Must be absolute, not relative."],
        ) -> str:
            """Synchronous wrapper for ls tool."""
            resolved_backend = self._get_backend(runtime)
            try:
                validated_path = validate_path(path)
            except ValueError as e:
                return f"Error: {e}"
            if (denied := self._denied("read", validated_path)) is not None:
                return denied
            return _format(resolved_backend.ls(validated_path))

        async def async_ls(
            runtime: ToolRuntime[None, FilesystemState],
            path: Annotated[str, "Absolute path to the directory to list. Must be absolute, not relative."],
        ) -> str:
            """Asynchronous wrapper for ls tool."""
            resolved_backend = self._get_backend(runtime)
            try:
                validated_path = validate_path(path)
            except ValueError as e:
                return f"Error: {e}"
            if (denied := self._denied("read", validated_path)) is not None:
                return denied
            return _format(await resolved_backend.als(validated_path))

        return StructuredTool.from_function(
            name="ls",
            description=tool_description,
            func=sync_ls,
            coroutine=async_ls,
        )

    def _create_read_file_tool(self) -> BaseTool:
        """Create the read_file tool."""
        tool_description = self._custom_tool_descriptions.get("read_file") or READ_FILE_TOOL_DESCRIPTION
        token_limit = self._tool_token_limit_before_evict

        def sync_read_file(
            file_path: Annotated[str, "Absolute path to the file to read. Must be absolute, not relative."],
            runtime: ToolRuntime[None, FilesystemState],
            offset: Annotated[int, "Line number to start reading from (0-indexed). Use for pagination of large files."] = DEFAULT_READ_OFFSET,
            limit: Annotated[int, "Maximum number of lines to read. Use for pagination of large files."] = DEFAULT_READ_LIMIT,
        ) -> ToolMessage | str:
            """Synchronous wrapper for read_file tool."""
            resolved_backend = self._get_backend(runtime)
            try:
                validated_path = validate_path(file_path)
            except ValueError as e:
                return f"Error: {e}"
            if (denied := self._denied("read", validated_path)) is not None:
                return denied

            ext = Path(validated_path).suffix.lower()
            if ext in IMAGE_EXTENSIONS:
                responses = resolved_backend.download_files([validated_path])
                if responses and responses[0].content is not None:
                    media_type = IMAGE_MEDIA_TYPES.get(ext, "image/png")
                    image_b64 = base64.standard_b64encode(responses[0].content).decode("utf-8")
                    return ToolMessage(
                        content_blocks=[create_image_block(base64=image_b64, mime_type=media_type)],
                        name="read_file",
                        tool_call_id=runtime.tool_call_id,
                        additional_kwargs={
                            "read_file_path": validated_path,
                            "read_file_media_type": media_type,
                        },
                    )
                if responses and responses[0].error:
                    return f"Error reading image: {responses[0].error}"
                return "Error reading image: unknown error"

            if ext == ".pdf":
                from bog_agents.middleware.pdf_reader import read_pdf

                responses = resolved_backend.download_files([validated_path])
                if responses and responses[0].content is not None:
                    return read_pdf(validated_path, data=responses[0].content, start_page=offset)
                if responses and responses[0].error:
                    return f"Error reading PDF: {responses[0].error}"
                return "Error reading PDF: unknown error"

            if ext in VIDEO_EXTENSIONS:
                from bog_agents.middleware.video_reader import MISSING_VIDEO_HINT, video_dependencies_available

                # Without the optional `[video]` extra we cannot sample frames.
                # Return the install hint rather than corrupting the bytes by
                # reading a binary container as text.
                if not video_dependencies_available():
                    return MISSING_VIDEO_HINT
                responses = resolved_backend.download_files([validated_path])
                if responses and responses[0].content is not None:
                    return _handle_video_read(validated_path, responses[0].content, runtime.tool_call_id, offset, limit)
                if responses and responses[0].error:
                    return f"Error reading video: {responses[0].error}"
                return "Error reading video: unknown error"

            result = resolved_backend.read(validated_path, offset=offset, limit=limit)

            lines = result.splitlines(keepends=True)
            if len(lines) > limit:
                lines = lines[:limit]
                result = "".join(lines)

            # Check if result exceeds token threshold and truncate if necessary
            if token_limit and len(result) >= NUM_CHARS_PER_TOKEN * token_limit:
                # Calculate truncation message length to ensure final result stays under threshold
                truncation_msg = READ_FILE_TRUNCATION_MSG.format(file_path=validated_path)
                max_content_length = NUM_CHARS_PER_TOKEN * token_limit - len(truncation_msg)
                result = result[:max_content_length]
                result += truncation_msg

            return result

        async def async_read_file(
            file_path: Annotated[str, "Absolute path to the file to read. Must be absolute, not relative."],
            runtime: ToolRuntime[None, FilesystemState],
            offset: Annotated[int, "Line number to start reading from (0-indexed). Use for pagination of large files."] = DEFAULT_READ_OFFSET,
            limit: Annotated[int, "Maximum number of lines to read. Use for pagination of large files."] = DEFAULT_READ_LIMIT,
        ) -> ToolMessage | str:
            """Asynchronous wrapper for read_file tool."""
            resolved_backend = self._get_backend(runtime)
            try:
                validated_path = validate_path(file_path)
            except ValueError as e:
                return f"Error: {e}"
            if (denied := self._denied("read", validated_path)) is not None:
                return denied

            ext = Path(validated_path).suffix.lower()
            if ext in IMAGE_EXTENSIONS:
                responses = await resolved_backend.adownload_files([validated_path])
                if responses and responses[0].content is not None:
                    media_type = IMAGE_MEDIA_TYPES.get(ext, "image/png")
                    image_b64 = base64.standard_b64encode(responses[0].content).decode("utf-8")
                    return ToolMessage(
                        content_blocks=[create_image_block(base64=image_b64, mime_type=media_type)],
                        name="read_file",
                        tool_call_id=runtime.tool_call_id,
                        additional_kwargs={
                            "read_file_path": validated_path,
                            "read_file_media_type": media_type,
                        },
                    )
                if responses and responses[0].error:
                    return f"Error reading image: {responses[0].error}"
                return "Error reading image: unknown error"

            if ext == ".pdf":
                from bog_agents.middleware.pdf_reader import read_pdf

                responses = await resolved_backend.adownload_files([validated_path])
                if responses and responses[0].content is not None:
                    return read_pdf(validated_path, data=responses[0].content, start_page=offset)
                if responses and responses[0].error:
                    return f"Error reading PDF: {responses[0].error}"
                return "Error reading PDF: unknown error"

            if ext in VIDEO_EXTENSIONS:
                from bog_agents.middleware.video_reader import MISSING_VIDEO_HINT, video_dependencies_available

                # Without the optional `[video]` extra we cannot sample frames.
                # Return the install hint rather than corrupting the bytes by
                # reading a binary container as text.
                if not video_dependencies_available():
                    return MISSING_VIDEO_HINT
                responses = await resolved_backend.adownload_files([validated_path])
                if responses and responses[0].content is not None:
                    return _handle_video_read(validated_path, responses[0].content, runtime.tool_call_id, offset, limit)
                if responses and responses[0].error:
                    return f"Error reading video: {responses[0].error}"
                return "Error reading video: unknown error"

            result = await resolved_backend.aread(validated_path, offset=offset, limit=limit)

            lines = result.splitlines(keepends=True)
            if len(lines) > limit:
                lines = lines[:limit]
                result = "".join(lines)

            # Check if result exceeds token threshold and truncate if necessary
            if token_limit and len(result) >= NUM_CHARS_PER_TOKEN * token_limit:
                # Calculate truncation message length to ensure final result stays under threshold
                truncation_msg = READ_FILE_TRUNCATION_MSG.format(file_path=validated_path)
                max_content_length = NUM_CHARS_PER_TOKEN * token_limit - len(truncation_msg)
                result = result[:max_content_length]
                result += truncation_msg

            return result

        return StructuredTool.from_function(
            name="read_file",
            description=tool_description,
            func=sync_read_file,
            coroutine=async_read_file,
        )

    def _create_write_file_tool(self) -> BaseTool:
        """Create the write_file tool."""
        tool_description = self._custom_tool_descriptions.get("write_file") or WRITE_FILE_TOOL_DESCRIPTION

        def sync_write_file(
            file_path: Annotated[str, "Absolute path where the file should be created. Must be absolute, not relative."],
            content: Annotated[str, "The text content to write to the file. This parameter is required."],
            runtime: ToolRuntime[None, FilesystemState],
        ) -> Command | str:
            """Synchronous wrapper for write_file tool."""
            resolved_backend = self._get_backend(runtime)
            try:
                validated_path = validate_path(file_path)
            except ValueError as e:
                return f"Error: {e}"
            if (denied := self._denied("write", validated_path)) is not None:
                return denied
            res: WriteResult = resolved_backend.write(validated_path, content)
            if res.error:
                return res.error
            # If backend returns state update, wrap into Command with ToolMessage
            if res.files_update is not None:
                return Command(
                    update={
                        "files": res.files_update,
                        "messages": [
                            ToolMessage(
                                content=f"Updated file {res.path}",
                                tool_call_id=runtime.tool_call_id,
                            )
                        ],
                    }
                )
            return f"Updated file {res.path}"

        async def async_write_file(
            file_path: Annotated[str, "Absolute path where the file should be created. Must be absolute, not relative."],
            content: Annotated[str, "The text content to write to the file. This parameter is required."],
            runtime: ToolRuntime[None, FilesystemState],
        ) -> Command | str:
            """Asynchronous wrapper for write_file tool."""
            resolved_backend = self._get_backend(runtime)
            try:
                validated_path = validate_path(file_path)
            except ValueError as e:
                return f"Error: {e}"
            if (denied := self._denied("write", validated_path)) is not None:
                return denied
            res: WriteResult = await resolved_backend.awrite(validated_path, content)
            if res.error:
                return res.error
            # If backend returns state update, wrap into Command with ToolMessage
            if res.files_update is not None:
                return Command(
                    update={
                        "files": res.files_update,
                        "messages": [
                            ToolMessage(
                                content=f"Updated file {res.path}",
                                tool_call_id=runtime.tool_call_id,
                            )
                        ],
                    }
                )
            return f"Updated file {res.path}"

        return StructuredTool.from_function(
            name="write_file",
            description=tool_description,
            func=sync_write_file,
            coroutine=async_write_file,
        )

    def _create_edit_file_tool(self) -> BaseTool:
        """Create the edit_file tool."""
        tool_description = self._custom_tool_descriptions.get("edit_file") or EDIT_FILE_TOOL_DESCRIPTION

        def sync_edit_file(
            file_path: Annotated[str, "Absolute path to the file to edit. Must be absolute, not relative."],
            old_string: Annotated[str, "The exact text to find and replace. Must be unique in the file unless replace_all is True."],
            new_string: Annotated[str, "The text to replace old_string with. Must be different from old_string."],
            runtime: ToolRuntime[None, FilesystemState],
            *,
            replace_all: Annotated[bool, "If True, replace all occurrences of old_string. If False (default), old_string must be unique."] = False,
        ) -> Command | str:
            """Synchronous wrapper for edit_file tool."""
            resolved_backend = self._get_backend(runtime)
            try:
                validated_path = validate_path(file_path)
            except ValueError as e:
                return f"Error: {e}"
            if (denied := self._denied("write", validated_path)) is not None:
                return denied
            res: EditResult = resolved_backend.edit(validated_path, old_string, new_string, replace_all=replace_all)
            if res.error:
                return res.error
            if res.files_update is not None:
                return Command(
                    update={
                        "files": res.files_update,
                        "messages": [
                            ToolMessage(
                                content=f"Successfully replaced {res.occurrences} instance(s) of the string in '{res.path}'",
                                tool_call_id=runtime.tool_call_id,
                            )
                        ],
                    }
                )
            return f"Successfully replaced {res.occurrences} instance(s) of the string in '{res.path}'"

        async def async_edit_file(
            file_path: Annotated[str, "Absolute path to the file to edit. Must be absolute, not relative."],
            old_string: Annotated[str, "The exact text to find and replace. Must be unique in the file unless replace_all is True."],
            new_string: Annotated[str, "The text to replace old_string with. Must be different from old_string."],
            runtime: ToolRuntime[None, FilesystemState],
            *,
            replace_all: Annotated[bool, "If True, replace all occurrences of old_string. If False (default), old_string must be unique."] = False,
        ) -> Command | str:
            """Asynchronous wrapper for edit_file tool."""
            resolved_backend = self._get_backend(runtime)
            try:
                validated_path = validate_path(file_path)
            except ValueError as e:
                return f"Error: {e}"
            if (denied := self._denied("write", validated_path)) is not None:
                return denied
            res: EditResult = await resolved_backend.aedit(validated_path, old_string, new_string, replace_all=replace_all)
            if res.error:
                return res.error
            if res.files_update is not None:
                return Command(
                    update={
                        "files": res.files_update,
                        "messages": [
                            ToolMessage(
                                content=f"Successfully replaced {res.occurrences} instance(s) of the string in '{res.path}'",
                                tool_call_id=runtime.tool_call_id,
                            )
                        ],
                    }
                )
            return f"Successfully replaced {res.occurrences} instance(s) of the string in '{res.path}'"

        return StructuredTool.from_function(
            name="edit_file",
            description=tool_description,
            func=sync_edit_file,
            coroutine=async_edit_file,
        )

    def _delete_denied(self, validated_path: str) -> str | None:
        """Return a permission-denied error when a recursive delete of `validated_path` is blocked.

        `delete` is recursive, so an exact-path deny check is not sufficient: a deny
        rule on `/secrets/**` does not match the literal path `/`, yet `delete("/")`
        would remove the denied subtree along with everything else. `_find_delete_deny_patterns`
        instead asks whether any deny-write pattern could match `validated_path` *or
        anything under it*, and blocks the whole call if so.

        Args:
            validated_path: The normalized path being deleted.

        Returns:
            The error string to return from the tool, or `None` when the delete may
                proceed.
        """
        if not self._permissions:
            return None
        denying = _find_delete_deny_patterns(self._permissions, validated_path)
        if denying:
            return f"Error: permission denied for write on {validated_path} (matches deny rule(s): {', '.join(denying)})"
        return None

    def _create_delete_tool(self) -> BaseTool:
        """Create the delete tool."""
        tool_description = self._custom_tool_descriptions.get("delete") or DELETE_TOOL_DESCRIPTION

        def _unsupported(resolved_backend: BackendProtocol) -> str | None:
            if supports_delete(resolved_backend):
                return None
            return "Error: Deletion not available. This agent's backend does not implement `delete`."

        def _to_result(res: DeleteResult, tool_call_id: str) -> Command | str:
            if res.error:
                return f"Error: {res.error}"
            message = f"Deleted {res.path}"
            if res.files_update is not None:
                return Command(
                    update={
                        "files": res.files_update,
                        "messages": [ToolMessage(content=message, tool_call_id=tool_call_id)],
                    }
                )
            return message

        def sync_delete(
            file_path: Annotated[str, "Absolute path to the file or directory to delete. Must be absolute, not relative."],
            runtime: ToolRuntime[None, FilesystemState],
        ) -> Command | str:
            """Synchronous wrapper for delete tool."""
            resolved_backend = self._get_backend(runtime)
            if (unsupported := _unsupported(resolved_backend)) is not None:
                return unsupported
            try:
                validated_path = validate_path(file_path)
            except ValueError as e:
                return f"Error: {e}"
            if (denied := self._delete_denied(validated_path)) is not None:
                return denied
            return _to_result(resolved_backend.delete(validated_path), runtime.tool_call_id)

        async def async_delete(
            file_path: Annotated[str, "Absolute path to the file or directory to delete. Must be absolute, not relative."],
            runtime: ToolRuntime[None, FilesystemState],
        ) -> Command | str:
            """Asynchronous wrapper for delete tool."""
            resolved_backend = self._get_backend(runtime)
            if (unsupported := _unsupported(resolved_backend)) is not None:
                return unsupported
            try:
                validated_path = validate_path(file_path)
            except ValueError as e:
                return f"Error: {e}"
            if (denied := self._delete_denied(validated_path)) is not None:
                return denied
            return _to_result(await resolved_backend.adelete(validated_path), runtime.tool_call_id)

        return StructuredTool.from_function(
            name="delete",
            description=tool_description,
            func=sync_delete,
            coroutine=async_delete,
        )

    def _create_glob_tool(self) -> BaseTool:
        """Create the glob tool."""
        tool_description = self._custom_tool_descriptions.get("glob") or GLOB_TOOL_DESCRIPTION

        def _format(result: GlobResult) -> str:
            """Filter denied matches out of a glob result, then render it."""
            if result.error:
                return f"Error: {result.error}"
            filtered = apply_permissions_to_glob_result(self._permissions, result)
            paths = [fi.get("path", "") for fi in filtered.matches or []]
            rendered = str(truncate_if_too_long(paths))
            if filtered.truncated:
                return f"{rendered}\n\n{SEARCH_TRUNCATION_NOTE}"
            return rendered

        def sync_glob(
            pattern: Annotated[str, "Glob pattern to match files (e.g., '**/*.py', '*.txt', '/subdir/**/*.md')."],
            runtime: ToolRuntime[None, FilesystemState],
            path: Annotated[str, "Base directory to search from. Defaults to root '/'."] = "/",
        ) -> str:
            """Synchronous wrapper for glob tool."""
            resolved_backend = self._get_backend(runtime)
            try:
                validated_path = validate_path(path)
            except ValueError as e:
                return f"Error: {e}"
            if (denied := self._denied("read", validated_path)) is not None:
                return denied
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(resolved_backend.glob, pattern, path=validated_path)
                try:
                    result = future.result(timeout=GLOB_TIMEOUT)
                except concurrent.futures.TimeoutError:
                    return f"Error: glob timed out after {GLOB_TIMEOUT}s. Try a more specific pattern or a narrower path."
            return _format(result)

        async def async_glob(
            pattern: Annotated[str, "Glob pattern to match files (e.g., '**/*.py', '*.txt', '/subdir/**/*.md')."],
            runtime: ToolRuntime[None, FilesystemState],
            path: Annotated[str, "Base directory to search from. Defaults to root '/'."] = "/",
        ) -> str:
            """Asynchronous wrapper for glob tool."""
            resolved_backend = self._get_backend(runtime)
            try:
                validated_path = validate_path(path)
            except ValueError as e:
                return f"Error: {e}"
            if (denied := self._denied("read", validated_path)) is not None:
                return denied
            try:
                result = await asyncio.wait_for(
                    resolved_backend.aglob(pattern, path=validated_path),
                    timeout=GLOB_TIMEOUT,
                )
            except TimeoutError:
                return f"Error: glob timed out after {GLOB_TIMEOUT}s. Try a more specific pattern or a narrower path."
            return _format(result)

        return StructuredTool.from_function(
            name="glob",
            description=tool_description,
            func=sync_glob,
            coroutine=async_glob,
        )

    def _grep_tool_description(self, *, include_execution: bool) -> str:
        """Return the grep description matching the current `execute` visibility.

        The description's "if you genuinely need regex, use `rg` via execute"
        escape hatch is only honest when an `execute` tool is actually reachable,
        so it is dropped when the backend cannot execute.

        Args:
            include_execution: Whether the `execute` tool is active for this request.

        Returns:
            The custom description if one was configured, else the matching default.
        """
        return self._custom_tool_descriptions.get("grep") or (GREP_TOOL_DESCRIPTION if include_execution else GREP_TOOL_DESCRIPTION_WITHOUT_EXECUTE)

    def _create_grep_tool(self) -> BaseTool:
        """Create the grep tool."""
        # Provisional: assume execute is available so the static description can point
        # at `rg`. `_apply_grep_description` reconciles this against the backend's real
        # execute capability at request time.
        tool_description = self._grep_tool_description(include_execution=True)

        def _format(
            result: GrepResult,
            pattern: str,
            output_mode: Literal["files_with_matches", "content", "count"],
        ) -> str:
            """Filter denied matches out of a grep result, then render it."""
            backend_had_matches = bool(result.matches)
            filtered = apply_permissions_to_grep_result(self._permissions, result)
            matches = filtered.matches or []
            if filtered.error and not matches:
                return filtered.error
            rendered = truncate_if_too_long(format_grep_matches(matches, output_mode))
            if filtered.error:
                # Truncate the error separately so the already-size-limited partial
                # matches survive being appended to it.
                return f"{truncate_if_too_long(filtered.error)}\n\nPartial matches:\n{rendered}"
            notes: list[str] = []
            if filtered.truncated:
                notes.append(SEARCH_TRUNCATION_NOTE)
            # Gate the regex hint on the *backend's* match count, not the filtered
            # one: when matches existed but were all redacted by a deny rule, the
            # empty result has nothing to do with regex syntax.
            elif not backend_had_matches and (hint := regex_literal_hint(pattern)) is not None:
                notes.append(hint)
            if notes:
                return "{}\n\n{}".format(rendered, "\n\n".join(notes))
            return rendered

        def sync_grep(
            pattern: Annotated[str, "Text pattern to search for (literal string, not regex)."],
            runtime: ToolRuntime[None, FilesystemState],
            path: Annotated[str | None, "Directory to search in. Defaults to current working directory."] = None,
            glob: Annotated[str | None, "Glob pattern to filter which files to search (e.g., '*.py')."] = None,
            output_mode: Annotated[
                Literal["files_with_matches", "content", "count"],
                "Output format: 'files_with_matches' (file paths only, default), 'content' (matching lines with context), 'count' (match counts per file).",
            ] = "files_with_matches",
        ) -> str:
            """Synchronous wrapper for grep tool."""
            resolved_backend = self._get_backend(runtime)
            if path is not None:
                try:
                    path = validate_path(path)
                except ValueError as e:
                    return f"Error: {e}"
                if (denied := self._denied("read", path)) is not None:
                    return denied
            return _format(resolved_backend.grep(pattern, path=path, glob=glob), pattern, output_mode)

        async def async_grep(
            pattern: Annotated[str, "Text pattern to search for (literal string, not regex)."],
            runtime: ToolRuntime[None, FilesystemState],
            path: Annotated[str | None, "Directory to search in. Defaults to current working directory."] = None,
            glob: Annotated[str | None, "Glob pattern to filter which files to search (e.g., '*.py')."] = None,
            output_mode: Annotated[
                Literal["files_with_matches", "content", "count"],
                "Output format: 'files_with_matches' (file paths only, default), 'content' (matching lines with context), 'count' (match counts per file).",
            ] = "files_with_matches",
        ) -> str:
            """Asynchronous wrapper for grep tool."""
            resolved_backend = self._get_backend(runtime)
            if path is not None:
                try:
                    path = validate_path(path)
                except ValueError as e:
                    return f"Error: {e}"
                if (denied := self._denied("read", path)) is not None:
                    return denied
            return _format(await resolved_backend.agrep(pattern, path=path, glob=glob), pattern, output_mode)

        return StructuredTool.from_function(
            name="grep",
            description=tool_description,
            func=sync_grep,
            coroutine=async_grep,
        )

    def _create_execute_tool(self) -> BaseTool:
        """Create the execute tool for sandbox command execution."""
        tool_description = self._custom_tool_descriptions.get("execute") or EXECUTE_TOOL_DESCRIPTION

        def sync_execute(
            command: Annotated[str, "Shell command to execute in the sandbox environment."],
            runtime: ToolRuntime[None, FilesystemState],
            timeout: Annotated[
                int | None,
                "Optional timeout in seconds for this command. Overrides the default timeout. Use 0 for no-timeout execution on backends that support it.",
            ] = None,
        ) -> str:
            """Synchronous wrapper for execute tool."""
            if timeout is not None:
                if timeout < 0:
                    return f"Error: timeout must be non-negative, got {timeout}."
                if timeout > self._max_execute_timeout:
                    return f"Error: timeout {timeout}s exceeds maximum allowed ({self._max_execute_timeout}s)."

            resolved_backend = self._get_backend(runtime)

            # Runtime check - fail gracefully if not supported
            if not _supports_execution(resolved_backend):
                return (
                    "Error: Execution not available. This agent's backend "
                    "does not support command execution (SandboxBackendProtocol). "
                    "To use the execute tool, provide a backend that implements SandboxBackendProtocol."
                )

            # Safe cast: _supports_execution validates that execute()/aexecute() exist
            # (either SandboxBackendProtocol or CompositeBackend with sandbox default)
            executable = cast("SandboxBackendProtocol", resolved_backend)
            if timeout is not None and not execute_accepts_timeout(type(executable)):
                return (
                    "Error: This sandbox backend does not support per-command "
                    "timeout overrides. Update your sandbox package to the "
                    "latest version, or omit the timeout parameter."
                )
            try:
                result = executable.execute(command, timeout=timeout) if timeout is not None else executable.execute(command)
            except NotImplementedError as e:
                # Handle case where execute() exists but raises NotImplementedError
                return f"Error: Execution not available. {e}"
            except ValueError as e:
                return f"Error: Invalid parameter. {e}"
            except PermissionError as e:
                # LocalShellBackend raises PermissionError for dangerous-command
                # patterns (e.g. `rm -rf`). langgraph's default tool-error
                # handler re-raises non-ToolInvocationError exceptions, which
                # would abort the turn — surface the "blocked" message as a tool
                # result the model can read and adapt to instead.
                return f"Error: {e}"

            # Format output for LLM consumption
            parts = [result.output]

            if result.exit_code is not None:
                status = "succeeded" if result.exit_code == 0 else "failed"
                parts.append(f"\n[Command {status} with exit code {result.exit_code}]")

            if result.truncated:
                parts.append("\n[Output was truncated due to size limits]")

            return "".join(parts)

        async def async_execute(
            command: Annotated[str, "Shell command to execute in the sandbox environment."],
            runtime: ToolRuntime[None, FilesystemState],
            # ASYNC109 - timeout is a semantic parameter forwarded to the
            # backend's implementation, not an asyncio.timeout() contract.
            timeout: Annotated[
                int | None,
                "Optional timeout in seconds for this command. Overrides the default timeout. Use 0 for no-timeout execution on backends that support it.",
            ] = None,
        ) -> str:
            """Asynchronous wrapper for execute tool."""
            if timeout is not None:
                if timeout < 0:
                    return f"Error: timeout must be non-negative, got {timeout}."
                if timeout > self._max_execute_timeout:
                    return f"Error: timeout {timeout}s exceeds maximum allowed ({self._max_execute_timeout}s)."

            resolved_backend = self._get_backend(runtime)

            # Runtime check - fail gracefully if not supported
            if not _supports_execution(resolved_backend):
                return (
                    "Error: Execution not available. This agent's backend "
                    "does not support command execution (SandboxBackendProtocol). "
                    "To use the execute tool, provide a backend that implements SandboxBackendProtocol."
                )

            # Safe cast: _supports_execution validates that execute()/aexecute() exist
            executable = cast("SandboxBackendProtocol", resolved_backend)
            if timeout is not None and not execute_accepts_timeout(type(executable)):
                return (
                    "Error: This sandbox backend does not support per-command "
                    "timeout overrides. Update your sandbox package to the "
                    "latest version, or omit the timeout parameter."
                )
            try:
                result = await executable.aexecute(command, timeout=timeout) if timeout is not None else await executable.aexecute(command)
            except NotImplementedError as e:
                # Handle case where execute() exists but raises NotImplementedError
                return f"Error: Execution not available. {e}"
            except ValueError as e:
                return f"Error: Invalid parameter. {e}"
            except PermissionError as e:
                # Dangerous-command guard (LocalShellBackend). See sync_execute:
                # surface as a tool-error string rather than letting it abort
                # the turn via langgraph's re-raising error handler.
                return f"Error: {e}"

            # Format output for LLM consumption
            parts = [result.output]

            if result.exit_code is not None:
                status = "succeeded" if result.exit_code == 0 else "failed"
                parts.append(f"\n[Command {status} with exit code {result.exit_code}]")

            if result.truncated:
                parts.append("\n[Output was truncated due to size limits]")

            return "".join(parts)

        return StructuredTool.from_function(
            name="execute",
            description=tool_description,
            func=sync_execute,
            coroutine=async_execute,
        )

    @staticmethod
    def _tool_name(tool: Any) -> str | None:
        """Extract a request tool's name from a `BaseTool`, a dict, or a test double."""
        if hasattr(tool, "name"):
            return cast("str | None", tool.name)
        get = getattr(tool, "get", None)
        if callable(get):
            return cast("str | None", get("name"))
        return None

    def _apply_grep_description(
        self,
        tools: list[Any],
        *,
        include_execution: bool,
    ) -> list[Any]:
        """Rewrite the default grep description to match `execute` availability.

        Only the two built-in defaults are rewritten; a user-supplied
        `custom_tool_descriptions["grep"]` is left alone.

        Args:
            tools: The request's tool list.
            include_execution: Whether the `execute` tool survived filtering.

        Returns:
            The same list object when nothing changed, else a new list with the grep
                tool copied and re-described.
        """
        if self._custom_tool_descriptions.get("grep"):
            return tools

        target = self._grep_tool_description(include_execution=include_execution)
        defaults = {GREP_TOOL_DESCRIPTION, GREP_TOOL_DESCRIPTION_WITHOUT_EXECUTE}
        rewritten: list[Any] = []
        changed = False
        for tool in tools:
            if self._tool_name(tool) != "grep" or not isinstance(tool, BaseTool):
                rewritten.append(tool)
                continue
            if tool.description in defaults and tool.description != target:
                rewritten.append(tool.model_copy(update={"description": target}))
                changed = True
            else:
                rewritten.append(tool)
        return rewritten if changed else tools

    def _prepare_request(self, request: ModelRequest[ContextT]) -> ModelRequest[ContextT]:
        """Drop capability-gated tools, then render and append the system prompt.

        `execute` and `delete` are optional per backend, so when the resolved backend
        cannot serve one it is filtered out of the request rather than advertised to
        the model and left to fail at call time. The system prompt is then rendered
        from the tools that actually survived, and from the *resolved* artifacts root
        (which a `CompositeBackend` can override) rather than a hardcoded
        `/large_tool_results`.

        Shared by the sync and async model-call paths — the only part that differs
        between them is sync vs. async human-message eviction.

        Args:
            request: The incoming model request.

        Returns:
            The request with unsupported tools removed and the prompt appended.
        """
        tool_names = {self._tool_name(tool) for tool in request.tools}
        has_execute_tool = "execute" in tool_names
        has_delete_tool = "delete" in tool_names

        backend: BackendProtocol | None = None
        if has_execute_tool or has_delete_tool or self._custom_system_prompt is None:
            backend = self._get_backend(request.runtime)

        execution_active = has_execute_tool and backend is not None and _supports_execution(backend)
        unsupported: set[str | None] = set()
        if has_execute_tool and not execution_active:
            unsupported.add("execute")
        if has_delete_tool and (backend is None or not supports_delete(backend)):
            unsupported.add("delete")

        if unsupported:
            request = request.override(tools=[tool for tool in request.tools if self._tool_name(tool) not in unsupported])

        visible_tools = list(request.tools)
        described = self._apply_grep_description(visible_tools, include_execution=execution_active)
        if described is not visible_tools:
            request = request.override(tools=described)

        if self._custom_system_prompt is not None:
            system_prompt = self._custom_system_prompt
        else:
            visible = {name for name in tool_names - unsupported if name is not None}
            tool_header, tool_descriptions = _build_fs_tools_section(visible)
            prompt_parts = [
                _FILESYSTEM_SYSTEM_PROMPT_TEMPLATE.format(
                    tool_header=tool_header,
                    tool_descriptions=tool_descriptions,
                    large_tool_results_prefix=self._resolve_artifacts_root(backend),
                )
            ]
            if execution_active:
                prompt_parts.append(EXECUTION_SYSTEM_PROMPT)
                if backend is not None and (route_prompt := _route_host_path_prompt(backend)):
                    prompt_parts.append(route_prompt)
            system_prompt = "\n\n".join(prompt_parts).strip()

        if system_prompt:
            new_system_message = append_to_system_message(request.system_message, system_prompt)
            request = request.override(system_message=new_system_message)

        return request

    def _oversized_human_messages(self, messages: list[AnyMessage]) -> list[int]:
        """Return the indices of `HumanMessage`s whose text exceeds the eviction threshold.

        Args:
            messages: The request's message list.

        Returns:
            Indices, in order. Empty when eviction is disabled or nothing is oversized.
        """
        if not self._human_message_token_limit_before_evict:
            return []
        threshold = NUM_CHARS_PER_TOKEN * self._human_message_token_limit_before_evict
        return [i for i, msg in enumerate(messages) if isinstance(msg, HumanMessage) and len(_extract_text_from_message(msg)) > threshold]

    def _human_eviction_path(self, resolved_backend: BackendProtocol | None, content_str: str) -> str:
        """Build the backend path an oversized human message's content is offloaded to."""
        root = self._resolve_artifacts_root(resolved_backend)
        prefix = "" if root == "/" else root
        return f"{prefix}/{CONVERSATION_HISTORY_DIRNAME}/{_human_message_digest(content_str)}.md"

    def _evict_human_messages(self, request: ModelRequest[ContextT]) -> list[AnyMessage] | None:
        """Offload oversized `HumanMessage`s and return the rewritten request messages.

        A 200k-token pasted payload otherwise goes straight to the model. The full
        content is written to the backend and the message the *model* sees is replaced
        with a head/tail preview naming the file, which the model can page through
        with `read_file`.

        This is a **view transformation**: the canonical history in LangGraph state is
        untouched, and the rewrite replaces message *text* only — the message count and
        order are unchanged, which is what keeps it composable with
        `SummarizationMiddleware`'s cutoff indices and `AnthropicPromptCachingMiddleware`'s
        stable prefix.

        Args:
            request: The model request being processed.

        Returns:
            The rewritten message list, or `None` when nothing needed eviction (fast
                path) or the backend write failed (fall through to the original text
                rather than pointing the model at a file that does not exist).
        """
        messages = list(request.messages)
        indices = self._oversized_human_messages(messages)
        if not indices:
            return None

        backend = self._get_backend_for_request(request)
        changed = False
        for i in indices:
            message = cast("HumanMessage", messages[i])
            content_str = _extract_text_from_message(message)
            file_path = self._human_eviction_path(backend, content_str)
            if file_path not in self._evicted_human_paths:
                if backend is None or backend.write(file_path, content_str).error:
                    continue
                self._evicted_human_paths.add(file_path)
            messages[i] = _build_truncated_human_message(message, file_path, content_str)
            changed = True
        return messages if changed else None

    async def _aevict_human_messages(self, request: ModelRequest[ContextT]) -> list[AnyMessage] | None:
        """(async) Offload oversized `HumanMessage`s. See `_evict_human_messages`."""
        messages = list(request.messages)
        indices = self._oversized_human_messages(messages)
        if not indices:
            return None

        backend = self._get_backend_for_request(request)
        changed = False
        for i in indices:
            message = cast("HumanMessage", messages[i])
            content_str = _extract_text_from_message(message)
            file_path = self._human_eviction_path(backend, content_str)
            if file_path not in self._evicted_human_paths:
                if backend is None:
                    continue
                result = await backend.awrite(file_path, content_str)
                if result.error:
                    continue
                self._evicted_human_paths.add(file_path)
            messages[i] = _build_truncated_human_message(message, file_path, content_str)
            changed = True
        return messages if changed else None

    def _get_backend_for_request(self, request: ModelRequest[ContextT]) -> BackendProtocol | None:
        """Resolve the backend for a model request, or `None` if it cannot be resolved.

        Human-message eviction must never break a turn, so a backend factory that
        raises here degrades to "no eviction" rather than failing the request.

        Args:
            request: The model request being processed.

        Returns:
            The resolved backend, or `None` when resolution failed.
        """
        try:
            return self._get_backend(request.runtime)
        except Exception:  # best-effort: a failed backend resolve must not break the turn
            return None

    def wrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], ModelResponse[ResponseT]],
    ) -> ModelResponse[ResponseT]:
        """Filter tools by backend capability, apply the prompt, and evict huge human messages.

        Args:
            request: The model request being processed.
            handler: The handler function to call with the modified request.

        Returns:
            The model response from the handler.
        """
        request = self._prepare_request(request)
        evicted = self._evict_human_messages(request)
        if evicted is not None:
            request = request.override(messages=evicted)
        return handler(request)

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]],
    ) -> ModelResponse[ResponseT]:
        """(async) Filter tools, apply the prompt, and evict huge human messages.

        Args:
            request: The model request being processed.
            handler: The handler function to call with the modified request.

        Returns:
            The model response from the handler.
        """
        request = self._prepare_request(request)
        evicted = await self._aevict_human_messages(request)
        if evicted is not None:
            request = request.override(messages=evicted)
        return await handler(request)

    def _guard_parallel_writes(self, state: AgentState[Any]) -> dict[str, Any] | None:
        """Reject conflicting parallel file mutations in the latest model response.

        When the model emits multiple write-class tool calls
        (`write_file`/`edit_file`/`multi_edit_file`) targeting the same file in a
        single AIMessage, only the first is allowed to run; each later conflicting
        call is removed from the AIMessage and answered with an error ToolMessage
        telling the model to sequence the edits across turns. This prevents the
        last-writer-wins state reducer from silently clobbering edits.

        Non-conflicting parallel calls (distinct files, or non-write tools such as
        `read_file`/`grep`) are left untouched.

        Args:
            state: The current agent state (must contain `messages`).

        Returns:
            A state update `{"messages": [...]}` (the rewritten AIMessage by id plus
            the new error ToolMessages) when a conflict is found, otherwise None.
        """
        messages = state.get("messages")
        if not messages:
            return None

        last_ai_msg: AIMessage | None = None
        for msg in reversed(messages):
            if isinstance(msg, AIMessage):
                last_ai_msg = msg
                break

        if last_ai_msg is None or not last_ai_msg.tool_calls:
            return None

        kept, conflicts = _detect_parallel_write_conflicts(last_ai_msg)
        if not conflicts:
            return None

        # Rewrite the AIMessage with the conflicting tool calls removed. Preserving
        # the original id makes the add_messages reducer overwrite it in place.
        rewritten_ai = AIMessage(
            content=last_ai_msg.content,
            id=last_ai_msg.id,
            name=last_ai_msg.name,
            tool_calls=kept,
            additional_kwargs=dict(last_ai_msg.additional_kwargs),
            response_metadata=dict(last_ai_msg.response_metadata),
        )

        return {"messages": [rewritten_ai, *conflicts]}

    def after_model(self, state: AgentState[Any], runtime: Runtime[ContextT]) -> dict[str, Any] | None:
        """Reject conflicting parallel file mutations after each model call.

        Args:
            state: The current agent state.
            runtime: The langgraph runtime (unused).

        Returns:
            A state update when conflicting parallel writes were rejected, else None.
        """
        return self._guard_parallel_writes(state)

    async def aafter_model(self, state: AgentState[Any], runtime: Runtime[ContextT]) -> dict[str, Any] | None:
        """(async) Reject conflicting parallel file mutations after each model call.

        Args:
            state: The current agent state.
            runtime: The langgraph runtime (unused).

        Returns:
            A state update when conflicting parallel writes were rejected, else None.
        """
        return self._guard_parallel_writes(state)

    def _process_large_message(
        self,
        message: ToolMessage,
        resolved_backend: BackendProtocol,
    ) -> tuple[ToolMessage, dict[str, FileData] | None]:
        """Process a large ToolMessage by evicting its content to filesystem.

        Args:
            message: The ToolMessage with large content to evict.
            resolved_backend: The filesystem backend to write the content to.

        Returns:
            A tuple of (processed_message, files_update):
            - processed_message: New ToolMessage with truncated content and file reference
            - files_update: Dict of file updates to apply to state, or None if eviction failed

        Note:
            Text is extracted from all text content blocks, joined, and used for both the
            size check and eviction. Non-text blocks (images, audio, etc.) are preserved in
            the replacement message so multimodal context is not lost. The model can recover
            the full text by reading the offloaded file from the backend.
        """
        # Early exit if eviction not configured
        if not self._tool_token_limit_before_evict:
            return message, None

        content_str = _extract_text_from_message(message)

        # Check if content exceeds eviction threshold
        if len(content_str) <= NUM_CHARS_PER_TOKEN * self._tool_token_limit_before_evict:
            return message, None

        # Write content to filesystem
        file_path = self._artifact_path(resolved_backend, message.tool_call_id)
        result = resolved_backend.write(file_path, content_str)
        if result.error:
            return message, None

        # Create preview showing head and tail of the result
        content_sample = _create_content_preview(content_str)
        replacement_text = TOO_LARGE_TOOL_MSG.format(
            tool_call_id=message.tool_call_id,
            file_path=file_path,
            content_sample=content_sample,
        )

        evicted = _build_evicted_content(message, replacement_text)
        processed_message = ToolMessage(
            content=cast("str | list[str | dict]", evicted),
            tool_call_id=message.tool_call_id,
            name=message.name,
            id=message.id,
            artifact=message.artifact,
            status=message.status,
            additional_kwargs=dict(message.additional_kwargs),
            response_metadata=dict(message.response_metadata),
        )
        return processed_message, result.files_update

    async def _aprocess_large_message(
        self,
        message: ToolMessage,
        resolved_backend: BackendProtocol,
    ) -> tuple[ToolMessage, dict[str, FileData] | None]:
        """Async version of _process_large_message.

        Uses async backend methods to avoid sync calls in async context.
        See _process_large_message for full documentation.
        """
        # Early exit if eviction not configured
        if not self._tool_token_limit_before_evict:
            return message, None

        content_str = _extract_text_from_message(message)

        if len(content_str) <= NUM_CHARS_PER_TOKEN * self._tool_token_limit_before_evict:
            return message, None

        # Write content to filesystem using async method
        file_path = self._artifact_path(resolved_backend, message.tool_call_id)
        result = await resolved_backend.awrite(file_path, content_str)
        if result.error:
            return message, None

        # Create preview showing head and tail of the result
        content_sample = _create_content_preview(content_str)
        replacement_text = TOO_LARGE_TOOL_MSG.format(
            tool_call_id=message.tool_call_id,
            file_path=file_path,
            content_sample=content_sample,
        )

        evicted = _build_evicted_content(message, replacement_text)
        processed_message = ToolMessage(
            content=cast("str | list[str | dict]", evicted),
            tool_call_id=message.tool_call_id,
            name=message.name,
            id=message.id,
            artifact=message.artifact,
            status=message.status,
            additional_kwargs=dict(message.additional_kwargs),
            response_metadata=dict(message.response_metadata),
        )
        return processed_message, result.files_update

    def _intercept_large_tool_result(self, tool_result: ToolMessage | Command, runtime: ToolRuntime) -> ToolMessage | Command:
        """Intercept and process large tool results before they're added to state.

        Args:
            tool_result: The tool result to potentially evict (ToolMessage or Command).
            runtime: The tool runtime providing access to the filesystem backend.

        Returns:
            Either the original result (if small enough) or a Command with evicted
            content written to filesystem and truncated message.

        Note:
            Handles both single ToolMessage results and Command objects containing
            multiple messages. Large content is automatically offloaded to filesystem
            to prevent context window overflow.
        """
        if isinstance(tool_result, ToolMessage):
            resolved_backend = self._get_backend(runtime)
            processed_message, files_update = self._process_large_message(
                tool_result,
                resolved_backend,
            )
            return (
                Command(
                    update={
                        "files": files_update,
                        "messages": [processed_message],
                    }
                )
                if files_update is not None
                else processed_message
            )

        if isinstance(tool_result, Command):
            update = tool_result.update
            if update is None:
                return tool_result
            command_messages = update.get("messages", [])
            accumulated_file_updates = dict(update.get("files", {}))
            resolved_backend = self._get_backend(runtime)
            processed_messages = []
            for message in command_messages:
                if not isinstance(message, ToolMessage):
                    processed_messages.append(message)
                    continue

                processed_message, files_update = self._process_large_message(
                    message,
                    resolved_backend,
                )
                processed_messages.append(processed_message)
                if files_update is not None:
                    accumulated_file_updates.update(files_update)
            return Command(update={**update, "messages": processed_messages, "files": accumulated_file_updates})
        msg = f"Unreachable code reached in _intercept_large_tool_result: for tool_result of type {type(tool_result)}"
        raise AssertionError(msg)

    async def _aintercept_large_tool_result(self, tool_result: ToolMessage | Command, runtime: ToolRuntime) -> ToolMessage | Command:
        """Async version of _intercept_large_tool_result.

        Uses async backend methods to avoid sync calls in async context.
        See _intercept_large_tool_result for full documentation.
        """
        if isinstance(tool_result, ToolMessage):
            resolved_backend = self._get_backend(runtime)
            processed_message, files_update = await self._aprocess_large_message(
                tool_result,
                resolved_backend,
            )
            return (
                Command(
                    update={
                        "files": files_update,
                        "messages": [processed_message],
                    }
                )
                if files_update is not None
                else processed_message
            )

        if isinstance(tool_result, Command):
            update = tool_result.update
            if update is None:
                return tool_result
            command_messages = update.get("messages", [])
            accumulated_file_updates = dict(update.get("files", {}))
            resolved_backend = self._get_backend(runtime)
            processed_messages = []
            for message in command_messages:
                if not isinstance(message, ToolMessage):
                    processed_messages.append(message)
                    continue

                processed_message, files_update = await self._aprocess_large_message(
                    message,
                    resolved_backend,
                )
                processed_messages.append(processed_message)
                if files_update is not None:
                    accumulated_file_updates.update(files_update)
            return Command(update={**update, "messages": processed_messages, "files": accumulated_file_updates})
        msg = f"Unreachable code reached in _aintercept_large_tool_result: for tool_result of type {type(tool_result)}"
        raise AssertionError(msg)

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command],
    ) -> ToolMessage | Command:
        """Check the size of the tool call result and evict to filesystem if too large.

        Args:
            request: The tool call request being processed.
            handler: The handler function to call with the modified request.

        Returns:
            The raw ToolMessage, or a pseudo tool message with the ToolResult in state.
        """
        if self._tool_token_limit_before_evict is None or request.tool_call["name"] in TOOLS_EXCLUDED_FROM_EVICTION:
            return handler(request)

        tool_result = handler(request)
        return self._intercept_large_tool_result(tool_result, request.runtime)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        """(async)Check the size of the tool call result and evict to filesystem if too large.

        Args:
            request: The tool call request being processed.
            handler: The handler function to call with the modified request.

        Returns:
            The raw ToolMessage, or a pseudo tool message with the ToolResult in state.
        """
        if self._tool_token_limit_before_evict is None or request.tool_call["name"] in TOOLS_EXCLUDED_FROM_EVICTION:
            return await handler(request)

        tool_result = await handler(request)
        return await self._aintercept_large_tool_result(tool_result, request.runtime)
