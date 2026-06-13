"""Project-local harness hooks at ``.bog-agents/hooks/``.

Where ``hooks.py`` ships a fire-and-forget JSON-config hook dispatcher,
this module provides the *blocking, response-aware* hooks the user
opted into via ``.bog-agents/hooks/`` directories. These scripts run in
the **harness** (the CLI), not the model, so they can:

* veto a tool call before it runs (``{"action": "block", "reason": "..."}``)
* rewrite tool arguments (``{"action": "modify", "args": {...}}``)
* inject context into a user prompt (``user-prompt`` event with
  ``{"action": "modify", "prompt": "..."}``)
* veto submission of a user message (``{"action": "block", ...}``)

Layout::

    .bog-agents/hooks/
    ├── pre-tool/        # before any tool call
    │   ├── 01-deny-rm-rf.sh
    │   └── 02-audit.py
    ├── post-tool/       # after a tool call
    │   └── notify.sh
    ├── stop/            # agent turn ended (success or interrupt)
    │   └── ding.sh
    └── user-prompt/     # before a user message hits the agent
        └── prepend-rules.sh

Files inside an event directory are run in lexical order. Scripts
receive a JSON event payload on stdin and write a JSON decision on
stdout. Non-zero exit, missing executable bit on POSIX, or unparseable
output are all treated as pass-through (allow with no modifications) so
a misconfigured hook never wedges the CLI.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Maps the high-level event name → directory under .bog-agents/hooks/.
# We accept a couple of natural aliases so users don't have to memorize
# whether the dir is "pre-tool" or "pre_tool".
EVENT_DIR_NAMES: dict[str, tuple[str, ...]] = {
    "pre-tool": ("pre-tool", "pre_tool", "pretool"),
    "post-tool": ("post-tool", "post_tool", "posttool"),
    "stop": ("stop",),
    "user-prompt": ("user-prompt", "user_prompt", "userprompt"),
}

# Per-script hard timeout. Short enough that a runaway hook can't wedge
# the agent for long, generous enough for legitimate work like running
# a quick grep or notifying a webhook.
HOOK_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True, slots=True)
class HookDecision:
    """Result returned by one or more harness hook scripts."""

    blocked: bool = False
    reason: str = ""
    modified_args: dict[str, Any] | None = None
    modified_prompt: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def allowed(self) -> bool:
        """Convenience inverse of ``blocked``."""
        return not self.blocked


def _discover_hooks_dir(project_root: Path) -> Path | None:
    """Return ``project_root/.bog-agents/hooks`` if it exists."""
    candidate = project_root / ".bog-agents" / "hooks"
    return candidate if candidate.is_dir() else None


# Env escape hatch for CI / headless / containers where a human cannot
# answer a trust prompt but the environment is known-good.
_TRUST_ENV = "BOG_AGENTS_TRUST_PROJECT_HOOKS"

# Remember which (root, fingerprint) pairs we've already warned about so an
# untrusted project doesn't spam the log on every prompt/tool call.
_warned_untrusted: set[tuple[str, str]] = set()


def hooks_fingerprint(project_root: Path) -> str:
    """Fingerprint every hook script under ``.bog-agents/hooks/**``.

    Reuses the MCP-trust fingerprint so any added/edited/removed hook script
    changes the fingerprint and forces a re-prompt.
    """
    from bog_agents_cli.mcp_trust import compute_config_fingerprint

    base = _discover_hooks_dir(project_root)
    if base is None:
        return compute_config_fingerprint([])
    scripts = sorted(p for p in base.rglob("*") if p.is_file() and not p.is_symlink())
    return compute_config_fingerprint(scripts)


def is_hooks_execution_allowed(project_root: Path) -> bool:
    """Return True if this project's hook scripts are trusted to execute.

    Deny-by-default (REVIEW.md v2 P0-8): hooks in a freshly-cloned repo must
    NOT run until the user explicitly trusts them. The ``BOG_AGENTS_TRUST_PROJECT_HOOKS``
    env var ("1"/"true"/"yes"/"on") is an opt-in escape hatch for CI/headless.
    """
    if os.environ.get(_TRUST_ENV, "").strip().lower() in {"1", "true", "yes", "on"}:
        return True
    from bog_agents_cli.mcp_trust import is_project_hooks_trusted

    root = str(project_root.resolve())
    fingerprint = hooks_fingerprint(project_root)
    if is_project_hooks_trusted(root, fingerprint):
        return True
    key = (root, fingerprint)
    if key not in _warned_untrusted:
        _warned_untrusted.add(key)
        logger.warning(
            "Project-local hooks under %s/.bog-agents/hooks are NOT trusted and "
            "were skipped (they run arbitrary code). Review them, then set %s=1 "
            "to enable for this project.",
            root,
            _TRUST_ENV,
        )
    return False


def _discover_event_dir(project_root: Path, event: str) -> Path | None:
    """Locate the directory of hook scripts for ``event``, if any."""
    base = _discover_hooks_dir(project_root)
    if base is None:
        return None
    for name in EVENT_DIR_NAMES.get(event, (event,)):
        candidate = base / name
        if candidate.is_dir():
            return candidate
    return None


def _is_executable(path: Path) -> bool:
    """Best-effort POSIX-executable check.

    On Windows the executable bit is meaningless so we accept any regular
    file with a known interpreter-friendly extension. On POSIX we honour
    the user-execute bit.
    """
    try:
        st = path.stat()
    except OSError:
        return False
    if not stat.S_ISREG(st.st_mode):
        return False
    if sys.platform == "win32":
        return path.suffix.lower() in {".bat", ".cmd", ".exe", ".ps1", ".py", ".sh"}
    return bool(st.st_mode & stat.S_IXUSR)


def _list_event_scripts(event_dir: Path) -> list[Path]:
    """Return executable hook scripts in ``event_dir``, lexical order."""
    scripts: list[Path] = []
    for entry in sorted(event_dir.iterdir()):
        if entry.name.startswith(".") or entry.name.startswith("_"):
            continue
        if _is_executable(entry):
            scripts.append(entry)
    return scripts


def _build_command(script: Path) -> list[str]:
    """Pick an interpreter for non-self-executing scripts.

    On Windows ``.py`` won't run by simply invoking the path, and ``.sh``
    needs ``bash``. POSIX trusts the script's shebang and just executes
    the path directly.
    """
    suffix = script.suffix.lower()
    if sys.platform == "win32":
        if suffix == ".py":
            return [sys.executable, str(script)]
        if suffix in {".sh", ".bash"}:
            return ["bash", str(script)]
        return [str(script)]
    return [str(script)]


async def _run_one_script(script: Path, payload_bytes: bytes) -> dict[str, Any] | None:
    """Run one hook script and return its parsed JSON decision (or None)."""
    cmd = _build_command(script)
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (FileNotFoundError, PermissionError, OSError) as exc:
        logger.warning("hook %s could not start: %s", script, exc)
        return None

    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(payload_bytes), timeout=HOOK_TIMEOUT_SECONDS
        )
    except TimeoutError:
        logger.warning(
            "hook %s timed out after %.1fs; killing", script, HOOK_TIMEOUT_SECONDS
        )
        with contextlib.suppress(ProcessLookupError, OSError):
            proc.kill()
        await proc.wait()
        return None
    except Exception:
        logger.warning("hook %s failed during communicate", script, exc_info=True)
        return None

    if proc.returncode != 0:
        snippet = stderr_bytes.decode("utf-8", errors="replace")[:400]
        logger.warning(
            "hook %s exited %d (treating as pass-through): %s",
            script,
            proc.returncode,
            snippet.strip(),
        )
        return None

    raw = stdout_bytes.decode("utf-8", errors="replace").strip()
    if not raw:
        return None
    try:
        decision = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning(
            "hook %s produced non-JSON stdout (%s): %.200s", script, exc, raw
        )
        return None
    if not isinstance(decision, dict):
        logger.warning(
            "hook %s decision must be a JSON object, got %s",
            script,
            type(decision).__name__,
        )
        return None
    return decision


def _project_root() -> Path:
    """Return the most-likely project root.

    Defers to ``BOG_AGENTS_PROJECT_ROOT`` for tests / CI overrides, then
    falls back to ``Path.cwd()``. We deliberately do NOT walk up looking
    for a marker because hook scope is ``$PWD`` by design — running the
    CLI from a subdir should not pick up the parent's hooks.
    """
    override = os.environ.get("BOG_AGENTS_PROJECT_ROOT")
    if override:
        candidate = Path(override).expanduser()
        if candidate.is_dir():
            return candidate
    return Path.cwd()


async def run_hooks(
    event: str,
    payload: dict[str, Any],
    *,
    project_root: Path | None = None,
) -> HookDecision:
    """Run every hook script for ``event`` and combine their decisions.

    The first ``block`` short-circuits — subsequent scripts are not run,
    matching the principle of least surprise (a deny script wins).
    ``modify`` decisions accumulate: each script sees the previous
    script's modifications applied to the payload.

    Args:
        event: One of ``"pre-tool"``, ``"post-tool"``, ``"stop"``,
            ``"user-prompt"`` (or any custom event under .bog-agents/hooks).
        payload: JSON-serializable dict sent to each script on stdin.
            ``"event"`` is added automatically.
        project_root: Override for tests; defaults to ``Path.cwd()``.

    Returns:
        A :class:`HookDecision` summarizing the combined outcome.
    """
    root = project_root or _project_root()
    event_dir = _discover_event_dir(root, event)
    if event_dir is None:
        return HookDecision()

    scripts = _list_event_scripts(event_dir)
    if not scripts:
        return HookDecision()

    # Trust gate (P0-8): never execute hook scripts from an untrusted project.
    if not is_hooks_execution_allowed(root):
        return HookDecision()

    current_payload = {"event": event, **payload}
    notes: list[str] = []
    modified_args: dict[str, Any] | None = None
    modified_prompt: str | None = None

    for script in scripts:
        payload_bytes = json.dumps(current_payload).encode("utf-8")
        decision = await _run_one_script(script, payload_bytes)
        if decision is None:
            continue

        action = str(decision.get("action", "")).lower()
        reason = str(decision.get("reason", "")).strip()

        if action == "block":
            return HookDecision(
                blocked=True,
                reason=reason or f"blocked by {script.name}",
                notes=notes,
            )

        if action == "modify":
            args_update = decision.get("args")
            if isinstance(args_update, dict):
                modified_args = dict(args_update)
                # Mirror the modification into the payload so the next
                # script sees the updated tool args.
                if isinstance(current_payload.get("tool_args"), dict):
                    current_payload["tool_args"] = modified_args
            prompt_update = decision.get("prompt")
            if isinstance(prompt_update, str):
                modified_prompt = prompt_update
                if "prompt" in current_payload:
                    current_payload["prompt"] = prompt_update
            if reason:
                notes.append(f"{script.name}: {reason}")
            continue

        # action == "allow" / unknown: keep going. Any reason becomes a note.
        if reason:
            notes.append(f"{script.name}: {reason}")

    return HookDecision(
        modified_args=modified_args,
        modified_prompt=modified_prompt,
        notes=notes,
    )


__all__ = [
    "EVENT_DIR_NAMES",
    "HOOK_TIMEOUT_SECONDS",
    "HookDecision",
    "run_hooks",
]
