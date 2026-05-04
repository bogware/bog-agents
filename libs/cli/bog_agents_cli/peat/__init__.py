"""Peat — your personal bog-agents assistant.

Peat is a long-lived sub-agent with a hand-crafted persona that runs
inside the CLI. He can hold a chat, schedule recurring jobs, do deep
product research, build personal digests from existing /qa results and
/replay recordings, and more.

Architecture (chosen via design discussion 2026-05-03):

- **In-process scheduler**: Peat jobs run while the CLI is open. They
  persist to disk so they survive restart, but they only fire while you
  have the app open. Anything fired-and-undelivered goes to the inbox
  (``~/.bog-agents/peat/inbox.json``) and is shown next time the CLI
  starts.
- **Hybrid tool surface**: interactive Peat gets the same tools as the
  main agent (fully empowered). Scheduled Peat jobs get a curated
  read+write subset — no shell — because cron jobs run unattended and
  shell-from-cron has a long history of nasty surprises.
- **Auto-mode asymmetry**: interactive Peat respects the same auto-mode
  rules as the regular agent (ask on destructive ops). Scheduled jobs
  are forced into a more restrictive "always-ask-but-no-one-here"
  posture by simply forbidding the destructive tool patterns up front.
- **Persona**: hand-crafted default in :mod:`bog_agents_cli.peat.persona`,
  fully overridable via settings (``~/.bog-agents/settings.json``,
  ``peat`` section).

File layout::

    ~/.bog-agents/peat/
        jobs/<job_id>.yaml      # scheduled jobs (recurring or one-shot)
        runs/<job_id>/          # per-run artifacts (each fired job's output)
        inbox.json              # buffered notifications for next CLI start
        research/<topic>.md     # deep-research reports
        digests/<date>.md       # weekly/ad-hoc digests
"""

from bog_agents_cli.peat.jobs import (
    PeatJob,
    PeatJobRun,
    delete_job,
    find_job,
    list_jobs,
    load_job,
    save_job,
)
from bog_agents_cli.peat.persona import (
    DEFAULT_PEAT_PERSONA,
    INBOX_FORMAT,
    PeatPersona,
    load_persona,
)
from bog_agents_cli.peat.research import (
    build_digest_prompt,
    build_research_prompt,
    collect_digest_inputs,
)
from bog_agents_cli.peat.runner import (
    SCHEDULED_TOOL_ALLOWLIST,
    build_interactive_prompt,
    build_scheduled_prompt,
    run_scheduled_job,
)
from bog_agents_cli.peat.scheduler import (
    PeatScheduler,
    append_inbox,
    clear_inbox,
    next_fire_time,
    read_inbox,
)

__all__ = [
    "DEFAULT_PEAT_PERSONA",
    "INBOX_FORMAT",
    "SCHEDULED_TOOL_ALLOWLIST",
    "PeatJob",
    "PeatJobRun",
    "PeatPersona",
    "PeatScheduler",
    "append_inbox",
    "build_digest_prompt",
    "build_interactive_prompt",
    "build_research_prompt",
    "build_scheduled_prompt",
    "clear_inbox",
    "collect_digest_inputs",
    "delete_job",
    "find_job",
    "list_jobs",
    "load_job",
    "load_persona",
    "next_fire_time",
    "read_inbox",
    "run_scheduled_job",
    "save_job",
]
