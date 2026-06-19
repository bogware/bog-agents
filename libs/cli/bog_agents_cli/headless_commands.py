"""Headless (non-interactive) execution of slash commands.

Most slash commands live as `_handle_*_command` methods on the Textual
`BogAgentsApp` and assume a live TUI. This module provides a curated,
TUI-free surface so AI agents and API users can drive informational and
configuration commands from the command line:

    bog-agents command "/help"
    bog-agents command "/commands" --json
    bog-agents command "/model"

Commands that are inherently interactive (or that drive the live agent
session) are not exposed here; for those, `run_headless_command` returns a
clear "not available headless" result that lists the commands that are.

Each headless command is a plain function `(args: str) -> HeadlessResult`,
registered in `HEADLESS_COMMANDS`. Keeping handlers as standalone functions
(rather than methods on the TUI app) makes them unit-testable without
spinning up Textual — the pattern CLAUDE.md recommends for new command
logic.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class HeadlessResult:
    """Outcome of a headless slash-command invocation.

    Attributes:
        ok: Whether the command succeeded.
        text: Human-readable output (printed in `text` mode).
        data: Optional structured payload (emitted in `json`/`jsonl` mode).
    """

    ok: bool
    text: str
    data: dict[str, Any] | None = None


def _ok(text: str, data: dict[str, Any] | None = None) -> HeadlessResult:
    return HeadlessResult(ok=True, text=text, data=data)


def _err(text: str, data: dict[str, Any] | None = None) -> HeadlessResult:
    return HeadlessResult(ok=False, text=text, data=data)


def _cmd_version(_args: str) -> HeadlessResult:
    """Show the CLI and SDK versions."""
    from bog_agents_cli._version import __version__ as cli_version
    from bog_agents_cli.update_manager import _installed_version

    sdk_version = _installed_version("bog-agents") or "unknown"
    return _ok(
        f"bog-agents-cli {cli_version}\nbog-agents (SDK) {sdk_version}",
        {"cli": cli_version, "sdk": sdk_version},
    )


def _cmd_model(_args: str) -> HeadlessResult:
    """Show the currently configured model."""
    from bog_agents_cli.config import settings

    current = getattr(settings, "model_name", None)
    text = (
        f"Current model: {current}"
        if current
        else "No model configured (a default is used at runtime)."
    )
    return _ok(text, {"model": current})


def _cmd_config(_args: str) -> HeadlessResult:
    """Show key resolved configuration values and the config file path."""
    from bog_agents_cli.config import settings

    cfg_path = Path.home() / ".bog-agents" / "config.toml"
    data: dict[str, Any] = {
        "config_path": str(cfg_path),
        "config_exists": cfg_path.exists(),
        "model": getattr(settings, "model_name", None),
    }
    lines = [
        f"Config file: {cfg_path}",
        f"Exists:      {cfg_path.exists()}",
        f"Model:       {data['model']}",
    ]
    return _ok("\n".join(lines), data)


def _cmd_update(_args: str) -> HeadlessResult:
    """Report whether a newer CLI release is available (status only).

    The interactive `/update` (inside the TUI) is what actually downloads and
    installs, since it asks for confirmation first. Headless callers get the
    status plus the exact command to run — never an unattended upgrade.
    """
    try:
        from bog_agents_cli.update_manager import (
            build_plan,
            get_suite_status,
            render_status,
        )

        status = get_suite_status()
        plan = build_plan(status)
    except Exception as exc:  # update check must never raise
        return _err(
            f"Update check failed: {exc}",
            {"error": "exception"},
        )

    data: dict[str, Any] = {
        "method": status.method.value,
        "current": plan.current,
        "latest": plan.latest,
        "update_available": plan.needs_update,
        "command": plan.display_command,
    }

    if not plan.needs_update:
        return _ok(
            f"{render_status(status)}\n\nYou're on the latest bog-agents-cli "
            f"(v{plan.current}).",
            data,
        )

    return _ok(
        f"{render_status(status)}\n\nUpdate available. Run `/update` inside the "
        f"TUI to install with confirmation, or run manually:\n  "
        f"{plan.display_command}\nThen restart.",
        data,
    )


def _cmd_changelog(_args: str) -> HeadlessResult:
    """Print the CLI changelog."""
    path = Path(__file__).resolve().parent.parent / "CHANGELOG.md"
    if not path.exists():
        return _err("CHANGELOG.md not found.", {"found": False})
    text = path.read_text(encoding="utf-8")
    return _ok(text, {"found": True, "path": str(path)})


def _command_rows() -> list[dict[str, Any]]:
    """Build the list of all slash commands with a headless-capable flag."""
    from bog_agents_cli.command_registry import SLASH_COMMAND_SPECS

    rows: list[dict[str, Any]] = []
    for spec in sorted(SLASH_COMMAND_SPECS, key=lambda s: s.name):
        rows.append(
            {
                "name": spec.name,
                "description": spec.description,
                "category": spec.category,
                "headless": spec.name.lstrip("/") in HEADLESS_COMMANDS,
            }
        )
    return rows


def _cmd_commands(_args: str) -> HeadlessResult:
    """List every slash command and mark which run headlessly."""
    rows = _command_rows()
    lines = ["Slash commands (* runs headlessly via `bog-agents command`):"]
    for row in rows:
        mark = "*" if row["headless"] else " "
        lines.append(f"  {mark} {row['name']:<22} {row['description']}")
    return _ok("\n".join(lines), {"commands": rows})


def _cmd_help(args: str) -> HeadlessResult:
    """Show help for all commands, or details for one named command."""
    target = args.strip().lstrip("/")
    if not target:
        return _cmd_commands("")
    from bog_agents_cli.command_registry import SLASH_COMMAND_SPECS

    for spec in SLASH_COMMAND_SPECS:
        if spec.name.lstrip("/") == target:
            headless = target in HEADLESS_COMMANDS
            data: dict[str, Any] = {
                "name": spec.name,
                "description": spec.description,
                "category": spec.category,
                "aliases": list(spec.aliases),
                "headless": headless,
                "subcommands": [list(sub) for sub in spec.subcommands],
            }
            lines = [
                f"{spec.name} — {spec.description}",
                f"Category: {spec.category}",
                f"Headless: {headless}",
            ]
            if spec.aliases:
                lines.append(f"Aliases: {', '.join(spec.aliases)}")
            if spec.subcommands:
                lines.append(
                    "Subcommands: " + ", ".join(name for name, _ in spec.subcommands)
                )
            return _ok("\n".join(lines), data)
    return _err(f"Unknown command: /{target}", {"name": target, "found": False})


# Registry of headless-capable commands: name -> (description, handler).
HEADLESS_COMMANDS: dict[str, tuple[str, Callable[[str], HeadlessResult]]] = {
    "commands": ("List all slash commands and which run headlessly", _cmd_commands),
    "help": ("Show help for all or a specific slash command", _cmd_help),
    "version": ("Show CLI and SDK versions", _cmd_version),
    "update": ("Check whether a newer CLI release is available", _cmd_update),
    "model": ("Show the configured model", _cmd_model),
    "config": ("Show resolved configuration", _cmd_config),
    "changelog": ("Show the CLI changelog", _cmd_changelog),
}


def run_headless_command(command_line: str, *, output_format: str = "text") -> int:
    """Execute a single slash command without the interactive TUI.

    Args:
        command_line: The command to run, with or without a leading slash and
            with optional arguments (e.g. `"/help model"`, `"commands"`).
        output_format: `"text"` (human-readable, default), or `"json"`/`"jsonl"`
            for a single machine-readable envelope on stdout.

    Returns:
        Exit code: `0` on success, `1` when the command ran but reported a
        failure, `2` when the command is unknown or not available headless.
    """
    raw = command_line.strip()
    if not raw:
        return _emit(
            _err('No command provided. Try `bog-agents command "/help"`.'),
            "empty",
            output_format,
        )

    name, _, args = raw.lstrip("/").partition(" ")
    name = name.strip().lower()
    entry = HEADLESS_COMMANDS.get(name)
    if entry is None:
        available = ", ".join(f"/{key}" for key in sorted(HEADLESS_COMMANDS))
        message = (
            f"/{name} is not available in non-interactive mode. "
            f"Headless commands: {available}. "
            "Run other commands inside the interactive TUI, or use a dedicated "
            "subcommand where one exists (e.g. `bog-agents threads list`)."
        )
        return _emit(
            _err(
                message,
                {
                    "command": name,
                    "error": "not_headless",
                    "headless_commands": sorted(HEADLESS_COMMANDS),
                },
            ),
            name,
            output_format,
        )

    _description, handler = entry
    try:
        result = handler(args)
    except Exception as exc:
        result = _err(
            f"Command /{name} failed: {exc}", {"command": name, "error": "exception"}
        )
    return _emit(result, name, output_format)


def _emit(result: HeadlessResult, command_name: str, output_format: str) -> int:
    """Write a `HeadlessResult` to stdout/stderr and return its exit code."""
    if output_format in ("json", "jsonl"):
        from bog_agents_cli.output import write_json

        payload: dict[str, Any] = {"ok": result.ok}
        payload.update(result.data or {"text": result.text})
        write_json(f"command:{command_name}", payload)
    else:
        stream = sys.stdout if result.ok else sys.stderr
        print(result.text, file=stream)
    if not result.ok:
        return (
            1
            if result.data and result.data.get("error") not in ("not_headless", "empty")
            else 2
        )
    return 0
