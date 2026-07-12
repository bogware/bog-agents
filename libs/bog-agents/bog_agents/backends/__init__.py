"""Memory backends for pluggable file storage."""

from typing import TYPE_CHECKING, Any

from bog_agents.backends.composite import CompositeBackend
from bog_agents.backends.filesystem import FilesystemBackend
from bog_agents.backends.local_shell import DEFAULT_EXECUTE_TIMEOUT, LocalShellBackend
from bog_agents.backends.protocol import (
    ASYNC_GREP_TIMEOUT,
    DEFAULT_GREP_TIMEOUT,
    BackendProtocol,
    DeleteResult,
    ExecuteOffloadResult,
    FileData,
    FileFormat,
    FileInfo,
    GlobResult,
    GrepMatch,
    GrepResult,
    LsResult,
    ReadResult,
    SandboxBackendProtocol,
    supports_delete,
)
from bog_agents.backends.sandbox import (
    MAX_OUTPUT_BYTES,
    TRUNCATION_MSG,
    BaseSandbox,
)
from bog_agents.backends.state import StateBackend
from bog_agents.backends.store import (
    BackendContext,
    NamespaceFactory,
    StoreBackend,
)
from bog_agents.backends.utils import MAX_BINARY_BYTES

if TYPE_CHECKING:
    from bog_agents.backends.context_hub import ContextHubBackend
    from bog_agents.backends.langsmith import LangSmithSandbox

# `context_hub` and `langsmith` reach for the `langsmith` SDK, an optional
# extra. Resolving them on attribute access keeps `import bog_agents.backends`
# working — and cheap — for the majority of users who never touch them.
_LAZY_IMPORTS: dict[str, tuple[str, str]] = {
    "ContextHubBackend": ("bog_agents.backends.context_hub", "ContextHubBackend"),
    "LangSmithSandbox": ("bog_agents.backends.langsmith", "LangSmithSandbox"),
}


def __getattr__(name: str) -> Any:  # noqa: ANN401
    """Resolve the optional, `langsmith`-backed backends on first access.

    Args:
        name: Attribute requested on the `bog_agents.backends` package.

    Returns:
        The resolved backend class.

    Raises:
        AttributeError: If `name` is not an exported symbol of this package.
    """
    target = _LAZY_IMPORTS.get(name)
    if target is None:
        msg = f"module {__name__!r} has no attribute {name!r}"
        raise AttributeError(msg)

    import importlib

    module_path, attr = target
    return getattr(importlib.import_module(module_path), attr)


def __dir__() -> list[str]:
    """List the package's exported symbols, including the lazy ones."""
    return sorted(__all__)


__all__ = [
    "ASYNC_GREP_TIMEOUT",
    "DEFAULT_EXECUTE_TIMEOUT",
    "DEFAULT_GREP_TIMEOUT",
    "MAX_BINARY_BYTES",
    "MAX_OUTPUT_BYTES",
    "TRUNCATION_MSG",
    "BackendContext",
    "BackendProtocol",
    "BaseSandbox",
    "CompositeBackend",
    "ContextHubBackend",
    "DeleteResult",
    "ExecuteOffloadResult",
    "FileData",
    "FileFormat",
    "FileInfo",
    "FilesystemBackend",
    "GlobResult",
    "GrepMatch",
    "GrepResult",
    "LangSmithSandbox",
    "LocalShellBackend",
    "LsResult",
    "NamespaceFactory",
    "ReadResult",
    "SandboxBackendProtocol",
    "StateBackend",
    "StoreBackend",
    "supports_delete",
]
