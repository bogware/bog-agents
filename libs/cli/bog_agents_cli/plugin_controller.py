"""TUI glue for `/plugin import|trust|untrust` and `/onboard import` (ROADMAP #62)."""

from __future__ import annotations

from pathlib import Path
from typing import Any


def _config_dir() -> Path:
    from bog_agents_cli._env_vars import bog_agents_home

    return bog_agents_home()


async def run_plugin_verb(app: Any, raw_arg: str) -> str:  # noqa: ANN401 - the App
    """Handle `import <tool> [--dry-run]`, `trust <name>`, `untrust <name>`."""
    import asyncio

    parts = raw_arg.split()
    verb = parts[0].lower()
    project = Path(getattr(app, "_cwd", ".") or ".")
    config_dir = _config_dir()
    if verb == "import":
        if len(parts) < 2:
            return "Usage: /plugin import <claude|codex|cursor|antigravity> [--dry-run]"
        from bog_agents_cli.plugin_import import format_import_report, import_from_tool

        dry = "--dry-run" in parts[2:]
        report = await asyncio.to_thread(
            import_from_tool,
            parts[1],
            project_root=project,
            config_dir=config_dir,
            dry_run=dry,
        )
        return format_import_report(report)
    if len(parts) < 2:
        return f"Usage: /plugin {verb} <name>"
    from bog_agents_cli.plugin_spec import (
        discover_agent_plugins,
        revoke_plugin_trust,
        trust_plugin,
    )

    name = parts[1]
    match = next(
        (
            p
            for p in discover_agent_plugins(config_dir=config_dir, project_root=project)
            if p.spec.name.lower() == name.lower()
        ),
        None,
    )
    if match is None:
        return f"No plugin named {name!r} under ~/.agents/plugins, {project / '.agents' / 'plugins'} or {config_dir / 'plugins'}."
    if verb == "trust":
        trust_plugin(match.spec.root, config_dir=config_dir)
        return f"Trusted {match.spec.name} ({match.spec.root}). Its skills, commands and hooks are active from the next agent build."
    revoked = revoke_plugin_trust(match.spec.root, config_dir=config_dir)
    return f"{'Revoked trust for' if revoked else 'No trust recorded for'} {match.spec.name}."


async def run_onboard_import(command: str) -> str:
    """`/onboard import <claude|codex|cline> [N] [--dry-run]` → import sessions as threads."""
    from bog_agents_cli.session_import import format_import_summary, import_sessions

    parts = command.split()
    if len(parts) < 3:
        return "Usage: /onboard import <claude|codex|cline> [limit] [--dry-run]"
    limit = 20
    for token in parts[3:]:
        if token.isdigit():
            limit = int(token)
    summary = await import_sessions(parts[2], limit=limit, dry_run="--dry-run" in parts)
    return format_import_summary(summary)


async def run_claude_compat_verb(
    app: Any,  # noqa: ANN401 - the App
    raw_arg: str,
    config_dir: Path,
) -> str | None:
    """`/plugin claude|claude-list|claude-import|sync-mcp|export-mcp`; `None` when not one of them."""
    import asyncio

    lowered = raw_arg.lower()
    cwd = Path(getattr(app, "_cwd", ".") or ".")
    if lowered in {"claude", "claude-status"}:
        from bog_agents_cli.claude_code_compat import (
            format_compat_status,
            get_claude_compat_status,
        )

        status = await asyncio.to_thread(get_claude_compat_status, cwd, config_dir)
        return format_compat_status(status)
    if lowered == "claude-import":
        from bog_agents_cli.claude_code_compat import (
            detect_claude_skills,
            import_claude_skill,
        )

        skills = await asyncio.to_thread(detect_claude_skills, cwd)
        if not skills:
            return "No Claude Code skills found in .claude/ directories.\n\nSkills are SKILL.md files in .claude/skills/ or ~/.claude/skills/."
        skills_dir = config_dir / "skills"
        imported: list[str] = []
        for skill in skills:
            dest = await asyncio.to_thread(import_claude_skill, skill, skills_dir)
            imported.append(f"  {skill.name} → {dest}")
        return (
            f"Imported {len(imported)} Claude skill(s) into bog-agents:\n"
            + "\n".join(imported)
        )
    if lowered == "claude-list":
        from bog_agents_cli.claude_code_compat import detect_claude_skills

        skills = await asyncio.to_thread(detect_claude_skills, cwd)
        if not skills:
            return "No Claude Code skills found."
        lines = [f"Claude Code skills ({len(skills)} found):", ""]
        for skill in skills:
            lines.append(f"  {skill.name} v{skill.version} — {skill.description}")
            lines.append(f"    {skill.source_path}")
        return "\n".join(lines)
    if lowered == "sync-mcp" or lowered.startswith("sync-mcp "):
        from bog_agents_cli.claude_code_compat import sync_mcp_configs

        parts = raw_arg.split()
        direction = parts[1] if len(parts) > 1 else "both"
        if direction not in {"both", "to-desktop", "from-desktop"}:
            return "Usage: /plugin sync-mcp [both|to-desktop|from-desktop]"
        result = await asyncio.to_thread(sync_mcp_configs, cwd, direction=direction)
        lines = ["MCP sync complete.", ""]
        if result.added_to_mcp_json:
            lines.append(f"Added to .mcp.json: {', '.join(result.added_to_mcp_json)}")
        if result.added_from_desktop:
            lines.append(
                f"Added from Claude Desktop: {', '.join(result.added_from_desktop)}"
            )
        if not result.added_to_mcp_json and not result.added_from_desktop:
            lines.append("Nothing to sync — configs are already in sync.")
        if result.errors:
            lines.append(f"Errors: {'; '.join(result.errors)}")
        return "\n".join(lines)
    if lowered == "export-mcp":
        from bog_agents_cli.claude_code_compat import export_mcp_from_extensions

        result = await asyncio.to_thread(export_mcp_from_extensions, config_dir, cwd)
        if result.added_to_mcp_json:
            return (
                f"Exported {len(result.added_to_mcp_json)} MCP server(s) to {result.output_path}:\n  "
                + "\n  ".join(result.added_to_mcp_json)
            )
        if result.errors:
            return f"Export errors: {'; '.join(result.errors)}"
        return "No new MCP servers to export."
    return None


async def run_compat_or_plugin_verb(
    app: Any,  # noqa: ANN401 - the App
    raw_arg: str,
    config_dir: Path,
) -> str | None:
    """Dispatch `/plugin` verbs that are not the install/enable/disable basics; `None` when unhandled."""
    lowered = raw_arg.lower()
    if lowered.startswith(("import ", "trust ", "untrust ")):
        return await run_plugin_verb(app, raw_arg)
    return await run_claude_compat_verb(app, raw_arg, config_dir)
