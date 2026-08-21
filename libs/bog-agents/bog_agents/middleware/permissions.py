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
4. **Result filters** (`apply_permissions_to_ls_result` and friends) — the
   argument-side check above is not sufficient for the bulk tools. `ls`, `glob`
   and `grep` can be called with no path argument at all, in which case there is
   no path to match a rule against, yet their results happily surface denied
   files (and, for `grep`, denied file *contents*). The filters below remove
   `deny`-mode entries from a bulk result before it reaches the model.

`validate_path`, `to_posix_path`, `_glob_anchor` and `_paths_overlap` all come
from `bog_agents.backends.utils`, which is the single source of truth for path
helpers. They are re-bound at module level here so existing importers of
`bog_agents.middleware.permissions.to_posix_path` (etc.) keep working.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from pathlib import PurePosixPath
from typing import Any, Literal, cast

import wcmatch.glob as wcglob
from langchain.agents.middleware import InterruptOnConfig
from langchain.agents.middleware.types import AgentMiddleware
from langchain.tools.tool_node import ToolCallRequest
from langchain_core.messages import ToolMessage

from bog_agents.backends.protocol import FileInfo, GlobResult, GrepMatch, GrepResult, LsResult
from bog_agents.backends.utils import (
    _GLOB_WILDCARD_CHARS,
    _glob_anchor,
    _paths_overlap,
    to_posix_path,
    validate_path,
)

FilesystemOperation = Literal["read", "write"]
FilesystemMode = Literal["allow", "deny", "interrupt"]

_FS_WCMATCH_FLAGS = wcglob.BRACE | wcglob.GLOBSTAR

_DEFAULT_FS_TOOL_OPS: dict[str, FilesystemOperation] = {
    "ls": "read",
    "read_file": "read",
    "glob": "read",
    "grep": "read",
    "write_file": "write",
    "edit_file": "write",
    "delete": "write",
}


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
    # `delete` names a single path, but the operation is *recursive*: it removes
    # the path plus everything under it. The plain exact-match check is therefore
    # not enough — see `_find_delete_deny_patterns`, which the enforcement
    # middleware uses instead for this tool.
    "delete": ("write", "file_path", "exact", None),
    "glob": ("read", "path", "bulk", "pattern"),
    "grep": ("read", "path", "bulk", None),
}


# Batch filesystem tools take a *list* of targets rather than a single path arg,
# so they don't fit `_FS_TOOL_PATH_ARGS`' single-path shape. Each maps the tool
# to (operation, extractor), where the extractor yields every path the call
# touches. A batch call is denied/interrupted if ANY of its targets hits a
# matching rule. Without this the two default batch tools sailed straight past
# the permission boundary (v4 SB-2), which also let a model rewrite its own
# authority files (`.bog-agents/laws.md`, `.mcp.json`, ...) through the CLI
# self-modification guard in auto-approve mode.
def _multi_edit_targets(args: dict[str, Any]) -> list[str]:
    """Extract every `file_path` a `multi_edit_file` call would write."""
    edits = args.get("edits")
    if not isinstance(edits, list):
        return []
    return [e["file_path"] for e in edits if isinstance(e, dict) and isinstance(e.get("file_path"), str)]


def _read_many_targets(args: dict[str, Any]) -> list[str]:
    """Extract every path/glob a `read_many_files` call would read."""
    paths = args.get("paths")
    if not isinstance(paths, list):
        return []
    return [p for p in paths if isinstance(p, str)]


_FS_BATCH_TOOL_ARGS: dict[str, tuple[FilesystemOperation, Callable[[dict[str, Any]], list[str]]]] = {
    "multi_edit_file": ("write", _multi_edit_targets),
    "read_many_files": ("read", _read_many_targets),
}


def _is_glob_entry(entry: str) -> bool:
    """Whether a batch target string is a glob pattern rather than a literal path."""
    return any(ch in entry for ch in "*?[")


def _batch_entry_mode(rules: list[FilesystemPermission], operation: FilesystemOperation, mode: FilesystemMode, entry: str) -> bool:
    """Whether a single batch target (literal path or glob) hits a rule of `mode`.

    A literal path is checked with the same first-match precedence as any
    single-path tool. A glob is treated like a bulk search root: it hits when
    its anchor could overlap a rule's subtree, so a broad `/**` or an absolute
    glob into a protected directory fails closed.

    Args:
        rules: Permission rules.
        operation: The batch tool's operation (`read`/`write`).
        mode: The rule mode to test for (`deny` or `interrupt`).
        entry: One target from the batch call.

    Returns:
        True when this entry matches a rule of `mode` for `operation`.
    """
    if not _is_glob_entry(entry):
        try:
            normalized = validate_path(entry)
        except ValueError:
            return False
        if normalized == "/.":
            normalized = "/"
        return _check_fs_permission(rules, operation, normalized) == mode
    anchor = _glob_anchor(entry)
    return any(
        _paths_overlap(anchor, _glob_anchor(pattern))
        for rule in rules
        if rule.mode == mode and operation in rule.operations
        for pattern in rule.paths
    )


# ---------------------------------------------------------------------------
# Recursive-delete overlap
# ---------------------------------------------------------------------------


def _wildcard_delete_overlap(pattern: str, anchor: str, target: str) -> bool:
    """Check whether a wildcard deny pattern overlaps a recursive delete target.

    Args:
        pattern: The original glob pattern (e.g. `/work/*.log`).
        anchor: The longest wildcard-free prefix of `pattern`.
        target: The absolute path being recursively deleted.

    Returns:
        True if the pattern's matches intersect the delete subtree.
    """
    # Root anchor ("/**/x"): the pattern can match anywhere, so block.
    if anchor == "/":
        return True
    # Target directly matches the glob: block.
    if wcglob.globmatch(target, pattern, flags=_FS_WCMATCH_FLAGS):
        return True
    # Anchor is inside the delete subtree: a recursive delete would remove
    # matching descendants — block.
    if PurePosixPath(anchor).is_relative_to(PurePosixPath(target)):
        return True
    # Target is below the anchor: safe to allow ONLY when the pattern suffix is a
    # single, non-`**` component (fixed depth) AND no ancestor of the target
    # matches the glob. `/work/*.log` can never match anything under
    # `/work/notes.txt`. But `/work/*` matches `/work/app`, so deleting
    # `/work/app/child` mutates a denied path's contents and must be blocked.
    # Patterns with directory wildcards (`/work/*/secrets`) could match
    # descendants of the target, so fail closed for those.
    if not PurePosixPath(target).is_relative_to(PurePosixPath(anchor)):
        return False
    anchor_parts = PurePosixPath(anchor).parts
    pattern_parts = PurePosixPath(to_posix_path(pattern)).parts
    suffix = pattern_parts[len(anchor_parts) :]
    if len(suffix) != 1 or "**" in suffix[0]:
        return True
    # Check whether any ancestor of the target (between anchor and target)
    # matches the glob. If so, the target lives inside a denied directory.
    target_parts = PurePosixPath(target).parts
    return any(
        wcglob.globmatch(str(PurePosixPath(*target_parts[:depth])), pattern, flags=_FS_WCMATCH_FLAGS)
        for depth in range(len(anchor_parts), len(target_parts))
    )


def _find_delete_deny_patterns(rules: list[FilesystemPermission], target: str) -> list[str]:
    """Return the deny-write patterns that block recursively deleting `target`.

    A recursive delete removes `target` and everything below it, so a deny-write
    pattern blocks the operation whenever it could match `target` *or anything in
    its subtree*. This is what stops `delete("/")` from quietly blowing away a
    denied subtree: a `deny` rule on `/secrets/**` anchors at `/secrets`, which is
    inside the `/` delete subtree, so the delete is refused outright rather than
    partially executed.

    Sibling file globs that cannot match anything inside the deleted subtree (e.g.
    deny `/work/*.log` when deleting `/work/notes.txt`) do not block. Literal
    (wildcard-free) deny patterns use the plain subtree-overlap check, so a deny on
    `/work` blocks deleting `/work/sub` as well as deleting `/`.

    Args:
        rules: Filesystem permission rules.
        target: Absolute, validated path being deleted.

    Returns:
        The matching deny-write patterns, or an empty list if the delete is allowed.
    """
    denying: list[str] = []
    seen: set[str] = set()
    for rule in rules:
        if rule.mode != "deny" or "write" not in rule.operations:
            continue
        for pattern in rule.paths:
            if pattern in seen:
                continue
            anchor = _glob_anchor(pattern)
            if any(c in _GLOB_WILDCARD_CHARS for c in pattern):
                overlaps = _wildcard_delete_overlap(pattern, anchor, target)
            else:
                # Literal pattern: subtree overlap in either direction.
                overlaps = _paths_overlap(target, anchor)
            if overlaps:
                seen.add(pattern)
                denying.append(pattern)
    return denying


# ---------------------------------------------------------------------------
# Result filtering for bulk tools (ls / glob / grep)
# ---------------------------------------------------------------------------
#
# SECURITY: the argument-side deny check in `FilesystemPermissionsMiddleware`
# only sees a tool call's *path argument*. `ls`, `glob` and `grep` do not require
# one — `grep(pattern="API_KEY")` with no `path` has nothing for a rule to match
# against, so a `deny` rule on `/secrets/**` never fires and the tool returns
# matches (including the matching lines' text) straight out of `/secrets`. The
# same hole leaks the existence and names of denied paths through `ls` and `glob`.
# The functions below close it by filtering denied entries out of the *result*.
#
# Interrupt-mode entries deliberately pass through unfiltered: `interrupt` means
# "ask the human when they actually access this", not "hide it". The HITL gate
# fires before the tool runs (see `_build_interrupt_on_from_permissions`), so by
# the time a result exists the human has already approved the call — dropping the
# entries here would silently empty the very listing they approved.


def filter_paths_by_permission(
    rules: list[FilesystemPermission],
    paths: list[str],
    *,
    operation: FilesystemOperation = "read",
) -> list[str]:
    """Drop paths whose effective mode is `deny`.

    Args:
        rules: Ordered list of permission rules.
        paths: Paths produced by a bulk tool.
        operation: Operation the bulk tool performs. Defaults to `"read"`.

    Returns:
        The paths whose effective mode is `allow` or `interrupt`, in input order.
    """
    if not rules:
        return list(paths)
    return [p for p in paths if _check_fs_permission(rules, operation, p) != "deny"]


def filter_file_infos_by_permission(
    rules: list[FilesystemPermission],
    infos: list[FileInfo],
    *,
    operation: FilesystemOperation = "read",
) -> list[FileInfo]:
    """Drop `FileInfo` entries whose effective mode is `deny`.

    Args:
        rules: Ordered list of permission rules.
        infos: Entries produced by `ls` or `glob`.
        operation: Operation the bulk tool performs. Defaults to `"read"`.

    Returns:
        The entries whose effective mode is `allow` or `interrupt`, in input order.
    """
    if not rules:
        return list(infos)
    return [fi for fi in infos if _check_fs_permission(rules, operation, fi.get("path", "")) != "deny"]


def filter_grep_matches_by_permission(
    rules: list[FilesystemPermission],
    matches: list[GrepMatch],
    *,
    operation: FilesystemOperation = "read",
) -> list[GrepMatch]:
    """Drop `GrepMatch` entries whose effective mode is `deny`.

    Each match carries the matching line's text, so an unfiltered match against a
    denied file leaks that file's contents, not merely its name.

    Args:
        rules: Ordered list of permission rules.
        matches: Matches produced by `grep`.
        operation: Operation the bulk tool performs. Defaults to `"read"`.

    Returns:
        The matches whose effective mode is `allow` or `interrupt`, in input order.
    """
    if not rules:
        return list(matches)
    return [m for m in matches if _check_fs_permission(rules, operation, m.get("path", "")) != "deny"]


def apply_permissions_to_ls_result(rules: list[FilesystemPermission], result: LsResult) -> LsResult:
    """Return `result` with `deny`-mode entries removed.

    Args:
        rules: Ordered list of permission rules.
        result: The backend's `ls` result.

    Returns:
        A new `LsResult` with denied entries filtered out. Results carrying an
        `error` (or no entries) are returned untouched.
    """
    if not rules or result.error is not None or result.entries is None:
        return result
    return replace(result, entries=filter_file_infos_by_permission(rules, result.entries))


def apply_permissions_to_glob_result(rules: list[FilesystemPermission], result: GlobResult) -> GlobResult:
    """Return `result` with `deny`-mode matches removed.

    Args:
        rules: Ordered list of permission rules.
        result: The backend's `glob` result.

    Returns:
        A new `GlobResult` with denied matches filtered out; `truncated` is
        preserved. Results carrying an `error` (or no matches) are returned
        untouched.
    """
    if not rules or result.error is not None or result.matches is None:
        return result
    return replace(result, matches=filter_file_infos_by_permission(rules, result.matches))


def apply_permissions_to_grep_result(rules: list[FilesystemPermission], result: GrepResult) -> GrepResult:
    """Return `result` with `deny`-mode matches removed.

    Args:
        rules: Ordered list of permission rules.
        result: The backend's `grep` result.

    Returns:
        A new `GrepResult` with denied matches filtered out; `truncated` is
        preserved. Results carrying an `error` (or no matches) are returned
        untouched.
    """
    if not rules or result.error is not None or result.matches is None:
        return result
    return replace(result, matches=filter_grep_matches_by_permission(rules, result.matches))


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


def _make_batch_when_predicate(
    rules: list[FilesystemPermission],
    operation: FilesystemOperation,
    extractor: Callable[[dict[str, Any]], list[str]],
) -> Callable[[ToolCallRequest], bool]:
    """Build a `when` predicate for a batch filesystem tool.

    Fires when ANY target of the batch call matches an interrupt-mode rule, so a
    `multi_edit_file`/`read_many_files` that touches a protected path is gated
    just like the single-path tools.

    Args:
        rules: Permission rules.
        operation: The batch tool's operation.
        extractor: Yields every path the call touches from its args.

    Returns:
        A predicate that fires when any target hits an interrupt-mode rule.
    """

    def when(req: ToolCallRequest) -> bool:
        args = req.tool_call.get("args", {}) or {}
        return any(_batch_entry_mode(rules, operation, "interrupt", entry) for entry in extractor(args))

    return when


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
    # Batch tools (multi_edit_file/read_many_files) fire when any of their many
    # targets hits an interrupt-mode rule.
    for tool_name, (op, extractor) in _FS_BATCH_TOOL_ARGS.items():
        if not any(r.mode == "interrupt" and op in r.operations for r in rules):
            continue
        config = {
            "allowed_decisions": allowed,
            "when": _make_batch_when_predicate(rules, op, extractor),
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

        `delete` is special-cased: it is recursive, so it is resolved through
        `_find_delete_deny_patterns` rather than an exact-path match. Otherwise
        `delete("/")` would sail past a `deny` rule on `/secrets/**` (the rule's
        pattern does not match the literal path `/`) and destroy the denied
        subtree.

        Args:
            request: The incoming tool-call request.

        Returns:
            The normalized path string when the call must be denied, else None.
        """
        tool_call = request.tool_call or {}
        tool_name = tool_call.get("name", "")
        args = tool_call.get("args", {}) or {}

        batch = _FS_BATCH_TOOL_ARGS.get(tool_name)
        if batch is not None:
            operation, extractor = batch
            for entry in extractor(args):
                if _batch_entry_mode(self.permissions, operation, "deny", entry):
                    if _is_glob_entry(entry):
                        return entry
                    try:
                        return validate_path(entry)
                    except ValueError:
                        return entry
            return None

        spec = _FS_TOOL_PATH_ARGS.get(tool_name)
        if spec is None:
            return None
        operation, path_arg_name, scope, _pattern_arg = spec
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

        if tool_name == "delete":
            return normalized if _find_delete_deny_patterns(self.permissions, normalized) else None

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
        tool_name = tool_call.get("name", "")
        batch = _FS_BATCH_TOOL_ARGS.get(tool_name)
        if batch is not None:
            return batch[0]
        spec = _FS_TOOL_PATH_ARGS.get(tool_name)
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
    "_find_delete_deny_patterns",
    "_glob_anchor",
    "_paths_overlap",
    "_wildcard_delete_overlap",
    "apply_permissions_to_glob_result",
    "apply_permissions_to_grep_result",
    "apply_permissions_to_ls_result",
    "filter_file_infos_by_permission",
    "filter_grep_matches_by_permission",
    "filter_paths_by_permission",
    "to_posix_path",
]
