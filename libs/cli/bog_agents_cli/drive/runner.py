"""``bog-agents drive`` runner — executes a parsed :class:`Script`.

The runner is intentionally thin: each action is matched to a handler
that calls a Pilot primitive or pokes app state directly when no Pilot
primitive fits (modal queries, approval dispatch, snapshot capture).

The public surface is :func:`run_script` (async) plus
:func:`build_app_for_script` so tests can swap the app construction
out for one with mocks.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, TextIO

from bog_agents_cli.drive.actions import (
    Action,
    ApprovalRespond,
    AssertWidget,
    ExpectModal,
    ExpectTranscript,
    Press,
    Script,
    SelectOption,
    SessionConfig,
    SetEnv,
    Shell,
    Slash,
    Snapshot,
    Submit,
    SwitchModel,
    Type,
    WaitForIdle,
)
from bog_agents_cli.drive.replay_model import resolve_drive_model
from bog_agents_cli.drive.snapshot import capture_snapshot

if TYPE_CHECKING:
    from bog_agents_cli.app import BogAgentsApp

logger = logging.getLogger(__name__)


__all__ = [
    "RunOptions",
    "ScriptResult",
    "StepResult",
    "build_app_for_script",
    "run_script",
    "run_script_path",
]


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class StepResult:
    """Outcome of executing one :class:`Action`."""

    index: int
    action: str
    ok: bool
    duration_ms: int
    detail: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def to_jsonl(self) -> str:
        record: dict[str, Any] = {
            "step": self.index,
            "action": self.action,
            "ok": self.ok,
            "duration_ms": self.duration_ms,
        }
        if self.detail:
            record["detail"] = self.detail
        if self.error:
            record["error"] = self.error
        return json.dumps(record, ensure_ascii=False, default=str)


@dataclass(slots=True)
class ScriptResult:
    """Aggregate outcome of executing a :class:`Script`."""

    steps: list[StepResult]
    duration_ms: int
    transcript: list[dict[str, Any]] = field(default_factory=list)

    @property
    def failed(self) -> int:
        return sum(1 for s in self.steps if not s.ok)

    @property
    def passed(self) -> int:
        return sum(1 for s in self.steps if s.ok)

    @property
    def exit_code(self) -> int:
        # Cap at 125 so we don't collide with shell-reserved exit codes
        # (126 = not executable, 127 = not found, 128+ = signal).
        return min(self.failed, 125)

    def summary_jsonl(self) -> str:
        return json.dumps(
            {
                "summary": {
                    "total": len(self.steps),
                    "passed": self.passed,
                    "failed": self.failed,
                    "duration_ms": self.duration_ms,
                }
            },
            ensure_ascii=False,
        )


# ---------------------------------------------------------------------------
# Run options
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class RunOptions:
    """Knobs the CLI layer passes into :func:`run_script`.

    Attributes:
        artifact_dir: Where snapshots land when the script uses a
            relative path. Defaults to ``<script-dir>/.drive-artifacts/<ts>``.
        output_stream: JSONL transcript destination. Defaults to stdout.
        stop_on_failure: When True, the run aborts at the first failed
            step; subsequent steps are reported as ``ok: false, error:
            "skipped (prior failure)"``.
        var_overrides: Map of ``${var}`` overrides (CLI ``--var k=v``).
        timeout_ms: Wall-clock cap on the whole run. ``0`` disables.
    """

    artifact_dir: Path | None = None
    output_stream: TextIO | None = None
    stop_on_failure: bool = False
    var_overrides: dict[str, str] = field(default_factory=dict)
    timeout_ms: int = 0


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


async def run_script_path(
    path: Path, options: RunOptions | None = None
) -> ScriptResult:
    """Load a script from *path* and execute it.

    Args:
        path: Path to the YAML script.
        options: Optional runtime knobs.

    Returns:
        The aggregate :class:`ScriptResult`.
    """
    from bog_agents_cli.drive.actions import load_script

    script = load_script(path)
    return await run_script(script, options=options)


async def run_script(
    script: Script,
    *,
    options: RunOptions | None = None,
) -> ScriptResult:
    """Execute *script* under a freshly-booted BogAgentsApp + Pilot."""
    opts = options or RunOptions()
    output = opts.output_stream if opts.output_stream is not None else sys.stdout

    # Resolve ${var} placeholders on every action.
    bundle_vars = _build_resolved_vars(script, opts.var_overrides)
    resolved_steps = [_substitute_action(step, bundle_vars) for step in script.steps]

    # Set process-level env BEFORE app construction so any startup-time
    # reads (e.g. BOG_AGENTS_HOME) see the right value.
    _apply_session_env(script.session)

    # Resolve artifact dir + ensure it exists.
    artifact_dir = opts.artifact_dir or _default_artifact_dir(script)
    artifact_dir.mkdir(parents=True, exist_ok=True)

    run_start = time.perf_counter()
    app = build_app_for_script(script)

    results: list[StepResult] = []
    transcript_snapshot: list[dict[str, Any]] = []

    async with app.run_test(size=(120, 40)) as pilot:
        # Let the app's on_mount finish so widgets, status bar, and the
        # message store are all wired up before we start dispatching.
        await pilot.pause()
        await pilot.pause()

        ctx = _ExecCtx(
            app=app,
            pilot=pilot,
            artifact_dir=artifact_dir,
            session=script.session,
            stop_requested=False,
        )

        for idx, action in enumerate(resolved_steps):
            if ctx.stop_requested and opts.stop_on_failure:
                results.append(
                    StepResult(
                        index=idx,
                        action=_action_name(action),
                        ok=False,
                        duration_ms=0,
                        error="skipped (prior failure)",
                    )
                )
                _emit(output, results[-1].to_jsonl())
                continue

            step_start = time.perf_counter()
            try:
                detail = await _dispatch_action(action, ctx)
                ok = True
                error: str | None = None
            except _ExpectationFailedError as exc:
                detail = exc.detail or {}
                ok = False
                error = str(exc)
                if opts.stop_on_failure:
                    ctx.stop_requested = True
            except TimeoutError as exc:
                detail = {}
                ok = False
                error = f"timeout: {exc}"
                if opts.stop_on_failure:
                    ctx.stop_requested = True
            except Exception as exc:
                logger.debug("drive step %d raised", idx, exc_info=True)
                detail = {}
                ok = False
                error = f"{type(exc).__name__}: {exc}"
                if opts.stop_on_failure:
                    ctx.stop_requested = True

            elapsed = int((time.perf_counter() - step_start) * 1000)
            results.append(
                StepResult(
                    index=idx,
                    action=_action_name(action),
                    ok=ok,
                    duration_ms=elapsed,
                    detail=detail,
                    error=error,
                )
            )
            _emit(output, results[-1].to_jsonl())

        transcript_snapshot = _dump_transcript(app)

    run_elapsed = int((time.perf_counter() - run_start) * 1000)
    summary = ScriptResult(
        steps=results,
        duration_ms=run_elapsed,
        transcript=transcript_snapshot,
    )
    _emit(output, summary.summary_jsonl())
    return summary


# ---------------------------------------------------------------------------
# App construction
# ---------------------------------------------------------------------------


def build_app_for_script(script: Script) -> BogAgentsApp:
    """Construct a :class:`BogAgentsApp` configured for *script*.

    Builds a real agent (driven by the resolved chat model), wires it
    into the app, and disables the deferred-server-startup path so the
    app boots immediately under ``run_test()``.
    """
    from langgraph.checkpoint.memory import InMemorySaver

    from bog_agents_cli.agent import create_cli_agent
    from bog_agents_cli.app import BogAgentsApp

    model = resolve_drive_model(script.session.model)
    cwd = _resolve_cwd(script)

    auto_approve = script.session.approval_mode == "auto-all"

    agent, _backend = create_cli_agent(
        model=model,
        assistant_id="drive",
        cwd=cwd,
        auto_approve=auto_approve,
        enable_memory=False,
        enable_skills=False,
        enable_repo_map=False,
        enable_checkpointing=False,
        enable_cost_tracking=False,
        enable_plan_mode=False,
        enable_git_tools=False,
        interactive=True,
        checkpointer=InMemorySaver(),
    )

    thread_id = script.session.thread_id or f"drive-{uuid.uuid4().hex[:8]}"
    return BogAgentsApp(
        agent=agent,
        assistant_id="drive",
        cwd=cwd,
        thread_id=thread_id,
        auto_approve=auto_approve,
    )


# ---------------------------------------------------------------------------
# Execution context + dispatch
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _ExecCtx:
    app: BogAgentsApp
    pilot: Any
    artifact_dir: Path
    session: SessionConfig
    stop_requested: bool


class _ExpectationFailedError(AssertionError):
    """An expect_* / assert_* step failed cleanly."""

    def __init__(self, message: str, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.detail = detail


async def _dispatch_action(action: Action, ctx: _ExecCtx) -> dict[str, Any]:
    """Run one action and return its detail payload."""
    if isinstance(action, Type):
        return await _do_type(action, ctx)
    if isinstance(action, Submit):
        return await _do_submit(action, ctx)
    if isinstance(action, Slash):
        return await _do_slash(action, ctx)
    if isinstance(action, Shell):
        return await _do_shell(action, ctx)
    if isinstance(action, Press):
        return await _do_press(action, ctx)
    if isinstance(action, WaitForIdle):
        return await _do_wait_for_idle(action, ctx)
    if isinstance(action, ExpectTranscript):
        return await _do_expect_transcript(action, ctx)
    if isinstance(action, ExpectModal):
        return await _do_expect_modal(action, ctx)
    if isinstance(action, SelectOption):
        return await _do_select_option(action, ctx)
    if isinstance(action, ApprovalRespond):
        return await _do_approval_respond(action, ctx)
    if isinstance(action, Snapshot):
        return _do_snapshot(action, ctx)
    if isinstance(action, AssertWidget):
        return await _do_assert_widget(action, ctx)
    if isinstance(action, SetEnv):
        return _do_set_env(action, ctx)
    if isinstance(action, SwitchModel):
        return await _do_switch_model(action, ctx)
    msg = f"unknown action: {type(action).__name__}"
    raise _ExpectationFailedError(msg)


# ---------------------------------------------------------------------------
# Individual handlers
# ---------------------------------------------------------------------------


async def _do_type(action: Type, ctx: _ExecCtx) -> dict[str, Any]:
    chat_input = ctx.app._chat_input
    if chat_input is None:
        msg = "chat input not mounted yet"
        raise _ExpectationFailedError(msg)

    if action.slow:
        # Realistic typing: chord one character at a time. Lets us hit
        # any autocomplete / mode-switch logic the widget runs during
        # composition (e.g. flipping to command mode on '/').
        # Type each character individually since Pilot.press chains use
        # Textual's key parser, which treats individual chars as keys.
        for ch in action.text:
            if ch == " ":
                await ctx.pilot.press("space")
            elif ch == "\n":
                await ctx.pilot.press("enter")
            else:
                await ctx.pilot.press(ch)
        await ctx.pilot.pause()
        return {"text": action.text, "slow": True}

    text_area = getattr(chat_input, "_text_area", None)
    if text_area is None:
        msg = "chat input has no text area"
        raise _ExpectationFailedError(msg)
    set_text = getattr(text_area, "load_text", None) or getattr(text_area, "text", None)
    if callable(set_text):
        set_text(action.text)
    else:
        # Fallback for older Textual: write to .text reactive
        try:
            text_area.text = action.text
        except Exception as exc:
            msg = f"could not set chat input text: {exc}"
            raise _ExpectationFailedError(msg) from exc
    await ctx.pilot.pause()
    return {"text": action.text, "slow": False}


async def _do_submit(action: Submit, ctx: _ExecCtx) -> dict[str, Any]:
    from bog_agents_cli.widgets.chat_input import ChatInput

    chat_input = ctx.app._chat_input
    if chat_input is None:
        msg = "chat input not mounted yet"
        raise _ExpectationFailedError(msg)

    if action.value is not None:
        # Replace the typed text first so a single Submit(value=...) is
        # a complete one-liner.
        await _do_type(Type(text=action.value, slow=False), ctx)

    text_area = getattr(chat_input, "_text_area", None)
    raw_value = ""
    if text_area is not None:
        raw_value = getattr(text_area, "text", "") or ""

    mode = action.mode
    if mode == "normal" and raw_value.startswith("/"):
        mode = "command"
    elif mode == "normal" and raw_value.startswith("!"):
        mode = "shell"

    ctx.app.post_message(ChatInput.Submitted(raw_value, mode))
    # Clear the input so the next type/submit starts clean.
    if text_area is not None:
        clear = getattr(text_area, "clear_text", None)
        if callable(clear):
            clear()
    await ctx.pilot.pause()
    return {"value": raw_value, "mode": mode}


async def _do_slash(action: Slash, ctx: _ExecCtx) -> dict[str, Any]:
    from bog_agents_cli.widgets.chat_input import ChatInput

    command = action.command if action.command.startswith("/") else f"/{action.command}"
    ctx.app.post_message(ChatInput.Submitted(command, "command"))
    await ctx.pilot.pause()
    return {"command": command}


async def _do_shell(action: Shell, ctx: _ExecCtx) -> dict[str, Any]:
    from bog_agents_cli.widgets.chat_input import ChatInput

    command = action.command.lstrip("!")
    ctx.app.post_message(ChatInput.Submitted(f"!{command}", "shell"))
    await ctx.pilot.pause()
    return {"command": command}


async def _do_press(action: Press, ctx: _ExecCtx) -> dict[str, Any]:
    for key in action.keys:
        await ctx.pilot.press(key)
    await ctx.pilot.pause()
    return {"keys": list(action.keys)}


async def _do_wait_for_idle(action: WaitForIdle, ctx: _ExecCtx) -> dict[str, Any]:
    deadline = time.perf_counter() + action.timeout_seconds
    while time.perf_counter() < deadline:
        running = bool(getattr(ctx.app, "_agent_running", False))
        shell_running = bool(getattr(ctx.app, "_shell_running", False))
        approval = getattr(ctx.app, "_pending_approval_widget", None)
        ask_user = getattr(ctx.app, "_pending_ask_user_widget", None)
        if not running and not shell_running and approval is None and ask_user is None:
            return {"timeout_seconds": action.timeout_seconds}
        await ctx.pilot.pause()
        await asyncio.sleep(0.05)
    msg = f"agent did not become idle within {action.timeout_seconds}s"
    raise _ExpectationFailedError(msg, {"timeout_seconds": action.timeout_seconds})


async def _do_expect_transcript(
    action: ExpectTranscript, ctx: _ExecCtx
) -> dict[str, Any]:
    pattern = re.compile(action.pattern)
    deadline = time.perf_counter() + action.timeout_seconds
    target_type = action.message_type.lower() if action.message_type else None
    while True:
        store = getattr(ctx.app, "_message_store", None)
        haystack: list[tuple[str, str]] = []
        if store is not None:
            for msg in store.get_all_messages():
                if target_type and msg.type.value.lower() != target_type:
                    continue
                content = msg.content or ""
                if msg.tool_output:
                    content = f"{content}\n{msg.tool_output}"
                haystack.append((msg.type.value, content))
        for msg_type, content in haystack:
            match = pattern.search(content)
            if match:
                return {
                    "pattern": action.pattern,
                    "matched": match.group(0),
                    "message_type": msg_type,
                }
        if time.perf_counter() >= deadline:
            break
        await ctx.pilot.pause()
        await asyncio.sleep(0.05)
    msg = (
        f"transcript never matched /{action.pattern}/ within {action.timeout_seconds}s"
    )
    raise _ExpectationFailedError(
        msg,
        {
            "pattern": action.pattern,
            "transcript_lines": len(haystack),
        },
    )


async def _do_expect_modal(action: ExpectModal, ctx: _ExecCtx) -> dict[str, Any]:
    target = action.name.lower()
    deadline = time.perf_counter() + action.timeout_seconds
    while time.perf_counter() < deadline:
        for screen in list(getattr(ctx.app, "screen_stack", []) or []):
            cls_name = type(screen).__name__.lower()
            if target in cls_name or cls_name == target:
                return {"name": type(screen).__name__}
        await ctx.pilot.pause()
        await asyncio.sleep(0.05)
    visible = [type(s).__name__ for s in getattr(ctx.app, "screen_stack", []) or []]
    msg = f"modal {action.name!r} never appeared within {action.timeout_seconds}s"
    raise _ExpectationFailedError(msg, {"visible_screens": visible})


async def _do_select_option(action: SelectOption, ctx: _ExecCtx) -> dict[str, Any]:
    screen = ctx.app.screen
    if screen is None:
        msg = "no screen mounted"
        raise _ExpectationFailedError(msg)

    # Walk focusable widgets in DOM order.
    candidates = list(screen.query("*"))
    focusable = [w for w in candidates if getattr(w, "can_focus", False)]
    if not focusable:
        # Some modals expose options as ListItem/ListView entries that
        # accept Enter without being focusable directly — fall back to
        # ListView selection by key navigation.
        for child in candidates:
            if (
                type(child).__name__ in ("ListView", "OptionList")
                and action.index is not None
            ):
                child.index = action.index
                await ctx.pilot.press("enter")
                await ctx.pilot.pause()
                return {"index": action.index, "label": None}
        msg = "modal has no focusable options"
        raise _ExpectationFailedError(msg)

    chosen: Any = None
    if action.index is not None and 0 <= action.index < len(focusable):
        chosen = focusable[action.index]
    elif action.label:
        needle = action.label.lower()
        for widget in focusable:
            text = _widget_text(widget).lower()
            if needle in text:
                chosen = widget
                break
    if chosen is None:
        labels = [_widget_text(w)[:40] for w in focusable]
        msg = f"no option matched (label={action.label!r}, index={action.index})"
        raise _ExpectationFailedError(msg, {"labels": labels})

    chosen.focus()
    await ctx.pilot.pause()
    await ctx.pilot.press("enter")
    await ctx.pilot.pause()
    return {"label": _widget_text(chosen), "index": action.index}


async def _do_approval_respond(
    action: ApprovalRespond, ctx: _ExecCtx
) -> dict[str, Any]:
    deadline = time.perf_counter() + action.timeout_seconds
    if action.wait:
        while time.perf_counter() < deadline:
            widget = getattr(ctx.app, "_pending_approval_widget", None)
            if widget is not None:
                break
            await ctx.pilot.pause()
            await asyncio.sleep(0.05)
        else:
            msg = f"no approval prompt appeared within {action.timeout_seconds}s"
            raise _ExpectationFailedError(msg)

    choice = action.choice.lower()
    keymap = {
        "approve": "y",
        "yes": "y",
        "auto": "a",
        "deny": "n",
        "no": "n",
        "reject": "n",
    }
    key = keymap.get(choice)
    if key is None:
        msg = f"unknown approval choice {action.choice!r}"
        raise _ExpectationFailedError(msg)
    await ctx.pilot.press(key)
    await ctx.pilot.pause()
    return {"choice": choice}


def _do_snapshot(action: Snapshot, ctx: _ExecCtx) -> dict[str, Any]:
    stem_arg = Path(action.path)
    stem = stem_arg if stem_arg.is_absolute() else ctx.artifact_dir / stem_arg
    result = capture_snapshot(ctx.app, stem)
    detail: dict[str, Any] = {}
    if result.svg_path:
        detail["svg"] = str(result.svg_path)
    if result.txt_path:
        detail["txt"] = str(result.txt_path)
    if not result.ok:
        msg_parts = []
        if result.svg_error:
            msg_parts.append(f"svg: {result.svg_error}")
        if result.txt_error:
            msg_parts.append(f"txt: {result.txt_error}")
        raise _ExpectationFailedError("; ".join(msg_parts), detail=detail)
    return detail


async def _do_assert_widget(action: AssertWidget, ctx: _ExecCtx) -> dict[str, Any]:
    screen = ctx.app.screen
    if screen is None:
        msg = "no screen mounted"
        raise _ExpectationFailedError(msg)
    try:
        matches = list(screen.query(action.selector))
    except Exception as exc:
        msg = f"invalid selector {action.selector!r}: {exc}"
        raise _ExpectationFailedError(msg) from exc
    if not matches:
        msg = f"no widget matched selector {action.selector!r}"
        raise _ExpectationFailedError(msg)
    if action.text_matches is None:
        return {"selector": action.selector, "count": len(matches)}
    pattern = re.compile(action.text_matches)
    for widget in matches:
        text = _widget_text(widget)
        if pattern.search(text):
            return {
                "selector": action.selector,
                "matched": pattern.search(text).group(0),
                "count": len(matches),
            }
    msg = (
        f"selector {action.selector!r} matched {len(matches)} widget(s) but "
        f"none of them contained /{action.text_matches}/"
    )
    raise _ExpectationFailedError(msg)


def _do_set_env(action: SetEnv, _ctx: _ExecCtx) -> dict[str, Any]:
    for k, v in action.values.items():
        os.environ[k] = v
    return {"keys": sorted(action.values)}


async def _do_switch_model(action: SwitchModel, ctx: _ExecCtx) -> dict[str, Any]:
    from bog_agents_cli.widgets.chat_input import ChatInput

    command = f"/model {action.model}"
    ctx.app.post_message(ChatInput.Submitted(command, "command"))
    await ctx.pilot.pause()
    return {"model": action.model}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _emit(stream: TextIO, line: str) -> None:
    stream.write(line)
    stream.write("\n")
    try:
        stream.flush()
    except (OSError, ValueError):
        pass


def _action_name(action: Action) -> str:
    return type(action).__name__.lower()


def _widget_text(widget: Any) -> str:
    for attr in ("renderable", "_content", "text", "label"):
        value = getattr(widget, attr, None)
        if value:
            return str(value)
    try:
        return str(widget.render())
    except Exception:
        return ""


def _resolve_cwd(script: Script) -> Path:
    raw = script.session.cwd
    if not raw:
        return Path.cwd()
    candidate = Path(raw)
    if candidate.is_absolute():
        return candidate
    if script.source_path is not None:
        return (script.source_path.parent / candidate).resolve()
    return candidate.resolve()


def _default_artifact_dir(script: Script) -> Path:
    base = script.source_path.parent if script.source_path else Path.cwd()
    stamp = time.strftime("%Y%m%dT%H%M%S")
    candidate = base / ".drive-artifacts" / stamp
    try:
        candidate.mkdir(parents=True, exist_ok=True)
        return candidate
    except OSError:
        # Read-only working tree (e.g. running on a CD-ROM in tests) —
        # fall back to a tmp dir so the script can still record snapshots.
        return Path(tempfile.mkdtemp(prefix="drive-artifacts-"))


def _apply_session_env(session: SessionConfig) -> None:
    for k, v in session.env.items():
        os.environ[k] = v


def _build_resolved_vars(script: Script, overrides: dict[str, str]) -> dict[str, str]:
    """Combine vars_spec defaults + session.vars + CLI overrides.

    Required vars without a default OR override raise immediately so
    the script doesn't run with empty placeholder strings.
    """
    resolved: dict[str, str] = {}
    for name, spec in script.vars_spec.items():
        default = spec.get("default")
        if default is not None:
            resolved[name] = str(default)
    resolved.update(script.session.vars)
    resolved.update(overrides)
    missing = [
        name
        for name, spec in script.vars_spec.items()
        if spec.get("required", False) and name not in resolved
    ]
    if missing:
        msg = f"required vars without a value: {sorted(missing)}"
        raise ValueError(msg)
    return resolved


_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _sub(text: str, mapping: dict[str, str]) -> str:
    def repl(match: re.Match[str]) -> str:
        name = match.group(1)
        return mapping.get(name, match.group(0))

    return _VAR_RE.sub(repl, text)


def _substitute_action(action: Action, mapping: dict[str, str]) -> Action:
    """Return a copy of *action* with ``${var}`` placeholders resolved."""
    if not mapping:
        return action
    if isinstance(action, Type):
        return Type(text=_sub(action.text, mapping), slow=action.slow)
    if isinstance(action, Submit):
        return Submit(
            value=_sub(action.value, mapping) if action.value else None,
            mode=action.mode,
        )
    if isinstance(action, Slash):
        return Slash(command=_sub(action.command, mapping))
    if isinstance(action, Shell):
        return Shell(command=_sub(action.command, mapping))
    if isinstance(action, ExpectTranscript):
        return ExpectTranscript(
            pattern=_sub(action.pattern, mapping),
            timeout_seconds=action.timeout_seconds,
            message_type=action.message_type,
        )
    if isinstance(action, ExpectModal):
        return ExpectModal(
            name=_sub(action.name, mapping),
            timeout_seconds=action.timeout_seconds,
        )
    if isinstance(action, SelectOption):
        return SelectOption(
            label=_sub(action.label, mapping) if action.label else None,
            index=action.index,
        )
    if isinstance(action, AssertWidget):
        return AssertWidget(
            selector=_sub(action.selector, mapping),
            text_matches=(
                _sub(action.text_matches, mapping) if action.text_matches else None
            ),
        )
    if isinstance(action, Snapshot):
        return Snapshot(path=_sub(action.path, mapping))
    if isinstance(action, SetEnv):
        return SetEnv(values={k: _sub(v, mapping) for k, v in action.values.items()})
    if isinstance(action, SwitchModel):
        return SwitchModel(model=_sub(action.model, mapping))
    return action


def _dump_transcript(app: BogAgentsApp) -> list[dict[str, Any]]:
    store = getattr(app, "_message_store", None)
    if store is None:
        return []
    out: list[dict[str, Any]] = []
    for msg in store.get_all_messages():
        record: dict[str, Any] = {"type": msg.type.value, "content": msg.content}
        if msg.tool_name:
            record["tool_name"] = msg.tool_name
        if msg.tool_args:
            record["tool_args"] = msg.tool_args
        if msg.tool_output:
            record["tool_output"] = msg.tool_output
        out.append(record)
    return out
