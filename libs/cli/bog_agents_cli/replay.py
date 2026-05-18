"""Record + replay for agent sessions, YAML edition.

A *recording* captures the user prompts, assistant messages, and tool calls
made during a CLI session. After recording, an auto-variabilizer pass
identifies likely parameters (Jira tickets, repo names, URLs, paths) and
turns them into ``${var}`` placeholders so the run can be replayed against
different inputs.

Recordings are saved as YAML for easy hand-editing. The legacy JSON loader
is preserved so older recordings still load.

File layout::

    ~/.bog-agents/replays/
        <session_id>.yaml    # new recordings
        <session_id>.json    # legacy recordings (still loadable)

YAML schema::

    session_id: 2026-05-03T10-15-22Z
    name: "Fetch ticket + open PR"
    description: ""
    recorded_at: 1732208122.0
    original_context:
      cwd: /work/myproject
    vars:
      jira_ticket: { type: string, default: "JIRA-134" }
      repo:        { type: string, default: "myorg/myrepo" }
      gh_token:    { type: secret }
    steps:
      - kind: user_message
        content: "Fetch ${jira_ticket} requirements then open a PR for ${repo}"
      - kind: tool_call
        tool: jira__get_issue
        args: { issue_id: "${jira_ticket}" }
      - kind: ai_message
        content: "Done. Created PR #42."

The ``vars`` block is a :class:`bog_agents_cli.vars.VarBundle` declaration.
``${name}`` substitution flows through that bundle at replay time, prompting
for any unfilled values (and routing secrets through the in-memory vault).
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from bog_agents_cli.vars import auto_variabilize

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ReplayStep:
    """A single recorded action.

    Attributes:
        kind: One of ``user_message``, ``ai_message``, ``tool_call``.
        content: Free-form text for messages.
        tool: Tool name for ``tool_call`` steps.
        args: Tool arguments (already variabilized at save time).
        result_pattern: First ~200 chars of the tool result (informational
            only — not used for matching during replay).
    """

    kind: str
    content: str = ""
    tool: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    result_pattern: str = ""


@dataclass
class ReplaySession:
    """A complete recorded session."""

    session_id: str
    name: str = ""
    description: str = ""
    recorded_at: float = 0.0
    original_context: dict[str, Any] = field(default_factory=dict)
    vars_spec: dict[str, dict[str, Any]] = field(default_factory=dict)
    steps: list[ReplayStep] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Recorder
# ---------------------------------------------------------------------------


class SessionRecorder:
    """Captures user/assistant/tool events into a :class:`ReplaySession`.

    The recorder is intentionally dumb — it appends events as they arrive
    via :meth:`record_user_message`, :meth:`record_ai_message`, and
    :meth:`record_tool_call`. Variabilization runs once at :meth:`finalize`
    so we can scan the entire transcript for repeated values.
    """

    def __init__(self, session_id: str | None = None, name: str = "") -> None:
        self._session = ReplaySession(
            session_id=session_id or _new_session_id(),
            name=name,
            recorded_at=time.time(),
        )
        self._recording = False

    @property
    def session_id(self) -> str:
        return self._session.session_id

    @property
    def is_recording(self) -> bool:
        return self._recording

    def start(self, *, context: dict[str, Any] | None = None) -> None:
        self._recording = True
        self._session.original_context = context or {}
        logger.info("recording started: %s", self._session.session_id)

    def stop(self) -> None:
        self._recording = False
        logger.info(
            "recording stopped: %s (%d steps)",
            self._session.session_id,
            len(self._session.steps),
        )

    def _bounded(self) -> bool:
        """Return True if the session is at its step cap (drop the next event)."""
        from bog_agents_cli._constants import REPLAY_MAX_STEPS

        return len(self._session.steps) >= REPLAY_MAX_STEPS

    def record_user_message(self, content: str) -> None:
        if not self._recording or not content or self._bounded():
            return
        self._session.steps.append(ReplayStep(kind="user_message", content=content))

    def record_ai_message(self, content: str) -> None:
        if not self._recording or not content or self._bounded():
            return
        # Cap AI messages — they tend to be long and we mainly need them as
        # a narrative reference for the user when editing the recording.
        from bog_agents_cli._constants import REPLAY_AI_MESSAGE_MAX_BYTES

        self._session.steps.append(
            ReplayStep(kind="ai_message", content=content[:REPLAY_AI_MESSAGE_MAX_BYTES])
        )

    def record_tool_call(
        self, tool: str, args: dict[str, Any], result: str = ""
    ) -> None:
        if not self._recording or not tool or self._bounded():
            return
        # L1: redact obvious credential-bearing keys *before* they hit
        # disk. Recordings are stored under ~/.bog-agents/replays/ in
        # plain YAML — anyone with the file gets these values. The
        # denylist matches keys exactly and as a substring, since some
        # callers pass camelCase ``apiKey`` and others pass
        # ``Authorization``-style header names.
        safe_args = _redact_secrets(args or {})
        self._session.steps.append(
            ReplayStep(
                kind="tool_call",
                tool=tool,
                args=safe_args,
                result_pattern=(result or "")[:200],
            )
        )

    def finalize(self) -> ReplaySession:
        """Run the auto-variabilizer pass and return the finished session.

        Variables are extracted from user-message content and from any
        string-valued tool args. The same literal across multiple steps
        gets the same placeholder name.
        """
        # First pass: collect literals across the session into a single
        # placeholder map so the same value appearing in two steps maps to
        # the same ${name}.
        global_vars: dict[str, str] = {}
        rewrites: list[tuple[ReplayStep, str | None, dict[str, Any] | None]] = []
        # Collect all candidate strings.
        for step in self._session.steps:
            if step.kind == "user_message":
                rewritten, _vmap = _shared_variabilize(step.content, global_vars)
                rewrites.append((step, rewritten, None))
            elif step.kind == "tool_call":
                new_args = {}
                for k, v in step.args.items():
                    if isinstance(v, str):
                        new_v, _ = _shared_variabilize(v, global_vars)
                        new_args[k] = new_v
                    else:
                        new_args[k] = v
                rewrites.append((step, None, new_args))
            else:
                rewrites.append((step, None, None))

        for step, new_content, new_args in rewrites:
            if new_content is not None:
                step.content = new_content
            if new_args is not None:
                step.args = new_args

        # Build the spec block.
        for name, default_value in global_vars.items():
            self._session.vars_spec[name] = {"type": "string", "default": default_value}

        return self._session


def _shared_variabilize(
    text: str, global_vars: dict[str, str]
) -> tuple[str, dict[str, str]]:
    """Variabilize ``text`` while sharing the placeholder map across calls.

    Args:
        text: Raw text.
        global_vars: Mutable map of var name → original literal, shared
            across the whole session so the same Jira ID picked up in two
            user messages collapses to one placeholder.

    Returns:
        Tuple of (rewritten_text, vars_added_this_call).
    """
    # First, replace any already-known literal with its existing placeholder.
    rewritten = text
    for name, literal in global_vars.items():
        rewritten = rewritten.replace(literal, "${" + name + "}")
    # Then run a fresh pass on whatever's left.
    rewritten, new_map = auto_variabilize(rewritten)
    # Promote new entries into global_vars under unique names.
    fresh: dict[str, str] = {}
    for name, literal in new_map.items():
        chosen = name
        # Avoid clashing with names already in global_vars.
        n = 1
        while chosen in global_vars:
            n += 1
            chosen = f"{name}_{n}"
        global_vars[chosen] = literal
        fresh[chosen] = literal
        if chosen != name:
            rewritten = rewritten.replace("${" + name + "}", "${" + chosen + "}")
    return rewritten, fresh


# L1: keys whose values are stripped from on-disk tool-call recordings.
# Match is case-insensitive substring. Keep this list conservative —
# we'd rather over-redact a benign field than leak a credential.
_REDACT_KEY_SUBSTRINGS = (
    "api_key",
    "apikey",
    "password",
    "passwd",
    "secret",
    "token",
    "authorization",
    "auth_header",
    "private_key",
    "client_secret",
)

_REDACTED_PLACEHOLDER = "***REDACTED***"


def _redact_secrets(args: dict[str, Any]) -> dict[str, Any]:
    """Return a shallow copy of *args* with credential-bearing values masked.

    Walks one level of nested dicts and lists since tool-call args are
    usually flat but a few wrappers ship a single ``headers`` dict.
    Strings only — non-string values that match a denylist key are
    replaced with the literal placeholder too, since a bool/int there
    almost certainly indicates a malformed arg the user doesn't want
    captured either.
    """

    def _redact_value(key: str, value: Any) -> Any:
        if any(needle in key.lower() for needle in _REDACT_KEY_SUBSTRINGS):
            return _REDACTED_PLACEHOLDER
        if isinstance(value, dict):
            return {k: _redact_value(k, v) for k, v in value.items()}
        if isinstance(value, list):
            return [
                _redact_value(key, item)
                if not isinstance(item, dict)
                else {k: _redact_value(k, v) for k, v in item.items()}
                for item in value
            ]
        return value

    return {k: _redact_value(k, v) for k, v in args.items()}


def _new_session_id() -> str:
    """Return a stable, sortable session id (timestamp + short uuid)."""
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    short = uuid.uuid4().hex[:6]
    return f"{stamp}-{short}"


# ---------------------------------------------------------------------------
# Save / load
# ---------------------------------------------------------------------------


def session_to_dict(session: ReplaySession) -> dict[str, Any]:
    """Serialize a :class:`ReplaySession` to a JSON/YAML-friendly dict."""
    return {
        "session_id": session.session_id,
        "name": session.name,
        "description": session.description,
        "recorded_at": session.recorded_at,
        "original_context": session.original_context,
        "vars": session.vars_spec,
        "steps": [_step_to_dict(s) for s in session.steps],
    }


def _step_to_dict(step: ReplayStep) -> dict[str, Any]:
    out: dict[str, Any] = {"kind": step.kind}
    if step.content:
        out["content"] = step.content
    if step.tool:
        out["tool"] = step.tool
    if step.args:
        out["args"] = step.args
    if step.result_pattern:
        out["result_pattern"] = step.result_pattern
    return out


def session_from_dict(data: dict[str, Any]) -> ReplaySession:
    """Inverse of :func:`session_to_dict`. Tolerates legacy JSON shapes."""
    # Legacy v1: 'actions' instead of 'steps' with 'action_type', 'tool_name', 'tool_args'.
    raw_steps = data.get("steps") or data.get("actions") or []
    steps: list[ReplayStep] = []
    for s in raw_steps:
        kind = s.get("kind") or _legacy_kind(s.get("action_type", ""))
        steps.append(
            ReplayStep(
                kind=kind,
                content=s.get("content", ""),
                tool=s.get("tool", s.get("tool_name", "")),
                args=s.get("args", s.get("tool_args", {})) or {},
                result_pattern=s.get("result_pattern", ""),
            )
        )
    # Legacy v1 stored vars as flat dict[str,str] (default values, no spec).
    raw_vars = data.get("vars", data.get("variables", {})) or {}
    vars_spec: dict[str, dict[str, Any]] = {}
    for name, spec_or_default in raw_vars.items():
        if isinstance(spec_or_default, dict):
            vars_spec[name] = spec_or_default
        else:
            vars_spec[name] = {"type": "string", "default": str(spec_or_default)}

    return ReplaySession(
        session_id=str(data.get("session_id", _new_session_id())),
        name=str(data.get("name", "")),
        description=str(data.get("description", "")),
        recorded_at=float(data.get("recorded_at", 0.0) or 0.0),
        original_context=dict(data.get("original_context", {}) or {}),
        vars_spec=vars_spec,
        steps=steps,
    )


def _legacy_kind(action_type: str) -> str:
    """Translate legacy action_type to current kind."""
    if action_type == "tool_call":
        return "tool_call"
    if action_type in ("user_message", "ai_message"):
        return action_type
    return action_type or "user_message"


def save_replay_session(config_dir: Path, session: ReplaySession) -> Path:
    """Save a session to ``<config_dir>/replays/<session_id>.yaml``.

    Args:
        config_dir: User config root (e.g. ``~/.bog-agents``).
        session: The session to save.

    Returns:
        Path to the written YAML file.
    """
    replays_dir = config_dir / "replays"
    replays_dir.mkdir(parents=True, exist_ok=True)
    path = replays_dir / f"{session.session_id}.yaml"
    text = "# bog-agents recorded session — edit freely.\n"
    text += "# 'vars' is a typed declaration; values resolve at replay time.\n"
    text += "# Use ${var_name} placeholders inside content/args to inject values.\n\n"
    text += yaml.safe_dump(
        session_to_dict(session), sort_keys=False, allow_unicode=True
    )
    path.write_text(text, encoding="utf-8")
    return path


def load_replay_session(file_path: Path) -> ReplaySession:
    """Load a session from disk. Accepts either YAML (.yaml/.yml) or JSON.

    Args:
        file_path: Path to the recording file.

    Returns:
        Parsed :class:`ReplaySession`.
    """
    raw = file_path.read_text(encoding="utf-8")
    suffix = file_path.suffix.lower()
    if suffix in (".yaml", ".yml"):
        data = yaml.safe_load(raw) or {}
    elif suffix == ".json":
        data = json.loads(raw)
    else:
        # Sniff: does it start with `{`? Treat as JSON.
        stripped = raw.lstrip()
        data = (
            json.loads(raw) if stripped.startswith("{") else (yaml.safe_load(raw) or {})
        )
    if not isinstance(data, dict):
        msg = f"recording {file_path} did not parse to a dict"
        raise ValueError(msg)
    return session_from_dict(data)


def list_replay_sessions(config_dir: Path) -> list[ReplaySession]:
    """List all saved recordings in ``<config_dir>/replays/``.

    Returns sessions sorted by ``recorded_at`` descending (most recent first).
    Both YAML and JSON files are picked up; corrupt files are skipped with a
    warning.
    """
    replays_dir = config_dir / "replays"
    if not replays_dir.exists():
        return []
    sessions: list[ReplaySession] = []
    for path in sorted(replays_dir.iterdir()):
        if path.suffix.lower() not in (".yaml", ".yml", ".json"):
            continue
        try:
            sessions.append(load_replay_session(path))
        except (
            yaml.YAMLError,
            json.JSONDecodeError,
            OSError,
            ValueError,
            KeyError,
        ) as exc:
            logger.warning("skipping unparseable recording %s: %s", path, exc)
    sessions.sort(key=lambda s: s.recorded_at, reverse=True)
    return sessions


def find_replay_session(config_dir: Path, session_id: str) -> Path | None:
    """Resolve ``session_id`` to a recording file.

    Checks for ``<id>.yaml``, ``<id>.yml``, and ``<id>.json`` in the
    replays directory. ``session_id`` may also be a substring — the first
    match wins (sorted alphabetically).
    """
    replays_dir = config_dir / "replays"
    if not replays_dir.exists():
        return None
    for ext in (".yaml", ".yml", ".json"):
        candidate = replays_dir / f"{session_id}{ext}"
        if candidate.is_file():
            return candidate
    # Substring match.
    matches = sorted(p for p in replays_dir.iterdir() if session_id in p.stem)
    return matches[0] if matches else None


# ---------------------------------------------------------------------------
# Drive-script emission
#
# Recordings are replayed by feeding their user prompts back into the
# drive runner (``bog-agents drive``). The earlier ``build_replay_prompt``
# helper baked the whole session into a single markdown blob and asked
# the LLM to follow it as a reference — which never round-tripped tool
# calls faithfully and never actually drove the TUI. This function emits
# a proper drive script: each user message becomes a ``Submit`` action
# followed by ``wait_for_idle``, with tool calls preserved as comments
# so a reviewer can see what the original agent did.
# ---------------------------------------------------------------------------


def session_to_drive_yaml(session: ReplaySession) -> str:
    """Render a recorded session as a ``bog-agents drive``-compatible YAML.

    The output is a complete drive script with:
        * a ``vars:`` block carrying every detected ``${var}`` (so a
          reviewer can edit defaults before re-running);
        * one ``submit:`` + ``wait_for_idle`` pair per recorded user
          message, in order;
        * comments above each user message describing the tool calls
          the original agent made and a preview of its response, so
          deviations show up in the diff against the new transcript.

    Args:
        session: Loaded replay session.

    Returns:
        YAML string ready to be written to disk.
    """
    spec_block: dict[str, Any] = {
        "name": session.name or session.session_id,
        "description": session.description
        or f"Drive script generated from recording {session.session_id}",
        "session": {
            "cwd": session.original_context.get("cwd", "."),
            "model": "fake:Recording replays use a fixed model by default.",
            "approval_mode": "auto-all",
        },
        "vars": session.vars_spec or {},
    }

    steps: list[Any] = []
    pending_comments: list[str] = []
    for step in session.steps:
        if step.kind == "tool_call":
            args_preview = json.dumps(step.args, default=str, ensure_ascii=False)
            if len(args_preview) > 200:
                args_preview = args_preview[:200] + "...(truncated)"
            pending_comments.append(
                f"original agent called {step.tool}({args_preview})"
            )
            continue
        if step.kind == "ai_message":
            snippet = step.content[:160].replace("\n", " ")
            pending_comments.append(f"original agent replied: {snippet}")
            continue
        if step.kind != "user_message":
            continue
        for note in pending_comments:
            steps.append({"_comment": note})
        pending_comments.clear()
        steps.append({"submit": {"value": step.content}})
        steps.append({"wait_for_idle": 60})
    # Trailing comments after the last user_message — keep them as a tail.
    for note in pending_comments:
        steps.append({"_comment": note})

    out = yaml.safe_dump(spec_block, sort_keys=False, allow_unicode=True)
    out += "\nsteps:\n"
    for step in steps:
        if "_comment" in step:
            out += f"  # {step['_comment']}\n"
            continue
        out += "  - " + yaml.safe_dump(step, default_flow_style=True).strip() + "\n"
    return out


def save_drive_script_for_session(config_dir: Path, session: ReplaySession) -> Path:
    """Write the drive-script form of *session* next to its YAML recording.

    Returns the on-disk path.
    """
    replays_dir = config_dir / "replays"
    replays_dir.mkdir(parents=True, exist_ok=True)
    path = replays_dir / f"{session.session_id}.drive.yaml"
    body = "# bog-agents drive script — generated from a /record session.\n"
    body += "# Edit freely; re-run with `bog-agents --drive <this-file>`.\n\n"
    body += session_to_drive_yaml(session)
    path.write_text(body, encoding="utf-8")
    return path
