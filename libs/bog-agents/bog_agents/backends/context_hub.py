"""`ContextHubBackend`: store files in a LangSmith Hub agent repo (persistent, versioned).

The hub repo is the source of truth: every `write` / `edit` / `delete` / `upload_files`
call pushes a commit, and the backend keeps a local cache of the file tree so reads do
not round-trip to the network on every call.

`langsmith` is imported lazily (see `_langsmith`) so that importing
`bog_agents.backends` — or any middleware that transitively touches it — does not pull
the LangSmith client into every process. Install with `pip install "bog-agents[hub]"`.
"""

from __future__ import annotations

import functools
import logging
import re
from typing import TYPE_CHECKING, Any, NamedTuple

from bog_agents.backends.protocol import (
    FILE_NOT_FOUND,
    INVALID_PATH,
    BackendProtocol,
    DeleteResult,
    EditResult,
    FileData,
    FileDownloadResponse,
    FileInfo,
    FileUploadResponse,
    GlobResult,
    GrepResult,
    LsResult,
    ReadResult,
    WriteResult,
)
from bog_agents.backends.utils import (
    _glob_search_files,
    create_file_data,
    file_data_to_string,
    grep_matches_from_files,
    perform_string_replacement,
    slice_read_response,
)

if TYPE_CHECKING:
    from langsmith import Client
    from langsmith.schemas import AgentContext

logger = logging.getLogger(__name__)

_URL_COMMIT_SUFFIX_RE = re.compile(r":([0-9a-f]{8,64})$")
"""Matches the `":<hash>"` suffix appended by langsmith's `_build_context_url`."""

_MISSING_LANGSMITH_MSG = "ContextHubBackend requires the `langsmith` package. Install it with: pip install 'bog-agents[hub]'"


class _LangSmith(NamedTuple):
    """The `langsmith` symbols `ContextHubBackend` needs, resolved lazily."""

    client_cls: Any
    file_entry: Any
    error: type[Exception]
    not_found: type[Exception]


@functools.lru_cache(maxsize=1)
def _langsmith() -> _LangSmith:
    """Import `langsmith` on first use.

    Returns:
        The resolved LangSmith symbols.

    Raises:
        ImportError: If `langsmith` is not installed.
    """
    try:
        from langsmith import Client
        from langsmith.schemas import FileEntry
        from langsmith.utils import LangSmithError, LangSmithNotFoundError
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise ImportError(_MISSING_LANGSMITH_MSG) from exc

    return _LangSmith(client_cls=Client, file_entry=FileEntry, error=LangSmithError, not_found=LangSmithNotFoundError)


class ContextHubBackend(BackendProtocol):
    """Backend that stores files in a LangSmith Hub agent repo (persistent).

    Every mutation is pushed as a commit against the last-known parent commit, so the
    agent's filesystem is versioned and shareable across runs and machines.

    Divergences from the in-memory backends, both deliberate:

    - `grep` performs a *literal* substring search (bog's cross-backend contract),
        not a regex search.
    - Content is text-only: the hub stores file bodies as strings, so binary uploads
        are rejected with `invalid_path`.
    """

    def __init__(
        self,
        identifier: str,
        *,
        client: Client | None = None,
    ) -> None:
        """Initialize the backend.

        Args:
            identifier: Hub agent repo, as `"owner/name"` or `"-/name"`.
            client: LangSmith client. Defaults to a fresh `langsmith.Client()`.
        """
        self._identifier = identifier
        self._client: Any = client if client is not None else _langsmith().client_cls()
        self._cache: dict[str, str] | None = None
        self._linked_entries: dict[str, str] = {}
        self._commit_hash: str | None = None

    # -- cache ---------------------------------------------------------------

    def _load_tree(self) -> None:
        """Fetch the file tree; a missing repo is treated as an empty one."""
        try:
            context: AgentContext = self._client.pull_agent(self._identifier)
        except _langsmith().not_found:
            self._cache = {}
            self._linked_entries = {}
            self._commit_hash = None
            return

        self._commit_hash = context.commit_hash
        self._cache = {}
        self._linked_entries = {}

        for path, entry in context.files.items():
            content = getattr(entry, "content", None)
            if content is not None:
                self._cache[path] = content
            else:
                self._linked_entries[path] = entry.repo_handle

    def _ensure_cache(self) -> dict[str, str]:
        """Load the file tree if it has not been loaded yet.

        Returns:
            The path -> content cache.

        Raises:
            RuntimeError: If the tree loaded but the cache was left unset.
        """
        if self._cache is None:
            self._load_tree()
        if self._cache is None:
            msg = "Context Hub cache failed to initialize"
            raise RuntimeError(msg)
        return self._cache

    def get_linked_entries(self) -> dict[str, str]:
        """Return linked-entry paths mapped to their repo handles.

        Linked entries are hub entries that point at another repo (agents, skills)
        rather than carrying inline file content.

        Returns:
            Mapping of hub path to repo handle.
        """
        self._ensure_cache()
        return dict(self._linked_entries)

    def has_prior_commits(self) -> bool:
        """Report whether the hub repo already exists with at least one commit.

        Returns:
            `True` if a parent commit hash is known.
        """
        self._ensure_cache()
        return self._commit_hash is not None

    def _commit(self, changes: dict[str, str | None]) -> None:
        """Push `changes` as one commit and update the cache on success.

        Args:
            changes: Mapping of hub-relative path to new content. A `None` value is
                the deletion marker: relative to `parent_commit`, the server drops
                that path from the tree.
        """
        if not changes:
            return

        file_entry = _langsmith().file_entry
        payload: dict[str, Any] = {
            path: file_entry(type="file", content=content) if content is not None else None for path, content in changes.items()
        }
        url = self._client.push_agent(
            self._identifier,
            files=payload,
            parent_commit=self._commit_hash,
        )
        match = _URL_COMMIT_SUFFIX_RE.search(url)
        if match:
            self._commit_hash = match.group(1)

        if self._cache is not None:
            # Rebuild rather than mutate in place: drop paths whose new content is
            # None (deletions) and overlay the rest as updates.
            deletions = {path for path, content in changes.items() if content is None}
            updates = {path: content for path, content in changes.items() if content is not None}
            self._cache = {path: content for path, content in self._cache.items() if path not in deletions} | updates

    @staticmethod
    def _strip_prefix(path: str) -> str:
        """Convert an absolute agent path to its hub-relative key.

        Args:
            path: Absolute path (e.g. `/notes/todo.md`).

        Returns:
            Hub-relative key (e.g. `notes/todo.md`).
        """
        return path.lstrip("/")

    def _files_view(self) -> dict[str, FileData]:
        """Project the cache into the absolute-path `FileData` mapping shared helpers expect.

        Returns:
            Mapping of absolute path to `FileData`.
        """
        cache = self._ensure_cache()
        return {f"/{path}": FileData(content=content, encoding="utf-8") for path, content in cache.items()}

    # -- structured API ------------------------------------------------------

    def ls(self, path: str = "/") -> LsResult:
        """List immediate files and subdirectories under `path` (non-recursive).

        Args:
            path: Absolute directory path.

        Returns:
            `LsResult` with the directory's entries, or an error if the hub is
                unavailable. Directory entries carry a trailing `/` and `is_dir=True`.
        """
        try:
            cache = self._ensure_cache()
        except _langsmith().error as exc:
            logger.exception("Hub pull failed for %r", self._identifier)
            return LsResult(error=f"Hub unavailable: {exc}")

        prefix = "/" + self._strip_prefix(path)
        if not prefix.endswith("/"):
            prefix += "/"

        entries: list[FileInfo] = []
        subdirs: set[str] = set()

        for hub_path, content in cache.items():
            file_path = f"/{hub_path}"
            if not file_path.startswith(prefix):
                continue

            relative = file_path[len(prefix) :]
            if not relative:
                continue

            if "/" in relative:
                subdir = prefix + relative.split("/", 1)[0] + "/"
                if subdir not in subdirs:
                    subdirs.add(subdir)
                    entries.append(FileInfo(path=subdir, is_dir=True))
                continue

            entries.append(FileInfo(path=file_path, is_dir=False, size=len(content.encode("utf-8"))))

        return LsResult(entries=entries)

    def read_file(
        self,
        file_path: str,
        offset: int = 0,
        limit: int = 2000,
    ) -> ReadResult:
        """Read raw file data for a line window.

        Args:
            file_path: Absolute file path.
            offset: Line number to start reading from (0-indexed).
            limit: Maximum number of lines to read.

        Returns:
            `ReadResult` with the sliced `FileData`, or an error message.
        """
        hub_path = self._strip_prefix(file_path)
        try:
            cache = self._ensure_cache()
        except _langsmith().error as exc:
            logger.exception("Hub pull failed for %r", self._identifier)
            return ReadResult(error=f"Hub unavailable: {exc}")

        content = cache.get(hub_path)
        if content is None:
            return ReadResult(error=f"Error: File '{file_path}' not found")

        file_data = create_file_data(content)
        sliced = slice_read_response(file_data, offset, limit)
        if isinstance(sliced, ReadResult):
            return sliced
        return ReadResult(file_data=FileData(content=sliced, encoding="utf-8", modified_at=file_data["modified_at"]))

    def write(self, file_path: str, content: str) -> WriteResult:
        """Commit `content` to `file_path`, creating it or overwriting an existing file.

        Args:
            file_path: Absolute file path.
            content: Content to store.

        Returns:
            `WriteResult` with the written path. `files_update` is `None` — the hub is
                external storage, so there is nothing for the caller to persist.
        """
        hub_path = self._strip_prefix(file_path)
        try:
            self._ensure_cache()  # populates _commit_hash for parent_commit on push
            self._commit({hub_path: content})
        except _langsmith().error as exc:
            logger.exception("Hub write failed for %r", self._identifier)
            self._cache = None
            return WriteResult(error=f"Hub unavailable: {exc}")
        return WriteResult(path=file_path)

    def edit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
        *,
        base_content: dict[str, Any] | None = None,
    ) -> EditResult:
        """Replace `old_string` with `new_string` and commit the result.

        Args:
            file_path: Absolute file path.
            old_string: Exact string to find.
            new_string: Replacement string.
            replace_all: If `True`, replace every occurrence.
            base_content: Optional `FileData` to edit against instead of the cached
                copy. Batch callers (`multi_edit_file`) pass a prior edit's result so
                chained edits to the same file compose.

        Returns:
            `EditResult` with the occurrence count, or an error message.
        """
        hub_path = self._strip_prefix(file_path)
        try:
            cache = self._ensure_cache()
            current: str | None = file_data_to_string(base_content) if base_content is not None else cache.get(hub_path)
            if current is None:
                return EditResult(error=f"Error: File '{file_path}' not found")

            result = perform_string_replacement(current, old_string, new_string, replace_all)
            if isinstance(result, str):
                return EditResult(error=result)

            new_content, occurrences = result
            self._commit({hub_path: new_content})
        except _langsmith().error as exc:
            logger.exception("Hub edit failed for %r", self._identifier)
            self._cache = None
            return EditResult(error=f"Hub unavailable: {exc}")
        return EditResult(path=file_path, occurrences=occurrences)

    def delete(self, file_path: str) -> DeleteResult:
        """Delete a file, or a directory and everything nested under it.

        Removes the exact key plus every key sharing the `file_path + "/"` prefix, so
        deleting a directory is recursive.

        Args:
            file_path: Absolute path of the file or directory to delete.

        Returns:
            `DeleteResult` listing every removed path, or an error if nothing is stored
                at or under the path (or the hub is unavailable).
        """
        hub_path = self._strip_prefix(file_path)
        try:
            cache = self._ensure_cache()
            base = hub_path.rstrip("/")
            prefix = base + "/"
            to_delete = [key for key in cache if key == base or key.startswith(prefix)]
            if not to_delete:
                return DeleteResult(error=f"Error: File '{file_path}' not found")
            self._commit(dict.fromkeys(to_delete, None))
        except _langsmith().error as exc:
            logger.exception("Hub delete failed for %r", self._identifier)
            self._cache = None
            return DeleteResult(error=f"Hub unavailable: {exc}")
        return DeleteResult(path=file_path, deleted_paths=sorted(f"/{key}" for key in to_delete))

    def grep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
    ) -> GrepResult:
        """Search file contents for a literal text pattern.

        Args:
            pattern: Literal substring to search for (not a regex).
            path: Optional directory or file path to search under.
            glob: Optional include-glob filtering which files are searched.

        Returns:
            `GrepResult` with the matches, or an error message.
        """
        try:
            files = self._files_view()
        except _langsmith().error as exc:
            logger.exception("Hub pull failed for %r", self._identifier)
            return GrepResult(error=f"Hub unavailable: {exc}")

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
            `GlobResult` with the matching file infos, or an error message.
        """
        try:
            files = self._files_view()
        except _langsmith().error as exc:
            logger.exception("Hub pull failed for %r", self._identifier)
            return GlobResult(error=f"Hub unavailable: {exc}")

        result = _glob_search_files(files, pattern, path)
        if result == "No files found":
            return GlobResult(matches=[])

        matches: list[FileInfo] = []
        for file_path in result.split("\n"):
            file_data = files.get(file_path)
            size = len(file_data["content"].encode("utf-8")) if file_data else 0
            matches.append(FileInfo(path=file_path, is_dir=False, size=size))
        return GlobResult(matches=matches)

    # -- bulk transfer -------------------------------------------------------

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        """Upload text files in one commit; non-UTF-8 inputs are rejected per file.

        Args:
            files: List of `(path, content)` tuples.

        Returns:
            One `FileUploadResponse` per input, in input order.
        """
        # `None` text marks an entry we will reject as invalid.
        decoded: list[tuple[str, str | None]] = []
        valid_files: dict[str, str | None] = {}
        for path, content in files:
            try:
                text = content.decode("utf-8")
            except UnicodeDecodeError:
                decoded.append((path, None))
                continue
            decoded.append((path, text))
            valid_files[self._strip_prefix(path)] = text  # last write wins

        commit_error: str | None = None
        if valid_files:
            try:
                self._ensure_cache()
                self._commit(valid_files)
            except _langsmith().error as exc:
                logger.exception("Hub batch upload failed for %r", self._identifier)
                self._cache = None
                commit_error = f"Hub unavailable: {exc}"

        results: list[FileUploadResponse] = []
        for path, text in decoded:
            if text is None:
                results.append(FileUploadResponse(path=path, error=INVALID_PATH))
            elif commit_error is not None:
                # Backend-specific error string: the `FileOperationError` literal union
                # has no member for a hub outage.
                results.append(FileUploadResponse(path=path, error=commit_error))
            else:
                results.append(FileUploadResponse(path=path))
        return results

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        """Download files as raw bytes.

        Args:
            paths: Absolute paths to download.

        Returns:
            One `FileDownloadResponse` per input path, in input order. Missing paths
                carry `file_not_found`.
        """
        try:
            cache = self._ensure_cache()
        except _langsmith().error as exc:
            logger.exception("Hub pull failed for %r", self._identifier)
            return [FileDownloadResponse(path=p, error=f"Hub unavailable: {exc}") for p in paths]

        results: list[FileDownloadResponse] = []
        for path in paths:
            content = cache.get(self._strip_prefix(path))
            if content is not None:
                results.append(FileDownloadResponse(path=path, content=content.encode("utf-8")))
            else:
                results.append(FileDownloadResponse(path=path, error=FILE_NOT_FOUND))
        return results
