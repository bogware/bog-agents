"""Tests for repo-committed `.prompt.md` slash commands (ROADMAP #14)."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pytest

from bog_agents_cli.prompt_commands import (
    PromptCommand,
    discover_prompt_commands,
    render_prompt_command,
)


def _write(prompts_dir: Path, name: str, content: str) -> None:
    prompts_dir.mkdir(parents=True, exist_ok=True)
    (prompts_dir / name).write_text(content, encoding="utf-8")


class TestDiscover:
    def test_discovers_with_frontmatter(self, tmp_path: Path) -> None:
        _write(
            tmp_path / ".bog-agents" / "prompts",
            "triage.prompt.md",
            "---\ndescription: Triage a bug\nargument-hint: '[issue]'\n---\n"
            "Investigate issue $ARGUMENTS and propose a fix.",
        )
        cmds = discover_prompt_commands(tmp_path, include_user=False)
        assert "/triage" in cmds
        cmd = cmds["/triage"]
        assert cmd.description == "Triage a bug"
        assert cmd.argument_hint == "[issue]"
        assert "$ARGUMENTS" in cmd.template
        assert cmd.scope == "project"

    def test_no_frontmatter_uses_body_and_default_desc(self, tmp_path: Path) -> None:
        _write(
            tmp_path / ".bog-agents" / "prompts",
            "ship.prompt.md",
            "Run the full release checklist.",
        )
        cmds = discover_prompt_commands(tmp_path, include_user=False)
        assert cmds["/ship"].template == "Run the full release checklist."
        assert "ship.prompt.md" in cmds["/ship"].description

    def test_empty_body_skipped(self, tmp_path: Path) -> None:
        _write(
            tmp_path / ".bog-agents" / "prompts", "empty.prompt.md", "---\nx: 1\n---\n"
        )
        assert discover_prompt_commands(tmp_path, include_user=False) == {}

    def test_no_dir_is_empty(self, tmp_path: Path) -> None:
        assert discover_prompt_commands(tmp_path, include_user=False) == {}

    def test_project_overrides_user(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_home = tmp_path / "home"
        _write(fake_home / ".bog-agents" / "prompts", "dup.prompt.md", "user version")
        project = tmp_path / "proj"
        _write(project / ".bog-agents" / "prompts", "dup.prompt.md", "project version")
        monkeypatch.setattr(Path, "home", lambda: fake_home)
        cmds = discover_prompt_commands(project, include_user=True)
        assert cmds["/dup"].template == "project version"
        assert cmds["/dup"].scope == "project"

    def test_malformed_frontmatter_falls_back_to_body(self, tmp_path: Path) -> None:
        _write(
            tmp_path / ".bog-agents" / "prompts",
            "bad.prompt.md",
            "---\n: : not valid yaml : :\n---\nbody text",
        )
        cmds = discover_prompt_commands(tmp_path, include_user=False)
        # Either it parses to empty meta + body, or treats whole file as body;
        # in both cases the command exists and is usable.
        assert "/bad" in cmds


class TestRender:
    def test_substitutes_arguments_token(self) -> None:
        cmd = PromptCommand(name="/t", description="d", template="Fix $ARGUMENTS now")
        assert render_prompt_command(cmd, "bug 42") == "Fix bug 42 now"

    def test_appends_when_no_token(self) -> None:
        cmd = PromptCommand(name="/t", description="d", template="Do the thing.")
        assert render_prompt_command(cmd, "extra ctx") == "Do the thing.\n\nextra ctx"

    def test_no_args_returns_template(self) -> None:
        cmd = PromptCommand(name="/t", description="d", template="Do the thing.")
        assert render_prompt_command(cmd, "") == "Do the thing."

    def test_token_with_empty_args(self) -> None:
        cmd = PromptCommand(name="/t", description="d", template="Fix $ARGUMENTS now")
        assert render_prompt_command(cmd, "") == "Fix  now"
