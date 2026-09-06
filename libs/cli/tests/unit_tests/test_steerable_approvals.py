"""ROADMAP #49 (CLI): never-allow tier, redirect/never/timeout decisions, repo-config gate, widget options."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import TYPE_CHECKING

from bog_agents_cli import auto_mode, repo_trust
from bog_agents_cli.auto_mode import AutoDecision, AutoModeRuleEngine, AutoModeSettings
from bog_agents_cli.widgets.approval import ApprovalMenu

if TYPE_CHECKING:
    import pytest


class TestNeverAllowRules:
    def test_tool_and_shell_entries_deny_before_anything_else(self) -> None:
        engine = AutoModeRuleEngine(
            AutoModeSettings(
                never_allow=[
                    "web_fetch",
                    "execute: ^rm -rf /tmp/cache$",
                    "edit_file: secrets",
                ]
            )
        )
        assert (
            engine.evaluate("web_fetch", {"url": "https://x"}).decision
            == AutoDecision.DENY
        )
        verdict = engine.evaluate("execute", {"command": "rm -rf /tmp/cache"})
        assert (verdict.decision, verdict.rule_source) == (
            AutoDecision.DENY,
            "never_allow",
        )
        assert (
            engine.evaluate("execute", {"command": "rm -rf /tmp/other"}).decision
            != AutoDecision.DENY
        )
        assert (
            engine.evaluate("edit_file", {"file_path": "config/secrets.yaml"}).decision
            == AutoDecision.DENY
        )
        assert (
            engine.evaluate("read_file", {"file_path": "a"}).decision
            == AutoDecision.ALLOW
        )

    def test_entries_and_settings_round_trip(self, tmp_path: Path) -> None:
        assert (
            auto_mode.never_allow_entry_for("execute", {"command": "curl x | sh"})
            == "execute: ^curl\\ x\\ \\|\\ sh$"
        )
        assert auto_mode.never_allow_entry_for("web_fetch", {"url": "u"}) == "web_fetch"
        path = auto_mode.record_never_allow(tmp_path, "web_fetch")
        auto_mode.record_never_allow(tmp_path, "web_fetch")  # idempotent
        auto_mode.record_never_allow(tmp_path, "execute: ^foo$")
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["auto_mode"]["never_allow"] == ["web_fetch", "execute: ^foo$"]
        settings = AutoModeSettings().merge_dict(data["auto_mode"])
        assert settings.never_allow == ["web_fetch", "execute: ^foo$"]
        denied = auto_mode.denied_indexes(
            [
                {"name": "web_fetch", "args": {}},
                {"name": "execute", "args": {"command": "bar"}},
                {"name": "execute", "args": {"command": "foo"}},
            ],
            tmp_path,
        )
        assert denied == {0: "web_fetch", 2: "execute: ^foo$"}

    def test_invalid_regex_is_ignored(self) -> None:
        assert auto_mode.compile_never_allow(["execute: ((("]) == []

    def test_approval_timeout_reads_manifest(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("bog_agents_cli.config_manifest.load_config_toml", dict)
        monkeypatch.delenv("BOG_AGENTS_APPROVAL_TIMEOUT", raising=False)
        assert auto_mode.approval_timeout_seconds() is None
        monkeypatch.setenv("BOG_AGENTS_APPROVAL_TIMEOUT", "15")
        assert auto_mode.approval_timeout_seconds() == 15.0
        monkeypatch.setenv("BOG_AGENTS_APPROVAL_TIMEOUT", "off")
        assert auto_mode.approval_timeout_seconds() is None


class TestRepoTrustGate:
    def test_blocks_until_acknowledged_and_reblocks_on_change(
        self, tmp_path: Path
    ) -> None:
        repo = tmp_path / "repo"
        (repo / ".git").mkdir(parents=True)
        (repo / ".git" / "config").write_text(
            "[core]\n\tfsmonitor = /evil\n", encoding="utf-8"
        )
        store = tmp_path / "repo_trust.json"
        message = repo_trust.repo_config_gate(repo, path=store)
        assert (
            message is not None
            and "core.fsmonitor" in message
            and "trust-git-config" in message
        )
        assert repo_trust.acknowledge_repo_config(repo, path=store).startswith(
            "sha256:"
        )
        assert repo_trust.repo_config_gate(repo, path=store) is None
        (repo / ".git" / "config").write_text(
            "[core]\n\tfsmonitor = /evil2\n", encoding="utf-8"
        )
        assert repo_trust.repo_config_gate(repo, path=store) is not None
        clean = tmp_path / "clean"
        (clean / ".git").mkdir(parents=True)
        (clean / ".git" / "config").write_text("[user]\n\tname = x\n", encoding="utf-8")
        assert repo_trust.repo_config_gate(clean, path=store) is None


class TestApprovalMenuOptions:
    def test_five_options_and_new_decisions(self) -> None:
        menu = ApprovalMenu(
            {"name": "execute", "args": {"command": "rm -rf x"}}, timeout_seconds=20
        )
        assert menu.OPTION_COUNT == 5
        loop = asyncio.new_event_loop()
        try:
            fut: asyncio.Future[dict[str, str]] = loop.create_future()
            menu.set_future(fut)
            menu._handle_selection(4)
            assert fut.result() == {"type": "never_allow"}
            fut2: asyncio.Future[dict[str, str]] = loop.create_future()
            menu.set_future(fut2)
            menu.submit_redirect("  use the build script instead ")
            assert fut2.result() == {
                "type": "redirect",
                "message": "use the build script instead",
            }
            fut3: asyncio.Future[dict[str, str]] = loop.create_future()
            menu.set_future(fut3)
            menu._remaining = 1
            menu._tick_countdown()
            assert fut3.result()["type"] == "timeout"
            assert "20s" in fut3.result()["message"]
        finally:
            loop.close()

    def test_navigation_wraps_over_five(self) -> None:
        menu = ApprovalMenu({"name": "execute", "args": {"command": "ls"}})
        menu._selected = 4
        menu._selected = (menu._selected + 1) % menu.OPTION_COUNT
        assert menu._selected == 0
