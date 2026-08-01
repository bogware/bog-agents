"""Sidechain transcripts — out-of-band notes keyed by agent id.

A *sidechain* is a lightweight append-only transcript that is deliberately
kept OUT of the main conversation thread. Each record is keyed by an agent
id (an interactive thread id, a background task id, or a subagent run id)
and persisted as JSONL under ``<config_dir>/sidechains/<agent_id>.jsonl`` so
it survives restarts and is trivial to diff or replay.

The feed is the substrate for two features:

* ``/btw <note>`` — drop an out-of-band note onto the current agent's
  sidechain instead of polluting the main thread.
* Background agents — each task appends ``submission`` / ``result`` /
  ``error`` records tagged with the parent thread id, so a follow-up
  invocation can continue where the sidechain left off (agentId
  continuation via `build_continuation_prompt`).
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

# Agent ids are typically uuids or `bg-XXX` slugs, but sanitize anyway so a
# crafted id can never escape the sidechains directory or collide with `..`.
_SAFE_ID = re.compile(r"[^A-Za-z0-9._-]+")

_SIDECHAIN_DIR = "sidechains"
_MAX_CONTINUATION_TRANSCRIPT_CHARS = 24_000


class _BtwApp(Protocol):
    """Minimal `BogAgentsApp` surface the `/btw` handler needs."""

    def _current_thread_id(self) -> str | None: ...

    def _mount_message(self, widget: object) -> Awaitable[None]: ...


def _safe_agent_id(agent_id: str) -> str:
    """Sanitize an agent id into a safe filesystem stem."""
    return _SAFE_ID.sub("-", agent_id).strip(".-") or "unknown"


@dataclass(frozen=True, slots=True)
class SidechainRecord:
    """One immutable entry in a sidechain transcript."""

    agent_id: str
    kind: str
    content: str
    ts: float
    parent_thread_id: str | None = None

    @property
    def is_note(self) -> bool:
        """True when the record is a user-authored `/btw` note."""
        return self.kind == "note"


class SidechainStore:
    """Append-only JSONL transcript store, safe for concurrent appends.

    Args:
        config_dir: Base config directory (e.g. ``settings.user_agents_dir``).
            Sidechain files live under ``<config_dir>/sidechains/``.
        now: Injectable clock for deterministic tests.
    """

    def __init__(
        self, config_dir: str | Path, *, now: Callable[[], float] = time.time
    ) -> None:
        self._root = Path(config_dir) / _SIDECHAIN_DIR
        self._lock = threading.Lock()
        self._now = now

    @property
    def root(self) -> Path:
        """The directory holding all sidechain transcript files."""
        return self._root

    def record_path(self, agent_id: str) -> Path:
        """The JSONL file for a given agent id."""
        return self._root / f"{_safe_agent_id(agent_id)}.jsonl"

    def record(
        self,
        agent_id: str,
        kind: str,
        content: str,
        *,
        parent_thread_id: str | None = None,
    ) -> SidechainRecord:
        """Append a record to the agent's sidechain transcript.

        Args:
            agent_id: The agent (thread/task/subagent) id to key the record on.
            kind: Record kind (`note`, `submission`, `result`, `error`,
                `cancelled`).
            content: Free-form text payload.
            parent_thread_id: Optional parent interactive thread id — lets a
                follow-up replay link background results back to the thread
                that spawned them.

        Returns:
            The immutable record that was persisted.
        """
        record = SidechainRecord(
            agent_id=agent_id,
            kind=kind,
            content=content,
            ts=self._now(),
            parent_thread_id=parent_thread_id,
        )
        path = self.record_path(agent_id)
        with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(asdict(record), sort_keys=True) + "\n")
        return record

    def load(self, agent_id: str) -> list[SidechainRecord]:
        """Load all records for an agent, oldest first.

        Returns:
            A list of `SidechainRecord` in append order. Corrupt lines are
            skipped with a debug log rather than failing the whole feed.
        """
        path = self.record_path(agent_id)
        if not path.exists():
            return []
        with self._lock, path.open(encoding="utf-8") as fh:
            records: list[SidechainRecord] = []
            for line_number, line in enumerate(fh, start=1):
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    records.append(self._from_json(stripped))
                except (ValueError, TypeError):
                    logger.debug(
                        "Skipping corrupt sidechain record %s:%d",
                        path,
                        line_number,
                        exc_info=True,
                    )
            return records

    @staticmethod
    def _from_json(line: str) -> SidechainRecord:
        """Parse one JSONL line into a `SidechainRecord`."""
        data = json.loads(line)
        return SidechainRecord(
            agent_id=str(data.get("agent_id", "")),
            kind=str(data.get("kind", "")),
            content=str(data.get("content", "")),
            ts=float(data.get("ts", 0.0)),
            parent_thread_id=(
                str(data["parent_thread_id"])
                if data.get("parent_thread_id") is not None
                else None
            ),
        )

    def agent_ids(self) -> list[str]:
        """All agent ids with a sidechain transcript, sorted."""
        if not self._root.exists():
            return []
        return sorted(path.stem for path in self._root.glob("*.jsonl"))

    def transcript_text(self, agent_id: str, *, limit: int | None = None) -> str:
        """Render an agent's transcript as plain text for prompt context."""
        records = self.load(agent_id)
        if limit is not None:
            records = records[-limit:]
        return "\n".join(f"[{r.kind}] {r.content}" for r in records)

    def continuation_prompt(self, agent_id: str, instruction: str) -> str:
        """Compose a continuation prompt from this store's transcript.

        The agentId-continuation mechanism: a follow-up invocation of the same
        agent id gets its out-of-band transcript as context, then the user's
        instruction, without touching the main conversation thread.

        Args:
            agent_id: The agent id whose transcript should be continued.
            instruction: The follow-up instruction for the agent.

        Returns:
            The composed prompt, or `instruction` unchanged when the sidechain
            has no records yet.
        """
        if not self.load(agent_id):
            return instruction
        transcript = self.transcript_text(agent_id)
        if len(transcript) > _MAX_CONTINUATION_TRANSCRIPT_CHARS:
            transcript = (
                transcript[-_MAX_CONTINUATION_TRANSCRIPT_CHARS:] + "\n…[truncated]"
            )
        return (
            f"Continue the recorded sidechain for agent '{agent_id}'.\n\n"
            f"<sidechain transcript>\n{transcript}\n</sidechain transcript>\n\n"
            f"Instruction: {instruction}"
        )


def build_continuation_prompt(
    agent_id: str, config_dir: str | Path, instruction: str
) -> str:
    """Build a prompt that continues a recorded sidechain.

    Used for agentId continuation: a follow-up invocation of the same agent
    id gets the out-of-band transcript as context, then the user's
    instruction, without touching the main conversation thread.

    Args:
        agent_id: The agent id whose transcript should be continued.
        config_dir: Base config directory (same one the store was created
            with).
        instruction: The follow-up instruction for the agent.

    Returns:
        The composed prompt, or ``instruction`` unchanged when the sidechain
        has no records yet.
    """
    return SidechainStore(config_dir).continuation_prompt(agent_id, instruction)


async def handle_btw_command(app: _BtwApp, command: str) -> None:
    """Implement the `/btw <note>` slash command.

    Drops the note onto the current agent's sidechain transcript (kept out of
    the main thread), mounting the raw command plus a confirmation message.

    Args:
        app: The `BogAgentsApp` instance — provides `_mount_message`,
            `_current_thread_id`, and the live `settings`.
        command: The raw slash-command string (including the leading `/`).
    """
    from bog_agents_cli.widgets.messages import AppMessage, UserMessage

    note = command[4:].strip()
    await app._mount_message(UserMessage(command))
    if not note:
        await app._mount_message(
            AppMessage(
                "Usage: /btw <note> — save an out-of-band note to the current "
                "agent's sidechain transcript (kept out of the main thread)."
            )
        )
        return

    from bog_agents_cli.config import settings

    agent_id = app._current_thread_id() or "interactive"
    store = SidechainStore(settings.user_agents_dir)
    store.record(agent_id, "note", note)
    path = store.record_path(agent_id)
    await app._mount_message(
        AppMessage(f"Note saved to sidechain '{agent_id}' ({path}).")
    )
