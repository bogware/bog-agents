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
import re
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
    # ROADMAP #64: hook bus v2.
    "PermissionRequest",
    "Interrupt",
    "PreModelSwitch",
    "PostModelSwitch",
)
DENY_EVENTS = frozenset(
    {"PreToolUse", "PostToolUse", "PermissionRequest", "PreModelSwitch"}
)  # may return decision=deny
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
    "PostToolUse": HookType.MODIFY,  # ROADMAP #64: may replace the tool result
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
    "PermissionRequest": HookType.GATE,
    "Interrupt": HookType.OBSERVE,
    "PreModelSwitch": HookType.GATE,
    "PostModelSwitch": HookType.OBSERVE,
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


def _matcher_matches(matcher: str, tool_name: str) -> bool:
    """Whether a hook `matcher` selects `tool_name`, Claude-Code style.

    Claude Code / Cursor matchers are *regexes* over vendor tool names, and
    `"*"` (or an empty matcher) means "all tools". Matching only by exact
    string equality — as the old code did — silently dropped the ubiquitous
    `"*"` and alternations like `"Edit|Write"`, so a migrated deny hook that
    guards every tool loaded but enforced nothing: the security gate failed
    open (T1-3). This restores the documented "load unchanged" semantics.

    Args:
        matcher: The hook's matcher (already alias-mapped for single names at
            load time; a wildcard/regex/alternation is preserved verbatim).
        tool_name: The bog tool name being invoked.

    Returns:
        True when the hook applies to this tool.
    """
    if not matcher or not tool_name:
        return True
    m = matcher.strip()
    if m in ("*", ".*"):
        return True
    if m == tool_name:
        return True
    # A regex/alternation over vendor names (e.g. "Edit|Write"): test each
    # alternative both as an alias lookup and as an anchored regex against the
    # bog tool name, so "Edit|Write" fires on edit_file/write_file and a
    # pattern like "Notebook.*" still works.
    for raw_alt in m.split("|"):
        alt = raw_alt.strip()
        if not alt:
            continue
        if alias_tool_name(alt) == tool_name:
            return True
        try:
            if re.fullmatch(alt, tool_name):
                return True
        except re.error:
            continue
    return False


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
    tool_result: str | None = None
    """ROADMAP #64: a `PostToolUse` hook's replacement for the tool result."""
    failed: bool = False
    """The hook crashed / timed out; `on_failure` decides what that means."""

    @property
    def asks(self) -> bool:
        """Whether this decision asks for a human (a failed hook with `on_failure: ask`)."""
        return self.action == "ask"

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
            tool_result = data.get("tool_result")
            if isinstance(tool_result, str):
                # ROADMAP #64: PostToolUse replacement — what the model sees.
                return HookDecision(
                    action="allow",
                    reason=str(data.get("reason", "")).strip(),
                    tool_result=tool_result,
                )
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
    `matcher` selects `tool_name` (see `_matcher_matches`: exact name, `"*"`,
    or a Claude-style regex/alternation). Hooks run with the JSON `payload` on
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
        if not _matcher_matches(str(matcher or ""), str(tool_name or "")):
            continue
        refusal = _hash_refusal(hook)
        if refusal is not None:
            logger.warning("Hook %s refused: %s", event, refusal.reason)
            return refusal
        decision = _run_decision_hook(command, payload_bytes, timeout)
        if decision.failed:
            decision = _on_failure(hook, decision)
        if decision.blocks or decision.asks or decision.tool_result is not None:
            logger.info("Hook %s %s: %s", event, decision.action, decision.reason)
            return decision
    return HookDecision()


def _on_failure(hook: dict[str, Any], failure: HookDecision) -> HookDecision:
    """Map a crashed / timed-out hook onto its `on_failure` policy (ROADMAP #64).

    `allow` (the default, fail-open) ignores the failure; `deny` blocks the
    action; `ask` hands the call to a human where an approval UI exists (the
    tool middleware treats it as allow — the approval path forced the prompt).
    """
    policy = str(hook.get("on_failure", "allow")).strip().lower()
    label = _hook_label(hook)
    if policy == "deny":
        return HookDecision(
            action="deny",
            reason=f"hook {label} failed ({failure.reason}) and on_failure=deny",
            failed=True,
        )
    if policy == "ask":
        return HookDecision(
            action="ask",
            reason=f"hook {label} failed ({failure.reason}); on_failure=ask",
            failed=True,
        )
    return HookDecision(failed=True, reason=failure.reason)


def _hook_label(hook: dict[str, Any]) -> str:
    command = hook.get("command")
    if isinstance(command, list) and command:
        return str(command[-1] if len(command) > 1 else command[0])
    return str(hook.get("name") or hook.get("prompt", "")[:40] or "?")


def _script_path(command: list[str], base: Path | None = None) -> Path | None:
    """The script a command runs: the last argument that exists as a file (never the interpreter itself).

    A one-element command is the script (or binary) itself; otherwise the
    executable in `command[0]` is skipped so a missing script never pins the
    interpreter's hash.
    """
    candidates = command[1:] if len(command) > 1 else command
    for arg in reversed(candidates):
        candidate = Path(arg)
        if not candidate.is_absolute() and base is not None:
            candidate = base / arg
        if candidate.is_file():
            return candidate
    return None


def script_sha256(command: list[str], base: Path | None = None) -> str | None:
    """`sha256:<hex>` of the script a hook command runs, or `None` when there is no file to hash."""
    import hashlib

    path = _script_path(command, base)
    if path is None:
        return None
    try:
        return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _hash_refusal(hook: dict[str, Any]) -> HookDecision | None:
    """A deny when the hook carries a pinned `sha256` and its script no longer matches (ROADMAP #64)."""
    pinned = hook.get("sha256")
    if not isinstance(pinned, str) or not pinned:
        return None
    command = hook.get("command")
    if not isinstance(command, list):
        return None
    base = hook.get("base")
    current = script_sha256(
        command, Path(base) if isinstance(base, str) and base else None
    )
    if current == pinned:
        return None
    return HookDecision(
        action="deny",
        reason=f"hook {_hook_label(hook)} changed since it was trusted (pinned {pinned[:19]}, now {(current or 'missing')[:19]})",
    )


def pin_hook_hashes(
    hooks: list[dict[str, Any]], base: Path | None = None
) -> list[dict[str, Any]]:
    """Copy of `hooks` with `sha256` (and `base`) set for every command hook whose script exists."""
    pinned: list[dict[str, Any]] = []
    for hook in hooks:
        entry = dict(hook)
        command = entry.get("command")
        if isinstance(command, list):
            digest = script_sha256(command, base)
            if digest is not None:
                entry["sha256"] = digest
                if base is not None:
                    entry["base"] = str(base)
        pinned.append(entry)
    return pinned


def load_hook_file(path: Path) -> list[dict[str, Any]]:
    """Hooks from a Claude-style map *or* a bog `{"hooks": [...]}` list, normalised to bog dicts."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return []
    hooks = raw.get("hooks") if isinstance(raw, dict) else None
    if isinstance(hooks, list):
        out: list[dict[str, Any]] = []
        for entry in hooks:
            if not isinstance(entry, dict):
                continue
            if str(entry.get("type", "")).lower() == "prompt":
                out.append(dict(entry))
                continue
            command = _normalize_command(entry)
            if command:
                out.append({**entry, "command": command})
        return out
    return _load_claude_hooks(path)


def hook_script_hashes(plugin_root: Path) -> dict[str, str]:
    """`{hook file: {script label: sha256}}` flattened to `"file::label"` keys, for `plugin_trust.json`."""
    hashes: dict[str, str] = {}
    for hook_file in _plugin_hook_files(plugin_root):
        for hook in load_hook_file(hook_file):
            command = hook.get("command")
            if not isinstance(command, list):
                continue
            digest = script_sha256(command, plugin_root)
            if digest is not None:
                hashes[f"{hook_file.name}::{_hook_label(hook)}"] = digest
    return hashes


def _plugin_hook_files(plugin_root: Path) -> list[Path]:
    files = (
        sorted((plugin_root / "hooks").glob("*.json"))
        if (plugin_root / "hooks").is_dir()
        else []
    )
    if (plugin_root / "hooks.json").is_file():
        files.append(plugin_root / "hooks.json")
    return files


def load_plugin_hooks(
    config_dir: Path, *, project_root: Path | None = None
) -> list[dict[str, Any]]:
    """Hooks shipped by trusted plugins (Open Plugins `hooks/*.json`), hash-pinned at trust time (ROADMAP #64).

    A plugin whose hook scripts changed since `/plugin trust` is skipped with a
    warning — re-trust it to accept the new scripts.
    """
    try:
        from bog_agents_cli.plugin_spec import (
            _read_trust,
            is_plugin_trusted,
            plugin_roots,
            trust_store_path,
        )
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    try:
        roots = plugin_roots(config_dir=config_dir, project_root=project_root)
    except Exception:
        logger.debug("plugin discovery failed", exc_info=True)
        return out
    trusted = _read_trust(trust_store_path(config_dir)).get("trusted", {})
    for root, _scope in roots:
        if not is_plugin_trusted(root, config_dir=config_dir):
            continue
        key = str(root.resolve()).replace("\\", "/").lower()
        pinned = (
            trusted.get(key, {}).get("hook_hashes")
            if isinstance(trusted, dict)
            else None
        )
        current = hook_script_hashes(root)
        if isinstance(pinned, dict) and pinned != current:
            logger.warning(
                "Plugin %s: hook scripts changed since it was trusted; its hooks are ignored until /plugin trust",
                root.name,
            )
            continue
        for hook_file in _plugin_hook_files(root):
            out.extend(
                pin_hook_hashes(
                    [_absolutize(h, root) for h in load_hook_file(hook_file)], root
                )
            )
    return out


def _absolutize(hook: dict[str, Any], root: Path) -> dict[str, Any]:
    """Resolve a plugin hook's relative script paths against the plugin root (hooks run from any cwd)."""
    command = hook.get("command")
    if not isinstance(command, list):
        return hook
    resolved = [
        str(root / arg)
        if not Path(arg).is_absolute() and (root / arg).is_file()
        else arg
        for arg in command
    ]
    return {**hook, "command": resolved}


def load_decision_hooks(
    project_root: Path,
    *,
    config_hooks: list[dict[str, Any]] | None = None,
    event: str,
    plugin_hooks: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Decision hooks for one event: vendor files + bog `hooks.json` + trusted plugin hooks."""
    hooks = [
        h
        for h in load_vendor_hooks(project_root)
        if event in (h.get("events") or [event])
    ]
    for hook in list(config_hooks or []) + list(plugin_hooks or []):
        events = hook.get("events")
        if (
            events
            and event in events
            and (
                isinstance(hook.get("command"), list)
                or str(hook.get("type", "")).lower() == "prompt"
            )
        ):
            hooks.append(hook)
    return hooks


def model_switch_refusal(
    project_root: str | Path,
    current: str,
    target: str,
    *,
    config_hooks: list[dict[str, Any]] | None = None,
) -> str | None:
    """Why a `PreModelSwitch` hook refuses switching to `target`, or `None` (ROADMAP #64)."""
    if config_hooks is None:
        from bog_agents_cli.hooks import _load_hooks

        config_hooks = _load_hooks()
    hooks = load_decision_hooks(
        Path(project_root), config_hooks=config_hooks, event="PreModelSwitch"
    )
    if not hooks:
        return None
    decision = evaluate_decision_hooks(
        "PreModelSwitch", {"from": current, "to": target}, hooks, tool_name=target
    )
    return decision.reason or "denied" if decision.blocks else None


def approval_hook_verdicts(
    action_requests: list[dict[str, Any]], project_root: Path
) -> tuple[dict[int, str], bool]:
    """For the approval UI: `({index: reason} denied by PermissionRequest hooks, force_prompt)` (ROADMAP #64).

    `force_prompt` is set when a `PreToolUse` hook with `on_failure: ask` failed
    for any call in the batch — the batch must then be shown to a human even in
    auto-approve modes.
    """
    from bog_agents_cli.hooks import _load_hooks

    config = _load_hooks()
    permission_hooks = load_decision_hooks(
        project_root, config_hooks=config, event="PermissionRequest"
    )
    ask_hooks = [
        h
        for h in load_decision_hooks(
            project_root, config_hooks=config, event="PreToolUse"
        )
        if str(h.get("on_failure", "")).lower() == "ask"
    ]
    denied: dict[int, str] = {}
    force_prompt = False
    for index, req in enumerate(action_requests):
        name = str(req.get("name", ""))
        payload = {"tool": name, "args": req.get("args", {}) or {}}
        if permission_hooks:
            verdict = evaluate_decision_hooks(
                "PermissionRequest", payload, permission_hooks, tool_name=name
            )
            if verdict.blocks:
                denied[index] = verdict.reason
                continue
        if (
            ask_hooks
            and evaluate_decision_hooks(
                "PreToolUse", payload, ask_hooks, tool_name=name
            ).asks
        ):
            force_prompt = True
    return denied, force_prompt


def announce_model_switch(spec: str, previous: str = "") -> None:
    """Fire the observe-only `PostModelSwitch` hook (ROADMAP #64)."""
    try:
        from bog_agents_cli.hooks import dispatch_hook_fire_and_forget

        dispatch_hook_fire_and_forget(
            "PostModelSwitch", {"model": spec, "previous": previous}
        )
    except Exception:
        logger.debug("PostModelSwitch dispatch failed", exc_info=True)


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
    return load_decision_hooks(
        project_root, config_hooks=config_hooks, event="PreToolUse"
    )


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
        logger.debug("Decision hook failed: %s — %s", command, exc)
        return HookDecision(
            failed=True, reason=f"{command[0]}: {exc.__class__.__name__}"
        )
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
    "announce_model_switch",
    "approval_hook_verdicts",
    "evaluate_decision_hooks",
    "hook_script_hashes",
    "hook_type_for_event",
    "load_decision_hooks",
    "load_hook_file",
    "load_plugin_hooks",
    "load_pretooluse_hooks",
    "load_vendor_hooks",
    "model_switch_refusal",
    "parse_hook_decision",
    "pin_hook_hashes",
    "script_sha256",
]
