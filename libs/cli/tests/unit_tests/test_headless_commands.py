"""Tests for headless (non-interactive) slash-command execution."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from bog_agents_cli.headless_commands import HEADLESS_COMMANDS, run_headless_command

if TYPE_CHECKING:
    import pytest


def test_version_command_text(capsys: pytest.CaptureFixture[str]) -> None:
    """`/version` prints CLI + SDK versions and exits 0."""
    rc = run_headless_command("/version")
    assert rc == 0
    out = capsys.readouterr().out
    assert "bog-agents-cli" in out
    assert "SDK" in out


def test_commands_lists_registry(capsys: pytest.CaptureFixture[str]) -> None:
    """`/commands` lists slash commands and marks headless ones."""
    rc = run_headless_command("commands")  # leading slash optional
    assert rc == 0
    out = capsys.readouterr().out
    assert "/model" in out
    assert "Slash commands" in out


def test_help_for_specific_command(capsys: pytest.CaptureFixture[str]) -> None:
    """`/help model` shows details for the model command."""
    rc = run_headless_command("/help model")
    assert rc == 0
    assert "model" in capsys.readouterr().out.lower()


def test_help_unknown_command_returns_1(capsys: pytest.CaptureFixture[str]) -> None:
    """`/help nonexistent` reports the command ran but failed (exit 1)."""
    rc = run_headless_command("/help totally-not-a-command")
    assert rc == 1
    assert "Unknown command" in capsys.readouterr().err


def test_non_headless_command_returns_2(capsys: pytest.CaptureFixture[str]) -> None:
    """A TUI-only command is rejected (exit 2) with guidance listing headless commands."""
    rc = run_headless_command("/plan")
    assert rc == 2
    err = capsys.readouterr().err
    assert "not available in non-interactive mode" in err
    assert "/help" in err


def test_empty_command_returns_2(capsys: pytest.CaptureFixture[str]) -> None:
    """An empty command line is rejected."""
    rc = run_headless_command("   ")
    assert rc == 2
    assert "No command" in capsys.readouterr().err


def test_json_output_envelope(capsys: pytest.CaptureFixture[str]) -> None:
    """`--json` mode emits a single machine-readable envelope on stdout."""
    rc = run_headless_command("/version", output_format="json")
    assert rc == 0
    envelope = json.loads(capsys.readouterr().out.strip())
    assert envelope["schema_version"] == 1
    assert envelope["command"] == "command:version"
    assert envelope["data"]["ok"] is True
    assert "cli" in envelope["data"]


def test_json_non_headless_envelope(capsys: pytest.CaptureFixture[str]) -> None:
    """Non-headless commands emit a structured error envelope in json mode."""
    rc = run_headless_command("/dashboard", output_format="json")
    assert rc == 2
    envelope = json.loads(capsys.readouterr().out.strip())
    assert envelope["data"]["ok"] is False
    assert envelope["data"]["error"] == "not_headless"
    assert "commands" in envelope["data"]["headless_commands"]


def test_registry_handlers_are_callable() -> None:
    """Every registered headless command exposes a (description, handler) pair."""
    for name, (description, handler) in HEADLESS_COMMANDS.items():
        assert isinstance(name, str)
        assert isinstance(description, str)
        assert callable(handler)


class TestMsysRecovery:
    """v6 CLI-8: Git Bash rewrites `/help` into a Git install path before Python runs."""

    def test_recovers_command_from_git_bash_path(self) -> None:
        from bog_agents_cli.headless_commands import recover_msys_command

        assert recover_msys_command("C:/Program Files/Git/help") == "help"
        assert (
            recover_msys_command("C:\\Program Files\\Git\\config get models.default")
            == "config get models.default"
        )
        assert recover_msys_command("C:/Program Files (x86)/Git/version") == "version"

    def test_leaves_normal_input_alone(self) -> None:
        from bog_agents_cli.headless_commands import recover_msys_command

        assert recover_msys_command("/help") == "/help"
        assert recover_msys_command("help") == "help"
        assert recover_msys_command("C:/Users/me/project") == "C:/Users/me/project"

    def test_mangled_help_runs(self, capsys) -> None:
        from bog_agents_cli.headless_commands import run_headless_command

        code = run_headless_command("C:/Program Files/Git/help")
        assert code == 0
        assert "not available" not in capsys.readouterr().out
