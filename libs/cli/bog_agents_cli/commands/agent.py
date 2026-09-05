"""Agent / multi-agent orchestration commands."""

from __future__ import annotations

from bog_agents_cli._spec import SlashCommandSpec
from bog_agents_cli.commands._base import SlashCommand

COMMANDS: tuple[SlashCommand, ...] = (
    SlashCommand(
        spec=SlashCommandSpec(
            "/agent",
            "Manage parallel agent threads and worktrees (list/spawn/panel/switch/stop)",
            "thread multi parallel worktree watch dashboard live status panel",
            "agent",
            available=True,
        ),
        handler_method="_handle_agent_command",
    ),
    SlashCommand(
        spec=SlashCommandSpec(
            "/ambient",
            "Manage the ambient agent daemon — autonomous scheduled and triggered agents",
            "daemon service schedule cron background autonomous trigger webhook",
            "agent",
            available=True,
        ),
        handler_method="_handle_ambient_command",
    ),
    SlashCommand(
        spec=SlashCommandSpec(
            "/background",
            "Manage background agent tasks (run/list/cancel/status)",
            "bg task async",
            "agent",
            available=True,
            subcommands=(
                ("list", "Show all background tasks"),
                ("status", "Show detail for a task (usage: /background status <id>)"),
                ("cancel", "Cancel a running task (usage: /background cancel <id>)"),
                ("cleanup", "Remove finished tasks from the table"),
            ),
        ),
        handler_method="_dispatch_background_command",
    ),
    SlashCommand(
        spec=SlashCommandSpec(
            "/tasks",
            "Command center: every thread, queued prompt, background/team/daemon task in one tree, with kill/steer/pause/diff",
            "tasks tree kill steer pause resume queue waiting status center recap",
            "agent",
            available=True,
            subcommands=(
                ("list", "Show the task tree (default)"),
                ("kill", "Stop a task (usage: /tasks kill <id>)"),
                (
                    "steer",
                    "Send instructions to a task (usage: /tasks steer <id> <text>)",
                ),
                (
                    "pause",
                    "Pause a team run: no new tasks are claimed (usage: /tasks pause <id>)",
                ),
                ("resume", "Resume a paused team run (usage: /tasks resume <id>)"),
                ("diff", "Show a task's worktree diff (usage: /tasks diff <id>)"),
                (
                    "queue",
                    "List, edit or drop queued prompts (usage: /tasks queue [edit <n> <text>|drop <n>])",
                ),
                ("recap", "Same as /recap"),
            ),
        ),
        handler_method="_handle_tasks_command",
    ),
    SlashCommand(
        spec=SlashCommandSpec(
            "/subtask",
            "Run a prompt in the background with this conversation as context (a fork of this agent)",
            "subtask fork background side task context",
            "agent",
            available=True,
        ),
        handler_method="_handle_fork_command",
    ),
    SlashCommand(
        spec=SlashCommandSpec(
            "/fork",
            "Record a fork of this session and continue the work in a background agent (--worktree for a fresh worktree)",
            "fork branch worktree background continue",
            "agent",
            available=True,
        ),
        handler_method="_handle_fork_command",
    ),
    SlashCommand(
        spec=SlashCommandSpec(
            "/async",
            "Fire-and-forget agent task — submit, get a job id, "
            "get a toast on completion",
            "background async detached fire-and-forget delegate task",
            "agent",
            available=True,
            subcommands=(
                ("<prompt>", "Submit a prompt to run in the background"),
                ("list", "Show all async tasks (alias for /background list)"),
                (
                    "wait <id> [timeout]",
                    "Block until <id> finishes, then show the result inline",
                ),
                ("status <id>", "Show one task's status"),
                ("cancel <id>", "Cancel a running task"),
            ),
        ),
        handler_method="_handle_async_command",
    ),
    SlashCommand(
        spec=SlashCommandSpec(
            "/butcher",
            "Slice a big job into foolproof vertical cuts and run them on weak worker models",
            "slice decompose vertical worker local ollama cheap delegate split carve",
            "agent",
            available=True,
            subcommands=(
                (
                    "<job>",
                    "Plan + execute the job (slices run sequentially, each verified)",
                ),
                ("list", "Show job directories under .bog-agents/butcher/"),
                ("show", "Show a job's report (usage: /butcher show <job-id>)"),
                ("config", "Show butcher/worker model configuration"),
            ),
        ),
        handler_method="_handle_butcher_command",
    ),
    SlashCommand(
        spec=SlashCommandSpec(
            "/dashboard",
            "Show the multi-agent dashboard with status and costs",
            "agents monitor panel",
            "agent",
            available=True,
        ),
        handler_method="_dispatch_dashboard_command",
    ),
    SlashCommand(
        spec=SlashCommandSpec(
            "/jobs",
            "Manage autonomous background jobs — list, attach, cancel, and monitor long-running tasks",
            "background async detached autonomous worktree notification webhook",
            "agent",
            available=True,
        ),
        handler_method="_handle_jobs_command",
    ),
    SlashCommand(
        spec=SlashCommandSpec(
            "/jtbd",
            "Jobs To Be Done — uncover the real job, execute against measurable outcomes",
            "jobs job-to-be-done outcome interview hire fire progress spec goal why",
            "agent",
            available=True,
            subcommands=(
                (
                    "<request>",
                    "Start the interview → Job Spec → outcome-driven execution",
                ),
                ("status", "Show the pending interview or active Job Spec"),
                ("verify", "Score the session's work against the active Job Spec"),
                ("cancel", "Abandon a pending interview"),
            ),
        ),
        handler_method="_handle_jtbd_command",
    ),
    SlashCommand(
        spec=SlashCommandSpec(
            "/operator",
            "Judge each prompt with an operator model and route it to easy/medium/hard/max tiers",
            "route routing judge dispatch tier model cascade preset local bedrock cheap",
            "agent",
            available=True,
            subcommands=(
                ("on", "Enable routing (reloads operator.toml)"),
                ("off", "Disable routing"),
                ("status", "Show tier map and recent routing decisions"),
                (
                    "preset",
                    "Switch preset (usage: /operator preset <anthropic|bedrock|local|hybrid|…>)",
                ),
                (
                    "force",
                    "Force the next prompt's tier (usage: /operator force <tier>)",
                ),
                ("test", "Dry-run the judge on a prompt without an agent turn"),
                ("config", "Show (and bootstrap) ~/.bog-agents/operator.toml"),
            ),
        ),
        handler_method="_handle_operator_command",
    ),
    SlashCommand(
        spec=SlashCommandSpec(
            "/race",
            "Fan a prompt out to N models in parallel; surface side-by-side and a suggested winner",
            "parallel multi-model fanout race fleet flagship worktree compete",
            "agent",
            available=True,
        ),
        handler_method="_handle_race_command",
    ),
    SlashCommand(
        spec=SlashCommandSpec(
            "/standing-orders",
            "Browse and install curated daemon-job templates (PR summary, bug finder, etc.)",
            "ambient daemon templates standing orders catalog flagship",
            "agent",
            available=True,
            subcommands=(
                ("list", "Show all available templates"),
                (
                    "install",
                    "Install a template (usage: /standing-orders install <id>)",
                ),
            ),
        ),
        handler_method="_handle_standing_orders_command",
    ),
)
