"""Main entry point and CLI loop for bog-agents."""

# ruff: noqa: E402
# Imports placed after warning filters to suppress deprecation warnings

# Suppress deprecation warnings from langchain_core (e.g., Pydantic V1 on Python 3.14+)
import warnings

warnings.filterwarnings("ignore", module="langchain_core._api.deprecation")

import argparse
import asyncio
import contextlib
import functools
import importlib.util
import json
import logging
import os
import shutil
import sys
import traceback
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bog_agents_cli.app import AppResult
    from bog_agents_cli.mcp_tools import MCPServerInfo

# Suppress Pydantic v1 compatibility warnings from langchain on Python 3.14+
warnings.filterwarnings("ignore", message=".*Pydantic V1.*", category=UserWarning)

from bog_agents_cli._version import __version__

logger = logging.getLogger(__name__)

# Duplicated from agent.DEFAULT_AGENT_NAME to avoid importing the heavy agent
# module at startup. Keep in sync with agent.py. Tested.
_DEFAULT_AGENT_NAME = "agent"


_REQUIRED_CLI_PACKAGES: tuple[tuple[str, str], ...] = (
    # (importable module name, pip distribution name)
    ("requests", "requests"),
    ("dotenv", "python-dotenv"),
    ("textual", "textual"),
    # langchain/langgraph/langsmith are pulled by `bog-agents-cli`'s
    # runtime deps; an incomplete install (e.g. interrupted pip download)
    # can leave them missing and crash the CLI deep inside textual_adapter
    # or remote_client. Surface that here with a clean recovery hint.
    ("langchain", "langchain"),
    ("langchain_core", "langchain-core"),
    ("langgraph", "langgraph"),
    ("langgraph_sdk", "langgraph-sdk"),
    ("langsmith", "langsmith"),
    ("httpx", "httpx"),
    ("bog_agents", "bog-agents"),
)


def check_cli_dependencies() -> None:
    """Check that the CLI's required runtime packages are importable.

    A missing package here usually means an incomplete pip install.
    We fail fast with a clear `pip install --upgrade --force-reinstall`
    hint instead of crashing later inside a deep import.
    """
    missing: list[str] = []
    for module_name, pip_name in _REQUIRED_CLI_PACKAGES:
        if importlib.util.find_spec(module_name) is None:
            missing.append(pip_name)

    if missing:
        print("\nMissing required CLI dependencies!")  # noqa: T201
        print("\nThe following packages are required to use the bog-agents CLI:")  # noqa: T201
        for pkg in missing:
            print(f"  - {pkg}")  # noqa: T201
        print("\nFix with:")  # noqa: T201
        print("  pip install --upgrade --force-reinstall bog-agents-cli")  # noqa: T201
        print("\nOr install with all providers:")  # noqa: T201
        print("  pip install --upgrade 'bog-agents-cli[all-providers]'")  # noqa: T201
        sys.exit(1)


_RIPGREP_URL = "https://github.com/BurntSushi/ripgrep#installation"

_RIPGREP_SUPPRESS_HINT = (
    "To suppress, add to ~/.bog-agents/config.toml:\n"
    "\\[warnings]\n"
    'suppress = \\["ripgrep"]'
)


def check_optional_tools(*, config_path: Path | None = None) -> list[str]:
    """Check for recommended external tools and return missing tool names.

    Skips tools that the user has suppressed via
    `[warnings].suppress` in `config.toml`.

    Args:
        config_path: Path to config file.

            Defaults to `~/.bog-agents/config.toml`.

    Returns:
        List of missing tool names (e.g. `["ripgrep"]`).
    """
    from bog_agents_cli.model_config import is_warning_suppressed

    missing: list[str] = []
    if shutil.which("rg") is None and not is_warning_suppressed("ripgrep", config_path):
        missing.append("ripgrep")
    return missing


def format_tool_warning_tui(tool: str) -> str:
    """Format a missing-tool warning for the TUI toast.

    Args:
        tool: Name of the missing tool.

    Returns:
        Plain-text warning suitable for `App.notify`.
    """
    if tool == "ripgrep":
        return (
            "ripgrep is not installed; the grep tool will use a slower fallback.\n"
            f"\nInstall: {_RIPGREP_URL}\n\n"
            f"{_RIPGREP_SUPPRESS_HINT}"
        )
    return f"{tool} is not installed."


def format_tool_warning_cli(tool: str) -> str:
    """Format a missing-tool warning for non-interactive Rich console output.

    Args:
        tool: Name of the missing tool.

    Returns:
        Rich-markup string suitable for `console.print`.
    """
    if tool == "ripgrep":
        return (
            "ripgrep is not installed; the grep tool will use a slower fallback.\n"
            f"Install: [link={_RIPGREP_URL}]{_RIPGREP_URL}[/link]\n\n"
            f"{_RIPGREP_SUPPRESS_HINT}\n"
        )
    return f"{tool} is not installed."


async def _preload_session_mcp_server_info(
    *,
    mcp_config_path: str | None,
    no_mcp: bool,
    trust_project_mcp: bool | None,
) -> list["MCPServerInfo"] | None:
    """Load MCP metadata for the interactive TUI in server mode.

    In server mode the actual MCP tools are created inside the LangGraph server
    process, but the local Textual app still needs MCP metadata for the welcome
    banner and `/mcp` viewer. This preloads the metadata in the CLI process and
    immediately cleans up any temporary MCP sessions it opened.

    Args:
        mcp_config_path: Optional explicit MCP config path.
        no_mcp: Whether MCP loading is disabled.
        trust_project_mcp: Project-level MCP trust decision.

    Returns:
        MCP server metadata for the TUI, or `None` when MCP is disabled.
    """
    if no_mcp:
        return None

    from bog_agents_cli.mcp_tools import resolve_and_load_mcp_tools
    from bog_agents_cli.project_utils import ProjectContext

    session_manager = None
    try:
        try:
            project_context = ProjectContext.from_user_cwd(Path.cwd())
        except OSError:
            logger.warning("Could not determine working directory for MCP preload")
            project_context = None
        _tools, session_manager, server_info = await resolve_and_load_mcp_tools(
            explicit_config_path=mcp_config_path,
            no_mcp=no_mcp,
            trust_project_mcp=trust_project_mcp,
            project_context=project_context,
        )
        return server_info
    finally:
        if session_manager is not None:
            try:
                await session_manager.cleanup()
            except Exception:
                logger.warning(
                    "MCP metadata preload cleanup failed",
                    exc_info=True,
                )


def parse_args() -> argparse.Namespace:
    """Parse command line arguments.

    Returns:
        Parsed arguments namespace.
    """
    from bog_agents_cli.output import add_json_output_arg
    from bog_agents_cli.skills import setup_skills_parser
    from bog_agents_cli.ui import (
        build_help_parent,
        show_help,
        show_list_help,
        show_reset_help,
        show_threads_delete_help,
        show_threads_help,
        show_threads_list_help,
    )

    # Factory that builds an argparse Action whose __call__ invokes the
    # supplied *help_fn* instead of argparse's default help text.  Each
    # subcommand can pass its own Rich-formatted help screen so that
    # `bog-agents <subcommand> -h` shows context-specific help.
    def _make_help_action(
        help_fn: Callable[[], None],
    ) -> type[argparse.Action]:
        """Create an argparse Action that displays *help_fn* and exits.

        argparse requires a *class* (not a callable) for custom actions.
        This factory uses a closure: the returned `_ShowHelp` class captures
        *help_fn* from the enclosing scope so that each subcommand can wire `-h`
        to its own Rich help screen.

        Args:
            help_fn: Callable that prints help text to the console.

        Returns:
            An argparse Action class wired to the given help function.
        """

        class _ShowHelp(argparse.Action):
            def __init__(
                self,
                option_strings: list[str],
                dest: str = argparse.SUPPRESS,
                default: str = argparse.SUPPRESS,
                **kwargs: Any,
            ) -> None:
                super().__init__(
                    option_strings=option_strings,
                    dest=dest,
                    default=default,
                    nargs=0,
                    **kwargs,
                )

            def __call__(
                self,
                parser: argparse.ArgumentParser,
                namespace: argparse.Namespace,  # noqa: ARG002  # Required by argparse Action interface
                values: str | Sequence[Any] | None,  # noqa: ARG002  # Required by argparse Action interface
                option_string: str | None = None,  # noqa: ARG002  # Required by argparse Action interface
            ) -> None:
                with contextlib.suppress(BrokenPipeError):
                    help_fn()
                parser.exit()

        return _ShowHelp

    help_parent = functools.partial(
        build_help_parent, make_help_action=_make_help_action
    )

    parser = argparse.ArgumentParser(
        description=("Bog Agents - AI Coding Assistant"),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        add_help=False,
    )
    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    subparsers.add_parser(
        "help",
        help="Show help information",
        add_help=False,
        parents=help_parent(show_help),
    )

    subparsers.add_parser(
        "list",
        help="List all available agents",
        add_help=False,
        parents=help_parent(show_list_help),
    )
    add_json_output_arg(subparsers.choices["list"])

    reset_parser = subparsers.add_parser(
        "reset",
        help="Reset an agent",
        add_help=False,
        parents=help_parent(show_reset_help),
    )
    add_json_output_arg(reset_parser)
    reset_parser.add_argument("--agent", required=True, help="Name of agent to reset")
    reset_parser.add_argument(
        "--target", dest="source_agent", help="Copy prompt from another agent"
    )

    setup_skills_parser(
        subparsers,
        make_help_action=_make_help_action,
        add_output_args=add_json_output_arg,
    )

    # Daemon management subcommand
    from bog_agents_cli.cmd_daemon import setup_daemon_parser

    setup_daemon_parser(subparsers)

    # Project verification subcommand — auto-detects typecheck/lint/test
    # commands or runs an explicit `.bog-agents/verify.{sh,cmd}` override.
    from bog_agents_cli.cmd_verify import setup_verify_parser

    setup_verify_parser(subparsers)

    # Thin HTTP client for a long-lived `--serve` instance.
    from bog_agents_cli.cmd_call import setup_call_parser

    setup_call_parser(subparsers)

    # Bedrock connection probe — credentials, region, model access,
    # tiny inference. Self-contained: doesn't load the agent.
    bedrock_parser = subparsers.add_parser(
        "test-bedrock",
        help="Probe AWS Bedrock connectivity (credentials, region, models, inference)",
    )
    bedrock_parser.add_argument(
        "--model",
        default=None,
        help=(
            "Bedrock model id to test inference against, e.g. "
            "anthropic.claude-sonnet-4-20250514-v1:0. When omitted, "
            "steps 1-5 (credentials, region, list models) still run."
        ),
    )
    bedrock_parser.add_argument(
        "--region",
        default=None,
        help="AWS region override (defaults to AWS_REGION env / profile config)",
    )

    command_parser = subparsers.add_parser(
        "command",
        help="Run a slash command non-interactively (e.g. 'command \"/help\"')",
    )
    command_parser.add_argument(
        "slash",
        help="The slash command to run, e.g. '/help', '/commands', '/model'",
    )
    add_json_output_arg(command_parser)

    # Expose bog-agents AS an MCP server (so other agents/IDEs can delegate to us)
    mcp_server_parser = subparsers.add_parser(
        "mcp-server",
        help=(
            "Run bog-agents as an MCP (Model Context Protocol) stdio server "
            "exposing a `run_task` tool, so any MCP client (Claude Desktop, "
            "Cursor, Zed, Copilot) can delegate a coding task to it."
        ),
    )
    mcp_server_parser.add_argument(
        "-M",
        "--model",
        metavar="MODEL",
        default=None,
        help="Model spec (e.g. anthropic:claude-sonnet-4-6). Auto-detects if omitted.",
    )
    mcp_server_parser.add_argument(
        "--permission-mode",
        choices=["acceptEdits", "bypass"],
        default="acceptEdits",
        metavar="MODE",
        help=(
            "Approval posture for delegated tasks. MCP has no human approver, so "
            "only autonomous modes are allowed: 'acceptEdits' (smart rule-engine "
            "auto-approval; default) or 'bypass' (approve everything)."
        ),
    )
    mcp_server_parser.add_argument(
        "--cwd",
        dest="mcp_cwd",
        metavar="PATH",
        default=None,
        help="Workspace root the agent operates in (default: current directory).",
    )

    threads_parser = subparsers.add_parser(
        "threads",
        help="Manage conversation threads",
        add_help=False,
        parents=help_parent(show_threads_help),
    )
    add_json_output_arg(threads_parser)
    threads_sub = threads_parser.add_subparsers(dest="threads_command")

    threads_list = threads_sub.add_parser(
        "list",
        aliases=["ls"],
        help="List threads",
        add_help=False,
        parents=help_parent(show_threads_list_help),
    )
    add_json_output_arg(threads_list)
    threads_list.add_argument(
        "--agent", default=None, help="Filter by agent name (default: show all)"
    )
    threads_list.add_argument(
        "-n",
        "--limit",
        type=int,
        default=None,
        help="Max number of threads to display (default: 20)",
    )
    threads_list.add_argument(
        "--sort",
        choices=["created", "updated"],
        default=None,
        help="Sort threads by timestamp (default: from config, or updated)",
    )
    threads_list.add_argument(
        "--branch",
        default=None,
        help="Filter by git branch name",
    )
    threads_list.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        default=False,
        help="Show all columns (branch, created, prompt)",
    )
    threads_list.add_argument(
        "-r",
        "--relative",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Show timestamps as relative time (default: from config, or absolute)",
    )
    threads_delete = threads_sub.add_parser(
        "delete",
        help="Delete a thread",
        add_help=False,
        parents=help_parent(show_threads_delete_help),
    )
    add_json_output_arg(threads_delete)
    threads_delete.add_argument("thread_id", help="Thread ID to delete")

    # Default interactive mode — argument order here determines the
    # usage line printed by argparse; keep in sync with ui.show_help().
    parser.add_argument(
        "-r",
        "--resume",
        dest="resume_thread",
        nargs="?",
        const="__MOST_RECENT__",
        default=None,
        metavar="ID",
        help="Resume thread: -r for most recent, -r <ID> for specific thread",
    )

    parser.add_argument(
        "-a",
        "--agent",
        default=_DEFAULT_AGENT_NAME,
        metavar="NAME",
        help="Agent to use (e.g., coder, researcher).",
    )

    parser.add_argument(
        "-M",
        "--model",
        metavar="MODEL",
        help="Model to use (e.g., claude-sonnet-4-6, gpt-5). "
        "Provider is auto-detected from model name.",
    )

    parser.add_argument(
        "--model-params",
        metavar="JSON",
        help="Extra kwargs to pass to the model as a JSON string "
        '(e.g., \'{"temperature": 0.7, "max_tokens": 4096}\'). '
        "These take priority, overriding config file values.",
    )

    parser.add_argument(
        "--profile-override",
        metavar="JSON",
        help="Override model profile fields as a JSON string "
        "(e.g., '{\"max_input_tokens\": 4096}'). "
        "Merged on top of config file profile overrides.",
    )

    parser.add_argument(
        "--default-model",
        metavar="MODEL",
        nargs="?",
        const="__SHOW__",
        default=None,
        help="Set the default model for future launches "
        "(e.g., anthropic:claude-opus-4-6). "
        "Use --default-model with no argument to show the current default. "
        "Use --clear-default-model to remove it.",
    )

    parser.add_argument(
        "--clear-default-model",
        action="store_true",
        help="Clear the default model, falling back to recent model "
        "or environment auto-detection.",
    )

    parser.add_argument(
        "-m",
        "--message",
        dest="initial_prompt",
        metavar="TEXT",
        help="Initial prompt to auto-submit when session starts",
    )

    parser.add_argument(
        "-n",
        "--non-interactive",
        dest="non_interactive_message",
        metavar="TEXT",
        help="Run a single task non-interactively and exit "
        "(shell disabled unless --shell-allow-list is set)",
    )

    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Clean output for piping — only the agent's response "
        "goes to stdout. Requires -n or piped stdin.",
    )

    parser.add_argument(
        "--no-stream",
        dest="no_stream",
        action="store_true",
        help="Buffer the full response and write it to stdout at once "
        "instead of streaming token-by-token. Requires -n or piped stdin.",
    )

    parser.add_argument(
        "-p",
        "--print",
        dest="print_message",
        metavar="TEXT",
        help="Run a single prompt non-interactively with clean output "
        "(equivalent to -n TEXT -q). Ideal for scripting and editor integrations.",
    )

    parser.add_argument(
        "--prompt",
        dest="prompt_name",
        metavar="NAME",
        help="Run a saved prompt from ~/.bog-agents/prompt_library.toml "
        "as the non-interactive task. Use --prompt-vars to supply "
        "variables. Mutually exclusive with -n / -p.",
    )
    parser.add_argument(
        "--prompt-vars",
        dest="prompt_vars",
        metavar="JSON",
        help="JSON object of variables to substitute into the prompt body "
        '(e.g. \'{"code": "...", "language": "python"}\'). '
        "Used with --prompt.",
    )
    parser.add_argument(
        "--pipeline",
        dest="pipeline_name",
        metavar="NAME",
        help="Run a saved pipeline from ~/.bog-agents/pipelines/<NAME>.yaml "
        "as the non-interactive task. The pipeline's steps are inlined as "
        "a multi-step prompt. Mutually exclusive with -n / -p / --prompt.",
    )

    add_json_output_arg(parser, default="text")
    parser.add_argument(
        "--jsonl",
        dest="output_format",
        action="store_const",
        const="jsonl",
        help=(
            "Stream newline-delimited JSON events (stream-json) for "
            "non-interactive runs: one object per line for start, text "
            "deltas, tool_call, tool_result, and a terminal final event "
            "with thread_id, response, tool_calls, and usage stats."
        ),
    )

    parser.add_argument(
        "--auto-approve",
        action="store_true",
        help=(
            "Auto-approve all tool calls without prompting "
            "(disables human-in-the-loop). Affected tools: shell "
            "execution, file writes/edits, web search, and URL fetch. "
            "Use with caution — the agent can execute arbitrary commands."
        ),
    )

    parser.add_argument(
        "--always-ask",
        action="store_true",
        help=(
            "Paranoid mode: every tool call requires explicit approval, "
            "EVEN if --auto-approve is set or a shell command is on the "
            "allow-list. Use for high-stakes sessions where you want to "
            "inspect each action before it runs. Toggle at runtime via "
            "/always-ask."
        ),
    )

    parser.add_argument(
        "--auto",
        dest="auto_mode",
        action="store_true",
        default=False,
        help=(
            "Smart auto-approve: auto-run tool calls that pass the rule engine; "
            "ask only for risky operations. Haiku evaluates uncertain shell "
            "commands. Overridden by --always-ask. Configure rules in "
            ".bog-agents/settings.json."
        ),
    )

    parser.add_argument(
        "--permission-mode",
        choices=["default", "acceptEdits", "plan", "bypass", "paranoid"],
        default=None,
        metavar="MODE",
        help=(
            "Permission mode (Claude-Code-style). 'default' prompts for each "
            "tool call; 'acceptEdits' auto-approves file edits + safe tools and "
            "asks only for risky shell (smart rule engine); 'plan' is read-only "
            "(mutating tools stripped); 'bypass' approves everything; "
            "'paranoid' forces approval for every call. In the TUI, Shift+Tab "
            "cycles default -> acceptEdits -> plan. Maps onto --auto-approve / "
            "--auto / --always-ask (do not combine with a conflicting flag)."
        ),
    )

    parser.add_argument(
        "--dangerously-skip-permissions",
        action="store_true",
        default=False,
        help="Alias for --permission-mode bypass (Claude-Code compatibility).",
    )

    parser.add_argument(
        "--sandbox",
        choices=["none", "docker", "modal", "daytona", "runloop", "langsmith"],
        default="none",
        metavar="TYPE",
        help=(
            "Sandbox for code execution (default: none — local only). "
            "``docker`` runs commands inside a local container; the others "
            "are remote providers requiring credentials. Tune the docker "
            "image with BOG_DOCKER_IMAGE (default python:3.11-slim)."
        ),
    )

    parser.add_argument(
        "--sandbox-id",
        metavar="ID",
        help="Existing sandbox ID to reuse (skips creation and cleanup)",
    )

    parser.add_argument(
        "--sandbox-setup",
        metavar="PATH",
        help="Path to setup script to run in sandbox after creation",
    )
    parser.add_argument(
        "--shell-allow-list",
        metavar="LIST",
        help="Comma-separated list of shell commands to auto-approve, "
        "'recommended' for safe defaults, or 'all' to allow any command. "
        "Applies to both -n and interactive modes.",
    )
    parser.add_argument(
        "--mcp-config",
        help="Path to MCP servers JSON configuration file (Claude Desktop format). "
        "Merged on top of auto-discovered configs (highest precedence).",
    )
    parser.add_argument(
        "--no-mcp",
        action="store_true",
        help="Disable all MCP tool loading (skip auto-discovery and explicit config)",
    )
    parser.add_argument(
        "--trust-project-mcp",
        action="store_true",
        help="Trust project-level MCP configs with stdio servers "
        "(skip interactive approval prompt)",
    )

    try:
        from importlib.metadata import (
            PackageNotFoundError,
            version as _pkg_version,
        )

        sdk_version = _pkg_version("bog-agents")
    except PackageNotFoundError:
        logger.debug("bog-agents SDK package not found in environment")
        sdk_version = "unknown"
    except Exception:
        logger.warning("Unexpected error looking up SDK version", exc_info=True)
        sdk_version = "unknown"
    parser.add_argument(
        "--acp",
        action="store_true",
        help="Run as an ACP server over stdio instead of launching the Textual UI",
    )

    parser.add_argument(
        "--serve",
        action="store_true",
        help="Start an HTTP API server instead of the TUI",
    )
    parser.add_argument(
        "--serve-host",
        default="127.0.0.1",
        help="Host for the HTTP API server (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--serve-port",
        type=int,
        default=8420,
        help="Port for the HTTP API server (default: 8420)",
    )

    parser.add_argument(
        "--drive",
        dest="drive_script",
        metavar="PATH",
        help=(
            "Run a YAML drive script that emulates a TUI user "
            "non-interactively. Emits JSONL on stdout (one line per "
            "step + summary). Exit code = number of failed steps."
        ),
    )
    parser.add_argument(
        "--drive-stdin",
        action="store_true",
        help="Read the drive script from stdin instead of a path.",
    )
    parser.add_argument(
        "--drive-var",
        dest="drive_vars",
        action="append",
        metavar="NAME=VALUE",
        help=(
            "Override a ${var} value used inside the drive script. "
            "May be passed multiple times."
        ),
    )
    parser.add_argument(
        "--drive-artifacts",
        dest="drive_artifacts",
        metavar="DIR",
        help=(
            "Directory for snapshot artefacts. Defaults to "
            "<script-dir>/.drive-artifacts/<timestamp>/."
        ),
    )
    parser.add_argument(
        "--drive-output",
        dest="drive_output",
        metavar="PATH",
        help="Write the JSONL transcript to this file instead of stdout.",
    )
    parser.add_argument(
        "--drive-stop-on-failure",
        dest="drive_stop_on_failure",
        action="store_true",
        help="Abort the run at the first failed assertion.",
    )

    parser.add_argument(
        "--pr",
        action="store_true",
        help="PR-output mode: run agent non-interactively and create a pull request",
    )
    parser.add_argument(
        "--pr-base",
        default="main",
        help="Base branch for PR creation (default: main)",
    )
    parser.add_argument(
        "--pr-draft",
        action="store_true",
        help="Create the PR as a draft",
    )

    parser.add_argument(
        "--auto-commit",
        action="store_true",
        help=(
            "Automatically create a git commit after each agent turn. "
            "Commits follow Conventional Commits format and are tagged with '(bog-agent)'. "
            "Only commits when there are staged or unstaged changes."
        ),
    )

    parser.add_argument(
        "--doctor",
        action="store_true",
        help="Run diagnostics to check environment, dependencies, and configuration",
    )
    parser.add_argument(
        "--doctor-deep",
        action="store_true",
        help=(
            "Like --doctor but also probes external dependencies (network, "
            "git, file write, MCP, model availability) and prints a one-page "
            "health summary. May take a few seconds."
        ),
    )

    parser.add_argument(
        "-v",
        "--version",
        action="version",
        version=f"bog-agents-cli {__version__}\nbog-agents (SDK) {sdk_version}",
    )
    parser.add_argument(
        "-h",
        "--help",
        action=_make_help_action(show_help),
    )

    return parser.parse_args()


# Unified --permission-mode -> legacy approval-flag mapping. The high-level
# flag is the single Claude-Code-style knob; downstream code (TUI + headless)
# reads the individual booleans, so we derive them here once.
_PERMISSION_MODE_FLAGS: dict[str, dict[str, bool]] = {
    "default": {},
    "acceptEdits": {"auto_mode": True},
    "plan": {"plan_mode": True},
    "bypass": {"auto_approve": True},
    "paranoid": {"always_ask": True},
}


def _normalize_permission_mode(args: argparse.Namespace) -> None:
    """Translate ``--permission-mode`` / ``--dangerously-skip-permissions``.

    Derives the legacy ``auto_approve`` / ``auto_mode`` / ``always_ask`` /
    ``plan_mode`` flags from the unified mode, validates against contradictory
    legacy flags (exit 2 on conflict), and always leaves an ``args.plan_mode``
    attribute so downstream call sites can read it unconditionally. Mutates
    ``args`` in place.
    """
    if not hasattr(args, "plan_mode"):
        args.plan_mode = False

    mode = getattr(args, "permission_mode", None)
    if getattr(args, "dangerously_skip_permissions", False):
        if mode is not None and mode != "bypass":
            _exit_permission_conflict(
                f"--dangerously-skip-permissions conflicts with "
                f"--permission-mode {mode}"
            )
        mode = "bypass"

    if mode is None:
        return

    target = _PERMISSION_MODE_FLAGS[mode]
    # Reject a --permission-mode that contradicts an explicitly-set legacy flag.
    legacy = {
        "--auto-approve": ("auto_approve", bool(getattr(args, "auto_approve", False))),
        "--auto": ("auto_mode", bool(getattr(args, "auto_mode", False))),
        "--always-ask": ("always_ask", bool(getattr(args, "always_ask", False))),
    }
    for flag, (key, is_set) in legacy.items():
        if is_set and not target.get(key, False):
            _exit_permission_conflict(f"--permission-mode {mode} conflicts with {flag}")

    args.auto_approve = target.get("auto_approve", False)
    args.auto_mode = target.get("auto_mode", False)
    args.always_ask = target.get("always_ask", False)
    args.plan_mode = target.get("plan_mode", False)


def _exit_permission_conflict(msg: str) -> None:
    """Print a permission-flag conflict error and exit with code 2."""
    sys.stderr.write(f"Error: {msg}.\n")
    sys.stderr.flush()
    sys.exit(2)


async def run_textual_cli_async(
    assistant_id: str,
    *,
    auto_approve: bool = False,
    always_ask: bool = False,
    auto_mode: bool = False,
    plan_mode: bool = False,
    auto_commit: bool = False,
    sandbox_type: str = "none",  # str (not None) to match argparse choices
    sandbox_id: str | None = None,
    sandbox_setup: str | None = None,
    model_name: str | None = None,
    model_params: dict[str, Any] | None = None,
    profile_override: dict[str, Any] | None = None,
    thread_id: str | None = None,
    initial_prompt: str | None = None,
    mcp_config_path: str | None = None,
    no_mcp: bool = False,
    trust_project_mcp: bool | None = None,
) -> "AppResult":
    """Run the Textual CLI interface (async version).

    Starts a LangGraph server in a subprocess and connects the TUI to it via the
    `langgraph-sdk` client.

    Args:
        assistant_id: Agent identifier for memory storage
        auto_approve: Whether to auto-approve tool usage
        always_ask: Paranoid mode — every tool call requires approval,
            overriding auto-approve and the shell allow-list.
        auto_mode: Smart auto-approval — tool calls are evaluated by the rule
            engine; only risky ones surface an approval dialog. Overridden by
            ``always_ask``.
        plan_mode: Start in plan mode (read-only; mutating tools stripped).
        auto_commit: Whether to auto-commit git changes after each agent turn
        sandbox_type: Type of sandbox
            ("none", "modal", "runloop", "daytona", "langsmith")
        sandbox_id: Optional existing sandbox ID to reuse.
        sandbox_setup: Optional path to setup script to run in the sandbox
            after creation.
        model_name: Optional model name to use
        model_params: Extra kwargs from `--model-params` to pass to the model.

            These override config file values.
        profile_override: Extra profile fields from `--profile-override`.

            Merged on top of config file profile overrides.
        thread_id: Thread ID to use (new or resumed)
        initial_prompt: Optional prompt to auto-submit when session starts
        mcp_config_path: Optional path to MCP servers JSON configuration file.

            Merged on top of auto-discovered configs (highest precedence).
        no_mcp: Disable all MCP tool loading.
        trust_project_mcp: Controls project-level stdio server trust.

            `True` to allow, `False` to deny, `None` to check trust store.

    Returns:
        An `AppResult` with the return code and final thread ID.
    """
    from rich.text import Text

    from bog_agents_cli.app import run_textual_app
    from bog_agents_cli.config import console, create_model_with_fallback
    from bog_agents_cli.model_config import ModelConfigError, save_recent_model

    try:
        result = create_model_with_fallback(
            model_name,
            extra_kwargs=model_params,
            profile_overrides=profile_override,
        )
    except ModelConfigError as e:
        from bog_agents_cli.app import AppResult

        console.print(f"[bold red]Error:[/bold red] {e}")
        return AppResult(return_code=1, thread_id=None)

    result.apply_to_settings()

    # Persist the resolved model so [models].recent is always populated,
    # not only after an explicit /model switch.
    save_recent_model(f"{result.provider}:{result.model_name}")

    from bog_agents_cli.app import AppResult

    # Build kwargs for deferred server startup (runs inside the TUI)
    server_kwargs: dict[str, Any] = {
        "assistant_id": assistant_id,
        "model_name": model_name,
        "model_params": model_params,
        "auto_approve": auto_approve,
        "sandbox_type": sandbox_type,
        "sandbox_id": sandbox_id,
        "sandbox_setup": sandbox_setup,
        "mcp_config_path": mcp_config_path,
        "no_mcp": no_mcp,
        "trust_project_mcp": trust_project_mcp,
        "interactive": True,
    }

    mcp_preload_kwargs: dict[str, Any] | None = None
    if not no_mcp:
        mcp_preload_kwargs = {
            "mcp_config_path": mcp_config_path,
            "no_mcp": no_mcp,
            "trust_project_mcp": trust_project_mcp,
        }

    try:
        result = await run_textual_app(
            assistant_id=assistant_id,
            backend=None,
            auto_approve=auto_approve,
            always_ask=always_ask,
            auto_mode=auto_mode,
            plan_mode=plan_mode,
            auto_commit=auto_commit,
            cwd=Path.cwd(),
            thread_id=thread_id,
            initial_prompt=initial_prompt,
            profile_override=profile_override,
            server_kwargs=server_kwargs,
            mcp_preload_kwargs=mcp_preload_kwargs,
        )
    except Exception as e:
        logger.debug("App error", exc_info=True)
        error_text = Text("Application error: ", style="red")
        error_text.append(str(e))
        console.print(error_text)
        if logger.isEnabledFor(logging.DEBUG):
            console.print(Text(traceback.format_exc(), style="dim"))
        return AppResult(return_code=1, thread_id=None)

    return result


async def _run_acp_cli_async(
    assistant_id: str,
    *,
    run_acp_agent: Callable[[Any], Any],
    agent_server_cls: type[Any],
    model_name: str | None = None,
    model_params: dict[str, Any] | None = None,
    profile_override: dict[str, Any] | None = None,
    mcp_config_path: str | None = None,
    no_mcp: bool = False,
    trust_project_mcp: bool | None = None,
) -> int:
    """Run ACP server mode and return a process exit code.

    Args:
        assistant_id: Agent identifier to initialize.
        run_acp_agent: ACP server runner function.
        agent_server_cls: ACP server class constructor.
        model_name: Optional model name to use.
        model_params: Extra kwargs from `--model-params` to pass to the model.
        profile_override: Extra profile fields from `--profile-override`.
        mcp_config_path: Optional path to MCP servers JSON configuration file.
        no_mcp: Disable all MCP tool loading.
        trust_project_mcp: Controls project-level stdio server trust.

    Returns:
        Exit code for ACP mode.
    """
    from bog_agents_cli.agent import create_cli_agent
    from bog_agents_cli.config import (
        create_model_with_fallback as create_model,
        settings,
    )
    from bog_agents_cli.model_config import ModelConfigError, save_recent_model
    from bog_agents_cli.tools import fetch_url, http_request, web_search

    try:
        model_result = create_model(
            model_name,
            extra_kwargs=model_params,
            profile_overrides=profile_override,
        )
    except ModelConfigError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        sys.stderr.flush()
        return 1
    model_result.apply_to_settings()

    # Persist the resolved model so [models].recent is always populated.
    save_recent_model(f"{model_result.provider}:{model_result.model_name}")

    tools: list[Any] = [http_request, fetch_url]
    if settings.has_tavily:
        tools.append(web_search)

    mcp_session_manager = None
    mcp_server_info = None
    try:
        from bog_agents_cli.mcp_tools import resolve_and_load_mcp_tools

        (
            mcp_tools,
            mcp_session_manager,
            mcp_server_info,
        ) = await resolve_and_load_mcp_tools(
            explicit_config_path=mcp_config_path,
            no_mcp=no_mcp,
            trust_project_mcp=trust_project_mcp,
        )
        tools.extend(mcp_tools)
    except FileNotFoundError as exc:
        msg = f"Error: MCP config file not found: {exc}\n"
        sys.stderr.write(msg)
        sys.stderr.flush()
        return 1
    except RuntimeError as exc:
        msg = f"Error: Failed to load MCP tools: {exc}\n"
        sys.stderr.write(msg)
        sys.stderr.flush()
        return 1

    try:
        from langgraph.checkpoint.memory import InMemorySaver

        agent_graph, _backend = create_cli_agent(
            model=model_result.model,
            assistant_id=assistant_id,
            tools=tools,
            mcp_server_info=mcp_server_info,
            checkpointer=InMemorySaver(),
        )
    except Exception as exc:
        sys.stderr.write(f"Error: failed to create agent: {exc}\n")
        sys.stderr.flush()
        logger.debug("ACP agent creation failed", exc_info=True)
        return 1

    server = agent_server_cls(agent_graph)  # Pregel is a CompiledStateGraph at runtime
    exit_code = 0
    try:
        await run_acp_agent(server)
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        sys.stderr.write(f"Error: ACP server failed: {exc}\n")
        sys.stderr.flush()
        logger.exception("ACP server crashed")
        exit_code = 1
    finally:
        if mcp_session_manager is not None:
            try:
                await mcp_session_manager.cleanup()
            except Exception:
                logger.warning("MCP session cleanup failed", exc_info=True)
    return exit_code


# Max time to wait for the first byte of piped stdin before concluding the
# stream is an idle pipe with nothing coming. Real piped input (`echo x |`,
# `cat file |`) is OS-buffered and ready immediately, so this grace is only
# ever fully consumed when there is genuinely no piped data.
_STDIN_PEEK_GRACE_SECONDS = 0.5


def _stdin_has_pending_data(grace: float = _STDIN_PEEK_GRACE_SECONDS) -> bool:
    """Best-effort, non-blocking check for pending data on a non-tty stdin.

    A bare ``sys.stdin.read()`` deadlocks when stdin is a non-tty *pipe* that
    a parent process keeps open without ever writing to or closing it. This is
    common when the CLI is launched by a service manager, an IDE task runner,
    the daemon, a CI executor, or any GUI app that wires up a child's stdin but
    never feeds it. The blocking read then hangs the whole CLI at startup —
    before the agent or model is ever touched — which superficially looks like
    "the provider hung" (most visibly Bedrock, whose server boot is the next
    step). We therefore peek before committing to the read.

    Args:
        grace: Max seconds to wait for the first byte to become available.

    Returns:
        True if stdin appears to have data (or EOF) ready, so a read will make
        progress; False if stdin looks like an idle pipe with nothing coming
        (in which case the caller should skip the read rather than hang). On
        any detection failure we return False — degrading the stdin-prepend
        convenience to a no-op is strictly preferable to an unbounded hang.
    """
    try:
        fd = sys.stdin.fileno()
    except (OSError, ValueError, AttributeError):
        # In-memory / file-like stream (StringIO, a test double, embedded
        # Python) with no backing OS fd — a read cannot block on an OS pipe,
        # so it is always safe to proceed.
        return True

    if not isinstance(fd, int):
        # Mocked or unusual stream object — skip OS-level readiness probing
        # and let the read proceed (it cannot be a real blocking pipe).
        return True

    if os.name != "nt":
        # POSIX: select works on pipes and regular files. Readable means data
        # or EOF is available, so the subsequent read won't block.
        try:
            import select

            return bool(select.select([fd], [], [], grace)[0])
        except (OSError, ValueError):
            return False

    # Windows: select doesn't support pipes/files, so inspect the handle.
    try:
        import ctypes
        import msvcrt
        import time
        from ctypes import wintypes

        handle = msvcrt.get_osfhandle(fd)
        file_type = ctypes.windll.kernel32.GetFileType(handle)
        file_type_disk = 1  # `< file` redirect — read hits EOF, never blocks
        file_type_pipe = 3
        if file_type == file_type_disk:
            return True
        if file_type != file_type_pipe:
            return False
        # Pipe: poll for buffered bytes within the grace window. A closed
        # (broken) pipe with no data reports failure → treat as no-data.
        avail = wintypes.DWORD(0)
        deadline = time.monotonic() + grace
        while True:
            ok = ctypes.windll.kernel32.PeekNamedPipe(
                handle, None, 0, None, ctypes.byref(avail), None
            )
            if not ok:
                return False
            if avail.value > 0:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.02)
    except Exception:  # detection must never raise into startup
        return False


def apply_stdin_pipe(args: argparse.Namespace) -> None:
    r"""Read piped stdin and merge it into the parsed CLI arguments.

    When stdin is not a TTY (i.e. input is piped), reads all available text
    and applies it to the argument namespace. If stdin is a TTY or the piped
    input is empty/whitespace-only, the function returns without modifying
    `args`. Leading and trailing whitespace is stripped from piped input.

    - If `non_interactive_message` is already set (`-n`), prepends the
        piped text to it (the CLI still runs non-interactively):

        ```bash
        cat context.txt | bog-agents -n "summarize this"
        # non_interactive_message = "{contents of context.txt}\n\nsummarize this"
        ```

    - If `initial_prompt` is already set (`-m`, but not `-n`), prepends
        the piped text to it (the CLI still runs interactively):

        ```bash
        cat error.log | bog-agents -m "explain this"
        # initial_prompt = "{contents of error.log}\n\nexplain this"
        ```

    - Otherwise, sets `non_interactive_message` to the piped text, causing
        the CLI to run non-interactively with it as the prompt:

        ```bash
        echo "fix the typo in README.md" | bog-agents
        # non_interactive_message = "fix the typo in README.md"
        ```

    Args:
        args: The parsed argument namespace (mutated in place).
    """
    from bog_agents_cli.config import console

    if sys.stdin is None:
        return

    try:
        is_tty = sys.stdin.isatty()
    except (ValueError, OSError):
        return

    if is_tty:
        return

    # stdin is a non-tty (pipe/redirect). Only commit to a blocking read once
    # we've confirmed data (or EOF) is actually available — otherwise an idle
    # pipe left open by a parent process would hang the CLI forever here. See
    # `_stdin_has_pending_data` for the cross-platform rationale.
    if not _stdin_has_pending_data():
        return

    max_stdin_bytes = 10 * 1024 * 1024  # 10 MiB

    try:
        stdin_text = sys.stdin.read(max_stdin_bytes + 1)
    except UnicodeDecodeError:
        msg = "Could not read piped input — ensure the input is valid text"
        console.print(f"[bold red]Error:[/bold red] {msg}")
        sys.exit(1)
    except (OSError, ValueError) as exc:
        msg = f"Failed to read piped input: {exc}"
        console.print(f"[bold red]Error:[/bold red] {msg}")
        sys.exit(1)

    if len(stdin_text) > max_stdin_bytes:
        msg = (
            f"Piped input exceeds {max_stdin_bytes // (1024 * 1024)} MiB limit. "
            "Consider writing the content to a file and referencing it instead."
        )
        console.print(f"[bold red]Error:[/bold red] {msg}")
        sys.exit(1)

    stdin_text = stdin_text.strip()

    if not stdin_text:
        return

    if args.non_interactive_message:
        args.non_interactive_message = f"{stdin_text}\n\n{args.non_interactive_message}"
    elif args.initial_prompt:
        args.initial_prompt = f"{stdin_text}\n\n{args.initial_prompt}"
    else:
        args.non_interactive_message = stdin_text

    # Restore stdin from the real terminal so the interactive Textual app
    # (used by the -m path) can read keyboard/mouse input normally.
    # Textual's driver reads from file descriptor 0 directly (not sys.stdin),
    # so we must replace the underlying fd with /dev/tty using os.dup2.
    try:
        tty_fd = os.open("/dev/tty", os.O_RDONLY)
    except OSError:
        # No controlling terminal (CI, Docker, headless). Non-interactive
        # path still works; interactive -m path will fail later with a
        # clear "not a terminal" error from Textual.
        return

    try:
        os.dup2(tty_fd, 0)
        os.close(tty_fd)
        sys.stdin = open(0, encoding="utf-8", closefd=False)  # noqa: SIM115  # fd 0 requires open() for TTY restoration
    except OSError:
        console.print(
            "[yellow]Warning:[/yellow] TTY restoration failed. "
            "Interactive mode (-m) may not work correctly."
        )
        logger.warning(
            "TTY restoration failed after opening /dev/tty",
            exc_info=True,
        )
        try:
            os.close(tty_fd)
        except OSError:
            logger.warning(
                "Failed to close TTY fd %d during cleanup",
                tty_fd,
                exc_info=True,
            )


def _print_session_stats(stats: Any, console: Any) -> None:  # noqa: ANN401
    """Print a session-level usage stats table to the console on TUI exit.

    Args:
        stats: The cumulative session stats from the Textual app.
        console: Rich console for output.
    """
    from bog_agents_cli.textual_adapter import SessionStats, print_usage_table

    if not isinstance(stats, SessionStats):
        return
    print_usage_table(stats, stats.wall_time_seconds, console)


def _check_mcp_project_trust(*, trust_flag: bool = False) -> bool | None:
    """Check whether project-level MCP stdio servers should be trusted.

    When the project has no stdio servers in project-level configs, returns
    `None` (no gate needed). When `--trust-project-mcp` was passed, returns
    `True`. Otherwise checks the persistent trust store; if untrusted, shows
    an interactive approval prompt.

    Args:
        trust_flag: Whether `--trust-project-mcp` was passed.

    Returns:
        `True` to allow project stdio servers, `False` to deny, or `None`
            when no project stdio servers exist.
    """
    from bog_agents_cli.mcp_tools import (
        classify_discovered_configs,
        discover_mcp_configs,
        extract_stdio_server_commands,
        load_mcp_config_lenient,
    )
    from bog_agents_cli.project_utils import ProjectContext

    try:
        project_context = ProjectContext.from_user_cwd(Path.cwd())
        config_paths = discover_mcp_configs(project_context=project_context)
    except (OSError, RuntimeError):
        return None

    _, project_configs = classify_discovered_configs(config_paths)
    if not project_configs:
        return None

    # Collect all stdio servers across project configs
    all_stdio: list[tuple[str, str, list[str]]] = []
    for path in project_configs:
        cfg = load_mcp_config_lenient(path)
        if cfg is not None:
            all_stdio.extend(extract_stdio_server_commands(cfg))

    if not all_stdio:
        return None

    if trust_flag:
        return True

    # Check trust store
    from bog_agents_cli.mcp_trust import (
        compute_config_fingerprint,
        is_project_mcp_trusted,
        trust_project_mcp,
    )

    project_root = str(
        (project_context.project_root or project_context.user_cwd).resolve()
    )
    fingerprint = compute_config_fingerprint(project_configs)

    if is_project_mcp_trusted(project_root, fingerprint):
        return True

    # Interactive prompt
    from rich.console import Console as _Console

    prompt_console = _Console(stderr=True)
    prompt_console.print()
    prompt_console.print(
        "[bold yellow]Project MCP servers require approval:[/bold yellow]"
    )
    for name, cmd, args in all_stdio:
        args_str = " ".join(args) if args else ""
        prompt_console.print(f'  [bold]"{name}"[/bold]:  {cmd} {args_str}')
    prompt_console.print()

    # In non-interactive contexts (cron, CI, daemon, redirected stdin) never
    # block on input(): default to deny so the host process cannot hang.
    if not sys.stdin.isatty():
        prompt_console.print(
            "[dim]stdin is not a TTY — denying project MCP servers (set BOG_AGENTS_MCP_TRUST=1 to override).[/dim]"
        )
        return False

    try:
        answer = input("Allow? [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = ""

    if answer == "y":
        trust_project_mcp(project_root, fingerprint)
        return True
    return False


def _run_doctor(console: Any) -> None:  # noqa: ANN401
    """Run environment diagnostics and print results.

    Checks Python version, package versions, optional tools, API keys,
    and configuration status.

    Args:
        console: Rich console for output.
    """
    from bog_agents_cli.doctor import run_doctor

    console.print(run_doctor(), markup=False)


_TERMINAL_RESTORE_SEQUENCES: tuple[str, ...] = (
    # Disable mouse tracking in every protocol Textual / prompt-toolkit
    # might have enabled. If the TUI exits abnormally (uncaught
    # exception, SIGTERM, agent crash mid-launch) the terminal is left
    # in mouse-tracking mode, and any subsequent click in the user's
    # shell shows up as garbage like `[<35;57;14M[` on stdin.
    "\033[?1003l",  # disable any-event mouse tracking
    "\033[?1002l",  # disable button-event tracking
    "\033[?1000l",  # disable basic mouse tracking
    "\033[?1006l",  # disable SGR mouse mode (the `<...M` form we observed)
    "\033[?1015l",  # disable urxvt mouse mode
    "\033[?2004l",  # disable bracketed-paste mode
    "\033[?25h",  # ensure cursor is visible
    "\033[?1049l",  # leave alternate screen buffer
)


def _restore_terminal() -> None:
    """Best-effort restore the terminal to a sane state.

    Runs as an atexit handler and from SIGTERM/SIGINT signal handlers.
    Idempotent: writing the disable sequences multiple times is safe.
    Silently skipped when stdout is not a TTY (piped output).
    """
    try:
        if not sys.stdout.isatty():
            return
        sys.stdout.write("".join(_TERMINAL_RESTORE_SEQUENCES))
        sys.stdout.flush()
    except (OSError, ValueError):
        # Closed stream during shutdown — nothing to restore.
        pass


def _install_terminal_restore_handlers() -> None:
    """Wire `_restore_terminal` to atexit + SIGTERM/SIGINT.

    Without this, a crash mid-Textual-startup leaves the terminal in
    mouse-tracking + alternate-screen mode and the user's shell shows
    SGR escape sequences as input (e.g. `[<35;57;14M[`).
    """
    import atexit
    import signal

    atexit.register(_restore_terminal)

    def _on_signal(_signum: int, _frame: object) -> None:
        _restore_terminal()
        # Re-raise the default signal so the process actually exits with
        # the right exit code (KeyboardInterrupt for SIGINT, etc.).
        sys.exit(130)

    for sig_name in ("SIGINT", "SIGTERM", "SIGHUP", "SIGBREAK"):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, _on_signal)
        except (OSError, ValueError):
            # Some platforms (Windows) don't allow setting all signals.
            pass


def cli_main() -> None:
    """Entry point for console script.

    Raises:
        SystemExit: Propagated from `sys.exit()` in subcommand dispatch
            paths (the standard argparse + subcommand exit pattern).
    """
    # Install terminal-restore handlers early — if any subsequent setup
    # crashes (e.g. before Textual takes over the screen) the terminal
    # still ends up in a sane state. Belt-and-suspenders for Textual's
    # own cleanup, which is bypassed on hard exits.
    _install_terminal_restore_handlers()

    # Install the panic-dump excepthook so any uncaught exception in
    # subsequent setup (or inside the Textual app) lands a redacted
    # crash report at ``~/.bog-agents/crash/<ts>.log``. Idempotent.
    try:
        from bog_agents_cli._panic import install_panic_handler

        install_panic_handler()
    except Exception:
        # Never let panic-handler installation itself prevent startup.
        logger.warning("panic handler install failed", exc_info=True)

    # Fix for gRPC fork issue on macOS
    # https://github.com/grpc/grpc/issues/37642
    if sys.platform == "darwin":
        os.environ["GRPC_ENABLE_FORK_SUPPORT"] = "0"

    # On legacy Windows consoles (cp1252) rich's checkmark/box-drawing glyphs
    # crash with UnicodeEncodeError. Reconfigure stdio to UTF-8 with a
    # replacement fallback so output never crashes the CLI.
    if sys.platform == "win32":
        for stream in (sys.stdout, sys.stderr):
            reconfigure = getattr(stream, "reconfigure", None)
            if reconfigure is not None:
                try:
                    reconfigure(encoding="utf-8", errors="replace")
                except (OSError, ValueError):
                    pass

    # Note: LANGSMITH_PROJECT is already overridden in config.py
    # (before LangChain imports). This ensures agent traces use
    # BOG_AGENTS_LANGSMITH_PROJECT while shell commands use the
    # user's original LANGSMITH_PROJECT (via LocalShellBackend env).

    # Fast path: print version without loading heavy dependencies
    if len(sys.argv) == 2 and sys.argv[1] in {
        "-v",
        "--version",
    }:  # argv length check for fast-path
        try:
            from importlib.metadata import (
                PackageNotFoundError,
                version as _pkg_version,
            )

            sdk_version = _pkg_version("bog-agents")
        except PackageNotFoundError:
            sdk_version = "unknown"
        except Exception:  # Best-effort SDK version lookup
            logger.debug("Unexpected error looking up SDK version", exc_info=True)
            sdk_version = "unknown"
        print(f"bog-agents-cli {__version__}\nbog-agents (SDK) {sdk_version}")  # noqa: T201  # CLI version output
        sys.exit(0)

    # --doctor fast path: skip Textual dependency checks
    if len(sys.argv) == 2 and sys.argv[1] == "--doctor":
        from rich.console import Console as _DoctorConsole

        _run_doctor(_DoctorConsole())
        sys.exit(0)

    # --doctor-deep fast path: like --doctor but probes external deps too.
    if len(sys.argv) == 2 and sys.argv[1] == "--doctor-deep":
        from rich.console import Console as _DoctorConsole

        from bog_agents_cli.doctor_deep import run_deep_doctor

        console = _DoctorConsole()
        console.print(run_deep_doctor(), markup=False)
        sys.exit(0)

    # ACP/serve modes do not require Textual, so skip UI dependency checks.
    if "--acp" not in sys.argv[1:] and "--serve" not in sys.argv[1:]:
        check_cli_dependencies()

    # Translate the user's ``[timeouts]`` settings into env vars before any
    # SDK code reads them. Existing env values win, so a shell override
    # (``BOG_AGENTS_MODEL_READ_TIMEOUT=300 bog ...``) still takes precedence
    # over what's in settings.json.
    try:
        from bog_agents_cli.timeouts import apply_to_env as _apply_timeout_env

        project_root = Path.cwd() if (Path.cwd() / ".bog-agents").is_dir() else None
        _apply_timeout_env(project_root=project_root)
    except Exception:
        # Misconfigured timeouts must never block startup. Log and continue
        # with whatever defaults the SDK and remote_client provide.
        logger.warning("timeouts: failed to apply settings cascade", exc_info=True)

    from bog_agents_cli.config import console, settings

    try:
        args = parse_args()

        model_params: dict[str, Any] | None = None
        raw_kwargs = getattr(args, "model_params", None)
        if raw_kwargs:
            try:
                model_params = json.loads(raw_kwargs)
            except json.JSONDecodeError as e:
                console.print(
                    f"[bold red]Error:[/bold red] --model-params is not valid JSON: {e}"
                )
                sys.exit(1)
            if not isinstance(model_params, dict):
                console.print(
                    "[bold red]Error:[/bold red] --model-params must be a JSON object"
                )
                sys.exit(1)

        profile_override: dict[str, Any] | None = None
        raw_profile = getattr(args, "profile_override", None)
        if raw_profile:
            try:
                profile_override = json.loads(raw_profile)
            except json.JSONDecodeError as e:
                console.print(
                    "[bold red]Error:[/bold red] "
                    f"--profile-override is not valid JSON: {e}"
                )
                sys.exit(1)
            if not isinstance(profile_override, dict):
                console.print(
                    "[bold red]Error:[/bold red] "
                    "--profile-override must be a JSON object"
                )
                sys.exit(1)

        if getattr(args, "acp", False):
            try:
                from acp import run_agent as run_acp_agent
                from bog_agents_acp.server import AgentServerACP
            except ImportError as exc:
                msg = (
                    f"ACP dependencies not available: {exc}\n"
                    "Install with: pip install 'bog-agents-cli[acp]'\n"
                    "  or: uv add 'bog-agents-cli[acp]'\n"
                )
                sys.stderr.write(msg)
                sys.stderr.flush()
                sys.exit(1)

            if getattr(args, "no_mcp", False) and getattr(args, "mcp_config", None):
                msg = "Error: --no-mcp and --mcp-config are mutually exclusive\n"
                sys.stderr.write(msg)
                sys.stderr.flush()
                sys.exit(2)

            exit_code = asyncio.run(
                _run_acp_cli_async(
                    assistant_id=args.agent,
                    run_acp_agent=run_acp_agent,
                    agent_server_cls=AgentServerACP,
                    model_name=getattr(args, "model", None),
                    model_params=model_params,
                    profile_override=profile_override,
                    mcp_config_path=getattr(args, "mcp_config", None),
                    no_mcp=getattr(args, "no_mcp", False),
                    trust_project_mcp=getattr(args, "trust_project_mcp", False),
                )
            )
            sys.exit(exit_code)

        # --serve: start HTTP API server
        if getattr(args, "serve", False):
            try:
                from bog_agents.serve import AgentServer, ServerConfig
            except ImportError as exc:
                msg = (
                    f"Serve dependencies not available: {exc}\n"
                    "Install with: pip install 'bog-agents[serve]'\n"
                    "  or: uv add 'bog-agents[serve]'\n"
                )
                sys.stderr.write(msg)
                sys.stderr.flush()
                sys.exit(1)

            from bog_agents_cli.config import create_model_with_fallback

            model_result = create_model_with_fallback(
                getattr(args, "model", None),
                extra_kwargs=model_params,
                profile_overrides=profile_override,
            )
            model_result.apply_to_settings()

            from bog_agents.graph import create_agent as _create_agent

            agent = _create_agent(model=model_result.model)
            server_config = ServerConfig(
                host=getattr(args, "serve_host", "127.0.0.1"),
                port=getattr(args, "serve_port", 8080),
            )
            server = AgentServer(agent=agent, config=server_config)

            console.print(
                f"Starting bog-agents HTTP server on "
                f"{server_config.host}:{server_config.port}"
            )
            server.run()
            sys.exit(0)

        # --drive / --drive-stdin: scripted TUI run, JSONL out, no chrome.
        if getattr(args, "drive_script", None) or getattr(args, "drive_stdin", False):
            from bog_agents_cli.drive.entrypoint import (
                parse_var_overrides,
                run_drive_entrypoint,
            )

            exit_code = run_drive_entrypoint(
                script_path=getattr(args, "drive_script", None),
                from_stdin=bool(getattr(args, "drive_stdin", False)),
                artifact_dir=getattr(args, "drive_artifacts", None),
                var_overrides=parse_var_overrides(getattr(args, "drive_vars", None)),
                stop_on_failure=bool(getattr(args, "drive_stop_on_failure", False)),
                output_path=getattr(args, "drive_output", None),
            )
            sys.exit(exit_code)

        # --pr: PR-output mode (requires -n)
        if getattr(args, "pr", False):
            task = getattr(args, "non_interactive_message", None) or getattr(
                args, "print_message", None
            )
            if not task:
                sys.stderr.write(
                    "Error: --pr requires a task via -n or --print.\n"
                    "Usage: bog-agents -n 'fix issue #123' --pr\n"
                )
                sys.stderr.flush()
                sys.exit(2)

            from bog_agents_cli.config import create_model_with_fallback
            from bog_agents_cli.pr_output import PRConfig, run_pr_mode

            model_result = create_model_with_fallback(
                getattr(args, "model", None),
                extra_kwargs=model_params,
                profile_overrides=profile_override,
            )
            model_result.apply_to_settings()

            from bog_agents.graph import create_agent as _create_agent

            agent = _create_agent(model=model_result.model)
            pr_config = PRConfig(
                base_branch=getattr(args, "pr_base", "main"),
                draft=getattr(args, "pr_draft", False),
            )

            console.print(f"Running PR mode: {task}")
            pr_result = asyncio.run(run_pr_mode(task, agent, config=pr_config))

            if pr_result.success:
                console.print(
                    f"[bold green]PR created:[/bold green] {pr_result.pr_url}"
                )
                console.print(f"Branch: {pr_result.branch_name}")
                console.print(f"Files changed: {len(pr_result.files_changed)}")
                console.print(f"Duration: {pr_result.duration_seconds:.1f}s")
            else:
                console.print(f"[bold red]PR failed:[/bold red] {pr_result.error}")
                sys.exit(1)
            sys.exit(0)

        # --print is a convenience alias for -n TEXT -q
        if getattr(args, "print_message", None):
            args.non_interactive_message = args.print_message
            args.quiet = True

        # Resolve the unified --permission-mode / --dangerously-skip-permissions
        # into the legacy approval flags (auto_approve/auto_mode/always_ask/
        # plan_mode) before any dispatch reads them. Exits 2 on conflicting flags.
        _normalize_permission_mode(args)

        # --prompt and --pipeline expand into a non-interactive task.
        # Resolution: read the named prompt/pipeline from disk, substitute
        # variables for prompts, inline pipeline steps for pipelines.
        prompt_name = getattr(args, "prompt_name", None)
        pipeline_name = getattr(args, "pipeline_name", None)
        if getattr(args, "prompt_vars", None) and not prompt_name:
            sys.stderr.write(
                "Warning: --prompt-vars has no effect without --prompt.\n",
            )
            sys.stderr.flush()
        if prompt_name and pipeline_name:
            sys.stderr.write(
                "Error: --prompt and --pipeline are mutually exclusive.\n",
            )
            sys.stderr.flush()
            sys.exit(2)
        if (prompt_name or pipeline_name) and args.non_interactive_message:
            sys.stderr.write(
                "Error: --prompt/--pipeline cannot be combined with -n/-p; "
                "they each replace the task.\n",
            )
            sys.stderr.flush()
            sys.exit(2)

        if prompt_name:
            try:
                from bog_agents_cli.prompts import resolve_prompt
            except ImportError:
                import tomllib

                def resolve_prompt(name: str, variables: dict) -> str:  # type: ignore[no-redef]
                    cands = [
                        Path.cwd() / ".bog-agents" / "prompt_library.toml",
                        Path.home() / ".bog-agents" / "prompt_library.toml",
                    ]
                    for p in cands:
                        if p.is_file():
                            data = tomllib.loads(p.read_text(encoding="utf-8"))
                            entry = (data.get("prompts") or {}).get(name)
                            if not entry:
                                continue
                            body = entry.get("body", "")
                            for k, v in (variables or {}).items():
                                body = body.replace("{{" + k + "}}", str(v))
                            return body
                    msg = f"Prompt '{name}' not found in prompt_library.toml"
                    raise ValueError(msg)

            try:
                prompt_vars = json.loads(args.prompt_vars) if args.prompt_vars else {}
            except json.JSONDecodeError as exc:
                sys.stderr.write(f"Error: --prompt-vars is not valid JSON: {exc}\n")
                sys.stderr.flush()
                sys.exit(2)
            try:
                args.non_interactive_message = resolve_prompt(prompt_name, prompt_vars)
            except (FileNotFoundError, ValueError) as exc:
                sys.stderr.write(f"Error: --prompt: {exc}\n")
                sys.stderr.flush()
                sys.exit(2)

        if pipeline_name:
            import yaml as _yaml

            cands = [
                Path.cwd() / ".bog-agents" / "pipelines" / f"{pipeline_name}.yaml",
                Path.cwd() / ".bog-agents" / "pipelines" / f"{pipeline_name}.yml",
                Path.home() / ".bog-agents" / "pipelines" / f"{pipeline_name}.yaml",
                Path.home() / ".bog-agents" / "pipelines" / f"{pipeline_name}.yml",
            ]
            yaml_path = next((c for c in cands if c.is_file()), None)
            if yaml_path is None:
                sys.stderr.write(
                    f"Error: --pipeline: '{pipeline_name}' not found "
                    f"under .bog-agents/pipelines or ~/.bog-agents/pipelines\n",
                )
                sys.stderr.flush()
                sys.exit(2)
            data = _yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
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
            args.non_interactive_message = "\n".join(lines)

        # --doctor: run diagnostics and exit
        if getattr(args, "doctor", False):
            _run_doctor(console)
            sys.exit(0)

        # Apply shell-allow-list from command line if provided (overrides env var)
        if args.shell_allow_list:
            from bog_agents_cli.config import parse_shell_allow_list

            try:
                settings.shell_allow_list = parse_shell_allow_list(
                    args.shell_allow_list
                )
            except ValueError as exc:
                sys.stderr.write(f"Error: --shell-allow-list: {exc}\n")
                sys.stderr.flush()
                sys.exit(2)

        # Only slurp piped stdin as a prompt for the bare interactive/-n path.
        # Subcommands own their own stdin semantics — most importantly
        # `mcp-server`, whose stdin IS the MCP JSON-RPC channel; reading it here
        # would consume the protocol handshake and break the server.
        if getattr(args, "command", None) is None:
            apply_stdin_pipe(args)

        if getattr(args, "no_mcp", False) and getattr(args, "mcp_config", None):
            from rich.console import Console as _Console

            _Console(stderr=True).print(
                "[bold red]Error:[/bold red] --no-mcp and --mcp-config "
                "are mutually exclusive"
            )
            sys.exit(2)

        if (args.quiet or args.no_stream) and not args.non_interactive_message:
            # Print to stderr (not the module-level stdout console) and exit
            # with code 2 to match the POSIX convention for usage errors, as
            # argparse's parser.error() would.
            from rich.console import Console as _Console

            flags = []
            if args.quiet:
                flags.append("--quiet")
            if args.no_stream:
                flags.append("--no-stream")
            flag = " and ".join(flags)
            _Console(stderr=True).print(
                f"[bold red]Error:[/bold red] {flag} requires "
                "--non-interactive (-n) or piped stdin"
            )
            sys.exit(2)

        # Handle --default-model / --clear-default-model (headless, no session)
        if args.clear_default_model:
            from bog_agents_cli.model_config import clear_default_model

            if clear_default_model():
                console.print("Default model cleared.")
            else:
                console.print(
                    "[bold red]Error:[/bold red] Could not clear default model. "
                    "Check permissions for ~/.bog-agents/"
                )
                sys.exit(1)
            sys.exit(0)

        if args.default_model is not None:
            from bog_agents_cli.model_config import (
                ModelConfig,
                save_default_model,
            )

            if args.default_model == "__SHOW__":
                config = ModelConfig.load()
                if config.default_model:
                    console.print(f"Default model: {config.default_model}")
                else:
                    console.print("No default model set.")
                sys.exit(0)

            model_spec = args.default_model
            # Auto-detect provider for bare model names
            from bog_agents_cli.config import detect_provider
            from bog_agents_cli.model_config import ModelSpec

            parsed = ModelSpec.try_parse(model_spec)
            if not parsed:
                provider = detect_provider(model_spec)
                if provider:
                    model_spec = f"{provider}:{model_spec}"

            if save_default_model(model_spec):
                console.print(f"Default model set to {model_spec}")
            else:
                console.print(
                    "[bold red]Error:[/bold red] Could not save default model. "
                    "Check permissions for ~/.bog-agents/"
                )
                sys.exit(1)
            sys.exit(0)

        output_format = getattr(args, "output_format", "text")

        if args.command == "help":
            from bog_agents_cli.ui import show_help

            show_help()
        elif args.command == "list":
            from bog_agents_cli.agent import list_agents

            list_agents(output_format=output_format)
        elif args.command == "reset":
            from bog_agents_cli.agent import reset_agent

            reset_agent(args.agent, args.source_agent, output_format=output_format)
        elif args.command == "skills":
            from bog_agents_cli.skills import execute_skills_command

            execute_skills_command(args)
        elif args.command == "daemon":
            from bog_agents_cli.cmd_daemon import execute_daemon_command

            execute_daemon_command(args)
        elif args.command == "verify":
            from bog_agents_cli.cmd_verify import cmd_verify

            sys.exit(cmd_verify(args))
        elif args.command == "call":
            from bog_agents_cli.cmd_call import cmd_call

            sys.exit(cmd_call(args))
        elif args.command == "command":
            from bog_agents_cli.headless_commands import run_headless_command

            sys.exit(run_headless_command(args.slash, output_format=output_format))
        elif args.command == "mcp-server":
            from bog_agents_cli.mcp_server import run_mcp_server

            sys.exit(
                asyncio.run(
                    run_mcp_server(
                        model_name=getattr(args, "model", None),
                        permission_mode=getattr(args, "permission_mode", None)
                        or "acceptEdits",
                        cwd=getattr(args, "mcp_cwd", None),
                    )
                )
            )
        elif args.command == "test-bedrock":
            from bog_agents_cli._bedrock import probe_bedrock, render_probe_report

            steps = probe_bedrock(model_id=args.model, region=args.region)
            print(render_probe_report(steps))  # noqa: T201  # CLI subcommand output
            # Exit non-zero if any step failed so CI / scripts can branch.
            sys.exit(0 if all(s.ok for s in steps) else 1)
        elif args.command == "threads":
            from bog_agents_cli.sessions import (
                delete_thread_command,
                list_threads_command,
            )
            from bog_agents_cli.ui import show_threads_help

            # "ls" is an argparse alias for "list" — argparse stores the
            # alias as-is in the namespace, so we must match both values.
            if args.threads_command in {"list", "ls"}:
                asyncio.run(
                    list_threads_command(
                        agent_name=getattr(args, "agent", None),
                        limit=getattr(args, "limit", None),
                        sort_by=getattr(args, "sort", None),
                        branch=getattr(args, "branch", None),
                        verbose=getattr(args, "verbose", False),
                        relative=getattr(args, "relative", None),
                        output_format=output_format,
                    )
                )
            elif args.threads_command == "delete":
                asyncio.run(
                    delete_thread_command(args.thread_id, output_format=output_format)
                )
            else:
                # No subcommand provided, show threads help screen
                show_threads_help()
        elif args.non_interactive_message:
            # Optional-tool warnings (e.g. missing ripgrep) used to fire here
            # but were too noisy. The grep tool falls back to a pure-Python
            # search transparently; users who want the prompt can still call
            # ``check_optional_tools()`` directly.
            # Non-interactive mode - execute single task and exit
            from bog_agents_cli.non_interactive import run_non_interactive

            # Resolve -r in non-interactive mode so thread history is loaded
            # into the LangGraph checkpointer before the agent runs. This
            # mirrors the interactive path's resume logic but uses stderr-
            # safe error reporting so --quiet/--json output stays clean.
            resume_thread_id: str | None = None
            resume_arg = getattr(args, "resume_thread", None)
            if resume_arg:
                from bog_agents_cli.sessions import (
                    get_most_recent,
                    get_thread_agent,
                    thread_exists,
                )

                if resume_arg == "__MOST_RECENT__":
                    agent_filter = (
                        args.agent if args.agent != _DEFAULT_AGENT_NAME else None
                    )
                    resume_thread_id = asyncio.run(get_most_recent(agent_filter))
                    if resume_thread_id and args.agent == _DEFAULT_AGENT_NAME:
                        resolved_agent = asyncio.run(get_thread_agent(resume_thread_id))
                        if resolved_agent:
                            args.agent = resolved_agent
                elif asyncio.run(thread_exists(resume_arg)):
                    resume_thread_id = resume_arg
                    if args.agent == _DEFAULT_AGENT_NAME:
                        resolved_agent = asyncio.run(get_thread_agent(resume_thread_id))
                        if resolved_agent:
                            args.agent = resolved_agent
                else:
                    sys.stderr.write(f"Error: thread '{resume_arg}' not found.\n")
                    sys.exit(2)

            # Validate --mcp-config early so a missing/unreadable file produces
            # a clean one-line error instead of a deep traceback through the
            # agent setup. This mirrors the interactive path's check above.
            mcp_config_arg = getattr(args, "mcp_config", None)
            if mcp_config_arg:
                from pathlib import Path as _Path

                if not _Path(mcp_config_arg).is_file():
                    sys.stderr.write(
                        f"Error: --mcp-config file not found: {mcp_config_arg}\n",
                    )
                    sys.stderr.flush()
                    sys.exit(2)

            # Validate the configured model's API key is present BEFORE we spin
            # up the agent. Without this, missing creds surface as a deep
            # traceback ending in "Could not resolve authentication method"
            # after the agent is already running.
            model_arg = getattr(args, "model", None)
            try:
                from bog_agents_cli.config import detect_provider
                from bog_agents_cli.model_config import (
                    PROVIDER_API_KEY_ENV,
                    ModelConfig,
                )

                spec_for_creds = model_arg
                if spec_for_creds is None:
                    cfg = ModelConfig.load()
                    spec_for_creds = cfg.default_model or cfg.recent_model
                if spec_for_creds:
                    if ":" in spec_for_creds:
                        provider = spec_for_creds.split(":", 1)[0].lower()
                    else:
                        provider = (detect_provider(spec_for_creds) or "").lower()
                    env_var = PROVIDER_API_KEY_ENV.get(provider)
                    # Local providers (ollama) don't need an API key; bedrock/
                    # vertexai use other auth flows. Skip the simple env-var
                    # gate for them; bedrock gets a dedicated boto3 probe
                    # below.
                    if (
                        env_var
                        and provider
                        not in ("ollama", "bedrock", "bedrock_converse", "vertexai")
                        and not os.environ.get(env_var)
                    ):
                        sys.stderr.write(
                            f"Error: model '{spec_for_creds}' requires "
                            f"{env_var} to be set in the environment.\n"
                            f"Hint: export {env_var}=... or set it in "
                            f"~/.bog-agents/.env\n",
                        )
                        sys.stderr.flush()
                        sys.exit(2)
                    # Bedrock pre-flight: probe boto3's credential chain so
                    # an expired SSO token surfaces as a clean one-line
                    # message instead of as a generic "internal error
                    # occurred" wrapped through langgraph's RemoteException.
                    if provider in ("bedrock", "bedrock_converse"):
                        try:
                            from bog_agents_cli.doctor import (
                                _bedrock_credential_status,
                            )

                            status, detail = _bedrock_credential_status()
                        except (
                            Exception
                        ):  # pre-flight only — never block on its own bug
                            status, detail = "OK", "probe-skipped"
                        if status == "FAIL":
                            sys.stderr.write(
                                f"Error: Bedrock credentials unavailable: {detail}\n"
                            )
                            sys.stderr.flush()
                            sys.exit(2)
            except SystemExit:
                raise
            except Exception:  # pre-flight only; agent will surface real errors
                logger.debug("API key pre-flight check failed", exc_info=True)

            exit_code = asyncio.run(
                run_non_interactive(
                    message=args.non_interactive_message,
                    assistant_id=args.agent,
                    model_name=getattr(args, "model", None),
                    model_params=model_params,
                    profile_override=profile_override,
                    sandbox_type=args.sandbox,
                    sandbox_id=args.sandbox_id,
                    sandbox_setup=getattr(args, "sandbox_setup", None),
                    quiet=args.quiet,
                    stream=not args.no_stream,
                    output_format=getattr(args, "output_format", "text"),
                    mcp_config_path=getattr(args, "mcp_config", None),
                    no_mcp=getattr(args, "no_mcp", False),
                    trust_project_mcp=getattr(args, "trust_project_mcp", False),
                    auto_commit=getattr(args, "auto_commit", False),
                    resume_thread_id=resume_thread_id,
                    auto_approve=getattr(args, "auto_approve", False),
                    always_ask=getattr(args, "always_ask", False),
                    auto_mode=getattr(args, "auto_mode", False),
                    plan_mode=getattr(args, "plan_mode", False),
                )
            )
            sys.exit(exit_code)
        else:
            # Interactive mode - handle thread resume
            from rich.style import Style
            from rich.text import Text

            from bog_agents_cli.config import (
                build_langsmith_thread_url,
            )
            from bog_agents_cli.sessions import (
                find_similar_threads,
                generate_thread_id,
                get_most_recent,
                get_thread_agent,
                thread_exists,
            )

            thread_id = None

            if args.resume_thread == "__MOST_RECENT__":
                # -r (no ID): Get most recent thread
                # If --agent specified, filter by that agent; otherwise get
                # most recent overall
                agent_filter = args.agent if args.agent != _DEFAULT_AGENT_NAME else None
                thread_id = asyncio.run(get_most_recent(agent_filter))
                if thread_id:
                    agent_name = asyncio.run(get_thread_agent(thread_id))
                    if agent_name:
                        args.agent = agent_name
                else:
                    if agent_filter:
                        msg = Text("No previous thread for '", style="yellow")
                        msg.append(args.agent)
                        msg.append("', starting new.", style="yellow")
                    else:
                        msg = Text("No previous threads, starting new.", style="yellow")
                    console.print(msg)

            elif args.resume_thread:
                # -r <ID>: Resume specific thread
                if asyncio.run(thread_exists(args.resume_thread)):
                    thread_id = args.resume_thread
                    if args.agent == _DEFAULT_AGENT_NAME:
                        agent_name = asyncio.run(get_thread_agent(thread_id))
                        if agent_name:
                            args.agent = agent_name
                else:
                    error_msg = Text("Thread '", style="red")
                    error_msg.append(args.resume_thread)
                    error_msg.append("' not found.", style="red")
                    console.print(error_msg)

                    # Check for similar thread IDs
                    similar = asyncio.run(find_similar_threads(args.resume_thread))
                    if similar:
                        console.print()
                        console.print("[yellow]Did you mean?[/yellow]")
                        for tid in similar:
                            hint = Text("  bog-agents -r ", style="cyan")
                            hint.append(str(tid), style="cyan")
                            console.print(hint)
                        console.print()

                    console.print(
                        "[dim]Use 'bog-agents threads list' to see "
                        "available threads.[/dim]"
                    )
                    console.print(
                        "[dim]Use 'bog-agents -r' to resume the most "
                        "recent thread.[/dim]"
                    )
                    sys.exit(1)

            # Generate new thread ID if not resuming
            if thread_id is None:
                thread_id = generate_thread_id()

            # Check project MCP trust before launching TUI
            mcp_trust_decision = _check_mcp_project_trust(
                trust_flag=getattr(args, "trust_project_mcp", False),
            )

            # Run Textual CLI
            return_code = 0
            try:
                result = asyncio.run(
                    run_textual_cli_async(
                        assistant_id=args.agent,
                        auto_approve=args.auto_approve,
                        always_ask=getattr(args, "always_ask", False),
                        auto_mode=getattr(args, "auto_mode", False),
                        plan_mode=getattr(args, "plan_mode", False),
                        auto_commit=getattr(args, "auto_commit", False),
                        sandbox_type=args.sandbox,
                        sandbox_id=args.sandbox_id,
                        sandbox_setup=getattr(args, "sandbox_setup", None),
                        model_name=getattr(args, "model", None),
                        model_params=model_params,
                        profile_override=profile_override,
                        thread_id=thread_id,
                        initial_prompt=getattr(args, "initial_prompt", None),
                        mcp_config_path=getattr(args, "mcp_config", None),
                        no_mcp=getattr(args, "no_mcp", False),
                        trust_project_mcp=mcp_trust_decision,
                    )
                )
                return_code = result.return_code
                # The user may have switched threads via /threads during the
                # session; use the final thread ID for teardown messages.
                thread_id = result.thread_id or thread_id
                _print_session_stats(result.session_stats, console)
            except Exception as e:  # Top-level error handler for the application
                error_msg = Text("\nApplication error: ", style="red")
                error_msg.append(str(e))
                console.print(error_msg)
                console.print(Text(traceback.format_exc(), style="dim"))
                sys.exit(1)

            # Show LangSmith thread link for threads with checkpointed
            # content (same table that backs the `/threads` listing).
            try:
                thread_url = build_langsmith_thread_url(thread_id)
                if thread_url and asyncio.run(thread_exists(thread_id)):
                    console.print()
                    ls_hint = Text("View this thread in LangSmith: ", style="dim")
                    ls_hint.append(
                        thread_url,
                        style=Style(dim=True, link=thread_url),
                    )
                    console.print(ls_hint)
            except Exception:
                logger.debug(
                    "Could not display LangSmith thread URL on teardown",
                    exc_info=True,
                )

            # Show resume hint on exit for threads with checkpointed content.
            if thread_id and return_code == 0 and asyncio.run(thread_exists(thread_id)):
                console.print()
                console.print("[dim]Resume this thread with:[/dim]")
                hint = Text("bog-agents -r ", style="cyan")
                hint.append(str(thread_id), style="cyan")
                console.print(hint)
    except KeyboardInterrupt:
        # Clean exit on Ctrl+C - suppress ugly traceback
        console.print("\n\n[yellow]Interrupted[/yellow]")
        sys.exit(0)


if __name__ == "__main__":
    cli_main()
