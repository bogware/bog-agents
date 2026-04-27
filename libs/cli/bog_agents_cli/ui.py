"""Help screens and argparse utilities for the CLI.

This module is imported at CLI startup to wire `-h` actions into the
argparse tree.  It must stay lightweight — no SDK or langchain imports.
"""

import argparse
from collections.abc import Callable

from bog_agents_cli._version import __version__
from bog_agents_cli.command_registry import (
    FEATURED_HELP_COMMANDS_LEFT,
    FEATURED_HELP_COMMANDS_RIGHT,
    describe_commands,
)
from bog_agents_cli.config import (
    COLORS,
    DOCS_URL,
    _is_editable_install,
    console,
)

_JSON_OPTION_LINE = "  --json                  Emit machine-readable JSON"
_HELP_OPTION_LINE = "  -h, --help              Show this help message"


def build_help_parent(
    help_fn: Callable[[], None],
    make_help_action: Callable[[Callable[[], None]], type[argparse.Action]],
) -> list[argparse.ArgumentParser]:
    """Build a parent parser whose `-h` invokes *help_fn*.

    This eliminates boilerplate: without the helper every `add_parser`
    call would need its own three-line parent-parser setup.  Used by both
    `main.parse_args` and `skills.commands.setup_skills_parser`.

    Args:
        help_fn: Zero-argument callable that renders a Rich help screen.
        make_help_action: Factory that turns *help_fn* into an argparse
            Action class (see `main._make_help_action`).

    Returns:
        Single-element list suitable for the `parents` kwarg of
        `add_parser`.
    """
    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument("-h", "--help", action=make_help_action(help_fn))
    return [parent]


def _print_option_section(*lines: str, title: str = "Options") -> None:
    """Print a help-screen options section with shared JSON/help flags.

    Args:
        *lines: Command-specific option lines to print before the shared flags.
        title: Section title to display.
    """
    console.print(f"[bold]{title}:[/bold]", style=COLORS["primary"])
    for line in lines:
        console.print(line)
    console.print(_JSON_OPTION_LINE)
    console.print(_HELP_OPTION_LINE)


def show_help() -> None:
    """Show top-level help information for the bog-agents CLI."""
    install_type = " (local)" if _is_editable_install() else ""
    banner_color = (
        COLORS["primary_dev"] if _is_editable_install() else COLORS["primary"]
    )
    console.print()
    console.print(
        f"[bold {banner_color}]bog-agents[/bold {banner_color}]"
        f" v{__version__}{install_type}"
        f"  [dim]— the trail's marked, saddle up[/dim]"
    )
    console.print()
    console.print(
        f"  Docs: [link={DOCS_URL}]{DOCS_URL}[/link]",
        style=COLORS["dim"],
    )
    console.print()

    # --- Usage ---
    console.print("[bold]Usage:[/bold]", style=COLORS["primary"])
    console.print(
        "  bog-agents                                     Start interactive session"
    )
    console.print(
        "  bog-agents -n 'Fix the failing tests'          Run a task and exit"
    )
    console.print(
        "  bog-agents -p 'Explain this function' < f.py   Pipe input, clean output"
    )
    console.print(
        "  bog-agents -r                                  Resume last conversation"
    )
    console.print()

    # --- Subcommands ---
    console.print("[bold]Subcommands:[/bold]", style=COLORS["primary"])
    console.print("  list                                List available agents")
    console.print("  reset --agent NAME [--target SRC]   Reset an agent's prompt")
    console.print("  skills <list|create|info|delete>     Manage agent skills")
    console.print("  threads <list|delete>                Manage conversation threads")
    console.print()

    # --- Core Options ---
    console.print("[bold]Core Options:[/bold]", style=COLORS["primary"])
    console.print(
        "  -M, --model MODEL          Model (e.g., claude-sonnet-4-6, gpt-4o, ollama:llama3)"
    )
    console.print("  -a, --agent NAME           Named agent to use (default: agent)")
    console.print(
        "  -r, --resume [ID]          Resume: -r for most recent, -r ID for specific"
    )
    console.print("  -m, --message TEXT         Auto-submit this prompt on start")
    console.print(
        "  --auto-approve             Auto-approve all tool calls (Shift+Tab to toggle)"
    )
    console.print(
        "  --auto-commit              Auto-commit git changes after each agent turn"
    )
    console.print("  --doctor                   Diagnose your environment")
    console.print("  -v, --version              Show CLI and SDK versions")
    console.print("  -h, --help                 This help screen")
    console.print()

    # --- Non-Interactive / Automation ---
    console.print("[bold]Non-Interactive (Automation):[/bold]", style=COLORS["primary"])
    console.print("  -n, --non-interactive MSG  Run single task, exit with code 0/1")
    console.print(
        "  -p, --print TEXT           Same as -n + -q (clean stdout for pipes)"
    )
    console.print("  -q, --quiet                Suppress chrome, pipe-friendly output")
    console.print("  --no-stream                Buffer full response (don't stream)")
    console.print("  --json                     Machine-readable JSON output")
    console.print(
        "  --prompt NAME              Run a saved prompt from prompt_library.toml"
    )
    console.print(
        "  --prompt-vars JSON         JSON object of {var: value} for --prompt"
    )
    console.print(
        "  --pipeline NAME            Run a saved pipeline from .bog-agents/pipelines/"
    )
    console.print(
        "  --shell-allow-list CMDS    Shell access: 'recommended', 'all', or comma list"
    )
    console.print(
        "  --pr                       Create a PR from agent output (needs -n)"
    )
    console.print("  --pr-base BRANCH           PR base branch (default: main)")
    console.print("  --pr-draft                 Create PR as draft")
    console.print()

    # --- Model Configuration ---
    console.print("[bold]Model Configuration:[/bold]", style=COLORS["primary"])
    console.print(
        "  --model-params JSON        Extra kwargs (e.g., '{\"temperature\": 0.7}')"
    )
    console.print("  --profile-override JSON    Override model profile fields")
    console.print("  --default-model [MODEL]    Set or show the default model")
    console.print("  --clear-default-model      Clear default model")
    console.print()

    # --- Sandboxes & MCP ---
    console.print("[bold]Sandboxes & MCP:[/bold]", style=COLORS["primary"])
    console.print(
        "  --sandbox TYPE             Remote sandbox (modal/daytona/runloop/langsmith)"
    )
    console.print("  --sandbox-id ID            Reuse an existing sandbox")
    console.print(
        "  --sandbox-setup PATH       Run setup script after sandbox creation"
    )
    console.print(
        "  --mcp-config PATH          MCP server config (merged with auto-discovered)"
    )
    console.print("  --no-mcp                   Disable all MCP loading")
    console.print(
        "  --trust-project-mcp        Trust project MCP configs without prompt"
    )
    console.print()

    # --- Server Modes ---
    console.print("[bold]Server Modes:[/bold]", style=COLORS["primary"])
    console.print("  --serve                    Start HTTP API server")
    console.print("  --serve-host HOST          Server host (default: 127.0.0.1)")
    console.print("  --serve-port PORT          Server port (default: 8420)")
    console.print("  --acp                      Run as ACP server over stdio")
    console.print()

    # --- Slash Commands ---
    console.print(
        "[bold]Slash Commands (inside interactive session):[/bold]",
        style=COLORS["primary"],
    )
    col1 = describe_commands(FEATURED_HELP_COMMANDS_LEFT)
    col2 = describe_commands(FEATURED_HELP_COMMANDS_RIGHT)
    for left, right in zip(col1, col2, strict=True):
        console.print(f"  {left[0]:<14} {left[1]:<28} {right[0]:<14} {right[1]}")
    console.print("  [dim]... and more. Type / to see all.[/dim]")
    console.print()

    # --- Examples ---
    console.print("[bold]Examples:[/bold]", style=COLORS["primary"])
    console.print(
        "  bog-agents                                      # Interactive session",
        style=COLORS["dim"],
    )
    console.print(
        "  bog-agents -M ollama:llama3                     # Use local Ollama model",
        style=COLORS["dim"],
    )
    console.print(
        "  bog-agents -n 'Summarize README.md'             # One-shot task",
        style=COLORS["dim"],
    )
    console.print(
        "  bog-agents -n 'Fix tests' --shell-allow-list all  # With shell access",
        style=COLORS["dim"],
    )
    console.print(
        "  bog-agents -p 'Explain this' < file.py          # Pipe-friendly output",
        style=COLORS["dim"],
    )
    console.print(
        "  bog-agents -p 'Review' < file.py | tee review.md  # Pipe to file",
        style=COLORS["dim"],
    )
    console.print(
        "  bog-agents -n 'Fix issue #42' --pr              # Fix + open PR",
        style=COLORS["dim"],
    )
    console.print(
        "  bog-agents -r                                   # Resume last thread",
        style=COLORS["dim"],
    )
    console.print()


def show_list_help() -> None:
    """Show help information for the `list` subcommand.

    Invoked via the `-h` argparse action or directly from `cli_main`.
    """
    console.print()
    console.print("[bold]Usage:[/bold]", style=COLORS["primary"])
    console.print("  bog-agents list [options]")
    console.print()
    console.print(
        "List all agents found in ~/.bog-agents/. Each agent has its own",
    )
    console.print(
        "AGENTS.md system prompt and separate thread history.",
    )
    console.print()
    _print_option_section()
    console.print()


def show_reset_help() -> None:
    """Show help information for the `reset` subcommand."""
    console.print()
    console.print("[bold]Usage:[/bold]", style=COLORS["primary"])
    console.print("  bog-agents reset --agent NAME [--target SRC]")
    console.print()
    console.print(
        "Restore an agent's AGENTS.md to the built-in default, or copy",
    )
    console.print(
        "another agent's AGENTS.md. This deletes the agent's directory",
    )
    console.print(
        "and recreates it with the new prompt.",
    )
    console.print()
    _print_option_section(
        "  --agent NAME            Agent to reset (required)",
        "  --target SRC            Copy AGENTS.md from another agent instead",
    )
    console.print()
    console.print("[bold]Examples:[/bold]", style=COLORS["primary"])
    console.print("  bog-agents reset --agent coder")
    console.print("  bog-agents reset --agent coder --target researcher")
    console.print()


def show_skills_help() -> None:
    """Show help information for the `skills` subcommand.

    Invoked via the `-h` argparse action or directly from
    `execute_skills_command` when no subcommand is given.
    """
    console.print()
    console.print("[bold]Usage:[/bold]", style=COLORS["primary"])
    console.print("  bog-agents skills <command> [options]")
    console.print()
    console.print("[bold]Commands:[/bold]", style=COLORS["primary"])
    console.print("  list|ls           List all available skills")
    console.print("  create <name>     Create a new skill")
    console.print("  info <name>       Show detailed information about a skill")
    console.print("  delete <name>     Delete a skill")
    console.print()
    _print_option_section(
        "  --agent <name>    Specify agent identifier (default: agent)",
        "  --project         Use project-level skills instead of user-level",
        title="Common options",
    )
    console.print()
    console.print("[bold]Examples:[/bold]", style=COLORS["primary"])
    console.print("  bog-agents skills list")
    console.print("  bog-agents skills list --project")
    console.print("  bog-agents skills create my-skill")
    console.print("  bog-agents skills create my-skill --agent myagent")
    console.print("  bog-agents skills info my-skill")
    console.print("  bog-agents skills delete my-skill")
    console.print("  bog-agents skills delete my-skill --force --project")
    console.print("  bog-agents skills delete -h")
    console.print()
    console.print(
        "[bold]Skill directories (highest precedence first):[/bold]",
        style=COLORS["primary"],
    )
    console.print(
        "  1. .agents/skills/                 project skills\n"
        "  2. .bog-agents/skills/             project skills (alias)\n"
        "  3. ~/.agents/skills/               user skills\n"
        "  4. ~/.bog-agents/<agent>/skills/   user skills (alias)\n"
        "  5. <package>/built_in_skills/      built-in skills",
    )
    console.print()


def show_skills_list_help() -> None:
    """Show help information for the `skills list` subcommand."""
    console.print()
    console.print("[bold]Usage:[/bold]", style=COLORS["primary"])
    console.print("  bog-agents skills list [options]")
    console.print()
    _print_option_section(
        "  --agent NAME            Agent identifier (default: agent)",
        "  --project               Show only project-level skills",
    )
    console.print()


def show_skills_create_help() -> None:
    """Show help information for the `skills create` subcommand."""
    console.print()
    console.print("[bold]Usage:[/bold]", style=COLORS["primary"])
    console.print("  bog-agents skills create <name> [options]")
    console.print()
    _print_option_section(
        "  --agent NAME            Agent identifier (default: agent)",
        "  --project               Create in project directory "
        "instead of user directory",
    )
    console.print()
    console.print("[bold]Examples:[/bold]", style=COLORS["primary"])
    console.print("  bog-agents skills create web-research")
    console.print("  bog-agents skills create my-skill --project")
    console.print()


def show_skills_info_help() -> None:
    """Show help information for the `skills info` subcommand."""
    console.print()
    console.print("[bold]Usage:[/bold]", style=COLORS["primary"])
    console.print("  bog-agents skills info <name> [options]")
    console.print()
    _print_option_section(
        "  --agent NAME            Agent identifier (default: agent)",
        "  --project               Search only in project skills",
    )
    console.print()


def show_skills_delete_help() -> None:
    """Show help information for the `skills delete` subcommand."""
    console.print()
    console.print("[bold]Usage:[/bold]", style=COLORS["primary"])
    console.print("  bog-agents skills delete <name> [options]")
    console.print()
    _print_option_section(
        "  --agent NAME            Agent identifier (default: agent)",
        "  --project               Search only in project skills",
        "  -f, --force             Skip confirmation prompt",
    )
    console.print()
    console.print("[bold]Examples:[/bold]", style=COLORS["primary"])
    console.print("  bog-agents skills delete old-skill")
    console.print("  bog-agents skills delete old-skill --force")
    console.print("  bog-agents skills delete old-skill --project")
    console.print()


def show_threads_help() -> None:
    """Show help information for the `threads` subcommand.

    Invoked via the `-h` argparse action or directly from `cli_main`
    when no threads subcommand is given.
    """
    console.print()
    console.print("[bold]Usage:[/bold]", style=COLORS["primary"])
    console.print("  bog-agents threads <command> [options]")
    console.print()
    console.print("[bold]Commands:[/bold]", style=COLORS["primary"])
    console.print("  list|ls           List all threads")
    console.print("  delete <ID>       Delete a thread")
    console.print()
    _print_option_section()
    console.print()
    console.print("[bold]Examples:[/bold]", style=COLORS["primary"])
    console.print("  bog-agents threads list")
    console.print("  bog-agents threads list -n 10")
    console.print("  bog-agents threads list --agent mybot")
    console.print("  bog-agents threads delete abc123")
    console.print()


def show_threads_delete_help() -> None:
    """Show help information for the `threads delete` subcommand."""
    console.print()
    console.print("[bold]Usage:[/bold]", style=COLORS["primary"])
    console.print("  bog-agents threads delete <ID> [options]")
    console.print()
    _print_option_section()
    console.print()
    console.print("[bold]Examples:[/bold]", style=COLORS["primary"])
    console.print("  bog-agents threads delete abc123")
    console.print()


def show_threads_list_help() -> None:
    """Show help information for the `threads list` subcommand."""
    console.print()
    console.print("[bold]Usage:[/bold]", style=COLORS["primary"])
    console.print("  bog-agents threads list [options]")
    console.print()
    _print_option_section(
        "  --agent NAME              Filter by agent name",
        "  --branch TEXT             Filter by git branch name",
        "  --sort {created,updated}  Sort order (default: from config, or updated)",
        "  -n, --limit N             Maximum threads to display (default: 20)",
        "  -v, --verbose             Show all columns (branch, created, prompt)",
        "  -r, --relative/--no-relative"
        "  Show relative timestamps (default: from config)",
    )
    console.print()
    console.print("[bold]Examples:[/bold]", style=COLORS["primary"])
    console.print("  bog-agents threads list")
    console.print("  bog-agents threads list -n 10")
    console.print("  bog-agents threads list --agent mybot")
    console.print("  bog-agents threads list --branch main -v")
    console.print("  bog-agents threads list --sort created --limit 50")
    console.print("  bog-agents threads list -r")
    console.print()
