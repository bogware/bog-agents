"""One-command import from other coding agents (ROADMAP #62).

`bog-agents plugin import <claude|codex|cursor|antigravity>` (and `/plugin
import <tool>` in the TUI) brings over what bog does *not* already read
natively, and says so for what it does:

- **skills** — Claude Code SKILL.md files → `<config>/skills/` (existing importer)
- **agents** — Claude Code `agents/*.md` → `.bog-agents/agents/<name>/AGENTS.md`
- **hooks** — user-level `~/.claude/settings.json` / `~/.cursor/hooks.json`
  → `~/.bog-agents/hooks.json` (project-level files already load natively)
- **rules** — AGENTS.md / CLAUDE.md / `.claude/rules` / `.cursor/rules` /
  `.cursorrules` are read natively; reported, never copied
- **memories** — Claude Code project memory + global CLAUDE.md, Codex global
  AGENTS.md → appended to `~/.bog-agents/memory.md` with provenance headers
- **mcp** — `~/.claude.json`, `~/.cursor/mcp.json`, `.cursor/mcp.json`,
  `~/.codex/config.toml` `[mcp_servers.*]` → the user / project `.mcp.json`

Everything is idempotent (re-running skips what is already there) and
`dry_run` reports without writing.
"""

from __future__ import annotations

import json
import logging
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SUPPORTED_TOOLS = ("claude", "codex", "cursor", "antigravity")
_IMPORT_MARK = "<!-- imported-from:"


@dataclass
class ImportReport:
    """What an import did (or would do)."""

    tool: str
    dry_run: bool = False
    sections: dict[str, list[str]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def add(self, section: str, line: str) -> None:
        """Record one line under a section."""
        self.sections.setdefault(section, []).append(line)

    @property
    def total(self) -> int:
        """Number of items imported (or that would be)."""
        return sum(len(v) for v in self.sections.values())


def format_import_report(report: ImportReport) -> str:
    """Render the report."""
    head = f"{'Would import' if report.dry_run else 'Imported'} from {report.tool}: {report.total} item(s)"
    lines = [head]
    for section, entries in report.sections.items():
        lines.append(f"  {section}:")
        lines.extend(f"    - {entry}" for entry in entries)
    if report.notes:
        lines.append("  notes:")
        lines.extend(f"    - {note}" for note in report.notes)
    return "\n".join(lines)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _merge_mcp(
    target: Path, servers: dict[str, Any], report: ImportReport, *, label: str
) -> None:
    from bog_agents_cli.claude_code_compat import _read_mcp_json, _write_mcp_json

    if not servers:
        return
    existing = _read_mcp_json(target)
    added = {
        name: cfg
        for name, cfg in servers.items()
        if name not in existing and isinstance(cfg, dict)
    }
    for name in added:
        report.add("mcp", f"{name} → {label}")
    skipped = [name for name in servers if name in existing]
    if skipped:
        report.notes.append(
            f"mcp: {', '.join(sorted(skipped))} already in {label}; kept the existing entries"
        )
    if added and not report.dry_run:
        _write_mcp_json(target, {**existing, **added})


def _merge_hooks(
    target: Path, hooks: list[dict[str, Any]], report: ImportReport, *, label: str
) -> None:
    from bog_agents_cli.io_utils import atomic_write_text

    if not hooks:
        return
    data = _read_json(target)
    current = (
        [h for h in data.get("hooks", []) if isinstance(h, dict)]
        if isinstance(data.get("hooks"), list)
        else []
    )
    fingerprints = {json.dumps(h, sort_keys=True) for h in current}
    new = [h for h in hooks if json.dumps(h, sort_keys=True) not in fingerprints]
    for hook in new:
        report.add(
            "hooks",
            f"{' '.join(map(str, hook.get('command', [])))[:60]} ({', '.join(hook.get('events', [])) or 'all events'}) → {label}",
        )
    if new and not report.dry_run:
        data["hooks"] = [*current, *new]
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(target, json.dumps(data, indent=2))


def _append_memory(
    target: Path, source: Path, report: ImportReport, *, label: str
) -> None:
    from bog_agents_cli.io_utils import atomic_write_text

    try:
        text = source.read_text(encoding="utf-8").strip()
    except OSError:
        return
    if not text:
        return
    marker = f"{_IMPORT_MARK} {label} -->"
    try:
        existing = target.read_text(encoding="utf-8") if target.is_file() else ""
    except OSError:
        existing = ""
    if marker in existing:
        report.notes.append(f"memory: {label} already imported")
        return
    report.add("memories", f"{label} → {target.name}")
    if not report.dry_run:
        block = f"\n\n{marker}\n## Imported from {label}\n\n{text}\n"
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(
            target, existing.rstrip("\n") + block if existing else block.lstrip("\n")
        )


def _import_agents(
    agent_files: list[Path], dest_dir: Path, report: ImportReport, *, label: str
) -> None:
    for path in agent_files:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        name = path.stem
        match = re.search(r"^name:\s*(.+)$", text, re.MULTILINE)
        if match:
            name = match.group(1).strip().strip("\"'") or name
        name = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-") or path.stem
        target = dest_dir / name / "AGENTS.md"
        if target.is_file():
            report.notes.append(f"agents: {name} already exists at {target}")
            continue
        if not text.startswith("---"):
            text = (
                f"---\nname: {name}\ndescription: imported from {label}\n---\n\n{text}"
            )
        report.add("agents", f"{name} → {target}")
        if not report.dry_run:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")


def _codex_mcp_servers(config_toml: Path) -> dict[str, Any]:
    try:
        data = tomllib.loads(config_toml.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    servers = data.get("mcp_servers")
    if not isinstance(servers, dict):
        return {}
    out: dict[str, Any] = {}
    for name, cfg in servers.items():
        if not isinstance(cfg, dict):
            continue
        entry: dict[str, Any] = {}
        if cfg.get("command"):
            entry["command"] = cfg["command"]
            entry["args"] = list(cfg.get("args", []))
        if cfg.get("url"):
            entry["url"] = cfg["url"]
        if isinstance(cfg.get("env"), dict):
            entry["env"] = dict(cfg["env"])
        if entry:
            out[str(name)] = entry
    return out


def import_from_tool(
    tool: str,
    *,
    project_root: Path,
    config_dir: Path,
    home: Path | None = None,
    assistant_id: str = "agent",
    dry_run: bool = False,
) -> ImportReport:
    """Import skills, agents, hooks, memories and MCP servers from `tool`.

    Args:
        tool: `claude`, `codex`, `cursor` or `antigravity`.
        project_root: The workspace to read project-level files from.
        config_dir: bog's config dir (`~/.bog-agents`).
        home: Home directory override (tests).
        assistant_id: The bog agent whose user-level agents dir receives
            user-level custom agents.
        dry_run: Report only.

    Returns:
        The `ImportReport`.
    """
    home = home or Path.home()
    tool = tool.strip().lower()
    report = ImportReport(tool=tool, dry_run=dry_run)
    if tool not in SUPPORTED_TOOLS:
        report.notes.append(
            f"unknown tool {tool!r}; supported: {', '.join(SUPPORTED_TOOLS)}"
        )
        return report
    if tool == "antigravity":
        report.notes.append(
            "antigravity: no documented on-disk layout to read yet; export its skills to a folder and run `/plugin install <dir>`"
        )
        return report
    from bog_agents_cli.hook_decisions import _load_claude_hooks

    user_mcp = config_dir / ".mcp.json"
    project_mcp = project_root / ".mcp.json"
    memory_file = config_dir / "memory.md"
    hooks_file = config_dir / "hooks.json"

    if tool == "claude":
        from bog_agents_cli.claude_code_compat import (
            detect_claude_skills,
            import_claude_skill,
        )

        skills_dir = config_dir / "skills"
        for skill in detect_claude_skills(project_root):
            dest = skills_dir / f"{skill.name}.md"
            if dest.is_file():
                report.notes.append(f"skills: {skill.name} already imported")
                continue
            report.add("skills", f"{skill.name} → {dest}")
            if not dry_run:
                skills_dir.mkdir(parents=True, exist_ok=True)
                import_claude_skill(skill, skills_dir)
        _import_agents(
            sorted((project_root / ".claude" / "agents").glob("*.md")),
            project_root / ".bog-agents" / "agents",
            report,
            label="Claude Code (project)",
        )
        _import_agents(
            sorted((home / ".claude" / "agents").glob("*.md")),
            config_dir / assistant_id / "agents",
            report,
            label="Claude Code (user)",
        )
        _merge_hooks(
            hooks_file,
            _load_claude_hooks(home / ".claude" / "settings.json"),
            report,
            label="~/.bog-agents/hooks.json",
        )
        if (project_root / ".claude" / "settings.json").is_file():
            report.notes.append(
                "hooks: .claude/settings.json loads natively (nothing to copy)"
            )
        for name in ("CLAUDE.md", "CLAUDE.local.md"):
            if (project_root / name).is_file():
                report.notes.append(f"rules: {name} is read natively")
        if (project_root / ".claude" / "rules").is_dir():
            report.notes.append("rules: .claude/rules/ is read natively")
        if (home / ".claude" / "CLAUDE.md").is_file():
            _append_memory(
                memory_file,
                home / ".claude" / "CLAUDE.md",
                report,
                label="Claude Code ~/.claude/CLAUDE.md",
            )
        for mem in sorted((home / ".claude" / "projects").glob("*/memory/*.md")):
            _append_memory(
                memory_file,
                mem,
                report,
                label=f"Claude Code memory {mem.parent.parent.name}/{mem.name}",
            )
        _merge_mcp(
            user_mcp,
            _read_json(home / ".claude.json").get("mcpServers", {}),
            report,
            label="~/.bog-agents/.mcp.json",
        )
        if project_mcp.is_file():
            report.notes.append("mcp: .mcp.json is shared with Claude Code natively")
    elif tool == "cursor":
        for name in (".cursorrules",):
            if (project_root / name).is_file():
                report.notes.append(f"rules: {name} is read natively")
        if (project_root / ".cursor" / "rules").is_dir():
            report.notes.append("rules: .cursor/rules/ (.md/.mdc) is read natively")
        if (project_root / ".cursor" / "hooks.json").is_file():
            report.notes.append("hooks: .cursor/hooks.json loads natively")
        _merge_hooks(
            hooks_file,
            _load_claude_hooks(home / ".cursor" / "hooks.json"),
            report,
            label="~/.bog-agents/hooks.json",
        )
        _merge_mcp(
            user_mcp,
            _read_json(home / ".cursor" / "mcp.json").get("mcpServers", {}),
            report,
            label="~/.bog-agents/.mcp.json",
        )
        _merge_mcp(
            project_mcp,
            _read_json(project_root / ".cursor" / "mcp.json").get("mcpServers", {}),
            report,
            label=".mcp.json",
        )
    elif tool == "codex":
        if (project_root / "AGENTS.md").is_file():
            report.notes.append("rules: AGENTS.md is read natively")
        if (home / ".codex" / "AGENTS.md").is_file():
            _append_memory(
                memory_file,
                home / ".codex" / "AGENTS.md",
                report,
                label="Codex ~/.codex/AGENTS.md",
            )
        _merge_mcp(
            user_mcp,
            _codex_mcp_servers(home / ".codex" / "config.toml"),
            report,
            label="~/.bog-agents/.mcp.json",
        )
    if report.total == 0 and not report.notes:
        report.notes.append(f"nothing found for {tool} under {home} / {project_root}")
    return report
