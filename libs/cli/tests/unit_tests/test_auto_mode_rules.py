"""Unit tests for bog_agents_cli.auto_mode — rule engine, settings, and utilities."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bog_agents_cli.auto_mode import (
    AutoDecision,
    AutoModeRuleEngine,
    AutoModeSettings,
    HaikuEvalConfig,
    RuleVerdict,
    _apply_settings_file,
    detect_ambiguities,
    haiku_risk_eval,
    load_auto_mode_settings,
)

# ---------------------------------------------------------------------------
# AutoModeRuleEngine — safe tools
# ---------------------------------------------------------------------------


class TestRuleEngineSafeTools:
    def _engine(self) -> AutoModeRuleEngine:
        return AutoModeRuleEngine(AutoModeSettings())

    @pytest.mark.parametrize(
        "tool_name",
        [
            "read_file",
            "read_many_files",
            "glob",
            "grep",
            "list_directory",
            "get_file_info",
            "search_files",
            "git_status",
            "git_log",
            "git_diff",
            "git_show",
        ],
    )
    def test_safe_tools_are_allowed(self, tool_name: str) -> None:
        engine = self._engine()
        verdict = engine.evaluate(tool_name, {})
        assert verdict.decision == AutoDecision.ALLOW
        assert verdict.rule_source == "safe_tools"

    def test_write_file_is_allowed_by_default(self) -> None:
        engine = self._engine()
        verdict = engine.evaluate("write_file", {"path": "/tmp/out.txt"})
        assert verdict.decision == AutoDecision.ALLOW

    def test_edit_file_is_allowed_by_default(self) -> None:
        engine = self._engine()
        verdict = engine.evaluate("edit_file", {"path": "src/main.py"})
        assert verdict.decision == AutoDecision.ALLOW

    def test_unknown_tool_defaults_to_allow(self) -> None:
        engine = self._engine()
        verdict = engine.evaluate("some_custom_tool", {"x": 1})
        assert verdict.decision == AutoDecision.ALLOW
        assert verdict.rule_source == "default"


# ---------------------------------------------------------------------------
# AutoModeRuleEngine — risky tools
# ---------------------------------------------------------------------------


class TestRuleEngineRiskyTools:
    def _engine(self) -> AutoModeRuleEngine:
        return AutoModeRuleEngine(AutoModeSettings())

    @pytest.mark.parametrize(
        "tool_name",
        [
            "delete_file",
            "remove_directory",
            "git_push",
            "git_reset",
        ],
    )
    def test_risky_tools_ask(self, tool_name: str) -> None:
        engine = self._engine()
        verdict = engine.evaluate(tool_name, {})
        assert verdict.decision == AutoDecision.ASK
        assert verdict.rule_source == "risky_tools"


# ---------------------------------------------------------------------------
# AutoModeRuleEngine — shell ask patterns (destructive operations)
# ---------------------------------------------------------------------------


class TestShellAskPatterns:
    def _engine(self) -> AutoModeRuleEngine:
        return AutoModeRuleEngine(AutoModeSettings())

    def _eval(self, cmd: str) -> RuleVerdict:
        return self._engine().evaluate("bash", {"command": cmd})

    @pytest.mark.parametrize(
        "cmd",
        [
            "rm -rf /tmp/dir",
            "rm file.txt",
            "rm",
            "rmdir old_folder",
            "del file.txt",
            "rd /s /q folder",
        ],
    )
    def test_file_deletion_triggers_ask(self, cmd: str) -> None:
        v = self._eval(cmd)
        assert v.decision == AutoDecision.ASK, f"Expected ASK for: {cmd!r}"

    @pytest.mark.parametrize(
        "cmd",
        [
            "git push --force origin main",
            "git push -f",
            "git reset --hard HEAD~1",
            "git clean -fd",
            "git checkout .",
            "git rebase main",
        ],
    )
    def test_git_destructive_triggers_ask(self, cmd: str) -> None:
        v = self._eval(cmd)
        assert v.decision == AutoDecision.ASK, f"Expected ASK for: {cmd!r}"

    @pytest.mark.parametrize(
        "cmd",
        [
            "DROP TABLE users",
            "TRUNCATE logs",
            "dropdb mydb",
        ],
    )
    def test_database_destructive_triggers_ask(self, cmd: str) -> None:
        v = self._eval(cmd)
        assert v.decision == AutoDecision.ASK, f"Expected ASK for: {cmd!r}"

    @pytest.mark.parametrize(
        "cmd",
        [
            'curl -X POST https://api.example.com/data -d "{}"',
            "curl --data payload.json https://api.example.com",
            "wget --post-data=foo http://example.com",
        ],
    )
    def test_network_writes_trigger_ask(self, cmd: str) -> None:
        v = self._eval(cmd)
        assert v.decision == AutoDecision.ASK, f"Expected ASK for: {cmd!r}"

    @pytest.mark.parametrize(
        "cmd",
        [
            "kill 1234",
            "killall python",
            "pkill -f myapp",
        ],
    )
    def test_process_kill_triggers_ask(self, cmd: str) -> None:
        v = self._eval(cmd)
        assert v.decision == AutoDecision.ASK, f"Expected ASK for: {cmd!r}"

    @pytest.mark.parametrize(
        "cmd",
        [
            "docker rm my_container",
            "docker rmi my_image",
            "docker prune",
            "docker system prune",
            "podman rm my_container",
        ],
    )
    def test_container_cleanup_triggers_ask(self, cmd: str) -> None:
        v = self._eval(cmd)
        assert v.decision == AutoDecision.ASK, f"Expected ASK for: {cmd!r}"

    @pytest.mark.parametrize(
        "cmd",
        [
            "dd if=/dev/zero of=/dev/sda",
            "dd bs=512 count=1 if=/dev/urandom of=/dev/sdb",
        ],
    )
    def test_raw_disk_write_triggers_ask(self, cmd: str) -> None:
        v = self._eval(cmd)
        assert v.decision == AutoDecision.ASK, f"Expected ASK for: {cmd!r}"

    @pytest.mark.parametrize(
        "cmd",
        [
            "aws s3 rm s3://my-bucket/key",
            "gsutil rm -r gs://my-bucket/",
        ],
    )
    def test_cloud_deletion_triggers_ask(self, cmd: str) -> None:
        v = self._eval(cmd)
        assert v.decision == AutoDecision.ASK, f"Expected ASK for: {cmd!r}"

    @pytest.mark.parametrize(
        "cmd",
        [
            "pip uninstall requests",
            "npm uninstall lodash",
            "uv remove requests",
        ],
    )
    def test_package_removal_triggers_ask(self, cmd: str) -> None:
        v = self._eval(cmd)
        assert v.decision == AutoDecision.ASK, f"Expected ASK for: {cmd!r}"

    def test_overwrite_redirect_triggers_ask(self) -> None:
        v = self._eval("echo hello > file.txt")
        assert v.decision == AutoDecision.ASK

    def test_append_redirect_does_not_trigger_ask(self) -> None:
        v = self._eval("echo hello >> file.txt")
        # >> is append (not an overwrite redirect) — should NOT match the > pattern
        assert v.decision != AutoDecision.ASK or v.rule_source != "ask_list"


# ---------------------------------------------------------------------------
# AutoModeRuleEngine — shell allow patterns (safe read-only ops)
# ---------------------------------------------------------------------------


class TestShellAllowPatterns:
    def _engine(self) -> AutoModeRuleEngine:
        return AutoModeRuleEngine(AutoModeSettings())

    def _eval(self, cmd: str) -> RuleVerdict:
        return self._engine().evaluate("bash", {"command": cmd})

    @pytest.mark.parametrize(
        "cmd",
        [
            "cat README.md",
            "head -20 file.py",
            "tail -f app.log",
            "grep -rn TODO src/",
            "rg 'def foo' .",
            "find . -name '*.py'",
            "ls -la",
            "dir",
            "wc -l src/*.py",
            "diff file_a.py file_b.py",
        ],
    )
    def test_file_reading_is_allowed(self, cmd: str) -> None:
        v = self._eval(cmd)
        assert v.decision == AutoDecision.ALLOW, f"Expected ALLOW for: {cmd!r}"
        assert v.rule_source == "allow_list"

    @pytest.mark.parametrize(
        "cmd",
        [
            "git status",
            "git log --oneline -10",
            "git diff HEAD",
            "git show abc123",
            "git branch -a",
        ],
    )
    def test_git_read_is_allowed(self, cmd: str) -> None:
        v = self._eval(cmd)
        assert v.decision == AutoDecision.ALLOW, f"Expected ALLOW for: {cmd!r}"

    @pytest.mark.parametrize(
        "cmd",
        [
            "npm test",
            "npm run test",
            "npm run typecheck",
            "pytest",
            "python -m pytest",
            "uv run pytest",
            "cargo test",
            "go test ./...",
            "vitest",
            "jest",
        ],
    )
    def test_test_runners_are_allowed(self, cmd: str) -> None:
        v = self._eval(cmd)
        assert v.decision == AutoDecision.ALLOW, f"Expected ALLOW for: {cmd!r}"

    @pytest.mark.parametrize(
        "cmd",
        [
            "tsc --noEmit",
            "mypy src/",
            "ruff check .",
            "ruff format --check .",
            "ty check bog_agents",
            "pyright",
        ],
    )
    def test_type_checkers_are_allowed(self, cmd: str) -> None:
        v = self._eval(cmd)
        assert v.decision == AutoDecision.ALLOW, f"Expected ALLOW for: {cmd!r}"


# ---------------------------------------------------------------------------
# AutoModeRuleEngine — tool name variants
# ---------------------------------------------------------------------------


class TestRuleEngineToolVariants:
    def _engine(self) -> AutoModeRuleEngine:
        return AutoModeRuleEngine(AutoModeSettings())

    @pytest.mark.parametrize("tool_name", ["execute", "run_command", "shell", "bash"])
    def test_shell_tools_route_to_pattern_matching(self, tool_name: str) -> None:
        engine = self._engine()
        verdict = engine.evaluate(tool_name, {"command": "git status"})
        assert verdict.decision == AutoDecision.ALLOW

    @pytest.mark.parametrize("tool_name", ["execute", "run_command", "shell", "bash"])
    def test_shell_tools_ask_on_destructive(self, tool_name: str) -> None:
        engine = self._engine()
        verdict = engine.evaluate(tool_name, {"command": "rm -rf /"})
        assert verdict.decision == AutoDecision.ASK

    def test_cmd_key_also_works(self) -> None:
        engine = self._engine()
        verdict = engine.evaluate("bash", {"cmd": "git status"})
        assert verdict.decision == AutoDecision.ALLOW


# ---------------------------------------------------------------------------
# AutoModeSettings — settings cascade and merge
# ---------------------------------------------------------------------------


class TestAutoModeSettings:
    def test_defaults_are_sensible(self) -> None:
        s = AutoModeSettings()
        assert s.enabled is False
        assert s.preflight_clarification is True
        assert s.haiku_eval.enabled is True
        assert s.extra_shell_ask_patterns == []
        assert s.extra_shell_allow_patterns == []

    def test_merge_dict_overrides_enabled(self) -> None:
        s = AutoModeSettings()
        merged = s.merge_dict({"enabled": True})
        assert merged.enabled is True

    def test_merge_dict_adds_extra_patterns(self) -> None:
        s = AutoModeSettings()
        merged = s.merge_dict({"shell_ask_patterns": [r"\bmy_custom_cmd\b"]})
        assert r"\bmy_custom_cmd\b" in merged.extra_shell_ask_patterns

    def test_merge_dict_invalid_list_type_falls_back(self) -> None:
        s = AutoModeSettings()
        # String instead of list — should fall back to existing value with warning
        merged = s.merge_dict({"shell_ask_patterns": "not-a-list"})
        assert merged.extra_shell_ask_patterns == []

    def test_merge_dict_invalid_list_type_safe_tools(self) -> None:
        s = AutoModeSettings()
        merged = s.merge_dict({"safe_tools": 42})
        assert merged.extra_safe_tools == []

    def test_haiku_config_merged(self) -> None:
        s = AutoModeSettings()
        merged = s.merge_dict({"haiku_eval": {"enabled": False}})
        assert merged.haiku_eval.enabled is False

    def test_extra_patterns_used_in_engine(self) -> None:
        s = AutoModeSettings(extra_shell_ask_patterns=[r"\bmy_dangerous_cmd\b"])
        engine = AutoModeRuleEngine(s)
        verdict = engine.evaluate("bash", {"command": "my_dangerous_cmd --go"})
        assert verdict.decision == AutoDecision.ASK

    def test_extra_safe_tools_used_in_engine(self) -> None:
        s = AutoModeSettings(extra_safe_tools=["custom_reader"])
        engine = AutoModeRuleEngine(s)
        verdict = engine.evaluate("custom_reader", {})
        assert verdict.decision == AutoDecision.ALLOW
        assert verdict.rule_source == "safe_tools"


# ---------------------------------------------------------------------------
# _apply_settings_file — file loading and error handling
# ---------------------------------------------------------------------------


class TestApplySettingsFile:
    def test_missing_file_returns_base(self, tmp_path: Path) -> None:
        base = AutoModeSettings()
        result = _apply_settings_file(base, tmp_path / "nonexistent.json")
        assert result is base

    def test_valid_settings_file_applied(self, tmp_path: Path) -> None:
        settings_file = tmp_path / "settings.json"
        settings_file.write_text(json.dumps({"auto_mode": {"enabled": True}}))
        base = AutoModeSettings()
        result = _apply_settings_file(base, settings_file)
        assert result.enabled is True

    def test_malformed_json_returns_base(self, tmp_path: Path) -> None:
        settings_file = tmp_path / "settings.json"
        settings_file.write_text("{bad json}")
        base = AutoModeSettings()
        result = _apply_settings_file(base, settings_file)
        assert result is base

    def test_oversized_file_is_skipped(self, tmp_path: Path) -> None:
        settings_file = tmp_path / "settings.json"
        # Write just over the 1 MB limit
        settings_file.write_bytes(b"x" * (1024 * 1024 + 1))
        base = AutoModeSettings()
        result = _apply_settings_file(base, settings_file)
        assert result is base

    def test_missing_auto_mode_section_returns_base(self, tmp_path: Path) -> None:
        settings_file = tmp_path / "settings.json"
        settings_file.write_text(json.dumps({"other_section": {"key": "val"}}))
        base = AutoModeSettings()
        result = _apply_settings_file(base, settings_file)
        assert result is base

    def test_empty_auto_mode_section_returns_base(self, tmp_path: Path) -> None:
        settings_file = tmp_path / "settings.json"
        settings_file.write_text(json.dumps({"auto_mode": {}}))
        base = AutoModeSettings()
        result = _apply_settings_file(base, settings_file)
        assert result is base


# ---------------------------------------------------------------------------
# load_auto_mode_settings — cascade
# ---------------------------------------------------------------------------


class TestLoadAutoModeSettings:
    def test_loads_without_project_root(self, tmp_path: Path) -> None:
        with patch("bog_agents_cli.auto_mode.Path.home", return_value=tmp_path):
            settings = load_auto_mode_settings()
        assert isinstance(settings, AutoModeSettings)

    def test_project_root_overrides_user_global(self, tmp_path: Path) -> None:
        user_dir = tmp_path / "user"
        user_dir.mkdir()
        (user_dir / ".bog-agents").mkdir()
        (user_dir / ".bog-agents" / "settings.json").write_text(
            json.dumps({"auto_mode": {"enabled": False}})
        )
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        (project_dir / ".bog-agents").mkdir()
        (project_dir / ".bog-agents" / "settings.json").write_text(
            json.dumps({"auto_mode": {"enabled": True}})
        )
        with patch("bog_agents_cli.auto_mode.Path.home", return_value=user_dir):
            settings = load_auto_mode_settings(project_root=project_dir)
        assert settings.enabled is True


# ---------------------------------------------------------------------------
# Consolidated git classifier (Feature #10) — new coverage beyond ask-list
# ---------------------------------------------------------------------------


class TestGitClassifierWiring:
    def _engine(self) -> AutoModeRuleEngine:
        return AutoModeRuleEngine(AutoModeSettings())

    def _eval(self, cmd: str) -> RuleVerdict:
        return self._engine().evaluate("bash", {"command": cmd})

    @pytest.mark.parametrize(
        "cmd",
        [
            "git push -ff origin main",
            # Forms the ask-list regexes (`-f\b`, `.*--force`) do not match, so
            # without the classifier they reach the ALLOW fallthrough and get
            # auto-approved.
            "git push -uf origin main",
            "git push -qfu origin main",
            "git push origin +main",
            "git push --delete origin feature",
            "git push origin :feature",
            "git branch -D stale",
            "git branch -d stale",
            "git tag -d v1.0",
            "git stash drop",
            "git stash clear",
            "git checkout -- file.py",
            "git checkout -f main",
            "git filter-branch -- --all",
            "git submodule update --force",
        ],
    )
    def test_destructive_git_asks(self, cmd: str) -> None:
        v = self._eval(cmd)
        assert v.decision == AutoDecision.ASK, f"Expected ASK for: {cmd!r}"
        assert v.rule_source == "git_ops", cmd

    def test_ask_list_cases_still_keep_priority(self) -> None:
        for cmd in (
            "git push --force origin main",
            "git reset --hard HEAD~1",
            "git clean -fd",
            "git checkout .",
            "cd /tmp && git clean -fdx",
        ):
            v = self._eval(cmd)
            assert v.decision == AutoDecision.ASK, cmd
            assert v.rule_source == "ask_list", cmd

    @pytest.mark.parametrize(
        "cmd",
        [
            "git ls-tree HEAD",
            "git blame src/main.py",
            "git grep TODO src",
            "git reflog",
            "git stash list",
        ],
    )
    def test_read_only_git_is_allowed(self, cmd: str) -> None:
        v = self._eval(cmd)
        assert v.decision == AutoDecision.ALLOW, f"Expected ALLOW for: {cmd!r}"
        assert v.rule_source == "git_ops", cmd

    def test_mutating_git_still_falls_through(self) -> None:
        v = self._eval("git commit -m 'fix'")
        assert v.decision == AutoDecision.ALLOW
        assert v.rule_source == "default"


# ---------------------------------------------------------------------------
# Bash-hygiene gate (Feature #9) — hang-prone / blocking commands
# ---------------------------------------------------------------------------


class TestBashHygieneWiring:
    def _engine(self) -> AutoModeRuleEngine:
        return AutoModeRuleEngine(AutoModeSettings())

    def _eval(self, cmd: str) -> RuleVerdict:
        return self._engine().evaluate("bash", {"command": cmd})

    @pytest.mark.parametrize(
        "cmd",
        [
            "sleep 3600",
            "while true; do echo hi; done",
            "yes",
            "ping 8.8.8.8",
            "watch df -h",
            "less big_file.txt",
            "read answer",
            "curl https://api.example.com",
            "ssh deploy@prod",
            "git commit",
        ],
    )
    def test_hang_prone_commands_ask(self, cmd: str) -> None:
        v = self._eval(cmd)
        assert v.decision == AutoDecision.ASK, f"Expected ASK for: {cmd!r}"
        assert v.rule_source == "bash_hygiene", cmd

    @pytest.mark.parametrize(
        "cmd",
        [
            "timeout 30 sleep 3600",
            "sleep 5",
            "yes | head -5",
            "git commit -m done",
            "ping -c 4 8.8.8.8",
            "tail -f app.log",  # explicitly allow-listed -> allow_list wins
        ],
    )
    def test_bounded_commands_not_hygiene_asked(self, cmd: str) -> None:
        v = self._eval(cmd)
        assert v.decision != AutoDecision.ASK or v.rule_source != "bash_hygiene", cmd


# ---------------------------------------------------------------------------
# detect_ambiguities — heuristic pattern matching
# ---------------------------------------------------------------------------


class TestDetectAmbiguities:
    def test_vague_scope_flagged(self) -> None:
        qs = detect_ambiguities("refactor everything in the codebase")
        assert len(qs) > 0

    def test_clear_task_not_flagged(self) -> None:
        qs = detect_ambiguities("add a login button to src/components/Header.tsx")
        assert qs == []

    def test_fix_it_flagged(self) -> None:
        qs = detect_ambiguities("please fix it")
        assert any("unclear" in q or "it" in q for q in qs)

    def test_deployment_prompt_flagged(self) -> None:
        qs = detect_ambiguities("deploy this to production")
        assert len(qs) > 0

    def test_destructive_prompt_flagged(self) -> None:
        qs = detect_ambiguities("delete all the old logs")
        assert len(qs) > 0

    def test_no_duplicate_questions(self) -> None:
        # "everything" and "all files" both match the broad-scope pattern
        qs = detect_ambiguities("fix everything and all files now")
        assert len(qs) == len(set(qs))


# ---------------------------------------------------------------------------
# haiku_risk_eval — API integration (mocked)
# ---------------------------------------------------------------------------


class TestHaikuRiskEval:
    async def test_safe_tool_returns_not_risky(self) -> None:
        mock_msg = MagicMock()
        mock_msg.content = [
            MagicMock(text='{"risky": false, "reason": "reading a file"}')
        ]
        with patch("anthropic.AsyncAnthropic") as mock_cls:
            mock_client = AsyncMock()
            mock_cls.return_value = mock_client
            mock_client.messages.create = AsyncMock(return_value=mock_msg)
            is_risky, reason = await haiku_risk_eval("read_file", {"path": "README.md"})
        assert is_risky is False
        assert "reading" in reason.lower()

    async def test_risky_tool_returns_risky(self) -> None:
        mock_msg = MagicMock()
        mock_msg.content = [
            MagicMock(text='{"risky": true, "reason": "deletes files"}')
        ]
        with patch("anthropic.AsyncAnthropic") as mock_cls:
            mock_client = AsyncMock()
            mock_cls.return_value = mock_client
            mock_client.messages.create = AsyncMock(return_value=mock_msg)
            is_risky, _reason = await haiku_risk_eval(
                "delete_file", {"path": "/etc/passwd"}
            )
        assert is_risky is True

    async def test_api_failure_returns_risky(self) -> None:
        with patch("anthropic.AsyncAnthropic") as mock_cls:
            mock_client = AsyncMock()
            mock_cls.return_value = mock_client
            mock_client.messages.create = AsyncMock(
                side_effect=Exception("network error")
            )
            is_risky, reason = await haiku_risk_eval("some_tool", {})
        # Fail-closed: API unavailable should be treated as risky
        assert is_risky is True
        assert "unavailable" in reason.lower() or "risky" in reason.lower()

    async def test_none_args_handled_gracefully(self) -> None:
        mock_msg = MagicMock()
        mock_msg.content = [MagicMock(text='{"risky": false, "reason": "ok"}')]
        with patch("anthropic.AsyncAnthropic") as mock_cls:
            mock_client = AsyncMock()
            mock_cls.return_value = mock_client
            mock_client.messages.create = AsyncMock(return_value=mock_msg)
            # tool_args=None should not raise
            is_risky, _ = await haiku_risk_eval("some_tool", None)  # type: ignore[arg-type]
        assert isinstance(is_risky, bool)

    async def test_anthropic_not_installed_returns_allow(self) -> None:
        import sys

        with patch.dict(sys.modules, {"anthropic": None}):
            is_risky, reason = await haiku_risk_eval("some_tool", {})
        assert is_risky is False
        assert "not available" in reason
