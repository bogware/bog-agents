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
            "/workflow",
            "Agent-authored workflows saved as /commands: author, list, run, resume, status",
            "workflow phases fan-out team budget author yaml command",
            "enterprise",
            available=True,
        ),
        handler_method="_handle_workflow_command",
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
    SlashCommand(
        spec=SlashCommandSpec(
            "/actionlog",
            "Hash-chained action log: verify a chain, export it signed with the TraceFile key, prune old chains",
            "compliance audit chain hash export sign retention",
            "enterprise",
            available=True,
            subcommands=(
                ("status", "List chains and whether each verifies (default)"),
                ("verify", "Verify one chain (usage: /actionlog verify [file])"),
                (
                    "export",
                    "Write a signed export (usage: /actionlog export [file] [--unsigned])",
                ),
                (
                    "prune",
                    "Delete chains past the retention policy (usage: /actionlog prune [--days N])",
                ),
            ),
        ),
        handler_method="_handle_actionlog_command",
    ),
)
