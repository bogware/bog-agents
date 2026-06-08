"""Filesystem-permission rules, enforcement middleware, and HITL interrupt glue.

This module ports the deepagents filesystem-permission system into bog-agents
as a self-contained unit. It provides three things:

1. `FilesystemPermission` — a declarative access rule (operations + path globs +
   mode) with validation in `__post_init__`.
2. `FilesystemPermissionsMiddleware` — a tool-call boundary enforcer that
   short-circuits `deny`-mode rules with a permission-denied `ToolMessage`
   (allow / interrupt rules pass through).
3. `_build_interrupt_on_from_permissions` — turns `interrupt`-mode rules into an
   `interrupt_on` mapping for
   [`HumanInTheLoopMiddleware`][langchain.agents.middleware.HumanInTheLoopMiddleware],
   using per-tool `when` predicates that fire only when a call's path could
   intersect an interrupt-mode rule.

`validate_path` is reused from `bog_agents.backends.utils`. The glob-anchor and
overlap helpers (`to_posix_path`, `_glob_anchor`, `_paths_overlap`) are not
present there, so they are ported verbatim here.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Literal, cast

import wcmatch.glob as wcglob
from langchain.agents.middleware import InterruptOnConfig
from langchain.agents.middleware.types import AgentMiddleware
from langchain.tools.tool_node import ToolCallRequest
from langchain_core.messages import ToolMessage

from bog_agents.backends.utils import validate_path

FilesystemOperation = Literal["read", "write"]

_FS_WCMATCH_FLAGS = wcglob.BRACE | wcglob.GLOBSTAR

_DEFAULT_FS_TOOL_OPS: dict[str, FilesystemOperation] = {
    "ls": "read",
    "read_file": "read",
    "glob": "read",
    "grep": "read",
    "write_file": "write",
    "edit_file": "write",
}


# ---------------------------------------------------------------------------
# Path helpers (ported from deepagents.backends.utils — not present in bog's
# backends/utils.py). `validate_path` is imported from bog instead.
# ---------------------------------------------------------------------------

# Characters that mark a glob path component as a wildcard segment for the
# purposes of `_glob_anchor`. Keep in sync with the wcmatch flags used by the
# filesystem middleware (`BRACE | GLOBSTAR`).
_GLOB_WILDCARD_CHARS = frozenset("*?[{")


def to_posix_path(path: str) -> str:
    r"""Normalize backslash separators to forward slashes for `PurePosixPath` use.

    Backends running on Windows return OS-native paths using backslashes.
    `PurePosixPath` treats backslashes as literal filename characters, so
    `PurePosixPath(r"C:\a\b").name` yields the full string instead of `"b"`.
    Normalize before constructing a `PurePosixPath`.

    This is best-effort: a POSIX directory literally named with a backslash will
    also be rewritten. That trade-off is accepted because such filenames are
    vanishingly rare in practice and the alternative (gating on `os.sep`) fails
    when a Windows-style path is handed to a non-Windows process.

    Args:
        path: Path string that may use backslash separators.

    Returns:
        The same path with every backslash replaced by `/`. Inputs that already
        use forward slashes are returned unchanged.
    """
    return path.replace("\\", "/")


def _glob_anchor(pattern: str) -> str:
    """Return the longest leading directory of `pattern` with no wildcards.

    For `/secrets/**` returns `/secrets`; for `/a/*/b` returns `/a`; for a
    pattern with a wildcard at or near the root (`/**/secrets`, `/*/foo`) falls
    back to `/`. The root fallback causes overlap checks to match any subtree —
    conservative over-gating, since we cannot statically pin down where the rule
    could resolve. Callers wanting precise gating should anchor the rule's
    leading components.

    Args:
        pattern: A glob pattern.

    Returns:
        The longest wildcard-free leading directory, or `/` if none.
    """
    parts = PurePosixPath(to_posix_path(pattern)).parts
    safe: list[str] = []
    for part in parts:
        if any(c in _GLOB_WILDCARD_CHARS for c in part):
            break
        safe.append(part)
    if not safe:
        return "/"
    return str(PurePosixPath(*safe))


def _paths_overlap(call_path: str, rule_anchor: str) -> bool:
    """Return True if the subtree at `call_path` intersects the subtree at `rule_anchor`.

    Two subtrees overlap when one is a (component-wise) prefix of the other, or
    they're equal. Comparison runs on `PurePosixPath` components, so `/secret`
    does not overlap `/secrets`. The root `/` overlaps everything.

    Args:
        call_path: Normalized path of the call's search root.
        rule_anchor: Anchor (wildcard-free prefix) of a rule's pattern.

    Returns:
        True if the two subtrees intersect.
    """
    a = PurePosixPath(call_path)
    b = PurePosixPath(rule_anchor)
    return a == b or a.is_relative_to(b) or b.is_relative_to(a)


# ---------------------------------------------------------------------------
# Permission rules
# ---------------------------------------------------------------------------


@dataclass
class FilesystemPermission:
    """A single access rule for filesystem operations."""

    operations: list[FilesystemOperation]
    paths: list[str]
    mode: Literal["allow", "deny", "interrupt"] = "allow"
    """Effect when a tool call matches this rule:

    - `"allow"` (default): the call proceeds.
    - `"deny"`: the tool returns a permission-denied error.
    - `"interrupt"`: the call is paused for human approval via
      [`HumanInTheLoopMiddleware`][langchain.agents.middleware.HumanInTheLoopMiddleware].

      Best paired with patterns that have a literal leading anchor (e.g.,
      `/secrets/**`, `/projects/*/secrets/**`). Bulk tools (`ls`/`glob`/`grep`)
      fire the interrupt based on whether their search subtree could overlap the
      rule's anchored prefix, so a fully unanchored pattern (`/**/secrets`)
      collapses to `/` and conservatively over-fires for any bulk call.
    """

    def __post_init__(self) -> None:
        """Validate permission path patterns.

        Raises:
            ValueError: If a path does not start with `/` or contains `..`.
            NotImplementedError: If a path contains `~` (home expansion is
                unsupported for permission patterns).
        """
        for path in self.paths:
            if not path.startswith("/"):
                msg = f"Permission path must start with '/': {path!r}"
                raise ValueError(msg)
            parts = PurePosixPath(path.replace("\\", "/")).parts
            if ".." in parts:
                msg = f"Permission path must not contain '..': {path!r}"
                raise ValueError(msg)
            if "~" in parts:
                msg = f"Permission path must not contain '~': {path!r}"
                raise NotImplementedError(msg)


def _check_fs_permission(
    rules: list[FilesystemPermission],
    operation: FilesystemOperation,
    path: str,
) -> Literal["allow", "deny", "interrupt"]:
    """Resolve the effective mode for an operation on a path (first match wins).

    Args:
        rules: Ordered list of permission rules.
        operation: The filesystem operation being attempted.
        path: Normalized path the operation targets.

    Returns:
        The mode (`"allow"`, `"deny"`, or `"interrupt"`) of the first rule whose
        operation set includes `operation` and one of whose patterns matches
        `path`. Defaults to `"allow"` when no rule matches.
    """
    for rule in rules:
        if operation not in rule.operations:
            continue
        if any(wcglob.globmatch(path, pattern, flags=_FS_WCMATCH_FLAGS) for pattern in rule.paths):
            return rule.mode
    return "allow"


# ---------------------------------------------------------------------------
# Interrupt (HITL) glue
# ---------------------------------------------------------------------------

# Scope of a filesystem tool's path argument:
#   - "exact": the call operates on exactly the named path (read_file,
#     write_file, edit_file). Interrupt fires iff that path matches an
#     interrupt-mode rule.
#   - "bulk":  the call's path argument names a search root and the call may
#     surface any descendant (ls, glob, grep). Interrupt fires whenever the
#     search subtree intersects an interrupt-mode rule's pattern, and — when
#     the path argument is omitted (`grep(path=None)`) — fires unconditionally
#     for any interrupt-mode rule, because a pathless bulk call can touch
#     anything.
ToolScope = Literal["exact", "bulk"]

# Map filesystem tool name -> (operation, path-arg name, scope, pattern-arg name).
# Drives `_build_interrupt_on_from_permissions` when synthesizing `when`
# predicates per tool. The optional pattern-arg name is set only for `glob`,
# whose `pattern` argument can itself redirect the search root (an absolute
# pattern ignores the call's `path`); see `_make_bulk_when_predicate`.
_FS_TOOL_PATH_ARGS: dict[str, tuple[FilesystemOperation, str, ToolScope, str | None]] = {
    "ls": ("read", "path", "bulk", None),
    "read_file": ("read", "file_path", "exact", None),
    "write_file": ("write", "file_path", "exact", None),
    "edit_file": ("write", "file_path", "exact", None),
    "glob": ("read", "path", "bulk", "pattern"),
    "grep": ("read", "path", "bulk", None),
}


def _make_fs_when_predicate(
    rules: list[FilesystemPermission],
    operation: FilesystemOperation,
    path_arg_name: str,
    scope: ToolScope,
    pattern_arg_name: str | None = None,
) -> Callable[[ToolCallRequest], bool]:
    """Build a `when` predicate that fires on interrupt-mode rule matches.

    The predicate's behavior depends on the tool's `ToolScope`:

    - `"exact"`: fire iff the call's path matches an interrupt-mode rule with
      normal first-match precedence. A preceding `deny` rule wins and the
      interrupt does not fire — the tool returns a permission-denied error
      instead.
    - `"bulk"`: fire iff the call's search subtree could intersect an
      interrupt-mode rule. With no path argument (e.g. `grep(path=None)`) we
      cannot localize the call, so we fire unconditionally for any
      interrupt-mode rule on the operation. `pattern_arg_name` (set for `glob`)
      additionally gates the call's `pattern`, which can redirect the search
      root independently of `path`.

    Args:
        rules: Permission rules.
        operation: Operation the tool performs.
        path_arg_name: Name of the tool's path argument.
        scope: Whether the tool is `"exact"` or `"bulk"`.
        pattern_arg_name: Optional name of a glob-pattern argument.

    Returns:
        A predicate taking a `ToolCallRequest` and returning whether to interrupt.
    """
    if scope == "exact":
        return _make_exact_when_predicate(rules, operation, path_arg_name)
    return _make_bulk_when_predicate(rules, operation, path_arg_name, pattern_arg_name)


def _make_exact_when_predicate(
    rules: list[FilesystemPermission],
    operation: FilesystemOperation,
    path_arg_name: str,
) -> Callable[[ToolCallRequest], bool]:
    """Build a `when` predicate for an exact-scope filesystem tool.

    Args:
        rules: Permission rules.
        operation: Operation the tool performs.
        path_arg_name: Name of the tool's path argument.

    Returns:
        A predicate that fires when the call's path matches an interrupt-mode rule.
    """

    def when(req: ToolCallRequest) -> bool:
        raw_path = req.tool_call.get("args", {}).get(path_arg_name)
        if not isinstance(raw_path, str):
            return False
        try:
            normalized = validate_path(raw_path)
        except ValueError:
            return False
        return _check_fs_permission(rules, operation, normalized) == "interrupt"

    return when


def _make_bulk_when_predicate(
    rules: list[FilesystemPermission],
    operation: FilesystemOperation,
    path_arg_name: str,
    pattern_arg_name: str | None = None,
) -> Callable[[ToolCallRequest], bool]:
    """Build a `when` predicate for a bulk-scope filesystem tool.

    Args:
        rules: Permission rules.
        operation: Operation the tool performs.
        path_arg_name: Name of the tool's path (search-root) argument.
        pattern_arg_name: Optional name of a glob-pattern argument that can
            redirect the search root.

    Returns:
        A predicate that fires when the call's search subtree could intersect an
        interrupt-mode rule.
    """
    # Precompute interrupt-mode rule anchors for this op so the predicate is a
    # single pass per call.
    interrupt_anchors: list[str] = [
        _glob_anchor(pattern) for rule in rules if rule.mode == "interrupt" and operation in rule.operations for pattern in rule.paths
    ]

    def when(req: ToolCallRequest) -> bool:
        if not interrupt_anchors:
            return False
        args = req.tool_call.get("args", {})
        raw_path = args.get(path_arg_name)
        if not isinstance(raw_path, str):
            # A missing path (pathless bulk call) can't be localized, so fire;
            # any other non-string is malformed, so don't.
            return raw_path is None
        try:
            normalized = validate_path(raw_path)
        except ValueError:
            return False
        # `validate_path` returns `/.` for current-directory aliases like `"."`,
        # `""`, and `"./"`. Those refer to the whole accessible tree just like a
        # missing path arg, so collapse to `/` so the root-overlaps-everything
        # branch in `_paths_overlap` fires. Without this, an agent could pass
        # `path="."` to bypass HITL.
        if normalized == "/.":
            normalized = "/"
        if any(_paths_overlap(normalized, anchor) for anchor in interrupt_anchors):
            return True
        # `glob`'s `pattern` can redirect the search root away from `path`, so
        # gating on `path` alone would let `glob(pattern="/secrets/**",
        # path="/workspace")` bypass an interrupt rule on `/secrets/**`.
        if pattern_arg_name is not None:
            raw_pattern = args.get(pattern_arg_name)
            if isinstance(raw_pattern, str) and _bulk_pattern_fires(raw_pattern, interrupt_anchors):
                return True
        return False

    return when


def _bulk_pattern_fires(raw_pattern: str, interrupt_anchors: list[str]) -> bool:
    """Whether a glob `pattern` reaches an interrupt-mode subtree regardless of `path`.

    An absolute pattern is matched from its own root — Python's `glob` ignores
    the backend's `os.chdir(path)` — so gate on the pattern's anchor. A relative
    pattern containing `..` can climb out of `path`; we cannot localize where it
    lands, so treat it as firing. Absoluteness comes from the raw pattern, not
    `_glob_anchor`: the anchor of a leading-wildcard relative pattern (`*.txt`)
    collapses to `/`, which would otherwise look absolute.

    Args:
        raw_pattern: The raw glob pattern from the call.
        interrupt_anchors: Precomputed anchors of interrupt-mode rules.

    Returns:
        True if the pattern could reach an interrupt-mode subtree.
    """
    posix_pattern = to_posix_path(raw_pattern)
    if posix_pattern.startswith("/"):
        return any(_paths_overlap(_glob_anchor(raw_pattern), anchor) for anchor in interrupt_anchors)
    return ".." in PurePosixPath(posix_pattern).parts


def _build_interrupt_on_from_permissions(
    rules: list[FilesystemPermission],
) -> dict[str, InterruptOnConfig]:
    """Generate `interrupt_on` configs from interrupt-mode permissions.

    Returns an entry for each filesystem tool whose operation could be triggered
    by at least one interrupt-mode rule. Each entry uses a `when` predicate so
    the interrupt only fires when the tool call's path argument matches an
    interrupt-mode rule.

    Args:
        rules: Permission rules.

    Returns:
        Mapping from filesystem tool name to its `InterruptOnConfig`. Empty when
        no rule uses `interrupt` mode.
    """
    if not any(r.mode == "interrupt" for r in rules):
        return {}

    # Offer the approver the full decision set, matching the default for
    # user-supplied `interrupt_on` tools. All four are human-controlled, so the
    # human stays the authorization gate: `edit`ed calls still re-enter the tool
    # and hit its pre-execution deny check, and `respond` skips execution.
    allowed: list[Literal["approve", "edit", "reject", "respond"]] = ["approve", "edit", "reject", "respond"]
    result: dict[str, InterruptOnConfig] = {}
    for tool_name, (op, arg, scope, pattern_arg) in _FS_TOOL_PATH_ARGS.items():
        if not any(r.mode == "interrupt" and op in r.operations for r in rules):
            continue
        # Each entry carries a `when` predicate so the interrupt only fires when
        # a call's path could intersect an interrupt-mode rule. Note: some
        # langchain releases (e.g. 1.3.x) declare `InterruptOnConfig` as a
        # TypedDict without a `when` key. The predicate is preserved here as an
        # extra runtime key (harmless on a plain dict) for callers whose HITL
        # wiring consumes it; we cast to keep the static type honest.
        config: dict[str, Any] = {
            "allowed_decisions": allowed,
            "when": _make_fs_when_predicate(rules, op, arg, scope, pattern_arg),
        }
        result[tool_name] = cast("InterruptOnConfig", config)
    return result


# ---------------------------------------------------------------------------
# Enforcement middleware
# ---------------------------------------------------------------------------


class FilesystemPermissionsMiddleware(AgentMiddleware):
    """Enforce `deny`-mode filesystem permission rules at the tool-call boundary.

    For every filesystem tool call (those listed in `_FS_TOOL_PATH_ARGS`), this
    middleware reads the call's path argument, normalizes it, and consults the
    permission rules. If the effective mode is `"deny"`, the call is
    short-circuited with a permission-denied `ToolMessage` and the underlying
    tool is never invoked. `allow` and `interrupt` rules pass through unchanged —
    `interrupt` is handled separately via `_build_interrupt_on_from_permissions`
    wired into `HumanInTheLoopMiddleware`.

    Non-filesystem tool calls always pass through unchanged.
    """

    name = "FilesystemPermissionsMiddleware"

    def __init__(self, *, permissions: list[FilesystemPermission]) -> None:
        """Initialize the middleware.

        Args:
            permissions: Ordered list of filesystem permission rules.
        """
        super().__init__()
        self.permissions: list[FilesystemPermission] = list(permissions or [])

    def _resolve_denied_path(self, request: ToolCallRequest) -> str | None:
        """Return the normalized path to deny, or None if the call should proceed.

        Looks up the tool in `_FS_TOOL_PATH_ARGS`. Non-filesystem tools return
        None. For filesystem tools, reads the path argument (a missing path on a
        bulk tool means the whole tree, represented as `/`), validates it (on
        `ValueError` returns None so the tool itself can reject the bad path),
        and returns the normalized path only when `_check_fs_permission` resolves
        to `"deny"`.

        Args:
            request: The incoming tool-call request.

        Returns:
            The normalized path string when the call must be denied, else None.
        """
        tool_call = request.tool_call or {}
        tool_name = tool_call.get("name", "")
        spec = _FS_TOOL_PATH_ARGS.get(tool_name)
        if spec is None:
            return None
        operation, path_arg_name, scope, _pattern_arg = spec
        args = tool_call.get("args", {}) or {}
        raw_path = args.get(path_arg_name)

        if not isinstance(raw_path, str):
            # A bulk tool without a path argument targets the whole tree; an
            # exact tool without a usable path can't be localized, so let the
            # tool reject it.
            if scope == "bulk" and raw_path is None:
                normalized = "/"
            else:
                return None
        else:
            try:
                normalized = validate_path(raw_path)
            except ValueError:
                # Let the tool reject the malformed path with its own error.
                return None
            if normalized == "/.":
                normalized = "/"

        if _check_fs_permission(self.permissions, operation, normalized) == "deny":
            return normalized
        return None

    def _make_deny_message(self, request: ToolCallRequest, operation: FilesystemOperation, path: str) -> ToolMessage:
        """Build the permission-denied `ToolMessage` for a blocked call.

        Args:
            request: The denied tool-call request.
            operation: The operation that was blocked.
            path: The normalized path that was blocked.

        Returns:
            A `ToolMessage` with `status="error"` carrying the denial reason.
        """
        tool_call = request.tool_call or {}
        content = f"Permission denied: {operation} access to {path} is blocked by a filesystem permission rule."
        return ToolMessage(
            content=content,
            tool_call_id=str(tool_call.get("id", "")),
            name=str(tool_call.get("name", "")),
            status="error",
        )

    def _operation_for(self, request: ToolCallRequest) -> FilesystemOperation:
        """Return the operation associated with the request's tool.

        Args:
            request: The tool-call request.

        Returns:
            The operation, defaulting to `"read"` for unknown tools.
        """
        tool_call = request.tool_call or {}
        spec = _FS_TOOL_PATH_ARGS.get(tool_call.get("name", ""))
        return spec[0] if spec is not None else "read"

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Any],
    ) -> ToolMessage | Any:
        """Deny blocked filesystem calls; otherwise pass through to the handler.

        Args:
            request: The incoming tool-call request.
            handler: The downstream tool-call handler.

        Returns:
            A permission-denied `ToolMessage` for blocked calls, else the
            handler's result.
        """
        if not self.permissions:
            return handler(request)
        denied_path = self._resolve_denied_path(request)
        if denied_path is not None:
            return self._make_deny_message(request, self._operation_for(request), denied_path)
        return handler(request)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Any]],
    ) -> ToolMessage | Any:
        """Async version of `wrap_tool_call`.

        Args:
            request: The incoming tool-call request.
            handler: The downstream async tool-call handler.

        Returns:
            A permission-denied `ToolMessage` for blocked calls, else the
            handler's result.
        """
        if not self.permissions:
            return await handler(request)
        denied_path = self._resolve_denied_path(request)
        if denied_path is not None:
            return self._make_deny_message(request, self._operation_for(request), denied_path)
        return await handler(request)


__all__ = [
    "FilesystemOperation",
    "FilesystemPermission",
    "FilesystemPermissionsMiddleware",
    "_build_interrupt_on_from_permissions",
    "_check_fs_permission",
]
