"""Tests for the unified timeouts settings cascade and env application."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from bog_agents_cli.timeouts import (
    TimeoutSettings,
    apply_to_env,
    load_timeout_settings,
    resolve_tool_timeout,
)

if TYPE_CHECKING:
    import pytest


class TestTimeoutSettingsMerge:
    """Direct merge_dict semantics on a single layer."""

    def test_defaults_match_baseline(self) -> None:
        # Model read stays at 600s — a model HTTP stream sends tokens
        # continuously, so a multi-minute gap is a genuine stall.
        # Remote read is DISABLED by default: a long tool call emits no
        # SSE events for its whole duration, so any finite per-chunk cap
        # kills real work; the liveness watchdog in remote_client.py
        # catches dead connections instead. Tool stays 7200s.
        settings = TimeoutSettings()
        assert settings.model_read_seconds == 600
        assert settings.remote_read_seconds is None
        assert settings.tool_seconds == 7200

    def test_int_override_replaces_default(self) -> None:
        settings = TimeoutSettings().merge_dict({"model_read_seconds": 300})
        assert settings.model_read_seconds == 300
        # other fields unchanged — remote stays disabled by default
        assert settings.remote_read_seconds is None

    def test_zero_disables_timeout(self) -> None:
        settings = TimeoutSettings().merge_dict({"tool_seconds": 0})
        assert settings.tool_seconds is None

    def test_none_string_disables(self) -> None:
        settings = TimeoutSettings().merge_dict({"remote_read_seconds": "none"})
        assert settings.remote_read_seconds is None

    def test_off_string_disables(self) -> None:
        settings = TimeoutSettings().merge_dict({"tool_seconds": "off"})
        assert settings.tool_seconds is None

    def test_string_int_coerced(self) -> None:
        settings = TimeoutSettings().merge_dict({"model_read_seconds": "3600"})
        assert settings.model_read_seconds == 3600

    def test_unknown_key_ignored(self) -> None:
        settings = TimeoutSettings().merge_dict(
            {"model_read_seconds": 300, "future_key": 999}
        )
        assert settings.model_read_seconds == 300

    def test_bool_value_keeps_default(self) -> None:
        # ``"tool_seconds": false`` should not be silently treated as 0.
        settings = TimeoutSettings().merge_dict({"tool_seconds": False})
        assert settings.tool_seconds == 7200

    def test_garbage_string_keeps_default(self) -> None:
        settings = TimeoutSettings().merge_dict({"model_read_seconds": "abc"})
        assert settings.model_read_seconds == 600


class TestLoadCascade:
    """Cascade walking with user + project layers."""

    def test_no_files_returns_defaults(self, tmp_path: Path) -> None:
        # tmp_path acts as the user home; no settings.json present.
        settings = load_timeout_settings(project_root=tmp_path)
        assert settings == TimeoutSettings()

    def test_user_file_applied(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "home"
        (home / ".bog-agents").mkdir(parents=True)
        (home / ".bog-agents" / "settings.json").write_text(
            json.dumps({"timeouts": {"model_read_seconds": 1234}})
        )
        monkeypatch.setattr(Path, "home", lambda: home)
        settings = load_timeout_settings()
        assert settings.model_read_seconds == 1234
        # Defaults preserved for unset fields — remote stays disabled.
        assert settings.remote_read_seconds is None

    def test_project_overrides_user(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "home"
        project = tmp_path / "proj"
        (home / ".bog-agents").mkdir(parents=True)
        (project / ".bog-agents").mkdir(parents=True)
        (home / ".bog-agents" / "settings.json").write_text(
            json.dumps({"timeouts": {"model_read_seconds": 1000, "tool_seconds": 500}})
        )
        (project / ".bog-agents" / "settings.json").write_text(
            json.dumps({"timeouts": {"model_read_seconds": 2000}})
        )
        monkeypatch.setattr(Path, "home", lambda: home)
        settings = load_timeout_settings(project_root=project)
        # Project overrides the user value.
        assert settings.model_read_seconds == 2000
        # User-only setting survives.
        assert settings.tool_seconds == 500


class TestApplyToEnv:
    """Translation from settings to env vars the SDK consumes."""

    def test_existing_env_wins(self) -> None:
        env: dict[str, str] = {"BOG_AGENTS_MODEL_READ_TIMEOUT": "60"}
        settings = TimeoutSettings(model_read_seconds=7200)
        apply_to_env(settings, env=env)
        # User's shell override is preserved.
        assert env["BOG_AGENTS_MODEL_READ_TIMEOUT"] == "60"

    def test_unset_env_populated_from_settings(self) -> None:
        env: dict[str, str] = {}
        settings = TimeoutSettings(
            model_read_seconds=600,
            remote_read_seconds=1200,
            tool_seconds=1800,
        )
        apply_to_env(settings, env=env)
        assert env["BOG_AGENTS_MODEL_READ_TIMEOUT"] == "600"
        assert env["BOG_AGENTS_REMOTE_READ_TIMEOUT"] == "1200"
        assert env["BOG_AGENTS_TOOL_TIMEOUT"] == "1800"

    def test_disabled_value_renders_as_none(self) -> None:
        env: dict[str, str] = {}
        settings = TimeoutSettings(tool_seconds=None)
        apply_to_env(settings, env=env)
        assert env["BOG_AGENTS_TOOL_TIMEOUT"] == "none"

    def test_default_remote_renders_as_none(self) -> None:
        # The default TimeoutSettings has remote_read_seconds=None, so the
        # env the SDK consumes must say "none" — that's what disables the
        # per-chunk SSE deadline in remote_client._resolve_read_timeout.
        env: dict[str, str] = {}
        apply_to_env(TimeoutSettings(), env=env)
        assert env["BOG_AGENTS_REMOTE_READ_TIMEOUT"] == "none"
        # Model layer keeps its finite default.
        assert env["BOG_AGENTS_MODEL_READ_TIMEOUT"] == "600"

    def test_remote_positive_override_survives(self) -> None:
        # A user who wants a hard SSE cap back can set a positive number.
        env: dict[str, str] = {}
        apply_to_env(TimeoutSettings(remote_read_seconds=1800), env=env)
        assert env["BOG_AGENTS_REMOTE_READ_TIMEOUT"] == "1800"


class TestResolveToolTimeout:
    """The runtime helper that the CLI calls when constructing sandboxes."""

    def test_default_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("BOG_AGENTS_TOOL_TIMEOUT", raising=False)
        assert resolve_tool_timeout() == 7200

    def test_env_value_used(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BOG_AGENTS_TOOL_TIMEOUT", "300")
        assert resolve_tool_timeout() == 300

    def test_env_none_disables(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BOG_AGENTS_TOOL_TIMEOUT", "none")
        assert resolve_tool_timeout() is None

    def test_env_zero_disables(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BOG_AGENTS_TOOL_TIMEOUT", "0")
        assert resolve_tool_timeout() is None

    def test_garbage_falls_back_to_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BOG_AGENTS_TOOL_TIMEOUT", "abc")
        assert resolve_tool_timeout() == 7200
