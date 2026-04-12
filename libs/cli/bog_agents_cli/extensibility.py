"""Unified plugin and extension management for the CLI."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from string import Formatter
from typing import Any, Literal

from bog_agents_cli.extensions import (
    InstalledExtension,
    disable_extension,
    enable_extension,
    format_extensions_list,
    install_extension,
    list_extensions,
    uninstall_extension,
)
from bog_agents_cli.plugin_marketplace import (
    PluginInfo,
    format_plugin_list,
    install_plugin_from_path,
    list_installed_plugins,
    uninstall_plugin,
)

ExtensibilityKind = Literal["plugin", "extension"]


@dataclass(frozen=True, slots=True)
class ExtensibilityItem:
    """Metadata for one installed plugin or extension."""

    kind: ExtensibilityKind
    name: str
    version: str
    description: str
    author: str = ""
    homepage: str = ""
    enabled: bool = True
    install_path: Path | None = None
    skills: tuple[str, ...] = ()
    commands: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ExtensionCommandSpec:
    """Normalized extension-provided slash command metadata."""

    extension_name: str
    name: str
    description: str
    prompt_template: str
    aliases: tuple[str, ...] = ()
    hidden_keywords: str = ""


def get_plugins_dir(config_dir: Path, *, create: bool = True) -> Path:
    """Return the plugin installation directory for a config root."""
    directory = config_dir / "plugins"
    if create:
        directory.mkdir(parents=True, exist_ok=True)
    return directory


def _plugin_to_item(plugin: PluginInfo) -> ExtensibilityItem:
    return ExtensibilityItem(
        kind="plugin",
        name=plugin.name,
        version=plugin.version,
        description=plugin.description,
        author=plugin.author,
        homepage=plugin.homepage,
        enabled=plugin.enabled,
        install_path=plugin.install_path,
        skills=plugin.skills,
        commands=plugin.commands,
    )


def _extension_to_item(ext: InstalledExtension) -> ExtensibilityItem:
    commands = tuple(
        name
        for name in (
            _normalize_command_name(command.get("name") or command.get("command"))
            for command in ext.manifest.commands
            if isinstance(command, dict)
        )
        if name
    )
    return ExtensibilityItem(
        kind="extension",
        name=ext.manifest.name,
        version=ext.manifest.version,
        description=ext.manifest.description,
        author=ext.manifest.author,
        homepage=ext.manifest.homepage,
        enabled=ext.enabled,
        install_path=ext.install_path,
        skills=tuple(ext.manifest.skills),
        commands=commands,
    )


def list_extensibility_items(config_dir: Path) -> list[ExtensibilityItem]:
    """List installed plugins and extensions together."""
    plugins_dir = get_plugins_dir(config_dir, create=False)
    plugins = [_plugin_to_item(item) for item in list_installed_plugins(plugins_dir)]
    extensions = [_extension_to_item(item) for item in list_extensions(config_dir)]
    return sorted([*plugins, *extensions], key=lambda item: (item.kind, item.name))


def find_extensibility_item(config_dir: Path, name: str) -> ExtensibilityItem | None:
    """Look up an installed plugin or extension by name."""
    normalized = name.strip().lower()
    for item in list_extensibility_items(config_dir):
        if item.name.lower() == normalized:
            return item
    return None


def install_extensibility_item(config_dir: Path, source: str) -> ExtensibilityItem:
    """Install a plugin or extension from a source path or URL.

    Args:
        config_dir: Base config directory.
        source: Local path or remote URL to install from.

    Returns:
        Installed package metadata.

    Raises:
        ValueError: If the source cannot be installed or reloaded.
    """
    source_path = Path(source).expanduser()
    if source.startswith(("http://", "https://", "git@")):
        installed = install_extension(config_dir, source)
        return _extension_to_item(installed)
    if source_path.is_dir() and (source_path / "bog-agents-extension.json").exists():
        installed = install_extension(config_dir, str(source_path))
        return _extension_to_item(installed)

    plugins_dir = get_plugins_dir(config_dir)
    install_plugin_from_path(str(source_path), plugins_dir=plugins_dir)
    installed_name = source_path.name
    installed = find_extensibility_item(config_dir, installed_name)
    if installed is None:
        msg = f"Installed item could not be reloaded: {installed_name}"
        raise ValueError(msg)
    return installed


def uninstall_extensibility_item(config_dir: Path, name: str) -> bool:
    """Uninstall a plugin or extension by name."""
    item = find_extensibility_item(config_dir, name)
    if item is None:
        return False
    if item.kind == "extension":
        return uninstall_extension(config_dir, item.name)
    uninstall_plugin(item.name, plugins_dir=get_plugins_dir(config_dir, create=False))
    return True


def enable_extensibility_item(config_dir: Path, name: str) -> bool:
    """Enable a plugin or extension."""
    item = find_extensibility_item(config_dir, name)
    if item is None or item.install_path is None:
        return False
    if item.kind == "extension":
        return enable_extension(config_dir, item.name)
    disabled_marker = item.install_path / ".disabled"
    if disabled_marker.exists():
        disabled_marker.unlink()
    return True


def disable_extensibility_item(config_dir: Path, name: str) -> bool:
    """Disable a plugin or extension without uninstalling it."""
    item = find_extensibility_item(config_dir, name)
    if item is None or item.install_path is None:
        return False
    if item.kind == "extension":
        return disable_extension(config_dir, item.name)
    (item.install_path / ".disabled").touch()
    return True


def format_extensibility_list(config_dir: Path) -> str:
    """Format installed plugins and extensions in one display block."""
    plugins = list_installed_plugins(get_plugins_dir(config_dir, create=False))
    extensions = list_extensions(config_dir)
    return "\n\n".join(
        [
            "Plugins",
            format_plugin_list(plugins),
            "Extensions",
            format_extensions_list(extensions),
        ]
    )


def describe_extensibility_item(item: ExtensibilityItem) -> str:
    """Render a detail block for one installed item."""
    status = "enabled" if item.enabled else "disabled"
    lines = [
        f"Name: {item.name}",
        f"Type: {item.kind}",
        f"Version: {item.version}",
        f"Status: {status}",
        f"Description: {item.description or '(none)'}",
    ]
    if item.author:
        lines.append(f"Author: {item.author}")
    if item.homepage:
        lines.append(f"Homepage: {item.homepage}")
    if item.install_path:
        lines.append(f"Install path: {item.install_path}")
    if item.skills:
        lines.append(f"Skills: {', '.join(item.skills)}")
    if item.commands:
        lines.append(f"Commands: {', '.join(item.commands)}")
    return "\n".join(lines)


def get_extension_skill_dirs(config_dir: Path) -> list[Path]:
    """Return directories that contain enabled extension-provided skills."""
    directories: list[Path] = []
    seen: set[Path] = set()
    for ext in list_extensions(config_dir):
        if not ext.enabled:
            continue
        for relative_path in ext.manifest.skills:
            skill_path = (ext.install_path / relative_path).resolve()
            skill_dir = skill_path.parent if skill_path.suffix else skill_path
            if skill_dir.exists() and skill_dir not in seen:
                seen.add(skill_dir)
                directories.append(skill_dir)
    return directories


def get_extension_commands(config_dir: Path) -> list[ExtensionCommandSpec]:
    """Return enabled extension-provided slash commands."""
    commands: list[ExtensionCommandSpec] = []
    seen: set[str] = set()
    for ext in list_extensions(config_dir):
        if not ext.enabled:
            continue
        for command in ext.manifest.commands:
            spec = _parse_extension_command(ext.manifest.name, command)
            if spec is None or spec.name in seen:
                continue
            commands.append(spec)
            seen.add(spec.name)
    return commands


def find_extension_command(
    config_dir: Path, command_name: str
) -> ExtensionCommandSpec | None:
    """Look up one enabled extension command by name or alias."""
    normalized = _normalize_command_name(command_name)
    if not normalized:
        return None
    for command in get_extension_commands(config_dir):
        if command.name == normalized or normalized in command.aliases:
            return command
    return None


def render_extension_command_prompt(
    spec: ExtensionCommandSpec,
    args: str,
) -> str:
    """Render an extension command prompt from a template."""
    formatter = Formatter()
    fields = {
        field_name
        for _, field_name, _, _ in formatter.parse(spec.prompt_template)
        if field_name
    }
    mapping = {
        "args": args.strip(),
        "raw_args": args,
        "extension": spec.extension_name,
        "command": spec.name,
    }
    try:
        rendered = spec.prompt_template.format_map(mapping)
    except (KeyError, ValueError):
        rendered = spec.prompt_template
    if args.strip() and not fields.intersection({"args", "raw_args"}):
        rendered = f"{rendered.rstrip()}\n\nArguments: {args.strip()}"
    return rendered.strip()


def _parse_extension_command(
    extension_name: str, command: dict[str, Any]
) -> ExtensionCommandSpec | None:
    raw_name = command.get("name") or command.get("command")
    name = _normalize_command_name(raw_name)
    if not name:
        return None
    template = (
        command.get("prompt") or command.get("template") or command.get("message")
    )
    if not isinstance(template, str) or not template.strip():
        return None
    description = command.get("description")
    aliases = tuple(
        alias
        for alias in (
            _normalize_command_name(value)
            for value in command.get("aliases", [])
            if isinstance(value, str)
        )
        if alias
    )
    hidden_keywords = command.get("hidden_keywords") or command.get("keywords") or ""
    return ExtensionCommandSpec(
        extension_name=extension_name,
        name=name,
        description=description.strip()
        if isinstance(description, str) and description.strip()
        else f"Extension command from {extension_name}",
        prompt_template=template,
        aliases=aliases,
        hidden_keywords=hidden_keywords if isinstance(hidden_keywords, str) else "",
    )


def _normalize_command_name(raw_name: object) -> str:
    if not isinstance(raw_name, str):
        return ""
    name = raw_name.strip().lower()
    if not name:
        return ""
    return name if name.startswith("/") else f"/{name}"
