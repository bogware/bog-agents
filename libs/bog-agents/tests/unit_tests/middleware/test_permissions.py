"""Unit tests for the filesystem-permission system (`bog_agents.middleware.permissions`)."""

from __future__ import annotations

import pytest
from langchain.tools.tool_node import ToolCallRequest
from langchain_core.messages import ToolMessage

from bog_agents.middleware.permissions import (
    FilesystemPermission,
    FilesystemPermissionsMiddleware,
    _build_interrupt_on_from_permissions,
    _check_fs_permission,
)


def _make_request(tool_name: str, args: dict[str, object], tool_call_id: str = "call_1") -> ToolCallRequest:
    """Build a real `ToolCallRequest` carrying just the tool_call payload.

    The permissions middleware and `when` predicates only read `request.tool_call`,
    so `tool`, `state`, and `runtime` are left as None.

    Args:
        tool_name: Name of the tool being called.
        args: Tool-call arguments.
        tool_call_id: Identifier for the tool call.

    Returns:
        A `ToolCallRequest` instance.
    """
    return ToolCallRequest(
        tool_call={"name": tool_name, "args": args, "id": tool_call_id, "type": "tool_call"},
        tool=None,
        state=None,
        runtime=None,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# FilesystemPermission.__post_init__
# ---------------------------------------------------------------------------


def test_post_init_rejects_non_slash_path() -> None:
    with pytest.raises(ValueError, match="must start with '/'"):
        FilesystemPermission(operations=["read"], paths=["relative/path"])


def test_post_init_rejects_dotdot() -> None:
    with pytest.raises(ValueError, match="must not contain '\\.\\.'"):
        FilesystemPermission(operations=["read"], paths=["/foo/../bar"])


def test_post_init_rejects_tilde() -> None:
    with pytest.raises(NotImplementedError, match="must not contain '~'"):
        FilesystemPermission(operations=["read"], paths=["/~/secrets"])


def test_post_init_accepts_valid() -> None:
    rule = FilesystemPermission(operations=["read", "write"], paths=["/workspace/**", "/data/file.txt"], mode="deny")
    assert rule.mode == "deny"
    assert rule.paths == ["/workspace/**", "/data/file.txt"]


# ---------------------------------------------------------------------------
# _check_fs_permission
# ---------------------------------------------------------------------------


def test_check_no_match_returns_allow() -> None:
    rules = [FilesystemPermission(operations=["read"], paths=["/secrets/**"], mode="deny")]
    assert _check_fs_permission(rules, "read", "/workspace/file.txt") == "allow"


def test_check_first_match_wins() -> None:
    rules = [
        FilesystemPermission(operations=["read"], paths=["/secrets/**"], mode="interrupt"),
        FilesystemPermission(operations=["read"], paths=["/secrets/**"], mode="deny"),
    ]
    # First matching rule (interrupt) wins over a later deny.
    assert _check_fs_permission(rules, "read", "/secrets/key.txt") == "interrupt"


def test_check_deny_beats_later_allow() -> None:
    rules = [
        FilesystemPermission(operations=["read"], paths=["/secrets/**"], mode="deny"),
        FilesystemPermission(operations=["read"], paths=["/secrets/**"], mode="allow"),
    ]
    assert _check_fs_permission(rules, "read", "/secrets/key.txt") == "deny"


def test_check_operation_filtering() -> None:
    rules = [FilesystemPermission(operations=["read"], paths=["/secrets/**"], mode="deny")]
    # A read-only rule does not affect a write operation.
    assert _check_fs_permission(rules, "write", "/secrets/key.txt") == "allow"
    assert _check_fs_permission(rules, "read", "/secrets/key.txt") == "deny"


def test_check_globstar_matching() -> None:
    rules = [FilesystemPermission(operations=["read"], paths=["/secrets/**"], mode="deny")]
    assert _check_fs_permission(rules, "read", "/secrets/nested/deep/key.txt") == "deny"
    # `**` should not match a sibling directory.
    assert _check_fs_permission(rules, "read", "/secretsX/key.txt") == "allow"


def test_check_brace_matching() -> None:
    rules = [FilesystemPermission(operations=["read"], paths=["/data/{a,b}/**"], mode="deny")]
    assert _check_fs_permission(rules, "read", "/data/a/file.txt") == "deny"
    assert _check_fs_permission(rules, "read", "/data/b/file.txt") == "deny"
    assert _check_fs_permission(rules, "read", "/data/c/file.txt") == "allow"


# ---------------------------------------------------------------------------
# _build_interrupt_on_from_permissions
# ---------------------------------------------------------------------------


def test_build_interrupt_empty_when_no_interrupt_rules() -> None:
    rules = [
        FilesystemPermission(operations=["read"], paths=["/secrets/**"], mode="deny"),
        FilesystemPermission(operations=["write"], paths=["/workspace/**"], mode="allow"),
    ]
    assert _build_interrupt_on_from_permissions(rules) == {}


def test_build_interrupt_only_for_matching_operation() -> None:
    # Interrupt rule covers only the read operation, so write_file/edit_file
    # (write-op tools) get no entry, while read-op tools do — including the
    # read-op batch tool read_many_files (v5 SAFE-1).
    rules = [FilesystemPermission(operations=["read"], paths=["/secrets/**"], mode="interrupt")]
    configs = _build_interrupt_on_from_permissions(rules)
    assert set(configs) == {"ls", "read_file", "glob", "grep", "read_many_files"}
    assert "write_file" not in configs
    assert "edit_file" not in configs
    assert "multi_edit_file" not in configs  # write-op batch tool, read rule


def test_build_interrupt_exact_predicate_fires_on_match() -> None:
    rules = [FilesystemPermission(operations=["read"], paths=["/secrets/**"], mode="interrupt")]
    configs = _build_interrupt_on_from_permissions(rules)
    when = configs["read_file"]["when"]
    assert when(_make_request("read_file", {"file_path": "/secrets/key.txt"})) is True
    assert when(_make_request("read_file", {"file_path": "/workspace/ok.txt"})) is False


def test_build_interrupt_bulk_fires_on_overlap_and_missing_path() -> None:
    rules = [FilesystemPermission(operations=["read"], paths=["/secrets/**"], mode="interrupt")]
    configs = _build_interrupt_on_from_permissions(rules)
    when = configs["grep"]["when"]
    # Overlapping subtree (call root is an ancestor of /secrets) fires.
    assert when(_make_request("grep", {"path": "/", "pattern": "x"})) is True
    # Exact subtree fires.
    assert when(_make_request("grep", {"path": "/secrets", "pattern": "x"})) is True
    # Non-overlapping subtree does not fire.
    assert when(_make_request("grep", {"path": "/workspace", "pattern": "x"})) is False
    # Missing (pathless) bulk call fires unconditionally.
    assert when(_make_request("grep", {"pattern": "x"})) is True


def test_build_interrupt_glob_pattern_redirection_fires() -> None:
    rules = [FilesystemPermission(operations=["read"], paths=["/secrets/**"], mode="interrupt")]
    configs = _build_interrupt_on_from_permissions(rules)
    when = configs["glob"]["when"]
    # `path` points elsewhere, but the absolute `pattern` redirects into /secrets.
    assert when(_make_request("glob", {"pattern": "/secrets/**", "path": "/workspace"})) is True
    # A pattern that stays in /workspace does not fire.
    assert when(_make_request("glob", {"pattern": "*.txt", "path": "/workspace"})) is False


# ---------------------------------------------------------------------------
# FilesystemPermissionsMiddleware
# ---------------------------------------------------------------------------


def test_middleware_denies_blocked_write_without_handler() -> None:
    rules = [FilesystemPermission(operations=["write"], paths=["/secrets/**"], mode="deny")]
    mw = FilesystemPermissionsMiddleware(permissions=rules)

    called = False

    def handler(req: ToolCallRequest) -> ToolMessage:
        nonlocal called
        called = True
        return ToolMessage(content="ok", tool_call_id="call_1", name="write_file")

    result = mw.wrap_tool_call(_make_request("write_file", {"file_path": "/secrets/key.txt", "content": "x"}), handler)
    assert isinstance(result, ToolMessage)
    assert result.status == "error"
    assert "Permission denied" in str(result.content)
    assert "write" in str(result.content)
    assert "/secrets/key.txt" in str(result.content)
    assert result.tool_call_id == "call_1"
    assert called is False


def test_middleware_allows_permitted_path_calls_handler() -> None:
    rules = [FilesystemPermission(operations=["write"], paths=["/secrets/**"], mode="deny")]
    mw = FilesystemPermissionsMiddleware(permissions=rules)

    called = False

    def handler(req: ToolCallRequest) -> ToolMessage:
        nonlocal called
        called = True
        return ToolMessage(content="ok", tool_call_id="call_1", name="write_file")

    result = mw.wrap_tool_call(_make_request("write_file", {"file_path": "/workspace/ok.txt", "content": "x"}), handler)
    assert called is True
    assert isinstance(result, ToolMessage)
    assert result.status != "error"


def test_middleware_passes_through_non_fs_tool() -> None:
    rules = [FilesystemPermission(operations=["write"], paths=["/secrets/**"], mode="deny")]
    mw = FilesystemPermissionsMiddleware(permissions=rules)

    called = False

    def handler(req: ToolCallRequest) -> ToolMessage:
        nonlocal called
        called = True
        return ToolMessage(content="ok", tool_call_id="call_1", name="some_other_tool")

    result = mw.wrap_tool_call(_make_request("some_other_tool", {"file_path": "/secrets/key.txt"}), handler)
    assert called is True
    assert isinstance(result, ToolMessage)
    assert result.status != "error"


def test_middleware_interrupt_mode_passes_through_deny_enforcement() -> None:
    # Interrupt-mode rules are NOT enforced as denials by this middleware.
    rules = [FilesystemPermission(operations=["write"], paths=["/secrets/**"], mode="interrupt")]
    mw = FilesystemPermissionsMiddleware(permissions=rules)

    called = False

    def handler(req: ToolCallRequest) -> ToolMessage:
        nonlocal called
        called = True
        return ToolMessage(content="ok", tool_call_id="call_1", name="write_file")

    result = mw.wrap_tool_call(_make_request("write_file", {"file_path": "/secrets/key.txt", "content": "x"}), handler)
    assert called is True
    assert result.status != "error"


async def test_middleware_async_denies_blocked_write() -> None:
    rules = [FilesystemPermission(operations=["write"], paths=["/secrets/**"], mode="deny")]
    mw = FilesystemPermissionsMiddleware(permissions=rules)

    called = False

    async def handler(req: ToolCallRequest) -> ToolMessage:
        nonlocal called
        called = True
        return ToolMessage(content="ok", tool_call_id="call_1", name="write_file")

    result = await mw.awrap_tool_call(_make_request("write_file", {"file_path": "/secrets/key.txt", "content": "x"}), handler)
    assert isinstance(result, ToolMessage)
    assert result.status == "error"
    assert called is False


def test_middleware_bulk_missing_path_denied_for_whole_tree_rule() -> None:
    # A deny rule on the whole tree should block a pathless bulk grep.
    rules = [FilesystemPermission(operations=["read"], paths=["/**"], mode="deny")]
    mw = FilesystemPermissionsMiddleware(permissions=rules)

    called = False

    def handler(req: ToolCallRequest) -> ToolMessage:
        nonlocal called
        called = True
        return ToolMessage(content="ok", tool_call_id="call_1", name="grep")

    result = mw.wrap_tool_call(_make_request("grep", {"pattern": "x"}), handler)
    assert isinstance(result, ToolMessage)
    assert result.status == "error"
    assert called is False


# ---------------------------------------------------------------------------
# Batch filesystem tools (multi_edit_file / read_many_files) — v5 SAFE-1
# ---------------------------------------------------------------------------


class TestBatchToolCoverage:
    """multi_edit_file / read_many_files must honor deny + interrupt rules.

    They take a *list* of targets, so they used to slip past the single-path
    permission boundary entirely (v4 SB-2), which also let a model rewrite its
    own authority files through the CLI self-modification guard.
    """

    def _deny_secrets(self) -> FilesystemPermissionsMiddleware:
        return FilesystemPermissionsMiddleware(permissions=[FilesystemPermission(paths=["/secrets/**"], operations=["read", "write"], mode="deny")])

    def _ran(self, mw: FilesystemPermissionsMiddleware, tool: str, args: dict) -> bool:
        out = mw.wrap_tool_call(_make_request(tool, args), lambda _r: "TOOL-RAN")
        return not isinstance(out, ToolMessage)

    def test_multi_edit_denied_when_any_target_denied(self) -> None:
        mw = self._deny_secrets()
        assert not self._ran(
            mw,
            "multi_edit_file",
            {"edits": [{"file_path": "/ok/a"}, {"file_path": "/secrets/x"}]},
        )

    def test_multi_edit_runs_when_all_targets_allowed(self) -> None:
        mw = self._deny_secrets()
        assert self._ran(mw, "multi_edit_file", {"edits": [{"file_path": "/ok/a"}]})

    def test_read_many_denied_on_literal_target(self) -> None:
        mw = self._deny_secrets()
        assert not self._ran(mw, "read_many_files", {"paths": ["/ok/a", "/secrets/x"]})

    def test_read_many_denied_on_broad_glob(self) -> None:
        # A `/**` read glob overlaps the denied subtree — fail closed.
        mw = self._deny_secrets()
        assert not self._ran(mw, "read_many_files", {"paths": ["/**"]})

    def test_read_many_runs_on_unrelated_paths(self) -> None:
        mw = self._deny_secrets()
        assert self._ran(mw, "read_many_files", {"paths": ["/ok/a.py", "/ok/b.py"]})

    def test_interrupt_rule_gates_multi_edit(self) -> None:
        interrupt_on = _build_interrupt_on_from_permissions([FilesystemPermission(paths=["/.bog-agents/**"], operations=["write"], mode="interrupt")])
        assert "multi_edit_file" in interrupt_on
        when = interrupt_on["multi_edit_file"]["when"]  # type: ignore[typeddict-item]
        assert when(_make_request("multi_edit_file", {"edits": [{"file_path": "/.bog-agents/laws.md"}]}))
        assert not when(_make_request("multi_edit_file", {"edits": [{"file_path": "/src/app.py"}]}))

    def test_interrupt_rule_gates_read_many(self) -> None:
        interrupt_on = _build_interrupt_on_from_permissions([FilesystemPermission(paths=["/secrets/**"], operations=["read"], mode="interrupt")])
        assert "read_many_files" in interrupt_on
        when = interrupt_on["read_many_files"]["when"]  # type: ignore[typeddict-item]
        assert when(_make_request("read_many_files", {"paths": ["/ok/a", "/secrets/x"]}))
        assert not when(_make_request("read_many_files", {"paths": ["/ok/a"]}))
