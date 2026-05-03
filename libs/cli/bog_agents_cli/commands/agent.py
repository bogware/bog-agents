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
        ),
        handler_method="_dispatch_background_command",
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
)
