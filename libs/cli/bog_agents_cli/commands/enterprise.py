"""Enterprise / multi-repo / team coordination commands."""

from __future__ import annotations

from bog_agents_cli._spec import SlashCommandSpec
from bog_agents_cli.commands._base import SlashCommand

COMMANDS: tuple[SlashCommand, ...] = (
    SlashCommand(
        spec=SlashCommandSpec(
            "/team",
            "Team shared config + `/team run` to run a governed agent team over a task ledger",
            "enterprise org swarm coordination invite members shared context prompts config run ledger workers",
            "enterprise",
            available=True,
        ),
        handler_method="_handle_team_command",
    ),
    SlashCommand(
        spec=SlashCommandSpec(
            "/workspace",
            "Multi-repository context — define repos in .bog-agents/workspace.toml",
            "multi-repo cross-repo microservices monorepo symbol resolution",
            "enterprise",
            available=True,
        ),
        handler_method="_handle_workspace_command",
    ),
    SlashCommand(
        spec=SlashCommandSpec(
            "/worktrees",
            "Manage parallel git worktrees — spawn agents in isolated branches",
            "parallel agents git worktree branch isolation conflict merge",
            "enterprise",
            available=True,
        ),
        handler_method="_handle_worktrees_command",
    ),
)
