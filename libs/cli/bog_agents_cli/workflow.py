"""Agent-authored workflows (ROADMAP #73): a YAML schema, a persisted runner, `/name` commands.

A workflow is a short list of phases — `context`, `work`, `review`, `verify`,
`synthesize` — each a fan-out of tasks run as a governed team (`bog_agents.teams`:
dependency-aware claims, `RunawayCaps` spawn / spend caps). Files live in
`.bog-agents/workflows/<name>.yaml` and load as `/<name> [args]` beside the
`.prompt.md` commands. Runs persist phase and per-task state (tokens, cost,
seconds) under `.bog-agents/workflows/runs/`, so a paused (budget) or failed
run resumes at the first unfinished phase instead of starting over. The model
side is injected (`author_workflow(invoke=…)`, `run_workflow(runner=…)`), so
everything here unit-tests without a live model.
"""

from __future__ import annotations

import json
import re
import shlex
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bog_agents.cost_ledger import CostLedger, RunawayCaps
from bog_agents.teams import DONE, LedgerTask, Mailbox, TaskLedger, TaskResult, run_team

if TYPE_CHECKING:
    from collections.abc import Sequence

PHASE_KINDS: tuple[str, ...] = ("context", "work", "review", "verify", "synthesize")
GATED_KINDS: frozenset[str] = frozenset({"review", "verify"})
RUN_STATES: tuple[str, ...] = ("pending", "running", "done", "failed", "paused")
WORKFLOWS_RELATIVE = Path(".bog-agents") / "workflows"
CONTEXT_CHARS_PER_TASK = 4000
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}$")
_PASS_RE = re.compile(r"\b(VERDICT:\s*PASS|PASS(?:ED)?)\b", re.IGNORECASE)
_FAIL_RE = re.compile(r"\b(VERDICT:\s*FAIL|FAIL(?:ED)?)\b", re.IGNORECASE)


# --------------------------------------------------------------------------- schema
@dataclass(frozen=True)
class WorkflowTask:
    """One unit of work inside a phase."""

    id: str
    title: str
    prompt: str = ""
    depends_on: tuple[str, ...] = ()


@dataclass(frozen=True)
class WorkflowPhase:
    """A phase: tasks fanned out over `workers` teammates; gated phases must all PASS."""

    name: str
    kind: str
    tasks: tuple[WorkflowTask, ...]
    workers: int = 1
    gate: bool = False


@dataclass(frozen=True)
class Workflow:
    """A parsed workflow file."""

    name: str
    description: str
    phases: tuple[WorkflowPhase, ...]
    args: tuple[str, ...] = ()
    budget_usd: float | None = None
    max_agents: int | None = None
    source: str = ""

    @property
    def task_count(self) -> int:
        """Total tasks across phases."""
        return sum(len(p.tasks) for p in self.phases)

    def usage(self) -> str:
        """`/name <arg> ...` for help and autocomplete."""
        return f"/{self.name}" + "".join(f" <{a}>" for a in self.args)


def parse_workflow(text: str, *, source: str = "") -> Workflow:
    """Parse and validate workflow YAML.

    Raises:
        ValueError: On a malformed document (bad name, unknown phase kind,
            duplicate or dangling task ids, empty phases).
    """
    import yaml

    try:
        data = yaml.safe_load(text)
    except Exception as exc:
        msg = f"workflow YAML does not parse: {exc}"
        raise ValueError(msg) from exc
    if not isinstance(data, dict):
        msg = "workflow must be a YAML mapping"
        raise ValueError(msg)  # noqa: TRY004
    name = str(data.get("name", "")).strip().lower()
    if not _NAME_RE.match(name):
        msg = f"workflow name {name!r} must be a slug (lower-case letters, digits, - or _)"
        raise ValueError(msg)
    raw_phases = data.get("phases")
    if not isinstance(raw_phases, list) or not raw_phases:
        msg = "workflow needs a non-empty `phases` list"
        raise ValueError(msg)
    seen: set[str] = set()
    phases: list[WorkflowPhase] = []
    for index, raw in enumerate(raw_phases):
        if not isinstance(raw, dict):
            msg = f"phase {index} must be a mapping"
            raise ValueError(msg)  # noqa: TRY004
        kind = str(raw.get("kind", "work")).strip().lower()
        if kind not in PHASE_KINDS:
            msg = f"phase {index} kind {kind!r} must be one of {', '.join(PHASE_KINDS)}"
            raise ValueError(msg)
        pname = str(raw.get("name", kind)).strip() or kind
        raw_tasks = raw.get("tasks")
        if isinstance(raw_tasks, str):
            raw_tasks = [{"title": raw_tasks}]
        if not isinstance(raw_tasks, list) or not raw_tasks:
            msg = f"phase {pname!r} needs a non-empty `tasks` list"
            raise ValueError(msg)
        tasks: list[WorkflowTask] = []
        for tindex, rt in enumerate(raw_tasks):
            if isinstance(rt, str):
                rt = {"title": rt}  # noqa: PLW2901 - normalise the short form
            if not isinstance(rt, dict):
                msg = f"task {tindex} in phase {pname!r} must be a mapping or a string"
                raise ValueError(msg)  # noqa: TRY004
            title = str(rt.get("title", "")).strip()
            if not title:
                msg = f"task {tindex} in phase {pname!r} has no title"
                raise ValueError(msg)
            tid = str(rt.get("id", "") or f"{pname}-{tindex + 1}").strip()
            if tid in seen:
                msg = f"duplicate task id {tid!r}"
                raise ValueError(msg)
            seen.add(tid)
            deps = rt.get("depends_on", []) or []
            if isinstance(deps, str):
                deps = [deps]
            tasks.append(
                WorkflowTask(
                    id=tid,
                    title=title,
                    prompt=str(rt.get("prompt", "") or ""),
                    depends_on=tuple(str(d) for d in deps),
                )
            )
        workers_raw = raw.get("workers", 1)
        workers = int(1 if workers_raw is None else workers_raw)
        if workers < 1:
            msg = f"phase {pname!r} workers must be >= 1"
            raise ValueError(msg)
        gate = bool(raw.get("gate", kind in GATED_KINDS))
        phases.append(
            WorkflowPhase(
                name=pname, kind=kind, tasks=tuple(tasks), workers=workers, gate=gate
            )
        )
    for phase in phases:
        for task in phase.tasks:
            for dep in task.depends_on:
                if dep not in seen:
                    msg = f"task {task.id!r} depends on unknown task {dep!r}"
                    raise ValueError(msg)
    args = data.get("args", []) or []
    if isinstance(args, str):
        args = [args]
    budget = data.get("budget_usd")
    max_agents = data.get("max_agents")
    return Workflow(
        name=name,
        description=str(data.get("description", "") or "").strip(),
        phases=tuple(phases),
        args=tuple(str(a) for a in args),
        budget_usd=float(budget) if budget is not None else None,
        max_agents=int(max_agents) if max_agents is not None else None,
        source=source,
    )


def render_workflow_yaml(workflow: Workflow) -> str:
    """Canonical YAML for a workflow (what `author_workflow` writes)."""
    import yaml

    doc: dict[str, Any] = {"name": workflow.name, "description": workflow.description}
    if workflow.args:
        doc["args"] = list(workflow.args)
    if workflow.budget_usd is not None:
        doc["budget_usd"] = workflow.budget_usd
    if workflow.max_agents is not None:
        doc["max_agents"] = workflow.max_agents
    doc["phases"] = [
        {
            "name": p.name,
            "kind": p.kind,
            "workers": p.workers,
            "gate": p.gate,
            "tasks": [
                {
                    "id": t.id,
                    "title": t.title,
                    **({"prompt": t.prompt} if t.prompt else {}),
                    **({"depends_on": list(t.depends_on)} if t.depends_on else {}),
                }
                for t in p.tasks
            ],
        }
        for p in workflow.phases
    ]
    return yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=100)


# --------------------------------------------------------------------------- files
def workflows_dir(project_root: str | Path) -> Path:
    """`<root>/.bog-agents/workflows`."""
    return Path(project_root) / WORKFLOWS_RELATIVE


def workflow_path(project_root: str | Path, name: str) -> Path:
    """Where `name` is stored."""
    return workflows_dir(project_root) / f"{name}.yaml"


def discover_workflows(project_root: str | Path) -> dict[str, Workflow]:
    """All parseable workflows in the project (name → workflow); unparseable files are skipped."""
    found: dict[str, Workflow] = {}
    directory = workflows_dir(project_root)
    if not directory.is_dir():
        return found
    for path in sorted(directory.glob("*.yaml")):
        try:
            workflow = parse_workflow(
                path.read_text(encoding="utf-8"), source=str(path)
            )
        except (ValueError, OSError):
            continue
        found[workflow.name] = workflow
    return found


def save_workflow(project_root: str | Path, workflow: Workflow) -> Path:
    """Write `workflow` to its file, returning the path."""
    path = workflow_path(project_root, workflow.name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_workflow_yaml(workflow), encoding="utf-8")
    return path


def bind_args(workflow: Workflow, raw_args: str) -> dict[str, str]:
    """Map `/name a b c` positional args onto the workflow's declared names (extras join the last one).

    Raises:
        ValueError: When fewer args than declared were given.
    """
    try:
        given = shlex.split(raw_args)
    except ValueError:
        given = raw_args.split()
    names = list(workflow.args)
    if len(given) < len(names):
        missing = ", ".join(names[len(given) :])
        msg = f"{workflow.usage()} — missing {missing}"
        raise ValueError(msg)
    bound = {name: given[i] for i, name in enumerate(names)}
    if names and len(given) > len(names):
        bound[names[-1]] = " ".join(given[len(names) - 1 :])
    bound["args"] = raw_args.strip()
    return bound


# --------------------------------------------------------------------------- authoring
AUTHOR_SCHEMA = """A bog-agents workflow is YAML with:
  name: slug                    # becomes the /name slash command
  description: one line
  args: [ticket]                # optional positional args, usable as {ticket} in prompts
  budget_usd: 5                 # optional hard spend cap for one run (the run pauses at the cap)
  max_agents: 12                # optional cap on teammate activations
  phases:                       # in order; kinds: context, work, review, verify, synthesize
    - name: research
      kind: context
      workers: 2                # fan-out width for this phase
      tasks:
        - id: map
          title: Map the modules involved in {ticket}
          prompt: optional longer instructions; {context} inserts earlier phases' results
    - name: implement
      kind: work
      tasks:
        - id: change
          title: Make the change
          depends_on: [map]
    - name: check
      kind: verify              # review/verify phases are gates: every task must end with VERDICT: PASS
      tasks:
        - title: Run the tests and report VERDICT: PASS or VERDICT: FAIL with reasons
    - name: summary
      kind: synthesize
      tasks:
        - title: Summarise what changed and how it was verified
Rules: task ids unique across phases; depends_on only names earlier or same-phase tasks;
keep prompts concrete; three to five phases; gated phases small."""


def _strip_fences(text: str) -> str:
    match = re.search(r"```(?:ya?ml)?\s*\n(.*?)```", text, re.DOTALL)
    return (match.group(1) if match else text).strip()


def author_workflow(
    description: str,
    *,
    invoke: Callable[[str], str],
    project_root: str | Path,
    name: str | None = None,
    retries: int = 1,
) -> tuple[Path, Workflow]:
    """Ask a model (injected `invoke`) to write a workflow for `description`, validate it, save it.

    Raises:
        ValueError: When the model's YAML is still invalid after `retries` corrections.
    """
    request = (
        "Write a bog-agents workflow for this job. Reply with YAML only, no prose.\n\n"
        f"{AUTHOR_SCHEMA}\n\nJob: {description.strip()}\n"
        + (f"Use the name: {name}\n" if name else "")
    )
    feedback = ""
    last_error = ""
    for _attempt in range(retries + 1):
        raw = invoke(request + feedback)
        try:
            workflow = parse_workflow(_strip_fences(raw))
        except ValueError as exc:
            last_error = str(exc)
            feedback = f"\n\nYour previous YAML was rejected: {last_error}. Fix it and reply with YAML only."
            continue
        if name and workflow.name != name:
            workflow = Workflow(
                **{**asdict(workflow), "name": name, "phases": workflow.phases}
            )
        return save_workflow(project_root, workflow), workflow
    msg = f"could not author a valid workflow: {last_error}"
    raise ValueError(msg)


# --------------------------------------------------------------------------- run state
@dataclass
class TaskMeter:
    """Per-task outcome and meter."""

    task_id: str
    status: str = "pending"
    output: str = ""
    error: str = ""
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    seconds: float = 0.0
    passed: bool | None = None


@dataclass
class PhaseRecord:
    """One phase's state within a run."""

    name: str
    kind: str
    status: str = "pending"
    tasks: dict[str, TaskMeter] = field(default_factory=dict)
    stop_reason: str = ""

    @property
    def done(self) -> bool:
        """Every task done and the phase closed."""
        return self.status == "done"

    def cost_usd(self) -> float:
        """Spend in this phase."""
        return sum(m.cost_usd for m in self.tasks.values())


@dataclass
class WorkflowRun:
    """Persisted state of one run."""

    workflow: str
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    args: dict[str, str] = field(default_factory=dict)
    phases: list[PhaseRecord] = field(default_factory=list)
    status: str = "pending"
    stop_reason: str = ""
    result: str = ""
    activations: int = 0
    started_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    @property
    def spent_usd(self) -> float:
        """Total spend so far."""
        return sum(p.cost_usd() for p in self.phases)

    @property
    def tokens(self) -> tuple[int, int]:
        """`(input, output)` tokens so far."""
        ins = sum(m.input_tokens for p in self.phases for m in p.tasks.values())
        outs = sum(m.output_tokens for p in self.phases for m in p.tasks.values())
        return ins, outs

    def resume_index(self) -> int:
        """Index of the first phase that is not done (== len when finished)."""
        for index, phase in enumerate(self.phases):
            if not phase.done:
                return index
        return len(self.phases)

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready mapping."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WorkflowRun:
        """Inverse of `to_dict`."""
        phases = [
            PhaseRecord(
                name=str(p.get("name", "")),
                kind=str(p.get("kind", "work")),
                status=str(p.get("status", "pending")),
                tasks={k: TaskMeter(**v) for k, v in (p.get("tasks") or {}).items()},
                stop_reason=str(p.get("stop_reason", "")),
            )
            for p in data.get("phases", [])
        ]
        return cls(
            workflow=str(data.get("workflow", "")),
            run_id=str(data.get("run_id", "")),
            args=dict(data.get("args") or {}),
            phases=phases,
            status=str(data.get("status", "pending")),
            stop_reason=str(data.get("stop_reason", "")),
            result=str(data.get("result", "")),
            activations=int(data.get("activations", 0) or 0),
            started_at=float(data.get("started_at", 0.0) or 0.0),
            updated_at=float(data.get("updated_at", 0.0) or 0.0),
        )

    def save(self, path: Path) -> Path:
        """Write the run as JSON."""
        self.updated_at = time.time()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: Path) -> WorkflowRun:
        """Read a run back."""
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def format_summary(self) -> str:
        """Phase table with meters for `/workflow status`."""
        ins, outs = self.tokens
        lines = [
            f"Workflow {self.workflow} run {self.run_id}: {self.status}"
            + (f" ({self.stop_reason})" if self.stop_reason else "")
        ]
        for phase in self.phases:
            counts = {}
            for meter in phase.tasks.values():
                counts[meter.status] = counts.get(meter.status, 0) + 1
            detail = (
                ", ".join(f"{n} {s}" for s, n in sorted(counts.items())) or "no tasks"
            )
            lines.append(
                f"  {phase.status:<8} {phase.kind:<11} {phase.name} — {detail}, ${phase.cost_usd():.4f}"
            )
        lines.append(
            f"  spend ${self.spent_usd:.4f}, tokens {ins} in / {outs} out, {self.activations} activations"
        )
        return "\n".join(lines)


def runs_dir(project_root: str | Path) -> Path:
    """`<root>/.bog-agents/workflows/runs`."""
    return workflows_dir(project_root) / "runs"


def run_path(project_root: str | Path, run: WorkflowRun) -> Path:
    """Where a run's JSON lives."""
    return runs_dir(project_root) / f"{run.workflow}-{run.run_id}.json"


def latest_run(project_root: str | Path, name: str) -> WorkflowRun | None:
    """The most recently updated run of `name`, if any."""
    directory = runs_dir(project_root)
    if not directory.is_dir():
        return None
    candidates = sorted(
        directory.glob(f"{name}-*.json"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    for path in candidates:
        try:
            return WorkflowRun.load(path)
        except (OSError, ValueError, TypeError):
            continue
    return None


# --------------------------------------------------------------------------- running
@dataclass
class TaskOutcome:
    """What a task runner returns."""

    success: bool
    output: str = ""
    error: str = ""
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    passed: bool | None = None


WorkflowTaskRunner = Callable[[WorkflowTask, str, PhaseRecord], Awaitable[TaskOutcome]]
"""`(task, rendered prompt, phase record) -> outcome`; the model side, injected."""


def render_task_prompt(
    task: WorkflowTask, *, args: dict[str, str], context: str, phase: WorkflowPhase
) -> str:
    """The prompt a teammate receives: title, prompt, bound args, prior-phase context, gate instructions."""
    body = task.title if not task.prompt else f"{task.title}\n\n{task.prompt}"
    values = {**args, "context": context}
    try:
        body = body.format_map(_Defaulting(values))
    except (ValueError, IndexError):
        pass
    if "{context}" not in (task.prompt or "") and context:  # noqa: RUF027 - a placeholder, not an f-string
        body += f"\n\nContext from earlier phases:\n{context}"
    if phase.gate:
        body += "\n\nEnd your answer with exactly one line: `VERDICT: PASS` or `VERDICT: FAIL` followed by the reasons."
    return body


class _Defaulting(dict):  # noqa: FURB189 - format_map needs a real dict subclass
    """`str.format_map` helper that leaves unknown placeholders in place."""

    def __missing__(self, key: str) -> str:
        return "".join(("{", key, "}"))


def context_for(run: WorkflowRun, upto: int) -> str:
    """Concatenate prior phases' outputs (bounded per task)."""
    parts: list[str] = []
    for phase in run.phases[:upto]:
        for tid, meter in phase.tasks.items():
            if meter.status == "done" and meter.output:
                parts.append(
                    f"### {phase.name} / {tid}\n{meter.output[:CONTEXT_CHARS_PER_TASK]}"
                )
    return "\n\n".join(parts)


def _verdict(outcome: TaskOutcome) -> bool:
    if outcome.passed is not None:
        return outcome.passed
    text = outcome.output[-400:]
    if _FAIL_RE.search(text) and not re.search(r"VERDICT:\s*PASS", text, re.IGNORECASE):
        return False
    return bool(_PASS_RE.search(text))


async def run_workflow(
    workflow: Workflow,
    *,
    runner: WorkflowTaskRunner,
    args: dict[str, str] | None = None,
    run: WorkflowRun | None = None,
    caps: RunawayCaps | None = None,
    cost_ledger: CostLedger | None = None,
    persist: Callable[[WorkflowRun], None] | None = None,
    on_event: Callable[[str], None] | None = None,
    budget_usd: float | None = None,
) -> WorkflowRun:
    """Run (or resume) `workflow`; returns the run with status done / failed / paused.

    Phases already `done` on a resumed run are skipped; a paused phase reruns
    only its unfinished tasks. Spend is checked against `workflow.budget_usd`
    after every task — reaching it pauses the run (resumable). A gated phase
    with any failing verdict fails the run at that phase.
    """
    args = dict(args or {})
    if run is None:
        run = WorkflowRun(
            workflow=workflow.name,
            args=args,
            phases=[PhaseRecord(name=p.name, kind=p.kind) for p in workflow.phases],
        )
    elif len(run.phases) != len(workflow.phases):
        run.phases = [PhaseRecord(name=p.name, kind=p.kind) for p in workflow.phases]
    budget = budget_usd if budget_usd is not None else workflow.budget_usd
    caps = caps or RunawayCaps(max_subagents=workflow.max_agents, max_cost_usd=budget)
    ledger_cost = cost_ledger if cost_ledger is not None else CostLedger(caps=caps)
    emit = on_event or (lambda _m: None)
    save = persist or (lambda _r: None)
    if budget is not None and run.spent_usd >= budget:
        run.status = "paused"
        run.stop_reason = f"budget ${budget:.2f} already spent (${run.spent_usd:.2f}); resume with a higher budget"
        save(run)
        return run
    run.status = "running"
    run.stop_reason = ""
    save(run)

    for index in range(run.resume_index(), len(workflow.phases)):
        phase, record = workflow.phases[index], run.phases[index]
        record.status = "running"
        record.stop_reason = ""
        for task in phase.tasks:
            record.tasks.setdefault(task.id, TaskMeter(task_id=task.id))
        emit(
            f"phase {phase.name} ({phase.kind}): {len(phase.tasks)} task(s) over {phase.workers} worker(s)"
        )
        save(run)
        context = context_for(run, index)
        pending = [t for t in phase.tasks if record.tasks[t.id].status != "done"]
        finished = {t.id for t in phase.tasks if record.tasks[t.id].status == "done"}
        board = TaskLedger()
        by_id = {t.id: t for t in phase.tasks}
        for task in pending:
            deps = [d for d in task.depends_on if d in by_id and d not in finished]
            board.add(
                task.title, description=task.prompt, depends_on=deps, task_id=task.id
            )
        budget_hit = False

        async def _teammate(
            _member: str,
            ledger_task: LedgerTask,
            _mailbox: Mailbox,
            *,
            _task_map: dict[str, WorkflowTask] = by_id,
            _phase: WorkflowPhase = phase,
            _record: PhaseRecord = record,
            _context: str = context,
        ) -> TaskResult:
            nonlocal budget_hit
            task = _task_map[ledger_task.id]
            meter = _record.tasks[task.id]
            meter.status = "running"
            started = time.monotonic()
            try:
                outcome = await runner(
                    task,
                    render_task_prompt(task, args=args, context=_context, phase=_phase),
                    _record,
                )
            except Exception as exc:  # a crashing task fails its task, never the loop
                outcome = TaskOutcome(success=False, error=str(exc))
            meter.seconds += time.monotonic() - started
            meter.cost_usd += outcome.cost_usd
            meter.input_tokens += outcome.input_tokens
            meter.output_tokens += outcome.output_tokens
            meter.output = outcome.output
            meter.error = outcome.error
            meter.passed = (
                _verdict(outcome) if _phase.gate and outcome.success else None
            )
            meter.status = "done" if outcome.success else "failed"
            if budget is not None and run.spent_usd >= budget:
                budget_hit = True
            save(run)
            return TaskResult(
                success=outcome.success, result=outcome.output, error=outcome.error
            )

        report = await run_team(
            board,
            [f"{phase.name}-{i + 1}" for i in range(phase.workers)],
            teammate_runner=_teammate,
            cost_ledger=ledger_cost,
            max_activations=workflow.max_agents or 1000,
        )
        run.activations += report.activations
        all_done = all(record.tasks[t.id].status == "done" for t in phase.tasks)
        if budget_hit and not all_done:
            record.status = "paused"
            record.stop_reason = f"budget ${budget:.2f} reached"
            run.status, run.stop_reason = "paused", record.stop_reason
            emit(f"phase {phase.name} paused: {record.stop_reason}")
            save(run)
            return run
        if not all_done:
            failed = [t.id for t in phase.tasks if record.tasks[t.id].status != "done"]
            record.status = (
                "failed"
                if report.stopped_reason == "completed"
                or any(record.tasks[t].status == "failed" for t in failed)
                else "paused"
            )
            record.stop_reason = (
                f"{report.stopped_reason}; unfinished: {', '.join(failed)}"
            )
            run.status, run.stop_reason = record.status, record.stop_reason
            emit(f"phase {phase.name} {record.status}: {record.stop_reason}")
            save(run)
            return run
        if phase.gate:
            failing = [tid for tid, m in record.tasks.items() if m.passed is False]
            if failing:
                record.status = "failed"
                record.stop_reason = f"gate failed: {', '.join(failing)}"
                run.status, run.stop_reason = (
                    "failed",
                    f"{phase.name}: {record.stop_reason}",
                )
                emit(f"phase {phase.name} gate failed ({', '.join(failing)})")
                save(run)
                return run
        record.status = "done"
        if phase.kind == "synthesize":
            run.result = "\n\n".join(
                m.output for m in record.tasks.values() if m.output
            )
        emit(f"phase {phase.name} done (${record.cost_usd():.4f})")
        save(run)
        if budget_hit and index + 1 < len(workflow.phases):
            run.status, run.stop_reason = (
                "paused",
                f"budget ${budget:.2f} reached after {phase.name}",
            )
            emit(run.stop_reason)
            save(run)
            return run

    run.status = "done"
    run.stop_reason = "completed"
    if not run.result:
        last = run.phases[-1] if run.phases else None
        run.result = (
            "\n\n".join(m.output for m in last.tasks.values() if m.output)
            if last
            else ""
        )
    save(run)
    return run


def describe_workflows(workflows: Sequence[Workflow]) -> str:
    """One line per workflow for `/workflow list`."""
    if not workflows:
        return "No workflows yet — `/workflow author <what it should do>` writes one to .bog-agents/workflows/."
    lines = [f"{'COMMAND':<28} PHASES  TASKS  DESCRIPTION"]
    lines.extend(
        f"{wf.usage():<28} {len(wf.phases):<7} {wf.task_count:<6} {wf.description[:70]}"
        for wf in workflows
    )
    return "\n".join(lines)


def describe_workflow(workflow: Workflow) -> str:
    """The phase tree for `/workflow show`."""
    lines = [f"{workflow.usage()} — {workflow.description}"]
    if workflow.budget_usd is not None or workflow.max_agents is not None:
        lines.append(
            f"  budget ${workflow.budget_usd if workflow.budget_usd is not None else '∞'}, max agents {workflow.max_agents or '∞'}"
        )
    for phase in workflow.phases:
        gate = " [gate]" if phase.gate else ""
        lines.append(
            f"  {phase.kind:<11} {phase.name} x {phase.workers} worker(s){gate}"
        )
        for task in phase.tasks:
            deps = f"  (after {', '.join(task.depends_on)})" if task.depends_on else ""
            lines.append(f"    - {task.id}: {task.title}{deps}")
    if workflow.source:
        lines.append(f"  file {workflow.source}")
    return "\n".join(lines)


__all__ = [
    "AUTHOR_SCHEMA",
    "DONE",
    "PHASE_KINDS",
    "PhaseRecord",
    "TaskMeter",
    "TaskOutcome",
    "Workflow",
    "WorkflowPhase",
    "WorkflowRun",
    "WorkflowTask",
    "WorkflowTaskRunner",
    "author_workflow",
    "bind_args",
    "context_for",
    "describe_workflow",
    "describe_workflows",
    "discover_workflows",
    "latest_run",
    "parse_workflow",
    "render_task_prompt",
    "render_workflow_yaml",
    "run_path",
    "run_workflow",
    "runs_dir",
    "save_workflow",
    "workflow_path",
    "workflows_dir",
]
