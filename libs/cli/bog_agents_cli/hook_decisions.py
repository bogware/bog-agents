"""Decision-capable hooks + Claude/Cursor compat (hook-bus completion).

bog's `hooks.py` fires observe-only hooks (fire-and-forget, output discarded).
Grok Build's hook bus adds two things this module brings to bog:

  1. **Decisions** — a `PreToolUse` (or `Stop`) hook can emit
     ``{"decision": "deny"|"block", "reason": "..."}`` on stdout (or exit 2 with
     a stderr reason) to *block* a tool call / turn end. Everything is
     **fail-open**: a hook that errors, times out, or prints garbage never
     blocks.
  2. **Compat-as-a-feature** — Claude Code (`.claude/settings.json`) and Cursor
     (`.cursor/hooks.json`) hook files load unchanged, with their tool names
     (`Bash`, `Edit`, `Read`, …) aliased onto bog's (`execute`, `edit_file`,
     `read_file`, …) so migrated hooks fire on the right events.

This is the tested core (event set, alias table, decision parsing, vendor
loaders, and a synchronous evaluator). Wiring the `PreToolUse` deny into the
live tool-call path is the remaining integration step.
"""

from __future__ import annotations

import json
import logging
import subprocess  # noqa: S404
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# The canonical lifecycle events (grok-aligned). Decision-capable events are a
# subset (see DENY_EVENTS / BLOCK_EVENTS).
CANONICAL_EVENTS: tuple[str, ...] = (
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "PostToolUseFailure",
    "PermissionDenied",
    "Stop",
    "StopFailure",
    "Notification",
    "SubagentStart",
    "SubagentStop",
    "PreCompact",
    "PostCompact",
    "SessionEnd",
    "FileWrite",
    "FileEdit",
    "ShellExecute",
    "ModelCall",
)
DENY_EVENTS = frozenset({"PreToolUse"})  # may return decision=deny
BLOCK_EVENTS = frozenset({"Stop", "SubagentStop"})  # may return decision=block


# ---------------------------------------------------------------------------
# Hook types — what a hook event is allowed to do with the action it sees
# ---------------------------------------------------------------------------


class HookType(StrEnum):
    """Capability of a hook event type.

    A type is a ceiling — a hook may use less, never more:

    - `OBSERVE` — fire-and-forget; cannot change the action.
    - `GATE` — may allow or block the action (deny/block decisions).
    - `MODIFY` — may rewrite the action's input (prompt/args) and gate it.
    """

    OBSERVE = "observe"
    GATE = "gate"
    MODIFY = "modify"


# Canonical (Claude/Cursor-style) events.
_CANONICAL_TYPES: dict[str, HookType] = {
    "SessionStart": HookType.OBSERVE,
    "UserPromptSubmit": HookType.MODIFY,
    "PreToolUse": HookType.MODIFY,
    "PostToolUse": HookType.OBSERVE,
    "PostToolUseFailure": HookType.OBSERVE,
    "PermissionDenied": HookType.OBSERVE,
    "Stop": HookType.GATE,
    "StopFailure": HookType.OBSERVE,
    "Notification": HookType.OBSERVE,
    "SubagentStart": HookType.OBSERVE,
    "SubagentStop": HookType.GATE,
    "PreCompact": HookType.OBSERVE,
    "PostCompact": HookType.OBSERVE,
    "SessionEnd": HookType.OBSERVE,
    "FileWrite": HookType.GATE,
    "FileEdit": HookType.GATE,
    "ShellExecute": HookType.GATE,
    "ModelCall": HookType.GATE,
}

# Dotted hook-bus events (`~/.bog-agents/hooks.json` `events`).
_DOTTED_TYPES: dict[str, HookType] = {
    "session.start": HookType.OBSERVE,
    "session.end": HookType.OBSERVE,
    "user.prompt": HookType.MODIFY,
    "context.compact": HookType.OBSERVE,
    "compact": HookType.OBSERVE,
    "error": HookType.OBSERVE,
    "tool.pre_call": HookType.MODIFY,
    "tool.post_call": HookType.OBSERVE,
    "model.pre_call": HookType.GATE,
    "model.post_call": HookType.OBSERVE,
    "file.pre_read": HookType.OBSERVE,
    "file.post_read": HookType.OBSERVE,
    "file.pre_write": HookType.GATE,
    "file.post_write": HookType.OBSERVE,
    "file.pre_edit": HookType.GATE,
    "file.post_edit": HookType.OBSERVE,
    "shell.pre_execute": HookType.GATE,
    "shell.post_execute": HookType.OBSERVE,
}

HOOK_TYPES: dict[str, HookType] = {**_CANONICAL_TYPES, **_DOTTED_TYPES}

# Derived sets, useful for "does this event gate?" checks.
GATE_EVENTS: frozenset[str] = frozenset(
    event for event, hook_type in HOOK_TYPES.items() if hook_type is HookType.GATE
)
MODIFY_EVENTS: frozenset[str] = frozenset(
    event for event, hook_type in HOOK_TYPES.items() if hook_type is HookType.MODIFY
)
OBSERVE_EVENTS: frozenset[str] = frozenset(
    event for event, hook_type in HOOK_TYPES.items() if hook_type is HookType.OBSERVE
)
# Events that can block or deny the action (gate plus modify).
GATING_EVENTS: frozenset[str] = GATE_EVENTS | MODIFY_EVENTS


def _normalize_event(event: str) -> str:
    """Strip the per-tool suffix from a tool event family."""
    for prefix in ("tool.pre_call.", "tool.post_call."):
        if event.startswith(prefix):
            return prefix[:-1]
    return event


def hook_type_for_event(event: str) -> HookType:
    """Return the hook type for an event.

    Per-tool events like ``tool.pre_call.execute`` are normalized to their
    family before lookup. Unknown events default to `OBSERVE`.

    Args:
        event: A canonical (``PreToolUse``) or dotted (``shell.pre_execute``)
            event name.

    Returns:
        The `HookType` for the event.
    """
    return HOOK_TYPES.get(_normalize_event(event), HookType.OBSERVE)


# Claude Code / Cursor tool names → bog tool names, so a migrated hook's
# `matcher` fires on the right tool.
_TOOL_ALIASES: dict[str, str] = {
    "bash": "execute",
    "shell": "execute",
    "edit": "edit_file",
    "multiedit": "multi_edit_file",
    "write": "write_file",
    "read": "read_file",
    "grep": "grep",
    "glob": "glob",
    "ls": "ls",
    "task": "task",
    "webfetch": "web_fetch",
    "websearch": "web_search",
}


def alias_tool_name(name: str) -> str:
    """Map a vendor tool name (Claude/Cursor) to bog's, or return it unchanged."""
    return _TOOL_ALIASES.get(name.strip().lower(), name)


@dataclass
class HookDecision:
    """The outcome of a decision-capable hook run.

    Attributes:
        action: ``"allow"`` (default), ``"deny"`` (block a tool call), or
            ``"block"`` (block a turn end).
        reason: Why, surfaced to the user/model.
    """

    action: str = "allow"
    reason: str = ""

    @property
    def blocks(self) -> bool:
        """Whether this decision stops the action (deny or block)."""
        return self.action in ("deny", "block")


def parse_hook_decision(stdout: str, exit_code: int) -> HookDecision:
    """Parse a hook's stdout + exit code into a `HookDecision`.

    Precedence (fail-open): valid JSON on stdout wins; else exit code 2 blocks
    with a generic reason; anything else allows.

    Args:
        stdout: The hook's captured stdout.
        exit_code: The hook's exit code.

    Returns:
        A `HookDecision` (``allow`` on any parse failure).
    """
    text = (stdout or "").strip()
    if text:
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            data = None
        if isinstance(data, dict):
            decision = str(data.get("decision", "")).strip().lower()
            reason = str(data.get("reason", "")).strip()
            if decision in ("deny", "block"):
                return HookDecision(
                    action=decision, reason=reason or "A hook denied this action."
                )
            if decision in ("allow", "approve", ""):
                # An explicit allow (or a JSON payload with no decision) permits.
                if data.get("continue") is False:
                    return HookDecision(
                        action="block", reason=str(data.get("stopReason", "")).strip()
                    )
                return HookDecision(action="allow", reason=reason)
    if exit_code == 2:
        return HookDecision(
            action="deny", reason="A hook blocked this action (exit 2)."
        )
    return HookDecision()


def load_vendor_hooks(project_root: Path) -> list[dict[str, Any]]:
    """Load Claude Code + Cursor hook files under `project_root`, normalized.

    Reads `.claude/settings.json` (Claude Code's `hooks` map) and
    `.cursor/hooks.json`, and returns bog-format hook dicts
    (``{"command": [...], "events": [...], "matcher": "<tool>"}``) with tool
    names aliased. Malformed files are skipped (never raise).

    Args:
        project_root: The project directory to scan.

    Returns:
        Normalized hook dicts (may be empty).
    """
    out: list[dict[str, Any]] = []
    out.extend(_load_claude_hooks(project_root / ".claude" / "settings.json"))
    out.extend(_load_claude_hooks(project_root / ".cursor" / "hooks.json"))
    return out


def _load_claude_hooks(path: Path) -> list[dict[str, Any]]:
    """Parse a Claude-Code-style hooks file into bog-format hook dicts."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return []
    hooks_map = raw.get("hooks", raw) if isinstance(raw, dict) else {}
    if not isinstance(hooks_map, dict):
        return []
    out: list[dict[str, Any]] = []
    for event, entries in hooks_map.items():
        if event not in CANONICAL_EVENTS or not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            matcher = alias_tool_name(str(entry.get("matcher", "")))
            for hook in entry.get("hooks", []):
                command = _normalize_command(hook)
                if command:
                    out.append(
                        {"command": command, "events": [event], "matcher": matcher}
                    )
    return out


def _normalize_command(hook: object) -> list[str] | None:
    """Turn a Claude/Cursor hook entry into an argv list, or None."""
    if not isinstance(hook, dict):
        return None
    command = hook.get("command")
    if isinstance(command, list) and command:
        return [str(c) for c in command]
    if isinstance(command, str) and command.strip():
        # Claude's `command` is a shell string; run it through the shell.
        return ["sh", "-c", command] if not _is_windows() else ["cmd", "/c", command]
    return None


def _is_windows() -> bool:
    import os

    return os.name == "nt"


def evaluate_decision_hooks(
    event: str,
    payload: dict[str, Any],
    hooks: list[dict[str, Any]],
    *,
    tool_name: str = "",
    timeout: float = 5.0,
) -> HookDecision:
    """Run matching decision hooks synchronously and return the first block.

    A hook matches when its `events` includes `event` (or is empty) and its
    `matcher` (if set) equals `tool_name`. Hooks run with the JSON `payload` on
    stdin and their stdout captured. Fail-open: a hook that errors/times out is
    ignored.

    Args:
        event: The lifecycle event (e.g. ``"PreToolUse"``).
        payload: JSON-serializable event payload (given to the hook on stdin).
        hooks: bog-format hook dicts.
        tool_name: The tool being called (for matcher filtering).
        timeout: Per-hook timeout in seconds.

    Returns:
        The first blocking `HookDecision`, else an allow.
    """
    payload_bytes = json.dumps({"event": event, **payload}).encode("utf-8")
    for hook in hooks:
        command = hook.get("command")
        if not isinstance(command, list) or not command:
            continue
        events = hook.get("events")
        if events and event not in events:
            continue
        matcher = hook.get("matcher")
        if matcher and tool_name and matcher != tool_name:
            continue
        decision = _run_decision_hook(command, payload_bytes, timeout)
        if decision.blocks:
            logger.info("Hook %s %s: %s", event, decision.action, decision.reason)
            return decision
    return HookDecision()


def load_pretooluse_hooks(
    project_root: Path,
    *,
    config_hooks: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Assemble the decision-capable `PreToolUse` hooks for a project.

    Merges ingested Claude/Cursor hook files (`load_vendor_hooks`) with any bog
    `hooks.json` entries that subscribe to the `PreToolUse` event, so both native
    and migrated decision hooks enforce.

    Args:
        project_root: The project directory (for vendor hook files).
        config_hooks: bog's own hook dicts (e.g. from `hooks._load_hooks()`).

    Returns:
        bog-format decision hooks (may be empty).
    """
    hooks = list(load_vendor_hooks(project_root))
    for hook in config_hooks or []:
        events = hook.get("events")
        if events and "PreToolUse" in events and isinstance(hook.get("command"), list):
            hooks.append(hook)
    return hooks


def _run_decision_hook(
    command: list[str], payload_bytes: bytes, timeout: float
) -> HookDecision:
    """Run one hook, capturing stdout, and parse its decision (fail-open)."""
    try:
        result = subprocess.run(  # noqa: S603
            command,
            input=payload_bytes,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (
        subprocess.TimeoutExpired,
        FileNotFoundError,
        PermissionError,
        OSError,
    ) as exc:
        logger.debug("Decision hook failed (fail-open): %s — %s", command, exc)
        return HookDecision()
    stdout = result.stdout.decode("utf-8", errors="replace") if result.stdout else ""
    return parse_hook_decision(stdout, result.returncode)


__all__ = [
    "BLOCK_EVENTS",
    "CANONICAL_EVENTS",
    "DENY_EVENTS",
    "GATE_EVENTS",
    "GATING_EVENTS",
    "HOOK_TYPES",
    "MODIFY_EVENTS",
    "OBSERVE_EVENTS",
    "HookDecision",
    "HookType",
    "alias_tool_name",
    "evaluate_decision_hooks",
    "hook_type_for_event",
    "load_pretooluse_hooks",
    "load_vendor_hooks",
    "parse_hook_decision",
]
