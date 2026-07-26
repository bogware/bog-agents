"""Protocol definition for pluggable memory backends.

This module defines the BackendProtocol that all backend implementations
must follow. Backends can store files in different locations (state, filesystem,
database, etc.) and provide a uniform interface for file operations.

The protocol exposes two generations of the read/list/search surface:

- The **structured** API (`ls`, `read_file`, `grep`, `glob`, `delete`) returns
    `*Result` dataclasses that carry an `error` field alongside the data.
- The **legacy** API (`ls_info`, `read`, `grep_raw`, `glob_info`) returns bare
    strings / lists and is retained as a delegating shim.

A backend may implement either generation: the base class forwards between them
via override detection, so a backend that only implements `ls_info` is still
reachable through `ls`, and one that only implements `ls` is still reachable
through `ls_info`. `read` is the one asymmetric case — it is the *rendered*
(line-numbered) form of `read_file`, so the base class can synthesize `read`
from `read_file` but not the reverse.
"""

import abc
import inspect
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Final, Literal, NotRequired, TypeAlias

import anyio
from langchain.tools import ToolRuntime
from typing_extensions import TypedDict

from bog_agents._api.deprecation import deprecated, warn_deprecated

logger = logging.getLogger(__name__)

FileFormat = Literal["v1", "v2"]
r"""File storage format version.

- `'v1'`: Legacy format — `content` stored as `list[str]` (lines split on `\n`),
    no `encoding` field.
- `'v2'`: Current format — `content` stored as a plain `str` (UTF-8 text or
    base64-encoded binary), with an `encoding` field (`"utf-8"` or `"base64"`).
"""

DEFAULT_GREP_TIMEOUT: Final = 15
"""Default timeout in seconds for one sync grep phase."""

ASYNC_GREP_TIMEOUT: Final = (2 * DEFAULT_GREP_TIMEOUT) + 5
"""Timeout in seconds for the async grep wrapper.

Gives a filesystem backend enough headroom to finish the worst-case sync path:
ripgrep timeout, then Python fallback timeout.
"""

FileOperationError = Literal[
    "file_not_found",  # Download: file doesn't exist
    "parent_not_found",  # Upload: parent directory doesn't exist
    "permission_denied",  # Both: access denied
    "is_directory",  # Download: tried to download directory as file
    "invalid_path",  # Both: path syntax malformed (parent dir missing, invalid chars)
]
"""Standardized error codes for file upload/download operations.

These represent common, recoverable errors that an LLM can understand and potentially fix:
- file_not_found: The requested file doesn't exist (download)
- parent_not_found: The parent directory doesn't exist (upload)
- permission_denied: Access denied for the operation
- is_directory: Attempted to download a directory as a file
- invalid_path: Path syntax is malformed or contains invalid characters
"""

# Named constants for the `FileOperationError` literals. Use these instead of bare
# string literals at producer/consumer sites so a rename surfaces as a type error
# rather than silently reverting to a fallback branch.
FILE_NOT_FOUND: Final = "file_not_found"
PARENT_NOT_FOUND: Final = "parent_not_found"
PERMISSION_DENIED: Final = "permission_denied"
IS_DIRECTORY: Final = "is_directory"
INVALID_PATH: Final = "invalid_path"


@dataclass
class FileDownloadResponse:
    """Result of a single file download operation.

    The response is designed to allow partial success in batch operations.
    The errors are standardized using FileOperationError literals
    for certain recoverable conditions for use cases that involve
    LLMs performing file operations.

    Attributes:
        path: The file path that was requested. Included for easy correlation
            when processing batch results, especially useful for error messages.
        content: File contents as bytes on success, None on failure.
        error: Standardized error code on failure, None on success.
            Uses FileOperationError literal for structured, LLM-actionable error reporting.

    Examples:
        >>> # Success
        >>> FileDownloadResponse(path="/app/config.json", content=b"{...}", error=None)
        >>> # Failure
        >>> FileDownloadResponse(path="/wrong/path.txt", content=None, error="file_not_found")
    """

    path: str
    content: bytes | None = None
    error: FileOperationError | str | None = None


@dataclass
class FileUploadResponse:
    """Result of a single file upload operation.

    The response is designed to allow partial success in batch operations.
    The errors are standardized using FileOperationError literals
    for certain recoverable conditions for use cases that involve
    LLMs performing file operations.

    Attributes:
        path: The file path that was requested. Included for easy correlation
            when processing batch results and for clear error messages.
        error: Standardized error code on failure, None on success.
            Uses FileOperationError literal for structured, LLM-actionable error reporting.

    Examples:
        >>> # Success
        >>> FileUploadResponse(path="/app/data.txt", error=None)
        >>> # Failure
        >>> FileUploadResponse(path="/readonly/file.txt", error="permission_denied")
    """

    path: str
    error: FileOperationError | str | None = None


class FileInfo(TypedDict):
    """Structured file listing info.

    Minimal contract used across backends. Only "path" is required.
    Other fields are best-effort and may be absent depending on backend.
    """

    path: str
    is_dir: NotRequired[bool]
    size: NotRequired[int]  # bytes (approx)
    modified_at: NotRequired[str]  # ISO timestamp if known


class ContextLine(TypedDict):
    """A non-matching line surrounding a grep match (deepagents 0.7 shape).

    Emitted in `content` output mode when context is requested. bog's grep does
    not populate context lines yet, so `GrepMatch.context_before/after` stay
    absent here today — the type exists for source-level drop-in parity so a
    0.7 consumer's `match.get("context_before")` type-checks and returns `None`.
    """

    line: int
    text: str


class GrepMatch(TypedDict):
    """Structured grep match entry.

    `context_before` / `context_after` mirror deepagents 0.7's `GrepMatch`; they
    are `NotRequired` and currently unpopulated by bog's backends (bog bounds
    grep by time rather than emitting context windows), so consumers should treat
    them as optional.
    """

    path: str
    line: int
    text: str
    context_before: NotRequired[list["ContextLine"]]
    context_after: NotRequired[list["ContextLine"]]


class FileData(TypedDict):
    r"""Data structure for storing file contents with metadata (v2 format).

    !!! note

        Legacy (v1) data may still carry `"content": list[str]` (lines split on
        `\n`) and omit `encoding`. Backends accept this for backwards
        compatibility; `bog_agents.backends.utils._normalize_content` is the
        single conversion point and emits a `DeprecationWarning`.
    """

    content: str
    """File content as a plain string (utf-8 text or base64-encoded binary)."""

    encoding: NotRequired[str]
    """Content encoding: `"utf-8"` for text, `"base64"` for binary."""

    created_at: NotRequired[str]
    """ISO 8601 timestamp of file creation."""

    modified_at: NotRequired[str]
    """ISO 8601 timestamp of last modification."""


@dataclass
class ReadResult:
    """Result from backend `read_file` operations.

    Attributes:
        error: Error message on failure, `None` on success.
        file_data: `FileData` dict on success, `None` on failure. The `content`
            it carries is the *sliced* window requested by the caller, not
            necessarily the whole file.
        total_lines: Total lines in the file, when known (`None` if not tracked).
        start_line: 1-indexed first line of the returned window, when known.
        end_line: 1-indexed last line of the returned window, when known.
        next_offset: Offset to pass to continue reading past this window, or
            `None` at EOF / when pagination is not tracked.

    The four pagination fields mirror deepagents 0.7's `ReadResult`. They are
    optional (default `None`) so bog's existing construction sites remain valid;
    a backend that doesn't track pagination simply leaves them unset.
    """

    error: str | None = None
    file_data: FileData | None = None
    total_lines: int | None = None
    start_line: int | None = None
    end_line: int | None = None
    next_offset: int | None = None

    def __post_init__(self) -> None:
        """Validate pagination fields leniently (all-None stays valid)."""
        # bog's common result leaves every pagination field None ("not tracked"),
        # which must stay valid. Only reject values that are outright impossible.
        for field_name in ("total_lines", "start_line", "end_line", "next_offset"):
            value = getattr(self, field_name)
            if value is not None and value < 0:
                msg = f"ReadResult.{field_name} must be non-negative, got {value}"
                raise ValueError(msg)


@dataclass
class LsResult:
    """Result from backend `ls` operations.

    Attributes:
        error: Error message on failure, `None` on success.
        entries: List of file info dicts on success, `None` on failure.
    """

    error: str | None = None
    entries: list["FileInfo"] | None = None


@dataclass
class GrepResult:
    """Result from backend `grep` operations.

    Attributes:
        error: Error message on failure, `None` on success.
        matches: List of grep match dicts. Populated on success and, when the
            search was cut short, with whatever was found before stopping.
            `None` only on a hard failure.
        truncated: `True` when the search stopped early (e.g. hit its time
            limit) and `matches` is therefore incomplete but still valid.
    """

    error: str | None = None
    matches: list["GrepMatch"] | None = None
    truncated: bool = False


@dataclass
class GlobResult:
    """Result from backend `glob` operations.

    Attributes:
        error: Error message on failure, `None` on success.
        matches: List of matching file info dicts. Populated on success and,
            when the walk was cut short, with whatever was found before
            stopping. `None` only on a hard failure.
        truncated: `True` when the walk stopped early (e.g. hit its time limit)
            and `matches` is therefore incomplete but still valid.
    """

    error: str | None = None
    matches: list["FileInfo"] | None = None
    truncated: bool = False


@dataclass
class WriteResult:
    """Result from backend write operations.

    Attributes:
        error: Error message on failure, None on success.
        path: Absolute path of written file, None on failure.
        files_update: State update dict for checkpoint backends, None for external storage.
            Checkpoint backends populate this with {file_path: file_data} for LangGraph state.
            External backends set None (already persisted to disk/S3/database/etc).

    Examples:
        >>> # Checkpoint storage
        >>> WriteResult(path="/f.txt", files_update={"/f.txt": {...}})
        >>> # External storage
        >>> WriteResult(path="/f.txt", files_update=None)
        >>> # Error
        >>> WriteResult(error="File exists")
    """

    error: str | None = None
    path: str | None = None
    files_update: dict[str, Any] | None = None


@dataclass
class EditResult:
    """Result from backend edit operations.

    Attributes:
        error: Error message on failure, None on success.
        path: Absolute path of edited file, None on failure.
        files_update: State update dict for checkpoint backends, None for external storage.
            Checkpoint backends populate this with {file_path: file_data} for LangGraph state.
            External backends set None (already persisted to disk/S3/database/etc).
        occurrences: Number of replacements made, None on failure.

    Examples:
        >>> # Checkpoint storage
        >>> EditResult(path="/f.txt", files_update={"/f.txt": {...}}, occurrences=1)
        >>> # External storage
        >>> EditResult(path="/f.txt", files_update=None, occurrences=2)
        >>> # Error
        >>> EditResult(error="File not found")
    """

    error: str | None = None
    path: str | None = None
    files_update: dict[str, Any] | None = None
    occurrences: int | None = None


@dataclass
class DeleteResult:
    """Result from backend `delete` operations.

    Deletion is recursive: it removes `path` plus everything nested under it.

    Attributes:
        error: Error message on failure, `None` on success.
        path: Absolute path of the deleted file, `None` on failure.
        files_update: State update dict for checkpoint backends, `None` for
            external storage. Unlike upstream deepagents — which pushes state
            updates out-of-band through the tool runtime — bog's state- and
            store-backed backends return their updates to the caller, so a
            recursive delete must be able to round-trip the removed keys.
        deleted_paths: Every path removed by the operation. A recursive delete
            of a directory reports each nested file, so callers can invalidate
            per-path caches without re-listing.

    Examples:
        >>> DeleteResult(path="/f.txt", deleted_paths=["/f.txt"])
        >>> DeleteResult(error="File not found")
    """

    error: str | None = None
    path: str | None = None
    files_update: dict[str, Any] | None = None
    deleted_paths: list[str] = field(default_factory=list)


def _render_read_result(result: ReadResult, start_line: int) -> str:
    """Render a `ReadResult` into the legacy line-numbered string form.

    Args:
        result: The structured read result to render.
        start_line: 1-indexed line number of the first line in
            `result.file_data`'s content window.

    Returns:
        The error message on failure, the empty-content warning when the window
            is blank, or `cat -n`-style numbered content.
    """
    # Imported here rather than at module scope: `backends.utils` imports this
    # module for its types, so a top-level import would be circular.
    from bog_agents.backends.utils import EMPTY_CONTENT_WARNING, file_data_to_string, format_content_with_line_numbers

    if result.error is not None:
        return result.error
    if result.file_data is None:
        return EMPTY_CONTENT_WARNING
    content = file_data_to_string(result.file_data)
    if not content or content.strip() == "":
        return EMPTY_CONTENT_WARNING
    return format_content_with_line_numbers(content, start_line=start_line)


# @abstractmethod to avoid breaking subclasses that only implement a subset
class BackendProtocol(abc.ABC):  # noqa: B024
    r"""Protocol for pluggable memory backends (single, unified).

    Backends can store files in different locations (state, filesystem, database, etc.)
    and provide a uniform interface for file operations.

    All file data is represented as dicts with the following structure:
    {
        "content": str,     # Text content (utf-8) or base64-encoded binary
        "encoding": str,    # "utf-8" for text, "base64" for binary data
        "created_at": str,  # ISO format timestamp
        "modified_at": str, # ISO format timestamp
    }

    !!! note

        Legacy (v1) data may still carry `"content": list[str]` (lines split on
        `\n`). Backends accept this for backwards compatibility and emit a
        `DeprecationWarning`.
    """

    # -- structured API ------------------------------------------------------

    def ls(self, path: str) -> "LsResult":
        """List all files in a directory with metadata.

        Args:
            path: Absolute path to the directory to list. Must start with `'/'`.

        Returns:
            `LsResult` with directory entries or error.

        Raises:
            NotImplementedError: If the backend implements neither `ls` nor the
                legacy `ls_info`.
        """
        if type(self).ls_info is not BackendProtocol.ls_info:
            warn_deprecated(
                since="0.10.0",
                removal="1.0.0",
                message="`ls_info` is deprecated and will be removed in bog-agents==1.0.0; rename to `ls` instead.",
                package="bog-agents",
            )
            return LsResult(entries=self.ls_info(path))

        raise NotImplementedError

    async def als(self, path: str) -> "LsResult":
        """Async version of `ls`."""
        return await anyio.to_thread.run_sync(self.ls, path)  # ty: ignore[unresolved-attribute]

    def read_file(
        self,
        file_path: str,
        offset: int = 0,
        limit: int = 2000,
    ) -> ReadResult:
        """Read raw file data for a line window.

        This is the structured counterpart to `read`: it returns the sliced
        `FileData` rather than a rendered, line-numbered string.

        Args:
            file_path: Absolute path to the file to read. Must start with `'/'`.
            offset: Line number to start reading from (0-indexed).
            limit: Maximum number of lines to read.

        Returns:
            `ReadResult` carrying the sliced `FileData`, or an error message.

        Raises:
            NotImplementedError: If the backend does not implement `read_file`.
        """
        raise NotImplementedError

    async def aread_file(
        self,
        file_path: str,
        offset: int = 0,
        limit: int = 2000,
    ) -> ReadResult:
        """Async version of `read_file`."""
        return await anyio.to_thread.run_sync(self.read_file, file_path, offset, limit)  # ty: ignore[unresolved-attribute]

    def grep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
    ) -> "GrepResult":
        """Search for a literal text pattern in files.

        Args:
            pattern: Literal string to search for (NOT regex). Performs exact
                substring matching within file content. Example: `"TODO"`
                matches any line containing `"TODO"`.
            path: Optional directory path to search in. If `None`, searches from
                the backend's default root.
            glob: Optional glob pattern filtering which FILES to search. Filters
                by filename/path, not content. Supports `*`, `**`, `?`, `[abc]`.

        Returns:
            `GrepResult` with matches or error.

        Raises:
            NotImplementedError: If the backend implements neither `grep` nor
                the legacy `grep_raw`.
        """
        if type(self).grep_raw is not BackendProtocol.grep_raw:
            warn_deprecated(
                since="0.10.0",
                removal="1.0.0",
                message="`grep_raw` is deprecated and will be removed in bog-agents==1.0.0; rename to `grep` instead.",
                package="bog-agents",
            )
            result = self.grep_raw(pattern, path, glob)
            if isinstance(result, str):
                return GrepResult(error=result)
            return GrepResult(matches=result)

        raise NotImplementedError

    async def agrep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
    ) -> "GrepResult":
        """Async version of `grep`.

        Wraps the sync call with an async timeout as a safety net. The timeout
        bounds how long the caller waits; it does not stop the worker thread.
        """
        import functools

        fn = functools.partial(self.grep, pattern, path, glob)
        result: GrepResult | None = None
        with anyio.move_on_after(ASYNC_GREP_TIMEOUT):
            result = await anyio.to_thread.run_sync(fn, abandon_on_cancel=True)  # ty: ignore[unresolved-attribute]

        if result is None:
            logger.warning(
                "agrep timed out after %ds (pattern=%r, path=%r, glob=%r)",
                ASYNC_GREP_TIMEOUT,
                pattern,
                path,
                glob,
            )
            return GrepResult(
                error=f"Error: grep timed out after {ASYNC_GREP_TIMEOUT}s. Try a more specific pattern or a narrower path.",
            )
        return result

    def glob(self, pattern: str, path: str | None = None) -> "GlobResult":
        """Find files matching a glob pattern.

        Args:
            pattern: Glob pattern with wildcards to match file paths. Supports
                `*`, `**`, `?`, `[abc]`.
            path: Optional base directory to search from. If omitted, the backend
                chooses its default search root.

        Returns:
            `GlobResult` with matching files or error.

        Raises:
            NotImplementedError: If the backend implements neither `glob` nor
                the legacy `glob_info`.
        """
        if type(self).glob_info is not BackendProtocol.glob_info:
            warn_deprecated(
                since="0.10.0",
                removal="1.0.0",
                message="`glob_info` is deprecated and will be removed in bog-agents==1.0.0; rename to `glob` instead.",
                package="bog-agents",
            )
            return GlobResult(matches=self.glob_info(pattern, path or "/"))

        raise NotImplementedError

    async def aglob(self, pattern: str, path: str | None = None) -> "GlobResult":
        """Async version of `glob`."""
        return await anyio.to_thread.run_sync(self.glob, pattern, path)  # ty: ignore[unresolved-attribute]

    def delete(self, file_path: str) -> DeleteResult:
        """Delete a path, recursively removing anything nested under it.

        This method is optional. Backends that do not implement it inherit this
        default, which raises `NotImplementedError`. Callers that need to support
        a mix of backends should guard with `supports_delete` before calling, or
        catch `NotImplementedError`.

        On hierarchical backends (e.g. `FilesystemBackend`) recursion means a
        directory and its contents; on key-value backends it means the exact key
        plus every key sharing the `file_path + "/"` prefix.

        Args:
            file_path: Absolute path to delete (a file, or a directory/prefix to
                remove recursively). Must start with `'/'`.

        Returns:
            `DeleteResult` with the deleted path on success, or an error if
                nothing exists at or under the path or removal fails.

        Raises:
            NotImplementedError: If the backend does not implement `delete`.
        """
        raise NotImplementedError

    async def adelete(self, file_path: str) -> DeleteResult:
        """Async version of `delete`."""
        return await anyio.to_thread.run_sync(self.delete, file_path)  # ty: ignore[unresolved-attribute]

    # -- read (rendered form) ------------------------------------------------

    def read(
        self,
        file_path: str,
        offset: int = 0,
        limit: int = 2000,
    ) -> str:
        """Read file content with line numbers.

        This is the *rendered* form of `read_file`: a backend that implements
        only `read_file` gets this for free. The forwarding is one-directional —
        the numbered string cannot be reversed back into `FileData`, so a backend
        that implements only `read` must still implement `read_file` to be
        reachable through the structured API.

        Args:
            file_path: Absolute path to the file to read. Must start with '/'.
            offset: Line number to start reading from (0-indexed). Default: 0.
            limit: Maximum number of lines to read. Default: 2000.

        Returns:
            String containing file content formatted with line numbers (cat -n format),
            starting at line 1. Lines longer than 2000 characters are truncated.

            Returns an error string if the file doesn't exist or can't be read.

        Raises:
            NotImplementedError: If the backend implements neither `read` nor
                `read_file`.

        !!! note
            - Use pagination (offset/limit) for large files to avoid context overflow
            - First scan: `read(path, limit=100)` to see file structure
            - Read more: `read(path, offset=100, limit=200)` for next section
            - ALWAYS read a file before editing it
            - If file exists but is empty, you'll receive a system reminder warning
        """
        if type(self).read_file is not BackendProtocol.read_file:
            return _render_read_result(self.read_file(file_path, offset, limit), start_line=offset + 1)

        raise NotImplementedError

    async def aread(
        self,
        file_path: str,
        offset: int = 0,
        limit: int = 2000,
    ) -> str:
        """Async version of read."""
        if type(self).read is BackendProtocol.read and type(self).aread_file is not BackendProtocol.aread_file:
            result = await self.aread_file(file_path, offset, limit)
            return _render_read_result(result, start_line=offset + 1)
        return await anyio.to_thread.run_sync(self.read, file_path, offset, limit)  # ty: ignore[unresolved-attribute]

    # -- write / edit --------------------------------------------------------

    def write(
        self,
        file_path: str,
        content: str,
    ) -> WriteResult:
        """Write content to a file in the filesystem.

        Args:
            file_path: Absolute path where the file should be created.
                       Must start with '/'.
            content: String content to write to the file.

        Returns:
            WriteResult
        """
        raise NotImplementedError

    async def awrite(
        self,
        file_path: str,
        content: str,
    ) -> WriteResult:
        """Async version of write."""
        return await anyio.to_thread.run_sync(self.write, file_path, content)  # ty: ignore[unresolved-attribute]

    def edit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
        *,
        base_content: dict[str, Any] | None = None,
    ) -> EditResult:
        """Perform exact string replacements in an existing file.

        Args:
            file_path: Absolute path to the file to edit. Must start with '/'.
            old_string: Exact string to search for and replace.
                       Must match exactly including whitespace and indentation.
            new_string: String to replace old_string with.
                       Must be different from old_string.
            replace_all: If True, replace all occurrences. If False (default),
                        old_string must be unique in the file or the edit fails.
            base_content: Optional FileData dict to use as the working copy
                instead of re-reading the file from the backend's store. Used by
                batch callers (e.g. multi_edit_file) to thread an earlier edit's
                result forward so chained edits to the same file compose, even on
                state-backed stores that are not mutated mid-batch. Backends that
                already reflect prior edits in their store (e.g. on-disk
                filesystem backends) may ignore this argument.

        Returns:
            EditResult
        """
        raise NotImplementedError

    async def aedit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
        *,
        base_content: dict[str, Any] | None = None,
    ) -> EditResult:
        """Async version of edit."""
        import functools

        # Only forward ``base_content`` when a caller actually supplied one.
        # Concrete backends that override ``edit`` without the (optional) kwarg
        # — e.g. the on-disk FilesystemBackend — would otherwise raise
        # ``TypeError`` on the default ``aedit`` path. Batch callers that need
        # chained-edit content still pass it (and tolerate a TypeError fallback).
        if base_content is None:
            fn = functools.partial(self.edit, file_path, old_string, new_string, replace_all)
        else:
            fn = functools.partial(self.edit, file_path, old_string, new_string, replace_all, base_content=base_content)
        return await anyio.to_thread.run_sync(fn)  # ty: ignore[unresolved-attribute]

    # -- bulk transfer -------------------------------------------------------

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        """Upload multiple files to the sandbox.

        This API is designed to allow developers to use it either directly or
        by exposing it to LLMs via custom tools.

        Args:
            files: List of (path, content) tuples to upload.

        Returns:
            List of FileUploadResponse objects, one per input file.
            Response order matches input order (response[i] for files[i]).
            Check the error field to determine success/failure per file.

        Examples:
            ```python
            responses = sandbox.upload_files(
                [
                    ("/app/config.json", b"{...}"),
                    ("/app/data.txt", b"content"),
                ]
            )
            ```
        """
        raise NotImplementedError

    async def aupload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        """Async version of upload_files."""
        return await anyio.to_thread.run_sync(self.upload_files, files)  # ty: ignore[unresolved-attribute]

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        """Download multiple files from the sandbox.

        This API is designed to allow developers to use it either directly or
        by exposing it to LLMs via custom tools.

        Args:
            paths: List of file paths to download.

        Returns:
            List of FileDownloadResponse objects, one per input path.
            Response order matches input order (response[i] for paths[i]).
            Check the error field to determine success/failure per file.
        """
        raise NotImplementedError

    async def adownload_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        """Async version of download_files."""
        return await anyio.to_thread.run_sync(self.download_files, paths)  # ty: ignore[unresolved-attribute]

    # -- deprecated methods --------------------------------------------------

    @deprecated(since="0.10.0", removal="1.0.0", alternative="ls", package="bog-agents")
    def ls_info(self, path: str) -> list["FileInfo"]:
        """List all files in a directory with metadata.

        !!! warning "Deprecated"

            Use `ls` instead. Will be removed in `bog-agents==1.0.0`.

        Args:
            path: Absolute path to the directory to list. Must start with '/'.

        Returns:
            List of FileInfo dicts containing file metadata.

        Raises:
            NotImplementedError: If the backend's `ls` reported an error, which
                this legacy shape cannot express.
        """
        result = self.ls(path)
        if result.error is not None:
            msg = "This behavior is only available via the new `ls` API."
            raise NotImplementedError(msg)
        return result.entries or []

    @deprecated(since="0.10.0", removal="1.0.0", alternative="als", package="bog-agents")
    async def als_info(self, path: str) -> list["FileInfo"]:
        """Async version of `ls_info`.

        !!! warning "Deprecated"

            Use `als` instead. Will be removed in `bog-agents==1.0.0`.

        Args:
            path: Absolute path to the directory to list.

        Returns:
            List of FileInfo dicts containing file metadata.

        Raises:
            NotImplementedError: If the backend's `als` reported an error.
        """
        result = await self.als(path)
        if result.error is not None:
            msg = "This behavior is only available via the new `als` API."
            raise NotImplementedError(msg)
        return result.entries or []

    @deprecated(since="0.10.0", removal="1.0.0", alternative="grep", package="bog-agents")
    def grep_raw(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
    ) -> list["GrepMatch"] | str:
        """Search for a literal text pattern in files.

        !!! warning "Deprecated"

            Use `grep` instead. Will be removed in `bog-agents==1.0.0`.

        Args:
            pattern: Literal string to search for (NOT regex).
            path: Optional directory path to search in.
            glob: Optional glob pattern to filter which FILES to search.

        Returns:
            On success, a list of `GrepMatch`; on error, the error message.
        """
        result = self.grep(pattern, path, glob)
        if result.error is not None:
            return result.error
        return result.matches or []

    @deprecated(since="0.10.0", removal="1.0.0", alternative="agrep", package="bog-agents")
    async def agrep_raw(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
    ) -> list["GrepMatch"] | str:
        """Async version of `grep_raw`.

        !!! warning "Deprecated"

            Use `agrep` instead. Will be removed in `bog-agents==1.0.0`.

        Args:
            pattern: Literal string to search for (NOT regex).
            path: Optional directory path to search in.
            glob: Optional glob pattern to filter which FILES to search.

        Returns:
            On success, a list of `GrepMatch`; on error, the error message.
        """
        result = await self.agrep(pattern, path, glob)
        if result.error is not None:
            return result.error
        return result.matches or []

    @deprecated(since="0.10.0", removal="1.0.0", alternative="glob", package="bog-agents")
    def glob_info(self, pattern: str, path: str | None = "/") -> list["FileInfo"]:
        """Find files matching a glob pattern.

        !!! warning "Deprecated"

            Use `glob` instead. Will be removed in `bog-agents==1.0.0`.

        Args:
            pattern: Glob pattern with wildcards to match file paths.
            path: Base directory to search from. `None` means the backend's
                default search root.

        Returns:
            List of matching FileInfo dicts.

        Raises:
            NotImplementedError: If the backend's `glob` reported an error.
        """
        result = self.glob(pattern, path)
        if result.error is not None:
            msg = "This behavior is only available via the new `glob` API."
            raise NotImplementedError(msg)
        return result.matches or []

    @deprecated(since="0.10.0", removal="1.0.0", alternative="aglob", package="bog-agents")
    async def aglob_info(self, pattern: str, path: str | None = "/") -> list["FileInfo"]:
        """Async version of `glob_info`.

        !!! warning "Deprecated"

            Use `aglob` instead. Will be removed in `bog-agents==1.0.0`.

        Args:
            pattern: Glob pattern with wildcards to match file paths.
            path: Base directory to search from.

        Returns:
            List of matching FileInfo dicts.

        Raises:
            NotImplementedError: If the backend's `aglob` reported an error.
        """
        result = await self.aglob(pattern, path)
        if result.error is not None:
            msg = "This behavior is only available via the new `aglob` API."
            raise NotImplementedError(msg)
        return result.matches or []


@dataclass
class ExecuteResponse:
    """Result of code execution.

    Simplified schema optimized for LLM consumption.
    """

    output: str
    """Combined stdout and stderr output of the executed command."""

    exit_code: int | None = None
    """The process exit code. 0 indicates success, non-zero indicates failure."""

    truncated: bool = False
    """Whether the output was truncated due to backend limitations."""


@dataclass(frozen=True, slots=True)
class ExecuteOffloadResult:
    """Result of a sandbox `execute_with_offload` call.

    `offloaded` describes the capture mechanism and is kept off `ExecuteResponse`
    (which an ordinary `execute` never sets).
    """

    offloaded: bool
    """Whether the output was left at the capture path.

    When `True`, `response.output` holds only a head/tail preview and the full
    output lives at the capture path on the sandbox filesystem. When `False`,
    `response.output` is the complete output.
    """

    response: ExecuteResponse
    """The command result. `response.truncated` indicates the output hit the size cap."""


class SandboxBackendProtocol(BackendProtocol):
    """Extension of `BackendProtocol` that adds shell command execution.

    Designed for backends running in isolated environments (containers, VMs,
    remote hosts).

    Adds `execute()`/`aexecute()` for shell commands and an `id` property.

    See `BaseSandbox` for a base class that implements all inherited file
    operations by delegating to `execute()`.
    """

    @property
    def id(self) -> str:
        """Unique identifier for the sandbox backend instance."""
        raise NotImplementedError

    def execute(
        self,
        command: str,
        *,
        timeout: int | None = None,
    ) -> ExecuteResponse:
        """Execute a shell command in the sandbox environment.

        Simplified interface optimized for LLM consumption.

        Args:
            command: Full shell command string to execute.
            timeout: Maximum time in seconds to wait for the command to complete.

                If None, uses the backend's default timeout.

                Callers should provide non-negative integer values for portable
                behavior across backends. A value of 0 may disable timeouts on
                backends that support no-timeout execution.

        Returns:
            ExecuteResponse with combined output, exit code, and truncation flag.
        """
        raise NotImplementedError

    async def aexecute(
        self,
        command: str,
        *,
        # ASYNC109 - timeout is a semantic parameter forwarded to the sync
        # implementation, not an asyncio.timeout() contract.
        timeout: int | None = None,  # noqa: ASYNC109
    ) -> ExecuteResponse:
        """Async version of execute.

        Uses `anyio.to_thread.run_sync` so this works correctly under both
        asyncio and trio event loops, including the LangGraph remote server
        (which runs under anyio).  Using `asyncio.to_thread` inside an anyio
        context raises `BlockingError` on certain backends.
        """
        # The middleware layer validates timeout support before calling, so
        # this guard only protects direct callers bypassing the middleware.
        import functools

        if timeout is not None and execute_accepts_timeout(type(self)):
            fn = functools.partial(self.execute, command, timeout=timeout)
        else:
            fn = functools.partial(self.execute, command)
        return await anyio.to_thread.run_sync(fn, abandon_on_cancel=True)  # ty: ignore[unresolved-attribute]


@lru_cache(maxsize=128)
def execute_accepts_timeout(cls: type[SandboxBackendProtocol]) -> bool:
    """Check whether a backend class's `execute` accepts a `timeout` kwarg.

    Older backend packages didn't lower-bound their SDK dependency, so they
    may not accept the `timeout` keyword added to `SandboxBackendProtocol`.

    Results are cached per class to avoid repeated introspection overhead.

    Args:
        cls: The backend class to introspect.

    Returns:
        True if `cls.execute` declares a `timeout` parameter.
    """
    try:
        sig = inspect.signature(cls.execute)
    except (ValueError, TypeError):
        logger.warning(
            "Could not inspect signature of %s.execute; assuming timeout is not supported. This may indicate a backend packaging issue.",
            cls.__qualname__,
            exc_info=True,
        )
        return False
    else:
        return "timeout" in sig.parameters


def _supports_delete(backend: BackendProtocol) -> bool:
    """Check whether a backend implements `delete`.

    `delete` is optional: backends that don't override it inherit the
    `NotImplementedError` default from `BackendProtocol`. This helper lets
    callers detect support without invoking the method (and triggering the
    error), mirroring the override check used for the legacy
    `ls_info`/`grep_raw`/`glob_info` methods.

    Args:
        backend: The backend instance to check.

    Returns:
        True if the backend overrides `delete`, False otherwise.
    """
    return type(backend).delete is not BackendProtocol.delete


def supports_delete(backend: BackendProtocol) -> bool:
    """Public alias of `_supports_delete`.

    Args:
        backend: The backend instance to check.

    Returns:
        True if the backend overrides `delete`, False otherwise.
    """
    return _supports_delete(backend)


BackendFactory: TypeAlias = Callable[[ToolRuntime], BackendProtocol]
BACKEND_TYPES = BackendProtocol | BackendFactory


def _resolve_backend(backend: BACKEND_TYPES, runtime: ToolRuntime) -> BackendProtocol:
    """Resolve a backend instance or a backend factory to a concrete backend.

    Args:
        backend: Either a `BackendProtocol` instance or a callable taking a
            `ToolRuntime` and returning one.
        runtime: The tool runtime passed to the factory form.

    Returns:
        The resolved `BackendProtocol` instance.
    """
    if isinstance(backend, BackendProtocol):
        return backend
    # Narrow on the nominal ABC rather than `callable()`: `ty` does not narrow a
    # callable union to the factory's return type.
    return backend(runtime)
