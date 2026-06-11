"""`/whisper` — passive observation mode.

Whisper is the silent twin of the interactive agent: it watches what
the user does (file edits, git activity, shell history) for a bounded
window, then synthesises a short observation report — "I noticed
you're refactoring auth; here are three things I spotted."

It is intentionally non-interactive while running. The user keeps
working in their editor; no prompts, no tool calls, no approvals.
When the timer expires (or the user calls ``/whisper stop``) the
collected events are flattened into a prompt and a single LLM call
produces the report.

Design notes:

* Events are stored in memory only — the lookback ends when the
  session does, so privacy is bounded.
* The watcher runs as an ``asyncio.Task`` inside the running app's
  event loop. No subprocess, no daemon. ``/whisper status`` peeks at
  the buffer; ``/whisper stop`` cancels the task and emits the report.
* When the user runs ``/whisper start`` while a session is already
  active, the previous session is replaced (with its events lost).
* The synthesis prompt is intentionally conservative — observations
  about what the user *seemingly intends*, not advice they didn't ask
  for.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from bog_agents_cli.feature_helpers import (
    _git,
    collect_git_context,
    invoke_model,
    resolve_active_model_spec,
    write_artifact,
)

logger = logging.getLogger(__name__)


_DEFAULT_DURATION_MINUTES = 30
_MAX_DURATION_MINUTES = 240
_DEFAULT_POLL_SECONDS = 8.0
_MAX_EVENTS = 200


WHISPER_SYSTEM_PROMPT = """\
You are an observant teammate who has been watching a developer work
for the last {duration} minutes. Synthesise what you saw into a SHORT
report — at most 250 words.

Use this structure:

## What I think you're doing
One paragraph summarising the apparent intent. If the events don't
support a clear conclusion, say "the activity is too varied to
generalise" and stop.

## Three things I noticed
Numbered list. Each item is:
1. ONE concrete observation tied to specific files or commits.
2. Why it caught your eye — is it surprising, risky, or just notable?

## A question I would ask if I were on your team
ONE question only. Specific. About the work, not their feelings.

Hard rules:
- NEVER give advice the user didn't ask for. You are observing,
  not coaching.
- NEVER invent file names, commit messages, or events not in the
  observation log.
- If the observation log is sparse, produce a sparse report. Don't
  pad. Two findings is fine; one is fine.
"""


# --------------------------------------------------------------------------- #
# In-memory event model                                                       #
# --------------------------------------------------------------------------- #


@dataclass
class WhisperEvent:
    """One observed event."""

    kind: str
    """One of ``edit``, ``new``, ``delete``, ``commit``, ``branch-change``."""

    detail: str
    timestamp: float = field(default_factory=time.time)


@dataclass
class WhisperSession:
    """In-memory state for one whisper run."""

    started_at: float
    duration_seconds: float
    cwd: Path
    task: asyncio.Task[None] | None = None
    events: list[WhisperEvent] = field(default_factory=list)
    last_branch: str = ""
    last_head: str = ""
    initial_modified: set[str] = field(default_factory=set)
    initial_untracked: set[str] = field(default_factory=set)
    last_modified: set[str] = field(default_factory=set)
    last_untracked: set[str] = field(default_factory=set)
    last_report: str = ""

    @property
    def is_running(self) -> bool:
        """True when the watcher task is alive."""
        return self.task is not None and not self.task.done()

    @property
    def remaining_seconds(self) -> float:
        """Seconds left in the observation window (0 when not running)."""
        if not self.is_running:
            return 0.0
        ends_at = self.started_at + self.duration_seconds
        return max(0.0, ends_at - time.time())

    def append(self, kind: str, detail: str) -> None:
        """Record one event, evicting the oldest when the buffer is full."""
        if len(self.events) >= _MAX_EVENTS:
            # Drop the oldest to keep memory bounded.
            self.events.pop(0)
        self.events.append(WhisperEvent(kind=kind, detail=detail))


# --------------------------------------------------------------------------- #
# Event collection                                                            #
# --------------------------------------------------------------------------- #


def _snapshot(cwd: Path) -> tuple[set[str], set[str], str, str]:
    """Return (modified, untracked, branch, head_sha) for the cwd."""
    git = collect_git_context(cwd)
    return (
        set(git.modified_files),
        set(git.untracked_files),
        git.branch,
        git.head_sha,
    )


def _diff_snapshots(
    session: WhisperSession, modified: set[str], untracked: set[str]
) -> None:
    """Translate snapshot deltas into session events."""
    became_modified = modified - session.last_modified
    no_longer_modified = session.last_modified - modified
    became_untracked = untracked - session.last_untracked
    no_longer_untracked = session.last_untracked - untracked

    for path in sorted(became_modified):
        session.append("edit", path)
    for path in sorted(no_longer_modified):
        # Either reverted or committed — git log will reveal which.
        session.append("settled", path)
    for path in sorted(became_untracked):
        session.append("new", path)
    for path in sorted(no_longer_untracked):
        # The file was either deleted or added to git.
        session.append("staged-or-deleted", path)


async def _watch_loop(
    session: WhisperSession, *, poll_seconds: float = _DEFAULT_POLL_SECONDS
) -> None:
    """The background watcher coroutine. Cancels cleanly on session stop."""
    try:
        while True:
            modified, untracked, branch, head = _snapshot(session.cwd)

            if session.last_branch and branch and branch != session.last_branch:
                session.append("branch-change", f"{session.last_branch} → {branch}")
            if branch:
                session.last_branch = branch

            if session.last_head and head and head != session.last_head:
                # A new commit landed during the window. Capture its subject.
                subject = _git(["log", "-1", "--format=%h %s", head], str(session.cwd))
                session.append("commit", subject or head)
            if head:
                session.last_head = head

            _diff_snapshots(session, modified, untracked)
            session.last_modified = modified
            session.last_untracked = untracked

            # Stop when the duration is up.
            if time.time() - session.started_at >= session.duration_seconds:
                break
            try:
                await asyncio.sleep(poll_seconds)
            except asyncio.CancelledError:
                return
    except asyncio.CancelledError:
        return
    except Exception:
        logger.exception("whisper watch loop crashed; ending session early")


# --------------------------------------------------------------------------- #
# Report synthesis                                                            #
# --------------------------------------------------------------------------- #


def render_events_for_prompt(
    session: WhisperSession, *, duration_minutes: float
) -> str:
    """Build a compact transcript of the observation window."""
    lines = [
        f"Observation window: {duration_minutes:.0f} minutes",
        f"Working dir: {session.cwd}",
        f"Initial branch: {session.last_branch or '(none)'}",
        "",
        "Events:",
    ]
    if not session.events:
        lines.append("(no observable activity)")
        return "\n".join(lines)
    for ev in session.events:
        ts = time.strftime("%H:%M:%S", time.localtime(ev.timestamp))
        lines.append(f"[{ts}] {ev.kind}: {ev.detail}")
    return "\n".join(lines)


async def synthesise_report(
    app: object, session: WhisperSession, *, duration_minutes: float
) -> str:
    """Call the model with the rendered events; return the report markdown.

    Raises:
        RuntimeError: When no active model spec can be resolved.
    """
    from bog_agents_cli.config import create_model_with_fallback

    spec = resolve_active_model_spec(app)
    if not spec:
        msg = "no active model — run /model first or set a default"
        raise RuntimeError(msg)
    profile = getattr(app, "_profile_override", None)
    model_result = create_model_with_fallback(spec, profile_overrides=profile)
    system = WHISPER_SYSTEM_PROMPT.format(duration=int(duration_minutes))
    body = render_events_for_prompt(session, duration_minutes=duration_minutes)
    return await invoke_model(model_result.model, system, body, timeout_seconds=90.0)


# --------------------------------------------------------------------------- #
# App handler glue                                                            #
# --------------------------------------------------------------------------- #


_SESSION_ATTR = "_whisper_session"


def _get_session(app: object) -> WhisperSession | None:
    return getattr(app, _SESSION_ATTR, None)


def _set_session(app: object, session: WhisperSession | None) -> None:
    try:
        setattr(app, _SESSION_ATTR, session)
    except Exception:
        logger.warning("Could not attach whisper session to app")


def _start_whisper(app: object, duration_minutes: float) -> WhisperSession:
    """Start a fresh whisper watch.

    Synchronous despite touching ``asyncio`` — ``create_task`` is
    synchronous (it just schedules the coroutine on the running loop).
    Callers in async contexts simply call this without ``await``.
    """
    cwd = Path(getattr(app, "_cwd", Path.cwd()))
    duration_seconds = duration_minutes * 60
    session = WhisperSession(
        started_at=time.time(),
        duration_seconds=duration_seconds,
        cwd=cwd,
    )
    # Seed the snapshot so the diff for the first poll is meaningful.
    modified, untracked, branch, head = _snapshot(cwd)
    session.initial_modified = set(modified)
    session.initial_untracked = set(untracked)
    session.last_modified = set(modified)
    session.last_untracked = set(untracked)
    session.last_branch = branch
    session.last_head = head

    session.task = asyncio.create_task(_watch_loop(session))
    _set_session(app, session)
    return session


async def _stop_whisper(app: object) -> WhisperSession | None:
    """Stop the current watch and return the session (without synthesising)."""
    session = _get_session(app)
    if session is None or session.task is None:
        return session
    if not session.task.done():
        session.task.cancel()
        try:
            await session.task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.warning("whisper task raised on cancel", exc_info=True)
    return session


async def handle_whisper_subcommand(app: object, raw_arg: str) -> None:
    """Dispatch ``/whisper <sub>`` subcommands."""
    from bog_agents_cli.widgets.messages import AppMessage, ErrorMessage

    arg = raw_arg.strip()
    head, _, rest = arg.partition(" ")
    head = head.lower()
    rest = rest.strip()

    if not head or head == "status":
        session = _get_session(app)
        if session is None or not session.is_running:
            await app._mount_message(  # type: ignore[attr-defined]
                AppMessage(
                    "[dim]No whisper session running.[/dim]\n"
                    "Start one with [bold]/whisper start[/bold]."
                )
            )
            return
        remaining = session.remaining_seconds / 60.0
        await app._mount_message(  # type: ignore[attr-defined]
            AppMessage(
                f"[bold]Whisper running[/bold]\n"
                f"  cwd: [cyan]{session.cwd}[/cyan]\n"
                f"  events captured: {len(session.events)}\n"
                f"  remaining: {remaining:.1f} min"
            )
        )
        return

    if head == "start":
        # Parse optional duration in minutes.
        try:
            duration = float(rest) if rest else float(_DEFAULT_DURATION_MINUTES)
        except ValueError:
            await app._mount_message(  # type: ignore[attr-defined]
                ErrorMessage(
                    "Usage: /whisper start [minutes]  (e.g. /whisper start 45)"
                )
            )
            return
        duration = max(1.0, min(float(_MAX_DURATION_MINUTES), duration))

        # Replace any existing session.
        existing = _get_session(app)
        if existing and existing.is_running:
            await _stop_whisper(app)

        session = _start_whisper(app, duration)
        await app._mount_message(  # type: ignore[attr-defined]
            AppMessage(
                f"[bold]Whisper started[/bold] — will observe for "
                f"{duration:.0f} minutes.\n"
                f"Run [bold]/whisper stop[/bold] to end early and get the report,\n"
                f"or [bold]/whisper status[/bold] to peek at the buffer."
            )
        )
        return

    if head == "stop":
        session = await _stop_whisper(app)
        if session is None:
            await app._mount_message(  # type: ignore[attr-defined]
                AppMessage("[dim]No whisper session to stop.[/dim]")
            )
            return
        if not session.events:
            await app._mount_message(  # type: ignore[attr-defined]
                AppMessage(
                    "[bold]Whisper stopped.[/bold] No events observed — "
                    "the working tree was quiet."
                )
            )
            return
        duration_minutes = (time.time() - session.started_at) / 60.0
        await app._set_spinner("Synthesising whisper report")  # type: ignore[attr-defined]
        try:
            report = await synthesise_report(
                app, session, duration_minutes=duration_minutes
            )
        except Exception as exc:
            logger.exception("/whisper synthesis failed")
            await app._set_spinner("")  # type: ignore[attr-defined]
            await app._mount_message(  # type: ignore[attr-defined]
                ErrorMessage(f"/whisper stop: synthesis failed: {exc}")
            )
            return
        await app._set_spinner("")  # type: ignore[attr-defined]
        session.last_report = report

        # Persist alongside dreams so the user has one place to look.
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
        try:
            path = write_artifact(
                "whispers",
                stamp,
                _wrap_with_frontmatter(report, duration_minutes, len(session.events)),
            )
        except OSError:
            path = None  # display-only fallback
            logger.warning("Could not write whisper artifact", exc_info=True)

        location = f"\n[dim]Saved to {path}[/dim]" if path else ""
        await app._mount_message(  # type: ignore[attr-defined]
            AppMessage(
                f"[bold]Whisper report[/bold] "
                f"[dim]({len(session.events)} events, "
                f"{duration_minutes:.1f} min)[/dim]\n\n"
                f"{report}{location}"
            )
        )
        return

    if head == "report":
        session = _get_session(app)
        if session is None or not session.last_report:
            await app._mount_message(  # type: ignore[attr-defined]
                AppMessage("[dim]No previous whisper report available.[/dim]")
            )
            return
        await app._mount_message(AppMessage(session.last_report))  # type: ignore[attr-defined]
        return

    await app._mount_message(  # type: ignore[attr-defined]
        AppMessage(
            "Usage:\n"
            "  /whisper start [minutes]   Begin watching (default 30)\n"
            "  /whisper stop              Stop and emit the report\n"
            "  /whisper status            Show running state + event count\n"
            "  /whisper report            Re-emit the last report"
        )
    )


def _wrap_with_frontmatter(body: str, duration_minutes: float, event_count: int) -> str:
    lines = [
        "---",
        f"duration_minutes: {duration_minutes:.1f}",
        f"events: {event_count}",
        f"generated: {time.strftime('%Y-%m-%dT%H:%M:%S')}",
        "kind: whisper",
        "---",
        "",
        body,
    ]
    return "\n".join(lines)


__all__ = [
    "WhisperEvent",
    "WhisperSession",
    "handle_whisper_subcommand",
    "render_events_for_prompt",
    "synthesise_report",
]
