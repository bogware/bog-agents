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
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bog_agents_cli.config_manifest import ConfigOption


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


_REDACTED_DISPLAY = "********"
"""Placeholder shown for a *set* redacted option, never its raw value."""


def _display_value(option: ConfigOption, value: object) -> str:
    """Render a resolved option value for display, redacting where flagged.

    Args:
        option: The `ConfigOption` the value was resolved for.
        value: The resolved value (may be `None` when unset with no default).

    Returns:
        The value as a display string: `(unset)` when `None`, the redaction
        placeholder for a set redacted option, or `str(value)` otherwise. A
        redacted option's raw value is never returned.
    """
    if value is None:
        return "(unset)"
    if option.redacted:
        return _REDACTED_DISPLAY
    return str(value)


def _option_row(option: ConfigOption, toml_data: dict[str, Any]) -> dict[str, Any]:
    """Resolve one option into a JSON-safe row (raw secrets never included)."""
    from bog_agents_cli.config_manifest import resolve_scalar

    value, source = resolve_scalar(option, toml_data=toml_data)
    return {
        "key": option.key,
        "group": option.group,
        "type": option.type,
        "source": source,
        "redacted": option.redacted,
        "set": value is not None,
        "value": _display_value(option, value),
    }


def _cmd_config(args: str) -> HeadlessResult:
    """Introspect resolved configuration via the config manifest.

    Usage:
        config [show]      List every option with its resolved value and source.
        config get <key>   Show the resolved value and source for one option.

    This is read-only introspection — it never mutates config. Redacted options
    (credentials) report only whether they are set, never the raw value.
    """
    from bog_agents_cli.config_manifest import (
        get_config_options,
        get_option,
        load_config_toml,
    )
    from bog_agents_cli.model_config import DEFAULT_CONFIG_PATH

    verb, _, rest = args.strip().partition(" ")
    verb = verb.strip().lower()
    toml_data = load_config_toml()

    if verb == "get":
        key = rest.strip()
        if not key:
            return _err(
                "Usage: config get <key>",
                {"error": "usage", "config_path": str(DEFAULT_CONFIG_PATH)},
            )
        option = get_option(key)
        if option is None:
            return _err(
                f"Unknown config key: {key}",
                {"error": "unknown_key", "key": key},
            )
        row = _option_row(option, toml_data)
        text = (
            f"{row['key']} = {row['value']}  ({row['type']}, source: {row['source']})"
        )
        return _ok(text, {"config_path": str(DEFAULT_CONFIG_PATH), "option": row})

    if verb not in ("", "show", "list"):
        return _err(
            f"Unknown config subcommand: {verb}. Try 'config show' or "
            "'config get <key>'.",
            {"error": "usage"},
        )

    rows = [_option_row(opt, toml_data) for opt in get_config_options()]
    data: dict[str, Any] = {
        "config_path": str(DEFAULT_CONFIG_PATH),
        "config_exists": DEFAULT_CONFIG_PATH.exists(),
        "options": rows,
    }
    lines = [
        f"Config file: {DEFAULT_CONFIG_PATH} "
        f"({'exists' if DEFAULT_CONFIG_PATH.exists() else 'not created'})",
        "",
    ]
    current_group = ""
    for row in rows:
        if row["group"] != current_group:
            current_group = row["group"]
            lines.append(f"[{current_group}]")
        lines.append(
            f"  {row['key']:<34} {row['value']:<24} ({row['type']}, {row['source']})"
        )
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


def _cmd_goal(_args: str) -> HeadlessResult:
    """Show the current project goal: objective, status, and rubric.

    Reads the file-backed goal (``.bog-agents/goal.json`` under the cwd) so the
    objective/rubric set via the interactive ``/goal`` and ``/rubric`` commands
    are visible headlessly. Agent-recorded status/note live in checkpointed
    session state and are not available without a live thread.
    """
    from bog_agents_cli import goal_controller

    record = goal_controller.load_goal(Path.cwd())
    data: dict[str, Any] = {
        "objective": record.objective or None,
        "status": record.status if record.is_set else None,
        "rubric": list(record.rubric),
        "note": record.note or None,
    }
    if not record.is_set:
        return _ok("No goal set. Set one with /goal <objective> in the TUI.", data)
    lines = [f"Goal: {record.objective}", f"Status: {record.status}"]
    if record.rubric:
        lines.append("Acceptance criteria:")
        lines.extend(f"  {i}. {c}" for i, c in enumerate(record.rubric, start=1))
    else:
        lines.append("Acceptance criteria: (none)")
    if record.note:
        lines.append(f"Latest note: {record.note}")
    return _ok("\n".join(lines), data)


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


def _cmd_findings(args: str) -> HeadlessResult:
    """The project's findings ledger (ROADMAP #59); `findings gate --max high` exits 1 when the gate fails."""
    from bog_agents_cli.findings_controller import run_findings_headless

    ok, text, data = run_findings_headless(args, Path.cwd())
    return HeadlessResult(ok=ok, text=text, data=data)


# Registry of headless-capable commands: name -> (description, handler).
def _cmd_tokens(args: str) -> HeadlessResult:
    """Measure the CLI agent's fixed per-turn cost (headless twin of ``/tokens middleware``).

    Builds the same middleware stack the TUI uses around a recording model (no
    provider call) and reports tokens per turn attributed to each middleware
    and tool. ``--mini`` measures the ``lean`` profile instead.
    """
    from bog_agents_cli.tokens_audit_controller import (
        LEAN_PROFILE,
        audit_cli_agent,
        render_cli_audit,
    )

    words = args.split()
    verb = words[0] if words and not words[0].startswith("-") else "middleware"
    if verb != "middleware":
        return HeadlessResult(
            ok=False, text="usage: tokens middleware [--mini]", data={}
        )
    harness_profile = LEAN_PROFILE if "--mini" in words else None
    audit = audit_cli_agent(harness_profile=harness_profile, cwd=Path.cwd())
    return HeadlessResult(
        ok=True,
        text=render_cli_audit(audit, harness_profile=harness_profile),
        data=audit.to_dict(),
    )


HEADLESS_COMMANDS: dict[str, tuple[str, Callable[[str], HeadlessResult]]] = {
    "commands": ("List all slash commands and which run headlessly", _cmd_commands),
    "help": ("Show help for all or a specific slash command", _cmd_help),
    "version": ("Show CLI and SDK versions", _cmd_version),
    "update": ("Check whether a newer CLI release is available", _cmd_update),
    "model": ("Show the configured model", _cmd_model),
    "config": (
        "Introspect resolved configuration (config show | config get <key>)",
        _cmd_config,
    ),
    "changelog": ("Show the CLI changelog", _cmd_changelog),
    "goal": ("Show the current project goal, status, and rubric", _cmd_goal),
    "tokens": (
        "Harness overhead per turn, attributed per middleware and tool (tokens middleware [--mini])",
        _cmd_tokens,
    ),
    "findings": (
        "Findings ledger: list | show | triage | gate | sarif | record (gate exits 1 on failure)",
        _cmd_findings,
    ),
}


_MSYS_GIT_PREFIXES: tuple[str, ...] = (
    "c:/program files/git",
    "c:\\program files\\git",
    "c:/program files (x86)/git",
    "c:\\program files (x86)\\git",
    "/c/program files/git",
)


def recover_msys_command(raw: str) -> str:
    """Undo Git Bash's rewrite of a leading-slash command argument.

    v6 CLI-8: on Windows, MSYS path conversion turns `bog-agents command
    "/help"` into `C:/Program Files/Git/help` before Python sees it, so the
    documented form failed with a baffling `/c:/program is not available`.
    When the argument starts with a Git-for-Windows install prefix, the
    remainder is the command the user actually typed.

    Args:
        raw: The stripped command line as received.

    Returns:
        The recovered command line, or `raw` unchanged.
    """
    lowered = raw.replace("\\", "/").lower()
    for prefix in _MSYS_GIT_PREFIXES:
        norm_prefix = prefix.replace("\\", "/")
        if lowered.startswith(norm_prefix):
            tail = raw.replace("\\", "/")[len(norm_prefix) :]
            return tail.lstrip("/").strip()
    return raw


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
    raw = recover_msys_command(command_line.strip())
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
            f"Headless commands: {available} (the leading slash is optional — "
            "on Git Bash for Windows prefer `bog-agents command help`). "
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
