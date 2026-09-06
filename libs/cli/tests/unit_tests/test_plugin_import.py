"""ROADMAP #62: one-command import from Claude Code, Cursor and Codex."""

from __future__ import annotations

import json
from pathlib import Path

from bog_agents_cli.plugin_import import format_import_report, import_from_tool


def _claude_home(home: Path) -> None:
    (home / ".claude" / "agents").mkdir(parents=True)
    (home / ".claude" / "agents" / "planner.md").write_text(
        "---\nname: planner\ndescription: plans\n---\nPlan things.", encoding="utf-8"
    )
    (home / ".claude" / "settings.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "Bash",
                            "hooks": [{"type": "command", "command": "echo pre"}],
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    (home / ".claude" / "CLAUDE.md").write_text("Always be kind.", encoding="utf-8")
    mem = home / ".claude" / "projects" / "E--Code-proj" / "memory"
    mem.mkdir(parents=True)
    (mem / "MEMORY.md").write_text("- remember the ratchet", encoding="utf-8")
    (home / ".claude.json").write_text(
        json.dumps({"mcpServers": {"github": {"command": "npx", "args": ["gh-mcp"]}}}),
        encoding="utf-8",
    )


def test_claude_import_is_complete_and_idempotent(tmp_path: Path) -> None:
    home, project, config = tmp_path / "home", tmp_path / "proj", tmp_path / "cfg"
    _claude_home(home)
    (project / ".claude" / "skills" / "deploy").mkdir(parents=True)
    (project / ".claude" / "skills" / "deploy" / "SKILL.md").write_text(
        "---\nname: deploy\ndescription: deploy it\n---\nDeploy.", encoding="utf-8"
    )
    (project / ".claude" / "agents").mkdir()
    (project / ".claude" / "agents" / "tester.md").write_text(
        "You test.", encoding="utf-8"
    )
    (project / "CLAUDE.md").write_text("# rules", encoding="utf-8")

    dry = import_from_tool(
        "claude", project_root=project, config_dir=config, home=home, dry_run=True
    )
    assert dry.total >= 5
    assert not (config / "memory.md").exists()

    report = import_from_tool(
        "claude", project_root=project, config_dir=config, home=home
    )
    text = format_import_report(report)
    assert "Imported from claude" in text
    assert (config / "skills" / "deploy.md").is_file()
    assert (
        (project / ".bog-agents" / "agents" / "tester" / "AGENTS.md")
        .read_text(encoding="utf-8")
        .startswith("---\nname: tester")
    )
    assert (config / "agent" / "agents" / "planner" / "AGENTS.md").is_file()
    hooks = json.loads((config / "hooks.json").read_text(encoding="utf-8"))["hooks"]
    assert hooks[0]["matcher"] == "execute"
    memory = (config / "memory.md").read_text(encoding="utf-8")
    assert "Always be kind." in memory and "remember the ratchet" in memory
    mcp = json.loads((config / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]
    assert "github" in mcp
    assert any("CLAUDE.md is read natively" in n for n in report.notes)

    again = import_from_tool(
        "claude", project_root=project, config_dir=config, home=home
    )
    assert again.total == 0
    assert any("already" in n for n in again.notes)
    assert (config / "memory.md").read_text(encoding="utf-8").count(
        "Always be kind."
    ) == 1


def test_cursor_and_codex_imports(tmp_path: Path) -> None:
    home, project, config = tmp_path / "home", tmp_path / "proj", tmp_path / "cfg"
    (project / ".cursor" / "rules").mkdir(parents=True)
    (project / ".cursor" / "rules" / "a.mdc").write_text("rule", encoding="utf-8")
    (project / ".cursor" / "mcp.json").write_text(
        json.dumps({"mcpServers": {"local": {"command": "x"}}}), encoding="utf-8"
    )
    (home / ".cursor").mkdir(parents=True)
    (home / ".cursor" / "mcp.json").write_text(
        json.dumps({"mcpServers": {"global": {"url": "https://mcp"}}}), encoding="utf-8"
    )
    report = import_from_tool(
        "cursor", project_root=project, config_dir=config, home=home
    )
    assert any("read natively" in n for n in report.notes)
    assert json.loads((project / ".mcp.json").read_text(encoding="utf-8"))[
        "mcpServers"
    ] == {"local": {"command": "x"}}
    assert (
        "global"
        in json.loads((config / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]
    )

    (home / ".codex").mkdir()
    (home / ".codex" / "config.toml").write_text(
        '[mcp_servers.docs]\ncommand = "npx"\nargs = ["docs-mcp"]\n[mcp_servers.docs.env]\nTOKEN = "x"\n',
        encoding="utf-8",
    )
    (home / ".codex" / "AGENTS.md").write_text("Global codex rules.", encoding="utf-8")
    report = import_from_tool(
        "codex", project_root=project, config_dir=config, home=home
    )
    mcp = json.loads((config / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]
    assert mcp["docs"] == {
        "command": "npx",
        "args": ["docs-mcp"],
        "env": {"TOKEN": "x"},
    }
    assert "Global codex rules." in (config / "memory.md").read_text(encoding="utf-8")
    assert report.total == 2


def test_antigravity_and_unknown(tmp_path: Path) -> None:
    report = import_from_tool(
        "antigravity", project_root=tmp_path, config_dir=tmp_path / "cfg", home=tmp_path
    )
    assert report.total == 0 and "antigravity" in report.notes[0]
    assert (
        "unknown tool"
        in import_from_tool(
            "vim", project_root=tmp_path, config_dir=tmp_path / "cfg", home=tmp_path
        ).notes[0]
    )
