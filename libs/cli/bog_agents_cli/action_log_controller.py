"""`/actionlog`, the CLI-side chains and the TraceFile-signed export (ROADMAP #74).

The agent's own chain (`model_call` / `tool_call` events) is written by the
SDK's `ActionLogMiddleware` inside the server process. This module owns the
CLI side: the approval chain (`approval` events from the approval adapter and
`expert_verdict` events from Expert Mode, one file per TUI process so two
writers never interleave), the `/actionlog status|verify|export|prune` verbs,
and the signed export that reuses the TraceFile Ed25519 key. Everything is a
plain function so it tests without the TUI.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bog_agents.action_log import ActionLog, apply_retention, expert_sink, verify_chain

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

DEFAULT_RETENTION_DAYS = 90
USAGE = "Usage: /actionlog [status] | /actionlog verify [file] | /actionlog export [file] [--unsigned] | /actionlog prune [--days N]"
_APPROVALS: ActionLog | None = None


def action_log_dir() -> Path:
    """`~/.bog-agents/action-log`."""
    from bog_agents_cli.config import settings

    return Path(settings.user_agents_dir) / "action-log"


def enabled() -> bool:
    """`compliance.action_log` (config.toml) / `BOG_AGENTS_ACTION_LOG`."""
    try:
        from bog_agents_cli.config_manifest import resolve_option

        return bool(resolve_option("compliance.action_log"))
    except Exception:
        return False


def retention_days() -> float:
    """`compliance.retention_days` (default 90)."""
    try:
        from bog_agents_cli.config_manifest import resolve_option

        value = resolve_option("compliance.retention_days")
        return float(value) if value else float(DEFAULT_RETENTION_DAYS)
    except Exception:
        return float(DEFAULT_RETENTION_DAYS)


def approvals_log() -> ActionLog | None:
    """This process's approval / verdict chain (created on first use), or `None` when the log is off."""
    global _APPROVALS  # noqa: PLW0603 - one chain per process by design
    if not enabled():
        return None
    if _APPROVALS is None:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        _APPROVALS = ActionLog(
            action_log_dir() / f"approvals-{stamp}-{os.getpid()}.jsonl",
            run_id=f"approvals-{stamp}-{os.getpid()}",
        )
    return _APPROVALS


def record_approval_events(decisions: list[Any]) -> int:
    """Mirror approval decisions into the chain; returns how many were written."""
    log = approvals_log()
    if log is None:
        return 0
    count = 0
    for decision in decisions:
        try:
            log.append(
                "approval",
                tool=str(getattr(decision, "tool", "")),
                call=str(getattr(decision, "call", ""))[:200],
                decision=str(getattr(decision, "decision", "")),
                rule_source=str(getattr(decision, "rule_source", "")),
                risk=str(getattr(decision, "risk", "")),
                reason=str(getattr(decision, "reason", ""))[:300],
                judge=str(getattr(decision, "judge", "")),
            )
            count += 1
        except Exception:
            logger.debug("approval event not recorded", exc_info=True)
    return count


def expert_audit_sink() -> Callable[[str, dict[str, Any]], None] | None:
    """An Expert-Mode audit sink writing `expert_verdict` events, or `None` when the log is off."""
    log = approvals_log()
    return expert_sink(log) if log is not None else None


def list_logs() -> list[Path]:
    """Chain files, newest first."""
    directory = action_log_dir()
    if not directory.is_dir():
        return []
    return sorted(
        directory.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True
    )


def signer() -> tuple[Callable[[bytes], str], str] | None:
    """`(sign, fingerprint)` from the TraceFile key, or `None` when signing is unavailable."""
    try:
        from bog_agents_cli.tracefile.controller import _resolve_key
        from bog_agents_cli.tracefile.signing import sign

        material = _resolve_key(None)
        if not material.can_sign:
            return None
        return (lambda payload: sign(material, payload)), material.fingerprint
    except Exception:
        logger.debug("TraceFile signer unavailable", exc_info=True)
        return None


def _pick(name: str) -> Path | None:
    logs = list_logs()
    if not logs:
        return None
    if not name:
        return logs[0]
    for path in logs:
        if name in (path.name, path.stem) or path.stem.startswith(name):
            return path
    return None


def run_actionlog_command(command: str) -> str:
    """Body of `/actionlog`."""
    try:
        tokens = shlex.split(command.strip())[1:]
    except ValueError:
        tokens = command.strip().split()[1:]
    verb = tokens[0].lower() if tokens else "status"
    args = tokens[1:]
    if verb == "status":
        state = (
            "on"
            if enabled()
            else "off (set compliance.action_log = true or BOG_AGENTS_ACTION_LOG=1)"
        )
        lines = [f"Action log: {state} — {action_log_dir()}"]
        logs = list_logs()
        if not logs:
            lines.append("  no chains yet")
        for path in logs[:10]:
            lines.append(f"  {path.name}: {verify_chain(path).describe()}")
        lines.append(USAGE)
        return "\n".join(lines)
    if verb == "verify":
        path = _pick(args[0] if args else "")
        if path is None:
            return (
                "No action log to verify."
                if not args
                else f"No action log matches {args[0]!r}."
            )
        return f"{path.name}: {verify_chain(path).describe()}"
    if verb == "export":
        unsigned = "--unsigned" in args
        names = [a for a in args if not a.startswith("--")]
        path = _pick(names[0] if names else "")
        if path is None:
            return "No action log to export."
        log = ActionLog(path)
        signing = None if unsigned else signer()
        bundle = log.export(
            sign=signing[0] if signing else None,
            signer_id=signing[1] if signing else "",
        )
        target = path.with_name(f"export-{path.stem}.json")
        target.write_text(json.dumps(bundle, indent=2), encoding="utf-8")
        how = f"signed with TraceFile key {signing[1]}" if signing else "unsigned"
        return f"Exported {bundle['count']} event(s) from {path.name} to {target} ({how}); head {bundle['head'][:19]}."
    if verb == "prune":
        days = retention_days()
        if "--days" in args:
            try:
                days = float(args[args.index("--days") + 1])
            except (IndexError, ValueError):
                return USAGE
        removed = apply_retention(action_log_dir(), keep_days=days)
        return f"Pruned {removed} chain(s) older than {days:g} day(s)."
    return USAGE


__all__ = [
    "DEFAULT_RETENTION_DAYS",
    "USAGE",
    "action_log_dir",
    "approvals_log",
    "enabled",
    "expert_audit_sink",
    "list_logs",
    "record_approval_events",
    "retention_days",
    "run_actionlog_command",
    "signer",
]
