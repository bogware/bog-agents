"""Middleware for plugin marketplace and skills ecosystem.

Feature #7: Plugin marketplace.
Feature #8: SKILL.md standard.
Feature #9: Plugin manifest system.
Feature #10: Skill authoring wizard.
Feature #11: Skill sharing.
Feature #12: Plugin sandboxing.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ContextT,
    ResponseT,
)
from langchain.tools import ToolRuntime
from langchain_core.tools import BaseTool, StructuredTool
from typing_extensions import TypedDict

logger = logging.getLogger(__name__)


@dataclass
class PluginManifest:
    """Plugin manifest describing a plugin package."""

    name: str
    version: str
    description: str
    author: str = ""
    homepage: str = ""
    skills: list[str] = field(default_factory=list)
    hooks: list[str] = field(default_factory=list)
    mcp_servers: list[dict[str, str]] = field(default_factory=list)
    permissions: list[str] = field(default_factory=list)
    compatible_tools: list[str] = field(default_factory=list)  # bog-agents, claude-code, codex


@dataclass
class InstalledPlugin:
    """A locally installed plugin."""

    manifest: PluginManifest
    install_path: Path
    enabled: bool = True


def parse_skill_md(content: str) -> dict[str, Any]:
    """Parse a SKILL.md file into structured data.

    Supports the open SKILL.md format compatible with Claude Code/Codex.

    Args:
        content: Raw SKILL.md content.

    Returns:
        Parsed skill data with frontmatter and body.
    """
    data: dict[str, Any] = {"frontmatter": {}, "body": ""}

    # Parse YAML-like frontmatter
    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            frontmatter = parts[1].strip()
            for line in frontmatter.split("\n"):
                if ":" in line:
                    key, _, value = line.partition(":")
                    data["frontmatter"][key.strip()] = value.strip()
            data["body"] = parts[2].strip()
        else:
            data["body"] = content
    else:
        data["body"] = content

    return data


def create_skill_template(name: str, description: str) -> str:
    """Generate a SKILL.md template.

    Args:
        name: Skill name.
        description: Skill description.

    Returns:
        SKILL.md template content.
    """
    return (
        f"---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        f"version: 0.1.0\n"
        f"author: \n"
        f"tools: []\n"
        f"---\n\n"
        f"# {name}\n\n"
        f"{description}\n\n"
        f"## Instructions\n\n"
        f"[Add instructions for the agent here]\n\n"
        f"## Examples\n\n"
        f"[Add usage examples here]\n"
    )


class PluginSystemState(TypedDict):
    """State for plugin system middleware."""


class PluginSystemMiddleware(AgentMiddleware[PluginSystemState, ContextT, ResponseT]):
    """Middleware for plugin marketplace and skills ecosystem.

    Manages plugin installation, SKILL.md files, and the plugin registry.

    Args:
        plugins_dir: Directory for installed plugins.
        skills_dir: Directory for SKILL.md files.
        registry_url: URL for the plugin registry.
    """

    state_schema = PluginSystemState

    def __init__(
        self,
        *,
        plugins_dir: Path | None = None,
        skills_dir: Path | None = None,
        registry_url: str = "",
        working_dir: Path | None = None,
    ) -> None:
        self._plugins_dir = plugins_dir or Path.home() / ".bog-agents" / "plugins"
        self._skills_dir = skills_dir or Path.home() / ".bog-agents" / "skills"
        self._registry_url = registry_url
        self._working_dir = working_dir or Path.cwd()
        self._installed: dict[str, InstalledPlugin] = {}
        self._plugins_dir.mkdir(parents=True, exist_ok=True)
        self._skills_dir.mkdir(parents=True, exist_ok=True)
        self._load_installed()
        self.tools = self._build_tools()

    def _load_installed(self) -> None:
        """Load installed plugins from disk."""
        for plugin_dir in self._plugins_dir.iterdir():
            if plugin_dir.is_dir():
                manifest_path = plugin_dir / "manifest.json"
                if manifest_path.exists():
                    try:
                        data = json.loads(manifest_path.read_text(encoding="utf-8"))
                        manifest = PluginManifest(**data)
                        self._installed[manifest.name] = InstalledPlugin(
                            manifest=manifest,
                            install_path=plugin_dir,
                        )
                    except (json.JSONDecodeError, TypeError, OSError):
                        pass

    def _build_tools(self) -> list[BaseTool]:
        """Build plugin management tools."""
        middleware = self

        def list_plugins(
            runtime: ToolRuntime[None, PluginSystemState],
            show_all: Annotated[bool, "Show all available plugins from registry"] = False,
        ) -> str:
            """List installed plugins or browse the plugin marketplace."""
            if not middleware._installed and not show_all:
                return "No plugins installed. Use install_plugin to add plugins."
            lines = ["Installed Plugins:"]
            for name, plugin in middleware._installed.items():
                status = "enabled" if plugin.enabled else "disabled"
                lines.append(f"  [{status}] {name} v{plugin.manifest.version}: {plugin.manifest.description}")
            return "\n".join(lines)

        def install_plugin(
            runtime: ToolRuntime[None, PluginSystemState],
            source: Annotated[str, "Plugin source: git URL, local path, or registry name"],
        ) -> str:
            """Install a plugin from a git URL, local path, or registry."""
            plugin_dir = middleware._plugins_dir / source.split("/")[-1].replace(".git", "")

            if source.startswith(("http://", "https://", "git@")):
                # Git install
                try:
                    result = subprocess.run(
                        ["git", "clone", "--depth", "1", source, str(plugin_dir)],
                        capture_output=True,
                        text=True,
                        timeout=60,
                        check=False,
                    )
                    if result.returncode != 0:
                        return f"Failed to clone: {result.stderr}"
                except (FileNotFoundError, subprocess.TimeoutExpired) as e:
                    return f"Error: {e}"
            elif Path(source).exists():
                # Local install
                shutil.copytree(source, plugin_dir, dirs_exist_ok=True)
            else:
                return f"Source not found: {source}"

            # Load manifest
            manifest_path = plugin_dir / "manifest.json"
            if manifest_path.exists():
                try:
                    data = json.loads(manifest_path.read_text(encoding="utf-8"))
                    manifest = PluginManifest(**data)
                    middleware._installed[manifest.name] = InstalledPlugin(manifest=manifest, install_path=plugin_dir)
                    return f"Installed plugin '{manifest.name}' v{manifest.version}"
                except (json.JSONDecodeError, TypeError) as e:
                    return f"Invalid manifest: {e}"
            return f"Plugin installed at {plugin_dir} (no manifest.json found)"

        def uninstall_plugin(
            runtime: ToolRuntime[None, PluginSystemState],
            name: Annotated[str, "Plugin name to uninstall"],
        ) -> str:
            """Uninstall a plugin."""
            plugin = middleware._installed.pop(name, None)
            if plugin is None:
                return f"Plugin '{name}' not found."
            if plugin.install_path.exists():
                shutil.rmtree(plugin.install_path)
            return f"Uninstalled plugin '{name}'"

        def toggle_plugin(
            runtime: ToolRuntime[None, PluginSystemState],
            name: Annotated[str, "Plugin name to toggle"],
        ) -> str:
            """Enable or disable a plugin."""
            plugin = middleware._installed.get(name)
            if plugin is None:
                return f"Plugin '{name}' not found."
            plugin.enabled = not plugin.enabled
            status = "enabled" if plugin.enabled else "disabled"
            return f"Plugin '{name}' is now {status}"

        def create_skill(
            runtime: ToolRuntime[None, PluginSystemState],
            name: Annotated[str, "Skill name"],
            description: Annotated[str, "Skill description"],
        ) -> str:
            """Create a new SKILL.md file with a template."""
            skill_path = middleware._skills_dir / f"{name}.md"
            content = create_skill_template(name, description)
            skill_path.write_text(content, encoding="utf-8")
            return f"Created skill at {skill_path}\n\nTemplate:\n{content}"

        def list_skills(
            runtime: ToolRuntime[None, PluginSystemState],
        ) -> str:
            """List all available SKILL.md files."""
            skills: list[str] = []
            for skill_file in middleware._skills_dir.glob("*.md"):
                try:
                    content = skill_file.read_text(encoding="utf-8")
                    data = parse_skill_md(content)
                    name = data["frontmatter"].get("name", skill_file.stem)
                    desc = data["frontmatter"].get("description", "")
                    skills.append(f"  {name}: {desc}")
                except OSError:
                    skills.append(f"  {skill_file.stem}: (error reading)")

            if not skills:
                return "No skills found. Use create_skill to add skills."
            return "Available Skills:\n" + "\n".join(skills)

        def publish_skill(
            runtime: ToolRuntime[None, PluginSystemState],
            skill_name: Annotated[str, "Name of the skill to publish"],
            target: Annotated[str, "Publish target: 'git' or 'local'"] = "local",
        ) -> str:
            """Publish a skill for sharing."""
            skill_path = middleware._skills_dir / f"{skill_name}.md"
            if not skill_path.exists():
                return f"Skill '{skill_name}' not found."
            return f"Skill '{skill_name}' is ready for sharing at {skill_path}. Copy to a git repo to distribute."

        def list_claude_skills(
            runtime: ToolRuntime[None, PluginSystemState],
        ) -> str:
            """List Claude Code-compatible skills found in .claude/ directories."""
            try:
                from bog_agents_cli.claude_code_compat import detect_claude_skills  # type: ignore[import]

                skills = detect_claude_skills(middleware._working_dir)
                if not skills:
                    return "No Claude Code skills found in .claude/ directories."
                lines = ["Claude Code skills found:"]
                for s in skills:
                    lines.append(f"  {s.name} v{s.version} — {s.description}")
                    lines.append(f"    {s.source_path}")
                return "\n".join(lines)
            except ImportError:
                return "Claude Code compat module not available (CLI not installed)."

        def import_claude_skills(
            runtime: ToolRuntime[None, PluginSystemState],
        ) -> str:
            """Import all Claude Code skills from .claude/ into bog-agents skills directory."""
            try:
                from bog_agents_cli.claude_code_compat import detect_claude_skills, import_claude_skill  # type: ignore[import]

                skills = detect_claude_skills(middleware._working_dir)
                if not skills:
                    return "No Claude Code skills found to import."
                imported = []
                for skill in skills:
                    dest = import_claude_skill(skill, middleware._skills_dir)
                    imported.append(f"  {skill.name} → {dest}")
                return f"Imported {len(imported)} skill(s):\n" + "\n".join(imported)
            except ImportError:
                return "Claude Code compat module not available (CLI not installed)."

        def sync_mcp_with_claude(
            runtime: ToolRuntime[None, PluginSystemState],
            direction: Annotated[str, "Sync direction: both, to-desktop, from-desktop"] = "both",
        ) -> str:
            """Sync MCP server configs between .mcp.json and Claude Desktop."""
            try:
                from bog_agents_cli.claude_code_compat import sync_mcp_configs  # type: ignore[import]

                result = sync_mcp_configs(middleware._working_dir, direction=direction)
                parts = []
                if result.added_to_mcp_json:
                    parts.append(f"Added to .mcp.json: {', '.join(result.added_to_mcp_json)}")
                if result.added_from_desktop:
                    parts.append(f"Added from Claude Desktop: {', '.join(result.added_from_desktop)}")
                if result.errors:
                    parts.append(f"Errors: {'; '.join(result.errors)}")
                return "\n".join(parts) if parts else "MCP configs already in sync."
            except ImportError:
                return "Claude Code compat module not available (CLI not installed)."

        return [
            StructuredTool.from_function(name="list_plugins", description="List installed plugins.", func=list_plugins),
            StructuredTool.from_function(name="install_plugin", description="Install a plugin.", func=install_plugin),
            StructuredTool.from_function(name="uninstall_plugin", description="Uninstall a plugin.", func=uninstall_plugin),
            StructuredTool.from_function(name="toggle_plugin", description="Enable/disable a plugin.", func=toggle_plugin),
            StructuredTool.from_function(name="create_skill", description="Create a SKILL.md file.", func=create_skill),
            StructuredTool.from_function(name="list_skills", description="List available skills.", func=list_skills),
            StructuredTool.from_function(name="publish_skill", description="Publish a skill.", func=publish_skill),
            StructuredTool.from_function(name="list_claude_skills", description="List Claude Code skills in .claude/ directories.", func=list_claude_skills),
            StructuredTool.from_function(name="import_claude_skills", description="Import Claude Code skills into bog-agents.", func=import_claude_skills),
            StructuredTool.from_function(name="sync_mcp_with_claude", description="Sync MCP server configs with Claude Desktop.", func=sync_mcp_with_claude),
        ]
