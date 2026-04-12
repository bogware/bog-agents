"""Tests for new CLI feature modules.

Tests for features: #4, #6, #14, #18, #20, #21, #22, #24, #27, #28, #31, #33, #45, #46, #49, #50.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from uuid import uuid4


class TestProfiles:
    """Tests for configuration profiles (#49)."""

    def test_builtin_profiles(self) -> None:
        """Test that built-in profiles exist."""
        from bog_agents_cli.profiles import BUILT_IN_PROFILES

        assert "review" in BUILT_IN_PROFILES
        assert "refactor" in BUILT_IN_PROFILES
        assert "debug" in BUILT_IN_PROFILES
        assert "quick" in BUILT_IN_PROFILES

    def test_load_profiles(self, tmp_path: Path) -> None:
        """Test loading profiles from config dir."""
        from bog_agents_cli.profiles import load_profiles

        profiles = load_profiles(tmp_path)
        # Should return built-in profiles even without config
        assert len(profiles) > 0

    def test_list_profiles(self, tmp_path: Path) -> None:
        """Test listing profiles."""
        from bog_agents_cli.profiles import list_profiles

        result = list_profiles(tmp_path)
        assert "review" in result
        assert "refactor" in result


class TestDoctor:
    """Tests for health check diagnostics (#33)."""

    def test_import(self) -> None:
        """Test module import."""
        from bog_agents_cli.doctor import run_doctor

        assert callable(run_doctor)

    def test_report_contains_expected_sections(self) -> None:
        """Doctor output should include the primary diagnostic sections."""
        from bog_agents_cli.doctor import run_doctor

        report = run_doctor()

        assert "Bog Agents" in report
        assert "Python" in report
        assert "Config" in report


class TestExtensions:
    """Tests for extension system (#18, #32)."""

    def test_parse_manifest(self, tmp_path: Path) -> None:
        """Test manifest parsing."""
        from bog_agents_cli.extensions import parse_manifest

        manifest_data = {
            "name": "test-ext",
            "version": "1.0.0",
            "description": "Test extension",
        }
        manifest_path = tmp_path / "bog-agents-extension.json"
        manifest_path.write_text(json.dumps(manifest_data))

        manifest = parse_manifest(manifest_path)
        assert manifest.name == "test-ext"
        assert manifest.version == "1.0.0"

    def test_list_extensions_empty(self, tmp_path: Path) -> None:
        """Test listing with no extensions."""
        from bog_agents_cli.extensions import list_extensions

        result = list_extensions(tmp_path)
        assert result == []


class TestRemote:
    """Tests for remote execution (#21)."""

    def test_load_config_missing(self, tmp_path: Path) -> None:
        """Test loading missing config."""
        from bog_agents_cli.remote import load_remote_config

        config = load_remote_config(tmp_path)
        assert config.provider == "langgraph-cloud"
        assert config.api_url == ""

    def test_format_tasks_empty(self) -> None:
        """Test formatting empty task list."""
        from bog_agents_cli.remote import format_remote_tasks

        result = format_remote_tasks([])
        assert "No remote tasks" in result


class TestOAuthMCP:
    """Tests for OAuth MCP (#31)."""

    def test_generate_pkce_pair(self) -> None:
        """Test PKCE pair generation."""
        from bog_agents_cli.oauth_mcp import generate_pkce_pair

        verifier, challenge = generate_pkce_pair()
        assert len(verifier) > 0
        assert len(challenge) > 0
        assert verifier != challenge

    def test_load_configs_missing(self, tmp_path: Path) -> None:
        """Test loading missing OAuth configs."""
        from bog_agents_cli.oauth_mcp import load_oauth_configs

        configs = load_oauth_configs(tmp_path)
        assert configs == {}


class TestTeach:
    """Tests for teaching sessions (#45)."""

    def test_teach_session_record(self) -> None:
        """Test recording actions."""
        from bog_agents_cli.teach import TeachSession

        session = TeachSession(name="test-skill")
        session.record_tool_call("read_file", {"path": "/test"}, "content")
        session.record_message("hello")
        assert len(session.actions) == 2

    def test_generate_skill(self) -> None:
        """Test skill generation."""
        from bog_agents_cli.teach import TeachSession, generate_skill_from_session

        session = TeachSession(name="test-skill", description="A test skill")
        session.record_tool_call("read_file", {"path": "/test"}, "content")
        skill = generate_skill_from_session(session)
        assert "test-skill" in skill
        assert "read_file" in skill


class TestReplay:
    """Tests for session replay (#50)."""

    def test_session_recorder(self) -> None:
        """Test session recorder."""
        from bog_agents_cli.replay import SessionRecorder

        recorder = SessionRecorder("test-session", "Test")
        assert not recorder.is_recording

        recorder.start_recording({"cwd": "/home"})
        assert recorder.is_recording

        recorder.record_tool_call("read_file", {"path": "/home/test"}, "result")
        recorder.record_message("hello", "user")

        session = recorder.stop_recording()
        assert len(session.actions) == 2
        assert session.session_id == "test-session"

    def test_save_load_session(self, tmp_path: Path) -> None:
        """Test saving and loading sessions."""
        from bog_agents_cli.replay import (
            SessionRecorder,
            load_replay_session,
            save_replay_session,
        )

        recorder = SessionRecorder("test-123")
        recorder.start_recording()
        recorder.record_tool_call("test_tool", {"arg": "val"})
        session = recorder.stop_recording()

        path = save_replay_session(tmp_path, session)
        loaded = load_replay_session(path)
        assert loaded.session_id == "test-123"
        assert len(loaded.actions) == 1


class TestKeybindings:
    """Tests for keybinding configuration (#24)."""

    def test_default_bindings(self) -> None:
        """Test default keybindings exist."""
        from bog_agents_cli.keybindings import DEFAULT_KEYBINDINGS

        assert "submit" in DEFAULT_KEYBINDINGS
        assert "cancel" in DEFAULT_KEYBINDINGS
        assert "quit" in DEFAULT_KEYBINDINGS

    def test_load_keybindings(self, tmp_path: Path) -> None:
        """Test loading keybindings."""
        from bog_agents_cli.keybindings import load_keybindings

        config = load_keybindings(tmp_path)
        assert config.get("submit") == "enter"

    def test_custom_keybindings(self, tmp_path: Path) -> None:
        """Test custom keybinding override."""
        from bog_agents_cli.keybindings import load_keybindings

        kb_file = tmp_path / "keybindings.json"
        kb_file.write_text(json.dumps({"submit": "ctrl+enter"}))

        config = load_keybindings(tmp_path)
        assert config.get("submit") == "ctrl+enter"


class TestInputShortcuts:
    """Tests for input shortcuts (#27, #28)."""

    def test_shell_prefix(self) -> None:
        """Test shell prefix parsing."""
        from bog_agents_cli.input_shortcuts import parse_input_shortcuts

        result = parse_input_shortcuts("!ls -la")
        assert result.action_type == "shell"
        assert result.content == "ls -la"

    def test_memory_prefix(self) -> None:
        """Test memory prefix parsing."""
        from bog_agents_cli.input_shortcuts import parse_input_shortcuts

        result = parse_input_shortcuts("# remember this")
        assert result.action_type == "memory"
        assert result.content == "remember this"

    def test_normal_message(self) -> None:
        """Test normal message (no prefix)."""
        from bog_agents_cli.input_shortcuts import parse_input_shortcuts

        result = parse_input_shortcuts("hello world")
        assert result.action_type == "message"

    def test_append_to_memory(self, tmp_path: Path) -> None:
        """Test appending to memory file."""
        from bog_agents_cli.input_shortcuts import append_to_memory

        md_path = tmp_path / "AGENTS.md"
        assert append_to_memory(md_path, "test note")
        content = md_path.read_text()
        assert "test note" in content


class TestCompactSelective:
    """Tests for selective compaction (#4)."""

    def test_parse_default(self) -> None:
        """Test default compaction."""
        from bog_agents_cli.compact_selective import parse_compact_args

        config = parse_compact_args("")
        assert config.keep_last_n == 5

    def test_parse_aggressive(self) -> None:
        """Test aggressive strategy."""
        from bog_agents_cli.compact_selective import parse_compact_args

        config = parse_compact_args("aggressive")
        assert config.keep_last_n == 3

    def test_parse_custom_last(self) -> None:
        """Test custom last:N."""
        from bog_agents_cli.compact_selective import parse_compact_args

        config = parse_compact_args("last:7")
        assert config.keep_last_n == 7


class TestReviewCommand:
    """Tests for review command (#14)."""

    def test_parse_staged(self) -> None:
        """Test parsing with no args (staged)."""
        from bog_agents_cli.review_command import parse_review_args

        target = parse_review_args("")
        assert target.target_type == "staged"

    def test_parse_commit(self) -> None:
        """Test parsing commit hash."""
        from bog_agents_cli.review_command import parse_review_args

        target = parse_review_args("HEAD~1")
        assert target.target_type == "commit"
        assert target.value == "HEAD~1"

    def test_parse_files(self) -> None:
        """Test parsing file paths."""
        from bog_agents_cli.review_command import parse_review_args

        target = parse_review_args("file1.py file2.py")
        assert target.target_type == "files"
        assert len(target.files) == 2

    def test_generate_prompt(self) -> None:
        """Test review prompt generation."""
        from bog_agents_cli.review_command import ReviewTarget, generate_review_prompt

        target = ReviewTarget(target_type="staged")
        prompt = generate_review_prompt(target)
        assert "Code Review" in prompt
        assert "staged" in prompt.lower()


class TestSessionFork:
    """Tests for session forking (#22)."""

    def test_create_fork(self, tmp_path: Path) -> None:
        """Test creating a fork."""
        from bog_agents_cli.session_fork import create_fork

        fork = create_fork(tmp_path, "thread-1", name="test fork")
        assert fork.parent_thread_id == "thread-1"
        assert "fork" in fork.fork_thread_id
        assert fork.name == "test fork"

    def test_list_forks(self, tmp_path: Path) -> None:
        """Test listing forks."""
        from bog_agents_cli.session_fork import create_fork, list_forks

        create_fork(tmp_path, "thread-1", name="fork 1")
        create_fork(tmp_path, "thread-1", name="fork 2")

        forks = list_forks(tmp_path, "thread-1")
        assert len(forks) == 2


class TestJSONOutput:
    """Tests for JSON output (#6)."""

    def test_stream_event(self) -> None:
        """Test stream event creation."""
        from bog_agents_cli.json_output import StreamEvent

        event = StreamEvent(event_type="message", data={"role": "ai"})
        assert event.event_type == "message"
        assert event.timestamp > 0

    def test_output_modes(self) -> None:
        """Test output mode enum."""
        from bog_agents_cli.json_output import OutputMode

        assert OutputMode.TEXT == "text"
        assert OutputMode.JSON == "json"
        assert OutputMode.STREAM_JSON == "stream-json"


class TestStreamingDiff:
    """Tests for streaming diff (#46)."""

    def test_generate_diff(self) -> None:
        """Test unified diff generation."""
        from bog_agents_cli.streaming_diff import DiffChunk, generate_unified_diff

        chunk = DiffChunk(
            file_path="test.py",
            old_content="def foo():\n    pass\n",
            new_content="def foo():\n    return 42\n",
        )
        diff = generate_unified_diff(chunk)
        assert "test.py" in diff
        assert "-    pass" in diff
        assert "+    return 42" in diff

    def test_edit_stats(self) -> None:
        """Test edit statistics."""
        from bog_agents_cli.streaming_diff import DiffChunk, compute_edit_stats

        chunk = DiffChunk(
            file_path="test.py",
            old_content="line1\nline2\n",
            new_content="line1\nline3\n",
        )
        stats = compute_edit_stats(chunk)
        assert stats["additions"] >= 0
        assert stats["deletions"] >= 0


class TestWebSearch:
    """Tests for web search (#20)."""

    def test_detect_provider_none(self) -> None:
        """Test provider detection when none configured."""
        import os

        from bog_agents_cli.web_search import detect_search_provider

        # Save and clear env vars
        saved = {}
        for key in ["TAVILY_API_KEY", "SERPER_API_KEY", "SEARXNG_URL"]:
            saved[key] = os.environ.pop(key, None)

        try:
            result = detect_search_provider()
            assert result is None
        finally:
            for key, val in saved.items():
                if val is not None:
                    os.environ[key] = val


class TestSlashCommands:
    """Tests for updated slash commands."""

    def test_new_commands_registered(self) -> None:
        """Test that new slash commands are in the list."""
        from bog_agents_cli.widgets.autocomplete import SLASH_COMMANDS

        command_names = {cmd for cmd, _, _ in SLASH_COMMANDS}
        assert "/review" in command_names
        assert "/doctor" in command_names
        assert "/commands" in command_names
        assert "/dashboard" in command_names
        assert "/extensions" in command_names
        assert "/logs" in command_names
        assert "/onboard" in command_names
        assert "/keybindings" in command_names
        assert "/permissions" in command_names
        assert "/resume" in command_names
        assert "/skills" in command_names
        assert "/tokens" in command_names
        assert "/agent" in command_names
        assert "/diff" in command_names
        assert "/effort" in command_names
        assert "/plan" in command_names
        assert "/plugin" in command_names
        assert "/profile" in command_names
        assert "/remote" in command_names
        assert "/worktree" in command_names

    def test_command_registry_drives_autocomplete(self) -> None:
        """Autocomplete commands should be derived from the central registry."""
        from bog_agents_cli.command_registry import (
            get_registered_command_names,
            get_slash_commands,
        )
        from bog_agents_cli.widgets.autocomplete import SLASH_COMMANDS

        assert get_slash_commands() == SLASH_COMMANDS
        assert {name for name, _, _ in SLASH_COMMANDS} == set(
            get_registered_command_names()
        )

    def test_extension_commands_appear_in_registry(self) -> None:
        """Enabled extension commands should flow into the central registry."""
        from bog_agents_cli.command_registry import get_command_spec, get_slash_commands

        tmp_path = Path("E:/Code/bog-agents/libs/cli/.tmp-command-tests") / uuid4().hex
        ext_dir = tmp_path / ".bog-agents" / "extensions" / "review-pack"
        ext_dir.mkdir(parents=True)
        (ext_dir / "bog-agents-extension.json").write_text(
            json.dumps(
                {
                    "name": "review-pack",
                    "version": "1.0.0",
                    "commands": [
                        {
                            "name": "/scout",
                            "description": "Scout a codebase slice",
                            "prompt": "Scout: {args}",
                            "aliases": ["/survey"],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        from unittest.mock import patch

        try:
            with patch("pathlib.Path.home", return_value=tmp_path):
                command_names = {name for name, _, _ in get_slash_commands()}
                assert "/scout" in command_names
                spec = get_command_spec("/survey")
                assert spec is not None
                assert spec.name == "/scout"
        finally:
            shutil.rmtree(tmp_path, ignore_errors=True)
