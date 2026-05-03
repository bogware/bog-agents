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

from bog_agents_cli.vars import VarBundle, auto_variabilize

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
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
        logger.info("recording stopped: %s (%d steps)", self._session.session_id, len(self._session.steps))

    def record_user_message(self, content: str) -> None:
        if not self._recording or not content:
            return
        self._session.steps.append(ReplayStep(kind="user_message", content=content))

    def record_ai_message(self, content: str) -> None:
        if not self._recording or not content:
            return
        # Cap AI messages — they tend to be long and we mainly need them as
        # a narrative reference for the user when editing the recording.
        self._session.steps.append(ReplayStep(kind="ai_message", content=content[:2000]))

    def record_tool_call(self, tool: str, args: dict[str, Any], result: str = "") -> None:
        if not self._recording or not tool:
            return
        self._session.steps.append(
            ReplayStep(
                kind="tool_call",
                tool=tool,
                args=dict(args or {}),
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


def _shared_variabilize(text: str, global_vars: dict[str, str]) -> tuple[str, dict[str, str]]:
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
    text += yaml.safe_dump(session_to_dict(session), sort_keys=False, allow_unicode=True)
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
        data = json.loads(raw) if stripped.startswith("{") else (yaml.safe_load(raw) or {})
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
        except (yaml.YAMLError, json.JSONDecodeError, OSError, ValueError, KeyError) as exc:
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
# Replay prompt builder
# ---------------------------------------------------------------------------


def build_replay_prompt(session: ReplaySession, bundle: VarBundle) -> str:
    """Render a session into an LLM-ready prompt with vars substituted.

    The output is a structured instruction block the agent can execute.
    Steps are described, not literally re-issued: the agent decides which
    tools to call (the recording is a *reference*, not a script).

    Args:
        session: Loaded session.
        bundle: A :class:`VarBundle` whose values have already been
            resolved (call ``await bundle.resolve(...)`` first).

    Returns:
        A markdown-style prompt for the agent.
    """
    title = session.name or session.session_id
    lines = [
        f"# Replay: {title}",
        "",
        session.description or "Replaying a previously recorded agent session.",
        "",
        "Follow these steps in order, adapting to the current environment as needed.",
        "Use the variables below — they have already been resolved for this run.",
        "",
        "## Variables",
    ]
    if not bundle.specs:
        lines.append("(none)")
    else:
        for name in bundle.specs:
            spec = bundle.specs[name]
            if spec.type == "secret":
                lines.append(f"- `{name}`: <secret> (use it directly when a tool needs it)")
            else:
                value = bundle.get(name)
                lines.append(f"- `{name}`: {value!r}")

    lines.extend(["", "## Steps", ""])
    for i, step in enumerate(session.steps, 1):
        if step.kind == "user_message":
            rendered = bundle.substitute(step.content)
            lines.append(f"{i}. **User originally said:** {rendered}")
        elif step.kind == "tool_call":
            rendered_args = bundle.substitute(step.args)
            args_str = ", ".join(f"{k}={v!r}" for k, v in rendered_args.items())
            lines.append(f"{i}. Call `{step.tool}({args_str})`")
            if step.result_pattern:
                lines.append(f"   _Original result hint:_ {step.result_pattern}")
        elif step.kind == "ai_message":
            # AI messages are hints for the agent on what was previously said —
            # not strict instructions. Keep them short.
            snippet = step.content[:200].replace("\n", " ")
            lines.append(f"{i}. _Previously the agent said:_ {snippet}")
        lines.append("")

    lines.extend(
        [
            "## Notes",
            "",
            "- The recorded steps are a reference. If the new context requires a different approach, follow it.",
            "- Verify each step succeeds before moving on. Stop and report if a step fails irrecoverably.",
        ]
    )
    return "\n".join(lines)
