"""Security tests: bulk-tool result filtering and recursive-delete denial.

The vulnerability these cover: filesystem permission rules were only ever checked
against a tool call's *path argument*. `ls`, `glob` and `grep` do not require one,
so `grep(pattern="API_KEY")` with no `path` never matched a `deny /secrets/**`
rule — and the tool's result (including the matching lines' text) was returned to
the model unfiltered. `delete` was likewise absent from the permission tables, so a
recursive `delete("/")` could blow away a denied subtree.
"""

from __future__ import annotations

import pytest
from langchain.tools.tool_node import ToolCallRequest
from langchain_core.messages import ToolMessage

from bog_agents.backends.protocol import FileInfo, GlobResult, GrepMatch, GrepResult, LsResult
from bog_agents.middleware.permissions import (
    FilesystemPermission,
    FilesystemPermissionsMiddleware,
    _find_delete_deny_patterns,
    _wildcard_delete_overlap,
    apply_permissions_to_glob_result,
    apply_permissions_to_grep_result,
    apply_permissions_to_ls_result,
    filter_file_infos_by_permission,
    filter_grep_matches_by_permission,
    filter_paths_by_permission,
)

DENY_SECRETS_READ = [FilesystemPermission(operations=["read"], paths=["/secrets/**"], mode="deny")]
DENY_SECRETS_WRITE = [FilesystemPermission(operations=["write"], paths=["/secrets/**"], mode="deny")]
INTERRUPT_SECRETS_READ = [FilesystemPermission(operations=["read"], paths=["/secrets/**"], mode="interrupt")]


def _make_request(tool_name: str, args: dict[str, object]) -> ToolCallRequest:
    """Build a `ToolCallRequest` carrying only the tool_call payload.

    Args:
        tool_name: Name of the tool being called.
        args: Tool-call arguments.

    Returns:
        A `ToolCallRequest` instance.
    """
    return ToolCallRequest(
        tool_call={"name": tool_name, "args": args, "id": "call_1", "type": "tool_call"},
        tool=None,
        state=None,
        runtime=None,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# THE EXPLOIT: pathless bulk calls leak denied paths and their contents.
# ---------------------------------------------------------------------------


def test_exploit_pathless_grep_cannot_exfiltrate_denied_file_contents() -> None:
    """`grep(pattern="API_KEY")` with NO path must not return hits from /secrets.

    Against the pre-fix code the middleware's arg-side check never fires (there is
    no path argument to match `/secrets/**` against) and the backend result is
    handed back verbatim — leaking both the filename and the matching line's text.
    """
    request = _make_request("grep", {"pattern": "API_KEY"})
    middleware = FilesystemPermissionsMiddleware(permissions=DENY_SECRETS_READ)

    backend_result = GrepResult(
        matches=[
            GrepMatch(path="/secrets/prod.env", line=3, text="API_KEY=sk-live-deadbeef"),
            GrepMatch(path="/workspace/app.py", line=9, text="API_KEY = os.environ['API_KEY']"),
        ],
    )

    # The pathless call is not denied at the argument boundary — nothing to match.
    assert not isinstance(middleware.wrap_tool_call(request, lambda _r: backend_result), ToolMessage)

    # ...so the RESULT must be filtered instead.
    filtered = apply_permissions_to_grep_result(DENY_SECRETS_READ, backend_result)
    paths = [m["path"] for m in filtered.matches or []]
    assert paths == ["/workspace/app.py"]
    assert not any("/secrets" in m["path"] for m in filtered.matches or [])
    assert not any("sk-live-deadbeef" in m["text"] for m in filtered.matches or [])


def test_exploit_pathless_ls_does_not_leak_denied_entries() -> None:
    backend_result = LsResult(
        entries=[
            FileInfo(path="/secrets/prod.env"),
            FileInfo(path="/workspace/app.py"),
        ],
    )
    filtered = apply_permissions_to_ls_result(DENY_SECRETS_READ, backend_result)
    assert [e["path"] for e in filtered.entries or []] == ["/workspace/app.py"]


def test_exploit_pathless_glob_does_not_leak_denied_entries() -> None:
    backend_result = GlobResult(
        matches=[
            FileInfo(path="/secrets/prod.env"),
            FileInfo(path="/secrets/nested/deep/key.pem"),
            FileInfo(path="/workspace/app.py"),
        ],
        truncated=True,
    )
    filtered = apply_permissions_to_glob_result(DENY_SECRETS_READ, backend_result)
    assert [e["path"] for e in filtered.matches or []] == ["/workspace/app.py"]
    assert filtered.truncated is True


# ---------------------------------------------------------------------------
# interrupt-mode entries are NOT filtered
# ---------------------------------------------------------------------------


def test_interrupt_entries_pass_through_grep() -> None:
    """`interrupt` means "ask the human on access", not "hide it"."""
    result = GrepResult(matches=[GrepMatch(path="/secrets/prod.env", line=1, text="API_KEY=x")])
    filtered = apply_permissions_to_grep_result(INTERRUPT_SECRETS_READ, result)
    assert [m["path"] for m in filtered.matches or []] == ["/secrets/prod.env"]


def test_interrupt_entries_pass_through_ls_and_glob() -> None:
    entries = [FileInfo(path="/secrets/prod.env"), FileInfo(path="/workspace/a.py")]
    ls_filtered = apply_permissions_to_ls_result(INTERRUPT_SECRETS_READ, LsResult(entries=list(entries)))
    glob_filtered = apply_permissions_to_glob_result(INTERRUPT_SECRETS_READ, GlobResult(matches=list(entries)))
    assert [e["path"] for e in ls_filtered.entries or []] == ["/secrets/prod.env", "/workspace/a.py"]
    assert [e["path"] for e in glob_filtered.matches or []] == ["/secrets/prod.env", "/workspace/a.py"]


def test_allow_and_unmatched_entries_pass_through() -> None:
    rules = [
        FilesystemPermission(operations=["read"], paths=["/secrets/public/**"], mode="allow"),
        FilesystemPermission(operations=["read"], paths=["/secrets/**"], mode="deny"),
    ]
    paths = ["/secrets/public/readme.md", "/secrets/prod.env", "/elsewhere/x.py"]
    assert filter_paths_by_permission(rules, paths) == ["/secrets/public/readme.md", "/elsewhere/x.py"]


# ---------------------------------------------------------------------------
# error results and degenerate inputs pass through untouched
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("result", "apply"),
    [
        (LsResult(error="Error: directory not found"), apply_permissions_to_ls_result),
        (GlobResult(error="Error: bad pattern"), apply_permissions_to_glob_result),
        (GrepResult(error="Error: grep timed out"), apply_permissions_to_grep_result),
    ],
)
def test_error_results_pass_through_untouched(result: object, apply: object) -> None:
    out = apply(DENY_SECRETS_READ, result)  # type: ignore[operator]
    assert out is result


def test_none_payloads_pass_through_untouched() -> None:
    assert apply_permissions_to_ls_result(DENY_SECRETS_READ, LsResult(entries=None)).entries is None
    assert apply_permissions_to_grep_result(DENY_SECRETS_READ, GrepResult(matches=None)).matches is None


def test_no_rules_is_identity() -> None:
    result = GrepResult(matches=[GrepMatch(path="/secrets/prod.env", line=1, text="x")])
    assert apply_permissions_to_grep_result([], result) is result
    assert filter_paths_by_permission([], ["/secrets/a"]) == ["/secrets/a"]


def test_entries_missing_a_path_key_are_kept() -> None:
    infos: list[FileInfo] = [{}]  # type: ignore[typeddict-item]
    assert filter_file_infos_by_permission(DENY_SECRETS_READ, infos) == infos


def test_write_deny_does_not_filter_read_results() -> None:
    """A deny rule scoped to `write` must not hide files from a read-side bulk tool."""
    matches = [GrepMatch(path="/secrets/prod.env", line=1, text="x")]
    assert filter_grep_matches_by_permission(DENY_SECRETS_WRITE, matches) == matches


# ---------------------------------------------------------------------------
# Recursive delete must not blow away a denied subtree
# ---------------------------------------------------------------------------


def test_recursive_delete_of_root_is_refused_when_a_subtree_is_denied() -> None:
    """`delete("/")` with a `deny /secrets/**` rule must be refused, not partially executed."""
    middleware = FilesystemPermissionsMiddleware(permissions=DENY_SECRETS_WRITE)
    request = _make_request("delete", {"file_path": "/"})

    def _handler(_r: ToolCallRequest) -> ToolMessage:
        msg = "delete tool must never be reached"
        raise AssertionError(msg)

    result = middleware.wrap_tool_call(request, _handler)
    assert isinstance(result, ToolMessage)
    assert result.status == "error"
    assert "Permission denied" in str(result.content)


def test_recursive_delete_of_wildcard_root_pattern_is_refused() -> None:
    assert _find_delete_deny_patterns(DENY_SECRETS_WRITE, "/") == ["/secrets/**"]
    assert _find_delete_deny_patterns(DENY_SECRETS_WRITE, "/secrets") == ["/secrets/**"]
    assert _find_delete_deny_patterns(DENY_SECRETS_WRITE, "/secrets/nested/key.pem") == ["/secrets/**"]


def test_delete_of_unrelated_sibling_is_allowed() -> None:
    assert _find_delete_deny_patterns(DENY_SECRETS_WRITE, "/workspace/notes.txt") == []


def test_delete_of_literal_denied_ancestor_and_descendant() -> None:
    rules = [FilesystemPermission(operations=["write"], paths=["/work"], mode="deny")]
    assert _find_delete_deny_patterns(rules, "/work/sub") == ["/work"]
    assert _find_delete_deny_patterns(rules, "/") == ["/work"]
    assert _find_delete_deny_patterns(rules, "/other") == []


def test_delete_sibling_file_glob_does_not_block() -> None:
    """deny `/work/*.log` must not block deleting `/work/notes.txt`."""
    rules = [FilesystemPermission(operations=["write"], paths=["/work/*.log"], mode="deny")]
    assert _find_delete_deny_patterns(rules, "/work/notes.txt") == []
    assert _find_delete_deny_patterns(rules, "/work/app.log") == ["/work/*.log"]
    # Deleting the whole directory would take the denied logs with it.
    assert _find_delete_deny_patterns(rules, "/work") == ["/work/*.log"]


def test_delete_under_a_denied_directory_glob_is_blocked() -> None:
    """deny `/work/*` matches `/work/app`, so deleting `/work/app/child` mutates a denied path."""
    rules = [FilesystemPermission(operations=["write"], paths=["/work/*"], mode="deny")]
    assert _find_delete_deny_patterns(rules, "/work/app/child") == ["/work/*"]


def test_delete_read_only_deny_rule_does_not_block() -> None:
    assert _find_delete_deny_patterns(DENY_SECRETS_READ, "/") == []


def test_wildcard_delete_overlap_root_anchor_fails_closed() -> None:
    assert _wildcard_delete_overlap("/**/secrets", "/", "/anything") is True


def test_delete_of_exactly_denied_path_is_refused_by_middleware() -> None:
    middleware = FilesystemPermissionsMiddleware(permissions=DENY_SECRETS_WRITE)
    request = _make_request("delete", {"file_path": "/secrets/prod.env"})
    result = middleware.wrap_tool_call(request, lambda _r: "deleted")
    assert isinstance(result, ToolMessage)
    assert result.status == "error"


def test_delete_of_allowed_path_passes_through() -> None:
    middleware = FilesystemPermissionsMiddleware(permissions=DENY_SECRETS_WRITE)
    request = _make_request("delete", {"file_path": "/workspace/tmp.txt"})
    assert middleware.wrap_tool_call(request, lambda _r: "deleted") == "deleted"


async def test_delete_denial_also_applies_on_the_async_path() -> None:
    middleware = FilesystemPermissionsMiddleware(permissions=DENY_SECRETS_WRITE)
    request = _make_request("delete", {"file_path": "/"})

    async def _handler(_r: ToolCallRequest) -> str:
        msg = "delete tool must never be reached"
        raise AssertionError(msg)

    result = await middleware.awrap_tool_call(request, _handler)
    assert isinstance(result, ToolMessage)
    assert result.status == "error"


# ---------------------------------------------------------------------------
# Re-bound helpers still importable from this module (single source of truth is
# bog_agents.backends.utils).
# ---------------------------------------------------------------------------


def test_path_helpers_are_rebound_from_backends_utils() -> None:
    from bog_agents.backends import utils as backend_utils
    from bog_agents.middleware import permissions as perms

    assert perms.to_posix_path is backend_utils.to_posix_path
    assert perms._glob_anchor is backend_utils._glob_anchor
    assert perms._paths_overlap is backend_utils._paths_overlap
