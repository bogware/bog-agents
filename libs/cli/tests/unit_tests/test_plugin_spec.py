"""ROADMAP #62: Agent Plugins 1.0 layout, discovery and trust."""

from __future__ import annotations

import json
from pathlib import Path

from bog_agents_cli import extensibility
from bog_agents_cli.plugin_spec import (
    discover_agent_plugins,
    is_plugin_trusted,
    load_plugin_spec,
    revoke_plugin_trust,
    safe_plugin_name,
    to_extension_manifest,
    trust_plugin,
)


def make_plugin(root: Path, name: str = "demo") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "plugin.json").write_text(
        json.dumps(
            {
                "name": name,
                "version": "1.2.3",
                "description": "Demo plugin",
                "author": "me",
            }
        ),
        encoding="utf-8",
    )
    (root / "skills" / "greet").mkdir(parents=True)
    (root / "skills" / "greet" / "SKILL.md").write_text(
        "---\nname: greet\ndescription: say hi\n---\nHi", encoding="utf-8"
    )
    (root / "agents").mkdir()
    (root / "agents" / "reviewer.md").write_text(
        "---\nname: reviewer\ndescription: reviews code\nmodel: sonnet\n---\nYou review.",
        encoding="utf-8",
    )
    (root / "commands").mkdir()
    (root / "commands" / "ship.md").write_text(
        "---\nname: ship\ndescription: Ship it\n---\nShip {args} carefully.",
        encoding="utf-8",
    )
    (root / "hooks").mkdir()
    (root / "hooks" / "hooks.json").write_text(
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
    (root / "mcp.json").write_text(
        json.dumps(
            {"mcpServers": {"fs": {"command": "npx", "args": ["-y", "server-fs"]}}}
        ),
        encoding="utf-8",
    )
    return root


def test_load_spec_and_manifest_mapping(tmp_path: Path) -> None:
    root = make_plugin(tmp_path / "demo")
    spec = load_plugin_spec(root)
    assert spec is not None
    assert (spec.name, spec.version) == ("demo", "1.2.3")
    assert spec.skills == ("skills/greet/SKILL.md",)
    assert spec.agents == ("agents/reviewer.md",)
    assert spec.commands == ("commands/ship.md",)
    assert spec.hooks == ("hooks/hooks.json",)
    assert "fs" in spec.mcp_servers
    manifest = to_extension_manifest(spec)
    assert manifest.commands == [
        {"name": "ship", "description": "Ship it", "prompt": "Ship {args} carefully."}
    ]
    assert manifest.agents[0]["name"] == "reviewer"
    assert manifest.mcp_servers[0]["name"] == "fs"
    assert (
        manifest.hooks and manifest.hooks[0]["matcher"] == "execute"
    )  # Bash aliased onto bog's tool name
    assert load_plugin_spec(tmp_path / "nope") is None
    (tmp_path / "bad").mkdir()
    (tmp_path / "bad" / "plugin.json").write_text("{}", encoding="utf-8")
    assert load_plugin_spec(tmp_path / "bad") is None
    assert safe_plugin_name("../My Plugin!") == "My-Plugin"


def test_discovery_scopes_and_workspace_trust(tmp_path: Path) -> None:
    home, project, config = tmp_path / "home", tmp_path / "proj", tmp_path / "cfg"
    make_plugin(home / ".agents" / "plugins" / "userpl", "userpl")
    make_plugin(project / ".agents" / "plugins" / "wspl", "wspl")
    make_plugin(config / "plugins" / "inst", "inst")
    found = {
        p.spec.name: p
        for p in discover_agent_plugins(
            config_dir=config, home=home, project_root=project
        )
    }
    assert found["userpl"].scope == "user" and found["userpl"].enabled
    assert found["inst"].scope == "installed" and found["inst"].enabled
    assert found["wspl"].scope == "workspace" and not found["wspl"].enabled
    root = found["wspl"].spec.root
    assert is_plugin_trusted(root, config_dir=config) is False
    trust_plugin(root, config_dir=config)
    assert is_plugin_trusted(root, config_dir=config) is True
    found = {
        p.spec.name: p
        for p in discover_agent_plugins(
            config_dir=config, home=home, project_root=project
        )
    }
    assert found["wspl"].enabled
    assert revoke_plugin_trust(root, config_dir=config) is True
    assert revoke_plugin_trust(root, config_dir=config) is False


def test_extensibility_lists_and_surfaces_plugins(tmp_path: Path, monkeypatch) -> None:
    project, config = tmp_path / "proj", tmp_path / "cfg"
    make_plugin(config / "plugins" / "inst", "inst")
    make_plugin(project / ".agents" / "plugins" / "wspl", "wspl")
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "home"))
    items = extensibility.list_extensibility_items(config, project)
    kinds = {(i.name, i.kind, i.enabled) for i in items}
    assert ("inst", "agent-plugin", True) in kinds
    assert ("wspl", "agent-plugin", False) in kinds
    # Only the enabled plugin contributes skills and commands.
    skill_dirs = extensibility.get_extension_skill_dirs(config, project)
    assert any(d.parent.parent.name == "inst" for d in skill_dirs)
    assert not any("wspl" in str(d) for d in skill_dirs)
    commands = extensibility.get_extension_commands(config, project)
    assert [c.extension_name for c in commands if c.name == "/ship"] == ["inst"]
    assert (
        extensibility.uninstall_extensibility_item(config, "wspl") is False
    )  # discovered, not installed
    assert extensibility.uninstall_extensibility_item(config, "inst") is True
    assert not (config / "plugins" / "inst").exists()
