"""Version-control commands."""

from __future__ import annotations

from bog_agents_cli._spec import SlashCommandSpec
from bog_agents_cli.commands._base import SlashCommand

COMMANDS: tuple[SlashCommand, ...] = (
    SlashCommand(
        spec=SlashCommandSpec(
            "/branch",
            "Manage git branches for local workflows",
            "git checkout switch create",
            "git",
            available=True,
        ),
        handler_method="_handle_branch_command",
    ),
    SlashCommand(
        spec=SlashCommandSpec(
            "/diff",
            "Show pending file changes as a diff",
            "changes git",
            "git",
            available=True,
        ),
        handler_method="_handle_diff_command",
    ),
    SlashCommand(
        spec=SlashCommandSpec(
            "/changes",
            "Turn-end changes tray: files in explanatory order, per-file diff, per-hunk revert",
            "tray diff review revert hunk changeset",
            "git",
            available=True,
            subcommands=(
                ("show", "Show one file's diff (usage: /changes show <n>)"),
                (
                    "revert",
                    "Revert a file or one hunk (usage: /changes revert <n> [hunk])",
                ),
                ("keep", "Keep everything and clear the tray"),
            ),
        ),
        handler_method="_handle_changes_command",
    ),
    SlashCommand(
        spec=SlashCommandSpec(
            "/pr",
            "Pull request management (create/list/review)",
            "github merge",
            "git",
            available=True,
        ),
        handler_method="_handle_pr_command",
    ),
    SlashCommand(
        spec=SlashCommandSpec(
            "/resolve",
            "AI-assisted merge conflict resolution",
            "conflict merge",
            "git",
            available=True,
        ),
        handler_method="_handle_resolve_command",
    ),
    SlashCommand(
        spec=SlashCommandSpec(
            "/undo",
            "Inspect or restore tracked file changes with git",
            "revert rollback",
            "git",
            available=True,
        ),
        handler_method="_handle_undo_command",
    ),
    SlashCommand(
        spec=SlashCommandSpec(
            "/worktree",
            "Manage git worktrees for isolated work",
            "isolate parallel",
            "git",
            available=True,
        ),
        handler_method="_handle_worktree_command",
    ),
)
