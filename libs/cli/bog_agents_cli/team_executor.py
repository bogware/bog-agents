"""CLI wiring for governed agent teams (#21) — run a real team over a ledger.

The SDK's `bog_agents.teams` owns the coordination substrate (atomic claimable
`TaskLedger`, `Mailbox`, the `run_team` coordinator loop under cost caps). This
module supplies the CLI execution layer: turn a task list into a ledger, wire a
real teammate runner (a non-interactive, auto-approving `create_cli_agent` that
works one task), and run the team under a `CostLedger`'s spawn/spend caps —
mirroring the #31 best-of-N wiring.

The teammate runner is injectable so `run_team_session`'s orchestration
(ledger construction, member fan-out, cap enforcement, reporting) unit-tests
without real models; the CLI wires the real `create_cli_agent`.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bog_agents.cost_ledger import CostLedger, RunawayCaps
from bog_agents.teams import (
    LedgerTask,
    Mailbox,
    TaskLedger,
    TaskResult,
    TeammateRunner,
    TeamReport,
    run_team,
)

logger = logging.getLogger(__name__)

# A task spec is either a bare title or a (title, [dependency-title, ...]) pair.
TaskSpec = str | tuple[str, Sequence[str]]

_MEMBERS_RE = re.compile(r"--members(?:=|\s+)(\S+)")


@dataclass
class TeamRunRequest:
    """Parsed `/team run` invocation.

    Attributes:
        task_specs: Work items in ledger form (titles, or ``(title, [deps])``
            when ``--chain`` wired a linear pipeline).
        members: Roster override (comma-separated ``--members``); empty means
            fall back to the configured team roster.
    """

    task_specs: list[TaskSpec] = field(default_factory=list)
    members: list[str] = field(default_factory=list)


def parse_team_run_args(raw: str) -> TeamRunRequest:
    """Parse `/team run` arguments into a `TeamRunRequest`.

    Syntax: ``[--members a,b] [--chain] <task1> | <task2> | ...``

    - ``--members`` overrides the configured roster (comma-separated).
    - ``--chain`` makes each task depend on the previous one, turning the list
      into a linear pipeline (task N is not claimable until task N-1 is done);
      without it every task is independent and runs as soon as a member frees.
    - Tasks are separated by ``|``; surrounding whitespace and blank tasks are
      dropped.

    Args:
        raw: The argument string after ``/team run``.

    Returns:
        The parsed request (empty ``task_specs`` if nothing parseable remains).
    """
    members: list[str] = []
    match = _MEMBERS_RE.search(raw)
    if match:
        members = [m.strip() for m in match.group(1).split(",") if m.strip()]
        raw = raw[: match.start()] + raw[match.end() :]

    chain = "--chain" in raw
    raw = raw.replace("--chain", "")

    titles = [t.strip() for t in raw.split("|") if t.strip()]
    if chain:
        specs: list[TaskSpec] = []
        for i, title in enumerate(titles):
            specs.append(title if i == 0 else (title, [titles[i - 1]]))
    else:
        specs = list(titles)
    return TeamRunRequest(task_specs=specs, members=members)


def _final_ai_text(result: Any) -> str:  # noqa: ANN401 - langgraph result mapping
    """Extract the last AI message text from an agent `ainvoke` result."""
    messages = result.get("messages", []) if isinstance(result, dict) else []
    for msg in reversed(messages):
        content = getattr(msg, "content", None)
        if content and getattr(msg, "type", None) == "ai":
            return content if isinstance(content, str) else str(content)
    return ""


def build_ledger(task_specs: Sequence[TaskSpec]) -> TaskLedger:
    """Build a `TaskLedger` from task specs, wiring dependencies by title.

    A spec is a title, or ``(title, [dep_title, ...])``. Dependencies are
    resolved to task ids so the coordinator only lets a task be claimed once its
    prerequisites are done.
    """
    ledger = TaskLedger()
    ids_by_title: dict[str, str] = {}
    normalized: list[tuple[str, list[str]]] = []
    for spec in task_specs:
        title, deps = (spec, []) if isinstance(spec, str) else (spec[0], list(spec[1]))
        normalized.append((title, deps))
        ids_by_title[title] = ""
    # First pass: create tasks (no deps yet) so every title has an id.
    for title, _deps in normalized:
        ids_by_title[title] = ledger.add(title).id
    # Second pass: wire dependency ids.
    for title, deps in normalized:
        task = ledger.get(ids_by_title[title])
        if task is not None:
            task.depends_on = [ids_by_title[d] for d in deps if d in ids_by_title]
    return ledger


def _team_file_tools(mailbox: Any, member: str, repo_dir: Path) -> list[Any]:  # noqa: ANN401 - Mailbox or MailboxStore
    """ROADMAP #76: `send_file` / `send_patch` / `receive_files` bound to this teammate, audit-logged when the action log is on."""
    try:
        from bog_agents.tools.team_files import team_file_tools

        from bog_agents_cli.action_log_controller import approvals_log

        log = approvals_log()
        audit = (
            (lambda kind, data: log.append(kind, **data)) if log is not None else None
        )
        return list(team_file_tools(mailbox, member, root=repo_dir, audit=audit))
    except Exception:
        logger.debug("team file tools unavailable", exc_info=True)
        return []


def build_worktree_teammate_runner(
    *,
    repo_dir: Path,
    resolve_model: Any,  # noqa: ANN401 - spec -> chat model
    model_spec: str,
    agent_factory: Any = None,  # noqa: ANN401 - defaults to create_cli_agent
) -> TeammateRunner:
    """Build a real teammate runner: one non-interactive agent per task.

    Teammates share the repository working directory, coordinated by the ledger
    (a task isn't claimable until its dependencies complete, so a dependent task
    sees its prerequisites' file changes). The agent runs auto-approving with
    checkpointing off so parallel teammates don't collide on shared state.

    Args:
        repo_dir: The repository the team works in.
        resolve_model: Resolves a model spec to a langchain chat model.
        model_spec: The model every teammate runs.
        agent_factory: Override for `create_cli_agent` (injected in tests).

    Returns:
        An async `teammate_runner(member, task, mailbox) -> TaskResult`.
    """
    if agent_factory is None:
        from bog_agents_cli.agent import create_cli_agent

        agent_factory = create_cli_agent

    async def _runner(member: str, task: LedgerTask, mailbox: Mailbox) -> TaskResult:
        model = resolve_model(model_spec)
        agent, _backend = agent_factory(
            model,
            assistant_id=f"team-{member}-{task.id}",
            cwd=repo_dir,
            interactive=False,
            auto_approve=True,
            enable_checkpointing=False,
            enable_memory=False,
            enable_plan_mode=False,
            extra_tools=_team_file_tools(mailbox, member, repo_dir),
        )
        # Fold in any peer messages addressed to this member so it has context.
        inbox = mailbox.drain(member)
        notes = "\n".join(f"[{m.sender}] {m.body}" for m in inbox)
        prompt = (
            task.title
            if not task.description
            else f"{task.title}\n\n{task.description}"
        )
        if notes:
            prompt = f"{prompt}\n\nTeam notes:\n{notes}"
        result = await agent.ainvoke({"messages": [("human", prompt)]})
        return TaskResult(success=True, result=_final_ai_text(result))

    return _runner


async def run_team_session(
    task_specs: Sequence[TaskSpec],
    members: Sequence[str],
    *,
    repo_dir: Path,
    resolve_model: Any = None,  # noqa: ANN401
    model_spec: str = "",
    caps: RunawayCaps | None = None,
    teammate_runner: Any = None,  # noqa: ANN401 - injected in tests
    agent_factory: Any = None,  # noqa: ANN401
    ledger: TaskLedger | None = None,
    mailbox: Mailbox | None = None,
    cost_ledger: CostLedger | None = None,
    pause_gate: asyncio.Event | None = None,
) -> TeamReport:
    """Run a governed team over `task_specs` and return the report.

    Builds the ledger, wires a real (or injected) teammate runner, and runs the
    coordinator under a `CostLedger`'s caps. Teammates coordinate through a
    shared `Mailbox`.

    Args:
        task_specs: The work items (titles, optionally with dependency titles).
        members: Teammate names (each can hold one task per round).
        repo_dir: Repository the team works in.
        resolve_model: Spec -> chat model (for the real runner).
        model_spec: Model every teammate runs (for the real runner).
        caps: Runaway caps (spawns / searches / spend); uncapped when None.
        teammate_runner: Injected runner (tests); real one built when None.
        agent_factory: Injected `create_cli_agent` (tests).
        ledger: Pre-built ledger (ROADMAP #68: the `/tasks` tree watches it); built from
            `task_specs` when `None`.
        mailbox: Shared mailbox to use (so `/tasks steer` can reach teammates).
        cost_ledger: Cost ledger to charge (so `/tasks` can show spend); a fresh one when `None`.
        pause_gate: When given, every task claim waits on this event first —
            `/tasks pause` clears it, `/tasks resume` sets it.

    Returns:
        The `TeamReport` with the final board and stop reason.
    """
    ledger = ledger if ledger is not None else build_ledger(task_specs)
    runner = teammate_runner or build_worktree_teammate_runner(
        repo_dir=repo_dir,
        resolve_model=resolve_model,
        model_spec=model_spec,
        agent_factory=agent_factory,
    )
    cost_ledger = (
        cost_ledger
        if cost_ledger is not None
        else CostLedger(caps=caps or RunawayCaps())
    )
    if pause_gate is not None:
        inner_runner = runner

        async def _gated(member: str, task: LedgerTask, box: Mailbox) -> TaskResult:
            await pause_gate.wait()
            return await inner_runner(member, task, box)

        runner = _gated
    return await run_team(
        ledger,
        members,
        teammate_runner=runner,
        cost_ledger=cost_ledger,
        mailbox=mailbox if mailbox is not None else Mailbox(),
    )
