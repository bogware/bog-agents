"""HITL gating for the mutating git tools (CLI-CORE-2 / v4).

GitToolsMiddleware is default-on, so git_commit / git_add / git_branch /
git_stash must be gated behind the approval prompt like write_file/execute —
but only on their mutating paths (branch listing and `git stash list`/`show`
stay un-prompted via a `when` predicate).
"""

from __future__ import annotations

from langchain.tools.tool_node import ToolCallRequest

from bog_agents_cli.agent import (
    _add_interrupt_on,
    _format_git_branch_description,
    _format_git_stash_description,
)


def _req(name: str, args: dict) -> ToolCallRequest:
    return ToolCallRequest(
        tool_call={"name": name, "args": args, "id": "c1", "type": "tool_call"},
        tool=None,
        state=None,
        runtime=None,  # type: ignore[arg-type]
    )


def test_all_mutating_git_tools_are_gated() -> None:
    interrupt_map = _add_interrupt_on()
    for tool in ("git_commit", "git_add", "git_branch", "git_stash"):
        assert tool in interrupt_map, tool
        assert interrupt_map[tool]["allowed_decisions"] == ["approve", "reject"]


def test_git_commit_and_add_are_unconditional() -> None:
    interrupt_map = _add_interrupt_on()
    # Always mutating -> no `when` predicate (gated on every call).
    assert "when" not in interrupt_map["git_commit"]
    assert "when" not in interrupt_map["git_add"]


def test_git_branch_gated_only_when_creating_or_switching() -> None:
    when = _add_interrupt_on()["git_branch"]["when"]
    assert when(_req("git_branch", {"name": "feature/x"})) is True
    assert when(_req("git_branch", {"name": "main", "checkout": True})) is True
    # A bare git_branch call just lists branches — not gated.
    assert when(_req("git_branch", {})) is False
    assert when(_req("git_branch", {"checkout": True})) is False


def test_git_stash_gated_only_on_mutating_actions() -> None:
    when = _add_interrupt_on()["git_stash"]["when"]
    for action in ("push", "pop", "drop"):
        assert when(_req("git_stash", {"action": action})) is True, action
    for action in ("list", "show"):
        assert when(_req("git_stash", {"action": action})) is False, action
    # Default action is list -> not gated.
    assert when(_req("git_stash", {})) is False


def test_descriptions_surface_the_mutation() -> None:
    branch = _format_git_branch_description(
        {"args": {"name": "feature/x", "checkout": True}}, None, None
    )
    assert "feature/x" in branch
    assert "switch" in branch.lower()

    drop = _format_git_stash_description({"args": {"action": "drop"}}, None, None)
    assert "drop" in drop
    assert "discard" in drop.lower()  # destructive action is called out
