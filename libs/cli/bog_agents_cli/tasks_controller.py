"""`/tasks` command center and `/recap` (ROADMAP #68).

One tree over every unit of work the TUI process can see:

- the interactive thread (status *waiting on you* while an approval menu is
  open, *running* while a turn or tracked session is in flight),
- prompts queued behind it (editable: `queue edit <n> <text>`, `queue drop <n>`),
- background tasks and persistent jobs (`BackgroundAgentManager`),
- remote tasks,
- `/team run` sessions with their ledger tasks, mailbox and spend
  (`TeamRunHandle`, registered by the App when a run starts),
- the ambient daemon's jobs and recent runs (when it is running).

Per-node verbs: `kill`, `steer <text>` (team mailbox / task inbox / next
prompt), `pause` + `resume` (team runs: the coordinator stops claiming new
tasks), `diff` (the task's worktree branch). Everything that needs no widget
is a pure function over a duck-typed `app`, so it unit-tests with a
`SimpleNamespace`; `run_tasks_command` / `run_recap_command` are the thin
App-facing dispatchers.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

ROOT_ID = "session"
MAIN_ID = "main"
STATUS_GLYPH: dict[str, str] = {
    "running": "▶",
    "waiting": "⏸",
    "paused": "⏸",
    "queued": "○",
    "idle": "·",
    "done": "✔",
    "failed": "✗",
    "cancelled": "-",
}
_TERMINAL = frozenset({"done", "failed", "cancelled"})
_REMOVALS: set[asyncio.Future[Any]] = set()
"""Widget removals kicked off from sync verbs, kept referenced until they finish."""


@dataclass
class TaskNode:
    """One unit of work in the task tree."""

    id: str
    kind: str
    title: str
    status: str
    detail: str = ""
    started_at: float | None = None
    finished_at: float | None = None
    tokens: int | None = None
    cost_usd: float | None = None
    actions: tuple[str, ...] = ()
    children: list[TaskNode] = field(default_factory=list)

    def walk(self) -> Iterator[TaskNode]:
        """Depth-first iteration over this node and its descendants.

        Yields:
            Each node, parents before children.
        """
        yield self
        for child in self.children:
            yield from child.walk()

    @property
    def terminal(self) -> bool:
        """Whether the node has finished (done / failed / cancelled)."""
        return self.status in _TERMINAL


@dataclass
class TeamRunHandle:
    """Live handle the App keeps for a `/team run` session so `/tasks` can see and steer it."""

    run_id: str
    title: str
    ledger: Any
    mailbox: Any
    cost_ledger: Any
    members: list[str]
    pause_gate: asyncio.Event
    worker: Any = None
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    status: str = "running"
    report: Any = None

    @property
    def paused(self) -> bool:
        """Whether new task claims are held back."""
        return not self.pause_gate.is_set()


# ---------------------------------------------------------------- normalisation


def normalize_status(value: object) -> str:
    """Map the many status vocabularies (enums, strings, ledger constants) onto the tree's."""
    raw = str(getattr(value, "value", value) or "").lower()
    table = {
        "queued": "queued",
        "pending": "queued",
        "open": "queued",
        "scheduled": "queued",
        "running": "running",
        "claimed": "running",
        "submitted": "running",
        "in_progress": "running",
        "completed": "done",
        "done": "done",
        "success": "done",
        "succeeded": "done",
        "failed": "failed",
        "error": "failed",
        "cancelled": "cancelled",
        "canceled": "cancelled",
        "paused": "paused",
        "waiting": "waiting",
        "idle": "idle",
        "disabled": "idle",
    }
    return table.get(raw, raw or "idle")


def _duration(started: float | None, finished: float | None) -> str:
    if not started:
        return ""
    end = finished or time.time()
    seconds = max(0, int(end - started))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


def _clip(text: str, width: int = 60) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= width else text[: width - 1] + "…"


# ---------------------------------------------------------------- builders


def pending_approval_summary(app: Any) -> str | None:  # noqa: ANN401 - the App
    """What the open approval menu is asking for, or `None` when nothing is pending."""
    menu = getattr(app, "_pending_approval_widget", None)
    if menu is None:
        return None
    requests = getattr(menu, "_action_requests", None) or []
    names: list[str] = []
    for req in requests:
        if isinstance(req, dict):
            name = str(req.get("name", "?"))
            args = req.get("args") or {}
            command = args.get("command") if isinstance(args, dict) else None
            names.append(f"{name} ({_clip(str(command), 40)})" if command else name)
    return "approve " + ", ".join(names) if names else "approve a tool call"


def main_thread_node(app: Any) -> TaskNode:  # noqa: ANN401 - the App
    """The interactive thread: waiting on you / running / idle, with context and spend."""
    thread_id = None
    getter = getattr(app, "_current_thread_id", None)
    if callable(getter):
        thread_id = getter()
    pending = pending_approval_summary(app)
    turns = getattr(app, "_turns", None)
    busy = bool(getattr(turns, "busy", False))
    if pending:
        status, detail = "waiting", f"waiting on you: {pending}"
    elif busy:
        status, detail = "running", "agent turn in flight"
    else:
        status, detail = "idle", "ready for your next prompt"
    tracker = getattr(app, "_token_tracker", None)
    tokens = int(getattr(tracker, "current_context", 0) or 0) or None
    cost: float | None = None
    stats = getattr(app, "_session_stats", None)
    if stats is not None:
        try:
            from bog_agents_cli.cost_controller import turn_cost_usd

            cost = turn_cost_usd(stats)
        except Exception:  # spend is decoration, never a blocker
            cost = None
    node = TaskNode(
        id=MAIN_ID,
        kind="thread",
        title=f"thread {thread_id[:12]}" if thread_id else "thread (new)",
        status=status,
        detail=detail,
        tokens=tokens,
        cost_usd=cost,
        actions=("steer",),
    )
    node.children.extend(queued_nodes(app))
    return node


def queued_nodes(app: Any) -> list[TaskNode]:  # noqa: ANN401 - the App
    """Prompts waiting behind the current turn, as `q1..qN`."""
    pending = list(getattr(app, "_pending_messages", None) or [])
    return [
        TaskNode(
            id=f"q{i}",
            kind="queued",
            title=_clip(getattr(msg, "text", str(msg))),
            status="queued",
            detail="internal prompt" if getattr(msg, "raw", False) else "",
            actions=("edit", "drop"),
        )
        for i, msg in enumerate(pending, start=1)
    ]


def _inbox_count(task: Any) -> int:  # noqa: ANN401 - BackgroundTask | RemoteTask
    metadata = getattr(task, "metadata", None)
    inbox = metadata.get("inbox") if isinstance(metadata, dict) else None
    return len(inbox) if isinstance(inbox, list) else 0


def background_nodes(app: Any) -> list[TaskNode]:  # noqa: ANN401 - the App
    """Background tasks / persistent jobs from the app's `BackgroundAgentManager`."""
    manager = getattr(app, "_bg_manager", None)
    if manager is None:
        return []
    tasks = manager.all_tasks
    tasks = tasks() if callable(tasks) else tasks
    nodes: list[TaskNode] = []
    for task in tasks:
        status = normalize_status(getattr(task, "status", ""))
        parts = []
        if getattr(task, "worktree_branch", ""):
            parts.append(f"worktree {task.worktree_branch}")
        if (count := _inbox_count(task)) > 0:
            parts.append(f"inbox {count}")
        if getattr(task, "error", None):
            parts.append(f"error: {_clip(str(task.error), 50)}")
        actions: list[str] = ["steer"]
        if status in ("running", "queued"):
            actions.insert(0, "kill")
        if getattr(task, "worktree_branch", ""):
            actions.append("diff")
        nodes.append(
            TaskNode(
                id=str(task.task_id),
                kind="job"
                if type(manager).__name__ == "PersistentJobsManager"
                else "background",
                title=_clip(getattr(task, "label", "") or getattr(task, "prompt", "")),
                status=status,
                detail=" · ".join(parts),
                started_at=getattr(task, "started_at", None),
                finished_at=getattr(task, "completed_at", None),
                actions=tuple(actions),
            )
        )
    return nodes


def remote_nodes(app: Any) -> list[TaskNode]:  # noqa: ANN401 - the App
    """Tasks running on a remote provider (`/remote`)."""
    remote = getattr(app, "_remote_tasks", None) or {}
    nodes: list[TaskNode] = []
    for task in remote.values():
        status = normalize_status(getattr(task, "status", ""))
        actions = ("kill", "steer") if status in ("running", "queued") else ("steer",)
        nodes.append(
            TaskNode(
                id=str(getattr(task, "task_id", "?")),
                kind="remote",
                title=_clip(getattr(task, "label", "") or getattr(task, "prompt", "")),
                status=status,
                detail=(
                    f"error: {_clip(str(task.error), 50)}"
                    if getattr(task, "error", "")
                    else ""
                ),
                actions=actions,
            )
        )
    return nodes


def team_nodes(app: Any) -> list[TaskNode]:  # noqa: ANN401 - the App
    """`/team run` sessions with one child per ledger task."""
    runs = getattr(app, "_team_runs", None) or {}
    nodes: list[TaskNode] = []
    for handle in runs.values():
        tasks = list(handle.ledger.tasks()) if hasattr(handle.ledger, "tasks") else []
        done = sum(1 for t in tasks if normalize_status(t.status) == "done")
        status = (
            "paused"
            if (handle.status == "running" and handle.paused)
            else handle.status
        )
        cost = getattr(handle.cost_ledger, "total_cost_usd", None)
        actions = (
            ("kill", "steer", "resume" if handle.paused else "pause")
            if handle.status == "running"
            else ()
        )
        node = TaskNode(
            id=handle.run_id,
            kind="team",
            title=handle.title,
            status=normalize_status(status),
            detail=f"{done}/{len(tasks)} tasks done · {', '.join(handle.members)}",
            started_at=handle.started_at,
            finished_at=handle.finished_at,
            cost_usd=float(cost) if cost is not None else None,
            actions=actions,
        )
        for task in tasks:
            owner = getattr(task, "owner", None)
            detail = f"by {owner}" if owner else ""
            if getattr(task, "error", ""):
                detail = f"{detail} · error: {_clip(task.error, 40)}".strip(" ·")
            node.children.append(
                TaskNode(
                    id=f"{handle.run_id}/{task.id}",
                    kind="team-task",
                    title=_clip(task.title),
                    status=normalize_status(task.status),
                    detail=detail,
                    actions=("steer",) if owner and handle.status == "running" else (),
                )
            )
        nodes.append(node)
    return nodes


async def daemon_nodes() -> list[TaskNode]:
    """The ambient daemon's jobs and recent runs; empty when it is not running."""
    try:
        from bog_agents_cli.daemon_client import (
            is_daemon_running,
            list_daemon_jobs,
            list_daemon_runs,
        )

        if not is_daemon_running():
            return []
        jobs = await list_daemon_jobs()
        runs = await list_daemon_runs()
    except Exception:  # a flaky daemon never breaks the tree
        return []
    by_job: dict[str, list[dict[str, Any]]] = {}
    for run in runs:
        by_job.setdefault(str(run.get("job_id", "")), []).append(run)
    nodes: list[TaskNode] = []
    for job in jobs:
        job_id = str(job.get("job_id") or job.get("id") or "?")
        enabled = job.get("enabled", True)
        node = TaskNode(
            id=f"daemon:{job_id}",
            kind="daemon-job",
            title=_clip(str(job.get("name") or job_id)),
            status="idle" if enabled else "cancelled",
            detail=("enabled" if enabled else "disabled")
            + (f" · next {job['next_run_at']}" if job.get("next_run_at") else ""),
        )
        for run in by_job.get(job_id, [])[:5]:
            node.children.append(
                TaskNode(
                    id=f"run:{run.get('run_id', '?')}",
                    kind="daemon-run",
                    title=f"run {str(run.get('run_id', '?'))[:8]}",
                    status=normalize_status(run.get("status", "")),
                    detail=_clip(str(run.get("error") or run.get("output") or ""), 50),
                    started_at=run.get("started_at") or None,
                    finished_at=run.get("finished_at") or None,
                )
            )
        nodes.append(node)
    return nodes


async def build_task_tree(app: Any, *, include_daemon: bool = True) -> TaskNode:  # noqa: ANN401 - the App
    """Assemble the whole tree for this session."""
    root = TaskNode(id=ROOT_ID, kind="session", title="session", status="idle")
    root.children.append(main_thread_node(app))
    root.children.extend(background_nodes(app))
    root.children.extend(remote_nodes(app))
    root.children.extend(team_nodes(app))
    if include_daemon:
        root.children.extend(await daemon_nodes())
    return root


def find_node(root: TaskNode, node_id: str) -> TaskNode | None:
    """Look a node up by id (exact, then unique prefix)."""
    wanted = node_id.strip()
    for node in root.walk():
        if node.id == wanted:
            return node
    matches = [n for n in root.walk() if n.id.startswith(wanted) and n.id != ROOT_ID]
    return matches[0] if len(matches) == 1 else None


# ---------------------------------------------------------------- rendering


def _counts(root: TaskNode) -> dict[str, int]:
    counts: dict[str, int] = {}
    for node in root.walk():
        if node.id == ROOT_ID or node.kind in ("queued", "team-task", "daemon-run"):
            continue
        counts[node.status] = counts.get(node.status, 0) + 1
    return counts


def render_task_tree(root: TaskNode) -> str:
    """Plain-text tree with a one-line summary and the verbs at the bottom."""
    counts = _counts(root)
    main = next((n for n in root.children if n.id == MAIN_ID), None)
    summary = ", ".join(
        f"{count} {status}"
        for status, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    )
    spend = f"  session ${main.cost_usd:.2f}" if main and main.cost_usd else ""
    lines = [f"Tasks — {summary or 'nothing running'}{spend}"]
    if main and main.status == "waiting":
        lines.append(f"⏸ {main.detail} — answer in the approval menu (y / n / r / x)")

    def _emit(node: TaskNode, depth: int) -> None:
        glyph = STATUS_GLYPH.get(node.status, "·")
        meta: list[str] = [node.status]
        if node.tokens:
            meta.append(f"{node.tokens / 1000:.1f}k tok")
        if node.cost_usd:
            meta.append(f"${node.cost_usd:.2f}")
        if node.started_at:
            meta.append(_duration(node.started_at, node.finished_at))
        head = f"{'  ' * depth}{glyph} {node.id:<14} {node.title}"
        tail = "  ".join(meta)
        line = f"{head}  [{tail}]"
        if node.detail and not (node.kind == "thread" and node.status == "waiting"):
            line += f"  {node.detail}"
        if node.actions:
            line += f"  ({'/'.join(node.actions)})"
        lines.append(line)
        for child in node.children:
            _emit(child, depth + 1)

    for child in root.children:
        _emit(child, 0)
    lines.append("")
    lines.append(
        "/tasks kill <id> | steer <id> <text> | pause|resume <id> | diff <id> | queue edit <n> <text> | queue drop <n> | /recap"
    )
    return "\n".join(lines)


# ---------------------------------------------------------------- team run registry


def _team_mailbox(app: Any) -> Any:  # noqa: ANN401 - the App / Mailbox or MailboxStore
    """ROADMAP #76: a SQLite mailbox keyed by the interactive thread so teammate messages outlive the session."""
    from bog_agents.teams import Mailbox

    getter = getattr(app, "_current_thread_id", None)
    thread = getter() if callable(getter) else None
    if not thread:
        return Mailbox()
    try:
        from bog_agents.mailbox_store import MailboxStore

        from bog_agents_cli._env_vars import bog_agents_home

        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(thread))[:80]
        return MailboxStore(bog_agents_home() / "mailboxes" / f"{safe}.db")
    except Exception:
        logger.debug(
            "persistent team mailbox unavailable; using in-memory", exc_info=True
        )
        return Mailbox()


def register_team_run(
    app: Any,  # noqa: ANN401 - the App
    task_specs: Sequence[Any],
    members: Sequence[str],
    *,
    caps: Any = None,  # noqa: ANN401 - RunawayCaps
) -> TeamRunHandle:
    """Create the ledger / mailbox / cost ledger for a `/team run` and expose them to `/tasks`."""
    from bog_agents.cost_ledger import CostLedger, RunawayCaps

    from bog_agents_cli.team_executor import build_ledger

    runs = getattr(app, "_team_runs", None)
    if runs is None:
        runs = {}
        app._team_runs = runs
    run_id = f"team-{len(runs) + 1}"
    gate = asyncio.Event()
    gate.set()
    turns = getattr(app, "_turns", None)
    handle = TeamRunHandle(
        run_id=run_id,
        title=f"{len(task_specs)} task(s), {len(members)} worker(s)",
        ledger=build_ledger(task_specs),
        mailbox=_team_mailbox(app),
        cost_ledger=CostLedger(caps=caps or RunawayCaps()),
        members=list(members),
        pause_gate=gate,
        worker=getattr(turns, "agent_worker", None),
    )
    runs[run_id] = handle
    return handle


def finish_team_run(handle: TeamRunHandle, *, status: str, report: Any = None) -> None:  # noqa: ANN401 - TeamReport
    """Mark a registered run finished (kept in the tree so `/recap` can report it)."""
    handle.status = status
    handle.finished_at = time.time()
    handle.report = report
    handle.pause_gate.set()


# ---------------------------------------------------------------- verbs


def push_task_inbox(task: Any, body: str, *, sender: str = "supervisor") -> int:  # noqa: ANN401 - BackgroundTask | RemoteTask
    """Queue a steering message on a background / remote task (the same shape `/team message` writes)."""
    metadata = getattr(task, "metadata", None)
    if not isinstance(metadata, dict):
        metadata = {}
        task.metadata = metadata
    inbox = metadata.get("inbox")
    if not isinstance(inbox, list):
        inbox = []
        metadata["inbox"] = inbox
    inbox.append({"body": body, "sender": sender, "created_at": time.time()})
    return len(inbox)


def _find_task(app: Any, task_id: str) -> Any:  # noqa: ANN401 - the App / task
    manager = getattr(app, "_bg_manager", None)
    if manager is not None:
        task = manager.get_status(task_id)
        if task is not None:
            return task
    return (getattr(app, "_remote_tasks", None) or {}).get(task_id)


async def kill_node(app: Any, node: TaskNode) -> str:  # noqa: ANN401 - the App
    """Stop the work behind `node`; returns the message to show."""
    if node.kind in ("background", "job"):
        manager = getattr(app, "_bg_manager", None)
        ok = manager.cancel(node.id) if manager is not None else False
        return (
            f"Cancel requested for {node.id}." if ok else f"{node.id} is not running."
        )
    if node.kind == "remote":
        handler = getattr(app, "_handle_agent_command", None)
        if handler is not None:
            await handler(f"/agent stop {node.id}", echo=False)
            return f"Stop requested for remote task {node.id}."
        return "Remote tasks cannot be stopped from here."
    if node.kind == "team":
        handle = (getattr(app, "_team_runs", None) or {}).get(node.id)
        if handle is None or handle.status != "running":
            return f"{node.id} is not running."
        worker = handle.worker
        if worker is not None and hasattr(worker, "cancel"):
            worker.cancel()
        finish_team_run(handle, status="cancelled")
        return f"Cancelled team run {node.id}."
    if node.kind == "queued":
        return drop_queued(app, int(node.id[1:]))
    return f"{node.kind} nodes cannot be killed from /tasks."


def steer_node(app: Any, node: TaskNode, text: str) -> str:  # noqa: ANN401 - the App
    """Send `text` to the work behind `node`."""
    if not text.strip():
        return "Usage: /tasks steer <id> <text>"
    if node.kind == "thread":
        return queue_prompt(app, text)
    if node.kind in ("background", "job", "remote"):
        task = _find_task(app, node.id)
        if task is None:
            return f"{node.id} not found."
        count = push_task_inbox(task, text)
        return f"Queued for {node.id} (inbox {count}); it is read at the task's next checkpoint."
    if node.kind in ("team", "team-task"):
        run_id, _, task_id = node.id.partition("/")
        handle = (getattr(app, "_team_runs", None) or {}).get(run_id)
        if handle is None:
            return f"{run_id} not found."
        recipient = (
            handle.mailbox.ALL
            if not task_id
            else (
                getattr(handle.ledger.get(task_id), "owner", None) or handle.mailbox.ALL
            )
        )
        handle.mailbox.send("supervisor", recipient, text)
        return f"Sent to {recipient} via the team mailbox."
    return f"{node.kind} nodes cannot be steered."


def set_paused(app: Any, node: TaskNode, *, paused: bool) -> str:  # noqa: ANN401 - the App
    """Pause / resume a team run (the coordinator stops claiming new tasks while paused)."""
    if node.kind != "team":
        return "Only team runs can be paused (background tasks: kill and resubmit)."
    handle = (getattr(app, "_team_runs", None) or {}).get(node.id)
    if handle is None or handle.status != "running":
        return f"{node.id} is not running."
    if paused:
        handle.pause_gate.clear()
        return f"Paused {node.id}: running tasks finish, no new task is claimed until /tasks resume {node.id}."
    handle.pause_gate.set()
    return f"Resumed {node.id}."


def queue_prompt(app: Any, text: str) -> str:  # noqa: ANN401 - the App
    """Append a prompt to the queue behind the current turn."""
    from bog_agents_cli.app import QueuedMessage

    pending = getattr(app, "_pending_messages", None)
    if pending is None:
        return "No prompt queue on this app."
    pending.append(QueuedMessage(text=text, mode="normal"))
    return f"Queued as q{len(pending)}; it runs when the current turn ends."


def edit_queued(app: Any, index: int, text: str) -> str:  # noqa: ANN401 - the App
    """Replace queued prompt `index` (1-based)."""
    from bog_agents_cli.app import QueuedMessage

    pending = getattr(app, "_pending_messages", None) or []
    if not 1 <= index <= len(pending):
        return f"No queued prompt q{index}."
    if not text.strip():
        return "Usage: /tasks queue edit <n> <text>"
    old = pending[index - 1]
    pending[index - 1] = QueuedMessage(
        text=text, mode=getattr(old, "mode", "normal"), raw=False
    )
    widgets = getattr(app, "_queued_widgets", None) or []
    if index <= len(widgets) and hasattr(widgets[index - 1], "update"):
        widgets[index - 1].update(text)
    return f"q{index} now: {_clip(text)}"


def drop_queued(app: Any, index: int) -> str:  # noqa: ANN401 - the App
    """Remove queued prompt `index` (1-based) and its widget."""
    pending = getattr(app, "_pending_messages", None)
    if pending is None or not 1 <= index <= len(pending):
        return f"No queued prompt q{index}."
    del pending[index - 1]
    widgets = getattr(app, "_queued_widgets", None)
    if widgets and index <= len(widgets):
        widget = widgets[index - 1]
        del widgets[index - 1]
        remover = getattr(widget, "remove", None)
        if callable(remover):
            result = remover()
            if asyncio.iscoroutine(result):
                task = asyncio.ensure_future(result)
                _REMOVALS.add(task)
                task.add_done_callback(_REMOVALS.discard)
    return f"Dropped q{index}."


async def diff_for_node(app: Any, node: TaskNode) -> tuple[bool, str]:  # noqa: ANN401 - the App
    """`git diff` for the node's worktree branch against HEAD."""
    task = _find_task(app, node.id) if node.kind in ("background", "job") else None
    branch = getattr(task, "worktree_branch", "") if task is not None else ""
    if not branch:
        return False, f"{node.id} has no worktree branch to diff."
    from bog_agents.git_env import NO_EXTERNAL_DIFF

    ok, output = await app._run_git(
        ["diff", *NO_EXTERNAL_DIFF, "--stat", f"HEAD...{branch}"]
    )
    if not ok:
        return False, output or f"git diff failed for {branch}"
    ok2, patch = await app._run_git(["diff", *NO_EXTERNAL_DIFF, f"HEAD...{branch}"])
    return True, f"{output}\n\n{patch if ok2 else ''}".strip()


async def run_tasks_command(app: Any, command: str) -> None:  # noqa: ANN401 - the App
    """Body of `/tasks`: render the tree or dispatch a verb."""
    from bog_agents_cli.widgets.messages import AppMessage, DiffMessage

    words = command.strip().split()
    verb = words[1].lower() if len(words) > 1 else "list"
    rest = words[2:]
    root = await build_task_tree(app, include_daemon=verb == "list")
    if verb in ("list", "tree", "show"):
        await app._mount_message(AppMessage(render_task_tree(root)))
        return
    if verb == "recap":
        await run_recap_command(app, "/recap")
        return
    if verb == "queue":
        sub = rest[0].lower() if rest else "list"
        if sub == "edit" and len(rest) >= 3 and rest[1].lstrip("q").isdigit():
            await app._mount_message(
                AppMessage(
                    edit_queued(app, int(rest[1].lstrip("q")), " ".join(rest[2:]))
                )
            )
        elif sub == "drop" and len(rest) >= 2 and rest[1].lstrip("q").isdigit():
            await app._mount_message(
                AppMessage(drop_queued(app, int(rest[1].lstrip("q"))))
            )
        else:
            queued = queued_nodes(app)
            text = "\n".join(f"{n.id}: {n.title}" for n in queued) or "Nothing queued."
            await app._mount_message(
                AppMessage(text + "\n\n/tasks queue edit <n> <text> | queue drop <n>")
            )
        return
    if not rest:
        await app._mount_message(
            AppMessage(
                f"Usage: /tasks {verb} <id>" + (" <text>" if verb == "steer" else "")
            )
        )
        return
    node = find_node(root, rest[0])
    if node is None:
        await app._mount_message(AppMessage(f"No task {rest[0]!r}. /tasks lists ids."))
        return
    if verb == "kill":
        message = await kill_node(app, node)
    elif verb == "steer":
        message = steer_node(app, node, " ".join(rest[1:]))
    elif verb in ("pause", "resume"):
        message = set_paused(app, node, paused=verb == "pause")
    elif verb == "diff":
        ok, text = await diff_for_node(app, node)
        await app._mount_message(DiffMessage(text) if ok else AppMessage(text))
        return
    else:
        message = f"Unknown verb {verb!r}. Verbs: list, kill, steer, pause, resume, diff, queue, recap."
    await app._mount_message(AppMessage(message))


# ---------------------------------------------------------------- recap


def build_recap(app: Any, root: TaskNode, *, notes: Sequence[Any] = ()) -> str:  # noqa: ANN401 - the App
    """Where this session stands: turns, spend, files, tasks, what needs you, and your `/btw` notes."""
    main = next((n for n in root.children if n.id == MAIN_ID), None)
    stats = getattr(app, "_session_stats", None)
    lines = ["## Recap", ""]
    if main is not None:
        lines.append(f"- **{main.title}** — {main.detail}")
    if stats is not None:
        turns = getattr(stats, "request_count", 0)
        tokens_in = getattr(stats, "input_tokens", 0)
        tokens_out = getattr(stats, "output_tokens", 0)
        files = len(getattr(stats, "file_records", []) or [])
        spend = f", ${main.cost_usd:.2f}" if main and main.cost_usd else ""
        lines.append(
            f"- {turns} model requests, {tokens_in:,} in / {tokens_out:,} out tokens{spend}; {files} file change(s) recorded this session"
        )
    queued = queued_nodes(app)
    if queued:
        lines.append(
            f"- {len(queued)} prompt(s) queued: "
            + "; ".join(f"{n.id} {n.title}" for n in queued)
        )
    work = [n for n in root.children if n.id != MAIN_ID]
    if work:
        lines.append("")
        lines.append("### Work")
        for node in work:
            glyph = STATUS_GLYPH.get(node.status, "·")
            extra = f" — {node.detail}" if node.detail else ""
            lines.append(f"- {glyph} {node.id} {node.title} [{node.status}]{extra}")
            for child in node.children:
                if child.kind in ("team-task", "daemon-run") and child.status in (
                    "running",
                    "failed",
                    "queued",
                ):
                    lines.append(
                        f"    - {STATUS_GLYPH.get(child.status, '·')} {child.title} [{child.status}] {child.detail}"
                    )
    needs_you = []
    if main is not None and main.status == "waiting":
        needs_you.append(main.detail)
    needs_you.extend(
        f"{n.id}: {n.detail or n.title}"
        for n in root.walk()
        if n.status == "failed" and n.id != ROOT_ID
    )
    if needs_you:
        lines.append("")
        lines.append("### Needs you")
        lines.extend(f"- {item}" for item in needs_you)
    if notes:
        lines.append("")
        lines.append("### Notes (/btw)")
        for record in list(notes)[-8:]:
            lines.append(f"- {_clip(getattr(record, 'content', str(record)), 120)}")
    return "\n".join(lines)


async def run_recap_command(app: Any, command: str) -> None:  # noqa: ANN401 - the App
    """Body of `/recap`."""
    del command
    from bog_agents_cli.widgets.messages import AppMessage

    root = await build_task_tree(app, include_daemon=False)
    notes: list[Any] = []
    try:
        from bog_agents_cli.config import settings
        from bog_agents_cli.sidechain import SidechainStore

        getter = getattr(app, "_current_thread_id", None)
        thread_id = getter() if callable(getter) else None
        store = SidechainStore(Path(settings.user_agents_dir))
        notes = [
            r
            for r in store.load(thread_id or "interactive")
            if getattr(r, "is_note", False)
        ]
    except Exception:  # notes are optional
        notes = []
    await app._mount_message(AppMessage(build_recap(app, root, notes=notes)))
