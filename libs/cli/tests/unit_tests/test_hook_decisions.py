"""Tests for decision-capable hooks + Claude/Cursor compat (hook-bus completion)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from bog_agents_cli.hook_decisions import (
    CANONICAL_EVENTS,
    HookDecision,
    alias_tool_name,
    evaluate_decision_hooks,
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
