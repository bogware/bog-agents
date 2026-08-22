"""Tests for decision-capable hooks + Claude/Cursor compat (hook-bus completion)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from bog_agents_cli.hook_decisions import (
    CANONICAL_EVENTS,
    GATE_EVENTS,
    GATING_EVENTS,
    HOOK_TYPES,
    MODIFY_EVENTS,
    OBSERVE_EVENTS,
    HookDecision,
    HookType,
    alias_tool_name,
    evaluate_decision_hooks,
    hook_type_for_event,
    load_vendor_hooks,
    parse_hook_decision,
)

PY = sys.executable


class TestAliasing:
    def test_claude_names_mapped(self) -> None:
        assert alias_tool_name("Bash") == "execute"
        assert alias_tool_name("Edit") == "edit_file"
        assert alias_tool_name("Read") == "read_file"

    def test_unknown_passthrough(self) -> None:
        assert alias_tool_name("my_custom_tool") == "my_custom_tool"


class TestParseDecision:
    def test_deny_from_json(self) -> None:
        d = parse_hook_decision('{"decision":"deny","reason":"no secrets"}', 0)
        assert d.action == "deny" and d.blocks is True and "no secrets" in d.reason

    def test_block_from_json(self) -> None:
        assert (
            parse_hook_decision('{"decision":"block","reason":"tests"}', 0).action
            == "block"
        )

    def test_allow_from_empty(self) -> None:
        assert parse_hook_decision("", 0).blocks is False

    def test_allow_from_plain_json(self) -> None:
        assert parse_hook_decision('{"foo":1}', 0).blocks is False

    def test_exit_code_2_blocks(self) -> None:
        assert parse_hook_decision("", 2).action == "deny"

    def test_garbage_stdout_fails_open(self) -> None:
        assert parse_hook_decision("not json at all", 0).blocks is False

    def test_continue_false_blocks(self) -> None:
        assert (
            parse_hook_decision('{"continue":false,"stopReason":"halt"}', 0).action
            == "block"
        )


class TestLoadVendorHooks:
    def test_claude_settings_normalized(self, tmp_path: Path) -> None:
        settings = tmp_path / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text(
            json.dumps(
                {
                    "hooks": {
                        "PreToolUse": [
                            {
                                "matcher": "Bash",
                                "hooks": [{"type": "command", "command": "echo hi"}],
                            }
                        ]
                    }
                }
            ),
            encoding="utf-8",
        )
        hooks = load_vendor_hooks(tmp_path)
        assert len(hooks) == 1
        assert hooks[0]["events"] == ["PreToolUse"]
        assert hooks[0]["matcher"] == "execute"  # Bash aliased
        assert hooks[0]["command"][-1] == "echo hi"

    def test_missing_files_empty(self, tmp_path: Path) -> None:
        assert load_vendor_hooks(tmp_path) == []

    def test_malformed_json_skipped(self, tmp_path: Path) -> None:
        s = tmp_path / ".claude" / "settings.json"
        s.parent.mkdir(parents=True)
        s.write_text("{ not json", encoding="utf-8")
        assert load_vendor_hooks(tmp_path) == []


class TestEvaluateDecisionHooks:
    def _deny_hook(self, tmp_path: Path) -> list[str]:
        script = tmp_path / "deny.py"
        script.write_text(
            'print(\'{"decision":"deny","reason":"blocked by policy"}\')',
            encoding="utf-8",
        )
        return [PY, str(script)]

    def _allow_hook(self, tmp_path: Path) -> list[str]:
        script = tmp_path / "allow.py"
        script.write_text("print('ok')", encoding="utf-8")
        return [PY, str(script)]

    def test_deny_hook_blocks(self, tmp_path: Path) -> None:
        hooks = [
            {
                "command": self._deny_hook(tmp_path),
                "events": ["PreToolUse"],
                "matcher": "execute",
            }
        ]
        d = evaluate_decision_hooks(
            "PreToolUse", {"command": "rm -rf /"}, hooks, tool_name="execute"
        )
        assert d.blocks is True
        assert "policy" in d.reason

    def test_matcher_filters_by_tool(self, tmp_path: Path) -> None:
        hooks = [
            {
                "command": self._deny_hook(tmp_path),
                "events": ["PreToolUse"],
                "matcher": "execute",
            }
        ]
        # A different tool → matcher doesn't match → allowed.
        d = evaluate_decision_hooks("PreToolUse", {}, hooks, tool_name="read_file")
        assert d.blocks is False

    def test_allow_hook_permits(self, tmp_path: Path) -> None:
        hooks = [
            {
                "command": self._allow_hook(tmp_path),
                "events": ["PreToolUse"],
                "matcher": "execute",
            }
        ]
        assert (
            evaluate_decision_hooks("PreToolUse", {}, hooks, tool_name="execute").blocks
            is False
        )

    def test_missing_hook_command_fails_open(self) -> None:
        # A command whose binary can't be found → FileNotFoundError → fail-open.
        hooks = [
            {
                "command": ["definitely-not-a-real-binary-xyz123"],
                "events": ["PreToolUse"],
            }
        ]
        assert evaluate_decision_hooks("PreToolUse", {}, hooks).blocks is False

    def test_event_filter(self, tmp_path: Path) -> None:
        hooks = [{"command": self._deny_hook(tmp_path), "events": ["Stop"]}]
        # Event doesn't match → not run.
        assert evaluate_decision_hooks("PreToolUse", {}, hooks).blocks is False


def test_canonical_events_include_grok_set() -> None:
    for name in (
        "SessionStart",
        "PreToolUse",
        "PostToolUse",
        "Stop",
        "PreCompact",
        "SessionEnd",
    ):
        assert name in CANONICAL_EVENTS


def test_hook_decision_default_allows() -> None:
    assert HookDecision().blocks is False


class TestHookTypes:
    def test_canonical_events_typed(self) -> None:
        assert hook_type_for_event("PreToolUse") is HookType.MODIFY
        assert hook_type_for_event("UserPromptSubmit") is HookType.MODIFY
        assert hook_type_for_event("Stop") is HookType.GATE
        assert hook_type_for_event("SubagentStop") is HookType.GATE
        assert hook_type_for_event("PostToolUse") is HookType.OBSERVE
        assert hook_type_for_event("Notification") is HookType.OBSERVE

    def test_dotted_events_typed(self) -> None:
        assert hook_type_for_event("shell.pre_execute") is HookType.GATE
        assert hook_type_for_event("file.pre_write") is HookType.GATE
        assert hook_type_for_event("file.pre_edit") is HookType.GATE
        assert hook_type_for_event("model.pre_call") is HookType.GATE
        assert hook_type_for_event("tool.pre_call") is HookType.MODIFY
        assert hook_type_for_event("tool.post_call") is HookType.OBSERVE
        assert hook_type_for_event("file.post_write") is HookType.OBSERVE
        assert hook_type_for_event("shell.post_execute") is HookType.OBSERVE

    def test_new_canonical_events_present(self) -> None:
        for name in ("FileWrite", "FileEdit", "ShellExecute", "ModelCall"):
            assert name in CANONICAL_EVENTS
            assert hook_type_for_event(name) is HookType.GATE

    def test_per_tool_event_normalized_to_family(self) -> None:
        assert hook_type_for_event("tool.pre_call.execute") is HookType.MODIFY
        assert hook_type_for_event("tool.post_call.read_file") is HookType.OBSERVE

    def test_unknown_event_defaults_to_observe(self) -> None:
        assert hook_type_for_event("totally.new.event") is HookType.OBSERVE

    def test_derived_sets_are_exhaustive_and_disjoint(self) -> None:
        assert set(HOOK_TYPES) == GATE_EVENTS | MODIFY_EVENTS | OBSERVE_EVENTS
        assert GATE_EVENTS.isdisjoint(OBSERVE_EVENTS)
        assert GATE_EVENTS.isdisjoint(MODIFY_EVENTS)
        assert MODIFY_EVENTS.isdisjoint(OBSERVE_EVENTS)
        assert GATING_EVENTS == GATE_EVENTS | MODIFY_EVENTS
        assert "PreToolUse" in MODIFY_EVENTS
        assert "Stop" in GATE_EVENTS
        assert "Notification" in OBSERVE_EVENTS
        assert "shell.pre_execute" in GATE_EVENTS


class TestMatcherWildcardAndRegex:
    """Claude/Cursor matchers are regexes; `*` and alternations must fire (T1-3).

    Exact-string equality silently dropped `"*"` (all tools) and `"Edit|Write"`,
    so a migrated deny hook loaded but enforced nothing — the gate failed open.
    """

    def _hook(self, matcher: str) -> list[dict]:
        return [
            {
                "command": ["python", "-c", "import sys;sys.exit(2)"],
                "events": ["PreToolUse"],
                "matcher": matcher,
            }
        ]

    def _action(self, matcher: str, tool: str) -> str:
        return evaluate_decision_hooks(
            "PreToolUse", {"tool": tool}, self._hook(matcher), tool_name=tool
        ).action

    def test_wildcard_matches_every_tool(self) -> None:
        assert self._action("*", "execute") == "deny"
        assert self._action("*", "edit_file") == "deny"
        assert self._action(".*", "read_file") == "deny"

    def test_alternation_matches_each_alias(self) -> None:
        assert self._action("Edit|Write", "edit_file") == "deny"
        assert self._action("Edit|Write", "write_file") == "deny"

    def test_alternation_does_not_overmatch(self) -> None:
        # `Edit|Write` must NOT fire on execute.
        assert self._action("Edit|Write", "execute") == "allow"

    def test_exact_and_aliased_still_work(self) -> None:
        assert self._action("execute", "execute") == "deny"
        assert self._action("Bash", "execute") == "deny"  # Bash aliases to execute
        assert self._action("execute", "read_file") == "allow"

    def test_empty_matcher_fires_for_all(self) -> None:
        assert self._action("", "execute") == "deny"

    def test_regex_pattern_matches(self) -> None:
        assert self._action("write_.*", "write_file") == "deny"
        assert self._action("write_.*", "read_file") == "allow"
