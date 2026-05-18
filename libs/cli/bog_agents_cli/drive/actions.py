"""Action grammar for `bog-agents drive` scripts.

A *drive script* is a YAML document that describes the exact sequence
of keystrokes, slash commands, prompts, modal interactions, and
assertions a user would perform against the TUI. The runner in
:mod:`bog_agents_cli.drive.runner` parses one into a :class:`Script`
and replays it under Textual's :class:`~textual.pilot.Pilot`.

Shape (abridged)::

    session:
      cwd: ./fixture-repo
      model: replay:fixture.jsonl     # or "fake:hi" or "anthropic:..."
      approval_mode: explicit         # explicit | auto-all | auto-reads
      vars:
        target: "README.md"
    vars:
      name: { type: string, default: "world" }
    steps:
      - type: "summarize ${target}"
      - submit
      - wait_for_idle: 30
      - expect_transcript_contains: "summary"
      - snapshot: artifacts/shot-1
      - { type: "/help" }              # shorthand: leading slash -> slash action

Strings beginning with ``/`` shorthand to a :class:`Slash` action so
common slash invocations stay compact. Strings beginning with ``!``
likewise shorthand to a shell command via :class:`Shell`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from itertools import starmap
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)


__all__ = [
    "Action",
    "ApprovalRespond",
    "AssertWidget",
    "ExpectModal",
    "ExpectTranscript",
    "Press",
    "Script",
    "ScriptLoadError",
    "SelectOption",
    "SessionConfig",
    "SetEnv",
    "Shell",
    "Slash",
    "Snapshot",
    "Submit",
    "SwitchModel",
    "Type",
    "WaitForIdle",
    "load_script",
    "parse_script",
]


# ---------------------------------------------------------------------------
# Action dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Type:
    """Type *text* into the chat input character-by-character."""

    text: str
    slow: bool = False
    """When True, route each character through Pilot.press(). When False
    (default), set the widget value directly — faster, still exercises
    the same downstream code path on submit."""


@dataclass(frozen=True, slots=True)
class Submit:
    """Submit the currently-typed chat input as a user message.

    When ``value`` is set, the typed contents are replaced with this
    string before submission — convenient for one-shot prompt steps.
    """

    value: str | None = None
    mode: str = "normal"
    """One of normal / command / shell. The chat input infers mode from
    prefixes (``/`` -> command, ``!`` -> shell) but the script can force
    a mode if the prefix is missing or ambiguous."""


@dataclass(frozen=True, slots=True)
class Slash:
    """Shorthand for type + submit with a slash command.

    ``Slash("/help")`` is equivalent to ``Type("/help")`` then
    ``Submit(mode="command")``.
    """

    command: str


@dataclass(frozen=True, slots=True)
class Shell:
    """Shorthand for a ``!shell-command`` invocation."""

    command: str


@dataclass(frozen=True, slots=True)
class Press:
    """Send one or more key chords through Pilot.

    Strings follow Textual's binding syntax (e.g. ``ctrl+c``, ``escape``,
    ``shift+tab``, ``up``, ``enter``).
    """

    keys: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class WaitForIdle:
    """Block until the agent finishes its current turn.

    Considered idle when the app's ``_agent_running`` is False AND no
    pending approval/ask-user widget is mounted. Times out after
    ``timeout_seconds``.
    """

    timeout_seconds: float = 30.0


@dataclass(frozen=True, slots=True)
class ExpectTranscript:
    """Assert that the rendered transcript contains a regex match.

    ``pattern`` is a Python regex evaluated against the concatenated
    text of every message in :class:`MessageStore`. Anchors and flags
    are honored; use ``(?s)`` for dot-all if needed. Optionally
    restricts the search to a message ``type`` (user/assistant/tool/etc).
    """

    pattern: str
    timeout_seconds: float = 10.0
    message_type: str | None = None


@dataclass(frozen=True, slots=True)
class ExpectModal:
    """Assert that a modal screen of the given class name is mounted.

    Matches the screen class's ``__name__`` (case-insensitive). Common
    values: ``ModelSelector``, ``ApprovalMenu``, ``AskUserMenu``,
    ``HelpScreen``.
    """

    name: str
    timeout_seconds: float = 5.0


@dataclass(frozen=True, slots=True)
class SelectOption:
    """Pick an option inside a modal by visible label or index.

    Walks the topmost modal screen for a focusable item matching
    ``label`` (substring, case-insensitive). When ``index`` is set
    instead, picks the Nth item. Activates by pressing Enter once
    focused.
    """

    label: str | None = None
    index: int | None = None


@dataclass(frozen=True, slots=True)
class ApprovalRespond:
    """Respond to the next pending tool-approval dialog.

    ``choice`` is one of:
        * ``"approve"`` / ``"yes"``: approve once
        * ``"auto"``: approve and enable auto-approve for this tool
        * ``"deny"`` / ``"no"`` / ``"reject"``: deny

    When ``wait`` is True (default) the action blocks until an approval
    widget appears (or the timeout fires).
    """

    choice: str = "approve"
    timeout_seconds: float = 5.0
    wait: bool = True


@dataclass(frozen=True, slots=True)
class Snapshot:
    """Capture an SVG screenshot + text-grid dump of the current screen.

    ``path`` is the artifact stem; the runner writes ``<path>.svg`` and
    ``<path>.txt`` side by side. Relative paths resolve against the
    runner's ``artifact_dir``.
    """

    path: str


@dataclass(frozen=True, slots=True)
class AssertWidget:
    """Assert a widget exists and (optionally) its rendered text matches a regex.

    ``selector`` is a Textual CSS query (e.g. ``"#status-bar"``,
    ``"StatusBar"``, ``".error-row"``). ``text_matches`` is an optional
    regex against the widget's ``str(widget.render())``.
    """

    selector: str
    text_matches: str | None = None


@dataclass(frozen=True, slots=True)
class SetEnv:
    """Set environment variables for subsequent steps."""

    values: dict[str, str]


@dataclass(frozen=True, slots=True)
class SwitchModel:
    """Switch the active model mid-run via the same code path /model uses."""

    model: str


# Union for static dispatch; the runner matches on type().
Action = (
    Type
    | Submit
    | Slash
    | Shell
    | Press
    | WaitForIdle
    | ExpectTranscript
    | ExpectModal
    | SelectOption
    | ApprovalRespond
    | Snapshot
    | AssertWidget
    | SetEnv
    | SwitchModel
)


# ---------------------------------------------------------------------------
# Script + session config
# ---------------------------------------------------------------------------


_VALID_APPROVAL_MODES = frozenset({"explicit", "auto-all", "auto-reads"})


@dataclass(frozen=True, slots=True)
class SessionConfig:
    """Configuration the runner applies before the first step runs.

    Attributes:
        cwd: Working directory the BogAgentsApp launches in. Resolved
            relative to the script file's parent directory.
        model: Model spec. Accepts the same provider:name syntax the
            real CLI takes, plus two driver-only schemes:
                ``replay:<fixture-path>`` — feed pre-recorded LLM chunks.
                ``fake:<text>`` — single fixed response, no fixture file.
        thread_id: Optional thread id. Defaults to ``drive-<random>``.
        approval_mode: How HITL approvals are handled when the script
            doesn't explicitly consume them. ``explicit`` (default)
            leaves them pending; ``auto-all`` approves anything;
            ``auto-reads`` approves read-only tools and prompts on the
            rest.
        vars: Pre-resolved ${var} values (override script-level vars).
        no_mcp: Disable MCP tool loading for the run (default True since
            drive scripts should be deterministic).
        env: Process env vars to set before the app boots.
    """

    cwd: str | None = None
    model: str = "fake:Hello from drive-fake."
    thread_id: str | None = None
    approval_mode: str = "explicit"
    vars: dict[str, str] = field(default_factory=dict)
    no_mcp: bool = True
    env: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.approval_mode not in _VALID_APPROVAL_MODES:
            msg = (
                f"approval_mode must be one of {sorted(_VALID_APPROVAL_MODES)}, "
                f"got {self.approval_mode!r}"
            )
            raise ValueError(msg)


@dataclass(slots=True)
class Script:
    """A parsed drive script ready to execute.

    ``vars_spec`` is the optional ``vars:`` declaration block from
    :mod:`bog_agents_cli.vars` — the runner uses it to resolve any
    unfilled ``${name}`` placeholders before binding them into
    :attr:`SessionConfig.vars`.
    """

    session: SessionConfig
    steps: list[Action]
    vars_spec: dict[str, dict[str, Any]] = field(default_factory=dict)
    source_path: Path | None = None
    name: str = ""
    description: str = ""


class ScriptLoadError(ValueError):
    """Raised when a script YAML is malformed or has unknown actions."""


# ---------------------------------------------------------------------------
# YAML loading + step parsing
# ---------------------------------------------------------------------------


def load_script(path: Path) -> Script:
    """Read *path* and parse it into a :class:`Script`.

    Args:
        path: Path to a YAML drive script.

    Returns:
        Parsed :class:`Script`. ``source_path`` is set so the runner
        can resolve session.cwd relative to the script file.

    Raises:
        ScriptLoadError: When the YAML is malformed or contains an
            unknown action keyword.
    """
    raw = path.read_text(encoding="utf-8")
    try:
        data = yaml.safe_load(raw) or {}
    except yaml.YAMLError as exc:
        msg = f"failed to parse {path}: {exc}"
        raise ScriptLoadError(msg) from exc
    if not isinstance(data, dict):
        msg = f"{path}: top-level YAML must be a mapping, got {type(data).__name__}"
        raise ScriptLoadError(msg)
    script = parse_script(data)
    script.source_path = path
    return script


def parse_script(data: dict[str, Any]) -> Script:
    """Parse an already-deserialized YAML dict into a :class:`Script`.

    Exposed separately so callers (HTTP server, tests) can feed dicts
    directly without writing a YAML file first.
    """
    session_raw = data.get("session") or {}
    if not isinstance(session_raw, dict):
        msg = f"'session' must be a mapping, got {type(session_raw).__name__}"
        raise ScriptLoadError(msg)
    try:
        session = SessionConfig(
            cwd=session_raw.get("cwd"),
            # Mirror SessionConfig's dataclass default; pulling it via
            # ``SessionConfig.model`` returns a slot descriptor under
            # frozen+slots, not the literal string.
            model=session_raw.get("model", "fake:Hello from drive-fake."),
            thread_id=session_raw.get("thread_id"),
            approval_mode=session_raw.get("approval_mode", "explicit"),
            vars={str(k): str(v) for k, v in (session_raw.get("vars") or {}).items()},
            no_mcp=bool(session_raw.get("no_mcp", True)),
            env={str(k): str(v) for k, v in (session_raw.get("env") or {}).items()},
        )
    except (TypeError, ValueError) as exc:
        msg = f"invalid session block: {exc}"
        raise ScriptLoadError(msg) from exc

    vars_spec_raw = data.get("vars") or {}
    if not isinstance(vars_spec_raw, dict):
        msg = f"'vars' must be a mapping, got {type(vars_spec_raw).__name__}"
        raise ScriptLoadError(msg)

    raw_steps = data.get("steps") or []
    if not isinstance(raw_steps, list):
        msg = f"'steps' must be a list, got {type(raw_steps).__name__}"
        raise ScriptLoadError(msg)
    actions: list[Action] = list(starmap(_parse_step, enumerate(raw_steps)))

    return Script(
        session=session,
        steps=actions,
        vars_spec={
            str(k): dict(v)
            if isinstance(v, dict)
            else {"type": "string", "default": str(v)}
            for k, v in vars_spec_raw.items()
        },
        name=str(data.get("name") or ""),
        description=str(data.get("description") or ""),
    )


def _parse_step(idx: int, step: Any) -> Action:
    """Parse a single ``steps`` entry into an :class:`Action`."""
    # Bare-string shorthand: leading "/" -> Slash, "!" -> Shell, else Submit(value).
    if isinstance(step, str):
        stripped = step.strip()
        if stripped.startswith("/"):
            return Slash(command=stripped)
        if stripped.startswith("!"):
            return Shell(command=stripped[1:])
        if stripped == "submit":
            return Submit()
        msg = (
            f"step {idx}: bare string must be a slash command, "
            f"shell ('!cmd'), or the keyword 'submit'; got {step!r}"
        )
        raise ScriptLoadError(msg)

    if not isinstance(step, dict):
        msg = f"step {idx}: expected mapping or string, got {type(step).__name__}"
        raise ScriptLoadError(msg)

    if len(step) != 1:
        # Multi-key form is supported only for actions whose payload is a
        # mapping (e.g. assert_widget). Pick a known action key and pass
        # the remainder as kwargs.
        known = next((k for k in step if k in _ACTION_BUILDERS), None)
        if known is None:
            msg = (
                f"step {idx}: cannot determine action — keys {sorted(step)} "
                f"don't include a known action name"
            )
            raise ScriptLoadError(msg)
        payload = {k: v for k, v in step.items() if k != known}
        payload.setdefault("_value", step[known])
        return _ACTION_BUILDERS[known](idx, payload)

    key, value = next(iter(step.items()))
    builder = _ACTION_BUILDERS.get(key)
    if builder is None:
        msg = f"step {idx}: unknown action {key!r}. Known: {sorted(_ACTION_BUILDERS)}"
        raise ScriptLoadError(msg)
    return builder(idx, value)


# ---------------------------------------------------------------------------
# Per-action builders
#
# Each builder takes (step_index, value) where value is whatever YAML
# produced for the action's RHS — a scalar, list, or dict. The builder
# normalises it into the dataclass.
# ---------------------------------------------------------------------------


def _b_type(_idx: int, value: Any) -> Type:
    if isinstance(value, dict):
        return Type(
            text=str(value.get("text", "")), slow=bool(value.get("slow", False))
        )
    return Type(text=str(value))


def _b_submit(_idx: int, value: Any) -> Submit:
    if value in (None, "", True):
        return Submit()
    if isinstance(value, str):
        return Submit(value=value)
    if isinstance(value, dict):
        return Submit(value=value.get("value"), mode=str(value.get("mode", "normal")))
    return Submit()


def _b_slash(_idx: int, value: Any) -> Slash:
    if isinstance(value, dict):
        return Slash(command=str(value.get("command", "")))
    return Slash(command=str(value))


def _b_shell(_idx: int, value: Any) -> Shell:
    return Shell(command=str(value))


def _b_press(_idx: int, value: Any) -> Press:
    if isinstance(value, str):
        keys = (value,)
    elif isinstance(value, list):
        keys = tuple(str(k) for k in value)
    else:
        keys = (str(value),)
    return Press(keys=keys)


def _b_wait(idx: int, value: Any) -> WaitForIdle:
    if value is None:
        return WaitForIdle()
    if isinstance(value, dict):
        return WaitForIdle(timeout_seconds=float(value.get("timeout_seconds", 30.0)))
    try:
        return WaitForIdle(timeout_seconds=float(value))
    except (TypeError, ValueError) as exc:
        msg = f"step {idx}: wait_for_idle expects a number of seconds, got {value!r}"
        raise ScriptLoadError(msg) from exc


def _b_expect_transcript(_idx: int, value: Any) -> ExpectTranscript:
    if isinstance(value, str):
        return ExpectTranscript(pattern=value)
    if isinstance(value, dict):
        return ExpectTranscript(
            pattern=str(value.get("pattern") or value.get("_value", "")),
            timeout_seconds=float(value.get("timeout_seconds", 10.0)),
            message_type=value.get("message_type"),
        )
    return ExpectTranscript(pattern=str(value))


def _b_expect_modal(_idx: int, value: Any) -> ExpectModal:
    if isinstance(value, dict):
        return ExpectModal(
            name=str(value.get("name") or value.get("_value", "")),
            timeout_seconds=float(value.get("timeout_seconds", 5.0)),
        )
    return ExpectModal(name=str(value))


def _b_select_option(_idx: int, value: Any) -> SelectOption:
    if isinstance(value, dict):
        return SelectOption(label=value.get("label"), index=value.get("index"))
    if isinstance(value, int):
        return SelectOption(index=value)
    return SelectOption(label=str(value))


def _b_on_approval(_idx: int, value: Any) -> ApprovalRespond:
    if isinstance(value, dict):
        return ApprovalRespond(
            choice=str(value.get("choice", "approve")),
            timeout_seconds=float(value.get("timeout_seconds", 5.0)),
            wait=bool(value.get("wait", True)),
        )
    return ApprovalRespond(choice=str(value))


def _b_snapshot(_idx: int, value: Any) -> Snapshot:
    if isinstance(value, dict):
        return Snapshot(path=str(value.get("path") or value.get("_value", "")))
    return Snapshot(path=str(value))


def _b_assert_widget(idx: int, value: Any) -> AssertWidget:
    if not isinstance(value, dict):
        msg = f"step {idx}: assert_widget expects a mapping, got {type(value).__name__}"
        raise ScriptLoadError(msg)
    return AssertWidget(
        selector=str(value.get("selector", "")),
        text_matches=value.get("text_matches"),
    )


def _b_set_env(idx: int, value: Any) -> SetEnv:
    if not isinstance(value, dict):
        msg = f"step {idx}: set_env expects a mapping, got {type(value).__name__}"
        raise ScriptLoadError(msg)
    return SetEnv(values={str(k): str(v) for k, v in value.items()})


def _b_switch_model(_idx: int, value: Any) -> SwitchModel:
    return SwitchModel(model=str(value))


_ACTION_BUILDERS: dict[str, Any] = {
    "type": _b_type,
    "submit": _b_submit,
    "slash": _b_slash,
    "shell": _b_shell,
    "press": _b_press,
    "wait_for_idle": _b_wait,
    "expect_transcript_contains": _b_expect_transcript,
    "expect_transcript": _b_expect_transcript,
    "expect_modal": _b_expect_modal,
    "select_option": _b_select_option,
    "on_approval": _b_on_approval,
    "approval_respond": _b_on_approval,
    "snapshot": _b_snapshot,
    "assert_widget": _b_assert_widget,
    "set_env": _b_set_env,
    "switch_model": _b_switch_model,
}
