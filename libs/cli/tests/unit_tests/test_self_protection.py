"""Self-modification guard (#24): the agent can't silently rewrite its authority."""

from __future__ import annotations

from bog_agents.middleware.permissions import (
    _build_interrupt_on_from_permissions,
    _check_fs_permission,
)

from bog_agents_cli.self_protection import (
    authority_file_permissions,
    command_targets_authority_file,
)


class TestAuthorityFilePermissions:
    def test_rules_are_interrupt_mode_writes(self) -> None:
        rules = authority_file_permissions()
        assert rules
        for rule in rules:
            assert rule.mode == "interrupt"
            assert rule.operations == ["write"]

    def test_authority_paths_resolve_to_interrupt(self) -> None:
        rules = authority_file_permissions()
        for path in (
            "/.bog-agents/laws.md",
            "/.bog-agents/constitution.md",
            "/.bog-agents/expert_rules/policy.yaml",
            "/.bog-agents/hooks/pre-commit.sh",
            "/.mcp.json",
        ):
            assert _check_fs_permission(rules, "write", path) == "interrupt", path

    def test_ordinary_writes_stay_allowed(self) -> None:
        rules = authority_file_permissions()
        for path in (
            "/src/main.py",
            "/README.md",
            "/.bog-agents/memory/note.md",  # memory is not authority
            "/.bog-agents/goal.json",  # goal state is not authority
            "/mcp.json",  # not the dotfile manifest
        ):
            assert _check_fs_permission(rules, "write", path) == "allow", path

    def test_reads_are_not_gated(self) -> None:
        rules = authority_file_permissions()
        assert _check_fs_permission(rules, "read", "/.bog-agents/laws.md") == "allow"

    def test_synthesizes_interrupt_on_for_file_tools(self) -> None:
        interrupt_on = _build_interrupt_on_from_permissions(
            authority_file_permissions()
        )
        # The write-family file tools must carry an interrupt config so a write
        # to an authority path prompts — even under --auto-approve.
        assert "write_file" in interrupt_on
        assert "edit_file" in interrupt_on


class TestShellCommandScreen:
    def test_flags_writes_to_authority(self) -> None:
        assert command_targets_authority_file("echo 'allow *' > .bog-agents/laws.md")
        assert command_targets_authority_file("sed -i s/x/y/ ~/.bog-agents/config.toml")
        assert command_targets_authority_file(
            "cat evil >> .bog-agents/expert_rules/policy.yaml"
        )
        assert command_targets_authority_file("cp payload .bog-agents/constitution.md")

    def test_ignores_reads_of_authority(self) -> None:
        assert not command_targets_authority_file("cat .bog-agents/laws.md")
        assert not command_targets_authority_file(
            "grep foo .bog-agents/expert_rules/policy.yaml"
        )

    def test_ignores_unrelated_writes(self) -> None:
        assert not command_targets_authority_file("echo hi > /tmp/out.txt")
        assert not command_targets_authority_file("ls -la && pytest -q")

    def test_empty_command(self) -> None:
        assert not command_targets_authority_file("")
