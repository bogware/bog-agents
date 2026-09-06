"""`/workflow` and `/<name>` dispatch (ROADMAP #73): list, show, author, run, resume, status.

The pure parts live in `workflow.py`; this module binds them to the App: a real
task runner (one non-interactive, auto-approving agent per task, tokens read
from `usage_metadata`, cost from the SDK price catalog), run persistence under
`.bog-agents/workflows/runs/`, and the `/name [args]` entry point that
`prompt_commands` discovery registers for every workflow file.
"""

from __future__ import annotations

import logging
import shlex
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bog_agents_cli.workflow import (
    TaskOutcome,
    Workflow,
    WorkflowRun,
    WorkflowTaskRunner,
    author_workflow,
    bind_args,
    describe_workflow,
    describe_workflows,
    discover_workflows,
    latest_run,
    run_path,
    run_workflow,
)

if TYPE_CHECKING:
    from bog_agents_cli.prompt_commands import PromptCommand

logger = logging.getLogger(__name__)

USAGE = (
    "Usage: /workflow list | /workflow show <name> | /workflow run <name> [args] | "
    "/workflow resume <name> [--budget USD] | /workflow status [name] | "
    "/workflow author <what it should do> [--name slug]"
)


def project_root(app: Any) -> Path:  # noqa: ANN401 - the App
    """The project root the TUI works in."""
    from bog_agents_cli.findings_controller import project_root as _root

    return _root(app)


async def _say(app: Any, text: str, *, error: bool = False) -> None:  # noqa: ANN401 - the App
    from bog_agents_cli.widgets.messages import AppMessage, ErrorMessage

    await app._mount_message((ErrorMessage if error else AppMessage)(text))


def _text(content: Any) -> str:  # noqa: ANN401 - message content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(part.get("text", "")) if isinstance(part, dict) else str(part)
            for part in content
        )
    return str(content)


def _usage(result: Any) -> tuple[int, int]:  # noqa: ANN401 - langgraph result mapping
    """Sum `usage_metadata` over the AI messages of an `ainvoke` result."""
    ins = outs = 0
    messages = result.get("messages", []) if isinstance(result, dict) else []
    for message in messages:
        usage = getattr(message, "usage_metadata", None)
        if isinstance(usage, dict):
            ins += int(usage.get("input_tokens", 0) or 0)
            outs += int(usage.get("output_tokens", 0) or 0)
    return ins, outs


def _final_text(result: Any) -> str:  # noqa: ANN401 - langgraph result mapping
    from bog_agents_cli.team_executor import _final_ai_text

    return _final_ai_text(result)


def _estimate_cost(spec: str, input_tokens: int, output_tokens: int) -> float:
    """USD from the SDK price catalog; 0 for an unpriced model."""
    try:
        from bog_agents.middleware.cost_tracker import price_for_model

        prices = price_for_model(spec.split(":", 1)[1] if ":" in spec else spec)
    except Exception:  # missing catalog
        return 0.0
    if prices is None:
        return 0.0
    return (input_tokens / 1_000_000) * prices[0] + (
        output_tokens / 1_000_000
    ) * prices[1]


def build_task_runner(
    app: Any,  # noqa: ANN401 - the App
    *,
    agent_factory: Any = None,  # noqa: ANN401 - create_cli_agent
    resolve_model: Callable[[str], Any] | None = None,
    model_spec: str = "",
) -> WorkflowTaskRunner:
    """One non-interactive, auto-approving agent per task; meters read from the result."""
    from bog_agents_cli.config import create_model, settings

    repo_dir = project_root(app)
    spec = model_spec or getattr(app, "_model_override", None) or settings.model_name

    def _resolve(s: str) -> Any:  # noqa: ANN401 - chat model
        return create_model(
            s, profile_overrides=getattr(app, "_profile_override", None)
        ).model

    resolver = resolve_model or _resolve
    if agent_factory is None:
        from bog_agents_cli.agent import create_cli_agent

        agent_factory = create_cli_agent

    async def _run(task: Any, prompt: str, phase: Any) -> TaskOutcome:  # noqa: ANN401 - workflow types
        agent, _backend = agent_factory(
            resolver(spec),
            assistant_id=f"workflow-{phase.name}-{task.id}",
            cwd=repo_dir,
            interactive=False,
            auto_approve=True,
            enable_checkpointing=False,
            enable_memory=False,
            enable_plan_mode=False,
        )
        result = await agent.ainvoke({"messages": [("human", prompt)]})
        ins, outs = _usage(result)
        cost = _estimate_cost(spec, ins, outs)
        return TaskOutcome(
            success=True,
            output=_final_text(result),
            cost_usd=cost,
            input_tokens=ins,
            output_tokens=outs,
        )

    return _run


def _find(app: Any, name: str) -> Workflow | None:  # noqa: ANN401 - the App
    return discover_workflows(project_root(app)).get(name.lstrip("/").strip().lower())


async def start_workflow_run(
    app: Any,  # noqa: ANN401 - the App
    workflow: Workflow,
    raw_args: str,
    *,
    run: WorkflowRun | None = None,
    budget_usd: float | None = None,
    task_runner: WorkflowTaskRunner | None = None,
) -> bool:
    """Bind args and start the run as a tracked session; `False` when the args were rejected."""
    try:
        args = (
            run.args if run is not None and run.args else bind_args(workflow, raw_args)
        )
    except ValueError as exc:
        await _say(app, str(exc), error=True)
        return False
    turns = getattr(app, "_turns", None)
    if turns is not None and getattr(turns, "busy", False):
        await _say(
            app,
            f"Cannot start {workflow.usage()} while another turn or session is in flight.",
            error=True,
        )
        return False
    verb = "Resuming" if run is not None else "Running"
    await _say(
        app,
        f"{verb} workflow /{workflow.name}: {len(workflow.phases)} phase(s), {workflow.task_count} task(s)"
        + (
            f", budget ${budget_usd or workflow.budget_usd:.2f}"
            if (budget_usd or workflow.budget_usd)
            else ""
        ),
    )
    app._start_tracked_session(
        _run_workflow_task(
            app, workflow, args, run=run, budget_usd=budget_usd, task_runner=task_runner
        ),
        name=f"/{workflow.name}",
    )
    return True


async def _run_workflow_task(
    app: Any,  # noqa: ANN401 - the App
    workflow: Workflow,
    args: dict[str, str],
    *,
    run: WorkflowRun | None,
    budget_usd: float | None,
    task_runner: WorkflowTaskRunner | None,
) -> None:
    root = project_root(app)
    runner = task_runner or build_task_runner(app)

    def _persist(state: WorkflowRun) -> None:
        try:
            state.save(run_path(root, state))
        except OSError:
            pass

    def _event(message: str) -> None:
        caller = getattr(app, "call_later", None)
        if callable(caller):
            caller(_say, app, f"[{workflow.name}] {message}")

    spinner = getattr(app, "_set_spinner", None)
    if callable(spinner):
        await spinner(f"Workflow /{workflow.name}")
    try:
        state = await run_workflow(
            workflow,
            runner=runner,
            args=args,
            run=run,
            persist=_persist,
            on_event=_event,
            budget_usd=budget_usd,
        )
    finally:
        if callable(spinner):
            await spinner("")
    summary = state.format_summary()
    if state.status == "done" and state.result:
        summary += f"\n\n{state.result}"
    elif state.status == "paused":
        summary += f"\n\nResume with /workflow resume {workflow.name}" + (
            " --budget <USD>" if "budget" in state.stop_reason else ""
        )
    await _say(app, summary, error=state.status == "failed")


async def run_workflow_slash(
    app: Any,  # noqa: ANN401 - the App
    cmd: PromptCommand,
    raw_args: str,
    *,
    task_runner: WorkflowTaskRunner | None = None,
) -> bool:
    """`/<name> [args]` for a discovered workflow file."""
    workflow = _find(app, cmd.name)
    if workflow is None:
        await _say(
            app, f"Workflow {cmd.name} is no longer on disk ({cmd.source}).", error=True
        )
        return True
    from bog_agents_cli.widgets.messages import UserMessage

    await app._mount_message(UserMessage(f"{cmd.name} {raw_args}".strip()))
    await start_workflow_run(app, workflow, raw_args, task_runner=task_runner)
    return True


def _author_invoke(app: Any) -> Callable[[str], str]:  # noqa: ANN401 - the App
    from bog_agents_cli.config import create_model, settings

    spec = getattr(app, "_model_override", None) or settings.model_name
    model = create_model(
        spec, profile_overrides=getattr(app, "_profile_override", None)
    ).model

    def _invoke(prompt: str) -> str:
        return _text(model.invoke(prompt).content)

    return _invoke


async def run_workflow_command(
    app: Any,  # noqa: ANN401 - the App
    command: str,
    *,
    invoke: Callable[[str], str] | None = None,
    task_runner: WorkflowTaskRunner | None = None,
) -> None:
    """Body of `/workflow`."""
    root = project_root(app)
    try:
        tokens = shlex.split(command.strip())[1:]
    except ValueError:
        tokens = command.strip().split()[1:]
    verb = tokens[0].lower() if tokens else "list"
    rest = tokens[1:]
    if verb in {"help", "-h", "--help"}:
        await _say(app, USAGE)
        return
    if verb == "list":
        await _say(app, describe_workflows(list(discover_workflows(root).values())))
        return
    if verb == "show":
        workflow = _find(app, rest[0]) if rest else None
        await _say(
            app,
            describe_workflow(workflow)
            if workflow
            else (f"No workflow named {rest[0]!r}." if rest else USAGE),
            error=not workflow,
        )
        return
    if verb == "status":
        names = [rest[0].lstrip("/")] if rest else list(discover_workflows(root))
        reports = [
            r.format_summary()
            for r in (latest_run(root, n) for n in names)
            if r is not None
        ]
        await _say(
            app, "\n\n".join(reports) if reports else "No workflow runs recorded yet."
        )
        return
    if verb == "author":
        name: str | None = None
        if "--name" in rest:
            index = rest.index("--name")
            name = rest[index + 1] if index + 1 < len(rest) else None
            del rest[index : index + 2]
        description = " ".join(rest).strip()
        if not description:
            await _say(app, USAGE)
            return
        await _say(app, f"Authoring a workflow for: {description}")
        try:
            path, workflow = author_workflow(
                description,
                invoke=invoke or _author_invoke(app),
                project_root=root,
                name=name,
            )
        except (
            Exception
        ) as exc:  # the model's YAML never validated, or the model call failed
            await _say(app, f"Could not author the workflow: {exc}", error=True)
            return
        _refresh_commands(app)
        await _say(
            app,
            f"Saved {path}\n\n{describe_workflow(workflow)}\n\nRun it with {workflow.usage()} (edit the YAML to tune it).",
        )
        return
    if verb in {"run", "resume"}:
        if not rest:
            await _say(app, USAGE)
            return
        workflow = _find(app, rest[0])
        if workflow is None:
            await _say(
                app,
                f"No workflow named {rest[0]!r}; /workflow list shows what exists.",
                error=True,
            )
            return
        budget: float | None = None
        if "--budget" in rest:
            index = rest.index("--budget")
            try:
                budget = float(rest[index + 1])
            except (IndexError, ValueError):
                await _say(app, "--budget needs a number (USD).", error=True)
                return
            del rest[index : index + 2]
        run = None
        if verb == "resume":
            run = latest_run(root, workflow.name)
            if run is None or run.status == "done":
                await _say(
                    app,
                    f"Nothing to resume for /{workflow.name}; use /workflow run {workflow.name}.",
                    error=True,
                )
                return
        await start_workflow_run(
            app,
            workflow,
            " ".join(rest[1:]),
            run=run,
            budget_usd=budget,
            task_runner=task_runner,
        )
        return
    await _say(app, f"Unknown verb {verb!r}.\n{USAGE}", error=True)


def _refresh_commands(app: Any) -> None:  # noqa: ANN401 - the App
    """Re-discover `/name` commands so a freshly authored workflow autocompletes."""
    try:
        from bog_agents_cli.prompt_commands import discover_prompt_commands

        app._prompt_commands = discover_prompt_commands(getattr(app, "_cwd", None))
        refresh = getattr(app, "_refresh_slash_command_cache", None)
        if callable(refresh):
            refresh()
    except Exception:
        logger.debug("workflow command refresh failed", exc_info=True)


__all__ = [
    "USAGE",
    "build_task_runner",
    "run_workflow_command",
    "run_workflow_slash",
    "start_workflow_run",
]
