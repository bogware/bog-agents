"""Execute AmbientJob tasks via create_agent()."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import smtplib
import time
import urllib.error
import urllib.request
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
from bog_agents_daemon.store import save_run, upsert_job

logger = logging.getLogger(__name__)


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
        prompt = _build_prompt(job)
        output = await _invoke_agent(job, prompt)
        run.output = output
        run.status = JobStatus.COMPLETED
    except Exception as exc:
        logger.exception("Job %s (%s) failed", job.job_id, job.name)
        run.error = str(exc)
        run.status = JobStatus.FAILED
    finally:
        run.finished_at = time.time()

    # Update job state
    job.last_run_at = run.started_at
    job.last_status = run.status
    job.last_output = run.output[:500] if run.output else run.error[:500]
    job.run_count += 1

    upsert_job(job)
    save_run(run)

    # Dispatch outputs best-effort. Capture per-target failures on the
    # run so an operator can tell from the run record that delivery
    # failed even though the agent itself completed successfully. The
    # log line alone wasn't enough — silent dispatch outages turned
    # into "why didn't the daily report arrive?" tickets with no
    # paper trail in the runs table.
    job_wd = Path(job.working_dir) if job.working_dir else None
    for output_config in job.outputs:
        try:
            await _dispatch_output(run, output_config, working_dir=job_wd)
        except Exception as exc:
            logger.exception(
                "Output dispatch failed for job %s target %s",
                job.job_id,
                output_config.target,
            )
            run.dispatch_errors.append(
                {"target": str(output_config.target), "error": str(exc)}
            )

    # If the agent run succeeded but dispatches failed, mark the run as
    # COMPLETED but keep ``error`` populated so HTTP clients and the
    # ``/runs`` CLI surface flag the partial failure. Status stays
    # COMPLETED to reflect that the work was done — the failure is in
    # delivery, not execution — but a non-empty ``error`` is the
    # universal signal callers already check.
    if run.dispatch_errors and run.status == JobStatus.COMPLETED and not run.error:
        run.error = (
            f"agent succeeded but {len(run.dispatch_errors)} output "
            f"target(s) failed; see dispatch_errors for details"
        )
        save_run(run)

    return run


def _build_prompt(job: AmbientJob) -> str:
    """Build the prompt string for a job invocation.

    For raw `prompt` jobs, the prompt is returned verbatim. For
    `skill_name` jobs, the skill's SKILL.md content is read from
    `~/.bog-agents/{agent}/skills/{skill}/SKILL.md` and inlined as the
    prompt body so the model has the full skill instructions, not just
    the skill name. For `pipeline_name` jobs, the YAML definition is
    loaded and converted to a structured request.

    Args:
        job: The job whose prompt to resolve.

    Returns:
        The resolved prompt string.

    Raises:
        ValueError: If no prompt source is configured or the named
            skill/pipeline can't be located.
    """
    if job.prompt:
        return job.prompt
    if job.skill_name:
        return _resolve_skill_prompt(job.skill_name)
    if job.pipeline_name:
        return _resolve_pipeline_prompt(job.pipeline_name)
    msg = f"Job '{job.name}' ({job.job_id}) has no prompt, skill, or pipeline configured"
    raise ValueError(msg)


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


def _parse_skill_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    """Extract YAML frontmatter from a SKILL.md.

    Args:
        content: Raw SKILL.md text.

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
    except yaml.YAMLError:
        frontmatter = {}
    if not isinstance(frontmatter, dict):
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
    frontmatter, body = _parse_skill_frontmatter(raw)

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


async def _invoke_agent(job: AmbientJob, prompt: str) -> str:
    """Invoke create_agent() with the job configuration and capture output.

    Uses a lazy import to avoid circular imports at module load time.  Enforces
    a per-job timeout (default 30 minutes, override with BOG_DAEMON_AGENT_TIMEOUT).

    Args:
        job: The job providing model and working_dir configuration.
        prompt: The resolved prompt to run.

    Returns:
        The last AI message content from the agent.

    Raises:
        TimeoutError: If the agent does not complete within the allowed time.
    """
    from bog_agents import create_agent
    from bog_agents.backends.local_shell import LocalShellBackend

    # Root the agent's filesystem and shell at the job's working_dir so
    # skills/pipelines that read or grep project files actually work. Without
    # this the SDK falls back to StateBackend and the agent reports "no files
    # are mounted" even when --working-dir was set.
    root_dir = Path(job.working_dir) if job.working_dir else Path.cwd()
    # virtual_mode=False so the agent operates on real absolute paths inside
    # the project tree (mirrors how the CLI wires its LocalShellBackend).
    backend = LocalShellBackend(root_dir=root_dir, inherit_env=True, env=os.environ.copy(), virtual_mode=False)

    kwargs: dict[str, Any] = {
        "enable_git_tools": True,
        "backend": backend,
        "working_dir": str(root_dir),
    }
    if job.model:
        kwargs["model"] = job.model

    agent = create_agent(**kwargs)

    async def _stream() -> str:
        result_output = ""
        async for chunk in agent.astream({"messages": [("human", prompt)]}):
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
        return result_output

    try:
        return await asyncio.wait_for(_stream(), timeout=_AGENT_TIMEOUT_SECONDS)
    except TimeoutError:
        msg = f"Agent timed out after {_AGENT_TIMEOUT_SECONDS}s for job {job.job_id} ({job.name})"
        raise TimeoutError(msg) from None


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

    file_path = output.file_path
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

    Args:
        run: The completed job run.
        output: The webhook output configuration.
    """
    import json
    import urllib.error

    url = output.webhook_url
    if not url:
        logger.warning("Webhook output for job %s has no webhook_url configured", run.job_id)
        return

    payload = json.dumps(
        {
            "run_id": run.run_id,
            "job_id": run.job_id,
            "job_name": run.job_name,
            "status": run.status.value,
            "output": run.output,
            "error": run.error,
            "started_at": run.started_at,
            "finished_at": run.finished_at,
        }
    ).encode()

    headers: dict[str, str] = {"Content-Type": "application/json", **output.webhook_headers}
    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")

    import asyncio

    try:
        await asyncio.to_thread(_urlopen_blocking, req)
    except urllib.error.URLError:
        logger.exception("Webhook dispatch failed for job %s", run.job_id)


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

    try:
        await asyncio.to_thread(_send_blocking)
        logger.debug("Email sent for job %s to %s", run.job_id, to_addrs)
    except Exception:
        logger.exception("Email dispatch failed for job %s", run.job_id)


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

    try:
        await asyncio.to_thread(_urlopen_blocking, req)
        logger.debug("Slack notification sent for job %s", run.job_id)
    except urllib.error.URLError:
        logger.exception("Slack dispatch failed for job %s", run.job_id)


async def _dispatch_github_comment(run: JobRun, output: OutputConfig) -> None:
    """Post run output as a GitHub issue/PR comment via the REST API.

    Reads the token from output config first, then falls back to GITHUB_TOKEN
    and GITHUB_API_KEY environment variables.

    Args:
        run: The completed job run.
        output: The GitHub comment output configuration.
    """
    repo = output.github_repo
    issue_number = output.github_issue_or_pr
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

    try:
        await asyncio.to_thread(_urlopen_blocking, req)
        logger.debug("GitHub comment posted for job %s on %s#%d", run.job_id, repo, issue_number)
    except urllib.error.URLError:
        logger.exception("GitHub comment dispatch failed for job %s", run.job_id)


def _urlopen_blocking(req: urllib.request.Request) -> None:
    """Open a URL request in a blocking manner (intended for to_thread use).

    Args:
        req: A `urllib.request.Request` object to open.
    """
    import urllib.request

    with urllib.request.urlopen(req, timeout=10):
        pass
