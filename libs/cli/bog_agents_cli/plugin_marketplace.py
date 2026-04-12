"""Plugin marketplace CLI interface.

Feature #7: Plugin marketplace — browse, install, manage plugins.
Feature #8: SKILL.md standard support.
Feature #9: Plugin manifest system.
Feature #10: Skill authoring wizard.
Feature #11: Skill sharing.
Feature #12: Plugin sandboxing.
"""

from __future__ import annotations

import json
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class PluginInfo:
    """Plugin metadata from registry or local install."""

    name: str
    version: str
    description: str
    author: str = ""
    homepage: str = ""
    downloads: int = 0
    rating: float = 0.0
    installed: bool = False
    enabled: bool = True
    install_path: Path | None = None
    skills: tuple[str, ...] = ()
    commands: tuple[str, ...] = ()


def get_plugins_dir(*, create: bool = True) -> Path:
    """Get the plugins directory.

    Args:
        create: When `True`, create the directory if missing.

    Returns:
        Path to plugins directory.
    """
    plugins_dir = Path.home() / ".bog-agents" / "plugins"
    if create:
        plugins_dir.mkdir(parents=True, exist_ok=True)
    return plugins_dir


def get_skills_dir() -> Path:
    """Get the skills directory.

    Returns:
        Path to skills directory.
    """
    skills_dir = Path.home() / ".bog-agents" / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    return skills_dir


def read_plugin_manifest(manifest_path: Path) -> dict[str, Any]:
    """Read a plugin manifest from disk.

    Args:
        manifest_path: Path to the plugin `manifest.json`.

    Returns:
        Parsed manifest dictionary.

    Raises:
        ValueError: If the manifest cannot be parsed.
    """
    try:
        return json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        msg = f"Could not read plugin manifest at {manifest_path}: {exc}"
        raise ValueError(msg) from exc


def list_installed_plugins(plugins_dir: Path | None = None) -> list[PluginInfo]:
    """List all installed plugins.

    Args:
        plugins_dir: Optional custom plugins directory.

    Returns:
        List of installed plugin info.
    """
    directory = plugins_dir or get_plugins_dir(create=False)
    if not directory.exists():
        return []
    plugins: list[PluginInfo] = []

    try:
        plugin_paths = sorted(directory.iterdir())
    except OSError:
        logger.warning("Could not read plugins directory: %s", directory, exc_info=True)
        return []

    for plugin_dir in plugin_paths:
        if plugin_dir.is_dir():
            manifest_path = plugin_dir / "manifest.json"
            if manifest_path.exists():
                try:
                    data = read_plugin_manifest(manifest_path)
                    plugins.append(
                        PluginInfo(
                            name=data.get("name", plugin_dir.name),
                            version=data.get("version", "0.0.0"),
                            description=data.get("description", ""),
                            author=data.get("author", ""),
                            homepage=data.get("homepage", ""),
                            enabled=not (plugin_dir / ".disabled").exists(),
                            installed=True,
                            install_path=plugin_dir,
                            skills=tuple(
                                entry
                                for entry in data.get("skills", [])
                                if isinstance(entry, str)
                            ),
                            commands=tuple(
                                entry.get("name", "").strip()
                                for entry in data.get("commands", [])
                                if isinstance(entry, dict)
                                and isinstance(entry.get("name"), str)
                                and entry.get("name", "").strip()
                            ),
                        )
                    )
                except ValueError:
                    plugins.append(
                        PluginInfo(
                            name=plugin_dir.name,
                            version="unknown",
                            description="(invalid manifest)",
                            installed=True,
                            enabled=not (plugin_dir / ".disabled").exists(),
                            install_path=plugin_dir,
                        )
                    )

    return plugins


def install_plugin_from_path(source: str, plugins_dir: Path | None = None) -> str:
    """Install a plugin from a local path.

    Args:
        source: Local path to plugin directory.
        plugins_dir: Optional custom plugins directory.

    Returns:
        Status message.
    """
    src = Path(source)
    if not src.exists():
        return f"Source not found: {source}"

    directory = plugins_dir or get_plugins_dir()
    dest = directory / src.name
    shutil.copytree(str(src), str(dest), dirs_exist_ok=True)
    return f"Installed plugin from {source} to {dest}"


def uninstall_plugin(name: str, plugins_dir: Path | None = None) -> str:
    """Uninstall a plugin by name.

    Args:
        name: Plugin name.
        plugins_dir: Optional custom plugins directory.

    Returns:
        Status message.
    """
    directory = plugins_dir or get_plugins_dir()
    plugin_dir = directory / name
    if not plugin_dir.exists():
        return f"Plugin '{name}' not found."
    shutil.rmtree(plugin_dir)
    return f"Uninstalled plugin '{name}'"


def create_skill_file(
    name: str, description: str, skills_dir: Path | None = None
) -> Path:
    """Create a new SKILL.md file with template content.

    Args:
        name: Skill name.
        description: Skill description.
        skills_dir: Optional custom skills directory.

    Returns:
        Path to created skill file.
    """
    directory = skills_dir or get_skills_dir()
    skill_path = directory / f"{name}.md"
    content = (
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
    skill_path.write_text(content, encoding="utf-8")
    return skill_path


def format_plugin_list(plugins: list[PluginInfo]) -> str:
    """Format plugin list for display.

    Args:
        plugins: List of plugins.

    Returns:
        Formatted string.
    """
    if not plugins:
        return "No plugins installed."

    lines: list[str] = []
    for p in plugins:
        status = "enabled" if p.enabled else "disabled"
        lines.append(f"  [{status}] {p.name} v{p.version}")
        if p.description:
            lines.append(f"           {p.description}")
        if p.author:
            lines.append(f"           by {p.author}")
    return "\n".join(lines)
