"""Claude Code plugin compatibility layer.

Provides bidirectional compatibility between bog-agents plugins/extensions
and the Claude Code ecosystem:

- Detect and import Claude Code skills from ``.claude/`` directories
- Parse Claude Code's SKILL.md format (name/description/version frontmatter)
- Sync MCP server configs between ``.mcp.json`` and Claude Code desktop config
- Convert bog-agents extensions to Claude Code compatible format
- Auto-discover Claude Desktop app MCP configurations

Claude Code plugin locations scanned:
    .claude/skills/        — project-level skills (SKILL.md files)
    ~/.claude/skills/      — user-level skills
    .claude/plugins/       — project-level plugin manifests
    ~/.claude/plugins/     — user-level plugin manifests
    .mcp.json              — project MCP servers (shared with Claude Code)
    ~/.bog-agents/.mcp.json — user MCP servers

Usage::

    from bog_agents_cli.claude_code_compat import (
        detect_claude_skills,
        import_claude_skill,
        sync_mcp_configs,
        get_claude_compat_status,
    )
"""

from __future__ import annotations

import json
import logging
import platform
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_CLAUDE_CODE_SKILL_DIRS: list[str] = [
    ".claude/skills",
    ".claude/commands",
    ".claude",
]
_USER_CLAUDE_DIRS: list[str] = [
    "~/.claude/skills",
    "~/.claude/commands",
    "~/.claude",
]
_MCP_CONFIG_FILENAMES: list[str] = [".mcp.json", ".bog-agents/.mcp.json"]
_BOG_AGENTS_EXTENSION_MANIFEST = "bog-agents-extension.json"
_PLUGIN_MANIFEST = "manifest.json"
_SKILL_MD_NAMES = ("SKILL.md", "skill.md", "Skill.md")


def _claude_desktop_config_path() -> Path | None:
    """Return the Claude Desktop MCP config path for the current platform."""
    system = platform.system()
    if system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    if system == "Linux":
        xdg = Path(
            __import__("os").environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
        )
        return xdg / "Claude" / "claude_desktop_config.json"
    if system == "Windows":
        appdata = Path(__import__("os").environ.get("APPDATA", ""))
        return appdata / "Claude" / "claude_desktop_config.json" if appdata.name else None
    return None


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class ClaudeSkill:
    """A skill loaded from a Claude Code-style SKILL.md file."""

    name: str
    description: str
    version: str
    author: str
    source_path: Path
    body: str
    frontmatter: dict[str, Any] = field(default_factory=dict)

    @property
    def compatible_tools(self) -> list[str]:
        """Return list of compatible tools from frontmatter."""
        tools = self.frontmatter.get("tools", [])
        return tools if isinstance(tools, list) else []


@dataclass
class ClaudeCompatStatus:
    """Claude Code compatibility status report."""

    claude_code_installed: bool
    claude_desktop_installed: bool
    claude_skills_found: list[ClaudeSkill]
    mcp_servers_in_project: dict[str, Any]
    mcp_servers_in_desktop: dict[str, Any]
    bog_agents_plugins_claude_compat: int
    project_root: Path


@dataclass
class MCPSyncResult:
    """Result of MCP configuration sync."""

    added_to_mcp_json: list[str]
    added_from_desktop: list[str]
    errors: list[str]
    output_path: Path | None = None


# ---------------------------------------------------------------------------
# SKILL.md parsing
# ---------------------------------------------------------------------------


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Parse YAML-style frontmatter from a SKILL.md document.

    Args:
        text: Raw file content.

    Returns:
        (frontmatter_dict, body_text) tuple. frontmatter_dict is empty
        if no ``---`` block is found.
    """
    if not text.startswith("---"):
        return {}, text

    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text

    fm: dict[str, Any] = {}
    for line in parts[1].strip().splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        # Crude list parsing: [a, b, c]
        if value.startswith("[") and value.endswith("]"):
            items = [v.strip().strip("'\"") for v in value[1:-1].split(",") if v.strip()]
            fm[key] = items
        else:
            # Remove surrounding quotes
            fm[key] = value.strip("'\"")

    return fm, parts[2].strip()


def parse_skill_md(path: Path) -> ClaudeSkill | None:
    """Parse a SKILL.md file into a ClaudeSkill.

    Args:
        path: Path to the SKILL.md file.

    Returns:
        ClaudeSkill or None if the file cannot be read/parsed.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        logger.debug("Cannot read skill file: %s", path)
        return None

    fm, body = _parse_frontmatter(text)
    name = fm.get("name", path.stem)
    description = fm.get("description", "")
    version = str(fm.get("version", "0.1.0"))
    author = fm.get("author", "")

    return ClaudeSkill(
        name=name,
        description=description,
        version=version,
        author=author,
        source_path=path,
        body=body,
        frontmatter=fm,
    )


# ---------------------------------------------------------------------------
# Skill discovery
# ---------------------------------------------------------------------------


def detect_claude_skills(project_root: Path) -> list[ClaudeSkill]:
    """Scan known Claude Code skill locations and return all found skills.

    Searches:
    - ``<project_root>/.claude/skills/`` and ``<project_root>/.claude/``
    - ``~/.claude/skills/`` and ``~/.claude/``

    Args:
        project_root: Root directory of the current project.

    Returns:
        List of ClaudeSkill objects, deduplicated by source path.
    """
    skills: list[ClaudeSkill] = []
    seen: set[Path] = set()

    search_dirs: list[Path] = []
    for rel in _CLAUDE_CODE_SKILL_DIRS:
        search_dirs.append(project_root / rel)
    for rel in _USER_CLAUDE_DIRS:
        search_dirs.append(Path(rel).expanduser())

    for d in search_dirs:
        if not d.is_dir():
            continue
        for skill_name in _SKILL_MD_NAMES:
            # Direct file in the directory
            direct = d / skill_name
            if direct.is_file() and direct not in seen:
                skill = parse_skill_md(direct)
                if skill:
                    skills.append(skill)
                    seen.add(direct)
        # Subdirectories containing SKILL.md
        for subdir in d.iterdir():
            if not subdir.is_dir():
                continue
            for skill_name in _SKILL_MD_NAMES:
                skill_path = subdir / skill_name
                if skill_path.is_file() and skill_path not in seen:
                    skill = parse_skill_md(skill_path)
                    if skill:
                        skills.append(skill)
                        seen.add(skill_path)

    return skills


def import_claude_skill(skill: ClaudeSkill, skills_dir: Path) -> Path:
    """Copy a Claude Code skill into the bog-agents skills directory.

    Args:
        skill: The skill to import.
        skills_dir: Destination directory (``~/.bog-agents/skills/``).

    Returns:
        Path to the imported file.
    """
    skills_dir.mkdir(parents=True, exist_ok=True)
    dest = skills_dir / f"{skill.name}.md"
    shutil.copy2(skill.source_path, dest)
    logger.info("Imported Claude skill '%s' to %s", skill.name, dest)
    return dest


# ---------------------------------------------------------------------------
# MCP configuration sync
# ---------------------------------------------------------------------------


def _read_mcp_json(path: Path) -> dict[str, Any]:
    """Read and return the mcpServers dict from a .mcp.json file."""
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data.get("mcpServers", {})
    except (json.JSONDecodeError, OSError):
        return {}


def _write_mcp_json(path: Path, servers: dict[str, Any]) -> None:
    """Write or update a .mcp.json file with the given server map."""
    existing: dict[str, Any] = {}
    if path.is_file():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    existing["mcpServers"] = servers
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(existing, indent=2), encoding="utf-8")


def get_project_mcp_servers(project_root: Path) -> dict[str, Any]:
    """Return MCP servers from the project .mcp.json.

    Args:
        project_root: Project root directory.

    Returns:
        Dict of server name → config.
    """
    for filename in _MCP_CONFIG_FILENAMES:
        path = project_root / filename
        servers = _read_mcp_json(path)
        if servers:
            return servers
    return {}


def get_claude_desktop_mcp_servers() -> dict[str, Any]:
    """Return MCP servers from Claude Desktop's config file.

    Args:
        None

    Returns:
        Dict of server name → config, or {} if not found.
    """
    config_path = _claude_desktop_config_path()
    if config_path is None or not config_path.is_file():
        return {}
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
        return data.get("mcpServers", {})
    except (json.JSONDecodeError, OSError):
        return {}


def sync_mcp_configs(
    project_root: Path,
    *,
    direction: str = "both",
    dry_run: bool = False,
) -> MCPSyncResult:
    """Synchronise MCP server configs between project .mcp.json and Claude Desktop.

    Merges server definitions in the specified direction without overwriting
    entries that already exist in the target.

    Args:
        project_root: Project root directory.
        direction: ``"both"`` (default), ``"to-desktop"``, or ``"from-desktop"``.
        dry_run: If True, compute the diff but do not write anything.

    Returns:
        MCPSyncResult describing what was added and any errors.
    """
    result = MCPSyncResult(added_to_mcp_json=[], added_from_desktop=[], errors=[])

    project_path = project_root / ".mcp.json"
    project_servers = _read_mcp_json(project_path)
    desktop_servers = get_claude_desktop_mcp_servers()

    if direction in {"both", "from-desktop"}:
        to_add: dict[str, Any] = {}
        for name, cfg in desktop_servers.items():
            if name not in project_servers:
                to_add[name] = cfg
                result.added_to_mcp_json.append(name)
        if to_add and not dry_run:
            merged = {**project_servers, **to_add}
            try:
                _write_mcp_json(project_path, merged)
                result.output_path = project_path
            except OSError as exc:
                result.errors.append(f"Cannot write {project_path}: {exc}")

    if direction in {"both", "to-desktop"}:
        desktop_path = _claude_desktop_config_path()
        if desktop_path is None:
            result.errors.append("Claude Desktop config path unknown for this platform.")
        else:
            to_add_desktop: dict[str, Any] = {}
            for name, cfg in project_servers.items():
                if name not in desktop_servers:
                    to_add_desktop[name] = cfg
                    result.added_from_desktop.append(name)
            if to_add_desktop and not dry_run:
                merged_desktop = {**desktop_servers, **to_add_desktop}
                try:
                    _write_mcp_json(desktop_path, merged_desktop)
                except OSError as exc:
                    result.errors.append(f"Cannot write Claude Desktop config: {exc}")

    return result


# ---------------------------------------------------------------------------
# Extension / plugin → Claude Code format
# ---------------------------------------------------------------------------


def export_mcp_from_extensions(
    config_dir: Path,
    project_root: Path,
    *,
    dry_run: bool = False,
) -> MCPSyncResult:
    """Extract MCP server configs from installed extensions/plugins and write to .mcp.json.

    Scans bog-agents extensions in *config_dir* for ``mcp_servers`` entries
    and merges them into the project ``.mcp.json``.

    Args:
        config_dir: The bog-agents config directory (``~/.bog-agents/``).
        project_root: Project root for writing ``.mcp.json``.
        dry_run: If True, compute but do not write.

    Returns:
        MCPSyncResult.
    """
    result = MCPSyncResult(added_to_mcp_json=[], added_from_desktop=[], errors=[])

    project_path = project_root / ".mcp.json"
    existing = _read_mcp_json(project_path)
    to_add: dict[str, Any] = {}

    for manifest_file in _EXTENSION_MANIFEST_PATHS(config_dir):
        try:
            data = json.loads(manifest_file.read_text(encoding="utf-8"))
            for srv in data.get("mcp_servers", []):
                if isinstance(srv, dict) and "name" in srv:
                    name = srv["name"]
                    if name not in existing and name not in to_add:
                        to_add[name] = {k: v for k, v in srv.items() if k != "name"}
                        result.added_to_mcp_json.append(name)
        except (json.JSONDecodeError, OSError, KeyError):
            continue

    if to_add and not dry_run:
        merged = {**existing, **to_add}
        try:
            _write_mcp_json(project_path, merged)
            result.output_path = project_path
        except OSError as exc:
            result.errors.append(f"Cannot write {project_path}: {exc}")

    return result


def _EXTENSION_MANIFEST_PATHS(config_dir: Path):  # noqa: N802
    """Yield all manifest files under config_dir."""
    for extensions_dir in (config_dir / "extensions", config_dir / "plugins"):
        if not extensions_dir.is_dir():
            continue
        for subdir in extensions_dir.iterdir():
            if not subdir.is_dir():
                continue
            for name in (_BOG_AGENTS_EXTENSION_MANIFEST, _PLUGIN_MANIFEST):
                manifest = subdir / name
                if manifest.is_file():
                    yield manifest
                    break


# ---------------------------------------------------------------------------
# Status / compatibility report
# ---------------------------------------------------------------------------


def get_claude_compat_status(project_root: Path, config_dir: Path) -> ClaudeCompatStatus:
    """Build a comprehensive Claude Code compatibility report.

    Args:
        project_root: Project root directory.
        config_dir: Bog-agents config directory.

    Returns:
        ClaudeCompatStatus with all discovered information.
    """
    import importlib.util

    claude_code_installed = (
        shutil.which("claude") is not None
        or importlib.util.find_spec("claude_code") is not None
    )

    desktop_path = _claude_desktop_config_path()
    claude_desktop_installed = desktop_path is not None and desktop_path.is_file()

    skills = detect_claude_skills(project_root)
    project_mcp = get_project_mcp_servers(project_root)
    desktop_mcp = get_claude_desktop_mcp_servers()

    # Count bog-agents plugins/extensions that declare Claude Code compatibility
    compat_count = 0
    for manifest_file in _EXTENSION_MANIFEST_PATHS(config_dir):
        try:
            data = json.loads(manifest_file.read_text(encoding="utf-8"))
            tools = data.get("compatible_tools", [])
            if "claude-code" in tools:
                compat_count += 1
        except (json.JSONDecodeError, OSError):
            continue

    return ClaudeCompatStatus(
        claude_code_installed=claude_code_installed,
        claude_desktop_installed=claude_desktop_installed,
        claude_skills_found=skills,
        mcp_servers_in_project=project_mcp,
        mcp_servers_in_desktop=desktop_mcp,
        bog_agents_plugins_claude_compat=compat_count,
        project_root=project_root,
    )


def format_compat_status(status: ClaudeCompatStatus) -> str:
    """Render a human-readable compatibility status report.

    Args:
        status: The status to render.

    Returns:
        Multi-line string.
    """
    def yn(flag: bool) -> str:
        return "yes" if flag else "no"

    lines = [
        "Claude Code Compatibility",
        "=" * 40,
        f"  claude CLI detected:        {yn(status.claude_code_installed)}",
        f"  Claude Desktop config:      {yn(status.claude_desktop_installed)}",
        f"  Claude skills found:        {len(status.claude_skills_found)}",
        f"  MCP servers (project):      {len(status.mcp_servers_in_project)}",
        f"  MCP servers (Claude Desktop): {len(status.mcp_servers_in_desktop)}",
        f"  Claude-compat plugins:      {status.bog_agents_plugins_claude_compat}",
        "",
    ]

    if status.claude_skills_found:
        lines.append("Skills detected:")
        for skill in status.claude_skills_found[:10]:
            lines.append(f"  {skill.name} — {skill.description} ({skill.source_path.parent.name})")
        if len(status.claude_skills_found) > 10:
            lines.append(f"  ... and {len(status.claude_skills_found) - 10} more")
        lines.append("")

    if status.mcp_servers_in_project:
        lines.append("Project MCP servers (.mcp.json):")
        for name in status.mcp_servers_in_project:
            lines.append(f"  {name}")
        lines.append("")

    if status.mcp_servers_in_desktop:
        lines.append("Claude Desktop MCP servers:")
        for name in status.mcp_servers_in_desktop:
            overlap = " (also in project)" if name in status.mcp_servers_in_project else ""
            lines.append(f"  {name}{overlap}")
        lines.append("")

    lines.append("Commands:")
    lines.append("  /plugin claude           — this status view")
    lines.append("  /plugin claude-import    — import Claude skills into bog-agents")
    lines.append("  /plugin sync-mcp         — sync MCP configs between project and Claude Desktop")
    lines.append("  /plugin export-mcp       — export extension MCP servers to .mcp.json")

    return "\n".join(lines)
