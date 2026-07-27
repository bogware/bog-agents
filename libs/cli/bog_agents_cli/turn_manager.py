"""Single-flight coordinator for the CLI's agent/shell turn lifecycle.

`BogAgentsApp` used to carry `_agent_running`, `_agent_worker`, and
`_shell_running` as bare attributes mutated from ~8 methods (user submit, queue
drain, `_send_prompt_to_agent`, butcher, compact, shell, cleanup). Ordering
mistakes between those writes were the root of two v4 findings:

* **CLI-CORE-1** — a cleanup `finally` re-asserted the flags *after* the queue
  drain had already started the next turn, clobbering it.
* **CLI-CORE-4** — a scheduled pipeline/file-watch prompt started a second
  concurrent turn because nothing checked the busy flag on that path.

Routing every begin/end and the single "is a turn in flight" question through
one object makes those invariants a property of this class instead of scattered
discipline. The app keeps `_agent_running` / `_agent_worker` / `_shell_running`
as delegating properties, so the ~25 read sites are untouched and any incidental
write still flows through here.

All methods run on Textual's single event loop, so the paired writes in
`begin_agent` / `end_agent` are atomic with respect to other turn transitions
without an explicit lock — the value is the *single choke point*, not mutual
exclusion against threads.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from textual.worker import Worker

__all__ = ["TurnManager"]


class TurnManager:
    """Owns the agent/shell run flags and the agent worker handle."""

    def __init__(self) -> None:
        self.agent_running: bool = False
        """True while an agent turn (worker-backed or inline) is in flight."""

        self.agent_worker: Worker[None] | None = None
        """The Textual worker running the current agent turn, if worker-backed."""

        self.shell_running: bool = False
        """True while a `!`-prefixed shell command is executing."""

    @property
    def busy(self) -> bool:
        """Whether an agent or shell turn is currently in flight.

        The single definition of "in flight" that every dispatch guard should
        consult before starting new work.
        """
        return self.agent_running or self.shell_running

    def begin_agent(self, worker: Worker[None]) -> None:
        """Mark a worker-backed agent turn as started.

        Sets the run flag and stores the worker handle together so the pair can
        never drift (running-without-worker or vice versa).
        """
        self.agent_running = True
        self.agent_worker = worker

    def end_agent(self) -> None:
        """Mark the current agent turn as finished, clearing the worker handle."""
        self.agent_running = False
        self.agent_worker = None

    def begin_shell(self) -> None:
        """Mark a `!` shell command as started."""
        self.shell_running = True

    def end_shell(self) -> None:
        """Mark the current shell command as finished."""
        self.shell_running = False
