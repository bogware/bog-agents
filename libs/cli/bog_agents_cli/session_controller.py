"""Sessions, the cross-process queue and detach / attach (ROADMAP #56).

The SDK owns the data (`bog_agents.session_registry`, `bog_agents.mailbox_store`);
this module owns the CLI's use of it: where the registry and mailbox live under
`~/.bog-agents`, how the TUI registers itself and heartbeats, how
`bog-agents queue` addresses a session and waits for its answer, and what
`/detach` and `bog-agents attach` do with the LangGraph server process.
Everything that touches the App takes it as a duck-typed argument so the
logic tests without Textual.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bog_agents.session_registry import (
    SessionRecord,
    find_session,
    format_sessions,
    heartbeat,
    list_sessions,
    prune_stale,
    register,
    unregister,
)

if TYPE_CHECKING:
    from bog_agents.mailbox_store import MailboxStore
    from bog_agents.teams import Message

logger = logging.getLogger(__name__)

QUEUE_SENDER_PREFIX = "queue:"
POLL_SECONDS = 2.0
"""How often the TUI heartbeats and looks for queued prompts."""


def registry_dir() -> Path:
    """`~/.bog-agents/sessions`."""
    from bog_agents_cli.config import settings

    return Path(settings.user_agents_dir) / "sessions"


def mailbox_path() -> Path:
    """`~/.bog-agents/mailbox.db` — one store, addressed by session id."""
    from bog_agents_cli.config import settings

    return Path(settings.user_agents_dir) / "mailbox.db"


def open_mailbox(path: Path | None = None) -> MailboxStore:
    """The shared mailbox store."""
    from bog_agents.mailbox_store import MailboxStore

    return MailboxStore(path or mailbox_path())


# ---------------------------------------------------------------------------
# Launch configuration (set by main.py before the TUI starts)
# ---------------------------------------------------------------------------


@dataclass
class LaunchConfig:
    """What main.py decided before the App existed: a session name and/or a record to re-attach."""

    name: str = ""
    attach: SessionRecord | None = None


_LAUNCH = LaunchConfig()


def configure_launch(
    *, name: str | None = None, attach: SessionRecord | None = None
) -> None:
    """Record the `--name` / `attach` decision for `start_session_queue`."""
    global _LAUNCH  # noqa: PLW0603 - launch-time handoff from main.py to the App
    _LAUNCH = LaunchConfig(name=name or "", attach=attach)


def launch_config() -> LaunchConfig:
    """The current launch configuration."""
    return _LAUNCH


# ---------------------------------------------------------------------------
# Command side: sessions / queue / attach
# ---------------------------------------------------------------------------


def sessions_report(*, include_stale: bool = False, prune: bool = False) -> str:
    """Text for `bog-agents sessions`."""
    directory = registry_dir()
    removed = prune_stale(registry_dir=directory) if prune else 0
    text = format_sessions(
        list_sessions(include_stale=include_stale, registry_dir=directory)
    )
    if prune:
        text += f"\nPruned {removed} stale record(s)."
    return text


def enqueue_prompt(
    session: str, prompt: str, *, wait: float | None = None
) -> tuple[int, str]:
    """`bog-agents queue`: drop `prompt` into the session's mailbox; optionally wait for its answer.

    Returns:
        `(exit_code, text)` — 0 queued/answered, 1 no such session, 2 timed out waiting.
    """
    if not prompt.strip():
        return 1, "Nothing to queue: the prompt is empty."
    try:
        record = find_session(session, registry_dir=registry_dir())
    except LookupError as exc:
        return 1, str(exc)
    store = open_mailbox(Path(record.mailbox_path) if record.mailbox_path else None)
    sender = f"{QUEUE_SENDER_PREFIX}{uuid.uuid4().hex[:8]}"
    store.send(sender, record.session_id, prompt)
    if wait is None:
        return (
            0,
            f"Queued for {record.label} ({record.state}); it runs on that session's next idle tick.",
        )
    replies = store.wait(sender, timeout=wait)
    if not replies:
        return (
            2,
            f"Queued for {record.label}, but no answer within {wait:.0f}s (the session may be busy); the prompt stays queued.",
        )
    return 0, replies[-1].body


def attach_target(session: str) -> SessionRecord:
    """The detached session `bog-agents attach <session>` should reconnect to.

    Raises:
        LookupError: When the session is unknown, still attached, or has no server URL.
    """
    record = find_session(session, registry_dir=registry_dir())
    if record.state != "detached" or not record.server_url:
        msg = f"session {record.label!r} is {record.state}, not detached; only a session left with /detach can be attached"
        raise LookupError(msg)
    return record


# ---------------------------------------------------------------------------
# App side: registration, heartbeat, queue drain, replies, detach
# ---------------------------------------------------------------------------


class SessionQueue:
    """The App's half of the registry + mailbox: one per session."""

    def __init__(
        self, record: SessionRecord, store: MailboxStore | None = None
    ) -> None:
        """Bind to a registry record (already written) and a mailbox store."""
        self.record = record
        self._store = store
        self._reply_to: list[str] = []

    @property
    def store(self) -> MailboxStore:
        """The mailbox store (opened lazily)."""
        if self._store is None:
            self._store = open_mailbox(
                Path(self.record.mailbox_path) if self.record.mailbox_path else None
            )
        return self._store

    @property
    def waiting(self) -> int:
        """Senders still owed an answer."""
        return len(self._reply_to)

    def heartbeat(self, *, busy: bool, thread_id: str | None = None) -> None:
        """Refresh the record; never raises."""
        try:
            heartbeat(
                self.record.session_id,
                state="busy" if busy else "idle",
                thread_id=thread_id,
                registry_dir=registry_dir(),
            )
        except Exception:
            logger.debug("Session heartbeat failed", exc_info=True)

    def pull(self) -> list[Message]:
        """Queued prompts addressed to this session (each returned once)."""
        try:
            return self.store.drain(self.record.session_id)
        except Exception:
            logger.debug("Mailbox drain failed", exc_info=True)
            return []

    def note_dispatched(self, sender: str) -> None:
        """Remember who to answer once the prompt's turn ends."""
        if sender.startswith(QUEUE_SENDER_PREFIX):
            self._reply_to.append(sender)

    def answer(self, text: str) -> int:
        """Send `text` to every sender waiting on this session; returns how many."""
        senders, self._reply_to = self._reply_to, []
        for sender in senders:
            try:
                self.store.send(self.record.session_id, sender, text)
            except Exception:
                logger.debug("Could not answer %s", sender, exc_info=True)
        return len(senders)

    def close(self, *, detached: bool = False) -> None:
        """Drop the registry record — unless the server lives on detached."""
        if detached:
            return
        try:
            unregister(self.record.session_id, registry_dir=registry_dir())
        except Exception:
            logger.debug("Could not remove the session record", exc_info=True)


def start_session_queue(app: Any) -> SessionQueue:  # noqa: ANN401 - duck-typed App
    """Register this TUI (or re-adopt the record it attaches to) and return its `SessionQueue`."""
    launch = launch_config()
    if launch.attach is not None:
        record = launch.attach
        try:
            heartbeat(record.session_id, state="idle", registry_dir=registry_dir())
        except Exception:
            logger.debug("Could not refresh the attached session record", exc_info=True)
        return SessionQueue(record)
    record = SessionRecord(
        name=launch.name,
        kind="tui",
        cwd=str(getattr(app, "_cwd", "") or ""),
        model=str(getattr(app, "_model_override", None) or ""),
        state="idle",
        thread_id=str(getattr(app, "_lc_thread_id", None) or ""),
        mailbox_path=str(mailbox_path()),
    )
    return SessionQueue(register(record, registry_dir=registry_dir()))


async def poll_session_queue(app: Any) -> None:  # noqa: ANN401 - duck-typed App
    """One tick: heartbeat, then (when idle) move queued prompts into the App's input queue."""
    queue: SessionQueue | None = getattr(app, "_session_queue", None)
    if queue is None:
        return
    busy = bool(app._turns.busy) or bool(getattr(app, "_connecting", False))
    queue.heartbeat(busy=busy, thread_id=getattr(app, "_lc_thread_id", None))
    if busy or app._pending_messages or getattr(app, "_exit", False):
        return
    pulled = queue.pull()
    if not pulled:
        return
    from bog_agents_cli.app import QueuedMessage

    for msg in pulled:
        app._pending_messages.append(QueuedMessage(text=msg.body, mode="normal"))
        queue.note_dispatched(msg.sender)
    await app._process_next_from_queue()


def turn_finished(app: Any) -> int:  # noqa: ANN401 - duck-typed App
    """End of a turn: answer the queued senders with the last assistant text; returns how many."""
    queue: SessionQueue | None = getattr(app, "_session_queue", None)
    if queue is None or not queue.waiting:
        return 0
    text = ""
    try:
        text = app._get_last_assistant_text() or ""
    except Exception:
        logger.debug("Could not read the last assistant text", exc_info=True)
    return queue.answer(text or "(the turn finished without an assistant message)")


def detach(app: Any) -> str:  # noqa: ANN401 - duck-typed App
    """`/detach`: let the agent server outlive the TUI and record how to come back."""
    proc = getattr(app, "_server_proc", None)
    if proc is None or not hasattr(proc, "detach"):
        return "Nothing to detach: this session has no agent server of its own (still connecting, or an in-process agent)."
    queue: SessionQueue | None = getattr(app, "_session_queue", None)
    if queue is None:
        return "Nothing to detach: this session is not registered."
    try:
        pid, url = proc.detach()
    except Exception as exc:
        return f"Could not detach the agent server: {exc}"
    heartbeat(
        queue.record.session_id,
        state="detached",
        thread_id=getattr(app, "_lc_thread_id", None),
        server_url=url,
        server_pid=pid,
        registry_dir=registry_dir(),
    )
    app._detached = True
    label = queue.record.label
    return (
        f"Detached. The agent server keeps running (pid {pid}) with this thread; come back with "
        f'`bog-agents attach {label}`, or hand it work meanwhile with `bog-agents queue --session {label} "<prompt>"`. '
        "Quitting an attached session stops the server."
    )


__all__ = [
    "POLL_SECONDS",
    "QUEUE_SENDER_PREFIX",
    "LaunchConfig",
    "SessionQueue",
    "attach_target",
    "configure_launch",
    "detach",
    "enqueue_prompt",
    "launch_config",
    "mailbox_path",
    "open_mailbox",
    "poll_session_queue",
    "registry_dir",
    "sessions_report",
    "start_session_queue",
    "turn_finished",
]
