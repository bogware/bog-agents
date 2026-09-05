"""Agent Plugins 1.0 (`plugin.json`) support (ROADMAP #62).

The cross-vendor plugin layout (spec of Aug 6, 2026; GA in Copilot, Kiro,
OpenHands and Cline) is a directory with a `plugin.json` at its root and
well-known subdirectories:

    plugin.json              name / version / description / author / homepage
    skills/<name>/SKILL.md   or skills/<name>.md
    agents/<name>.md         frontmatter name/description/model + body
    commands/<name>.md       frontmatter name/description + body (prompt template)
    hooks/*.json             Claude-style hook maps or bog `{"hooks": [...]}`
    mcp.json                 `{"mcpServers": {...}}`

This module reads that layout into bog's `ExtensionManifest` (so every
existing extension surface — skills, commands, MCP, hooks — lights up
unchanged), discovers plugins in `~/.agents/plugins` and the workspace's
`.agents/plugins` (workspace ones stay **disabled until trusted**), and keeps
the small trust store that records the user's decision.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bog_agents_cli.extensions import ExtensionManifest

logger = logging.getLogger(__name__)

PLUGIN_JSON = "plugin.json"
TRUST_STORE_NAME = "plugin_trust.json"
_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True)
class PluginSpec:
    """A parsed Agent Plugins 1.0 directory."""

    root: Path
    name: str
    version: str = "0.0.0"
    description: str = ""
    author: str = ""
    homepage: str = ""
    skills: tuple[str, ...] = ()
    agents: tuple[str, ...] = ()
    commands: tuple[str, ...] = ()
    hooks: tuple[str, ...] = ()
    mcp_servers: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentPlugin:
    """A discovered plugin with its scope and trust state."""

    spec: PluginSpec
    scope: str  # "user" | "workspace" | "installed"
    trusted: bool

    @property
    def enabled(self) -> bool:
        """Workspace plugins run only once trusted; user/installed ones always."""
        return self.scope != "workspace" or self.trusted


def safe_plugin_name(name: str) -> str:
    """Restrict a plugin name to a single path-safe component."""
    cleaned = _NAME_RE.sub("-", name.strip()).strip("-.")
    return cleaned[:80] or "plugin"


def _frontmatter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---"):
        return {}, text
    parts = text.split("\n---", 2)
    if len(parts) < 2:
        return {}, text
    header = parts[0][3:]
    body = parts[1].lstrip("\n") if len(parts) > 1 else ""
    if len(parts) == 3:
        body = (
            parts[1].lstrip("\n") + "\n---" + parts[2]
            if False
            else parts[1].lstrip("\n")
        )
    meta: dict[str, Any] = {}
    for line in header.splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            meta[key.strip()] = value.strip().strip("\"'")
    return meta, body


def load_plugin_spec(root: Path) -> PluginSpec | None:
    """Parse `root/plugin.json` and scan the well-known subdirectories.

    Args:
        root: The plugin directory.

    Returns:
        The spec, or `None` when there is no readable `plugin.json` with a name.
    """
    manifest = root / PLUGIN_JSON
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or not str(data.get("name", "")).strip():
        return None
    skills: list[str] = []
    skills_dir = root / "skills"
    if skills_dir.is_dir():
        for entry in sorted(skills_dir.iterdir()):
            if entry.is_dir() and (entry / "SKILL.md").is_file():
                skills.append(
                    str((entry / "SKILL.md").relative_to(root)).replace("\\", "/")
                )
            elif entry.is_file() and entry.suffix.lower() == ".md":
                skills.append(str(entry.relative_to(root)).replace("\\", "/"))
    agents = (
        tuple(
            str(p.relative_to(root)).replace("\\", "/")
            for p in sorted((root / "agents").glob("*.md"))
        )
        if (root / "agents").is_dir()
        else ()
    )
    commands = (
        tuple(
            str(p.relative_to(root)).replace("\\", "/")
            for p in sorted((root / "commands").glob("*.md"))
        )
        if (root / "commands").is_dir()
        else ()
    )
    hooks: list[str] = []
    if (root / "hooks").is_dir():
        hooks.extend(
            str(p.relative_to(root)).replace("\\", "/")
            for p in sorted((root / "hooks").glob("*.json"))
        )
    if (root / "hooks.json").is_file():
        hooks.append("hooks.json")
    mcp: dict[str, Any] = {}
    mcp_path = root / "mcp.json"
    if mcp_path.is_file():
        try:
            raw = json.loads(mcp_path.read_text(encoding="utf-8"))
            servers = raw.get("mcpServers", raw) if isinstance(raw, dict) else {}
            mcp = {str(k): v for k, v in servers.items() if isinstance(v, dict)}
        except (OSError, json.JSONDecodeError):
            logger.debug("plugin %s: unreadable mcp.json", root, exc_info=True)
    return PluginSpec(
        root=root,
        name=str(data.get("name")).strip(),
        version=str(data.get("version") or "0.0.0"),
        description=str(data.get("description") or ""),
        author=str(data.get("author") or ""),
        homepage=str(data.get("homepage") or data.get("repository") or ""),
        skills=tuple(skills),
        agents=agents,
        commands=commands,
        hooks=tuple(hooks),
        mcp_servers=mcp,
    )


def _load_hook_file(path: Path) -> list[dict[str, Any]]:
    """Read a bog `{"hooks": [...]}` file or a Claude/Cursor hook map into bog hook dicts."""
    from bog_agents_cli.hook_decisions import _load_claude_hooks

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if (
        isinstance(raw, dict)
        and isinstance(raw.get("hooks"), list)
        and all(isinstance(h, dict) and "command" in h for h in raw["hooks"])
    ):
        return [h for h in raw["hooks"] if isinstance(h.get("command"), (list, str))]
    return _load_claude_hooks(path)


def to_extension_manifest(spec: PluginSpec) -> ExtensionManifest:
    """Map a plugin spec onto bog's `ExtensionManifest` (skills, commands, agents, hooks, MCP)."""
    commands: list[dict[str, Any]] = []
    for rel in spec.commands:
        path = spec.root / rel
        try:
            meta, body = _frontmatter(path.read_text(encoding="utf-8"))
        except OSError:
            continue
        name = str(meta.get("name") or path.stem)
        if body.strip():
            commands.append(
                {
                    "name": name,
                    "description": str(
                        meta.get("description") or f"{spec.name} command"
                    ),
                    "prompt": body.strip(),
                }
            )
    agents: list[dict[str, Any]] = []
    for rel in spec.agents:
        path = spec.root / rel
        try:
            meta, _body = _frontmatter(path.read_text(encoding="utf-8"))
        except OSError:
            continue
        agents.append(
            {
                "name": str(meta.get("name") or path.stem),
                "description": str(meta.get("description") or ""),
                "path": rel,
            }
        )
    hooks: list[dict[str, Any]] = []
    for rel in spec.hooks:
        hooks.extend(_load_hook_file(spec.root / rel))
    mcp_servers = [
        {"name": name, **config} for name, config in spec.mcp_servers.items()
    ]
    return ExtensionManifest(
        name=spec.name,
        version=spec.version,
        description=spec.description,
        author=spec.author,
        homepage=spec.homepage,
        skills=list(spec.skills),
        hooks=hooks,
        mcp_servers=mcp_servers,
        commands=commands,
        agents=agents,
    )


# ------------------------------------------------------------------ trust store


def trust_store_path(config_dir: Path) -> Path:
    """`<config_dir>/plugin_trust.json`."""
    return config_dir / TRUST_STORE_NAME


def _read_trust(store: Path) -> dict[str, Any]:
    try:
        data = json.loads(store.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _key(root: Path) -> str:
    return str(root.resolve()).replace("\\", "/").lower()


def is_plugin_trusted(root: Path, *, config_dir: Path) -> bool:
    """Whether the user trusted this plugin directory."""
    return _key(root) in _read_trust(trust_store_path(config_dir)).get("trusted", {})


def trust_plugin(root: Path, *, config_dir: Path) -> None:
    """Record trust for a plugin directory (workspace plugins run only after this)."""
    from bog_agents_cli.io_utils import atomic_write_text

    store = trust_store_path(config_dir)
    data = _read_trust(store)
    trusted = data.setdefault("trusted", {})
    trusted[_key(root)] = {"name": root.name, "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    try:  # ROADMAP #64: pin the plugin's hook scripts by hash; a change untrusts its hooks
        from bog_agents_cli.hook_decisions import hook_script_hashes

        trusted[_key(root)]["hook_hashes"] = hook_script_hashes(root)
    except Exception:  # noqa: S110 - trust must still be recorded without hooks
        pass
    store.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(store, json.dumps(data, indent=2))


def revoke_plugin_trust(root: Path, *, config_dir: Path) -> bool:
    """Forget trust for a plugin directory; `True` when an entry was removed."""
    from bog_agents_cli.io_utils import atomic_write_text

    store = trust_store_path(config_dir)
    data = _read_trust(store)
    trusted = data.get("trusted", {})
    if _key(root) not in trusted:
        return False
    del trusted[_key(root)]
    atomic_write_text(store, json.dumps(data, indent=2))
    return True


# ------------------------------------------------------------------ discovery


def plugin_roots(
    *, config_dir: Path, home: Path | None = None, project_root: Path | None = None
) -> list[tuple[Path, str]]:
    """Directories to scan: `~/.agents/plugins` (user), `.agents/plugins` (workspace), `<config>/plugins` (installed)."""
    home = home or Path.home()
    roots = [
        (home / ".agents" / "plugins", "user"),
        (config_dir / "plugins", "installed"),
    ]
    if project_root is not None:
        roots.append((project_root / ".agents" / "plugins", "workspace"))
    return roots


def discover_agent_plugins(
    *, config_dir: Path, home: Path | None = None, project_root: Path | None = None
) -> list[AgentPlugin]:
    """Find every `plugin.json` plugin in the known roots, with its scope and trust."""
    found: list[AgentPlugin] = []
    seen: set[str] = set()
    for base, scope in plugin_roots(
        config_dir=config_dir, home=home, project_root=project_root
    ):
        if not base.is_dir():
            continue
        try:
            entries = sorted(p for p in base.iterdir() if p.is_dir())
        except OSError:
            continue
        for entry in entries:
            spec = load_plugin_spec(entry)
            if spec is None or _key(entry) in seen:
                continue
            seen.add(_key(entry))
            found.append(
                AgentPlugin(
                    spec=spec,
                    scope=scope,
                    trusted=scope != "workspace"
                    or is_plugin_trusted(entry, config_dir=config_dir),
                )
            )
    return found
