"""StateBackend: Store files in LangGraph agent state (ephemeral)."""

import base64
import threading
from typing import TYPE_CHECKING, Any

from langgraph._internal._constants import CONFIG_KEY_READ, CONFIG_KEY_SEND
from langgraph.config import get_config

from bog_agents.backends.protocol import (
    BackendProtocol,
    DeleteResult,
    EditResult,
    FileData,
    FileDownloadResponse,
    FileFormat,
    FileInfo,
    FileUploadResponse,
    GlobResult,
    GrepResult,
    LsResult,
    ReadResult,
    WriteResult,
)
from bog_agents.backends.utils import (
    _get_backend_read_file_type,
    _glob_search_files,
    _to_legacy_file_data,
    create_file_data,
    file_data_to_string,
    grep_matches_from_files,
    perform_string_replacement,
    slice_read_response,
    update_file_data,
)

if TYPE_CHECKING:
    from langchain.tools import ToolRuntime


def _content_size(file_data: dict[str, Any]) -> int:
    r"""Return the byte-ish size of a file's content, tolerating v1 and v2 shapes.

    v2 stores `content` as a `str`; v1 (legacy) stores it as `list[str]` (lines
    split on `\n`). Taking `len()` of a v2 `str` after `"\n".join(...)` would
    interleave newlines between every character, so the shape must be checked.

    Args:
        file_data: A `FileData`-shaped dict (v1 or v2).

    Returns:
        Length of the content in characters.
    """
    raw = file_data.get("content", "")
    if isinstance(raw, list):
        return len("\n".join(raw))
    return len(raw)


def _sliced_file_data(file_data: dict[str, Any], sliced: str) -> FileData:
    """Build a `FileData` for a read window, carrying over encoding/timestamps.

    Args:
        file_data: The stored `FileData` the window was sliced from.
        sliced: The sliced content for the requested line window.

    Returns:
        A v2 `FileData` holding only the sliced window.
    """
    result = FileData(content=sliced, encoding=file_data.get("encoding", "utf-8"))
    if "created_at" in file_data:
        result["created_at"] = file_data["created_at"]
    if "modified_at" in file_data:
        result["modified_at"] = file_data["modified_at"]
    return result


class StateBackend(BackendProtocol):
    """Backend that stores files in agent state (ephemeral).

    Uses LangGraph's state management and checkpointing. Files persist within
    a conversation thread but not across threads. State is automatically
    checkpointed after each agent step.

    State is read through LangGraph's `CONFIG_KEY_READ` when the backend runs
    inside a graph execution, which yields read-your-writes semantics within a
    superstep. Outside a graph (or when an explicit `runtime` was supplied and
    no graph config is present) it falls back to `runtime.state`.

    Mutating operations return their state delta on the result's `files_update`
    field rather than pushing it out-of-band; the filesystem middleware turns
    that into a LangGraph `Command`.
    """

    def __init__(
        self,
        runtime: "ToolRuntime | None" = None,
        *,
        file_format: FileFormat = "v2",
    ) -> None:
        r"""Initialize StateBackend.

        Args:
            runtime: Optional tool runtime. When omitted, state is read from the
                ambient LangGraph config (`get_config()`), so `StateBackend()`
                works anywhere inside a graph execution.
            file_format: Storage format for newly written files. `"v2"` (default)
                stores `content` as a plain `str` with an `encoding` field;
                `"v1"` stores `content` as `list[str]` (lines split on `\n`) with
                no `encoding` field, for consumers still reading the legacy shape.
        """
        self.runtime = runtime
        self._file_format: FileFormat = file_format
        self._lock = threading.RLock()

    # -- state access --------------------------------------------------------

    def _graph_config(self) -> dict[str, Any] | None:
        """Return the ambient LangGraph config when it exposes the state channels.

        Returns:
            The config dict when running inside a graph execution that provides
                `CONFIG_KEY_READ`, otherwise `None`.
        """
        try:
            config = get_config()
        except (RuntimeError, KeyError):
            return None
        configurable = config.get("configurable") or {}
        if CONFIG_KEY_READ not in configurable:
            return None
        return dict(config)

    def _read_files(self) -> dict[str, Any]:
        """Read the current `files` mapping.

        Prefers the LangGraph `files` channel (with `fresh=True`, so writes
        queued earlier in the same superstep are visible), falling back to the
        runtime's state snapshot.

        Returns:
            Mapping of absolute path to `FileData`.

        Raises:
            RuntimeError: If the backend has no runtime and is used outside a
                LangGraph graph execution.
        """
        config = self._graph_config()
        if config is not None:
            read = config["configurable"][CONFIG_KEY_READ]
            try:
                files = read("files", True)
            except (KeyError, TypeError, ValueError):
                files = None
            if files:
                return dict(files)
            if self.runtime is None:
                return {}

        if self.runtime is None:
            msg = (
                "StateBackend() without a runtime must be used inside a LangGraph graph execution. "
                'To pre-populate files outside a graph, pass them on invoke: agent.invoke({"messages": [...], "files": {...}})'
            )
            raise RuntimeError(msg)

        with self._lock:
            return dict(self.runtime.state.get("files", {}) or {})

    def _send_files_update(self, update: dict[str, Any]) -> None:
        """Queue a partial `files` update on the LangGraph channel, if available.

        This is a best-effort supplement to the `files_update` returned on the
        result: it makes the write visible to a later `_read_files` inside the
        same superstep. Outside a graph execution it is a no-op, and the caller's
        `files_update` remains the only channel for the delta.

        Args:
            update: Partial `files` mapping; a `None` value marks a deletion.
        """
        config = self._graph_config()
        if config is None:
            return
        send = config["configurable"].get(CONFIG_KEY_SEND)
        if send is None:
            return
        send([("files", update)])

    def _prepare_for_storage(self, file_data: dict[str, Any]) -> dict[str, Any]:
        """Convert `FileData` into the configured on-state storage format.

        Args:
            file_data: `FileData` to persist.

        Returns:
            The dict actually stored in the `files` channel.
        """
        if self._file_format == "v1":
            return _to_legacy_file_data(file_data)
        return {**file_data}

    # -- structured API ------------------------------------------------------

    def ls(self, path: str) -> LsResult:
        """List files and directories in the specified directory (non-recursive).

        Args:
            path: Absolute path to directory.

        Returns:
            `LsResult` whose entries cover files and directories directly in the
                directory. Directories have a trailing `/` and `is_dir=True`.
        """
        files = self._read_files()
        infos: list[FileInfo] = []
        subdirs: set[str] = set()

        normalized_path = path if path.endswith("/") else path + "/"

        for k, fd in files.items():
            if not k.startswith(normalized_path):
                continue

            relative = k[len(normalized_path) :]

            if "/" in relative:
                subdir_name = relative.split("/")[0]
                subdirs.add(normalized_path + subdir_name + "/")
                continue

            infos.append(
                {
                    "path": k,
                    "is_dir": False,
                    "size": _content_size(fd),
                    "modified_at": fd.get("modified_at", ""),
                }
            )

        infos.extend(FileInfo(path=subdir, is_dir=True, size=0, modified_at="") for subdir in sorted(subdirs))

        infos.sort(key=lambda x: x.get("path", ""))
        return LsResult(entries=infos)

    def read_file(
        self,
        file_path: str,
        offset: int = 0,
        limit: int = 2000,
    ) -> ReadResult:
        """Read raw file data for a line window.

        Args:
            file_path: Absolute file path.
            offset: Line offset to start reading from (0-indexed).
            limit: Maximum number of lines to read.

        Returns:
            `ReadResult` with the sliced `FileData`, or an error message.
        """
        files = self._read_files()
        file_data = files.get(file_path)

        if file_data is None:
            return ReadResult(error=f"Error: File '{file_path}' not found")

        if _get_backend_read_file_type(file_path) != "text":
            return ReadResult(file_data=file_data)

        sliced = slice_read_response(file_data, offset, limit)
        if isinstance(sliced, ReadResult):
            return sliced
        return ReadResult(file_data=_sliced_file_data(file_data, sliced))

    def write(
        self,
        file_path: str,
        content: str,
    ) -> WriteResult:
        """Write content to a file, creating it or overwriting an existing one.

        Args:
            file_path: Absolute file path.
            content: Content to store.

        Returns:
            `WriteResult` carrying the `files_update` the caller applies to state.
        """
        files = self._read_files()
        existing = files.get(file_path)
        new_file_data = update_file_data(existing, content) if existing is not None else create_file_data(content)

        update = {file_path: self._prepare_for_storage(new_file_data)}
        self._send_files_update(update)
        return WriteResult(path=file_path, files_update=update)

    def edit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
        *,
        base_content: dict[str, Any] | None = None,
    ) -> EditResult:
        """Edit a file by replacing string occurrences.

        Args:
            file_path: Absolute file path to edit.
            old_string: Exact string to find.
            new_string: Replacement string.
            replace_all: If True, replace all occurrences.
            base_content: Optional FileData dict to edit against instead of
                re-reading from state. Batch callers (multi_edit_file) pass the
                result of a prior edit so chained edits to the same file compose
                — state is only mutated at the end of the batch via the returned
                Command, so re-reading here would discard intermediate edits.

        Returns:
            `EditResult` with `files_update` and the occurrence count.
        """
        if base_content is not None:
            file_data = base_content
        else:
            file_data = self._read_files().get(file_path)

        if file_data is None:
            return EditResult(error=f"Error: File '{file_path}' not found")

        content = file_data_to_string(file_data)
        result = perform_string_replacement(content, old_string, new_string, replace_all)

        if isinstance(result, str):
            return EditResult(error=result)

        new_content, occurrences = result
        new_file_data = update_file_data(file_data, new_content)

        update = {file_path: self._prepare_for_storage(new_file_data)}
        self._send_files_update(update)
        return EditResult(path=file_path, files_update=update, occurrences=int(occurrences))

    def delete(self, file_path: str) -> DeleteResult:
        """Delete a file, or a directory and everything nested under it.

        Removes the exact key `file_path` plus every key sharing the
        `file_path + "/"` prefix, so deleting a directory is recursive.

        Args:
            file_path: Absolute path of the file or directory to delete.

        Returns:
            `DeleteResult` whose `files_update` maps each removed key to `None`
                (the `files` reducer's deletion marker), or an error when nothing
                is stored at or under the path.
        """
        files = self._read_files()

        base = file_path.rstrip("/") or "/"
        prefix = base + "/"
        to_delete = [key for key in files if key == base or key.startswith(prefix)]
        if not to_delete:
            return DeleteResult(error=f"Error: File '{file_path}' not found")

        update: dict[str, Any] = dict.fromkeys(to_delete, None)
        self._send_files_update(update)
        return DeleteResult(path=file_path, files_update=update, deleted_paths=sorted(to_delete))

    def grep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
    ) -> GrepResult:
        """Search state files for a literal text pattern.

        Args:
            pattern: Literal substring to search for (not a regex).
            path: Optional directory or file path to search under.
            glob: Optional include-glob filtering which files are searched.

        Returns:
            `GrepResult` with the matches, or an error message.
        """
        files = self._read_files()
        matches = grep_matches_from_files(files, pattern, path if path is not None else "/", glob)
        if isinstance(matches, str):
            return GrepResult(error=matches)
        return GrepResult(matches=matches)

    def glob(self, pattern: str, path: str | None = None) -> GlobResult:
        """Find files matching a glob pattern.

        Args:
            pattern: Glob pattern to match against paths.
            path: Optional base directory to search from.

        Returns:
            `GlobResult` with the matching file infos.
        """
        files = self._read_files()
        result = _glob_search_files(files, pattern, path)
        if result == "No files found":
            return GlobResult(matches=[])

        infos: list[FileInfo] = []
        for p in result.split("\n"):
            fd = files.get(p)
            infos.append(
                {
                    "path": p,
                    "is_dir": False,
                    "size": _content_size(fd) if fd else 0,
                    "modified_at": fd.get("modified_at", "") if fd else "",
                }
            )
        return GlobResult(matches=infos)

    # -- bulk transfer -------------------------------------------------------

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        """Upload multiple files into state.

        Text payloads are stored as UTF-8; payloads that do not decode as UTF-8
        are base64-encoded and tagged with `encoding="base64"`. Binary payloads
        are always stored in the v2 shape even when `file_format="v1"`, because
        v1 has no `encoding` field to record them with.

        Unlike `write`/`edit`, the protocol gives `upload_files` no result object
        to hand a `files_update` back on, so the update is applied directly: via
        the LangGraph `files` channel inside a graph, or onto `runtime.state`
        outside one.

        Args:
            files: List of `(path, content)` tuples to upload.

        Returns:
            List of `FileUploadResponse` objects, one per input file, in input order.
        """
        existing = self._read_files()
        responses: list[FileUploadResponse] = []
        update: dict[str, Any] = {}

        for path, content in files:
            try:
                text = content.decode("utf-8")
                encoding = "utf-8"
            except UnicodeDecodeError:
                text = base64.standard_b64encode(content).decode("ascii")
                encoding = "base64"

            if encoding == "base64":
                update[path] = {**create_file_data(text, encoding=encoding)}
            else:
                prev = existing.get(path)
                file_data = update_file_data(prev, text) if prev is not None else create_file_data(text)
                update[path] = self._prepare_for_storage(file_data)
            responses.append(FileUploadResponse(path=path, error=None))

        if update:
            self._apply_upload_update(update)
        return responses

    def _apply_upload_update(self, update: dict[str, Any]) -> None:
        """Persist an upload's state delta, in-graph or against the runtime snapshot.

        Args:
            update: Partial `files` mapping to merge in.
        """
        if self._graph_config() is not None:
            self._send_files_update(update)
            return
        if self.runtime is None:
            return
        with self._lock:
            state_files = self.runtime.state.setdefault("files", {})
            state_files.update(update)

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        """Download multiple files from state.

        Args:
            paths: List of file paths to download.

        Returns:
            List of `FileDownloadResponse` objects, one per input path, in input order.
        """
        state_files = self._read_files()
        responses: list[FileDownloadResponse] = []

        for path in paths:
            file_data = state_files.get(path)

            if file_data is None:
                responses.append(FileDownloadResponse(path=path, content=None, error="file_not_found"))
                continue

            content_str = file_data_to_string(file_data)
            if file_data.get("encoding") == "base64":
                content_bytes = base64.standard_b64decode(content_str)
            else:
                content_bytes = content_str.encode("utf-8")

            responses.append(FileDownloadResponse(path=path, content=content_bytes, error=None))

        return responses
