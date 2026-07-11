"""Composite backend that routes file operations by path prefix.

Routes operations to different backends based on path prefixes. Use this when you
need different storage strategies for different paths (e.g., state for temp files,
persistent store for memories).

Examples:
    ```python
    from bog_agents.backends.composite import CompositeBackend
    from bog_agents.backends.state import StateBackend
    from bog_agents.backends.store import StoreBackend

    runtime = make_runtime()
    composite = CompositeBackend(default=StateBackend(runtime), routes={"/memories/": StoreBackend(runtime)})

    composite.write("/temp.txt", "ephemeral")
    composite.write("/memories/note.md", "persistent")
    ```
"""

import logging
from collections import defaultdict
from dataclasses import replace
from typing import Any, cast

from bog_agents.backends.protocol import (
    BackendProtocol,
    DeleteResult,
    EditResult,
    ExecuteResponse,
    FileDownloadResponse,
    FileInfo,
    FileUploadResponse,
    GlobResult,
    GrepMatch,
    GrepResult,
    LsResult,
    ReadResult,
    SandboxBackendProtocol,
    WriteResult,
    execute_accepts_timeout,
    supports_delete,
)
from bog_agents.backends.state import StateBackend

logger = logging.getLogger(__name__)

_DELETE_UNSUPPORTED_ERROR = "Error: deletion is not supported for '{file_path}'."


def _prefix_for(route_prefix: str) -> str:
    """Return the route prefix in the form used to rebuild virtual paths.

    Routes may be registered with or without a trailing slash (`_route_for_path`
    accepts both), and the backend-local paths handed back to us always start
    with `/`. Stripping the trailing slash — rather than dropping the last
    character — keeps `"/abcd"` from being rebuilt as `"/abc"`.

    Args:
        route_prefix: The route prefix as registered (e.g. `/memories/` or `/abcd`).

    Returns:
        The prefix with any trailing slash removed.
    """
    return route_prefix.rstrip("/")


def _remap_grep_path(m: GrepMatch, route_prefix: str) -> GrepMatch:
    """Create a new GrepMatch with the route prefix prepended to the path."""
    return cast(
        "GrepMatch",
        {
            **m,
            "path": f"{_prefix_for(route_prefix)}{m['path']}",
        },
    )


def _strip_route_from_pattern(pattern: str, route_prefix: str) -> str:
    """Strip a route prefix from a glob pattern when the pattern targets that route.

    If the pattern (ignoring a leading `/`) starts with the route prefix
    (also ignoring its leading `/`), the overlapping prefix is removed so
    the pattern is relative to the backend's internal root.

    Args:
        pattern: The glob pattern, possibly absolute (e.g. `/memories/**/*.md`).
        route_prefix: The route prefix (e.g. `/memories/`).

    Returns:
        The pattern with the route prefix stripped, or the original pattern
        if it doesn't match the route.
    """
    bare_pattern = pattern.lstrip("/")
    bare_prefix = route_prefix.strip("/") + "/"
    if bare_pattern.startswith(bare_prefix):
        return bare_pattern[len(bare_prefix) :]
    return pattern


def _remap_file_info_path(fi: FileInfo, route_prefix: str) -> FileInfo:
    """Create a new FileInfo with the route prefix prepended to the path."""
    return cast(
        "FileInfo",
        {
            **fi,
            "path": f"{_prefix_for(route_prefix)}{fi['path']}",
        },
    )


def _route_for_path(
    *,
    default: BackendProtocol,
    sorted_routes: list[tuple[str, BackendProtocol]],
    path: str,
) -> tuple[BackendProtocol, str, str | None]:
    """Route a path to a backend and normalize it for that backend.

    Returns the selected backend, the normalized path to pass to that backend,
    and the matched route prefix (or None if the default backend is used).

    Normalization rules:
    - If path is exactly the route root without trailing slash (e.g., "/memories"),
      route to that backend and return backend_path "/".
    - If path starts with the route prefix (e.g., "/memories/notes.txt"), strip the
      route prefix and ensure the result starts with "/".
    - Otherwise return the default backend and the original path.
    """
    for route_prefix, backend in sorted_routes:
        prefix_no_slash = route_prefix.rstrip("/")
        if path == prefix_no_slash:
            return backend, "/", route_prefix

        # Ensure route_prefix ends with / for startswith check to enforce boundary
        normalized_prefix = route_prefix if route_prefix.endswith("/") else f"{route_prefix}/"
        if path.startswith(normalized_prefix):
            suffix = path[len(normalized_prefix) :]
            backend_path = f"/{suffix}" if suffix else "/"
            return backend, backend_path, route_prefix
    return default, path, None


class CompositeBackend(BackendProtocol):
    """Routes file operations to different backends by path prefix.

    Matches paths against route prefixes (longest first) and delegates to the
    corresponding backend. Unmatched paths use the default backend.

    Search operations (`grep`, `glob`) only fan out across every backend when the
    caller asked for the whole tree (`path` of `"/"` or `None`). A `path` that
    resolves to a single route searches that route; a `path` that resolves to the
    default backend searches only the default backend. A scoped search must never
    surface entries from a route the caller did not ask for.

    Attributes:
        default: Backend for paths that don't match any route.
        routes: Map of path prefixes to backends (e.g., {"/memories/": store_backend}).
        sorted_routes: Routes sorted by length (longest first) for correct matching.

    Examples:
        ```python
        composite = CompositeBackend(default=StateBackend(runtime), routes={"/memories/": StoreBackend(runtime), "/cache/": StoreBackend(runtime)})

        composite.write("/temp.txt", "data")
        composite.write("/memories/note.txt", "data")
        ```
    """

    def __init__(
        self,
        default: BackendProtocol | StateBackend,
        routes: dict[str, BackendProtocol],
        *,
        artifacts_root: str = "/large_tool_results",
    ) -> None:
        """Initialize composite backend.

        Args:
            default: Backend for paths that don't match any route.
            routes: Map of path prefixes to backends. Prefixes must start with "/"
                and should end with "/" (e.g., "/memories/").
            artifacts_root: Root path for middleware-generated artifacts such as
                offloaded tool results.

                Defaults to `/large_tool_results` to match the filesystem
                middleware's standard offload location.
        """
        # Default backend
        self.default = default

        # Virtual routes
        self.routes = routes
        self.artifacts_root = artifacts_root

        # Sort routes by length (longest first) for correct prefix matching
        self.sorted_routes = sorted(routes.items(), key=lambda x: len(x[0]), reverse=True)

    def _get_backend_and_key(self, key: str) -> tuple[BackendProtocol, str]:
        backend, stripped_key, _route_prefix = _route_for_path(
            default=self.default,
            sorted_routes=self.sorted_routes,
            path=key,
        )
        return backend, stripped_key

    def _sync_state(self, file_path: str, files_update: dict[str, Any] | None) -> None:
        """Best-effort mirror of a backend's `files_update` into the default backend's state.

        Keeps `ls`/`grep` listings coherent within a single tool call, before
        LangGraph applies the returned update. A `None` value is the state
        reducer's deletion marker, so it pops the key rather than storing `None`.

        Args:
            file_path: The virtual path the operation targeted, for logging.
            files_update: The backend's state update, or `None` for external storage.
        """
        if not files_update:
            return
        try:
            runtime = getattr(self.default, "runtime", None)
            if runtime is None:
                return
            state = runtime.state
            files = state.get("files", {})
            for key, value in files_update.items():
                if value is None:
                    files.pop(key, None)
                else:
                    files[key] = value
            state["files"] = files
        except Exception as exc:  # noqa: BLE001  # Intentional for best-effort state sync
            logger.debug("composite state-sync skipped for %s: %s", file_path, exc)

    # -- coercion helpers ----------------------------------------------------
    #
    # Third-party backends predating the structured API may still return the
    # legacy shapes from `ls`/`grep`/`glob`. Normalize at the boundary so the
    # merge logic below only ever handles one shape.

    @staticmethod
    def _coerce_ls_result(raw: LsResult | list[FileInfo]) -> LsResult:
        """Normalize a legacy `list[FileInfo]` return to `LsResult`."""
        if isinstance(raw, LsResult):
            return raw
        return LsResult(entries=raw)

    @staticmethod
    def _coerce_grep_result(raw: GrepResult | list[GrepMatch] | str) -> GrepResult:
        """Normalize a legacy `list[GrepMatch] | str` return to `GrepResult`."""
        if isinstance(raw, GrepResult):
            return raw
        if isinstance(raw, str):
            return GrepResult(error=raw)
        return GrepResult(matches=raw)

    @staticmethod
    def _coerce_glob_result(raw: GlobResult | list[FileInfo]) -> GlobResult:
        """Normalize a legacy `list[FileInfo]` return to `GlobResult`."""
        if isinstance(raw, GlobResult):
            return raw
        return GlobResult(matches=raw)

    # -- structured API ------------------------------------------------------

    def ls(self, path: str) -> LsResult:
        """List directory contents (non-recursive).

        If path matches a route, lists only that backend. If path is "/", aggregates
        default backend plus virtual route directories. Otherwise lists default backend.

        Args:
            path: Absolute directory path starting with "/".

        Returns:
            `LsResult` whose entries have route prefixes restored. Directories have
            a trailing "/" and `is_dir=True`.

        Examples:
            ```python
            result = composite.ls("/")
            result = composite.ls("/memories/")
            ```
        """
        backend, backend_path, route_prefix = _route_for_path(
            default=self.default,
            sorted_routes=self.sorted_routes,
            path=path,
        )
        if route_prefix is not None:
            routed = self._coerce_ls_result(backend.ls(backend_path))
            if routed.error is not None:
                return routed
            return LsResult(entries=[_remap_file_info_path(fi, route_prefix) for fi in (routed.entries or [])])

        # At root, aggregate default and all routed backends
        if path == "/":
            default_result = self._coerce_ls_result(self.default.ls(path))
            if default_result.error is not None:
                return default_result
            results: list[FileInfo] = list(default_result.entries or [])
            for prefix, _backend in self.sorted_routes:
                # Add the route itself as a directory (e.g., /memories/)
                results.append(
                    FileInfo(
                        path=prefix,
                        is_dir=True,
                        size=0,
                        modified_at="",
                    )
                )

            results.sort(key=lambda x: x.get("path", ""))
            return LsResult(entries=results)

        # Path doesn't match a route: query only default backend
        return self._coerce_ls_result(self.default.ls(path))

    async def als(self, path: str) -> LsResult:
        """Async version of `ls`."""
        backend, backend_path, route_prefix = _route_for_path(
            default=self.default,
            sorted_routes=self.sorted_routes,
            path=path,
        )
        if route_prefix is not None:
            routed = self._coerce_ls_result(await backend.als(backend_path))
            if routed.error is not None:
                return routed
            return LsResult(entries=[_remap_file_info_path(fi, route_prefix) for fi in (routed.entries or [])])

        # At root, aggregate default and all routed backends
        if path == "/":
            default_result = self._coerce_ls_result(await self.default.als(path))
            if default_result.error is not None:
                return default_result
            results: list[FileInfo] = list(default_result.entries or [])
            for prefix, _backend in self.sorted_routes:
                # Add the route itself as a directory (e.g., /memories/)
                results.append(
                    FileInfo(
                        path=prefix,
                        is_dir=True,
                        size=0,
                        modified_at="",
                    )
                )

            results.sort(key=lambda x: x.get("path", ""))
            return LsResult(entries=results)

        # Path doesn't match a route: query only default backend
        return self._coerce_ls_result(await self.default.als(path))

    def read_file(
        self,
        file_path: str,
        offset: int = 0,
        limit: int = 2000,
    ) -> ReadResult:
        """Read raw file data, routing to the appropriate backend.

        Args:
            file_path: Absolute file path.
            offset: Line offset to start reading from (0-indexed).
            limit: Maximum number of lines to read.

        Returns:
            `ReadResult` carrying the sliced `FileData`, or an error message.
        """
        backend, stripped_key = self._get_backend_and_key(file_path)
        return backend.read_file(stripped_key, offset, limit)

    async def aread_file(
        self,
        file_path: str,
        offset: int = 0,
        limit: int = 2000,
    ) -> ReadResult:
        """Async version of `read_file`."""
        backend, stripped_key = self._get_backend_and_key(file_path)
        return await backend.aread_file(stripped_key, offset, limit)

    def read(
        self,
        file_path: str,
        offset: int = 0,
        limit: int = 2000,
    ) -> str:
        """Read file content with line numbers, routing to appropriate backend.

        Args:
            file_path: Absolute file path.
            offset: Line offset to start reading from (0-indexed).
            limit: Maximum number of lines to read.

        Returns:
            Formatted file content with line numbers, or error message.
        """
        backend, stripped_key = self._get_backend_and_key(file_path)
        return backend.read(stripped_key, offset=offset, limit=limit)

    async def aread(
        self,
        file_path: str,
        offset: int = 0,
        limit: int = 2000,
    ) -> str:
        """Async version of read."""
        backend, stripped_key = self._get_backend_and_key(file_path)
        return await backend.aread(stripped_key, offset=offset, limit=limit)

    def grep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
    ) -> GrepResult:
        """Search files for literal text pattern.

        Routes to backends based on path: a routed path searches that one backend,
        "/" or None searches every backend, and any other path searches only the
        default backend.

        The first backend to report an error short-circuits the merge — a partial
        result is never passed off as a complete one. `truncated` is OR-ed across
        every backend that contributed.

        Args:
            pattern: Literal text to search for (NOT regex).
            path: Directory to search. None searches all backends.
            glob: Glob pattern to filter files (e.g., "*.py", "**/*.txt").
                Filters by filename, not content.

        Returns:
            `GrepResult` whose match paths have route prefixes restored.

        Examples:
            ```python
            result = composite.grep("TODO", path="/memories/")
            result = composite.grep("error", path="/")
            result = composite.grep("import", path="/", glob="*.py")
            ```
        """
        if path is not None:
            backend, backend_path, route_prefix = _route_for_path(
                default=self.default,
                sorted_routes=self.sorted_routes,
                path=path,
            )
            if route_prefix is not None:
                routed = self._coerce_grep_result(backend.grep(pattern, backend_path, glob))
                if routed.error is not None:
                    return routed
                return GrepResult(
                    matches=[_remap_grep_path(m, route_prefix) for m in (routed.matches or [])],
                    truncated=routed.truncated,
                )

        # If path is None or "/", search default and all routed backends and merge
        if path is None or path == "/":
            default_result = self._coerce_grep_result(self.default.grep(pattern, path, glob))
            if default_result.error is not None:
                return default_result
            all_matches: list[GrepMatch] = list(default_result.matches or [])
            truncated = default_result.truncated

            for route_prefix, backend in self.routes.items():
                routed = self._coerce_grep_result(backend.grep(pattern, "/", glob))
                if routed.error is not None:
                    return routed
                all_matches.extend(_remap_grep_path(m, route_prefix) for m in (routed.matches or []))
                truncated = truncated or routed.truncated

            return GrepResult(matches=all_matches, truncated=truncated)

        # Path specified but doesn't match a route - search only default
        return self._coerce_grep_result(self.default.grep(pattern, path, glob))

    async def agrep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
    ) -> GrepResult:
        """Async version of grep.

        See `grep()` for detailed documentation on routing behavior and parameters.
        """
        if path is not None:
            backend, backend_path, route_prefix = _route_for_path(
                default=self.default,
                sorted_routes=self.sorted_routes,
                path=path,
            )
            if route_prefix is not None:
                routed = self._coerce_grep_result(await backend.agrep(pattern, backend_path, glob))
                if routed.error is not None:
                    return routed
                return GrepResult(
                    matches=[_remap_grep_path(m, route_prefix) for m in (routed.matches or [])],
                    truncated=routed.truncated,
                )

        # If path is None or "/", search default and all routed backends and merge
        if path is None or path == "/":
            default_result = self._coerce_grep_result(await self.default.agrep(pattern, path, glob))
            if default_result.error is not None:
                return default_result
            all_matches: list[GrepMatch] = list(default_result.matches or [])
            truncated = default_result.truncated

            for route_prefix, backend in self.routes.items():
                routed = self._coerce_grep_result(await backend.agrep(pattern, "/", glob))
                if routed.error is not None:
                    return routed
                all_matches.extend(_remap_grep_path(m, route_prefix) for m in (routed.matches or []))
                truncated = truncated or routed.truncated

            return GrepResult(matches=all_matches, truncated=truncated)

        # Path specified but doesn't match a route - search only default
        return self._coerce_grep_result(await self.default.agrep(pattern, path, glob))

    def glob(self, pattern: str, path: str | None = None) -> GlobResult:
        """Find files matching a glob pattern, routing by path prefix.

        Routes to backends based on path: a routed path searches that one backend,
        "/" or None searches every backend, and any other path searches only the
        default backend — a glob scoped to `/src` must not surface entries from a
        `/memories/` route.

        The first backend to report an error short-circuits the merge. `truncated`
        is OR-ed across every backend that contributed.

        Args:
            pattern: Glob pattern with wildcards (e.g., "**/*.md").
            path: Base directory to search from. None searches all backends.

        Returns:
            `GlobResult` whose match paths have route prefixes restored.
        """
        if path is not None:
            backend, backend_path, route_prefix = _route_for_path(
                default=self.default,
                sorted_routes=self.sorted_routes,
                path=path,
            )
            if route_prefix is not None:
                routed = self._coerce_glob_result(backend.glob(pattern, backend_path))
                if routed.error is not None:
                    return routed
                return GlobResult(
                    matches=[_remap_file_info_path(fi, route_prefix) for fi in (routed.matches or [])],
                    truncated=routed.truncated,
                )

        # Whole-tree search: fan out to the default backend AND every route.
        if path is None or path == "/":
            default_result = self._coerce_glob_result(self.default.glob(pattern, path))
            if default_result.error is not None:
                return default_result
            results: list[FileInfo] = list(default_result.matches or [])
            truncated = default_result.truncated

            for route_prefix, backend in self.routes.items():
                routed = self._coerce_glob_result(backend.glob(_strip_route_from_pattern(pattern, route_prefix), "/"))
                if routed.error is not None:
                    return routed
                results.extend(_remap_file_info_path(fi, route_prefix) for fi in (routed.matches or []))
                truncated = truncated or routed.truncated

            results.sort(key=lambda x: x.get("path", ""))
            return GlobResult(matches=results, truncated=truncated)

        # Scoped to a path the default backend owns: routed backends are out of scope.
        return self._coerce_glob_result(self.default.glob(pattern, path))

    async def aglob(self, pattern: str, path: str | None = None) -> GlobResult:
        """Async version of glob.

        See `glob()` for detailed documentation on routing behavior and parameters.
        """
        if path is not None:
            backend, backend_path, route_prefix = _route_for_path(
                default=self.default,
                sorted_routes=self.sorted_routes,
                path=path,
            )
            if route_prefix is not None:
                routed = self._coerce_glob_result(await backend.aglob(pattern, backend_path))
                if routed.error is not None:
                    return routed
                return GlobResult(
                    matches=[_remap_file_info_path(fi, route_prefix) for fi in (routed.matches or [])],
                    truncated=routed.truncated,
                )

        # Whole-tree search: fan out to the default backend AND every route.
        if path is None or path == "/":
            default_result = self._coerce_glob_result(await self.default.aglob(pattern, path))
            if default_result.error is not None:
                return default_result
            results: list[FileInfo] = list(default_result.matches or [])
            truncated = default_result.truncated

            for route_prefix, backend in self.routes.items():
                routed = self._coerce_glob_result(await backend.aglob(_strip_route_from_pattern(pattern, route_prefix), "/"))
                if routed.error is not None:
                    return routed
                results.extend(_remap_file_info_path(fi, route_prefix) for fi in (routed.matches or []))
                truncated = truncated or routed.truncated

            results.sort(key=lambda x: x.get("path", ""))
            return GlobResult(matches=results, truncated=truncated)

        # Scoped to a path the default backend owns: routed backends are out of scope.
        return self._coerce_glob_result(await self.default.aglob(pattern, path))

    # -- write / edit / delete -----------------------------------------------

    def write(
        self,
        file_path: str,
        content: str,
    ) -> WriteResult:
        """Write a file, routing to appropriate backend.

        Args:
            file_path: Absolute file path.
            content: File content as a string.

        Returns:
            `WriteResult` with the full virtual path restored.
        """
        backend, stripped_key = self._get_backend_and_key(file_path)
        res = backend.write(stripped_key, content)
        if res.path is not None:
            res = replace(res, path=file_path)
        self._sync_state(file_path, res.files_update)
        return res

    async def awrite(
        self,
        file_path: str,
        content: str,
    ) -> WriteResult:
        """Async version of write."""
        backend, stripped_key = self._get_backend_and_key(file_path)
        res = await backend.awrite(stripped_key, content)
        if res.path is not None:
            res = replace(res, path=file_path)
        self._sync_state(file_path, res.files_update)
        return res

    def edit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> EditResult:
        """Edit a file, routing to appropriate backend.

        Args:
            file_path: Absolute file path.
            old_string: String to find and replace.
            new_string: Replacement string.
            replace_all: If True, replace all occurrences.

        Returns:
            `EditResult` with the full virtual path restored.
        """
        backend, stripped_key = self._get_backend_and_key(file_path)
        res = backend.edit(stripped_key, old_string, new_string, replace_all=replace_all)
        if res.path is not None:
            res = replace(res, path=file_path)
        self._sync_state(file_path, res.files_update)
        return res

    async def aedit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> EditResult:
        """Async version of edit."""
        backend, stripped_key = self._get_backend_and_key(file_path)
        res = await backend.aedit(stripped_key, old_string, new_string, replace_all=replace_all)
        if res.path is not None:
            res = replace(res, path=file_path)
        self._sync_state(file_path, res.files_update)
        return res

    def _remap_delete_result(self, res: DeleteResult, file_path: str, route_prefix: str | None) -> DeleteResult:
        """Translate a routed backend's local paths back into virtual paths.

        The routed backend saw a stripped key (`/note.md`), so every path it
        reports — `path`, `deleted_paths`, and the `files_update` keys the state
        reducer will consume — has to be lifted back under the route prefix
        before it reaches the caller.

        Args:
            res: The routed backend's result, with backend-local paths.
            file_path: The original virtual path the caller asked to delete.
            route_prefix: The matched route prefix, or `None` for the default backend.

        Returns:
            A `DeleteResult` whose paths are all virtual.
        """
        if res.error is not None:
            return res

        if route_prefix is None:
            return replace(res, path=file_path if res.path is not None else None)

        prefix = _prefix_for(route_prefix)
        files_update: dict[str, Any] | None = None
        if res.files_update is not None:
            files_update = {f"{prefix}{key}": value for key, value in res.files_update.items()}
        return replace(
            res,
            path=file_path if res.path is not None else None,
            files_update=files_update,
            deleted_paths=[f"{prefix}{p}" for p in res.deleted_paths],
        )

    def delete(self, file_path: str) -> DeleteResult:
        """Delete a path, routing to the appropriate backend.

        `CompositeBackend` always advertises delete support (it overrides this
        method), so the delete tool is never filtered out for it. A route may
        still point at a backend that does not implement `delete`; rather than
        letting `NotImplementedError` escape to the caller, that case is reported
        as a `DeleteResult` error.

        Args:
            file_path: Absolute path to delete (recursively, for a directory).

        Returns:
            `DeleteResult` with virtual paths restored, or an error — including
                when the routed backend does not support deletion.
        """
        backend, stripped_key, route_prefix = _route_for_path(
            default=self.default,
            sorted_routes=self.sorted_routes,
            path=file_path,
        )
        if not supports_delete(backend):
            return DeleteResult(error=_DELETE_UNSUPPORTED_ERROR.format(file_path=file_path))
        try:
            res = backend.delete(stripped_key)
        except NotImplementedError:
            return DeleteResult(error=_DELETE_UNSUPPORTED_ERROR.format(file_path=file_path))
        res = self._remap_delete_result(res, file_path, route_prefix)
        self._sync_state(file_path, res.files_update)
        return res

    async def adelete(self, file_path: str) -> DeleteResult:
        """Async version of delete."""
        backend, stripped_key, route_prefix = _route_for_path(
            default=self.default,
            sorted_routes=self.sorted_routes,
            path=file_path,
        )
        if not supports_delete(backend):
            return DeleteResult(error=_DELETE_UNSUPPORTED_ERROR.format(file_path=file_path))
        try:
            res = await backend.adelete(stripped_key)
        except NotImplementedError:
            return DeleteResult(error=_DELETE_UNSUPPORTED_ERROR.format(file_path=file_path))
        res = self._remap_delete_result(res, file_path, route_prefix)
        self._sync_state(file_path, res.files_update)
        return res

    # -- execution -----------------------------------------------------------

    def execute(
        self,
        command: str,
        *,
        timeout: int | None = None,
    ) -> ExecuteResponse:
        """Execute a shell command via the default backend.

        Unlike file operations, execution is not path-routable — it always
        delegates to the default backend.

        Args:
            command: Shell command to execute.
            timeout: Maximum time in seconds to wait for the command to complete.

                If None, uses the backend's default timeout.

        Returns:
            ExecuteResponse with output, exit code, and truncation flag.

        Raises:
            NotImplementedError: If the default backend is not a
                `SandboxBackendProtocol` (i.e., it doesn't support execution).
        """
        if isinstance(self.default, SandboxBackendProtocol):
            if timeout is not None and execute_accepts_timeout(type(self.default)):
                return self.default.execute(command, timeout=timeout)
            return self.default.execute(command)

        # This shouldn't be reached if the runtime check in the execute tool works correctly,
        # but we include it as a safety fallback.
        msg = (
            "Default backend doesn't support command execution (SandboxBackendProtocol). "
            "To enable execution, provide a default backend that implements SandboxBackendProtocol."
        )
        raise NotImplementedError(msg)

    async def aexecute(
        self,
        command: str,
        *,
        # ASYNC109 - timeout is a semantic parameter forwarded to the underlying
        # backend's implementation, not an asyncio.timeout() contract.
        timeout: int | None = None,  # noqa: ASYNC109
    ) -> ExecuteResponse:
        """Async version of execute.

        See `execute()` for detailed documentation on parameters and behavior.
        """
        if isinstance(self.default, SandboxBackendProtocol):
            if timeout is not None and execute_accepts_timeout(type(self.default)):
                return await self.default.aexecute(command, timeout=timeout)
            return await self.default.aexecute(command)

        # This shouldn't be reached if the runtime check in the execute tool works correctly,
        # but we include it as a safety fallback.
        msg = (
            "Default backend doesn't support command execution (SandboxBackendProtocol). "
            "To enable execution, provide a default backend that implements SandboxBackendProtocol."
        )
        raise NotImplementedError(msg)

    # -- bulk transfer -------------------------------------------------------

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        """Upload multiple files, batching by backend for efficiency.

        Groups files by their target backend, calls each backend's upload_files
        once with all files for that backend, then merges results in original order.

        Args:
            files: List of (path, content) tuples to upload.

        Returns:
            List of FileUploadResponse objects, one per input file.
            Response order matches input order.
        """
        # Pre-allocate result list
        results: list[FileUploadResponse | None] = [None] * len(files)

        # Group files by backend, tracking original indices
        backend_batches: dict[BackendProtocol, list[tuple[int, str, bytes]]] = defaultdict(list)

        for idx, (path, content) in enumerate(files):
            backend, stripped_path = self._get_backend_and_key(path)
            backend_batches[backend].append((idx, stripped_path, content))

        # Process each backend's batch
        for backend, batch in backend_batches.items():
            # Extract data for backend call
            indices, stripped_paths, contents = zip(*batch, strict=False)
            batch_files = list(zip(stripped_paths, contents, strict=False))

            # Call backend once with all its files
            batch_responses = backend.upload_files(batch_files)

            # Place responses at original indices with original paths
            for i, orig_idx in enumerate(indices):
                results[orig_idx] = FileUploadResponse(
                    path=files[orig_idx][0],  # Original path
                    error=batch_responses[i].error if i < len(batch_responses) else None,
                )

        return cast("list[FileUploadResponse]", results)

    async def aupload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        """Async version of upload_files."""
        # Pre-allocate result list
        results: list[FileUploadResponse | None] = [None] * len(files)

        # Group files by backend, tracking original indices
        backend_batches: dict[BackendProtocol, list[tuple[int, str, bytes]]] = defaultdict(list)

        for idx, (path, content) in enumerate(files):
            backend, stripped_path = self._get_backend_and_key(path)
            backend_batches[backend].append((idx, stripped_path, content))

        # Process each backend's batch
        for backend, batch in backend_batches.items():
            # Extract data for backend call
            indices, stripped_paths, contents = zip(*batch, strict=False)
            batch_files = list(zip(stripped_paths, contents, strict=False))

            # Call backend once with all its files
            batch_responses = await backend.aupload_files(batch_files)

            # Place responses at original indices with original paths
            for i, orig_idx in enumerate(indices):
                results[orig_idx] = FileUploadResponse(
                    path=files[orig_idx][0],  # Original path
                    error=batch_responses[i].error if i < len(batch_responses) else None,
                )

        return cast("list[FileUploadResponse]", results)

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        """Download multiple files, batching by backend for efficiency.

        Groups paths by their target backend, calls each backend's download_files
        once with all paths for that backend, then merges results in original order.

        Args:
            paths: List of file paths to download.

        Returns:
            List of FileDownloadResponse objects, one per input path.
            Response order matches input order.
        """
        # Pre-allocate result list
        results: list[FileDownloadResponse | None] = [None] * len(paths)

        backend_batches: dict[BackendProtocol, list[tuple[int, str]]] = defaultdict(list)

        for idx, path in enumerate(paths):
            backend, stripped_path = self._get_backend_and_key(path)
            backend_batches[backend].append((idx, stripped_path))

        # Process each backend's batch
        for backend, batch in backend_batches.items():
            # Extract data for backend call
            indices, stripped_paths = zip(*batch, strict=False)

            # Call backend once with all its paths
            batch_responses = backend.download_files(list(stripped_paths))

            # Place responses at original indices with original paths
            for i, orig_idx in enumerate(indices):
                results[orig_idx] = FileDownloadResponse(
                    path=paths[orig_idx],  # Original path
                    content=batch_responses[i].content if i < len(batch_responses) else None,
                    error=batch_responses[i].error if i < len(batch_responses) else None,
                )

        return cast("list[FileDownloadResponse]", results)

    async def adownload_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        """Async version of download_files."""
        # Pre-allocate result list
        results: list[FileDownloadResponse | None] = [None] * len(paths)

        backend_batches: dict[BackendProtocol, list[tuple[int, str]]] = defaultdict(list)

        for idx, path in enumerate(paths):
            backend, stripped_path = self._get_backend_and_key(path)
            backend_batches[backend].append((idx, stripped_path))

        # Process each backend's batch
        for backend, batch in backend_batches.items():
            # Extract data for backend call
            indices, stripped_paths = zip(*batch, strict=False)

            # Call backend once with all its paths
            batch_responses = await backend.adownload_files(list(stripped_paths))

            # Place responses at original indices with original paths
            for i, orig_idx in enumerate(indices):
                results[orig_idx] = FileDownloadResponse(
                    path=paths[orig_idx],  # Original path
                    content=batch_responses[i].content if i < len(batch_responses) else None,
                    error=batch_responses[i].error if i < len(batch_responses) else None,
                )

        return cast("list[FileDownloadResponse]", results)
