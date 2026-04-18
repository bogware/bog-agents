"""Execute AmbientJob tasks via create_agent()."""

from __future__ import annotations

import logging
import time
import urllib.request
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
) -> JobRun:
    """Execute an AmbientJob and persist the result.

    Creates a JobRun record, invokes the agent with the job's prompt, captures
    the output, updates the job's run history, and dispatches to all configured
    output targets.

    Args:
        job: The job to execute.
        trigger_type: How this execution was initiated.
        trigger_context: Optional metadata from the trigger (e.g. webhook payload).

    Returns:
        The completed JobRun record.
    """
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

    # Dispatch outputs best-effort
    for output_config in job.outputs:
        try:
            await _dispatch_output(run, output_config)
        except Exception:
            logger.exception(
                "Output dispatch failed for job %s target %s",
                job.job_id,
                output_config.target,
            )

    return run


def _build_prompt(job: AmbientJob) -> str:
    """Build the prompt string for a job invocation.

    Args:
        job: The job whose prompt to resolve.

    Returns:
        The resolved prompt string.

    Raises:
        ValueError: If no prompt source is configured.
    """
    if job.prompt:
        return job.prompt
    if job.skill_name:
        return f"Run the skill named '{job.skill_name}'."
    if job.pipeline_name:
        return f"Run the pipeline named '{job.pipeline_name}'."
    msg = f"Job '{job.name}' ({job.job_id}) has no prompt, skill, or pipeline configured"
    raise ValueError(msg)


async def _invoke_agent(job: AmbientJob, prompt: str) -> str:
    """Invoke create_agent() with the job configuration and capture output.

    Uses a lazy import to avoid circular imports at module load time.

    Args:
        job: The job providing model and working_dir configuration.
        prompt: The resolved prompt to run.

    Returns:
        The last AI message content from the agent.
    """
    from bog_agents import create_agent

    kwargs: dict[str, Any] = {"enable_git_tools": True}
    if job.model:
        kwargs["model"] = job.model
    if job.working_dir:
        kwargs["working_dir"] = job.working_dir

    agent = create_agent(**kwargs)

    result_output = ""
    async for chunk in agent.astream({"messages": [("human", prompt)]}):
        for node_output in chunk.values():
            messages = node_output.get("messages", [])
            for msg in messages:
                content = getattr(msg, "content", None)
                if content and hasattr(msg, "type") and msg.type == "ai":
                    result_output = content if isinstance(content, str) else str(content)

    return result_output


async def _dispatch_output(run: JobRun, output: OutputConfig) -> None:
    """Send run output to a configured target.

    Args:
        run: The completed job run with output to dispatch.
        output: The output destination configuration.
    """
    target = output.target

    if target == OutputTarget.FILE:
        await _dispatch_file(run, output)
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
    else:
        logger.debug(
            "Output target %s not yet implemented (phase 2 stub)", target.value
        )


async def _dispatch_file(run: JobRun, output: OutputConfig) -> None:
    """Write run output to a file.

    Args:
        run: The completed job run.
        output: The file output configuration.
    """
    import aiofiles

    file_path = output.file_path
    if not file_path:
        logger.warning("File output for job %s has no file_path configured", run.job_id)
        return

    mode = "a" if output.append else "w"
    async with aiofiles.open(file_path, mode=mode, encoding="utf-8") as f:
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


def _urlopen_blocking(req: urllib.request.Request) -> None:
    """Open a URL request in a blocking manner (intended for to_thread use).

    Args:
        req: A `urllib.request.Request` object to open.
    """
    import urllib.request

    with urllib.request.urlopen(req, timeout=10):
        pass
