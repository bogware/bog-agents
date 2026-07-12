"""StoreBackend: Adapter for LangGraph's BaseStore (persistent, cross-thread)."""

import base64
import re
import warnings
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Generic

from langgraph.config import get_config, get_store
from langgraph.store.base import BaseStore, Item, PutOp
from langgraph.typing import ContextT, StateT

from bog_agents._api.deprecation import warn_deprecated
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
    from langgraph.runtime import Runtime


@dataclass
class BackendContext(Generic[StateT, ContextT]):
    """Context passed to (first-generation) namespace factory functions.

    Factories written against this shape reach the runtime through
    `ctx.runtime` and state through `ctx.state`. Second-generation factories
    receive the runtime directly. Both keep working — see
    `_NamespaceRuntimeCompat`.
    """

    state: StateT
    runtime: "Runtime[ContextT]"


class _NamespaceRuntimeCompat:
    """Argument handed to a `NamespaceFactory`; duck-types as runtime *and* context.

    First-generation factories were written against `BackendContext`
    (`ctx.runtime.context.user_id`, `ctx.state[...]`). Upstream's factories take
    a `Runtime` and read its attributes directly (`rt.context.user_id`). This
    wrapper answers both: `.runtime` / `.state` are served locally and every
    other attribute is proxied to the wrapped runtime.
    """

    def __init__(self, runtime: object | None, state: object = None) -> None:
        """Initialize the compatibility wrapper.

        Args:
            runtime: The runtime to proxy to (a `ToolRuntime` or a `Runtime`).
            state: State to expose as `.state` for first-generation factories.
        """
        self._runtime = runtime
        self._state = state

    @property
    def runtime(self) -> object | None:
        """The wrapped runtime (first-generation `BackendContext` accessor)."""
        return self._runtime

    @property
    def state(self) -> object:
        """The wrapped state (first-generation `BackendContext` accessor)."""
        return self._state

    def __getattr__(self, name: str) -> object:
        """Proxy any other attribute to the wrapped runtime.

        Args:
            name: Attribute name requested by a second-generation factory.

        Returns:
            The attribute value from the wrapped runtime.

        Raises:
            AttributeError: If no runtime is available (running outside a graph).
        """
        runtime = self.__dict__.get("_runtime")
        if runtime is None:
            msg = f"Runtime is not available (running outside graph execution), cannot access '.{name}'"
            raise AttributeError(msg)
        return getattr(runtime, name)


# Type alias for namespace factory functions. Accepts both generations of
# factory: see `_NamespaceRuntimeCompat` for how one argument serves both.
NamespaceFactory = Callable[[Any], tuple[str, ...]]

# Allowed characters in namespace components: alphanumeric, plus characters
# common in user IDs (hyphen, underscore, dot, @, +, colon, tilde).
_NAMESPACE_COMPONENT_RE = re.compile(r"^[A-Za-z0-9\-_.@+:~]+$")


def _validate_namespace(namespace: tuple[str, ...]) -> tuple[str, ...]:
    """Validate a namespace tuple returned by a NamespaceFactory.

    Each component must be a non-empty string containing only safe characters:
    alphanumeric (a-z, A-Z, 0-9), hyphen (-), underscore (_), dot (.),
    at sign (@), plus (+), colon (:), and tilde (~).

    Characters like `*`, `?`, `[`, `]`, `{`, `}`, etc. are
    rejected to prevent wildcard or glob injection in store lookups.

    Args:
        namespace: The namespace tuple to validate.

    Returns:
        The validated namespace tuple (unchanged).

    Raises:
        ValueError: If the namespace is empty, contains empty strings, or
            strings with disallowed characters.
        TypeError: If the namespace contains non-string elements.
    """
    if not namespace:
        msg = "Namespace tuple must not be empty."
        raise ValueError(msg)

    for i, component in enumerate(namespace):
        if not isinstance(component, str):
            msg = f"Namespace component at index {i} must be a string, got {type(component).__name__}."
            raise TypeError(msg)
        if not component:
            msg = f"Namespace component at index {i} must not be empty."
            raise ValueError(msg)
        if not _NAMESPACE_COMPONENT_RE.match(component):
            msg = (
                f"Namespace component at index {i} contains disallowed characters: {component!r}. "
                f"Only alphanumeric characters, hyphens, underscores, dots, @, +, colons, and tildes are allowed."
            )
            raise ValueError(msg)

    return namespace


def _content_size(file_data: dict[str, Any]) -> int:
    r"""Return the size of a file's content, tolerating v1 and v2 shapes.

    v2 stores `content` as a `str`; v1 (legacy) stores it as `list[str]`.
    `"\n".join()` on a v2 `str` would interleave newlines between characters, so
    the shape must be checked before measuring.

    Args:
        file_data: A `FileData`-shaped dict (v1 or v2).

    Returns:
        Length of the content in characters.
    """
    raw = file_data.get("content", "")
    if isinstance(raw, list):
        return len("\n".join(raw))
    return len(raw)


def _sliced_file_data(file_data: FileData, sliced: str) -> FileData:
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


class StoreBackend(BackendProtocol):
    """Backend that stores files in LangGraph's BaseStore (persistent).

    Uses LangGraph's Store for persistent, cross-conversation storage.
    Files are organized via namespaces and persist across all threads.

    The namespace can include an optional assistant_id for multi-agent isolation.
    """

    def __init__(
        self,
        runtime: "ToolRuntime | None" = None,
        *,
        store: BaseStore | None = None,
        namespace: NamespaceFactory | None = None,
        file_format: FileFormat = "v2",
    ) -> None:
        r"""Initialize StoreBackend.

        Args:
            runtime: Optional tool runtime providing store access and config.
                When omitted, the store is taken from `store=` or resolved at
                call time via `get_store()`.
            store: Optional `BaseStore` used directly, bypassing the runtime and
                the ambient graph context. Lets `StoreBackend(store=...)` be used
                standalone (scripts, tests, offline migrations).
            namespace: Optional callable returning a namespace tuple for scoping
                store operations. Wildcards (`*`) are forbidden. If `None`, uses
                legacy assistant_id detection from metadata (deprecated).

                The callable receives an argument that duck-types as both the
                first-generation `BackendContext` (`ctx.runtime`, `ctx.state`)
                and a `Runtime` (`rt.context`, `rt.store`), so both generations
                of factory work.
            file_format: Storage format for newly written files. `"v2"` (default)
                stores `content` as a plain `str` with an `encoding` field;
                `"v1"` stores `content` as `list[str]` (lines split on `\n`) with
                no `encoding` field.

        Example:
            `namespace=lambda rt: ("filesystem", rt.context.user_id)`
        """
        self.runtime = runtime
        self._store = store
        self._namespace = namespace
        self._file_format: FileFormat = file_format

    def _get_store(self) -> BaseStore:
        """Get the store instance.

        Resolution order: the explicit `store=` argument, then the runtime's
        store, then the ambient graph context via `get_store()`.

        Returns:
            The `BaseStore` to operate on.

        Raises:
            ValueError: If no store can be resolved.
        """
        if self._store is not None:
            return self._store

        runtime_store = getattr(self.runtime, "store", None) if self.runtime is not None else None
        if runtime_store is not None:
            return runtime_store

        try:
            store = get_store()
        except (RuntimeError, KeyError):
            store = None
        if store is None:
            msg = (
                "Store is required but not available. Use StoreBackend inside a LangGraph graph execution "
                "(e.g. via create_agent), or pass one explicitly: StoreBackend(store=my_store)."
            )
            raise ValueError(msg)
        return store

    def _get_namespace(self) -> tuple[str, ...]:
        """Get the namespace for store operations.

        If a namespace factory was provided at init, calls it with a
        `_NamespaceRuntimeCompat` wrapper. Otherwise falls back to the legacy
        assistant_id detection (deprecated).

        Returns:
            The validated namespace tuple.
        """
        if self._namespace is not None:
            runtime: object | None = self.runtime
            if runtime is None:
                # Import locally: `get_runtime` is only meaningful inside a graph
                # execution, and importing it eagerly is unnecessary otherwise.
                from langgraph.runtime import get_runtime

                try:
                    runtime = get_runtime()
                except (RuntimeError, KeyError, LookupError):
                    runtime = None
            state = getattr(runtime, "state", None)
            compat = _NamespaceRuntimeCompat(runtime, state)
            return _validate_namespace(self._namespace(compat))

        return self._get_namespace_legacy()

    def _get_namespace_legacy(self) -> tuple[str, ...]:
        """Legacy namespace resolution: check metadata for assistant_id.

        Preference order:

        1. Use `self.runtime.config` if present (tests pass this explicitly).
        2. Fall back to `langgraph.config.get_config()` if available.
        3. Default to `("filesystem",)`.

        If an assistant_id is available in the config metadata, return
        `(assistant_id, "filesystem")` to provide per-assistant isolation.

        Returns:
            The namespace tuple.

        !!! warning "Deprecated"

            Pass `namespace` to `StoreBackend` instead of relying on legacy detection.
        """
        warnings.warn(
            "StoreBackend without explicit `namespace` is deprecated. Pass `namespace=lambda ctx: (...)` to StoreBackend.",
            DeprecationWarning,
            stacklevel=3,
        )
        namespace = "filesystem"

        runtime_cfg = getattr(self.runtime, "config", None) if self.runtime is not None else None
        if isinstance(runtime_cfg, dict):
            assistant_id = runtime_cfg.get("metadata", {}).get("assistant_id")
            if assistant_id:
                return _validate_namespace((assistant_id, namespace))
            return (namespace,)

        try:
            cfg = get_config()
        except Exception:  # noqa: BLE001  # Intentional for resilient config fallback
            return (namespace,)

        try:
            assistant_id = cfg.get("metadata", {}).get("assistant_id")
        except Exception:  # noqa: BLE001  # Intentional for resilient config fallback
            assistant_id = None

        if assistant_id:
            return _validate_namespace((assistant_id, namespace))
        return (namespace,)

    def _convert_store_item_to_file_data(self, store_item: Item) -> FileData:
        r"""Convert a store `Item` to `FileData`.

        Accepts both storage formats: v2 (`content: str` + `encoding`) and legacy
        v1 (`content: list[str]`, lines split on `\n`, no `encoding`). Timestamps
        are optional — a hand-written store item without them still converts.

        Args:
            store_item: The store `Item` containing file data.

        Returns:
            `FileData` dict with content, encoding, and any timestamps present.

        Raises:
            ValueError: If `content` is missing, or is neither `str` nor `list[str]`.
        """
        raw_content = store_item.value.get("content")
        if raw_content is None:
            msg = f"Store item does not contain valid content field. Got: {store_item.value.keys()}"
            raise ValueError(msg)

        if isinstance(raw_content, list):
            warn_deprecated(
                since="0.10.0",
                removal="1.0.0",
                message=(
                    "Store items with `list[str]` content (the v1 format) are deprecated and will be removed in bog-agents==1.0.0. "
                    "Store `content` as a plain `str` with an `encoding` field instead."
                ),
                package="bog-agents",
            )
            content = "\n".join(raw_content)
        elif isinstance(raw_content, str):
            content = raw_content
        else:
            # ValueError, not TypeError: every caller (ls/grep/glob/download)
            # catches ValueError to skip a corrupt item without aborting the
            # batch. A TypeError here would escape those guards.
            msg = f"Store item content must be a str or list[str], got {type(raw_content).__name__}."
            raise ValueError(msg)  # noqa: TRY004

        result = FileData(content=content, encoding=store_item.value.get("encoding", "utf-8"))
        created_at = store_item.value.get("created_at")
        if isinstance(created_at, str):
            result["created_at"] = created_at
        modified_at = store_item.value.get("modified_at")
        if isinstance(modified_at, str):
            result["modified_at"] = modified_at
        return result

    def _convert_file_data_to_store_value(self, file_data: FileData | dict[str, Any]) -> dict[str, Any]:
        """Convert `FileData` to a dict suitable for `store.put()`.

        When `file_format="v1"`, emits the legacy shape (`content` as `list[str]`,
        no `encoding` key).

        Args:
            file_data: The `FileData` to convert.

        Returns:
            The dict to persist in the store.
        """
        if self._file_format == "v1":
            return _to_legacy_file_data(file_data)

        result: dict[str, Any] = {
            "content": file_data["content"],
            "encoding": file_data.get("encoding", "utf-8"),
        }
        if "created_at" in file_data:
            result["created_at"] = file_data["created_at"]
        if "modified_at" in file_data:
            result["modified_at"] = file_data["modified_at"]
        return result

    def _search_store_paginated(
        self,
        store: BaseStore,
        namespace: tuple[str, ...],
        *,
        query: str | None = None,
        filter: dict[str, Any] | None = None,  # noqa: A002  # Matches LangGraph BaseStore.search() API
        page_size: int = 100,
    ) -> list[Item]:
        """Search store with automatic pagination to retrieve all results.

        Args:
            store: The store to search.
            namespace: Hierarchical path prefix to search within.
            query: Optional query for natural language search.
            filter: Key-value pairs to filter results.
            page_size: Number of items to fetch per page (default: 100).

        Returns:
            List of all items matching the search criteria.

        Example:
            ```python
            store = _get_store(runtime)
            namespace = _get_namespace()
            all_items = _search_store_paginated(store, namespace)
            ```
        """
        all_items: list[Item] = []
        offset = 0
        while True:
            page_items = store.search(
                namespace,
                query=query,
                filter=filter,
                limit=page_size,
                offset=offset,
            )
            if not page_items:
                break
            all_items.extend(page_items)
            if len(page_items) < page_size:
                break
            offset += page_size

        return all_items

    async def _asearch_store_paginated(
        self,
        store: BaseStore,
        namespace: tuple[str, ...],
        *,
        query: str | None = None,
        filter: dict[str, Any] | None = None,  # noqa: A002  # Matches LangGraph BaseStore.asearch() API
        page_size: int = 100,
    ) -> list[Item]:
        """Async version of `_search_store_paginated`.

        Args:
            store: The store to search.
            namespace: Hierarchical path prefix to search within.
            query: Optional query for natural language search.
            filter: Key-value pairs to filter results.
            page_size: Number of items to fetch per page (default: 100).

        Returns:
            List of all items matching the search criteria.
        """
        all_items: list[Item] = []
        offset = 0
        while True:
            page_items = await store.asearch(
                namespace,
                query=query,
                filter=filter,
                limit=page_size,
                offset=offset,
            )
            if not page_items:
                break
            all_items.extend(page_items)
            if len(page_items) < page_size:
                break
            offset += page_size

        return all_items

    def _load_files(self, store: BaseStore, namespace: tuple[str, ...]) -> dict[str, Any]:
        """Load every file in a namespace as a path -> `FileData` mapping.

        Args:
            store: The store to read from.
            namespace: The namespace to read.

        Returns:
            Mapping of key to `FileData`; unconvertible items are skipped.
        """
        files: dict[str, Any] = {}
        for item in self._search_store_paginated(store, namespace):
            try:
                files[item.key] = self._convert_store_item_to_file_data(item)
            except ValueError:
                continue
        return files

    # -- structured API ------------------------------------------------------

    def ls(self, path: str) -> LsResult:
        """List files and directories in the specified directory (non-recursive).

        Args:
            path: Absolute path to directory.

        Returns:
            `LsResult` whose entries cover files and directories directly in the
                directory. Directories have a trailing `/` and `is_dir=True`.
        """
        store = self._get_store()
        namespace = self._get_namespace()

        # Retrieve all items and filter by path prefix locally to avoid
        # coupling to store-specific filter semantics
        items = self._search_store_paginated(store, namespace)
        infos: list[FileInfo] = []
        subdirs: set[str] = set()

        normalized_path = path if path.endswith("/") else path + "/"

        for item in items:
            if not str(item.key).startswith(normalized_path):
                continue

            relative = str(item.key)[len(normalized_path) :]

            if "/" in relative:
                subdir_name = relative.split("/")[0]
                subdirs.add(normalized_path + subdir_name + "/")
                continue

            try:
                fd = self._convert_store_item_to_file_data(item)
            except ValueError:
                continue
            infos.append(
                {
                    "path": item.key,
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
        store = self._get_store()
        namespace = self._get_namespace()
        item: Item | None = store.get(namespace, file_path)

        if item is None:
            return ReadResult(error=f"Error: File '{file_path}' not found")

        return self._read_result_from_item(item, file_path, offset, limit)

    async def aread_file(
        self,
        file_path: str,
        offset: int = 0,
        limit: int = 2000,
    ) -> ReadResult:
        """Async version of `read_file` using native store async methods.

        Args:
            file_path: Absolute file path.
            offset: Line offset to start reading from (0-indexed).
            limit: Maximum number of lines to read.

        Returns:
            `ReadResult` with the sliced `FileData`, or an error message.
        """
        store = self._get_store()
        namespace = self._get_namespace()
        item: Item | None = await store.aget(namespace, file_path)

        if item is None:
            return ReadResult(error=f"Error: File '{file_path}' not found")

        return self._read_result_from_item(item, file_path, offset, limit)

    def _read_result_from_item(self, item: Item, file_path: str, offset: int, limit: int) -> ReadResult:
        """Turn a store `Item` into a `ReadResult` for a line window.

        Args:
            item: The store item holding the file.
            file_path: The path the item was fetched for (drives text/binary classification).
            offset: Line offset to start reading from (0-indexed).
            limit: Maximum number of lines to read.

        Returns:
            `ReadResult` with the sliced `FileData`, or an error message.
        """
        try:
            file_data = self._convert_store_item_to_file_data(item)
        except ValueError as e:
            return ReadResult(error=f"Error: {e}")

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
            content: Content to write.

        Returns:
            `WriteResult`. External storage sets `files_update=None`.
        """
        store = self._get_store()
        namespace = self._get_namespace()

        existing = store.get(namespace, file_path)
        file_data = self._file_data_for_write(existing, content)
        store.put(namespace, file_path, self._convert_file_data_to_store_value(file_data))
        return WriteResult(path=file_path, files_update=None)

    async def awrite(
        self,
        file_path: str,
        content: str,
    ) -> WriteResult:
        """Async version of write using native store async methods.

        Args:
            file_path: Absolute file path.
            content: Content to write.

        Returns:
            `WriteResult`. External storage sets `files_update=None`.
        """
        store = self._get_store()
        namespace = self._get_namespace()

        existing = await store.aget(namespace, file_path)
        file_data = self._file_data_for_write(existing, content)
        await store.aput(namespace, file_path, self._convert_file_data_to_store_value(file_data))
        return WriteResult(path=file_path, files_update=None)

    def _file_data_for_write(self, existing: Item | None, content: str) -> dict[str, Any]:
        """Build the `FileData` a write should persist.

        Overwrites an existing file (preserving its `created_at`) rather than
        rejecting the write.

        Args:
            existing: The store item currently at the path, if any.
            content: New content.

        Returns:
            The `FileData` to persist.
        """
        if existing is None:
            return create_file_data(content)
        try:
            existing_file_data = self._convert_store_item_to_file_data(existing)
        except ValueError:
            # A corrupt item is not a reason to refuse the write: replace it.
            return create_file_data(content)
        return update_file_data(existing_file_data, content)

    def edit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
        *,
        base_content: dict[str, Any] | None = None,  # noqa: ARG002  # accepted for cross-backend call parity; the store already reflects prior edits
    ) -> EditResult:
        """Edit a file by replacing string occurrences.

        Args:
            file_path: Absolute file path to edit.
            old_string: Exact string to find.
            new_string: Replacement string.
            replace_all: If True, replace all occurrences.
            base_content: Optional `FileData` to edit against instead of
                re-reading from the store. Ignored here — the store already
                reflects prior edits — but accepted so batch callers can pass it
                uniformly across backends.

        Returns:
            `EditResult`. External storage sets `files_update=None`.
        """
        store = self._get_store()
        namespace = self._get_namespace()

        item = store.get(namespace, file_path)
        if item is None:
            return EditResult(error=f"Error: File '{file_path}' not found")

        try:
            file_data = self._convert_store_item_to_file_data(item)
        except ValueError as e:
            return EditResult(error=f"Error: {e}")

        result = perform_string_replacement(file_data_to_string(file_data), old_string, new_string, replace_all)
        if isinstance(result, str):
            return EditResult(error=result)

        new_content, occurrences = result
        new_file_data = update_file_data(file_data, new_content)
        store.put(namespace, file_path, self._convert_file_data_to_store_value(new_file_data))
        return EditResult(path=file_path, files_update=None, occurrences=int(occurrences))

    async def aedit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
        *,
        base_content: dict[str, Any] | None = None,  # noqa: ARG002  # accepted for cross-backend call parity; the store already reflects prior edits
    ) -> EditResult:
        """Async version of edit using native store async methods.

        Args:
            file_path: Absolute file path to edit.
            old_string: Exact string to find.
            new_string: Replacement string.
            replace_all: If True, replace all occurrences.
            base_content: Ignored; see `edit`.

        Returns:
            `EditResult`. External storage sets `files_update=None`.
        """
        store = self._get_store()
        namespace = self._get_namespace()

        item = await store.aget(namespace, file_path)
        if item is None:
            return EditResult(error=f"Error: File '{file_path}' not found")

        try:
            file_data = self._convert_store_item_to_file_data(item)
        except ValueError as e:
            return EditResult(error=f"Error: {e}")

        result = perform_string_replacement(file_data_to_string(file_data), old_string, new_string, replace_all)
        if isinstance(result, str):
            return EditResult(error=result)

        new_content, occurrences = result
        new_file_data = update_file_data(file_data, new_content)
        await store.aput(namespace, file_path, self._convert_file_data_to_store_value(new_file_data))
        return EditResult(path=file_path, files_update=None, occurrences=int(occurrences))

    def delete(self, file_path: str) -> DeleteResult:
        """Delete a file, or a directory and everything nested under it.

        Removes the exact key `file_path` plus every key sharing the
        `file_path + "/"` prefix. Wildcards in `file_path` are treated literally.

        Args:
            file_path: Absolute path of the file or directory to delete.

        Returns:
            `DeleteResult` listing the removed keys, or an error when nothing is
                stored at or under the path.
        """
        store = self._get_store()
        namespace = self._get_namespace()

        items = self._search_store_paginated(store, namespace)
        to_delete = self._keys_to_delete(items, file_path)
        if not to_delete:
            return DeleteResult(error=f"Error: File '{file_path}' not found")

        store.batch([PutOp(namespace, key, None) for key in to_delete])
        return DeleteResult(path=file_path, files_update=None, deleted_paths=to_delete)

    async def adelete(self, file_path: str) -> DeleteResult:
        """Async version of `delete` using native store async methods.

        Args:
            file_path: Absolute path of the file or directory to delete.

        Returns:
            `DeleteResult` listing the removed keys, or an error when nothing is
                stored at or under the path.
        """
        store = self._get_store()
        namespace = self._get_namespace()

        items = await self._asearch_store_paginated(store, namespace)
        to_delete = self._keys_to_delete(items, file_path)
        if not to_delete:
            return DeleteResult(error=f"Error: File '{file_path}' not found")

        await store.abatch([PutOp(namespace, key, None) for key in to_delete])
        return DeleteResult(path=file_path, files_update=None, deleted_paths=to_delete)

    @staticmethod
    def _keys_to_delete(items: list[Item], file_path: str) -> list[str]:
        """Select the keys a recursive delete of `file_path` should remove.

        Args:
            items: Every item in the namespace.
            file_path: The path being deleted.

        Returns:
            Sorted list of keys equal to `file_path` or nested under it.
        """
        base = file_path.rstrip("/") or "/"
        prefix = base + "/"
        return sorted(key for item in items if (key := str(item.key)) == base or key.startswith(prefix))

    def grep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
    ) -> GrepResult:
        """Search store files for a literal text pattern.

        Args:
            pattern: Literal substring to search for (not a regex).
            path: Optional directory or file path to search under.
            glob: Optional include-glob filtering which files are searched.

        Returns:
            `GrepResult` with the matches, or an error message.
        """
        store = self._get_store()
        namespace = self._get_namespace()
        files = self._load_files(store, namespace)
        matches = grep_matches_from_files(files, pattern, path, glob)
        if isinstance(matches, str):
            return GrepResult(error=matches)
        return GrepResult(matches=matches)

    def glob(self, pattern: str, path: str | None = None) -> GlobResult:
        """Find files matching a glob pattern in the store.

        Args:
            pattern: Glob pattern to match against paths.
            path: Optional base directory to search from.

        Returns:
            `GlobResult` with the matching file infos.
        """
        store = self._get_store()
        namespace = self._get_namespace()
        files = self._load_files(store, namespace)
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
        """Upload multiple files to the store.

        Args:
            files: List of `(path, content)` tuples where content is bytes.

        Returns:
            List of `FileUploadResponse` objects, one per input file.
                Response order matches input order.
        """
        store = self._get_store()
        namespace = self._get_namespace()
        responses: list[FileUploadResponse] = []

        for path, content in files:
            # Guard each file independently so one bad payload (e.g. non-UTF-8
            # bytes) does not poison the whole batch — partial success is part
            # of the BackendProtocol upload contract.
            try:
                content_str = content.decode("utf-8")
                file_data = create_file_data(content_str)
                store.put(namespace, path, self._convert_file_data_to_store_value(file_data))
            except (UnicodeDecodeError, ValueError, KeyError):
                responses.append(FileUploadResponse(path=path, error="invalid_path"))
                continue
            responses.append(FileUploadResponse(path=path, error=None))

        return responses

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        """Download multiple files from the store.

        Args:
            paths: List of file paths to download.

        Returns:
            List of `FileDownloadResponse` objects, one per input path.
                Response order matches input order.
        """
        store = self._get_store()
        namespace = self._get_namespace()
        responses: list[FileDownloadResponse] = []

        for path in paths:
            item = store.get(namespace, path)

            if item is None:
                responses.append(FileDownloadResponse(path=path, content=None, error="file_not_found"))
                continue

            # Guard conversion per-file so one corrupt store item does not abort
            # the whole batch — partial success is part of the BackendProtocol
            # download contract, mirroring the ls/grep/glob `except ValueError`.
            try:
                file_data = self._convert_store_item_to_file_data(item)
                content_str = file_data_to_string(file_data)
                if file_data.get("encoding") == "base64":
                    content_bytes = base64.standard_b64decode(content_str)
                else:
                    content_bytes = content_str.encode("utf-8")
            except (ValueError, KeyError):
                responses.append(FileDownloadResponse(path=path, content=None, error="invalid_path"))
                continue

            responses.append(FileDownloadResponse(path=path, content=content_bytes, error=None))

        return responses
