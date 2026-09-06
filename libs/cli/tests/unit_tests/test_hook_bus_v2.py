"""ROADMAP #64: hook bus v2 — result replacement, new events, on_failure, hash pins, plugin hooks, prompt hooks."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from langchain.tools.tool_node import ToolCallRequest
from langchain_core.messages import ToolMessage

from bog_agents_cli import hook_decisions as hd
from bog_agents_cli.hook_middleware import PreToolUseHookMiddleware
from bog_agents_cli.prompt_hooks import evaluate_prompt_hooks, is_prompt_hook

if TYPE_CHECKING:
    import pytest

PY = sys.executable


def _req(name: str = "execute", **args: Any) -> ToolCallRequest:
    return ToolCallRequest(
        tool_call={"name": name, "args": args or {"command": "ls"}, "id": "t1"},
        tool=None,
        state={},
        runtime=None,  # type: ignore[arg-type]
    )


def _script(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def _hook(
    script: Path, *, events: list[str], matcher: str = "*", **extra: Any
) -> dict[str, Any]:
    return {"command": [PY, str(script)], "events": events, "matcher": matcher, **extra}


def _ok_handler(_request: ToolCallRequest) -> ToolMessage:
    return ToolMessage(content="secret output", tool_call_id="t1", name="execute")


class TestEvents:
    def test_new_events_and_types(self) -> None:
        for name in (
            "PermissionRequest",
            "Interrupt",
            "PreModelSwitch",
            "PostModelSwitch",
        ):
            assert name in hd.CANONICAL_EVENTS
        assert hd.hook_type_for_event("PermissionRequest") is hd.HookType.GATE
        assert hd.hook_type_for_event("PreModelSwitch") is hd.HookType.GATE
        assert hd.hook_type_for_event("Interrupt") is hd.HookType.OBSERVE
        assert hd.hook_type_for_event("PostModelSwitch") is hd.HookType.OBSERVE
        assert hd.hook_type_for_event("PostToolUse") is hd.HookType.MODIFY
        assert {
            "PreToolUse",
            "PostToolUse",
            "PermissionRequest",
            "PreModelSwitch",
        } <= hd.DENY_EVENTS


class TestPostToolUse:
    def test_tool_result_replaces_the_message(self, tmp_path: Path) -> None:
        script = _script(
            tmp_path, "redact.py", 'print(\'{"tool_result": "REDACTED"}\')'
        )
        mw = PreToolUseHookMiddleware(
            [], post_hooks=[_hook(script, events=["PostToolUse"], matcher="execute")]
        )
        result = mw.wrap_tool_call(_req(), _ok_handler)
        assert isinstance(result, ToolMessage)
        assert result.content == "REDACTED" and result.status != "error"

    def test_block_marks_the_result_as_error(self, tmp_path: Path) -> None:
        script = _script(
            tmp_path,
            "block.py",
            'print(\'{"decision": "block", "reason": "leaks a key"}\')',
        )
        mw = PreToolUseHookMiddleware(
            [], post_hooks=[_hook(script, events=["PostToolUse"])]
        )
        result = mw.wrap_tool_call(_req(), _ok_handler)
        assert isinstance(result, ToolMessage)
        assert (
            result.status == "error"
            and "PostToolUse hook" in result.content
            and "leaks a key" in result.content
        )

    def test_async_path_replaces_too(self, tmp_path: Path) -> None:
        script = _script(
            tmp_path, "redact.py", 'print(\'{"tool_result": "REDACTED"}\')'
        )
        mw = PreToolUseHookMiddleware(
            [], post_hooks=[_hook(script, events=["PostToolUse"])]
        )

        async def handler(_request: ToolCallRequest) -> ToolMessage:
            return _ok_handler(_request)

        result = asyncio.run(mw.awrap_tool_call(_req(), handler))
        assert isinstance(result, ToolMessage) and result.content == "REDACTED"

    def test_parse_tool_result(self) -> None:
        decision = hd.parse_hook_decision(
            '{"tool_result": "x", "reason": "trimmed"}', 0
        )
        assert (
            decision.tool_result == "x"
            and not decision.blocks
            and decision.reason == "trimmed"
        )


class TestOnFailure:
    def _crashing(self, **extra: Any) -> dict[str, Any]:
        return {
            "command": ["definitely-not-a-binary-xyz-123"],
            "events": ["PreToolUse"],
            "matcher": "*",
            **extra,
        }

    def test_default_is_fail_open(self) -> None:
        decision = hd.evaluate_decision_hooks(
            "PreToolUse", {"tool": "execute"}, [self._crashing()], tool_name="execute"
        )
        assert not decision.blocks and not decision.asks

    def test_deny_and_ask(self) -> None:
        deny = hd.evaluate_decision_hooks(
            "PreToolUse",
            {"tool": "execute"},
            [self._crashing(on_failure="deny")],
            tool_name="execute",
        )
        assert deny.blocks and "on_failure=deny" in deny.reason and deny.failed
        ask = hd.evaluate_decision_hooks(
            "PreToolUse",
            {"tool": "execute"},
            [self._crashing(on_failure="ask")],
            tool_name="execute",
        )
        assert ask.asks and not ask.blocks

    def test_middleware_honours_deny_but_lets_ask_through(self) -> None:
        called: list[str] = []

        def handler(_request: ToolCallRequest) -> ToolMessage:
            called.append("ran")
            return _ok_handler(_request)

        denied = PreToolUseHookMiddleware(
            [self._crashing(on_failure="deny")]
        ).wrap_tool_call(_req(), handler)
        assert (
            isinstance(denied, ToolMessage)
            and denied.status == "error"
            and called == []
        )
        allowed = PreToolUseHookMiddleware(
            [self._crashing(on_failure="ask")]
        ).wrap_tool_call(_req(), handler)
        assert (
            isinstance(allowed, ToolMessage)
            and allowed.content == "secret output"
            and called == ["ran"]
        )


class TestHashPin:
    def test_pinned_script_change_is_refused(self, tmp_path: Path) -> None:
        script = _script(tmp_path, "allow.py", 'print(\'{"decision": "allow"}\')')
        hook = hd.pin_hook_hashes([_hook(script, events=["PreToolUse"])])[0]
        assert hook["sha256"].startswith("sha256:")
        assert not hd.evaluate_decision_hooks(
            "PreToolUse", {"tool": "execute"}, [hook], tool_name="execute"
        ).blocks
        script.write_text(
            'print(\'{"decision": "allow"}\')  # tampered', encoding="utf-8"
        )
        refused = hd.evaluate_decision_hooks(
            "PreToolUse", {"tool": "execute"}, [hook], tool_name="execute"
        )
        assert refused.blocks and "changed since it was trusted" in refused.reason
        assert hd.script_sha256([PY, str(tmp_path / "missing.py")]) is None
        assert (
            hd.pin_hook_hashes(
                [{"command": [PY, "-c", "print(1)"], "events": ["PreToolUse"]}]
            )[0].get("sha256")
            is None
        )


class TestModelSwitch:
    def test_pre_model_switch_deny(self, tmp_path: Path) -> None:
        script = _script(
            tmp_path,
            "gate.py",
            "import json, sys\n"
            "d = json.load(sys.stdin)\n"
            "print(json.dumps({'decision': 'deny', 'reason': 'opus is not approved'}) if 'opus' in d.get('to', '') else '{}')\n",
        )
        hook = _hook(script, events=["PreModelSwitch"])
        assert (
            hd.model_switch_refusal(
                tmp_path, "a", "anthropic:claude-opus-4-6", config_hooks=[hook]
            )
            == "opus is not approved"
        )
        assert (
            hd.model_switch_refusal(
                tmp_path, "a", "anthropic:claude-haiku-4-5", config_hooks=[hook]
            )
            is None
        )
        assert (
            hd.model_switch_refusal(
                tmp_path, "a", "anthropic:claude-opus-4-6", config_hooks=[]
            )
            is None
        )


class TestApprovalVerdicts:
    def test_permission_request_denies_and_ask_forces_prompt(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        deny_script = _script(
            tmp_path,
            "perm.py",
            'print(\'{"decision": "deny", "reason": "not during freeze"}\')',
        )
        hooks = [
            _hook(deny_script, events=["PermissionRequest"], matcher="execute"),
            {
                "command": ["definitely-not-a-binary-xyz-123"],
                "events": ["PreToolUse"],
                "matcher": "read_file",
                "on_failure": "ask",
            },
        ]
        monkeypatch.setattr("bog_agents_cli.hooks._load_hooks", lambda: hooks)
        denied, force_prompt = hd.approval_hook_verdicts(
            [
                {"name": "execute", "args": {"command": "make deploy"}},
                {"name": "read_file", "args": {"path": "x"}},
            ],
            tmp_path,
        )
        assert denied == {0: "not during freeze"} and force_prompt is True
        monkeypatch.setattr("bog_agents_cli.hooks._load_hooks", list)
        assert hd.approval_hook_verdicts(
            [{"name": "execute", "args": {}}], tmp_path
        ) == ({}, False)


class TestPluginHooks:
    def test_trusted_plugin_hooks_are_pinned_and_refused_after_change(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from bog_agents_cli import plugin_spec

        root = tmp_path / "plugins" / "guard"
        (root / "hooks").mkdir(parents=True)
        deny = root / "hooks" / "deny.py"
        deny.write_text(
            'print(\'{"decision": "deny", "reason": "plugin says no"}\')',
            encoding="utf-8",
        )
        (root / "hooks" / "hooks.json").write_text(
            json.dumps(
                {
                    "hooks": [
                        {
                            "command": [PY, "hooks/deny.py"],
                            "events": ["PreToolUse"],
                            "matcher": "execute",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        cfg = tmp_path / "cfg"
        cfg.mkdir()
        monkeypatch.setattr(
            plugin_spec, "plugin_roots", lambda **_kw: [(root, "workspace")]
        )

        assert hd.load_plugin_hooks(cfg, project_root=tmp_path) == []  # not trusted yet
        plugin_spec.trust_plugin(root, config_dir=cfg)
        store = json.loads((cfg / "plugin_trust.json").read_text(encoding="utf-8"))
        assert any(
            v.startswith("sha256:")
            for v in next(iter(store["trusted"].values()))["hook_hashes"].values()
        )

        loaded = hd.load_plugin_hooks(cfg, project_root=tmp_path)
        assert len(loaded) == 1 and loaded[0]["sha256"].startswith("sha256:")
        assert Path(loaded[0]["command"][1]).is_absolute()
        verdict = hd.evaluate_decision_hooks(
            "PreToolUse", {"tool": "execute"}, loaded, tool_name="execute"
        )
        assert verdict.blocks and "plugin says no" in verdict.reason

        deny.write_text('print(\'{"decision": "allow"}\')', encoding="utf-8")
        assert (
            hd.load_plugin_hooks(cfg, project_root=tmp_path) == []
        )  # hash changed: ignored until re-trusted
        plugin_spec.trust_plugin(root, config_dir=cfg)
        assert len(hd.load_plugin_hooks(cfg, project_root=tmp_path)) == 1


class TestPromptHooks:
    HOOK: ClassVar[dict[str, Any]] = {
        "type": "prompt",
        "prompt": "Deny any shell command that deletes files",
        "events": ["PreToolUse"],
        "matcher": "execute",
    }

    def test_judge_verdicts_and_fail_closed(self) -> None:
        assert is_prompt_hook(self.HOOK) and not is_prompt_hook({"command": ["x"]})
        payload = {"tool": "execute", "args": {"command": "rm -rf build"}}
        deny = evaluate_prompt_hooks(
            "PreToolUse",
            payload,
            [self.HOOK],
            invoke=lambda _s, _u: '{"decision": "deny", "reason": "deletes files"}',
            tool_name="execute",
        )
        assert deny.blocks and deny.reason == "deletes files"
        allow = evaluate_prompt_hooks(
            "PreToolUse",
            payload,
            [self.HOOK],
            invoke=lambda _s, _u: 'Sure: {"decision": "allow"}',
            tool_name="execute",
        )
        assert not allow.blocks

        def boom(_s: str, _u: str) -> str:
            raise TimeoutError("judge down")

        assert evaluate_prompt_hooks(
            "PreToolUse", payload, [self.HOOK], invoke=boom, tool_name="execute"
        ).blocks
        assert evaluate_prompt_hooks(
            "PreToolUse",
            payload,
            [self.HOOK],
            invoke=lambda _s, _u: "maybe?",
            tool_name="execute",
        ).blocks
        assert evaluate_prompt_hooks(
            "PreToolUse", payload, [self.HOOK], invoke=None, tool_name="execute"
        ).blocks
        calls: list[str] = []
        untouched = evaluate_prompt_hooks(
            "PreToolUse",
            payload,
            [self.HOOK],
            invoke=lambda _s, u: calls.append(u) or "{}",
            tool_name="read_file",
        )
        assert not untouched.blocks and calls == []

    def test_middleware_runs_prompt_hooks(self) -> None:
        called: list[str] = []

        def handler(_request: ToolCallRequest) -> ToolMessage:
            called.append("ran")
            return _ok_handler(_request)

        mw = PreToolUseHookMiddleware(
            [],
            prompt_hooks=[self.HOOK],
            prompt_invoke=lambda _s, _u: '{"decision": "deny", "reason": "no"}',
        )
        blocked = mw.wrap_tool_call(_req(), handler)
        assert (
            isinstance(blocked, ToolMessage)
            and blocked.status == "error"
            and called == []
        )
        mw = PreToolUseHookMiddleware(
            [],
            prompt_hooks=[self.HOOK],
            prompt_invoke=lambda _s, _u: '{"decision": "allow"}',
        )
        assert mw.wrap_tool_call(_req(), handler).content == "secret output"

    def test_load_decision_hooks_keeps_prompt_entries(self, tmp_path: Path) -> None:
        hooks = hd.load_decision_hooks(
            tmp_path,
            config_hooks=[self.HOOK, {"command": ["x"], "events": ["PostToolUse"]}],
            event="PreToolUse",
        )
        assert hooks == [self.HOOK]
