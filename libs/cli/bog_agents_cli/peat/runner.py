"""Peat sub-agent runner.

Two surfaces:

1. **Interactive** — :func:`build_interactive_prompt` produces a prompt
   the regular agent can execute with full tools. The CLI's ``/peat`` chat
   handler calls this. Auto-mode rules apply normally.

2. **Scheduled** — :func:`run_scheduled_job` is the callback the
   :class:`PeatScheduler` invokes. It runs a sub-agent with a curated
   read+write toolset (no shell, no destructive ops) and writes the
   result to ``~/.bog-agents/peat/runs/<job_id>/<run_id>.md``.

The asymmetry matters: scheduled jobs run unattended, often while the
user is away from the keyboard. We don't want them to delete files,
shell out, or post anywhere external on their own.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING

from bog_agents_cli.peat.jobs import PeatJob, PeatJobRun

if TYPE_CHECKING:
    from bog_agents_cli.peat.persona import PeatPersona

logger = logging.getLogger(__name__)


# Tool names the scheduled subset is allowed to use. Read-mostly with a
# narrow write surface (only into peat/runs/, peat/digests/, peat/research/).
SCHEDULED_TOOL_ALLOWLIST: frozenset[str] = frozenset(
    {
        # File reading
        "read_file",
        "read_many_files",
        "list_directory",
        "glob",
        "grep",
        "search_files",
        "get_file_info",
        # Git read-only
        "git_status",
        "git_log",
        "git_diff",
        "git_show",
        # Plan / TODO management
        "write_todos",
        # Subagent dispatch — peat may decompose work via sub-tasks
        "task",
        # Writing — but only into the peat/ tree (enforced at the path layer
        # via the runner's prompt, not via tool whitelist)
        "write_file",
        "edit_file",
    }
)


# ---------------------------------------------------------------------------
# Prompt builders
# ---------------------------------------------------------------------------


def build_interactive_prompt(persona: PeatPersona, user_message: str) -> str:
    """Render Peat's interactive system context + the user message.

    The caller injects this as the agent's prompt for a ``/peat`` chat
    turn. The persona's full system prompt is rendered, then the user's
    message is appended.

    Args:
        persona: Resolved Peat persona.
        user_message: The user's free-form `/peat` arg.

    Returns:
        A complete prompt suitable for ``_send_prompt_to_agent``.
    """
    return f"{persona.to_system_prompt()}\n\n## Current request\n\n{user_message.strip()}\n"


def build_scheduled_prompt(persona: PeatPersona, job: PeatJob, run_id: str, run_dir: Path) -> str:
    """Render the prompt a scheduled job runs against.

    The prompt explicitly enumerates the scheduled-tool restrictions
    (no shell, no posting, write-only-into-the-peat-tree) so the agent
    self-enforces even if a user override loosens system-level rules.

    Args:
        persona: Resolved persona.
        job: The job being fired.
        run_id: Unique id for this firing.
        run_dir: Directory where the agent should write the run output.

    Returns:
        A prompt for the sub-agent.
    """
    # Forward-slash path string — agents (and write_file) accept either,
    # but a single style is easier to splice into the prompt and avoids
    # shell-quote ambiguity if the agent ever echoes it back.
    output_path_str = str(run_dir / f"{run_id}.md").replace("\\", "/")
    # Build the prompt with a single f-string and NO subsequent
    # ``.format()`` call — a leftover ``.format()`` here previously
    # crashed with ``KeyError`` whenever ``job.prompt`` contained literal
    # braces (JSON snippets, code samples, AGENTS.md fragments).
    return (
        f"{persona.to_system_prompt()}\n"
        "\n## Scheduled run — restricted mode\n\n"
        "You are running this turn as a scheduled job, unattended. The user is "
        "not at the keyboard. These rules apply for this turn only:\n\n"
        "1. **No shell, no subprocess.** Do not call `execute`/`run_command`/"
        "`shell` tools, even if they are technically available.\n"
        "2. **No external posts.** Do not call MCP tools that write to Slack, "
        "Jira, GitHub, email, etc. Read-only MCP calls are fine.\n"
        "3. **No destructive file operations.** Do not delete files, do not "
        "edit files outside the `peat/` directory tree.\n"
        "4. **Stay within budget.** Hard timeout: "
        f"{job.timeout_s} seconds. Stop when you have something useful, even "
        "if not perfect.\n"
        f"5. **Write your output here:** `{output_path_str}`. Use `write_file` "
        "with that exact path. The first line of the file should be a one-line "
        "summary the user can scan in their inbox.\n\n"
        "If you cannot complete the task within these rules, stop and write a "
        "short note explaining what you'd need (a credential, a confirmation, "
        "etc.) so the user can resolve it next time they open the CLI.\n\n"
        "## Job\n\n"
        f"**Name:** {job.name or job.job_id}\n"
        f"**Run id:** {run_id}\n\n"
        "**Task:**\n\n"
        f"{job.prompt.strip()}\n"
    )


# ---------------------------------------------------------------------------
# Scheduled runner
# ---------------------------------------------------------------------------


# The CLI hands the scheduler this kind of callback. It is async and
# returns the *raw text output* the agent produced. The runner here turns
# that into a PeatJobRun and writes the artifact file.
AgentInvokeFn = Callable[[str, frozenset[str]], Awaitable[str]]
"""Callback signature for invoking the agent sub-graph.

Args:
    prompt: The system+user prompt to run.
    allowed_tools: Set of tool names the agent is permitted to call. The
        CLI is responsible for actually filtering — this is informational.

Returns:
    The agent's final text output.
"""


async def run_scheduled_job(
    job: PeatJob,
    *,
    persona: PeatPersona,
    config_dir: Path,
    invoke_agent: AgentInvokeFn,
) -> PeatJobRun:
    """Execute a single scheduled job. Returns the completed run record.

    This is what the CLI registers with :class:`PeatScheduler` as its
    runner callback. The CLI provides ``invoke_agent`` (which knows how
    to talk to the running langgraph server).

    Args:
        job: The job to fire.
        persona: Resolved persona.
        config_dir: Bog-agents user config dir (``~/.bog-agents``).
        invoke_agent: Callback that runs the agent with a given prompt
            and tool allowlist, returning the final text.

    Returns:
        Completed :class:`PeatJobRun`.
    """
    run_id = f"run-{int(time.time())}-{uuid.uuid4().hex[:4]}"
    run_dir = config_dir / "peat" / "runs" / job.job_id
    run_dir.mkdir(parents=True, exist_ok=True)
    output_path = run_dir / f"{run_id}.md"

    started = time.time()
    prompt = build_scheduled_prompt(persona, job, run_id, run_dir)
    try:
        text = await invoke_agent(prompt, SCHEDULED_TOOL_ALLOWLIST)
    except Exception as exc:
        logger.exception("peat scheduled job %s failed in agent invocation", job.job_id)
        return PeatJobRun(
            job_id=job.job_id,
            run_id=run_id,
            started_at=started,
            duration_s=time.time() - started,
            status="fail",
            error=f"agent invoke: {exc.__class__.__name__}: {exc}",
        )

    duration = time.time() - started

    # If the agent didn't write the artifact via write_file, persist its
    # final text directly. This is a fallback — Peat is instructed to use
    # write_file but local models occasionally skip the tool call.
    if not output_path.exists() and text:
        with output_path.open("w", encoding="utf-8") as fh:
            fh.write(text)
            if persona.sign_off:
                fh.write("\n\n")
                fh.write(persona.sign_off)
                fh.write("\n")

    summary = _first_line(text or _safe_read_first_line(output_path))
    return PeatJobRun(
        job_id=job.job_id,
        run_id=run_id,
        started_at=started,
        duration_s=duration,
        status="ok",
        summary=summary,
        output_path=str(output_path),
    )


def _first_line(text: str | None) -> str:
    if not text:
        return ""
    for line in text.splitlines():
        s = line.strip()
        if s:
            return s[:200]
    return ""


def _safe_read_first_line(path: Path) -> str:
    if not path.is_file():
        return ""
    try:
        with path.open("r", encoding="utf-8") as fh:
            for _ in range(20):  # scan first 20 lines
                line = fh.readline()
                if not line:
                    break
                s = line.strip()
                if s:
                    return s[:200]
    except OSError:
        return ""
    return ""
