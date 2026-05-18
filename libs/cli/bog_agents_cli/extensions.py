"""Extension system for distributing skills, tools, hooks, and themes.

Feature #18: Plugin/extension marketplace — distributable extension packages.
Feature #32: Extension manifest system — package format for distributing
skills + tools + hooks together.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess  # noqa: S404
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Extension manifest filename
MANIFEST_FILENAME = "bog-agents-extension.json"


@dataclass
class ExtensionManifest:
    """Manifest for a Bog Agents extension package."""

    name: str
    """Extension name (unique identifier)."""

    version: str
    """Semantic version string."""

    description: str = ""
    """Human-readable description."""

    author: str = ""
    """Extension author."""

    license: str = ""
    """License identifier."""

    homepage: str = ""
    """Homepage or repository URL."""

    skills: list[str] = field(default_factory=list)
    """Relative paths to skill files within the extension."""

    hooks: list[dict[str, Any]] = field(default_factory=list)
    """Hook definitions to register."""

    mcp_servers: list[dict[str, Any]] = field(default_factory=list)
    """MCP server configurations."""

    commands: list[dict[str, Any]] = field(default_factory=list)
    """Custom slash command definitions."""

    agents: list[dict[str, Any]] = field(default_factory=list)
    """Sub-agent definitions."""

    settings: dict[str, Any] = field(default_factory=dict)
    """Extension-specific settings with defaults."""

    dependencies: list[str] = field(default_factory=list)
    """Python package dependencies."""

    compatibility: str = ""
    """Minimum Bog Agents CLI version."""


@dataclass
class InstalledExtension:
    """An installed extension."""

    manifest: ExtensionManifest
    """The extension manifest."""

    install_path: Path
    """Directory where the extension is installed."""

    enabled: bool = True
    """Whether the extension is currently enabled."""

    source: str = ""
    """Where the extension was installed from (URL, path, etc.)."""


def parse_manifest(manifest_path: Path) -> ExtensionManifest:
    """Parse an extension manifest file.

    Args:
        manifest_path: Path to the manifest JSON file.

    Returns:
        Parsed ExtensionManifest.

    Raises:
        ValueError: If the manifest is invalid.
    """
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        msg = f"Failed to read manifest: {e}"
        raise ValueError(msg) from e

    if "name" not in data or "version" not in data:
        msg = "Manifest must have 'name' and 'version' fields"
        raise ValueError(msg)

    return ExtensionManifest(
        name=data["name"],
        version=data["version"],
        description=data.get("description", ""),
        author=data.get("author", ""),
        license=data.get("license", ""),
        homepage=data.get("homepage", ""),
        skills=data.get("skills", []),
        hooks=data.get("hooks", []),
        mcp_servers=data.get("mcp_servers", []),
        commands=data.get("commands", []),
        agents=data.get("agents", []),
        settings=data.get("settings", {}),
        dependencies=data.get("dependencies", []),
        compatibility=data.get("compatibility", ""),
    )


def get_extensions_dir(config_dir: Path, *, create: bool = True) -> Path:
    """Get the extensions installation directory.

    Args:
        config_dir: Base config directory.
        create: When `True`, create the directory if missing.

    Returns:
        Path to extensions directory.
    """
    ext_dir = config_dir / "extensions"
    if create:
        ext_dir.mkdir(parents=True, exist_ok=True)
    return ext_dir


def list_extensions(config_dir: Path | None) -> list[InstalledExtension]:
    """List all installed extensions.

    Args:
        config_dir: Base config directory. Returns an empty list when ``None``.

    Returns:
        List of installed extensions.
    """
    if config_dir is None:
        return []
    ext_dir = get_extensions_dir(config_dir, create=False)
    if not ext_dir.exists():
        return []
    extensions = []

    try:
        ext_paths = sorted(ext_dir.iterdir())
    except OSError:
        logger.warning(
            "Could not read extensions directory: %s", ext_dir, exc_info=True
        )
        return []

    for ext_path in ext_paths:
        if not ext_path.is_dir():
            continue

        manifest_path = ext_path / MANIFEST_FILENAME
        if not manifest_path.exists():
            continue

        try:
            manifest = parse_manifest(manifest_path)
            # Check if disabled
            disabled_marker = ext_path / ".disabled"
            extensions.append(
                InstalledExtension(
                    manifest=manifest,
                    install_path=ext_path,
                    enabled=not disabled_marker.exists(),
                )
            )
        except ValueError as e:
            logger.warning("Invalid extension at %s: %s", ext_path, e)

    return extensions


def install_extension(
    config_dir: Path,
    source: str,
) -> InstalledExtension:
    """Install an extension from a source (local path or git URL).

    Args:
        config_dir: Base config directory.
        source: Local path or git URL to install from.

    Returns:
        The installed extension.

    Raises:
        ValueError: If installation fails.
    """
    ext_dir = get_extensions_dir(config_dir)
    source_path = Path(source)

    if source_path.is_dir():
        # Local installation
        manifest_path = source_path / MANIFEST_FILENAME
        if not manifest_path.exists():
            msg = f"No {MANIFEST_FILENAME} found in {source}"
            raise ValueError(msg)

        manifest = parse_manifest(manifest_path)
        install_path = ext_dir / manifest.name

        if install_path.exists():
            shutil.rmtree(install_path)

        shutil.copytree(source_path, install_path)

    elif source.startswith(("http://", "https://", "git@")):
        # Git clone
        git_path = shutil.which("git")
        if not git_path:
            msg = "git is required to install extensions from URLs"
            raise ValueError(msg)

        # Clone to temp location first
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(  # noqa: S603
                [git_path, "clone", "--depth=1", source, tmp + "/ext"],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            if result.returncode != 0:
                msg = f"Failed to clone {source}: {result.stderr}"
                raise ValueError(msg)

            cloned_path = Path(tmp) / "ext"
            manifest_path = cloned_path / MANIFEST_FILENAME
            if not manifest_path.exists():
                msg = f"No {MANIFEST_FILENAME} found in {source}"
                raise ValueError(msg)

            manifest = parse_manifest(manifest_path)
            install_path = ext_dir / manifest.name

            if install_path.exists():
                shutil.rmtree(install_path)

            shutil.copytree(cloned_path, install_path)
    else:
        msg = f"Invalid source: {source}. Provide a local path or git URL."
        raise ValueError(msg)

    # Install Python dependencies if any
    if manifest.dependencies:
        try:
            subprocess.run(  # noqa: S603
                ["uv", "pip", "install", *manifest.dependencies],
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            logger.warning("Could not install dependencies for %s", manifest.name)

    return InstalledExtension(
        manifest=manifest,
        install_path=install_path,
        enabled=True,
        source=source,
    )


def uninstall_extension(config_dir: Path, name: str) -> bool:
    """Uninstall an extension.

    Args:
        config_dir: Base config directory.
        name: Extension name to uninstall.

    Returns:
        True if the extension was found and removed.
    """
    ext_dir = get_extensions_dir(config_dir)
    ext_path = ext_dir / name

    if ext_path.exists():
        shutil.rmtree(ext_path)
        return True
    return False


def enable_extension(config_dir: Path, name: str) -> bool:
    """Enable a disabled extension.

    Args:
        config_dir: Base config directory.
        name: Extension name to enable.

    Returns:
        True if the extension was found and enabled.
    """
    ext_dir = get_extensions_dir(config_dir)
    disabled_marker = ext_dir / name / ".disabled"

    if disabled_marker.exists():
        disabled_marker.unlink()
        return True
    return False


def disable_extension(config_dir: Path, name: str) -> bool:
    """Disable an extension without uninstalling.

    Args:
        config_dir: Base config directory.
        name: Extension name to disable.

    Returns:
        True if the extension was found and disabled.
    """
    ext_dir = get_extensions_dir(config_dir)
    ext_path = ext_dir / name

    if ext_path.exists():
        (ext_path / ".disabled").touch()
        return True
    return False


def format_extensions_list(extensions: list[InstalledExtension]) -> str:
    """Format extensions list for display.

    Args:
        extensions: List of installed extensions.

    Returns:
        Formatted string.
    """
    if not extensions:
        return "No extensions installed.\n\nInstall with: /extensions install <path-or-url>"

    lines = ["## Installed Extensions\n"]
    for ext in extensions:
        status = "enabled" if ext.enabled else "disabled"
        lines.append(f"- **{ext.manifest.name}** v{ext.manifest.version} [{status}]")
        if ext.manifest.description:
            lines.append(f"  {ext.manifest.description}")
        if ext.manifest.skills:
            lines.append(f"  Skills: {len(ext.manifest.skills)}")
        if ext.manifest.hooks:
            lines.append(f"  Hooks: {len(ext.manifest.hooks)}")
        if ext.manifest.commands:
            lines.append(f"  Commands: {len(ext.manifest.commands)}")

    return "\n".join(lines)
