"""Execute AmbientJob tasks via create_agent()."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import smtplib
import time
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

from bog_agents_daemon.models import (
    AmbientJob,
    JobRun,
    JobStatus,
    OutputConfig,
    OutputTarget,
    TriggerType,
)
from bog_agents_daemon.store import record_run_result, save_run, spend_db_path

logger = logging.getLogger(__name__)

_MAX_DISPATCH_ERRORS = 20
"""Cap on per-run dispatch_errors entries.

A job with many output targets that all fail (e.g. a 100-recipient
fanout during a Slack outage) used to inflate the JobRun JSON to
megabytes. Beyond this cap, additional failures are collapsed to a
single ``(overflow)`` entry recording the truncated count.
"""


class BudgetPausedError(Exception):
    """The agent hit the job's `budget_usd` mid-run (ROADMAP #51).

    Carries what `resume_paused_run` needs to continue the very same graph
    run: the compiled agent (with its in-memory checkpointer) and the run
    config that names the thread.
    """

    def __init__(self, payload: dict[str, Any], *, agent: Any, config: dict[str, Any]) -> None:
        """Store the interrupt payload and the paused graph."""
        super().__init__(str(payload.get("message") or "budget reached"))
        self.payload = payload
        self.agent = agent
        self.config = config


@dataclass
class PausedRun:
    """A run parked on a `budget_reached` interrupt, awaiting a raise-cap resume."""

    job: AmbientJob
    run: JobRun
    agent: Any
    config: dict[str, Any]
    trigger_type: TriggerType


_PAUSED_RUNS: dict[str, PausedRun] = {}
_RESUME_TASKS: set[asyncio.Task[Any]] = set()


def is_paused(run_id: str) -> bool:
    """Whether `run_id` is parked on a budget pause in this daemon process."""
    return run_id in _PAUSED_RUNS


def paused_run_ids() -> list[str]:
    """Run ids currently parked on a budget pause."""
    return sorted(_PAUSED_RUNS)


def _budget_interrupt_payload(chunk: Any) -> dict[str, Any] | None:
    """Return the `budget_reached` payload carried by a stream chunk, if any."""
    if not isinstance(chunk, dict) or "__interrupt__" not in chunk:
        return None
    interrupts = chunk.get("__interrupt__") or ()
    for item in interrupts:
        value = getattr(item, "value", item)
        if isinstance(value, dict) and value.get("type") == "budget_reached":
            return value
    return None


def _spend_ledger() -> Any:
    from bog_agents.spend_ledger import SpendLedger

    return SpendLedger(spend_db_path())


def job_spent_today_usd(job: AmbientJob) -> float:
    """Today's recorded spend for `job` (best-effort; `0.0` when the ledger is unreadable)."""
    from bog_agents.spend_ledger import daemon_scope

    try:
        ledger = _spend_ledger()
        try:
            return ledger.total_usd(daemon_scope(job.job_id))
        finally:
            ledger.close()
    except Exception:
        logger.debug("spend ledger unreadable; treating today's spend as 0", exc_info=True)
        return 0.0


def _record_job_spend(job: AmbientJob, *, input_tokens: int, output_tokens: int) -> float:
    """Price a run's tokens for the job's model and record them under the job's scope."""
    from bog_agents.middleware.cost_tracker import price_for_model
    from bog_agents.spend_ledger import daemon_scope

    if not (input_tokens or output_tokens):
        return 0.0
    price = price_for_model(job.model or "")
    if price is None:
        return 0.0
    usd = (input_tokens * price[0] + output_tokens * price[1]) / 1_000_000
    try:
        ledger = _spend_ledger()
        try:
            ledger.record(daemon_scope(job.job_id), usd, model=job.model, input_tokens=input_tokens, output_tokens=output_tokens)
        finally:
            ledger.close()
    except Exception:
        logger.debug("could not record job spend", exc_info=True)
    return usd


async def _collect_stream(job: AmbientJob, agent: Any, stream_input: Any, config: dict[str, Any] | None) -> str:
    """Drain one `astream` pass, returning the last AI text; raise `BudgetPausedError` on a budget interrupt."""
    result_output = ""
    tokens_in = 0
    tokens_out = 0
    stream = agent.astream(stream_input, config=config) if config is not None else agent.astream(stream_input)
    async for chunk in stream:
        payload = _budget_interrupt_payload(chunk)
        if payload is not None:
            _record_job_spend(job, input_tokens=tokens_in, output_tokens=tokens_out)
            raise BudgetPausedError(payload, agent=agent, config=config or {})
        if not isinstance(chunk, dict):
            continue
        for node_output in chunk.values():
            # Some middleware writes state via langgraph reducer
            # primitives (Overwrite, Send, Command) that aren't
            # iterable. Only consume `messages` when it's an actual
            # list — the add_messages reducer normalises real
            # message updates into a list before they show up here.
            if not isinstance(node_output, dict):
                continue
            messages = node_output.get("messages")
            if not isinstance(messages, list):
                continue
            for msg in messages:
                content = getattr(msg, "content", None)
                if content and hasattr(msg, "type") and msg.type == "ai":
                    result_output = content if isinstance(content, str) else str(content)
                usage = getattr(msg, "usage_metadata", None)
                if isinstance(usage, dict):
                    tokens_in += int(usage.get("input_tokens") or 0)
                    tokens_out += int(usage.get("output_tokens") or 0)
    _record_job_spend(job, input_tokens=tokens_in, output_tokens=tokens_out)
    return result_output


async def resume_paused_run(run_id: str, *, budget_usd: float) -> JobRun:
    """Continue a budget-paused run with a raised cap (ROADMAP #51).

    Args:
        run_id: A run parked by `run_job` (see `is_paused`).
        budget_usd: The new cap; must exceed what the run has spent or the
            graph pauses again immediately.

    Returns:
        The finished (or re-paused) `JobRun`.

    Raises:
        KeyError: If `run_id` is not paused in this process.
    """
    from langgraph.types import Command

    paused = _PAUSED_RUNS.pop(run_id)
    job, run = paused.job, paused.run
    run.status = JobStatus.RUNNING
    run.error = ""
    save_run(run)
    try:
        output = await asyncio.wait_for(
            _collect_stream(job, paused.agent, Command(resume={"budget_usd": budget_usd}), paused.config),
            timeout=_AGENT_TIMEOUT_SECONDS,
        )
    except BudgetPausedError as exc:
        _park_run(job, run, exc, trigger_type=paused.trigger_type)
    except Exception as exc:
        logger.exception("Resumed run %s (%s) failed", run.run_id, job.name)
        run.error = str(exc)
        run.status = JobStatus.FAILED
        run.finished_at = time.time()
    else:
        run.output = output
        run.status = JobStatus.COMPLETED
        run.finished_at = time.time()
    return await _finish_run(job, run)


def _park_run(job: AmbientJob, run: JobRun, exc: BudgetPausedError, *, trigger_type: TriggerType) -> None:
    """Mark `run` paused and keep the graph so it can be resumed."""
    run.status = JobStatus.PAUSED
    run.error = f"budget_reached: {exc.payload.get('message', 'budget reached')}"
    run.finished_at = 0.0
    _PAUSED_RUNS[run.run_id] = PausedRun(job=job, run=run, agent=exc.agent, config=exc.config, trigger_type=trigger_type)
    logger.warning("Job %s (%s) run %s paused on budget: %s", job.job_id, job.name, run.run_id, run.error)


async def run_job(
    job: AmbientJob,
    *,
    trigger_type: TriggerType = TriggerType.MANUAL,
    trigger_context: dict[str, Any] | None = None,
    _existing_run: JobRun | None = None,
) -> JobRun:
    """Execute an AmbientJob and persist the result.

    Creates a JobRun record (or reuses one), invokes the agent with the
    job's prompt, captures the output, updates the job's run history, and
    dispatches to all configured output targets.

    Args:
        job: The job to execute.
        trigger_type: How this execution was initiated.
        trigger_context: Optional metadata from the trigger (e.g. webhook payload).
        _existing_run: When the API has already persisted a placeholder run
            record (so HTTP clients can see a run_id immediately), reuse it
            instead of allocating a new one.

    Returns:
        The completed JobRun record.
    """
    if _existing_run is not None:
        run = _existing_run
    else:
        run = JobRun(
            job_id=job.job_id,
            job_name=job.name,
            trigger_type=trigger_type,
            trigger_context=trigger_context or {},
        )
        save_run(run)

    try:
        prompt = _build_prompt(job, trigger_type=trigger_type, trigger_context=trigger_context)
    except Exception as exc:
        # Prompt/skill/pipeline resolution is deterministic — retrying it would
        # just fail identically, so mark FAILED without consuming retries.
        logger.exception("Job %s (%s) prompt resolution failed", job.job_id, job.name)
        run.error = str(exc)
        run.status = JobStatus.FAILED
        run.finished_at = time.time()
    else:
        spent_today = job_spent_today_usd(job) if job.daily_ceiling_usd else 0.0
        if job.daily_ceiling_usd and spent_today >= job.daily_ceiling_usd:
            # ROADMAP #51: the job's daily ceiling is already reached — record
            # the trigger, spend nothing.
            run.error = f"daily ceiling reached: ${spent_today:.2f} of ${job.daily_ceiling_usd:.2f} spent today"
            run.status = JobStatus.SKIPPED
            run.finished_at = time.time()
        else:
            try:
                output, run.attempts, agent_exc = await _invoke_agent_with_retry(job, prompt, trigger_type=trigger_type, run_id=run.run_id)
            except BudgetPausedError as exc:
                _park_run(job, run, exc, trigger_type=trigger_type)
            else:
                if agent_exc is None:
                    run.output = output
                    run.status = JobStatus.COMPLETED
                else:
                    run.error = str(agent_exc)
                    run.status = JobStatus.FAILED
                run.finished_at = time.time()

    return await _finish_run(job, run)


async def _finish_run(job: AmbientJob, run: JobRun) -> JobRun:
    """Persist the run outcome on the job, dispatch outputs (unless paused), return the run."""
    # Update job state. Merge ONLY run-state fields into the current on-disk
    # record (read-modify-write) so a concurrent config edit (PATCH /jobs)
    # isn't clobbered by this pre-run snapshot. (REVIEW.md v2 P1-56.) Mirror
    # onto the local object too for callers that read the returned job.
    job.last_run_at = run.started_at
    job.last_status = run.status
    job.last_output = run.output[:500] if run.output else run.error[:500]
    job.run_count += 1

    record_run_result(
        job,
        last_run_at=run.started_at,
        last_status=run.status,
        last_output=job.last_output,
    )
    save_run(run)
    if run.status == JobStatus.PAUSED:
        # Nothing to deliver yet; `resume_paused_run` finishes and dispatches.
        return run

    # Dispatch outputs best-effort. Capture per-target failures on the
    # run so an operator can tell from the run record that delivery
    # failed even though the agent itself completed successfully. The
    # log line alone wasn't enough — silent dispatch outages turned
    # into "why didn't the daily report arrive?" tickets with no
    # paper trail in the runs table.
    #
    # Cap the captured errors at _MAX_DISPATCH_ERRORS so a job with
    # hundreds of broken targets (e.g. a Slack webhook outage hitting
    # a fanout job) doesn't bloat the JobRun record. Beyond the cap we
    # collapse to a count so the JSON stays bounded.
    job_wd = Path(job.working_dir) if job.working_dir else None
    overflow_count = 0
    for output_config in job.outputs:
        try:
            await _dispatch_with_retry(
                run,
                output_config,
                working_dir=job_wd,
                max_retries=job.max_retries,
                backoff=job.retry_backoff_seconds,
            )
        except Exception as exc:
            logger.exception(
                "Output dispatch failed for job %s target %s",
                job.job_id,
                output_config.target,
            )
            if len(run.dispatch_errors) < _MAX_DISPATCH_ERRORS:
                run.dispatch_errors.append({"target": str(output_config.target), "error": str(exc)})
            else:
                overflow_count += 1
    if overflow_count:
        run.dispatch_errors.append({
            "target": "(overflow)",
            "error": f"{overflow_count} additional dispatch failure(s) truncated",
        })

    # If the agent run succeeded but dispatches failed, mark the run as
    # COMPLETED but keep ``error`` populated so HTTP clients and the
    # ``/runs`` CLI surface flag the partial failure. Status stays
    # COMPLETED to reflect that the work was done — the failure is in
    # delivery, not execution — but a non-empty ``error`` is the
    # universal signal callers already check.
    if run.dispatch_errors and run.status == JobStatus.COMPLETED and not run.error:
        run.error = f"agent succeeded but {len(run.dispatch_errors)} output target(s) failed; see dispatch_errors for details"
        save_run(run)

    return run


_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def render_template(template: str, values: Mapping[str, Any]) -> str:
    """Substitute `{name}` placeholders in `template` from `values`.

    Only bare identifiers in braces are placeholders (`{pr_number}`); anything
    else — JSON literals, format specs, unknown names — is left verbatim so a
    prompt that quotes code or a payload never blows up on an unknown key.

    Args:
        template: The text to render.
        values: Placeholder name → replacement value (stringified).

    Returns:
        The rendered text.
    """
    if "{" not in template:
        return template

    def _sub(match: re.Match[str]) -> str:
        key = match.group(1)
        return str(values[key]) if key in values else match.group(0)

    return _PLACEHOLDER_RE.sub(_sub, template)


def template_values(
    *,
    job_name: str,
    job_id: str,
    working_dir: str = "",
    trigger_type: TriggerType | str = TriggerType.MANUAL,
    trigger_context: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    """Build the placeholder map for a job prompt or output field.

    v6 DMN-1: the daemon used to send `job.prompt` to the model verbatim, so
    the issue number, body and branch parsed by the GitHub front door, a CI
    webhook payload, or the path a file trigger fired on never reached the
    agent — and the documented `{trigger_context_json}` / `{pr_number}` /
    `{date}` / `{trigger_path}` placeholders were inert text.

    Available keys: `date`, `time`, `datetime`, `job_name`, `job_id`,
    `working_dir`, `trigger_type`, `trigger_context_json`, every top-level
    string-keyed entry of the trigger context (non-string values as JSON),
    plus the aliases `number` / `pr_number` / `issue_number` and
    `trigger_path`.

    Args:
        job_name: The job's display name.
        job_id: The job's identifier.
        working_dir: The job's working directory ("" when unset).
        trigger_type: How the run was initiated.
        trigger_context: Trigger metadata (webhook payload, GitHub event, …).

    Returns:
        A name → string mapping for `render_template`.
    """
    ctx: dict[str, Any] = dict(trigger_context or {})
    now = datetime.now().astimezone()
    values: dict[str, str] = {
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M:%S"),
        "datetime": now.isoformat(timespec="seconds"),
        "job_name": job_name,
        "job_id": job_id,
        "working_dir": working_dir,
        "trigger_type": str(getattr(trigger_type, "value", trigger_type)),
        "trigger_context_json": json.dumps(ctx, sort_keys=True, default=str),
    }
    for key, value in ctx.items():
        if isinstance(key, str) and key.isidentifier():
            values.setdefault(key, value if isinstance(value, str) else json.dumps(value, default=str))
    number = ctx.get("number") or ctx.get("pr_number") or ctx.get("issue_number")
    if number not in (None, "", 0):
        for alias in ("number", "pr_number", "issue_number"):
            values.setdefault(alias, str(number))
    path = ctx.get("trigger_path") or ctx.get("path") or ctx.get("changed_path")
    if path:
        values.setdefault("trigger_path", str(path))
    return values


def _render_output_field(value: str, run: JobRun) -> str:
    """Render placeholders in an output field (file path, issue number) for `run`."""
    if not value or "{" not in value:
        return value
    return render_template(
        value,
        template_values(
            job_name=run.job_name,
            job_id=run.job_id,
            trigger_type=run.trigger_type,
            trigger_context=run.trigger_context,
        ),
    )


def _build_prompt(
    job: AmbientJob,
    *,
    trigger_type: TriggerType = TriggerType.MANUAL,
    trigger_context: Mapping[str, Any] | None = None,
) -> str:
    """Build the prompt string for a job invocation.

    Resolves the prompt source — the raw `prompt`, the named skill's SKILL.md
    (read from `~/.bog-agents/{agent}/skills/{skill}/SKILL.md` and inlined so
    the model has the full instructions), or the named pipeline's YAML turned
    into a structured request — then renders `{placeholder}` references from
    the trigger (see `template_values`). Unknown placeholders stay verbatim.

    Args:
        job: The job whose prompt to resolve.
        trigger_type: How this run was initiated.
        trigger_context: Trigger metadata to expose as placeholders.

    Returns:
        The resolved, rendered prompt string.

    Raises:
        ValueError: If no prompt source is configured or the named
            skill/pipeline can't be located.
    """
    if job.prompt:
        base = job.prompt
    elif job.skill_name:
        base = _resolve_skill_prompt(job.skill_name)
    elif job.pipeline_name:
        base = _resolve_pipeline_prompt(job.pipeline_name)
    else:
        msg = f"Job '{job.name}' ({job.job_id}) has no prompt, skill, or pipeline configured"
        raise ValueError(msg)
    return render_template(
        base,
        template_values(
            job_name=job.name,
            job_id=job.job_id,
            working_dir=job.working_dir,
            trigger_type=trigger_type,
            trigger_context=trigger_context,
        ),
    )


_SKILL_SEARCH_PATHS = (
    (".bog-agents", "skills"),
    (".agents", "skills"),
)


def _find_skill_file(skill_name: str) -> Path | None:
    """Locate a SKILL.md by name under the standard search paths.

    Returns the first path that exists, or None when the skill isn't
    found anywhere.
    """
    candidates = [Path.cwd() / segs[0] / segs[1] / skill_name / "SKILL.md" for segs in _SKILL_SEARCH_PATHS] + [
        Path.home() / ".bog-agents" / "agent" / "skills" / skill_name / "SKILL.md",
        Path.home() / ".bog-agents" / "skills" / skill_name / "SKILL.md",
    ]
    for path in candidates:
        if path.is_file():
            return path
    return None


def _parse_skill_frontmatter(content: str, *, skill_path: Path | None = None) -> tuple[dict[str, Any], str]:
    """Extract YAML frontmatter from a SKILL.md.

    Args:
        content: Raw SKILL.md text.
        skill_path: Optional path to the SKILL.md, used only to make the
            warning logged on malformed frontmatter actionable.

    Returns:
        Tuple of (frontmatter dict, body string). Returns ({}, content)
        when no frontmatter is present.
    """
    import re

    import yaml

    match = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", content, re.DOTALL)
    if not match:
        return {}, content
    try:
        frontmatter = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as exc:
        # Malformed frontmatter silently dropped any `chain:` declaration,
        # so chained skills vanished with no diagnostic. Log so operators
        # can find the broken SKILL.md.
        logger.warning("Skill frontmatter is not valid YAML (%s): %s", skill_path or "<unknown>", exc)
        frontmatter = {}
    if not isinstance(frontmatter, dict):
        logger.warning("Skill frontmatter is not a mapping (%s); ignoring", skill_path or "<unknown>")
        frontmatter = {}
    return frontmatter, match.group(2)


def _resolve_skill_prompt(
    skill_name: str,
    *,
    _seen: set[str] | None = None,
    _depth: int = 0,
) -> str:
    """Load a skill's SKILL.md content and wrap it as a runnable prompt.

    Skill chaining: when a SKILL.md frontmatter declares a `chain:` list
    of skill names, those skills are loaded recursively and their bodies
    are concatenated into a single composite prompt with clear section
    boundaries. Cycle-safe (a skill can't transitively depend on itself);
    capped at 8 levels deep to bound runtime cost.

    Args:
        skill_name: Skill identifier.
        _seen: Internal — set of names already in the current chain
            traversal (used to detect cycles). Callers should leave this
            at the default.
        _depth: Internal — recursion depth gate.

    Raises:
        ValueError: If the skill can't be located, or a chain cycle/depth
            limit is hit.
    """
    if _depth > 8:
        msg = f"Skill chain exceeds 8 levels deep (currently resolving '{skill_name}')"
        raise ValueError(msg)
    if _seen is None:
        _seen = set()
    if skill_name in _seen:
        chain_repr = " -> ".join([*sorted(_seen), skill_name])
        msg = f"Skill chain cycle detected: {chain_repr}"
        raise ValueError(msg)
    _seen = _seen | {skill_name}

    path = _find_skill_file(skill_name)
    if path is None:
        msg = f"Skill '{skill_name}' not found under .bog-agents/skills, .agents/skills, or ~/.bog-agents/.../skills"
        raise ValueError(msg)

    raw = path.read_text(encoding="utf-8")
    frontmatter, body = _parse_skill_frontmatter(raw, skill_path=path)

    # Resolve chained skills first so their content is available for
    # context. The `chain:` value can be a list of names or a single name.
    chain_field = frontmatter.get("chain")
    chained_sections: list[str] = []
    if chain_field:
        chain_names: list[str] = [chain_field] if isinstance(chain_field, str) else list(chain_field)
        for chained_name in chain_names:
            if not isinstance(chained_name, str) or not chained_name.strip():
                continue
            chained_body = _resolve_skill_prompt(chained_name.strip(), _seen=_seen, _depth=_depth + 1)
            chained_sections.append(f"### Skill `{chained_name.strip()}` (chained)\n\n{chained_body}")

    if chained_sections:
        chained_block = "\n\n".join(chained_sections)
        return (
            f"You are running the skill `{skill_name}` which composes "
            f"{len(chained_sections)} chained skill(s). Execute each chained "
            f"skill's instructions in order, then apply this skill's body.\n\n"
            f"{chained_block}\n\n"
            f"### Skill `{skill_name}` (primary)\n\n{body}"
        )
    return f"You are running the skill `{skill_name}`. Follow the instructions in this SKILL.md to completion:\n\n{body}"


def _resolve_pipeline_prompt(pipeline_name: str) -> str:
    """Load a pipeline YAML and synthesise a multi-step prompt.

    Looks under `<cwd>/.bog-agents/pipelines/<name>.yaml`, then
    `~/.bog-agents/pipelines/<name>.yaml`. The agent receives the
    pipeline's description plus an enumerated step list and is
    instructed to walk it deterministically.
    """
    from pathlib import Path

    import yaml

    candidates = [
        Path.cwd() / ".bog-agents" / "pipelines" / f"{pipeline_name}.yaml",
        Path.cwd() / ".bog-agents" / "pipelines" / f"{pipeline_name}.yml",
        Path.home() / ".bog-agents" / "pipelines" / f"{pipeline_name}.yaml",
        Path.home() / ".bog-agents" / "pipelines" / f"{pipeline_name}.yml",
    ]
    for path in candidates:
        if path.is_file():
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            steps = data.get("steps", []) or []
            description = data.get("description", "")
            lines = [
                f"You are running the pipeline `{pipeline_name}`.",
                f"Description: {description}" if description else "",
                "",
                "Execute these steps in order, using your tools as needed.",
                "Treat each step as its own subtask and complete it fully",
                "before moving on. The final response should summarise the",
                "outcome of every step.",
                "",
                "Steps:",
            ]
            for i, step in enumerate(steps, 1):
                step_id = step.get("id", f"step-{i}")
                step_type = step.get("type", "message")
                body = step.get("text") or step.get("name", "")
                lines.append(f"{i}. [{step_type}] {step_id}: {body}")
            return "\n".join(lines)
    msg = f"Pipeline '{pipeline_name}' not found under .bog-agents/pipelines or ~/.bog-agents/pipelines"
    raise ValueError(msg)


def _load_agent_timeout() -> int:
    """Read BOG_DAEMON_AGENT_TIMEOUT from the environment, defaulting to 1800 (30 min).

    Returns:
        Timeout in seconds; falls back to 1800 if the env var is absent or non-numeric.
    """
    raw = os.environ.get("BOG_DAEMON_AGENT_TIMEOUT", "")
    if raw:
        try:
            return int(raw)
        except ValueError:
            logger.warning("Invalid BOG_DAEMON_AGENT_TIMEOUT=%r; using default 1800s", raw)
    return 1800


_AGENT_TIMEOUT_SECONDS = _load_agent_timeout()


def _allow_unattended_shell() -> bool:
    """Whether unrestricted real-path shell is opted-in for unattended triggers.

    Network/event triggers (WEBHOOK, GIT_PUSH, and any non-MANUAL trigger) run
    unattended and may be reachable from outside the host (the webhook path is
    HMAC-secret-only). By default such jobs run with `virtual_mode=True` so the
    shell/filesystem are confined to the job's `working_dir`. Setting
    `BOG_DAEMON_ALLOW_UNATTENDED_SHELL=1` (or `true`/`yes`) opts the operator
    into unrestricted real-absolute-path shell for these triggers.

    Returns:
        True only when the env var is explicitly set to an affirmative value.
    """
    return os.environ.get("BOG_DAEMON_ALLOW_UNATTENDED_SHELL", "").strip().lower() in ("1", "true", "yes")


def _select_backend(root_dir: Path, job: AmbientJob, trigger_type: TriggerType) -> Any:
    """Choose the agent backend for a run based on the trigger's trust level.

    DMN-1 (v4): `virtual_mode` confines only *filesystem* tools — the SDK is
    explicit that a shell-capable backend's `execute()` runs unrestricted on the
    host regardless. So for unattended triggers (cron, interval, file-change,
    git-push, and the HMAC-secret-only webhook path) confining the shell means
    NOT giving the agent a shell-capable backend at all, and NOT handing it the
    daemon's environment (secrets). A git-push job whose prompt ingests
    attacker-authored commit text is the worst case.

    - MANUAL triggers are token-authenticated (an operator is at the keyboard):
      unrestricted real-path `LocalShellBackend` with the inherited environment,
      mirroring the CLI.
    - Non-MANUAL triggers get a non-sandbox `FilesystemBackend` rooted at
      `working_dir`, so filesystem read/grep for skills/pipelines still works but
      the `execute` tool reports "not available" and no host environment is
      exposed. Operators can opt back into unrestricted shell per deployment with
      `BOG_DAEMON_ALLOW_UNATTENDED_SHELL=1`; when they do and a network-reachable
      trigger fires, a WARNING is logged.

    Args:
        root_dir: The job's working directory, used as the backend root.
        job: The job (for identifying it in the opt-in warning).
        trigger_type: How the run was initiated.

    Returns:
        A `BackendProtocol` for `create_agent`.
    """
    is_manual = trigger_type == TriggerType.MANUAL
    shell_allowed = is_manual or _allow_unattended_shell()

    if not shell_allowed:
        from bog_agents.backends.filesystem import FilesystemBackend

        # No shell tool (non-sandbox backend), no inherited env; filesystem
        # tools stay confined to root_dir via virtual_mode.
        return FilesystemBackend(root_dir=root_dir, virtual_mode=True)

    from bog_agents.backends.local_shell import LocalShellBackend

    if not is_manual:
        logger.warning(
            "Unattended %s job %s (%s) is running with UNRESTRICTED host shell and "
            "the daemon's environment because BOG_DAEMON_ALLOW_UNATTENDED_SHELL is "
            "set; a network-reachable trigger now has real-path shell on the host.",
            trigger_type.value,
            job.job_id,
            job.name,
        )
    # virtual_mode=False operates on real absolute paths inside the project tree
    # (mirrors the CLI); execute() runs on the host.
    env = os.environ.copy()
    # #27: honor the committed `.bog-agents/sandbox.toml` egress allowlist for
    # unattended shell runs by surfacing it in the backend's env, where the #22
    # local-sandbox proxy reads it. (Preinstall is deliberately NOT auto-run on
    # the daemon host — that would run committed shell unattended, the exact risk
    # DMN-1 guards; provider sandboxes run it via the CLI factory instead.)
    from bog_agents.sandbox_config import SANDBOX_NETWORK_ALLOWLIST_ENV, load_sandbox_config

    spec = load_sandbox_config(root_dir)
    if spec is not None and spec.network_allowlist:
        env[SANDBOX_NETWORK_ALLOWLIST_ENV] = ",".join(spec.network_allowlist)
        logger.info("Applying sandbox spec for job %s (%s): %s", job.job_id, job.name, spec.summary())
    return LocalShellBackend(root_dir=root_dir, inherit_env=True, env=env, virtual_mode=False)


async def _invoke_agent(
    job: AmbientJob,
    prompt: str,
    *,
    trigger_type: TriggerType = TriggerType.MANUAL,
    run_id: str = "",
) -> str:
    """Invoke create_agent() with the job configuration and capture output.

    Uses a lazy import to avoid circular imports at module load time.  Enforces
    a per-job timeout (default 30 minutes, override with BOG_DAEMON_AGENT_TIMEOUT).

    Safe-by-default unattended posture (V3-11, hardened in DMN-1/v4): for
    non-MANUAL triggers (cron, interval, file-change, git-push, and especially
    externally-reachable webhooks) the agent gets a non-sandbox
    `FilesystemBackend` — filesystem read/grep for skills/pipelines still works,
    but there is no host shell (`execute` reports "not available") and the
    daemon's environment is not exposed. `virtual_mode` alone was insufficient:
    it confines only filesystem tools, never the shell. A MANUAL trigger is
    token-authenticated and keeps the unrestricted real-path `LocalShellBackend`.
    Operators who deliberately want unrestricted unattended shell opt in with
    `BOG_DAEMON_ALLOW_UNATTENDED_SHELL=1`; when they do and a network-reachable
    trigger fires, a WARNING is logged. See `_select_backend`.

    Args:
        job: The job providing model and working_dir configuration.
        prompt: The resolved prompt to run.
        trigger_type: How this run was initiated; controls the shell sandbox
            posture (MANUAL stays unrestricted, others harden by default).
        run_id: The run's id, used as the checkpoint thread when the job has a
            `budget_usd` (so a budget pause can be resumed).

    Returns:
        The last AI message content from the agent.

    Raises:
        TimeoutError: If the agent does not complete within the allowed time.
        BudgetPausedError: If the job's `budget_usd` was hit (ROADMAP #51); the
            paused graph rides on the exception for `resume_paused_run`.
    """
    from bog_agents import create_agent
    from bog_agents.feature_config import FeatureConfig

    # Root the agent's filesystem and shell at the job's working_dir so
    # skills/pipelines that read or grep project files actually work. Without
    # this the SDK falls back to StateBackend and the agent reports "no files
    # are mounted" even when --working-dir was set.
    root_dir = Path(job.working_dir) if job.working_dir else Path.cwd()

    backend = _select_backend(root_dir, job, trigger_type)

    # V3-13: use the FeatureConfig path instead of the deprecated bare
    # `enable_git_tools=` kwarg (which flows through **legacy_feature_flags and
    # emits a DeprecationWarning on every job; removed at bog-agents 1.0).
    # ROADMAP #51: a job budget turns on cost tracking; the SDK pauses the
    # graph with a `budget_reached` interrupt at the cap, which needs a
    # checkpointer + thread to park on. Uncapped jobs keep the old shape.
    kwargs: dict[str, Any] = {
        "config": FeatureConfig(enable_git_tools=True, enable_cost_tracking=job.budget_usd is not None, budget_usd=job.budget_usd),
        "backend": backend,
        "working_dir": str(root_dir),
    }
    if job.model:
        kwargs["model"] = job.model
    run_config: dict[str, Any] | None = None
    if job.thread_id:
        # ROADMAP #55: continue the interactive thread that created the job —
        # reopen the CLI's checkpointer and make the event the next message.
        saver_cm = open_thread_checkpointer(job)
        if saver_cm is not None:
            prompt = continuation_prompt(job, prompt, trigger_type=trigger_type)
            async with saver_cm as saver:
                kwargs["checkpointer"] = saver
                run_config = {"configurable": {"thread_id": job.thread_id}}
                agent = create_agent(**kwargs)
                return await _run_with_timeout(job, agent, prompt, run_config)
    if job.budget_usd is not None:
        from langgraph.checkpoint.memory import MemorySaver

        kwargs["checkpointer"] = MemorySaver()
        run_config = {"configurable": {"thread_id": run_id or f"run-{int(time.time() * 1000)}"}}
    agent = create_agent(**kwargs)
    return await _run_with_timeout(job, agent, prompt, run_config)


async def _run_with_timeout(job: AmbientJob, agent: Any, prompt: str, run_config: dict[str, Any] | None) -> str:
    try:
        return await asyncio.wait_for(
            _collect_stream(job, agent, {"messages": [("human", prompt)]}, run_config),
            timeout=_AGENT_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        msg = f"Agent timed out after {_AGENT_TIMEOUT_SECONDS}s for job {job.job_id} ({job.name})"
        raise TimeoutError(msg) from None


def thread_checkpoint_db(job: AmbientJob) -> Path:
    """The SQLite checkpoint database a thread-linked job resumes from (the CLI's `sessions.db` by default)."""
    if job.checkpoint_db:
        return Path(job.checkpoint_db).expanduser()
    raw = os.environ.get("BOG_AGENTS_HOME", "").strip()
    home = Path(raw).expanduser() if raw else Path.home() / ".bog-agents"
    return home / "sessions.db"


def open_thread_checkpointer(job: AmbientJob) -> Any:
    """An `AsyncSqliteSaver` context manager for the job's thread, or `None` when it cannot be opened.

    `None` (with a logged warning) means the run falls back to a fresh agent:
    the sqlite checkpointer is not installed, or the database does not exist.
    """
    try:
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    except ImportError:
        logger.warning("Job %s wants thread %s but langgraph-checkpoint-sqlite is not installed; running fresh", job.job_id, job.thread_id)
        return None
    path = thread_checkpoint_db(job)
    if not path.exists():
        logger.warning("Job %s wants thread %s but %s does not exist; running fresh", job.job_id, job.thread_id, path)
        return None
    return AsyncSqliteSaver.from_conn_string(str(path))


def continuation_prompt(job: AmbientJob, prompt: str, *, trigger_type: TriggerType) -> str:
    """Frame a thread-linked run as a continuation: what fired, and the goal the thread was pursuing."""
    lines = [f"[ambient: {trigger_type.value} trigger for job {job.name or job.job_id}; this continues your earlier thread]"]
    if job.goal_ref:
        try:
            data = json.loads(Path(job.goal_ref).read_text(encoding="utf-8"))
            objective = str(data.get("objective") or "").strip() if isinstance(data, dict) else ""
        except (OSError, ValueError):
            objective = ""
        if objective:
            lines.append(f"Goal: {objective}")
    lines.append(prompt)
    return "\n".join(lines)


async def _invoke_agent_with_retry(
    job: AmbientJob,
    prompt: str,
    *,
    trigger_type: TriggerType = TriggerType.MANUAL,
    run_id: str = "",
) -> tuple[str, int, Exception | None]:
    """Invoke the agent, retrying transient failures per the job's retry policy.

    Retries up to `job.max_retries` additional times (total attempts =
    `max_retries + 1`) with exponential backoff starting at
    `job.retry_backoff_seconds` and doubling each retry. With the default
    `max_retries=0` this is a single attempt, identical to the pre-retry
    behaviour, so existing jobs are unaffected.

    Args:
        job: The job supplying model config and retry policy.
        prompt: The resolved prompt to run.
        trigger_type: How this run was initiated (controls the shell posture).

    Returns:
        A tuple of `(output, attempts, last_exception)`. `last_exception` is
        None on success; on exhaustion it holds the final failure and `output`
        is the empty string.
    """
    total = max(0, job.max_retries) + 1
    delay = max(0.0, job.retry_backoff_seconds)
    last_exc: Exception | None = None
    for attempt in range(1, total + 1):
        try:
            output = await _invoke_agent(job, prompt, trigger_type=trigger_type, run_id=run_id)
        except BudgetPausedError:
            # Deterministic, not transient: retrying would spend the same
            # budget again. The caller parks the run instead.
            raise
        except Exception as exc:
            last_exc = exc
            if attempt < total:
                logger.warning(
                    "Job %s (%s) agent attempt %d/%d failed (%s); retrying in %.1fs",
                    job.job_id,
                    job.name,
                    attempt,
                    total,
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)
                delay *= 2
                continue
            logger.exception("Job %s (%s) failed after %d attempt(s)", job.job_id, job.name, attempt)
            return "", attempt, exc
        else:
            if attempt > 1:
                logger.info("Job %s (%s) succeeded on retry attempt %d/%d", job.job_id, job.name, attempt, total)
            return output, attempt, None
    # Unreachable — the loop always returns — but keeps the return type total.
    return "", total, last_exc


async def _dispatch_with_retry(
    run: JobRun,
    output: OutputConfig,
    *,
    working_dir: Path | None = None,
    max_retries: int = 0,
    backoff: float = 2.0,
) -> None:
    """Dispatch one output target, retrying transient delivery failures.

    Retries up to `max_retries` additional times with exponential backoff. The
    final failure is re-raised so `run_job` records it in `run.dispatch_errors`
    (bounded by `_MAX_DISPATCH_ERRORS`).

    Args:
        run: The completed job run to deliver.
        output: The output target configuration.
        working_dir: Optional job working directory (anchors file output).
        max_retries: Extra delivery attempts on failure (0 = single-shot).
        backoff: Base backoff in seconds, doubled each retry.
    """
    total = max(0, max_retries) + 1
    delay = max(0.0, backoff)
    for attempt in range(1, total + 1):
        try:
            await _dispatch_output(run, output, working_dir=working_dir)
            return
        except Exception:
            if attempt >= total:
                raise
            logger.warning(
                "Dispatch to %s for job %s failed (attempt %d/%d); retrying in %.1fs",
                output.target,
                run.job_id,
                attempt,
                total,
                delay,
            )
            await asyncio.sleep(delay)
            delay *= 2


async def _dispatch_output(run: JobRun, output: OutputConfig, *, working_dir: Path | None = None) -> None:
    """Send run output to a configured target.

    Args:
        run: The completed job run with output to dispatch.
        output: The output destination configuration.
        working_dir: Optional job working directory, forwarded to file output.
    """
    target = output.target

    if target == OutputTarget.FILE:
        await _dispatch_file(run, output, working_dir=working_dir)
    elif target == OutputTarget.STDOUT:
        logger.info("Job '%s' (%s): %s", run.job_name, run.job_id, run.output)
    elif target == OutputTarget.LOG:
        logger.info(
            "Job '%s' (%s) output:\n%s",
            run.job_name,
            run.job_id,
            run.output,
        )
    elif target == OutputTarget.WEBHOOK:
        await _dispatch_webhook(run, output)
    elif target == OutputTarget.EMAIL:
        await _dispatch_email(run, output)
    elif target == OutputTarget.SLACK:
        await _dispatch_slack(run, output)
    elif target == OutputTarget.GITHUB_COMMENT:
        await _dispatch_github_comment(run, output)


async def _dispatch_file(run: JobRun, output: OutputConfig, *, working_dir: Path | None = None) -> None:
    """Write run output to a file.

    Guards against path traversal: only writes to paths inside the user's
    home directory, the system temp dir, the current working directory, or
    the job's configured `working_dir`. A relative `file_path` is anchored
    to `working_dir` (or cwd) before resolution so jobs can write into
    their own project tree.

    Args:
        run: The completed job run.
        output: The file output configuration.
        working_dir: Optional job working directory used as both the
            anchor for relative paths and an additional allow-listed root.
    """
    import tempfile

    import aiofiles

    file_path = _render_output_field(output.file_path, run)
    if not file_path:
        logger.warning("File output for job %s has no file_path configured", run.job_id)
        return

    base = (working_dir or Path.cwd()).expanduser().resolve()
    raw = Path(file_path).expanduser()
    if not raw.is_absolute():
        raw = base / raw
    resolved = raw.resolve()

    allowed_roots = {
        Path.home().resolve(),
        Path(tempfile.gettempdir()).resolve(),
        Path.cwd().resolve(),
        base,
    }

    def _is_under(child: Path, parent: Path) -> bool:
        try:
            child.relative_to(parent)
        except ValueError:
            return False
        return True

    if not any(_is_under(resolved, root) for root in allowed_roots):
        logger.error(
            "File output for job %s rejected: path %s is outside allowed directories %s",
            run.job_id,
            resolved,
            sorted(str(r) for r in allowed_roots),
        )
        return

    resolved.parent.mkdir(parents=True, exist_ok=True)

    # Re-resolve AFTER mkdir to defeat a TOCTOU where the parent gets
    # replaced with a symlink between the validation and the open. If the
    # post-mkdir resolve no longer lies under an allowed root, abort.
    try:
        resolved_parent = resolved.parent.resolve(strict=True)
    except OSError:
        logger.error("File output for job %s: parent dir vanished after mkdir", run.job_id)
        return
    if not any(_is_under(resolved_parent, root) for root in allowed_roots):
        logger.error(
            "File output for job %s rejected: parent %s escaped allowed roots after mkdir",
            run.job_id,
            resolved_parent,
        )
        return

    final_path = resolved_parent / resolved.name
    mode = "a" if output.append else "w"
    async with aiofiles.open(final_path, mode=mode, encoding="utf-8") as f:
        header = f"--- {run.job_name} ({run.run_id}) at {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(run.started_at))} ---\n"
        await f.write(header + run.output + "\n")


async def _dispatch_webhook(run: JobRun, output: OutputConfig) -> None:
    """POST run output to a webhook URL.

    Delivery failures propagate to `run_job`, which records them in
    `run.dispatch_errors` (capped) so a failed POST is visible in the run
    record — not only in the log. (An earlier version swallowed `URLError`
    here, so the outer capture never saw network-target failures — the very
    targets most likely to fail. DMN-3b/v4.)

    Args:
        run: The completed job run.
        output: The webhook output configuration.
    """
    url = output.webhook_url
    if not url:
        logger.warning("Webhook output for job %s has no webhook_url configured", run.job_id)
        return

    payload = json.dumps({
        "run_id": run.run_id,
        "job_id": run.job_id,
        "job_name": run.job_name,
        "status": run.status.value,
        "output": run.output,
        "error": run.error,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
    }).encode()

    headers: dict[str, str] = {"Content-Type": "application/json", **output.webhook_headers}
    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")

    await asyncio.to_thread(_urlopen_blocking, req)


async def _dispatch_email(run: JobRun, output: OutputConfig) -> None:
    """Send run output via SMTP email.

    Uses STARTTLS for port 587, SSL for port 465, and plain SMTP otherwise.
    Runs the blocking smtplib call in a thread to avoid blocking the event loop.

    Args:
        run: The completed job run.
        output: The email output configuration.
    """
    to_addrs = output.to_addrs
    if not to_addrs:
        logger.warning("Email output for job %s has no to_addrs configured", run.job_id)
        return

    try:
        subject = output.subject_template.format(job_name=run.job_name) if output.subject_template else f"[bog-agents] {run.job_name} completed"
    except KeyError:
        # Unknown placeholder in template — fall back gracefully
        subject = f"[bog-agents] {run.job_name} completed"
    body = run.output or run.error or "(no output)"
    from_addr = output.from_addr or output.smtp_username or "bog-agents@localhost"

    def _send_blocking() -> None:
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = from_addr
        msg["To"] = ", ".join(to_addrs)

        host = output.smtp_host or "localhost"
        port = output.smtp_port

        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=15) as smtp:
                if output.smtp_username and output.smtp_password:
                    smtp.login(output.smtp_username, output.smtp_password)
                smtp.sendmail(from_addr, to_addrs, msg.as_string())
        else:
            with smtplib.SMTP(host, port, timeout=15) as smtp:
                if port != 25:
                    smtp.starttls()
                if output.smtp_username and output.smtp_password:
                    smtp.login(output.smtp_username, output.smtp_password)
                smtp.sendmail(from_addr, to_addrs, msg.as_string())

    # Delivery failures (SMTP auth/connection errors) propagate to run_job so
    # they land in run.dispatch_errors, not just the log. (DMN-3b/v4.)
    await asyncio.to_thread(_send_blocking)
    logger.debug("Email sent for job %s to %s", run.job_id, to_addrs)


async def _dispatch_slack(run: JobRun, output: OutputConfig) -> None:
    """POST run output to a Slack incoming webhook URL.

    Args:
        run: The completed job run.
        output: The Slack output configuration.
    """
    url = output.slack_webhook_url
    if not url:
        logger.warning("Slack output for job %s has no slack_webhook_url configured", run.job_id)
        return

    truncated = run.output[:1900] if run.output else run.error[:1900] if run.error else "(no output)"
    payload = json.dumps({"text": f"*{run.job_name}* completed ({run.status}):\n```{truncated}```"}).encode()

    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    # Delivery failures propagate to run_job → run.dispatch_errors. (DMN-3b/v4.)
    await asyncio.to_thread(_urlopen_blocking, req)
    logger.debug("Slack notification sent for job %s", run.job_id)


async def _dispatch_github_comment(run: JobRun, output: OutputConfig) -> None:
    """Post run output as a GitHub issue/PR comment via the REST API.

    Reads the token from output config first, then falls back to GITHUB_TOKEN
    and GITHUB_API_KEY environment variables.

    Args:
        run: The completed job run.
        output: The GitHub comment output configuration.
    """
    repo = output.github_repo
    raw_issue = _render_output_field(str(output.github_issue_or_pr or ""), run)
    try:
        issue_number = int(raw_issue) if raw_issue else 0
    except ValueError:
        logger.warning(
            "GitHub comment output for job %s could not resolve issue/PR number %r (unrendered placeholder?)",
            run.job_id,
            raw_issue,
        )
        return
    token = output.github_token or os.environ.get("GITHUB_TOKEN") or os.environ.get("GITHUB_API_KEY") or ""

    if not repo:
        logger.warning("GitHub comment output for job %s has no github_repo configured", run.job_id)
        return
    if not issue_number:
        logger.warning("GitHub comment output for job %s has no github_issue_or_pr configured", run.job_id)
        return
    if not token:
        logger.warning("GitHub comment output for job %s has no token (set GITHUB_TOKEN env var)", run.job_id)
        return

    owner_repo = repo.split("/", 1)
    if len(owner_repo) != 2:
        logger.warning("GitHub comment output for job %s: github_repo must be 'owner/repo', got %r", run.job_id, repo)
        return

    url = f"https://api.github.com/repos/{repo}/issues/{issue_number}/comments"
    body_text = run.output or run.error or "(no output)"
    payload = json.dumps({"body": f"## bog-agents: {run.job_name}\n\n{body_text}"}).encode()

    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        method="POST",
    )

    # Delivery failures propagate to run_job → run.dispatch_errors. (DMN-3b/v4.)
    await asyncio.to_thread(_urlopen_blocking, req)
    logger.debug("GitHub comment posted for job %s on %s#%d", run.job_id, repo, issue_number)


def _urlopen_blocking(req: urllib.request.Request) -> None:
    """Open a URL request in a blocking manner (intended for to_thread use).

    Args:
        req: A `urllib.request.Request` object to open.
    """
    import urllib.request

    with urllib.request.urlopen(req, timeout=10):
        pass
