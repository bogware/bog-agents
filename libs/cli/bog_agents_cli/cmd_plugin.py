"""`bog-agents plugin …` and `bog-agents threads import|export …` (ROADMAP #62)."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any


def _config_dir() -> Path:
    from bog_agents_cli._env_vars import bog_agents_home

    return bog_agents_home()


def setup_plugin_parser(subparsers: Any) -> argparse.ArgumentParser:  # noqa: ANN401 - argparse subparsers
    """Register `plugin list|install|import|trust|untrust`."""
    plugin_p = subparsers.add_parser(
        "plugin",
        help="Agent Plugins 1.0: list, install (pinned), import from other tools, trust",
    )
    sub = plugin_p.add_subparsers(dest="plugin_command")
    sub.add_parser(
        "list", help="List installed plugins, extensions and discovered Agent Plugins"
    )
    install = sub.add_parser(
        "install",
        help="Install a plugin from a dir, zip, zip URL, git URL, or marketplace name",
    )
    install.add_argument(
        "source", help="Directory, .zip, URL, git URL, or a name from --marketplace"
    )
    install.add_argument(
        "--sha256",
        default=None,
        help="Pin: SHA-256 of the archive (zip/URL) or directory digest (dir/git)",
    )
    install.add_argument(
        "--marketplace",
        default=None,
        help="marketplace.json path or URL to resolve a plugin name",
    )
    imp = sub.add_parser(
        "import",
        help="Import skills/agents/hooks/memories/MCP from claude | codex | cursor | antigravity",
    )
    imp.add_argument("tool", choices=["claude", "codex", "cursor", "antigravity"])
    imp.add_argument(
        "--project", default=".", help="Project root to read project-level files from"
    )
    imp.add_argument(
        "--dry-run", dest="dry_run", action="store_true", help="Report without writing"
    )
    trust = sub.add_parser(
        "trust", help="Trust a workspace plugin (.agents/plugins/<name>) so it can run"
    )
    trust.add_argument("name")
    trust.add_argument("--project", default=".")
    untrust = sub.add_parser("untrust", help="Revoke trust for a workspace plugin")
    untrust.add_argument("name")
    untrust.add_argument("--project", default=".")
    return plugin_p


def setup_threads_transfer_parsers(threads_sub: Any) -> None:  # noqa: ANN401 - argparse subparsers
    """Register `threads import <source>` and `threads export <thread_id>`."""
    imp = threads_sub.add_parser(
        "import",
        help="Import sessions from claude | codex | cline | bog (JSONL export)",
    )
    imp.add_argument("source", choices=["claude", "codex", "cline", "bog"])
    imp.add_argument(
        "paths", nargs="*", help="Explicit transcript files (default: discover)"
    )
    imp.add_argument(
        "--limit", type=int, default=20, help="Max sessions to import (newest first)"
    )
    imp.add_argument(
        "--agent", default="agent", help="Agent name to list the threads under"
    )
    imp.add_argument("--dry-run", dest="dry_run", action="store_true")
    exp = threads_sub.add_parser(
        "export", help="Export a thread as com.bogware.thread JSONL"
    )
    exp.add_argument("thread_id")
    exp.add_argument(
        "--out", default=None, help="Output file (default: ./bog-thread-<id>.jsonl)"
    )


def _print(text: str) -> None:
    print(text)  # noqa: T201 - CLI output


def handle_plugin_command(args: argparse.Namespace) -> int:
    """Dispatch `bog-agents plugin …`; returns an exit code."""
    from bog_agents_cli.extensibility import (
        format_extensibility_list,
        list_extensibility_items,
    )

    command = getattr(args, "plugin_command", None)
    config_dir = _config_dir()
    project = Path(getattr(args, "project", ".") or ".").resolve()
    if command in (None, "list"):
        _print(format_extensibility_list(config_dir))
        agent_plugins = [
            i
            for i in list_extensibility_items(config_dir, project)
            if i.kind == "agent-plugin"
        ]
        if agent_plugins:
            _print("\nAgent Plugins 1.0")
            for item in agent_plugins:
                state = (
                    "enabled"
                    if item.enabled
                    else "disabled (untrusted workspace plugin)"
                )
                _print(f"  [{state}] {item.name} v{item.version}  {item.install_path}")
        return 0
    if command == "install":
        from bog_agents_cli.plugin_install import PluginInstallError, install_plugin

        try:
            result = install_plugin(
                args.source,
                dest_root=config_dir / "plugins",
                sha256=args.sha256,
                marketplace=args.marketplace,
            )
        except PluginInstallError as exc:
            _print(f"Install failed: {exc}")
            return 1
        _print(
            f"Installed {result.spec.name} v{result.spec.version} → {result.path}\n  sha256 {result.sha256}"
        )
        return 0
    if command == "import":
        from bog_agents_cli.plugin_import import format_import_report, import_from_tool

        report = import_from_tool(
            args.tool, project_root=project, config_dir=config_dir, dry_run=args.dry_run
        )
        _print(format_import_report(report))
        return 0
    if command in {"trust", "untrust"}:
        from bog_agents_cli.plugin_spec import (
            discover_agent_plugins,
            revoke_plugin_trust,
            trust_plugin,
        )

        match = next(
            (
                p
                for p in discover_agent_plugins(
                    config_dir=config_dir, project_root=project
                )
                if p.spec.name.lower() == args.name.lower()
            ),
            None,
        )
        if match is None:
            _print(
                f"No plugin named {args.name!r} found under ~/.agents/plugins, {project / '.agents' / 'plugins'} or {config_dir / 'plugins'}."
            )
            return 1
        if command == "trust":
            trust_plugin(match.spec.root, config_dir=config_dir)
            _print(
                f"Trusted {match.spec.name} ({match.spec.root}); its skills, commands and hooks are now active."
            )
        else:
            _print(
                f"{'Revoked' if revoke_plugin_trust(match.spec.root, config_dir=config_dir) else 'No trust recorded for'} {match.spec.name}."
            )
        return 0
    _print(
        "Usage: bog-agents plugin list | install <source> [--sha256 X] [--marketplace M] | import <tool> [--dry-run] | trust <name> | untrust <name>"
    )
    return 2


def handle_threads_import(args: argparse.Namespace) -> int:
    """`bog-agents threads import <source> [paths...]`."""
    from bog_agents_cli.session_import import format_import_summary, import_sessions

    paths = [Path(p) for p in getattr(args, "paths", []) or []] or None
    summary = asyncio.run(
        import_sessions(
            args.source,
            agent_name=args.agent,
            limit=args.limit,
            dry_run=args.dry_run,
            paths=paths,
        )
    )
    _print(format_import_summary(summary))
    return 0 if summary.imported or summary.dry_run else 1


def handle_threads_export(args: argparse.Namespace) -> int:
    """`bog-agents threads export <thread_id> [--out FILE]`."""
    from bog_agents_cli.session_import import default_export_path, export_thread

    out = Path(args.out) if args.out else default_export_path(args.thread_id)
    written = asyncio.run(export_thread(args.thread_id, out))
    if written is None:
        _print(f"Thread {args.thread_id!r} not found.")
        return 1
    _print(f"Exported {args.thread_id} → {written}")
    return 0


def exit_with(code: int) -> None:
    """`sys.exit` helper kept separate so tests can call the handlers directly."""
    sys.exit(code)
