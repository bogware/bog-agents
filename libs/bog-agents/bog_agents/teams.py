"""Governed agent teams — claimable task ledger, mailboxes, coordinator (#21).

The market moved to shared-tasklist agent teams (Claude Code, Devin, Factory).
bog already owns the team registry, parallel worktrees, orchestrator, and — as
of #25 — a per-agent cost ledger with runaway caps. The missing delta this
module supplies is the *coordination substrate*:

- **TaskLedger** — a shared board whose `claim_next` is atomic, so two teammates
  never grab the same task, and dependency-aware, so a task isn't claimable
  until its prerequisites are done. This is what turns static fan-out into a
  real team.
- **Mailbox** — addressed peer messaging (member→member or `@all`), beyond a
  broadcast log, so teammates coordinate.
- **run_team** — the coordinator loop: round by round, free members claim
  claimable tasks and work them concurrently under the cost ledger's spawn and
  spend caps, until the board drains or deadlocks. It's injectable
  (`teammate_runner`), so the coordination logic unit-tests without real agents;
  the CLI/daemon supply the real runner. The moat over ungoverned team modes:
  this runs under the cost caps, and composes with expert rules / DLP / audit.
"""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bog_agents.cost_ledger import CostLedger

# Task lifecycle states.
OPEN = "open"
CLAIMED = "claimed"
DONE = "done"
FAILED = "failed"


@dataclass
class LedgerTask:
    """One unit of work on the shared board."""

    id: str
    title: str
    description: str = ""
    status: str = OPEN
    owner: str | None = None
    depends_on: list[str] = field(default_factory=list)
    result: str = ""
    error: str = ""

    @property
    def terminal(self) -> bool:
        """True once the task is done or failed."""
        return self.status in (DONE, FAILED)


class TaskLedger:
    """A shared, atomically-claimable, dependency-aware task board.

    Thread- and coroutine-safe: `claim_next` is guarded so concurrent teammates
    can't grab the same task. A task is only claimable once every id in its
    `depends_on` has completed successfully (a failed dependency blocks it,
    surfacing as a deadlock rather than running work on a broken prerequisite).
    """

    def __init__(self) -> None:
        """Create an empty task board."""
        self._tasks: dict[str, LedgerTask] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()

    def add(self, title: str, *, description: str = "", depends_on: Sequence[str] = (), task_id: str | None = None) -> LedgerTask:
        """Post a task to the board and return it."""
        tid = task_id or uuid.uuid4().hex[:12]
        task = LedgerTask(id=tid, title=title, description=description, depends_on=list(depends_on))
        with self._lock:
            self._tasks[tid] = task
            self._order.append(tid)
        return task

    def get(self, task_id: str) -> LedgerTask | None:
        """Look up a task by id."""
        with self._lock:
            return self._tasks.get(task_id)

    def _deps_done(self, task: LedgerTask) -> bool:
        return all(self._tasks.get(dep, LedgerTask(id=dep, title="")).status == DONE for dep in task.depends_on)

    def claim_next(self, member: str) -> LedgerTask | None:
        """Atomically claim the first open task whose dependencies are done.

        Returns the claimed task (now `CLAIMED` by `member`), or None when no
        task is currently claimable (board done, or remaining tasks are blocked).
        """
        with self._lock:
            for tid in self._order:
                task = self._tasks[tid]
                if task.status == OPEN and self._deps_done(task):
                    task.status = CLAIMED
                    task.owner = member
                    return task
            return None

    def complete(self, task_id: str, member: str, result: str = "") -> None:
        """Mark a claimed task done with its result."""
        with self._lock:
            task = self._tasks.get(task_id)
            if task is not None:
                task.status = DONE
                task.owner = member
                task.result = result

    def fail(self, task_id: str, member: str, error: str = "") -> None:
        """Mark a claimed task failed with a reason."""
        with self._lock:
            task = self._tasks.get(task_id)
            if task is not None:
                task.status = FAILED
                task.owner = member
                task.error = error

    def tasks(self) -> list[LedgerTask]:
        """All tasks in insertion order (a snapshot)."""
        with self._lock:
            return [self._tasks[tid] for tid in self._order]

    def is_done(self) -> bool:
        """True when no task remains open or claimed (all terminal)."""
        with self._lock:
            return all(t.terminal for t in self._tasks.values())

    def has_claimable(self) -> bool:
        """True when at least one open task's dependencies are satisfied."""
        with self._lock:
            return any(t.status == OPEN and self._deps_done(t) for t in self._tasks.values())

    def format_board(self) -> str:
        """Render the board grouped by status."""
        marks = {OPEN: "○", CLAIMED: "◐", DONE: "●", FAILED: "✗"}
        lines = ["## Task board", ""]
        for task in self.tasks():
            owner = f" @{task.owner}" if task.owner else ""
            dep = f" (needs {', '.join(task.depends_on)})" if task.depends_on else ""
            lines.append(f"{marks.get(task.status, '?')} {task.title}{owner}{dep}")
        return "\n".join(lines)


@dataclass(frozen=True)
class Attachment:
    """A file, directory (zip) or patch staged for a teammate (ROADMAP #76).

    Attributes:
        kind: `file`, `dir` (a zip) or `patch` (a `git diff`).
        name: File name in the exchange directory.
        path: Absolute path of the staged copy.
        sha256: `sha256:<hex>` of the staged bytes (content address).
        size: Staged size in bytes.
        redactions: DLP redactions applied before staging.
        source: Where it came from (path or `git diff HEAD`).
    """

    kind: str
    name: str
    path: str
    sha256: str
    size: int = 0
    redactions: int = 0
    source: str = ""

    def to_dict(self) -> dict[str, object]:
        """JSON-ready mapping."""
        return {
            "kind": self.kind,
            "name": self.name,
            "path": self.path,
            "sha256": self.sha256,
            "size": self.size,
            "redactions": self.redactions,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> Attachment:
        """Inverse of `to_dict` (tolerant of missing optional keys)."""
        return cls(
            kind=str(data.get("kind", "file")),
            name=str(data.get("name", "")),
            path=str(data.get("path", "")),
            sha256=str(data.get("sha256", "")),
            size=int(data.get("size", 0) or 0),  # type: ignore[call-overload]
            redactions=int(data.get("redactions", 0) or 0),  # type: ignore[call-overload]
            source=str(data.get("source", "")),
        )


@dataclass
class Message:
    """One addressed message between teammates."""

    sender: str
    recipient: str  # a member name, or "@all" for a broadcast
    body: str
    ts: float = field(default_factory=time.time)
    attachments: tuple[Attachment, ...] = ()


class Mailbox:
    """Per-member addressed inboxes for peer coordination.

    `send` addresses a specific member (or `@all`); `inbox` returns messages a
    member can see (addressed to them or broadcast) without consuming them;
    `drain` returns and marks them consumed so a claim-loop reads each once.
    """

    ALL = "@all"

    def __init__(self) -> None:
        """Create an empty mailbox."""
        self._messages: list[Message] = []
        self._consumed: dict[str, int] = {}
        self._lock = threading.Lock()

    def send(self, sender: str, recipient: str, body: str, *, attachments: tuple[Attachment, ...] = ()) -> Message:
        """Post a message (optionally carrying attachments); returns the stored `Message`."""
        msg = Message(sender=sender, recipient=recipient, body=body, attachments=tuple(attachments))
        with self._lock:
            self._messages.append(msg)
        return msg

    def _visible(self, member: str) -> list[Message]:
        return [m for m in self._messages if m.recipient in (member, self.ALL) and m.sender != member]

    def inbox(self, member: str) -> list[Message]:
        """All messages `member` can see (peek, non-consuming)."""
        with self._lock:
            return list(self._visible(member))

    def drain(self, member: str) -> list[Message]:
        """Return unread messages for `member` and mark them read."""
        with self._lock:
            visible = self._visible(member)
            already = self._consumed.get(member, 0)
            fresh = visible[already:]
            self._consumed[member] = len(visible)
            return fresh


@dataclass
class TaskResult:
    """A teammate's outcome for one task."""

    success: bool
    result: str = ""
    error: str = ""


@dataclass
class TeamReport:
    """The outcome of a team run."""

    ledger: TaskLedger
    activations: int = 0
    stopped_reason: str = "completed"

    @property
    def all_done(self) -> bool:
        """True when every task finished (done, not failed)."""
        return all(t.status == DONE for t in self.ledger.tasks())

    def format_summary(self) -> str:
        """Render the final board + activation count + stop reason."""
        done = sum(1 for t in self.ledger.tasks() if t.status == DONE)
        failed = sum(1 for t in self.ledger.tasks() if t.status == FAILED)
        return f"{self.ledger.format_board()}\n\n{done} done, {failed} failed over {self.activations} activations ({self.stopped_reason})."


TeammateRunner = Callable[[str, LedgerTask, Mailbox], Awaitable[TaskResult]]
"""Run one teammate on one claimed task (with mailbox access) → result."""


async def run_team(
    ledger: TaskLedger,
    members: Sequence[str],
    *,
    teammate_runner: TeammateRunner,
    cost_ledger: CostLedger | None = None,
    mailbox: Mailbox | None = None,
    max_activations: int = 1000,
) -> TeamReport:
    """Coordinate `members` working `ledger` until it drains, under cost caps.

    Round by round: each free member atomically claims a claimable task (subject
    to the cost ledger's subagent-spawn cap), all claims for the round run
    concurrently via `teammate_runner`, and results are written back. Stops when
    the board is done, nothing is claimable (deadlock), a cost/spawn cap denies
    further work, or `max_activations` is exceeded.

    Args:
        ledger: The shared task board.
        members: Teammate names (each can hold one task per round).
        teammate_runner: Runs a member on a claimed task (injected).
        cost_ledger: When set, its spawn cap gates each activation and its cost
            cap is checked before each round.
        mailbox: Shared mailbox passed to each teammate (created if None).
        max_activations: Hard ceiling on total teammate activations (runaway backstop).

    Returns:
        A `TeamReport` with the final ledger and stop reason.
    """
    box = mailbox or Mailbox()
    activations = 0
    reason = "completed"

    while not ledger.is_done():
        if cost_ledger is not None and not cost_ledger.check_cost().allowed:
            reason = "cost cap reached"
            break

        assignments: list[tuple[str, LedgerTask]] = []
        cap_stop = False
        for member in members:
            if cost_ledger is not None and not cost_ledger.register_subagent_spawn().allowed:
                reason = "spawn cap reached"
                cap_stop = True
                break
            task = ledger.claim_next(member)
            if task is None:
                break
            assignments.append((member, task))

        if not assignments:
            # A spawn cap already set the reason; otherwise nothing claimable and
            # not done means the board is blocked by a failed dependency.
            if not cap_stop and not ledger.is_done():
                reason = "deadlocked (blocked by failed dependencies)"
            break

        results = await asyncio.gather(*[_run_teammate(teammate_runner, m, t, box) for m, t in assignments])
        for (member, task), res in zip(assignments, results, strict=True):
            if res.success:
                ledger.complete(task.id, member, res.result)
            else:
                ledger.fail(task.id, member, res.error)

        activations += len(assignments)
        if cap_stop:
            break  # ran what this round's budget allowed, then stop
        if activations >= max_activations:
            reason = "max activations reached"
            break

    return TeamReport(ledger=ledger, activations=activations, stopped_reason=reason)


async def _run_teammate(runner: TeammateRunner, member: str, task: LedgerTask, mailbox: Mailbox) -> TaskResult:
    """Invoke a teammate runner, converting any exception into a failed result."""
    try:
        return await runner(member, task, mailbox)
    except Exception as exc:  # noqa: BLE001 - a crashing teammate fails its task, not the team
        return TaskResult(success=False, error=str(exc))
