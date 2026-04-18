"""Textual UI application for bog-agents-cli."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import signal
import sys
import time
import uuid
import webbrowser
from collections import deque
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Literal, cast

from rich.text import Text
from textual.app import App
from textual.binding import Binding, BindingType
from textual.containers import Container, VerticalScroll
from textual.css.query import NoMatches
from textual.message import Message
from textual.screen import ModalScreen

from bog_agents_cli._debug import configure_debug_logging
from bog_agents_cli.clipboard import copy_selection_to_clipboard, read_clipboard_text
from bog_agents_cli.config import (
    DOCS_URL,
    SHELL_TOOL_NAMES,
    build_langsmith_thread_url,
    create_model,
    detect_provider,
    is_shell_command_allowed,
    newline_shortcut,
    settings,
)
from bog_agents_cli.configurable_model import CLIContext
from bog_agents_cli.hooks import dispatch_hook
from bog_agents_cli.model_config import ModelSpec, save_recent_model
from bog_agents_cli.textual_adapter import (
    SessionStats,
    TextualUIAdapter,
    _get_git_branch,
    execute_task_textual,
    format_token_count,
)
from bog_agents_cli.widgets.approval import ApprovalMenu
from bog_agents_cli.widgets.ask_user import AskUserMenu
from bog_agents_cli.widgets.chat_input import ChatInput
from bog_agents_cli.widgets.loading import LoadingWidget
from bog_agents_cli.widgets.message_store import (
    MessageData,
    MessageStore,
    MessageType,
    ToolStatus,
)
from bog_agents_cli.widgets.messages import (
    AppMessage,
    AssistantMessage,
    ErrorMessage,
    QueuedUserMessage,
    ToolCallMessage,
    UserMessage,
)
from bog_agents_cli.widgets.model_selector import ModelSelectorScreen
from bog_agents_cli.widgets.status import StatusBar
from bog_agents_cli.widgets.thread_selector import (
    DeleteThreadConfirmScreen,
    ThreadSelectorScreen,
)
from bog_agents_cli.widgets.welcome import WelcomeBanner

logger = logging.getLogger(__name__)
configure_debug_logging(logger)
_monotonic = time.monotonic

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from bog_agents.backends import CompositeBackend
    from bog_agents.backends.protocol import BackendProtocol
    from bog_agents.middleware.summarization import SummarizationMiddleware
    from langchain_core.runnables import RunnableConfig
    from langgraph.pregel import Pregel
    from textual.app import ComposeResult
    from textual.events import Click, MouseDown, MouseMove, MouseUp, Paste
    from textual.scrollbar import ScrollUp
    from textual.widget import Widget
    from textual.widgets import Static
    from textual.worker import Worker

    from bog_agents_cli.ask_user import AskUserWidgetResult, Question
    from bog_agents_cli.mcp_tools import MCPServerInfo
    from bog_agents_cli.pipeline import Pipeline
    from bog_agents_cli.remote_client import RemoteAgent
    from bog_agents_cli.server import ServerProcess
    from bog_agents_cli.widgets.pipeline_screen import PipelineRunRequest

# iTerm2 Cursor Guide Workaround
# ===============================
# iTerm2's cursor guide (highlight cursor line) causes visual artifacts when
# Textual takes over the terminal in alternate screen mode. We disable it at
# module load and restore on exit. Both atexit and exit() override are used
# for defense-in-depth: atexit catches abnormal termination (SIGTERM, unhandled
# exceptions), while exit() ensures restoration before Textual's cleanup.

# Detection: check env vars AND that stderr is a TTY (avoids false positives
# when env vars are inherited but running in non-TTY context like CI)
_IS_ITERM = (
    (
        os.environ.get("LC_TERMINAL", "") == "iTerm2"
        or os.environ.get("TERM_PROGRAM", "") == "iTerm.app"
    )
    and hasattr(os, "isatty")
    and os.isatty(2)
)

# iTerm2 cursor guide escape sequences (OSC 1337)
# Format: OSC 1337 ; HighlightCursorLine=<yes|no> ST
# Where OSC = ESC ] (0x1b 0x5d) and ST = ESC \ (0x1b 0x5c)
_ITERM_CURSOR_GUIDE_OFF = "\x1b]1337;HighlightCursorLine=no\x1b\\"
_ITERM_CURSOR_GUIDE_ON = "\x1b]1337;HighlightCursorLine=yes\x1b\\"


def _format_compact_limit(
    keep: tuple[str, int | float], context_limit: int | None
) -> str:
    """Format compact retention settings into a human-readable limit string.

    Args:
        keep: Retention policy tuple from summarization defaults.
        context_limit: Model context limit when available.

    Returns:
        A short display string describing the compact retention limit.
    """
    keep_type, keep_value = keep

    if keep_type == "messages":
        count = int(keep_value)
        noun = "message" if count == 1 else "messages"
        return f"last {count} {noun}"

    if keep_type == "tokens":
        return f"{format_token_count(int(keep_value))} tokens"

    if keep_type == "fraction":
        percent = float(keep_value) * 100
        if context_limit is not None:
            token_limit = max(1, int(context_limit * float(keep_value)))
            return f"{format_token_count(token_limit)} tokens"
        return f"{percent:.0f}% of context window"

    return "current retention threshold"


def _write_iterm_escape(sequence: str) -> None:
    """Write an iTerm2 escape sequence to stderr.

    Silently fails if the terminal is unavailable (redirected, closed, broken
    pipe). This is a cosmetic feature, so failures should never crash the app.
    """
    if not _IS_ITERM:
        return
    try:
        import sys

        if sys.__stderr__ is not None:
            sys.__stderr__.write(sequence)
            sys.__stderr__.flush()
    except OSError:
        # Terminal may be unavailable (redirected, closed, broken pipe)
        pass


# Disable cursor guide at module load (before Textual takes over)
_write_iterm_escape(_ITERM_CURSOR_GUIDE_OFF)

if _IS_ITERM:
    import atexit

    def _restore_cursor_guide() -> None:
        """Restore iTerm2 cursor guide on exit.

        Registered with atexit to ensure the cursor guide is re-enabled
        when the CLI exits, regardless of how the exit occurs.
        """
        _write_iterm_escape(_ITERM_CURSOR_GUIDE_ON)

    atexit.register(_restore_cursor_guide)


def _extract_model_params_flag(raw_arg: str) -> tuple[str, dict[str, Any] | None]:
    """Extract `--model-params` and its JSON value from a `/model` arg string.

    Handles quoted (`'...'` / `"..."`) and bare `{...}` values with balanced
    braces so that JSON containing spaces works without quoting.

    Note:
        The bare-brace mode counts `{` / `}` characters without awareness of
        JSON string contents. Values that contain literal braces inside strings
        (e.g., `{"stop": "end}here"}`) will mis-parse. Users should quote the
        value in that case.

    Args:
        raw_arg: The argument string after `/model `.

    Returns:
        Tuple of `(remaining_args, parsed_dict | None)`. Returns `None` for the
            dict when the flag is absent.

    Raises:
        ValueError: If the value is missing, has unclosed quotes,
            unbalanced braces, or is not valid JSON.
        TypeError: If the parsed JSON is not a dict.
    """
    flag = "--model-params"
    idx = raw_arg.find(flag)
    if idx == -1:
        return raw_arg, None

    before = raw_arg[:idx].rstrip()
    after = raw_arg[idx + len(flag) :].lstrip()

    if not after:
        msg = "--model-params requires a JSON object value"
        raise ValueError(msg)

    # Determine the JSON string boundaries.
    if after[0] in {"'", '"'}:
        quote = after[0]
        end = -1
        backslash_count = 0
        for i, ch in enumerate(after[1:], start=1):
            if ch == "\\":
                backslash_count += 1
                continue
            if ch == quote and backslash_count % 2 == 0:
                end = i
                break
            backslash_count = 0
        if end == -1:
            msg = f"Unclosed {quote} in --model-params value"
            raise ValueError(msg)
        # Parse the quoted token with shlex so escaped quotes are unescaped.
        json_str = shlex.split(after[: end + 1], posix=True)[0]
        rest = after[end + 1 :].lstrip()
    elif after[0] == "{":
        # Walk forward to find the matching closing brace.
        depth = 0
        end = -1
        for i, ch in enumerate(after):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        if end == -1:
            msg = "Unbalanced braces in --model-params value"
            raise ValueError(msg)
        json_str = after[: end + 1]
        rest = after[end + 1 :].lstrip()
    else:
        # Non-brace, non-quoted — take the next whitespace-delimited token.
        parts = after.split(None, 1)
        json_str = parts[0]
        rest = parts[1] if len(parts) > 1 else ""

    remaining = f"{before} {rest}".strip()
    try:
        params = json.loads(json_str)
    except json.JSONDecodeError:
        msg = (
            f"Invalid JSON in --model-params: {json_str!r}. "
            'Expected format: --model-params \'{"key": "value"}\''
        )
        raise ValueError(msg) from None
    if not isinstance(params, dict):
        msg = "--model-params must be a JSON object, got " + type(params).__name__
        raise TypeError(msg)
    return remaining, params


InputMode = Literal["normal", "shell", "command"]


@dataclass(frozen=True, slots=True)
class QueuedMessage:
    """Represents a queued user message awaiting processing.

    Attributes:
        text: The message text content.
        mode: The input mode that determines message routing.
    """

    text: str
    mode: InputMode


@dataclass(slots=True)
class PreviewServerRecord:
    """Tracked preview server process for `/preview`."""

    preview_id: str
    command: str
    cwd: str
    port: int | None = None
    url: str | None = None
    process: asyncio.subprocess.Process | None = None
    started_at: float = field(default_factory=time.time)


@dataclass(slots=True)
class RecordingSessionState:
    """Ephemeral replay-recording state for `/record`."""

    session_id: str
    name: str
    thread_id: str
    cwd: str
    started_at: float
    baseline_message_count: int = 0


class TextualTokenTracker:
    """Token tracker that updates the status bar."""

    def __init__(
        self,
        update_callback: Callable[[int], None],
        hide_callback: Callable[[], None] | None = None,
    ) -> None:
        """Initialize with callbacks to update the display."""
        self._update_callback = update_callback
        self._hide_callback = hide_callback
        self.current_context = 0

    def add(self, total_tokens: int, _output_tokens: int = 0) -> None:
        """Update token count from a response.

        Args:
            total_tokens: Total context tokens (input + output from usage_metadata)
            _output_tokens: Unused, kept for backwards compatibility
        """
        self.current_context = total_tokens
        self._update_callback(self.current_context)

    def reset(self) -> None:
        """Reset token count."""
        self.current_context = 0
        self._update_callback(0)

    def hide(self) -> None:
        """Hide the token display (e.g., during streaming)."""
        if self._hide_callback:
            self._hide_callback()

    def show(self) -> None:
        """Show the token display with current value (e.g., after interrupt)."""
        self._update_callback(self.current_context)


def _new_thread_id() -> str:
    """Deferred-import wrapper around `sessions.generate_thread_id`.

    Returns:
        UUID7 string.
    """
    from bog_agents_cli.sessions import generate_thread_id

    return generate_thread_id()


def _read_clipboard_image() -> bytes | None:
    """Attempt to read raw image bytes from the system clipboard.

    Supports:
    - Linux (Wayland): wl-paste --type image/png
    - Linux (X11): xclip -selection clipboard -t image/png -o
    - macOS: pngpaste or osascript

    Returns:
        Raw PNG bytes if an image is on the clipboard, else None.
    """
    import shutil
    import subprocess  # noqa: S404  # clipboard helpers require subprocess
    import sys
    import tempfile
    from pathlib import Path

    try:
        if sys.platform == "darwin":
            if shutil.which("pngpaste"):
                with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
                    tmp = f.name
                result = subprocess.run(  # noqa: S603
                    ["pngpaste", tmp], capture_output=True, check=False
                )
                if result.returncode == 0:
                    data = Path(tmp).read_bytes()
                    Path(tmp).unlink(missing_ok=True)
                    return data or None
            return None

        if shutil.which("wl-paste"):
            result = subprocess.run(
                ["wl-paste", "--type", "image/png"],
                capture_output=True,
                check=False,
            )
            if result.returncode == 0 and result.stdout:
                return result.stdout

        if shutil.which("xclip"):
            result = subprocess.run(
                ["xclip", "-selection", "clipboard", "-t", "image/png", "-o"],
                capture_output=True,
                check=False,
            )
            if result.returncode == 0 and result.stdout:
                return result.stdout
    except (OSError, subprocess.SubprocessError):
        pass
    return None


async def _get_current_git_branch() -> str | None:
    """Return the current git branch name, or None if not in a git repo.

    Returns:
        Branch name string, or None.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "rev-parse",
            "--abbrev-ref",
            "HEAD",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode == 0:
            branch = stdout.decode().strip()
            return branch if branch != "HEAD" else None
    except (OSError, FileNotFoundError):
        pass
    return None


class TextualSessionState:
    """Session state for the Textual app."""

    def __init__(
        self,
        *,
        auto_approve: bool = False,
        thread_id: str | None = None,
    ) -> None:
        """Initialize session state.

        Args:
            auto_approve: Whether to auto-approve tool calls
            thread_id: Optional thread ID (generates UUID7 if not provided)
        """
        self.auto_approve = auto_approve
        self.thread_id = thread_id or _new_thread_id()

    def reset_thread(self) -> str:
        """Reset to a new thread.

        Returns:
            The new thread_id.
        """
        self.thread_id = _new_thread_id()
        return self.thread_id


_COMMAND_URLS: dict[str, str] = {
    "/changelog": "https://github.com/bogware/bog-agents/blob/main/libs/cli/CHANGELOG.md",
    "/docs": DOCS_URL,
    "/feedback": "https://github.com/bogware/bog-agents/issues/new/choose",
}

# Prompt for /remember command - triggers agent to review conversation and update
# memory/skills
REMEMBER_PROMPT = """Review our conversation and capture valuable knowledge. Focus especially on **best practices** we discussed or discovered—these are the most important things to preserve.

## Step 1: Identify Best Practices and Key Learnings

Scan the conversation for:

### Best Practices (highest priority)
- **Patterns that worked well** - approaches, techniques, or solutions we found effective
- **Anti-patterns to avoid** - mistakes, gotchas, or approaches that caused problems
- **Quality standards** - criteria we established for good code, documentation, or processes
- **Decision rationale** - why we chose one approach over another

### Other Valuable Knowledge
- Coding conventions and style preferences
- Project architecture decisions
- Workflows and processes we developed
- Tools, libraries, or techniques worth remembering
- Feedback I gave about your behavior or outputs

## Step 2: Decide Where to Store Each Learning

For each best practice or learning, choose the right destination:

### -> Memory (AGENTS.md) for preferences and guidelines
Use memory when the knowledge is:
- A preference or guideline (not a multi-step process)
- Something to always keep in mind
- A simple rule or pattern

**Global** (`~/.bog-agents/agent/AGENTS.md`): Universal preferences across all projects
**Project** (`.bog-agents/AGENTS.md`): Project-specific conventions and decisions

### -> Skill for reusable workflows and methodologies
**Create a skill when** we developed:
- A multi-step process worth reusing
- A methodology for a specific type of task
- A workflow with best practices baked in
- A procedure that should be followed consistently

Skills are more powerful than memory entries because they can encode **how** to do something well, not just **what** to remember.

## Step 3: Create Skills for Significant Best Practices

If we established best practices around a workflow or process, capture them in a skill.

**Example:** If we discussed best practices for code review, create a `code-review` skill that encodes those practices into a reusable workflow.

### Skill Location
`~/.bog-agents/agent/skills/<skill-name>/SKILL.md`

### Skill Structure
```
skill-name/
├── SKILL.md          (required - main instructions with best practices)
├── scripts/          (optional - executable code)
├── references/       (optional - detailed documentation)
└── assets/           (optional - templates, examples)
```

### SKILL.md Format
```markdown
---
name: skill-name
description: "What this skill does AND when to use it. Include triggers like 'when the user asks to X' or 'when working with Y'. This description determines when the skill activates."
---

# Skill Name

## Overview
Brief explanation of what this skill accomplishes.

## Best Practices
Capture the key best practices upfront:
- Best practice 1: explanation
- Best practice 2: explanation

## Process
Step-by-step instructions (imperative form):
1. First, do X
2. Then, do Y
3. Finally, do Z

## Common Pitfalls
- Pitfall to avoid and why
- Another anti-pattern we discovered
```

### Key Principles
1. **Encode best practices prominently** - Put them near the top so they guide the entire workflow
2. **Concise is key** - Only include non-obvious knowledge. Every paragraph should justify its token cost.
3. **Clear triggers** - The description determines when the skill activates. Be specific.
4. **Imperative form** - Write as commands: "Create a file" not "You should create a file"
5. **Include anti-patterns** - What NOT to do is often as valuable as what to do

## Step 4: Update Memory for Simpler Learnings

For preferences, guidelines, and simple rules that don't warrant a full skill:

```markdown
## Best Practices
- When doing X, always Y because Z
- Avoid A because it leads to B
```

Use `edit_file` to update existing files or `write_file` to create new ones.

## Step 5: Summarize Changes

List what you captured and where you stored it:
- Skills created (with key best practices encoded)
- Memory entries added (with location)
"""


class BogAgentsApp(App):
    """Main Textual application for bog-agents-cli."""

    TITLE = "Bog Agents"
    CSS_PATH = "app.tcss"
    ENABLE_COMMAND_PALETTE = False

    # Scroll speed (default is 3 lines per scroll event)
    SCROLL_SENSITIVITY_Y = 1.0
    _SELECTION_DRAG_THRESHOLD = 1

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "interrupt", "Interrupt", show=False, priority=True),
        Binding(
            "ctrl+c",
            "quit_or_interrupt",
            "Quit/Interrupt",
            show=False,
            priority=True,
        ),
        Binding("ctrl+d", "quit_app", "Quit", show=False, priority=True),
        Binding("ctrl+shift+c,ctrl+insert", "copy_selection", "Copy", show=False),
        Binding(
            "ctrl+shift+v,shift+insert",
            "paste_clipboard",
            "Paste",
            show=False,
        ),
        Binding("ctrl+t", "toggle_auto_approve", "Toggle Auto-Approve", show=False),
        Binding(
            "shift+tab",
            "toggle_auto_approve",
            "Toggle Auto-Approve",
            show=False,
            priority=True,
        ),
        Binding(
            "ctrl+e",
            "toggle_tool_output",
            "Toggle Tool Output",
            show=False,
            priority=True,
        ),
        # Approval menu keys (handled at App level for reliability)
        Binding("up", "approval_up", "Up", show=False),
        Binding("k", "approval_up", "Up", show=False),
        Binding("down", "approval_down", "Down", show=False),
        Binding("j", "approval_down", "Down", show=False),
        Binding("enter", "approval_select", "Select", show=False),
        Binding("y", "approval_yes", "Yes", show=False),
        Binding("1", "approval_yes", "Yes", show=False),
        Binding("2", "approval_auto", "Auto", show=False),
        Binding("a", "approval_auto", "Auto", show=False),
        Binding("3", "approval_no", "No", show=False),
        Binding("n", "approval_no", "No", show=False),
    ]
    _COMMAND_HANDLER_NAMES: ClassVar[dict[str, str]] = {
        "/agent": "_handle_agent_command",
        "/audit": "_handle_audit_command",
        "/background": "_dispatch_background_command",
        "/branch": "_handle_branch_command",
        "/changelog": "_handle_reference_url_command",
        "/clear": "_handle_clear_command",
        "/commands": "_handle_help_command",
        "/compact": "_handle_compact_command",
        "/context": "_handle_tokens_command",
        "/cost": "_handle_tokens_command",
        "/dashboard": "_dispatch_dashboard_command",
        "/diff": "_handle_diff_command",
        "/docs": "_handle_reference_url_command",
        "/doctor": "_handle_doctor_command",
        "/effort": "_handle_effort_command",
        "/extensions": "_handle_plugin_command",
        "/feedback": "_handle_reference_url_command",
        "/harbor": "_handle_harbor_command",
        "/health": "_handle_health_command",
        "/help": "_handle_help_command",
        "/image": "_handle_image_command",
        "/init": "_dispatch_init_command",
        "/infra": "_handle_infra_command",
        "/keybindings": "_handle_keybindings_command",
        "/logs": "_dispatch_logs_command",
        "/mcp": "_handle_mcp_command",
        "/migrate": "_handle_migrate_command",
        "/model": "_handle_model_command",
        "/onboard": "_dispatch_onboard_command",
        "/pipeline": "_handle_pipeline_command",
        "/plan": "_handle_plan_command",
        "/permissions": "_handle_permissions_command",
        "/plugin": "_handle_plugin_command",
        "/pr": "_handle_pr_command",
        "/prompt": "_handle_prompt_command",
        "/preview": "_handle_preview_command",
        "/profile": "_handle_profile_command",
        "/q": "_handle_quit_command",
        "/quit": "_handle_quit_command",
        "/record": "_handle_record_command",
        "/recommend": "_dispatch_recommend_command",
        "/reload": "_handle_reload_command",
        "/repomap": "_handle_repomap_command",
        "/remember": "_handle_remember_command",
        "/remote": "_handle_remote_command",
        "/replay": "_handle_replay_command",
        "/rewind": "_handle_rewind_command",
        "/resolve": "_handle_resolve_command",
        "/resume": "_handle_resume_command",
        "/review": "_handle_review_command",
        "/session": "_handle_session_command",
        "/settings": "_handle_settings_command",
        "/skills": "_handle_skills_command",
        "/test": "_handle_test_command",
        "/team": "_handle_team_command",
        "/think": "_handle_think_command",
        "/threads": "_handle_threads_command",
        "/tokens": "_handle_tokens_command",
        "/trace": "_handle_trace_command",
        "/rules": "_handle_rules_command",
        "/search": "_handle_search_command",
        "/undo": "_handle_undo_command",
        "/vars": "_handle_vars_command",
        "/version": "_handle_version_command",
        "/worktree": "_handle_worktree_command",
        "/worktrees": "_handle_worktrees_command",
    }

    class ServerReady(Message):
        """Posted by the background server-startup worker on success."""

        def __init__(
            self,
            agent: Any,  # noqa: ANN401
            server_proc: Any,  # noqa: ANN401
            mcp_server_info: list[Any] | None,
        ) -> None:
            super().__init__()
            self.agent = agent
            self.server_proc = server_proc
            self.mcp_server_info = mcp_server_info

    class ServerStartFailed(Message):
        """Posted by the background server-startup worker on failure."""

        def __init__(self, error: Exception) -> None:
            super().__init__()
            self.error = error

    def __init__(
        self,
        *,
        agent: Pregel | None = None,
        assistant_id: str | None = None,
        backend: CompositeBackend | None = None,
        auto_approve: bool = False,
        auto_commit: bool = False,
        cwd: str | Path | None = None,
        thread_id: str | None = None,
        initial_prompt: str | None = None,
        mcp_server_info: list[MCPServerInfo] | None = None,
        profile_override: dict[str, Any] | None = None,
        server_proc: ServerProcess | None = None,
        server_kwargs: dict[str, Any] | None = None,
        mcp_preload_kwargs: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialize the Bog Agents application.

        Args:
            agent: Pre-configured LangGraph agent, or `None` when server
                startup is deferred via `server_kwargs`.
            assistant_id: Agent identifier for memory storage
            backend: Backend for file operations
            auto_approve: Whether to start with auto-approve enabled
            auto_commit: Whether to auto-commit after each agent turn
            cwd: Current working directory to display
            thread_id: Optional thread ID for session persistence
            initial_prompt: Optional prompt to auto-submit when session starts
            mcp_server_info: MCP server metadata for the `/mcp` viewer.
            profile_override: Extra profile fields from `--profile-override`,
                retained so later profile-aware behavior stays consistent with
                the CLI override, including model selection details,
                compaction budget display, and on-demand `create_model()`
                calls such as `/compact`.
            server_proc: LangGraph server process for the interactive session.
            server_kwargs: When provided, server startup is deferred.

                The app shows a "Connecting..." state and starts the server in
                the background using these kwargs
                for `start_server_and_get_agent`.
            mcp_preload_kwargs: Kwargs for `_preload_session_mcp_server_info`,
                run concurrently with server startup when `server_kwargs` is set.
            **kwargs: Additional arguments passed to parent
        """
        super().__init__(**kwargs)
        self._agent = agent
        self._assistant_id = assistant_id or "agent"
        self._backend = backend
        self._auto_approve = auto_approve
        self._auto_commit = auto_commit
        self._cwd = str(cwd) if cwd else str(Path.cwd())
        # Avoid collision with App._thread_id
        self._lc_thread_id = thread_id
        self._initial_prompt = initial_prompt
        self._mcp_server_info = mcp_server_info
        self._profile_override = profile_override
        self._server_proc = server_proc
        self._server_kwargs = server_kwargs
        self._mcp_preload_kwargs = mcp_preload_kwargs
        self._connecting = server_kwargs is not None
        self._model_override: str | None = None
        self._model_params_override: dict[str, Any] | None = None
        self._mcp_tool_count = sum(len(s.tools) for s in (mcp_server_info or []))
        self._status_bar: StatusBar | None = None
        self._chat_input: ChatInput | None = None
        self._session_name: str | None = None
        self._active_team_name: str | None = None
        self._active_profile_name: str | None = None
        self._active_profile_prompt: str | None = None
        self._plan_mode_enabled = False
        self._effort_level = "high"
        self._base_auto_approve = auto_approve
        self._base_model_spec = (
            f"{settings.model_provider}:{settings.model_name}"
            if settings.model_provider and settings.model_name
            else settings.model_name
        )
        self._quit_pending = False
        self._session_state: TextualSessionState | None = None
        self._ui_adapter: TextualUIAdapter | None = None
        self._pending_approval_widget: ApprovalMenu | None = None
        self._pending_ask_user_widget: AskUserMenu | None = None
        # Agent task tracking for interruption
        self._agent_worker: Worker[None] | None = None
        self._agent_running = False
        # Shell command process tracking for interruption (! commands)
        self._shell_process: asyncio.subprocess.Process | None = None
        self._shell_worker: Worker[None] | None = None
        self._shell_running = False
        self._loading_widget: LoadingWidget | None = None
        self._token_tracker: TextualTokenTracker | None = None
        # Cumulative usage stats across all turns in this session
        self._session_stats: SessionStats = SessionStats()
        # User message queue for sequential processing
        self._pending_messages: deque[QueuedMessage] = deque()
        self._queued_widgets: deque[QueuedUserMessage] = deque()
        self._processing_pending = False
        self._thread_switching = False
        self._mouse_down_position: tuple[int, int] | None = None
        self._mouse_drag_distance = 0
        self._model_switching = False
        # Message virtualization store
        self._message_store = MessageStore()
        self._preview_servers: dict[str, PreviewServerRecord] = {}
        self._recording_state: RecordingSessionState | None = None
        self._remote_tasks: dict[str, Any] = {}
        # Lazily imported here to avoid pulling image dependencies into
        # argument parsing paths.
        from bog_agents_cli.input import MediaTracker

        self._image_tracker = MediaTracker()

    def _remote_agent(self) -> RemoteAgent | None:
        """Return the agent narrowed to `RemoteAgent`, or `None`.

        Returns `None` when:

        - No agent is configured (`self._agent is None`).
        - The agent is a local `Pregel` graph (e.g. ACP mode, test harnesses).

        Used to gate features that require a server-backed agent (e.g. model
        switching via `ConfigurableModelMiddleware`, checkpointer fallback).
        Checks the agent type rather than server ownership so this works for
        both CLI-spawned servers and externally managed ones.

        Returns:
            The `RemoteAgent` instance, or `None` for local agents.
        """
        from bog_agents_cli.remote_client import RemoteAgent

        return self._agent if isinstance(self._agent, RemoteAgent) else None

    def compose(self) -> ComposeResult:
        """Compose the application layout.

        Yields:
            UI components for the main chat area and status bar.
        """
        # Main chat area with scrollable messages
        # VerticalScroll tracks user scroll intent for better auto-scroll behavior
        with VerticalScroll(id="chat"):
            yield WelcomeBanner(
                thread_id=self._lc_thread_id,
                mcp_tool_count=self._mcp_tool_count,
                connecting=self._connecting,
                id="welcome-banner",
            )
            yield Container(id="messages")
        with Container(id="bottom-app-container"):
            yield ChatInput(
                cwd=self._cwd,
                image_tracker=self._image_tracker,
                id="input-area",
            )

        # Status bar at bottom
        yield StatusBar(cwd=self._cwd, id="status-bar")

    async def on_mount(self) -> None:
        """Initialize components after mount."""
        self._status_bar = self.query_one("#status-bar", StatusBar)
        self._chat_input = self.query_one("#input-area", ChatInput)

        # Set initial auto-approve state
        if self._auto_approve:
            self._status_bar.set_auto_approve(enabled=True)

        # Set git branch in status bar
        self._status_bar.branch = _get_git_branch() or ""

        # Create session state
        self._session_state = TextualSessionState(
            auto_approve=self._auto_approve,
            thread_id=self._lc_thread_id,
        )

        # Create token tracker that updates status bar
        self._token_tracker = TextualTokenTracker(
            self._update_tokens, self._hide_tokens
        )

        # Create UI adapter if agent is provided (deferred when connecting)
        if self._agent:
            self._init_agent_adapter()

        # Deferred server startup: run in background while TUI is visible
        if self._server_kwargs is not None:
            self.run_worker(
                self._start_server_background,
                exclusive=True,
                group="server-startup",
            )

        # Background update check (opt-out via BOG_AGENTS_NO_UPDATE_CHECK)
        if not os.environ.get("BOG_AGENTS_NO_UPDATE_CHECK"):
            self.run_worker(
                self._check_for_updates,
                exclusive=True,
                group="startup-update-check",
            )

        # Focus the input (autocomplete is now built into ChatInput)
        self._chat_input.focus_input()

        # Fire session.start hook (non-blocking fire-and-forget)
        from bog_agents_cli.hooks import dispatch_hook_fire_and_forget

        dispatch_hook_fire_and_forget(
            "session.start",
            {
                "thread_id": self._lc_thread_id or "",
                "cwd": self._cwd,
                "auto_approve": self._auto_approve,
                "auto_commit": self._auto_commit,
            },
        )

        # Seed default prompts and pipelines to ~/.bog-agents/ (additive, non-fatal)
        self.run_worker(
            self._seed_defaults,
            exclusive=True,
            group="startup-seed-defaults",
        )

        # Start pipeline scheduler (daemon thread — errors are non-fatal)
        self._init_pipeline_scheduler()

        # Warn about missing optional tools (advisory only — never block startup)
        try:
            from bog_agents_cli.main import (
                check_optional_tools,
                format_tool_warning_tui,
            )
        except ImportError:
            logger.warning(
                "Could not import optional tools checker; skipping tool warnings",
                exc_info=True,
            )
        else:
            try:
                for tool in check_optional_tools():
                    self.notify(
                        format_tool_warning_tui(tool),
                        severity="warning",
                        timeout=15,
                    )
            except Exception:
                logger.debug("Failed to check for optional tools", exc_info=True)

        # Auto-submit initial prompt if provided via -m flag.
        # This check must come first because _lc_thread_id and _agent are
        # always set (even for brand-new sessions), so an elif after the
        # thread-history branch would never execute.
        # When connecting, defer until on_bog_agents_app_server_ready fires.
        if not self._connecting:
            if self._initial_prompt and self._initial_prompt.strip():
                prompt = self._initial_prompt
                self.call_after_refresh(
                    lambda: asyncio.create_task(self._handle_user_message(prompt))
                )
            elif self._lc_thread_id and self._agent:
                self.call_after_refresh(
                    lambda: asyncio.create_task(self._load_thread_history())
                )

    def _init_agent_adapter(self) -> None:
        """Create the UI adapter and kick off background cache prewarming."""
        self._ui_adapter = TextualUIAdapter(
            mount_message=self._mount_message,
            update_status=self._update_status,
            request_approval=self._request_approval,
            on_auto_approve_enabled=self._on_auto_approve_enabled,
            scroll_to_bottom=self._scroll_chat_to_bottom,
            set_spinner=self._set_spinner,
            set_active_message=self._set_active_message,
            sync_message_content=self._sync_message_content,
            request_ask_user=self._request_ask_user,
        )
        if self._token_tracker:
            self._ui_adapter.set_token_tracker(self._token_tracker)

        self.run_worker(
            self._prewarm_threads_cache,
            exclusive=True,
            group="startup-thread-prewarm",
        )
        self.run_worker(
            self._prewarm_model_caches,
            exclusive=True,
            group="startup-model-prewarm",
        )

    async def _start_server_background(self) -> None:
        """Background worker: start server + MCP preload concurrently."""
        from bog_agents_cli.server_manager import start_server_and_get_agent

        coros: list[Any] = [start_server_and_get_agent(**self._server_kwargs)]  # type: ignore[arg-type]

        if self._mcp_preload_kwargs is not None:
            from bog_agents_cli.main import _preload_session_mcp_server_info

            coros.append(_preload_session_mcp_server_info(**self._mcp_preload_kwargs))

        try:
            results = await asyncio.gather(*coros, return_exceptions=True)
        except Exception as exc:  # defensive catch around gather
            self.post_message(self.ServerStartFailed(error=exc))
            return

        server_result = results[0]
        if isinstance(server_result, BaseException):
            self.post_message(
                self.ServerStartFailed(
                    error=server_result
                    if isinstance(server_result, Exception)
                    else RuntimeError(str(server_result)),
                )
            )
            return

        agent, server_proc, _ = server_result

        # Assign immediately so the finally block in run_textual_app can
        # clean up the server even if the ServerReady message is never
        # processed (e.g. user quits during startup).
        self._server_proc = server_proc

        mcp_info = None
        if len(results) > 1 and not isinstance(results[1], BaseException):
            mcp_info = results[1]
        elif len(results) > 1 and isinstance(results[1], BaseException):
            logger.warning(
                "MCP metadata preload failed: %s",
                results[1],
                exc_info=results[1],
            )

        self.post_message(
            self.ServerReady(
                agent=agent,
                server_proc=server_proc,
                mcp_server_info=mcp_info,
            )
        )

    def on_bog_agents_app_server_ready(self, event: ServerReady) -> None:
        """Handle successful background server startup."""
        self._connecting = False
        self._agent = event.agent
        self._server_proc = event.server_proc
        self._mcp_server_info = event.mcp_server_info
        self._mcp_tool_count = sum(len(s.tools) for s in (event.mcp_server_info or []))

        # Update welcome banner to show ready state
        try:
            banner = self.query_one("#welcome-banner", WelcomeBanner)
            banner.set_connected(self._mcp_tool_count)
        except NoMatches:
            logger.warning("Welcome banner not found during server ready transition")

        # Now that the agent is available, set up the adapter
        self._init_agent_adapter()

        # Handle deferred initial prompt or thread history
        if self._initial_prompt and self._initial_prompt.strip():
            prompt = self._initial_prompt
            self.call_after_refresh(
                lambda: asyncio.create_task(self._handle_user_message(prompt))
            )
        elif self._lc_thread_id and self._agent:
            self.call_after_refresh(
                lambda: asyncio.create_task(self._load_thread_history())
            )

        # Drain any messages the user typed while the server was starting.
        # (If an initial prompt exists, its cleanup path will drain the queue.)
        if self._pending_messages and not (
            self._initial_prompt and self._initial_prompt.strip()
        ):
            self.call_after_refresh(
                lambda: asyncio.create_task(self._process_next_from_queue())
            )

    def on_bog_agents_app_server_start_failed(self, event: ServerStartFailed) -> None:
        """Handle background server startup failure."""
        self._connecting = False
        logger.error("Server startup failed: %s", event.error, exc_info=event.error)
        self.notify(
            f"Failed to start server: {event.error}",
            severity="error",
            timeout=30,
        )
        # Update banner to show persistent failure state
        try:
            banner = self.query_one("#welcome-banner", WelcomeBanner)
            banner.set_failed(str(event.error))
        except NoMatches:
            logger.warning("Welcome banner not found during server failure transition")

        # Discard any messages queued while the server was starting
        if self._pending_messages:
            self._pending_messages.clear()
            for w in self._queued_widgets:
                w.remove()
            self._queued_widgets.clear()

    async def _prewarm_threads_cache(self) -> None:  # noqa: PLR6301  # Worker hook kept as instance method
        """Prewarm thread selector cache without blocking app startup."""
        from bog_agents_cli.sessions import (
            get_thread_limit,
            prewarm_thread_message_counts,
        )

        await prewarm_thread_message_counts(limit=get_thread_limit())

    async def _prewarm_model_caches(self) -> None:
        """Prewarm model discovery and profile caches without blocking startup."""
        try:
            from bog_agents_cli.model_config import (
                get_available_models,
                get_model_profiles,
            )

            await asyncio.to_thread(get_available_models)
            await asyncio.to_thread(
                get_model_profiles, cli_override=self._profile_override
            )
        except Exception:
            logger.debug("Could not prewarm model caches", exc_info=True)

    async def _check_for_updates(self) -> None:
        """Check PyPI for a newer bog-agents-cli version and notify the user."""
        try:
            from bog_agents_cli.update_check import is_update_available

            available, latest = await asyncio.to_thread(is_update_available)
            if available:
                from bog_agents_cli._version import __version__ as cli_version

                self.notify(
                    f"Update available: v{latest} (current: v{cli_version}). "
                    "Run: uv tool upgrade bog-agents-cli",
                    severity="information",
                    timeout=15,
                )
        except Exception:
            logger.debug("Background update check failed", exc_info=True)

    def on_scroll_up(self, _event: ScrollUp) -> None:
        """Handle scroll up to check if we need to hydrate older messages."""
        self._check_hydration_needed()

    def _update_status(self, message: str) -> None:
        """Update the status bar with a message."""
        if self._status_bar:
            self._status_bar.set_status_message(message)

    def _update_tokens(self, count: int) -> None:
        """Update the token count in status bar."""
        if self._status_bar:
            self._status_bar.set_tokens(count)

    def _hide_tokens(self) -> None:
        """Hide the token display during streaming."""
        if self._status_bar:
            self._status_bar.hide_tokens()

    def _scroll_chat_to_bottom(self) -> None:
        """Scroll chat to bottom using sticky scroll pattern.

        Only scrolls if user is already at/near the bottom so we don't
        interrupt reading when the user has deliberately scrolled up.

        The check is deferred via `call_after_refresh` when Textual hasn't
        finished the layout pass yet (`max_scroll_y == 0` before layout),
        which was the root cause of intermittent non-scrolling.
        """
        try:
            chat = self.query_one("#chat", VerticalScroll)
        except Exception:
            return

        total = chat.max_scroll_y
        if total <= 0:
            # Layout pass hasn't run yet — defer one frame and retry once.
            # This handles the common case where content was just mounted and
            # the scroll container hasn't measured its new height.
            self.call_after_refresh(self._scroll_chat_to_bottom_immediate)
            return

        # Sticky scroll: scroll only when user is within 15% of the bottom
        # (or 200px, whichever is larger) — tolerant enough to handle partial
        # tool-output expansion without snapping away mid-read.
        threshold = max(200, int(total * 0.15))
        if (total - chat.scroll_y) < threshold:
            chat.scroll_end(animate=False)

    def _scroll_chat_to_bottom_immediate(self) -> None:
        """Deferred scroll — called after the layout pass completes.

        Scrolls unconditionally (no sticky check) because this is only invoked
        when we already determined the user should scroll but the layout wasn't
        ready during `_scroll_chat_to_bottom`.
        """
        try:
            chat = self.query_one("#chat", VerticalScroll)
            chat.scroll_end(animate=False)
        except Exception:  # noqa: S110
            pass

    def _check_hydration_needed(self) -> None:
        """Check if we need to hydrate messages from the store.

        Called when user scrolls up near the top of visible messages.
        """
        if not self._message_store.has_messages_above:
            return

        try:
            chat = self.query_one("#chat", VerticalScroll)
        except NoMatches:
            logger.debug("Skipping hydration check: #chat container not found")
            return

        scroll_y = chat.scroll_y
        viewport_height = chat.size.height

        if self._message_store.should_hydrate_above(scroll_y, viewport_height):
            self.call_later(self._hydrate_messages_above)

    async def _hydrate_messages_above(self) -> None:
        """Hydrate older messages when user scrolls near the top.

        This recreates widgets for archived messages and inserts them
        at the top of the messages container.
        """
        if not self._message_store.has_messages_above:
            return

        try:
            chat = self.query_one("#chat", VerticalScroll)
        except NoMatches:
            logger.debug("Skipping hydration: #chat not found")
            return

        try:
            messages_container = self.query_one("#messages", Container)
        except NoMatches:
            logger.debug("Skipping hydration: #messages not found")
            return

        to_hydrate = self._message_store.get_messages_to_hydrate()
        if not to_hydrate:
            return

        old_scroll_y = chat.scroll_y
        first_child = (
            messages_container.children[0] if messages_container.children else None
        )

        # Build widgets in chronological order, then mount in reverse so
        # each is inserted before the previous first_child, resulting in
        # correct chronological order in the DOM.
        hydrated_count = 0
        hydrated_widgets: list[tuple] = []  # (widget, msg_data)
        for msg_data in to_hydrate:
            try:
                widget = msg_data.to_widget()
                hydrated_widgets.append((widget, msg_data))
            except Exception:
                logger.warning(
                    "Failed to create widget for message %s",
                    msg_data.id,
                    exc_info=True,
                )

        for widget, msg_data in reversed(hydrated_widgets):
            try:
                if first_child:
                    await messages_container.mount(widget, before=first_child)
                else:
                    await messages_container.mount(widget)
                first_child = widget
                hydrated_count += 1
                # Render Markdown content for hydrated assistant messages
                if isinstance(widget, AssistantMessage) and msg_data.content:
                    await widget.set_content(msg_data.content)
            except Exception:
                logger.warning(
                    "Failed to mount hydrated widget %s",
                    widget.id,
                    exc_info=True,
                )

        # Only update store for the number we actually mounted
        if hydrated_count > 0:
            self._message_store.mark_hydrated(hydrated_count)

        # Adjust scroll position to maintain the user's view.
        # Widget heights aren't known until after layout, so we use a
        # heuristic. A more accurate approach would measure actual heights
        # via call_after_refresh.
        estimated_height_per_message = 5  # terminal rows, rough estimate
        added_height = hydrated_count * estimated_height_per_message
        chat.scroll_y = old_scroll_y + added_height

    async def _mount_before_queued(self, container: Container, widget: Widget) -> None:
        """Mount a widget in the messages container, before any queued widgets.

        Queued-message widgets must stay at the bottom of the container so
        they remain visually anchored below the current agent response.
        This helper inserts `widget` just before the first queued widget,
        or appends at the end when the queue is empty.

        Args:
            container: The `#messages` container to mount into.
            widget: The widget to mount.
        """
        first_queued = self._queued_widgets[0] if self._queued_widgets else None
        if first_queued is not None and first_queued.parent is container:
            try:
                await container.mount(widget, before=first_queued)
            except Exception:
                logger.warning(
                    "Stale queued-widget reference; appending at end",
                    exc_info=True,
                )
            else:
                return
        await container.mount(widget)

    def _is_spinner_at_correct_position(self, container: Container) -> bool:
        """Check whether the loading spinner is already correctly positioned.

        The spinner should be immediately before the first queued widget, or
        at the very end of the container when the queue is empty.

        Args:
            container: The `#messages` container.

        Returns:
            `True` if the spinner is already in the correct position.
        """
        children = list(container.children)
        if not children or self._loading_widget not in children:
            return False

        if self._queued_widgets:
            first_queued = self._queued_widgets[0]
            if first_queued not in children:
                return False
            return children.index(self._loading_widget) == (
                children.index(first_queued) - 1
            )

        return children[-1] == self._loading_widget

    async def _set_spinner(self, status: str | None) -> None:
        """Show, update, or hide the loading spinner.

        Args:
            status: The status text to display (e.g., "Thinking", "Summarizing"),
                or `None` to hide the spinner.
        """
        if status is None:
            # Hide
            if self._loading_widget:
                await self._loading_widget.remove()
                self._loading_widget = None
            return

        messages = self.query_one("#messages", Container)

        if self._loading_widget is None:
            # Create new
            self._loading_widget = LoadingWidget(status)
            await self._mount_before_queued(messages, self._loading_widget)
        else:
            # Update existing
            self._loading_widget.set_status(status)
            # Reposition if not already at the correct location
            if not self._is_spinner_at_correct_position(messages):
                await self._loading_widget.remove()
                await self._mount_before_queued(messages, self._loading_widget)
        # NOTE: Don't call _scroll_chat_to_bottom() here - it would re-anchor
        # and drag user back to bottom if they've scrolled away during streaming

    async def _request_approval(
        self,
        action_requests: Any,  # noqa: ANN401  # ActionRequest uses dynamic typing
        assistant_id: str | None,
    ) -> asyncio.Future:
        """Request user approval inline in the messages area.

        Mounts ApprovalMenu in the messages area (inline with chat).
        ChatInput stays visible - user can still see it.

        If another approval is already pending, queue this one.

        Auto-approves shell commands that are in the configured allow-list.

        Args:
            action_requests: List of action request dicts to approve
            assistant_id: The assistant ID for display purposes

        Returns:
            A Future that resolves to the user's decision.
        """
        loop = asyncio.get_running_loop()
        result_future: asyncio.Future = loop.create_future()

        # Check if ALL actions in the batch are auto-approvable shell commands
        if settings.shell_allow_list and action_requests:
            all_auto_approved = True
            approved_commands = []

            for req in action_requests:
                if req.get("name") in SHELL_TOOL_NAMES:
                    command = req.get("args", {}).get("command", "")
                    if is_shell_command_allowed(command, settings.shell_allow_list):
                        approved_commands.append(command)
                    else:
                        all_auto_approved = False
                        break
                else:
                    # Non-shell commands need normal approval
                    all_auto_approved = False
                    break

            if all_auto_approved and approved_commands:
                # Auto-approve all commands in the batch
                result_future.set_result({"type": "approve"})

                # Mount system messages showing the auto-approvals
                try:
                    messages = self.query_one("#messages", Container)
                    for command in approved_commands:
                        auto_msg = AppMessage(
                            f"✓ Auto-approved shell command (allow-list): {command}"
                        )
                        await self._mount_before_queued(messages, auto_msg)
                    self._scroll_chat_to_bottom()
                except Exception:  # noqa: S110  # Resilient auto-message display
                    pass  # Don't fail if we can't show the message

                return result_future

        # If there's already a pending approval, wait for it to complete first
        if self._pending_approval_widget is not None:
            while self._pending_approval_widget is not None:  # noqa: ASYNC110  # Simple polling is sufficient here
                await asyncio.sleep(0.1)

        # Create menu with unique ID to avoid conflicts
        unique_id = f"approval-menu-{uuid.uuid4().hex[:8]}"
        menu = ApprovalMenu(action_requests, assistant_id, id=unique_id)
        menu.set_future(result_future)

        # Store reference
        self._pending_approval_widget = menu

        # Mount approval inline in messages area (not replacing ChatInput)
        try:
            messages = self.query_one("#messages", Container)
            await self._mount_before_queued(messages, menu)
            # Scroll to make approval visible (but don't re-anchor)
            self.call_after_refresh(menu.scroll_visible)
            # Focus approval menu
            self.call_after_refresh(menu.focus)
        except Exception as e:
            logger.exception(
                "Failed to mount approval menu (id=%s) in messages container",
                unique_id,
            )
            self._pending_approval_widget = None
            if not result_future.done():
                result_future.set_exception(e)

        return result_future

    def _on_auto_approve_enabled(self) -> None:
        """Handle auto-approve being enabled via the HITL approval menu.

        Called when the user selects "Auto-approve all" from an approval
        dialog. Syncs the auto-approve state across the app flag, status
        bar indicator, and session state so subsequent tool calls skip
        the approval prompt.
        """
        self._auto_approve = True
        if self._status_bar:
            self._status_bar.set_auto_approve(enabled=True)
        if self._session_state:
            self._session_state.auto_approve = True

    async def _remove_ask_user_widget(  # noqa: PLR6301  # Shared helper used by ask_user event handlers
        self,
        widget: AskUserMenu,
        *,
        context: str,
    ) -> None:
        """Remove an ask_user widget without surfacing cleanup races.

        Args:
            widget: Ask-user widget instance to remove.
            context: Short context string for diagnostics.
        """
        try:
            await widget.remove()
        except Exception:
            logger.debug(
                "Failed to remove ask-user widget during %s",
                context,
                exc_info=True,
            )

    async def _request_ask_user(
        self,
        questions: list[Question],
    ) -> asyncio.Future[AskUserWidgetResult]:
        """Display the ask_user widget and return a Future with user response.

        Args:
            questions: List of question dicts, each with `question`, `type`,
                and optional `choices` and `required` keys.

        Returns:
            A Future that resolves to a dict with `'type'` (`'answered'` or
                `'cancelled'`) and, when answered, an `'answers'` list.
        """
        loop = asyncio.get_running_loop()
        result_future: asyncio.Future[AskUserWidgetResult] = loop.create_future()

        if self._pending_ask_user_widget is not None:
            deadline = _monotonic() + 30
            while self._pending_ask_user_widget is not None:
                if _monotonic() > deadline:
                    logger.error(
                        "Timed out waiting for previous ask-user widget to "
                        "clear. Forcefully cleaning up."
                    )
                    old_widget = self._pending_ask_user_widget
                    if old_widget is not None:
                        old_widget.action_cancel()
                        self._pending_ask_user_widget = None
                        await self._remove_ask_user_widget(
                            old_widget,
                            context="ask-user timeout cleanup",
                        )
                    break
                await asyncio.sleep(0.1)

        unique_id = f"ask-user-menu-{uuid.uuid4().hex[:8]}"
        menu = AskUserMenu(questions, id=unique_id)
        menu.set_future(result_future)

        self._pending_ask_user_widget = menu

        try:
            messages = self.query_one("#messages", Container)
            await self._mount_before_queued(messages, menu)
            self.call_after_refresh(menu.scroll_visible)
            self.call_after_refresh(menu.focus_active)
        except Exception as e:
            logger.exception(
                "Failed to mount ask-user menu (id=%s)",
                unique_id,
            )
            self._pending_ask_user_widget = None
            if not result_future.done():
                result_future.set_exception(e)

        return result_future

    async def on_ask_user_menu_answered(
        self,
        event: Any,  # noqa: ARG002, ANN401
    ) -> None:
        """Handle ask_user menu answers - remove widget and refocus input."""
        if self._pending_ask_user_widget:
            widget = self._pending_ask_user_widget
            self._pending_ask_user_widget = None
            await self._remove_ask_user_widget(widget, context="ask-user answered")

        if self._chat_input:
            self.call_after_refresh(self._chat_input.focus_input)

    async def on_ask_user_menu_cancelled(
        self,
        event: Any,  # noqa: ARG002, ANN401
    ) -> None:
        """Handle ask_user menu cancellation - remove widget and refocus input."""
        if self._pending_ask_user_widget:
            widget = self._pending_ask_user_widget
            self._pending_ask_user_widget = None
            await self._remove_ask_user_widget(widget, context="ask-user cancelled")

        if self._chat_input:
            self.call_after_refresh(self._chat_input.focus_input)

    async def _process_message(self, value: str, mode: InputMode) -> None:
        """Route a message to the appropriate handler based on mode.

        Args:
            value: The message text to process.
            mode: The input mode that determines message routing.
        """
        if mode == "shell":
            await self._handle_shell_command(value.removeprefix("!"))
        elif mode == "command":
            await self._handle_command(value)
        elif mode == "normal":
            await self._handle_user_message(value)
        else:
            logger.warning("Unrecognized input mode %r, treating as normal", mode)
            await self._handle_user_message(value)

    async def on_chat_input_submitted(self, event: ChatInput.Submitted) -> None:
        """Handle submitted input from ChatInput widget."""
        value = event.value
        mode: InputMode = event.mode  # type: ignore[assignment]  # Textual event mode is str at type level but InputMode at runtime

        # Reset quit pending state on any input
        self._quit_pending = False

        await dispatch_hook("user.prompt", {})

        # Prevent message handling while a thread switch is in-flight.
        if self._thread_switching:
            self.notify(
                "Thread switch in progress. Please wait.",
                severity="warning",
                timeout=3,
            )
            return

        # If agent/shell is running or server is still starting up, enqueue
        # instead of processing. Messages queued during connection are drained
        # once the server is ready (see on_bog_agents_app_server_ready).
        if self._agent_running or self._shell_running or self._connecting:
            self._pending_messages.append(QueuedMessage(text=value, mode=mode))
            queued_widget = QueuedUserMessage(value)
            self._queued_widgets.append(queued_widget)
            await self._mount_message(queued_widget)
            return

        await self._process_message(value, mode)

    def on_chat_input_mode_changed(self, event: ChatInput.ModeChanged) -> None:
        """Update status bar when input mode changes."""
        if self._status_bar:
            self._status_bar.set_mode(event.mode)

    async def on_approval_menu_decided(
        self,
        event: Any,  # noqa: ARG002, ANN401  # Textual event handler signature
    ) -> None:
        """Handle approval menu decision - remove from messages and refocus input."""
        # Remove ApprovalMenu using stored reference
        if self._pending_approval_widget:
            await self._pending_approval_widget.remove()
            self._pending_approval_widget = None

        # Refocus the chat input
        if self._chat_input:
            self.call_after_refresh(self._chat_input.focus_input)

    async def _handle_shell_command(self, command: str) -> None:
        """Handle a shell command (! prefix).

        Thin dispatcher that mounts the user message and spawns a worker
        so the event loop stays free for key events (Esc/Ctrl+C).

        Args:
            command: The shell command to execute.
        """
        await self._mount_message(UserMessage(f"!{command}"))
        self._shell_running = True

        if self._chat_input:
            self._chat_input.set_cursor_active(active=False)

        self._shell_worker = self.run_worker(
            self._run_shell_task(command),
            exclusive=False,
        )

    async def _run_shell_task(self, command: str) -> None:
        """Run a shell command in a background worker.

        This mirrors `_run_agent_task`: running in a worker keeps the event
        loop free so Esc/Ctrl+C can cancel the worker -> raise
        `CancelledError` -> kill the process.

        Args:
            command: The shell command to execute.

        Raises:
            CancelledError: If the command is interrupted by the user.
        """
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self._cwd,
                start_new_session=(sys.platform != "win32"),
            )
            self._shell_process = proc

            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(), timeout=60
                )
            except TimeoutError:
                await self._kill_shell_process()
                await self._mount_message(ErrorMessage("Command timed out (60s limit)"))
                return
            except asyncio.CancelledError:
                await self._kill_shell_process()
                raise

            output = (stdout_bytes or b"").decode(errors="replace").strip()
            stderr_text = (stderr_bytes or b"").decode(errors="replace").strip()
            if stderr_text:
                output += f"\n[stderr]\n{stderr_text}"

            if output:
                msg = AssistantMessage(f"```\n{output}\n```")
                await self._mount_message(msg)
                await msg.write_initial_content()
            else:
                await self._mount_message(AppMessage("Command completed (no output)"))

            if proc.returncode and proc.returncode != 0:
                await self._mount_message(ErrorMessage(f"Exit code: {proc.returncode}"))

            # Scroll to show the output (user-initiated command, so scroll is expected)
            chat = self.query_one("#chat", VerticalScroll)
            chat.scroll_end(animate=False)

        except OSError as e:
            logger.exception("Failed to execute shell command: %s", command)
            err_msg = f"Failed to run command: {e}"
            await self._mount_message(ErrorMessage(err_msg))
        finally:
            await self._cleanup_shell_task()

    async def _cleanup_shell_task(self) -> None:
        """Clean up after shell command task completes or is cancelled."""
        was_interrupted = self._shell_process is not None and (
            self._shell_worker is not None and self._shell_worker.is_cancelled
        )
        self._shell_process = None
        self._shell_running = False
        self._shell_worker = None
        if was_interrupted:
            await self._mount_message(AppMessage("Command interrupted"))
        if self._chat_input:
            self._chat_input.set_cursor_active(active=True)
        await self._process_next_from_queue()

    async def _kill_shell_process(self) -> None:
        """Terminate the running shell command process.

        On POSIX, sends SIGTERM to the entire process group (killing children).
        On Windows, terminates only the root process. No-op if the process has
        already exited. Waits up to 5s for clean shutdown, then escalates
        to SIGKILL.
        """
        proc = self._shell_process
        if proc is None or proc.returncode is not None:
            return

        try:
            if sys.platform != "win32":
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            else:
                proc.terminate()
        except ProcessLookupError:
            return
        except OSError:
            logger.warning(
                "Failed to terminate shell process (pid=%s)", proc.pid, exc_info=True
            )
            return

        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except TimeoutError:
            logger.warning(
                "Shell process (pid=%s) did not exit after SIGTERM; sending SIGKILL",
                proc.pid,
            )
            kill_signal = getattr(signal, "SIGKILL", signal.SIGTERM)
            with suppress(ProcessLookupError, OSError):
                if sys.platform != "win32":
                    os.killpg(os.getpgid(proc.pid), kill_signal)
                else:
                    proc.kill()
            with suppress(ProcessLookupError, OSError):
                await proc.wait()
        except (ProcessLookupError, OSError):
            pass

    async def _open_url_command(self, command: str, cmd: str) -> None:
        """Open a URL in the browser and display a clickable link.

        Args:
            command: The raw command text (displayed as user message).
            cmd: The normalized slash command used to look up the URL.
        """
        url = _COMMAND_URLS[cmd]
        await self._mount_message(UserMessage(command))
        webbrowser.open(url)
        link = Text(url, style="dim italic")
        link.stylize(f"link {url}", 0)
        await self._mount_message(AppMessage(link))

    @staticmethod
    async def _build_thread_message(prefix: str, thread_id: str) -> str | Text:
        """Build a thread status message, hyperlinking the ID when possible.

        Attempts to resolve the LangSmith thread URL with a short timeout.
        Falls back to plain text if tracing is not configured or resolution
        fails.

        Args:
            prefix: Label before the thread ID (e.g. `'Resumed thread'`).
            thread_id: The thread identifier.

        Returns:
            A Rich `Text` with a clickable thread ID, or a plain string.
        """
        try:
            url = await asyncio.wait_for(
                asyncio.to_thread(build_langsmith_thread_url, thread_id),
                timeout=2.0,
            )
        except (
            TimeoutError,
            Exception,
        ):  # Resilient non-interactive mode error handling
            url = None

        if url:
            return Text.assemble(
                f"{prefix}: ",
                (thread_id, f"link {url}"),
            )
        return f"{prefix}: {thread_id}"

    async def _handle_trace_command(self, command: str) -> None:
        """Open the current thread in LangSmith.

        Shows a hint if no conversation has been started yet or if LangSmith
        tracing is not configured. Otherwise, opens the thread URL in the
        default browser and displays a clickable link.

        Args:
            command: The raw command text (displayed as user message).
        """
        await self._mount_message(UserMessage(command))
        if not self._session_state:
            await self._mount_message(AppMessage("No active session."))
            return
        thread_id = self._session_state.thread_id
        try:
            url = await asyncio.to_thread(build_langsmith_thread_url, thread_id)
        except Exception:
            logger.exception("Failed to build LangSmith thread URL for %s", thread_id)
            await self._mount_message(
                AppMessage("Failed to resolve LangSmith thread URL.")
            )
            return
        if not url:
            await self._mount_message(
                AppMessage(
                    "LangSmith tracing is not configured. "
                    "Set LANGSMITH_API_KEY and LANGSMITH_TRACING=true to enable."
                )
            )
            return
        try:
            webbrowser.open(url)
        except Exception:
            logger.debug("Could not open browser for URL: %s", url, exc_info=True)
        link = Text(url, style="dim italic")
        link.stylize(f"link {url}", 0)
        await self._mount_message(AppMessage(link))

    @staticmethod
    def _match_slash_commands(query: str, *, limit: int = 8) -> list[tuple[str, str]]:
        """Return best matching slash commands for a help/typo query.

        Args:
            query: Search text, with or without leading `/`.
            limit: Maximum number of matches to return.

        Returns:
            List of `(command, description)` tuples ordered by best match first.
        """
        from bog_agents_cli.command_registry import search_slash_commands

        return [
            (spec.name, spec.description)
            for spec in search_slash_commands(query, limit=limit)
        ]

    @classmethod
    def _build_command_reference(cls, query: str = "") -> str:
        """Build a human-friendly slash-command reference snippet.

        Args:
            query: Optional command or keyword filter.

        Returns:
            Renderable plain-text command reference.
        """
        matches = cls._match_slash_commands(query)
        if not matches:
            return (
                f"No slash commands matched '{query}'.\n"
                "Try `/commands` to browse everything."
            )

        title = (
            f"Slash commands matching '{query}':"
            if query
            else "Available slash commands:"
        )
        lines = [title]
        lines.extend(f"  {name:<14} {desc}" for name, desc in matches)
        if query:
            lines.append("")
            lines.append(
                "Tip: run the command directly or use `/commands` for a broader list."
            )
        else:
            lines.append("")
            lines.append("Tip: use `/help <command-or-keyword>` to narrow this list.")
        return "\n".join(lines)

    @staticmethod
    def _command_name(command: str) -> str:
        """Return the normalized slash command name without arguments."""
        stripped = command.strip().lower()
        if not stripped:
            return ""
        return stripped.split(maxsplit=1)[0]

    def _resolve_command_handler(
        self, command_name: str
    ) -> Callable[[str], Awaitable[None]] | None:
        """Return the bound handler for a slash command."""
        handler_name = self._COMMAND_HANDLER_NAMES.get(command_name)
        if handler_name is None:
            return None
        handler = getattr(self, handler_name, None)
        if handler is None:
            logger.warning("No handler method found for slash command %s", command_name)
            return None
        return handler

    @staticmethod
    def _refresh_slash_command_cache() -> None:
        """Refresh the shared slash-command cache used by autocomplete."""
        from bog_agents_cli.command_registry import get_slash_commands
        from bog_agents_cli.widgets import autocomplete

        autocomplete.SLASH_COMMANDS[:] = get_slash_commands()

    async def _current_thread_metadata(self) -> dict[str, object]:
        """Load persisted metadata for the active thread, if available."""
        from bog_agents_cli.sessions import get_thread_metadata

        thread_id = self._current_thread_id()
        if not thread_id:
            return {}
        metadata = await get_thread_metadata(thread_id)
        label = metadata.get("label")
        if isinstance(label, str) and label.strip():
            self._session_name = label.strip()
        return metadata

    def _build_session_summary_from_messages(self) -> str:
        """Create a compact session summary from the current message store."""
        from bog_agents_cli.widgets.message_store import MessageType

        snippets: list[str] = []
        for message in self._message_store.get_all_messages():
            if message.type not in {
                MessageType.USER,
                MessageType.ASSISTANT,
                MessageType.ERROR,
            }:
                continue
            content = " ".join(message.content.split())
            if not content:
                continue
            snippets.append(content)
            if len(snippets) >= 4:
                break
        summary = " | ".join(snippets)
        if len(summary) > 240:
            return summary[:237].rstrip() + "..."
        return summary or "Conversation in progress."

    @staticmethod
    def _expand_user_path(value: str) -> Path:
        """Resolve a user-provided filesystem path."""
        return Path(value).expanduser()

    def _build_cli_context(self) -> CLIContext:
        """Build per-turn runtime context for middleware-aware commands."""
        parts: list[str] = []
        if self._active_profile_prompt:
            parts.append(self._active_profile_prompt)

        # Inject team shared context if configured
        team_ctx = self._get_team_shared_context()
        if team_ctx:
            parts.append(team_ctx)

        return CLIContext(
            model=self._model_override,
            model_params=self._model_params_override or {},
            effort_level=self._effort_level,
            plan_mode=self._plan_mode_enabled,
            system_prompt_append="\n\n".join(parts) if parts else None,
        )

    def _get_team_shared_context(self) -> str:
        """Return team shared context text if a team config exists."""
        try:
            from bog_agents_cli.team_config import (
                get_shared_context_text,
                load_team_config,
            )

            cfg = load_team_config(Path(self._cwd))
            if cfg is None:
                return ""
            return get_shared_context_text(cfg, Path(self._cwd))
        except Exception:
            return ""

    async def _ensure_background_manager(self) -> None:
        """Create the background task manager on first use."""
        if hasattr(self, "_bg_manager"):
            return

        from bog_agents_cli.background_agents import BackgroundAgentManager

        def _create_bg_agent():
            """Create an isolated agent instance for background tasks."""
            from bog_agents_cli.config import create_model, settings

            model_spec = self._model_override or settings.model_name
            try:
                result = create_model(model_spec)
                return result.model
            except Exception:
                logger.debug("Failed to create background agent model", exc_info=True)
                return self._agent

        def _make_agent():
            from bog_agents.graph import create_agent as _create_sdk_agent

            model = _create_bg_agent()
            return _create_sdk_agent(model=model)

        self._bg_manager = BackgroundAgentManager(
            agent_factory=_make_agent,
            on_complete=lambda task: self.call_from_thread(
                self._notify_background_complete, task
            ),
        )

    async def _apply_runtime_model_override(
        self,
        model_spec: str,
        *,
        extra_kwargs: dict[str, Any] | None = None,
    ) -> str:
        """Apply a session-local model override without recreating the graph."""
        display = model_spec.removeprefix(":")
        parsed = ModelSpec.try_parse(display)
        if parsed is None:
            provider = detect_provider(display)
            if provider:
                display = f"{provider}:{display}"

        create_model(
            display,
            extra_kwargs=extra_kwargs,
            profile_overrides=self._profile_override,
        ).apply_to_settings()
        self._model_override = display
        self._model_params_override = extra_kwargs
        if self._status_bar:
            self._status_bar.set_model(
                provider=settings.model_provider or "",
                model=settings.model_name or "",
            )
        return display

    async def _run_git(
        self,
        args: list[str],
        *,
        cwd: str | Path | None = None,
    ) -> tuple[bool, str]:
        """Run a git command in the current working directory."""
        from bog_agents_cli.pr_output import _run_git

        resolved_cwd = str(cwd) if cwd is not None else str(self._cwd)
        return await asyncio.to_thread(_run_git, args, cwd=resolved_cwd)

    async def _get_repo_root(self) -> Path | None:
        """Resolve the current git repository root, if any."""
        success, output = await self._run_git(["rev-parse", "--show-toplevel"])
        if not success or not output:
            return None
        return Path(output.strip())

    def _current_thread_id(self) -> str | None:
        """Return the current interactive thread identifier, if any."""
        if self._session_state and self._session_state.thread_id:
            return self._session_state.thread_id
        return self._lc_thread_id

    def _load_team_registry(self) -> Any:  # noqa: ANN401
        """Load the workspace-local team registry."""
        from bog_agents_cli.team_orchestration import load_team_registry

        return load_team_registry(Path(self._cwd))

    def _save_team_registry(self, registry: Any) -> None:  # noqa: ANN401
        """Persist the workspace-local team registry."""
        from bog_agents_cli.team_orchestration import save_team_registry

        save_team_registry(registry, Path(self._cwd))

    def _active_team(self) -> str | None:
        """Return the active team name, loading persisted state when needed."""
        if self._active_team_name:
            return self._active_team_name
        registry = self._load_team_registry()
        if registry.active_team:
            self._active_team_name = registry.active_team
        return self._active_team_name

    @staticmethod
    def _task_team_name(task: Any) -> str | None:  # noqa: ANN401
        """Return the team name recorded on a task, if any."""
        metadata = getattr(task, "metadata", None)
        if not isinstance(metadata, dict):
            return None
        team_name = metadata.get("team_name")
        if isinstance(team_name, str) and team_name.strip():
            return team_name.strip()
        return None

    @staticmethod
    def _task_inbox_count(task: Any) -> int:  # noqa: ANN401
        """Return queued coordination messages for a task."""
        metadata = getattr(task, "metadata", None)
        if not isinstance(metadata, dict):
            return 0
        inbox = metadata.get("inbox")
        return len(inbox) if isinstance(inbox, list) else 0

    def _team_task_snapshot(self, team_name: str) -> tuple[list[Any], list[Any]]:
        """Return local and remote tasks assigned to one team."""
        local_tasks: list[Any] = []
        if hasattr(self, "_bg_manager"):
            local_tasks = [
                task
                for task in self._bg_manager.all_tasks
                if self._task_team_name(task) == team_name
            ]
        remote_tasks = [
            task
            for task in self._remote_tasks.values()
            if self._task_team_name(task) == team_name
        ]
        return local_tasks, remote_tasks

    def _build_team_effective_prompt(
        self,
        prompt: str,
        team_name: str | None,
    ) -> tuple[str, str | None]:
        """Augment a task prompt with persisted team memory when available."""
        if not team_name:
            return prompt, None
        from bog_agents_cli.team_orchestration import build_team_brief, find_team

        registry = self._load_team_registry()
        team = find_team(registry, team_name)
        if team is None:
            return prompt, None
        brief = build_team_brief(team).strip()
        if not brief:
            return prompt, None
        effective_prompt = (
            f"# Team coordination brief\n{brief}\n\n# Assigned task\n{prompt}"
        )
        return effective_prompt, brief

    def _build_team_status(self, team_name: str) -> str:
        """Build a readable status report for one team."""
        from bog_agents_cli.team_orchestration import find_team, format_team_profile

        registry = self._load_team_registry()
        team = find_team(registry, team_name)
        if team is None:
            return f"Team '{team_name}' was not found."

        local_tasks, remote_tasks = self._team_task_snapshot(team.name)
        inbox_count = sum(
            self._task_inbox_count(task) for task in [*local_tasks, *remote_tasks]
        )
        lines = [
            format_team_profile(
                team,
                active=registry.active_team.lower() == team.name.lower()
                if registry.active_team
                else False,
                local_tasks=len(local_tasks),
                remote_tasks=len(remote_tasks),
                inbox_count=inbox_count,
            )
        ]
        if local_tasks:
            lines.append("")
            lines.append("Local workers:")
            lines.extend(f"  {task.status_line}" for task in local_tasks)
        if remote_tasks:
            lines.append("")
            lines.append("Remote workers:")
            for task in remote_tasks:
                lines.append(
                    f"  [{task.task_id}] {task.status} {task.label or task.prompt[:32]}"
                )
        return "\n".join(lines)

    @staticmethod
    def _slugify_branch_fragment(value: str) -> str:
        """Convert free-form text into a git-branch-friendly fragment."""
        chars = []
        previous_dash = False
        for char in value.lower():
            if char.isalnum():
                chars.append(char)
                previous_dash = False
                continue
            if not previous_dash:
                chars.append("-")
                previous_dash = True
        slug = "".join(chars).strip("-")
        return slug or "task"

    @staticmethod
    def _build_agent_task_label(prompt: str, *, label: str = "", index: int = 1) -> str:
        """Build a stable human-readable label for managed worker tasks."""
        base = label or (prompt[:32].strip() or "task")
        if index > 1:
            return f"{base} #{index}"
        return base

    def _build_background_runner(self) -> Callable[[Any], Awaitable[Any]]:
        """Create the async runner used for managed local agent tasks."""

        async def _run(task: Any) -> Any:  # noqa: ANN401
            from bog_agents_cli.agent import create_cli_agent
            from bog_agents_cli.config import settings

            model_spec = (
                task.model
                or self._model_override
                or settings.model_name
                or self._base_model_spec
                or "openai:gpt-5.4-mini"
            )
            agent_graph, _backend = create_cli_agent(
                model=model_spec,
                assistant_id=self._assistant_id,
                auto_approve=self._auto_approve,
                enable_plan_mode=self._plan_mode_enabled,
                effort_level=self._effort_level,
                profile=self._active_profile_name or "",
                cwd=task.working_dir or self._cwd,
            )
            config: RunnableConfig = {
                "configurable": {"thread_id": f"bg-{task.task_id}"}
            }
            metadata = getattr(task, "metadata", None)
            effective_prompt = (
                metadata.get("effective_prompt", task.prompt)
                if isinstance(metadata, dict)
                else task.prompt
            )
            prompt_text = str(effective_prompt)
            input_data = {"messages": [{"role": "user", "content": prompt_text}]}
            return await agent_graph.ainvoke(
                input_data,
                config=config,
                context=cast("Any", self._build_cli_context()),
            )

        return _run

    async def _submit_managed_local_task(
        self,
        prompt: str,
        *,
        label: str = "",
        model: str | None = None,
        working_dir: str | None = None,
        strategy: str = "local",
        worktree_branch: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Submit a managed local worker task through the background manager."""
        await self._ensure_background_manager()
        return await self._bg_manager.submit(
            prompt,
            model=model,
            working_dir=working_dir,
            label=label,
            strategy=strategy,
            parent_thread_id=self._current_thread_id(),
            worktree_branch=worktree_branch,
            metadata=metadata,
            runner=self._build_background_runner(),
        )

    async def _refresh_remote_tasks(self) -> None:
        """Refresh all tracked remote tasks in-place."""
        await self._load_persisted_remote_tasks()
        if not self._remote_tasks:
            return
        from bog_agents_cli.remote import check_remote_task, load_remote_config

        config = await asyncio.to_thread(load_remote_config, settings.user_agents_dir)
        refreshed = [
            await check_remote_task(config, task)
            for task in self._remote_tasks.values()
        ]
        self._remote_tasks = {task.task_id: task for task in refreshed}
        await self._persist_remote_tasks()

    async def _load_persisted_remote_tasks(self) -> int:
        """Merge persisted remote tasks into the in-memory registry."""
        from bog_agents_cli.remote import load_remote_tasks

        loaded = await asyncio.to_thread(load_remote_tasks, settings.user_agents_dir)
        added = 0
        for task in loaded:
            if task.task_id in self._remote_tasks:
                continue
            self._remote_tasks[task.task_id] = task
            added += 1
        return added

    async def _persist_remote_tasks(self) -> None:
        """Persist tracked remote tasks for restart-safe recovery."""
        from bog_agents_cli.remote import save_remote_tasks

        try:
            await asyncio.to_thread(
                save_remote_tasks,
                settings.user_agents_dir,
                list(self._remote_tasks.values()),
            )
        except OSError:
            logger.debug("Could not persist remote task registry", exc_info=True)

    async def _store_remote_task(self, task: Any) -> None:  # noqa: ANN401
        """Track and persist a remote task."""
        self._remote_tasks[task.task_id] = task
        await self._persist_remote_tasks()

    async def _drop_remote_tasks(self, task_ids: list[str]) -> int:
        """Remove tracked remote tasks by ID and persist the change."""
        removed = 0
        for task_id in task_ids:
            if self._remote_tasks.pop(task_id, None) is not None:
                removed += 1
        if removed:
            await self._persist_remote_tasks()
        return removed

    async def _resolve_remote_task(self, task_id: str) -> Any | None:  # noqa: ANN401
        """Return a tracked remote task, loading persisted state if needed."""
        task = self._remote_tasks.get(task_id)
        if task is not None:
            return task
        await self._load_persisted_remote_tasks()
        return self._remote_tasks.get(task_id)

    @staticmethod
    def _format_rewind_checkpoint_token(index: int, checkpoint_id: str) -> str:
        """Render a concise numbered checkpoint selector token."""
        return f"{index}. {checkpoint_id[:12]}"

    @staticmethod
    def _checkpoint_match_score(checkpoint_id: str, token: str, index: int) -> int:
        """Return a simple match score for resolving checkpoint selectors."""
        normalized = token.strip().lower()
        candidate = checkpoint_id.lower()
        if not normalized:
            return 0
        if normalized.isdigit() and int(normalized) == index:
            return 100
        if normalized == candidate:
            return 95
        if candidate.startswith(normalized):
            return 80
        if normalized in candidate:
            return 40
        return 0

    async def _seed_thread_from_checkpoint(
        self,
        new_thread_id: str,
        payload: dict[str, Any],
    ) -> None:
        """Create a new thread state from a decoded checkpoint payload.

        Raises:
            RuntimeError: If no active agent is available for rewind seeding.
        """
        if self._agent is None:
            msg = "Cannot rewind without an active agent."
            raise RuntimeError(msg)
        config: RunnableConfig = {"configurable": {"thread_id": new_thread_id}}
        if remote := self._remote_agent():
            await remote.aensure_thread(config)  # ty: ignore[invalid-argument-type]
        state_update: dict[str, Any] = {"messages": list(payload.get("messages", []))}
        if "summarization_event" in payload:
            state_update["_summarization_event"] = payload["summarization_event"]
        await self._agent.aupdate_state(config, state_update)

    def _format_background_task_detail(self, task: Any) -> str:  # noqa: ANN401
        """Format one managed background task for `/agent status`."""
        lines = [
            f"Task: {task.task_id}",
            f"Status: {task.status}",
            f"Strategy: {task.strategy}",
            f"Label: {task.label or '(none)'}",
            f"Model: {task.model or '(default)'}",
            f"CWD: {task.working_dir or '(session cwd)'}",
        ]
        if team_name := self._task_team_name(task):
            lines.append(f"Team: {team_name}")
        if inbox_count := self._task_inbox_count(task):
            lines.append(f"Inbox: {inbox_count} queued message(s)")
        if task.worktree_branch:
            lines.append(f"Worktree branch: {task.worktree_branch}")
        if task.parent_thread_id:
            lines.append(f"Parent thread: {task.parent_thread_id}")
        if task.result:
            preview = (
                task.result if len(task.result) <= 400 else task.result[:400] + "..."
            )
            lines.extend(["", "Result:", preview])
        if task.error:
            lines.append(f"Error: {task.error}")
        return "\n".join(lines)

    async def _handle_reference_url_command(self, command: str) -> None:
        """Open a slash-command reference URL in the browser."""
        await self._open_url_command(command, self._command_name(command))

    async def _handle_quit_command(self, _command: str) -> None:
        """Exit the app."""
        self.exit()

    async def _handle_version_command(self, command: str) -> None:
        """Show CLI and SDK versions."""
        await self._mount_message(UserMessage(command))
        try:
            from bog_agents_cli._version import __version__ as cli_version

            cli_line = f"bog-agents-cli version: {cli_version}"
        except ImportError:
            logger.debug("bog_agents_cli._version module not found")
            cli_line = "bog-agents-cli version: unknown"
        except Exception:
            logger.warning("Unexpected error looking up CLI version", exc_info=True)
            cli_line = "bog-agents-cli version: unknown"
        try:
            from importlib.metadata import (
                PackageNotFoundError,
                version as _pkg_version,
            )

            sdk_version = _pkg_version("bog-agents")
            sdk_line = f"bog-agents (SDK) version: {sdk_version}"
        except PackageNotFoundError:
            logger.debug("bog-agents SDK package not found in environment")
            sdk_line = "bog-agents (SDK) version: unknown"
        except Exception:
            logger.warning("Unexpected error looking up SDK version", exc_info=True)
            sdk_line = "bog-agents (SDK) version: unknown"
        await self._mount_message(AppMessage(f"{cli_line}\n{sdk_line}"))

    async def _handle_clear_command(self, _command: str) -> None:
        """Clear the current chat session and start a fresh thread."""
        self._pending_messages.clear()
        self._queued_widgets.clear()
        await self._clear_messages()
        if self._token_tracker:
            self._token_tracker.reset()
        self._update_status("")
        if self._session_state:
            new_thread_id = self._session_state.reset_thread()
            try:
                banner = self.query_one("#welcome-banner", WelcomeBanner)
                banner.update_thread_id(new_thread_id)
            except NoMatches:
                pass
            await self._mount_message(
                AppMessage(f"Started new thread: {new_thread_id}")
            )

    async def _handle_compact_command(self, command: str) -> None:
        """Trigger conversation compaction."""
        await self._mount_message(UserMessage(command))
        await self._handle_compact()

    async def _handle_threads_command(self, _command: str) -> None:
        """Open the interactive thread selector."""
        await self._show_thread_selector()

    async def _dispatch_background_command(self, command: str) -> None:
        """Mount the slash command and forward to the background handler."""
        await self._mount_message(UserMessage(command))
        await self._handle_background_command(command)

    async def _dispatch_dashboard_command(self, command: str) -> None:
        """Mount the slash command and forward to the dashboard handler."""
        await self._mount_message(UserMessage(command))
        await self._handle_dashboard_command()

    async def _dispatch_recommend_command(self, command: str) -> None:
        """Mount the slash command and forward to the recommend handler."""
        await self._mount_message(UserMessage(command))
        await self._handle_recommend_command(command)

    async def _dispatch_init_command(self, _command: str) -> None:
        """Forward to the repo-init helper."""
        await self._handle_init_command()

    async def _dispatch_logs_command(self, _command: str) -> None:
        """Forward to the log viewer helper."""
        await self._handle_logs_command()

    async def _dispatch_onboard_command(self, _command: str) -> None:
        """Forward to the onboarding helper."""
        await self._handle_onboard_command()

    async def _handle_tokens_command(self, command: str) -> None:
        """Show token usage and context breakdown."""
        await self._mount_message(UserMessage(command))
        if self._token_tracker and self._token_tracker.current_context > 0:
            count = self._token_tracker.current_context
            formatted = format_token_count(count)

            model_name = settings.model_name
            context_limit = settings.model_context_limit

            if context_limit is not None:
                limit_str = format_token_count(context_limit)
                pct = count / context_limit * 100
                usage = f"{formatted} / {limit_str} tokens ({pct:.0f}%)"
            else:
                usage = f"{formatted} tokens used"

            msg = f"{usage} | {model_name}" if model_name else usage

            conv_tokens = await self._get_conversation_token_count()
            if conv_tokens is not None:
                overhead = max(0, count - conv_tokens)
                overhead_str = format_token_count(overhead)
                conv_str = format_token_count(conv_tokens)

                overhead_unit = " tokens" if overhead < 1000 else ""
                conv_unit = " tokens" if conv_tokens < 1000 else ""

                msg += (
                    f"\n|- System prompt + tools: ~{overhead_str}{overhead_unit} (fixed)"
                    f"\n`- Conversation: ~{conv_str}{conv_unit}"
                )

            await self._mount_message(AppMessage(msg))
            return

        model_name = settings.model_name
        context_limit = settings.model_context_limit

        parts: list[str] = ["No token usage yet"]
        if context_limit is not None:
            limit_str = format_token_count(context_limit)
            parts.append(f"{limit_str} token context window")
        if model_name:
            parts.append(model_name)

        await self._mount_message(AppMessage(" | ".join(parts)))

    async def _handle_remember_command(self, command: str) -> None:
        """Build and send the memory-capture prompt."""
        cmd = command.lower().strip()
        additional_context = ""
        if cmd.startswith("/remember "):
            additional_context = command.strip()[len("/remember ") :].strip()

        if additional_context:
            final_prompt = (
                f"{REMEMBER_PROMPT}\n\n"
                f"**Additional context from user:** {additional_context}"
            )
        else:
            final_prompt = REMEMBER_PROMPT

        await self._handle_user_message(final_prompt)

    async def _handle_mcp_command(self, command: str) -> None:
        """Handle /mcp — MCP server marketplace and management.

        Usage:
          /mcp                       — show active servers (viewer)
          /mcp list                  — list configured servers in ~/.bog-agents/.mcp.json
          /mcp catalog               — browse the full registry
          /mcp search <query>        — search registry by keyword
          /mcp install <id>          — install a server from the registry
          /mcp add <name> <cmd> ...  — add a custom stdio server
          /mcp remove <name>         — remove a server from user config
          /mcp info <id>             — show registry entry details
          /mcp trust                 — manage project stdio server trust
          /mcp help                  — show this help

        Args:
            command: Full slash command string.
        """
        from bog_agents_cli.mcp_config_manager import (
            add_server,
            list_servers,
            missing_required,
            remove_server,
            resolve_env_values,
            server_exists,
        )
        from bog_agents_cli.mcp_registry import (
            build_server_config,
            get_entry,
            list_categories,
            list_entries,
            search_entries,
        )

        # Parse subcommand and args
        tail = command[len("/mcp"):].strip()
        parts = tail.split(maxsplit=1)
        subcommand = parts[0].lower() if parts else ""
        rest = parts[1].strip() if len(parts) > 1 else ""

        # ---- no subcommand → open viewer ----
        if not subcommand or subcommand == "view":
            await self._show_mcp_viewer()
            return

        await self._mount_message(UserMessage(command))

        # ---- list ----
        if subcommand == "list":
            configured = list_servers()
            if not configured:
                await self._mount_message(
                    AppMessage(
                        "No MCP servers configured in [cyan]~/.bog-agents/.mcp.json[/cyan].\n\n"
                        "Browse the catalog with [bold]/mcp catalog[/bold] or "
                        "install a server with [bold]/mcp install <id>[/bold].\n"
                        "[dim]Example: /mcp install jira[/dim]"
                    )
                )
                return
            lines = [f"[bold]Configured MCP servers[/bold] ({len(configured)} total)\n"]
            for name, cfg in sorted(configured.items()):
                transport = cfg.get("type", cfg.get("transport", "stdio"))
                if transport == "stdio":
                    cmd_display = f"[dim]{cfg.get('command', '?')} {' '.join(cfg.get('args', [])[:2])}...[/dim]"
                else:
                    cmd_display = f"[dim]{cfg.get('url', '?')}[/dim]"
                lines.append(f"  [cyan]{name}[/cyan] [{transport}] {cmd_display}")
            lines.append(
                "\n[dim]Use [bold]/mcp remove <name>[/bold] to uninstall, "
                "[bold]/mcp[/bold] to open the live viewer[/dim]"
            )
            await self._mount_message(AppMessage("\n".join(lines)))

        # ---- catalog ----
        elif subcommand == "catalog":
            entries = list_entries()
            categories = list_categories()
            lines = [f"[bold]MCP Server Catalog[/bold] — {len(entries)} servers\n"]
            for cat in categories:
                cat_entries = [e for e in entries if e.category == cat]
                lines.append(f"\n[bold yellow]{cat.upper()}[/bold yellow]")
                for e in cat_entries:
                    src_tag = f"[dim][{e.source}][/dim]" if e.source != "official" else ""
                    lines.append(
                        f"  [cyan]{e.id:<22}[/cyan] {e.display_name:<20} {src_tag}\n"
                        f"    [dim]{e.description}[/dim]"
                    )
            lines.append(
                "\n[dim]Install with [bold]/mcp install <id>[/bold] · "
                "Details with [bold]/mcp info <id>[/bold] · "
                "Search with [bold]/mcp search <query>[/bold][/dim]"
            )
            await self._mount_message(AppMessage("\n".join(lines)))

        # ---- search ----
        elif subcommand == "search":
            if not rest:
                await self._mount_message(
                    AppMessage("Usage: [bold]/mcp search <query>[/bold]\nExample: /mcp search jira")
                )
                return
            results = search_entries(rest)
            if not results:
                await self._mount_message(
                    AppMessage(
                        f"No servers matching [cyan]{rest!r}[/cyan].\n"
                        "Try [bold]/mcp catalog[/bold] to browse all servers."
                    )
                )
                return
            lines = [f"[bold]Search results for[/bold] [cyan]{rest!r}[/cyan]\n"]
            for e in results:
                src_tag = f" [dim][{e.source}][/dim]" if e.source != "official" else ""
                lines.append(
                    f"  [cyan]{e.id}[/cyan] — {e.display_name}{src_tag}\n"
                    f"    [dim]{e.description}[/dim]"
                )
            lines.append(
                "\n[dim]Install with [bold]/mcp install <id>[/bold] · "
                "Details with [bold]/mcp info <id>[/bold][/dim]"
            )
            await self._mount_message(AppMessage("\n".join(lines)))

        # ---- info ----
        elif subcommand == "info":
            if not rest:
                await self._mount_message(
                    AppMessage("Usage: [bold]/mcp info <id>[/bold]\nExample: /mcp info jira")
                )
                return
            entry = get_entry(rest)
            if entry is None:
                await self._mount_message(
                    AppMessage(
                        f"Server [cyan]{rest!r}[/cyan] not found in registry.\n"
                        "Try [bold]/mcp search <query>[/bold] or [bold]/mcp catalog[/bold]."
                    )
                )
                return
            lines = [
                f"[bold]{entry.display_name}[/bold] [dim](id: {entry.id})[/dim]",
                f"  {entry.description}",
                f"\n  [bold]Source:[/bold] {entry.source}  [bold]Category:[/bold] {entry.category}  [bold]Transport:[/bold] {entry.transport}",
            ]
            if entry.transport == "stdio":
                arg_str = " ".join(entry.args)
                lines.append(f"\n  [bold]Command:[/bold] [dim]{entry.command} {arg_str}[/dim]")
            if entry.required_env:
                lines.append(
                    "\n  [bold]Required env vars:[/bold]\n"
                    + "\n".join(
                        f"    [cyan]{v}[/cyan]  [dim]{entry.vars_hints.get(v, '')}[/dim]"
                        for v in entry.required_env
                    )
                )
            if entry.optional_env:
                lines.append(
                    "\n  [bold]Optional env vars:[/bold]\n"
                    + "\n".join(
                        f"    [cyan]{v}[/cyan]  [dim]{entry.vars_hints.get(v, '')}[/dim]"
                        for v in entry.optional_env
                    )
                )
            if entry.install_notes:
                lines.append(f"\n  [bold]Setup notes:[/bold]\n  {entry.install_notes}")
            installed = server_exists(entry.id)
            lines.append(
                f"\n  [bold]Status:[/bold] {'[green]Installed[/green]' if installed else '[dim]Not installed[/dim]'}"
            )
            if not installed:
                lines.append(f"\n  Install with: [bold]/mcp install {entry.id}[/bold]")
            await self._mount_message(AppMessage("\n".join(lines)))

        # ---- install ----
        elif subcommand == "install":
            if not rest:
                await self._mount_message(
                    AppMessage(
                        "Usage: [bold]/mcp install <id>[/bold]\n"
                        "Browse available servers with [bold]/mcp catalog[/bold]."
                    )
                )
                return
            server_id = rest.strip()
            entry = get_entry(server_id)
            if entry is None:
                await self._mount_message(
                    AppMessage(
                        f"Server [cyan]{server_id!r}[/cyan] not found in registry.\n"
                        "Try [bold]/mcp search <query>[/bold] to find servers."
                    )
                )
                return
            if server_exists(server_id):
                await self._mount_message(
                    AppMessage(
                        f"[cyan]{server_id}[/cyan] is already configured.\n"
                        f"Use [bold]/mcp remove {server_id}[/bold] first to reinstall."
                    )
                )
                return

            # Resolve env vars from vars store + os.environ
            env_values = await asyncio.to_thread(
                resolve_env_values, entry.required_env, entry.optional_env
            )
            missing = missing_required(entry.required_env, env_values)

            lines = [f"[bold]Installing[/bold] [cyan]{entry.display_name}[/cyan]...\n"]

            if missing:
                lines.append(
                    "[yellow]Missing required env vars[/yellow] "
                    "(server will be added but may not start until these are set):\n"
                )
                for var in missing:
                    hint = entry.vars_hints.get(var, "")
                    lines.append(f"  [cyan]{var}[/cyan]  [dim]{hint}[/dim]")
                lines.append(
                    "\n[dim]Set them with [bold]/vars set VAR_NAME value[/bold] "
                    "and they will be picked up automatically.[/dim]"
                )

            server_cfg = build_server_config(entry, env_values)
            success = add_server(server_id, server_cfg)
            if success:
                lines.append(
                    f"\n[green]✓ Added[/green] [cyan]{server_id}[/cyan] to "
                    f"[dim]~/.bog-agents/.mcp.json[/dim]"
                )
                if entry.install_notes:
                    lines.append(f"\n[bold]Setup notes:[/bold]\n{entry.install_notes}")
                lines.append(
                    "\n[dim]Restart bog-agents (or start a new session) for the server to connect.[/dim]"
                )
            else:
                lines.append("\n[red]✗ Failed to write config[/red] — check file permissions on ~/.bog-agents/")
            await self._mount_message(AppMessage("\n".join(lines)))

        # ---- add (custom server) ----
        elif subcommand == "add":
            # /mcp add <name> <command> [arg1 arg2 ...]
            add_parts = rest.split(maxsplit=1)
            if len(add_parts) < 2:
                await self._mount_message(
                    AppMessage(
                        "Usage: [bold]/mcp add <name> <command> [args...][/bold]\n\n"
                        "Examples:\n"
                        "  [dim]/mcp add my-server npx -y my-mcp-package[/dim]\n"
                        "  [dim]/mcp add local-tools python -m my_tools.mcp_server[/dim]"
                    )
                )
                return
            name = add_parts[0]
            cmd_and_args = add_parts[1].split()
            cmd = cmd_and_args[0]
            args = cmd_and_args[1:]
            server_cfg = {"command": cmd, "args": args}
            if server_exists(name):
                await self._mount_message(
                    AppMessage(
                        f"[cyan]{name}[/cyan] already exists.\n"
                        f"Use [bold]/mcp remove {name}[/bold] first."
                    )
                )
                return
            if add_server(name, server_cfg):
                await self._mount_message(
                    AppMessage(
                        f"[green]✓ Added[/green] [cyan]{name}[/cyan]  "
                        f"[dim]{cmd} {' '.join(args)}[/dim]\n\n"
                        "[dim]Restart bog-agents for the server to connect.[/dim]"
                    )
                )
            else:
                await self._mount_message(
                    AppMessage("[red]✗ Failed to save config.[/red] Check file permissions.")
                )

        # ---- remove ----
        elif subcommand in ("remove", "rm", "uninstall", "delete"):
            if not rest:
                await self._mount_message(
                    AppMessage("Usage: [bold]/mcp remove <name>[/bold]")
                )
                return
            name = rest.strip()
            if remove_server(name):
                await self._mount_message(
                    AppMessage(
                        f"[green]✓ Removed[/green] [cyan]{name}[/cyan] from "
                        f"[dim]~/.bog-agents/.mcp.json[/dim]\n"
                        "[dim]Restart bog-agents for the change to take effect.[/dim]"
                    )
                )
            else:
                self.notify(f"Server '{name}' not found in user config.", severity="warning", timeout=3)

        # ---- trust ----
        elif subcommand == "trust":
            from bog_agents_cli.mcp_tools import discover_mcp_configs
            from bog_agents_cli.mcp_trust import (
                compute_config_fingerprint,
                trust_project_mcp,
            )

            config_paths = await asyncio.to_thread(discover_mcp_configs)
            if not config_paths:
                await self._mount_message(
                    AppMessage("No .mcp.json files found in this project.")
                )
                return
            fingerprint = await asyncio.to_thread(compute_config_fingerprint, config_paths)
            project_root = str(Path.cwd().resolve())
            ok = await asyncio.to_thread(trust_project_mcp, project_root, fingerprint)
            if ok:
                await self._mount_message(
                    AppMessage(
                        f"[green]✓ Trusted[/green] project MCP config for [cyan]{project_root}[/cyan]\n"
                        "[dim]Project stdio servers will be loaded on the next session start.[/dim]"
                    )
                )
            else:
                await self._mount_message(
                    AppMessage("[red]✗ Failed to write trust record.[/red]")
                )

        # ---- help / unknown ----
        else:
            await self._mount_message(
                AppMessage(
                    "[bold]/mcp[/bold] — MCP server marketplace\n\n"
                    "  [cyan]/mcp[/cyan]                      — open live server viewer\n"
                    "  [cyan]/mcp list[/cyan]                 — list configured servers\n"
                    "  [cyan]/mcp catalog[/cyan]              — browse full registry\n"
                    "  [cyan]/mcp search <query>[/cyan]       — search registry\n"
                    "  [cyan]/mcp info <id>[/cyan]            — show server details\n"
                    "  [cyan]/mcp install <id>[/cyan]         — install from registry\n"
                    "  [cyan]/mcp add <name> <cmd> ...[/cyan] — add custom stdio server\n"
                    "  [cyan]/mcp remove <name>[/cyan]        — remove from user config\n"
                    "  [cyan]/mcp trust[/cyan]                — trust project stdio servers\n\n"
                    "[dim]Popular: jira · github · slack · postgres · terraform · azure-devops[/dim]"
                )
            )

    async def _handle_model_command(self, command: str) -> None:
        """Switch models or manage the default model."""
        cmd = command.lower().strip()
        model_arg = None
        set_default = False
        extra_kwargs: dict[str, Any] | None = None
        if cmd.startswith("/model "):
            raw_arg = command.strip()[len("/model ") :].strip()
            try:
                raw_arg, extra_kwargs = _extract_model_params_flag(raw_arg)
            except (ValueError, TypeError) as exc:
                await self._mount_message(UserMessage(command))
                await self._mount_message(ErrorMessage(str(exc)))
                return
            if raw_arg.startswith("--default"):
                set_default = True
                model_arg = raw_arg[len("--default") :].strip() or None
            else:
                model_arg = raw_arg or None

        if set_default:
            await self._mount_message(UserMessage(command))
            if extra_kwargs:
                await self._mount_message(
                    ErrorMessage(
                        "--model-params cannot be used with --default. "
                        "Model params are applied per-session, not persisted."
                    )
                )
            elif model_arg == "--clear":
                await self._clear_default_model()
            elif model_arg:
                await self._set_default_model(model_arg)
            else:
                await self._mount_message(
                    AppMessage(
                        "Usage: /model --default provider:model\n"
                        "       /model --default --clear"
                    )
                )
            return

        if model_arg:
            await self._mount_message(UserMessage(command))
            await self._switch_model(model_arg, extra_kwargs=extra_kwargs)
            return

        await self._show_model_selector(extra_kwargs=extra_kwargs)

    async def _handle_reload_command(self, command: str) -> None:
        """Reload config from environment variables and `.env`."""
        await self._mount_message(UserMessage(command))
        try:
            changes = settings.reload_from_environment()

            from bog_agents_cli.model_config import clear_caches

            clear_caches()
        except (OSError, ValueError):
            logger.exception("Failed to reload configuration")
            await self._mount_message(
                AppMessage(
                    "Failed to reload configuration. Check your .env file and "
                    "environment variables for syntax errors, then try again."
                )
            )
            return
        if changes:
            report = "Configuration reloaded. Changes:\n" + "\n".join(
                f"  - {change}" for change in changes
            )
        else:
            report = "Configuration reloaded. No changes detected."
        report += "\nModel config caches cleared."
        await self._mount_message(AppMessage(report))

    async def _handle_repomap_command(self, command: str) -> None:
        """Show or refresh the semantic repository map."""
        from bog_agents_cli.repo_map_display import (
            get_repo_map_stats,
            get_repo_map_text,
        )

        await self._mount_message(UserMessage(command))
        raw = command.strip()[len("/repomap"):].strip().lower()

        if raw in {"help", "--help", "-h"}:
            await self._mount_message(AppMessage(
                "[bold]/repomap[/bold] — Semantic repository map\n\n"
                "Indexes the project structure (classes, functions, types) and injects\n"
                "a compact map into the agent's context.\n\n"
                "Usage:\n"
                "  [bold]/repomap[/bold]           Show the current map (build if needed)\n"
                "  [bold]/repomap refresh[/bold]   Force a full rebuild (clear cache)\n"
                "  [bold]/repomap status[/bold]    Show cache statistics\n"
                "  [bold]/repomap help[/bold]      Show this help\n\n"
                "The map is cached in [italic].bog-agents/repomap.json[/italic] and updated\n"
                "incrementally — only changed files are re-parsed."
            ))
            return

        if raw == "status":
            stats = await asyncio.to_thread(get_repo_map_stats, self._cwd)
            if not stats.get("cached"):
                await self._mount_message(AppMessage(
                    "No repo map cache found. Run [bold]/repomap[/bold] to build it."
                ))
            else:
                import time as _time
                built_ago = int(_time.time() - stats.get("built_at", 0))
                age = f"{built_ago // 60}m" if built_ago < 3600 else f"{built_ago // 3600}h"
                await self._mount_message(AppMessage(
                    f"Repo map cached: [green]{stats['file_count']}[/green] symbols indexed, "
                    f"built {age} ago\n"
                    f"Cache: {stats['cache_path']}"
                ))
            return

        force = raw == "refresh"
        if force:
            await self._mount_message(AppMessage("Rebuilding repository map from scratch..."))
        else:
            await self._mount_message(AppMessage("Building repository map..."))

        map_text = await asyncio.to_thread(get_repo_map_text, self._cwd, refresh=force)

        # Show a preview (first 60 lines) to avoid flooding the TUI
        lines = map_text.splitlines()
        preview_count = 60
        if len(lines) > preview_count:
            preview = "\n".join(lines[:preview_count])
            preview += f"\n\n... ({len(lines) - preview_count} more lines — full map injected into agent context)"
        else:
            preview = map_text

        await self._mount_message(AppMessage(f"[bold]Repository Map[/bold]\n\n{preview}"))

    async def _handle_doctor_command(self, command: str) -> None:
        """Run local health diagnostics from inside the TUI."""
        from bog_agents_cli.doctor import run_doctor

        await self._mount_message(UserMessage(command))
        report = await asyncio.to_thread(run_doctor)
        await self._mount_message(AppMessage(report))

    async def _handle_review_command(self, command: str) -> None:
        """Generate a structured code-review prompt and send it to the agent."""
        from bog_agents_cli.review_command import (
            generate_review_prompt,
            parse_review_args,
        )

        await self._mount_message(UserMessage(command))
        raw_arg = command.strip()[len("/review") :].strip()
        target = parse_review_args(raw_arg)
        prompt = generate_review_prompt(target)
        await self._mount_message(AppMessage("Starting structured code review..."))
        await self._send_prompt_to_agent(prompt)

    async def _run_prompt_backed_command(
        self,
        command: str,
        *,
        prompt_key: str,
        default_prompt: str,
        announcement: str,
    ) -> None:
        """Run a slash command that translates into an agent prompt."""
        from bog_agents_cli.prompts import get_prompt

        await self._mount_message(UserMessage(command))
        prompt = get_prompt(prompt_key, default_prompt)
        await self._mount_message(AppMessage(announcement))
        await self._send_prompt_to_agent(prompt)

    async def _handle_audit_command(self, command: str) -> None:
        """Handle `/audit` as a dependency and project risk audit."""
        from bog_agents_cli.test_tools_cli import generate_audit_prompt

        await self._run_prompt_backed_command(
            command,
            prompt_key="audit",
            default_prompt=generate_audit_prompt(),
            announcement="Auditing dependencies and project risk posture...",
        )

    async def _handle_harbor_command(self, command: str) -> None:
        """Handle /harbor — Harbor benchmark evaluation interface.

        Usage:
          /harbor                  — show Harbor status and recent results
          /harbor results [dir]    — list recent trajectory files
          /harbor show [dir]       — detailed summary of the latest trajectory
          /harbor tools [dir]      — show tool usage breakdown
          /harbor help             — show this help

        Args:
            command: Full slash command string.
        """
        await self._mount_message(UserMessage(command))

        tail = command[len("/harbor"):].strip()
        parts = tail.split(maxsplit=1)
        subcommand = parts[0].lower() if parts else ""
        rest = parts[1].strip() if len(parts) > 1 else ""

        # Default search dir
        default_dir = Path.home() / ".bog-agents" / "harbor-results"

        # ---- status / no subcommand ----
        if not subcommand or subcommand == "status":
            lines = ["[bold]Harbor Evaluation[/bold] — Terminal Bench 2.0\n"]

            # Check harbor package availability
            try:
                import importlib.util
                harbor_available = importlib.util.find_spec("harbor") is not None
                bog_harbor_available = importlib.util.find_spec("bog_agents_harbor") is not None
            except Exception:
                harbor_available = False
                bog_harbor_available = False

            lines.append(
                f"  harbor package:         {'[green]installed[/green]' if harbor_available else '[dim]not installed[/dim]'}"
            )
            lines.append(
                f"  bog-agents-harbor:      {'[green]installed[/green]' if bog_harbor_available else '[dim]not installed[/dim]'}"
            )

            import os
            langsmith_set = bool(os.environ.get("LANGSMITH_API_KEY"))
            lines.append(
                f"  LangSmith tracing:      {'[green]configured[/green]' if langsmith_set else '[dim]not configured[/dim]'}"
            )

            # Show recent trajectory count
            try:
                from bog_agents_harbor.reporter import find_trajectories
                trajectories = find_trajectories(default_dir)
                if trajectories:
                    lines.append(f"\n  Recent results ({len(trajectories)} found in {default_dir}):")
                    for t in trajectories[:5]:
                        import time
                        age = time.time() - t.stat().st_mtime
                        age_str = f"{int(age // 3600)}h ago" if age > 3600 else f"{int(age // 60)}m ago"
                        lines.append(f"    [dim]{t.parent.name}[/dim]  [{age_str}]")
                    if len(trajectories) > 5:
                        lines.append(f"    [dim]... and {len(trajectories) - 5} more[/dim]")
                else:
                    lines.append(f"\n  [dim]No results found in {default_dir}[/dim]")
            except ImportError:
                lines.append("\n  [dim]Install bog-agents-harbor to view results[/dim]")
            except OSError:
                pass

            lines.append(
                "\n[dim]Commands: [bold]/harbor results[/bold] · [bold]/harbor show[/bold] · [bold]/harbor tools[/bold][/dim]"
            )
            await self._mount_message(AppMessage("\n".join(lines)))

        # ---- results ----
        elif subcommand == "results":
            search_dir = Path(rest) if rest else default_dir
            try:
                from bog_agents_harbor.reporter import (
                    find_trajectories,
                    load_trajectory,
                )
            except ImportError:
                await self._mount_message(
                    AppMessage(
                        "[yellow]bog-agents-harbor is not installed.[/yellow]\n"
                        "Install it with: [bold]pip install bog-agents-harbor[/bold]"
                    )
                )
                return

            trajectories = await asyncio.to_thread(find_trajectories, search_dir)
            if not trajectories:
                await self._mount_message(
                    AppMessage(f"No trajectory files found under [cyan]{search_dir}[/cyan].")
                )
                return

            lines = [f"[bold]Recent Harbor results[/bold] ({len(trajectories)} found)\n"]
            for path in trajectories[:20]:
                try:
                    report = await asyncio.to_thread(load_trajectory, path)
                    reward_str = (
                        f"  reward={report.reward:.2f}" if report.reward is not None else ""
                    )
                    token_str = (
                        f"  tokens={report.total_tokens:,}" if report.total_tokens else ""
                    )
                    lines.append(
                        f"  [cyan]{path.parent.name}[/cyan]\n"
                        f"    model={report.model_name}  steps={report.total_steps}"
                        f"  tools={report.tool_call_count}{token_str}{reward_str}"
                    )
                except (ValueError, OSError):
                    lines.append(f"  [dim]{path}  (unreadable)[/dim]")
            await self._mount_message(AppMessage("\n".join(lines)))

        # ---- show ----
        elif subcommand == "show":
            search_dir = Path(rest) if rest else default_dir
            try:
                from bog_agents_harbor.reporter import (
                    find_trajectories,
                    format_summary,
                    load_trajectory,
                )
            except ImportError:
                await self._mount_message(
                    AppMessage("[yellow]bog-agents-harbor is not installed.[/yellow]")
                )
                return

            trajectories = await asyncio.to_thread(find_trajectories, search_dir, limit=1)
            if not trajectories:
                await self._mount_message(
                    AppMessage(f"No trajectory files found under [cyan]{search_dir}[/cyan].")
                )
                return

            report = await asyncio.to_thread(load_trajectory, trajectories[0])
            summary = await asyncio.to_thread(format_summary, report, verbose=True)
            await self._mount_message(AppMessage(f"[dim]{summary}[/dim]"))

        # ---- tools ----
        elif subcommand == "tools":
            search_dir = Path(rest) if rest else default_dir
            try:
                from bog_agents_harbor.reporter import (
                    find_trajectories,
                    format_tool_usage,
                    load_trajectory,
                )
            except ImportError:
                await self._mount_message(
                    AppMessage("[yellow]bog-agents-harbor is not installed.[/yellow]")
                )
                return

            trajectories = await asyncio.to_thread(find_trajectories, search_dir, limit=1)
            if not trajectories:
                await self._mount_message(
                    AppMessage(f"No trajectory files found under [cyan]{search_dir}[/cyan].")
                )
                return

            report = await asyncio.to_thread(load_trajectory, trajectories[0])
            tool_str = await asyncio.to_thread(format_tool_usage, report)
            await self._mount_message(
                AppMessage(
                    f"[bold]Tool usage[/bold] — {report.session_id}\n\n{tool_str}"
                )
            )

        # ---- help / unknown ----
        else:
            await self._mount_message(
                AppMessage(
                    "[bold]/harbor[/bold] — Harbor benchmark evaluation\n\n"
                    "  [cyan]/harbor[/cyan]                  — status + recent results\n"
                    "  [cyan]/harbor results [dir][/cyan]   — list recent trajectory files\n"
                    "  [cyan]/harbor show [dir][/cyan]      — detailed latest trajectory\n"
                    "  [cyan]/harbor tools [dir][/cyan]     — tool usage breakdown\n\n"
                    "[dim]Trajectories are saved under ~/.bog-agents/harbor-results/ by default.[/dim]"
                )
            )

    async def _handle_health_command(self, command: str) -> None:
        """Handle `/health` as a codebase health analysis command."""
        from bog_agents_cli.code_intelligence_cli import generate_health_prompt

        raw_arg = command.strip()[len("/health") :].strip()
        lowered = raw_arg.lower()
        if lowered in {"help", "--help", "-h"}:
            await self._mount_message(UserMessage(command))
            await self._mount_message(
                AppMessage(
                    "Usage: /health | /health quick [paths...] | "
                    "/health detail <area> | /health <paths...>"
                )
            )
            return

        paths: list[str] | None = None
        default_prompt = generate_health_prompt()
        announcement = "Analyzing codebase health..."
        if lowered.startswith("quick"):
            suffix = raw_arg[5:].strip()
            paths = shlex.split(suffix) if suffix else None
            default_prompt = generate_health_prompt(paths)
            default_prompt += (
                "\nKeep the report concise and executive-ready. "
                "Focus on the biggest health risks first."
            )
            announcement = "Running a quick health scan..."
        elif lowered.startswith("detail "):
            area = raw_arg[7:].strip()
            if not area:
                await self._mount_message(UserMessage(command))
                await self._mount_message(AppMessage("Usage: /health detail <area>"))
                return
            default_prompt = generate_health_prompt()
            default_prompt += (
                f"\nFocus deeply on `{area}`. "
                "Include concrete file-level findings and recommended fixes."
            )
            announcement = f"Inspecting health details for {area}..."
        elif raw_arg and lowered != "full":
            paths = shlex.split(raw_arg)
            default_prompt = generate_health_prompt(paths)
            announcement = "Analyzing targeted codebase health..."

        await self._run_prompt_backed_command(
            command,
            prompt_key="health",
            default_prompt=default_prompt,
            announcement=announcement,
        )

    async def _handle_migrate_command(self, command: str) -> None:
        """Handle `/migrate` as a migration-planning workflow."""
        from bog_agents_cli.code_intelligence_cli import generate_migration_prompt

        raw_arg = command.strip()[len("/migrate") :].strip()
        try:
            tokens = shlex.split(raw_arg)
        except ValueError as exc:
            await self._mount_message(UserMessage(command))
            await self._mount_message(AppMessage(f"Could not parse /migrate: {exc}"))
            return
        if len(tokens) < 2:
            await self._mount_message(UserMessage(command))
            await self._mount_message(
                AppMessage("Usage: /migrate <from-tech> <to-tech> [constraints...]")
            )
            return
        from_tech, to_tech = tokens[0], tokens[1]
        default_prompt = generate_migration_prompt(from_tech, to_tech)
        if len(tokens) > 2:
            default_prompt += (
                f"\nAdditional constraints and context:\n{' '.join(tokens[2:])}\n"
            )
        await self._run_prompt_backed_command(
            command,
            prompt_key="migrate",
            default_prompt=default_prompt,
            announcement=f"Planning migration from {from_tech} to {to_tech}...",
        )

    async def _handle_infra_command(self, command: str) -> None:
        """Handle `/infra` as infrastructure generation guidance."""
        from bog_agents_cli.code_intelligence_cli import generate_infra_prompt

        raw_arg = command.strip()[len("/infra") :].strip()
        try:
            tokens = shlex.split(raw_arg)
        except ValueError as exc:
            await self._mount_message(UserMessage(command))
            await self._mount_message(AppMessage(f"Could not parse /infra: {exc}"))
            return
        if len(tokens) < 2:
            await self._mount_message(UserMessage(command))
            await self._mount_message(AppMessage("Usage: /infra <type> <description>"))
            return
        infra_type = tokens[0]
        description = " ".join(tokens[1:])
        await self._run_prompt_backed_command(
            command,
            prompt_key="infra",
            default_prompt=generate_infra_prompt(infra_type, description),
            announcement=f"Designing {infra_type} infrastructure plan...",
        )

    async def _handle_test_command(self, command: str) -> None:
        """Handle `/test` for test generation and quality analysis."""
        from bog_agents_cli.test_tools_cli import (
            generate_audit_prompt,
            generate_coverage_prompt,
            generate_test_prompt,
            parse_test_command,
        )

        raw_arg = command.strip()[len("/test") :].strip()
        if raw_arg.lower() in {"help", "--help", "-h"}:
            await self._mount_message(UserMessage(command))
            await self._mount_message(
                AppMessage(
                    "Usage: /test generate <file> [framework] | "
                    "/test coverage [path] | /test gaps <file-or-path> | "
                    "/test benchmark [path] | /test audit"
                )
            )
            return

        parsed = parse_test_command(raw_arg)
        action = parsed["action"].lower()
        argument = parsed["argument"].strip()
        default_prompt = ""
        announcement = "Preparing test workflow..."

        if action in {"", "coverage"}:
            target = argument or "tests/"
            default_prompt = generate_coverage_prompt(target)
            announcement = f"Analyzing test coverage for {target}..."
        elif action == "generate":
            try:
                tokens = shlex.split(argument)
            except ValueError as exc:
                await self._mount_message(UserMessage(command))
                await self._mount_message(
                    AppMessage(f"Could not parse /test generate: {exc}")
                )
                return
            if not tokens:
                await self._mount_message(UserMessage(command))
                await self._mount_message(
                    AppMessage("Usage: /test generate <file> [framework]")
                )
                return
            source_file = tokens[0]
            framework = tokens[1] if len(tokens) > 1 else "pytest"
            default_prompt = generate_test_prompt(source_file, framework)
            announcement = f"Generating {framework} tests for {source_file}..."
        elif action == "gaps":
            if not argument:
                await self._mount_message(UserMessage(command))
                await self._mount_message(
                    AppMessage("Usage: /test gaps <file-or-path>")
                )
                return
            default_prompt = (
                f"Analyze test gaps for {argument}.\n"
                "Report which functions, methods, and error paths are not covered.\n"
                "Recommend the highest-value tests to add next.\n"
            )
            announcement = f"Reviewing test gaps for {argument}..."
        elif action == "benchmark":
            target = argument or "."
            default_prompt = (
                f"Design a benchmark plan for {target}.\n"
                "Identify critical hot paths, propose benchmark cases, and "
                "suggest tools or commands to run them locally.\n"
            )
            announcement = f"Preparing benchmark plan for {target}..."
        elif action == "audit":
            default_prompt = generate_audit_prompt()
            announcement = "Auditing dependencies and test-related risks..."
        else:
            await self._mount_message(UserMessage(command))
            await self._mount_message(
                AppMessage(
                    "Usage: /test generate <file> [framework] | "
                    "/test coverage [path] | /test gaps <file-or-path> | "
                    "/test benchmark [path] | /test audit"
                )
            )
            return

        await self._run_prompt_backed_command(
            command,
            prompt_key="test",
            default_prompt=default_prompt,
            announcement=announcement,
        )

    async def _handle_resolve_command(self, command: str) -> None:
        """Handle `/resolve` as a merge-conflict resolution workflow."""
        from bog_agents_cli.pr_cli import generate_conflict_resolution_prompt

        await self._mount_message(UserMessage(command))
        success, output = await self._run_git(
            ["diff", "--name-only", "--diff-filter=U"]
        )
        if not success:
            await self._mount_message(
                AppMessage(
                    "Could not inspect merge conflicts in this working directory.\n"
                    f"{output or 'Make sure you are inside a git repository.'}"
                )
            )
            return
        conflicted_files = [
            line.strip() for line in output.splitlines() if line.strip()
        ]
        if not conflicted_files:
            await self._mount_message(AppMessage("No merge conflicts detected."))
            return

        prompt = generate_conflict_resolution_prompt()
        prompt += "\nCurrently conflicted files:\n"
        prompt += "\n".join(f"- {path}" for path in conflicted_files)
        await self._mount_message(
            AppMessage(
                f"Preparing merge-conflict resolution plan for {len(conflicted_files)} file(s)..."
            )
        )
        await self._send_prompt_to_agent(prompt)

    async def _handle_settings_command(self, command: str) -> None:
        """Show settings UI or print the settings path."""
        cmd = command.lower().strip()
        if cmd == "/settings" or "show" in cmd:
            await self._show_settings_screen()
            return
        if "path" in cmd:
            from bog_agents_cli.model_config import DEFAULT_CONFIG_PATH

            await self._mount_message(UserMessage(command))
            await self._mount_message(AppMessage(str(DEFAULT_CONFIG_PATH)))
            return
        await self._show_settings_screen()

    async def _handle_unknown_command(self, command: str) -> None:
        """Render an unknown-command message with suggestions."""
        cmd = self._command_name(command)
        from bog_agents_cli.extensibility import (
            find_extension_command,
            render_extension_command_prompt,
        )

        extension_command = await asyncio.to_thread(
            find_extension_command,
            settings.user_agents_dir,
            cmd,
        )
        if extension_command is not None:
            await self._mount_message(UserMessage(command))
            raw_args = command.strip()[len(cmd) :].strip()
            prompt = render_extension_command_prompt(extension_command, raw_args)
            await self._mount_message(
                AppMessage(
                    f"Running extension command {extension_command.name} "
                    f"from {extension_command.extension_name}..."
                )
            )
            await self._send_prompt_to_agent(prompt)
            return

        await self._mount_message(UserMessage(command))
        suggestions = self._match_slash_commands(cmd, limit=3)
        if suggestions:
            suggestion_lines = "\n".join(
                f"  {name} - {desc}" for name, desc in suggestions
            )
            message = (
                f"Unknown command: {cmd}\n\n"
                "Closest matches:\n"
                f"{suggestion_lines}\n\n"
                "Tip: use `/help <command-or-keyword>` or `/commands`."
            )
        else:
            message = (
                f"Unknown command: {cmd}\n\n"
                "Use `/commands` to browse available slash commands."
            )
        await self._mount_message(AppMessage(message))

    async def _handle_help_command(self, command: str) -> None:
        """Handle `/help` and `/commands` command discovery.

        Args:
            command: Full slash command text.
        """
        await self._mount_message(UserMessage(command))

        if command.startswith("/commands"):
            raw_arg = command.strip()[len("/commands") :].strip()
        else:
            raw_arg = command.strip()[len("/help") :].strip()

        if raw_arg:
            help_text = self._build_command_reference(raw_arg)
        else:
            help_text = (
                "Commands: /help, /commands, /quit, /clear, /compact, /resume, "
                "/threads, /agent, /background, /diff, /worktree, /remote, "
                "/plugin, /profile, /plan, /effort, /mcp, "
                "/model [--model-params JSON] [--default], /reload, "
                "/remember, /tokens, /session, /permissions, /keybindings, "
                "/skills, /review, /doctor, /trace, /logs, /init, "
                "/changelog, /docs, /feedback\n\n"
                "Interactive Features:\n"
                "  Enter           Submit your message\n"
                f"  {newline_shortcut():<15} Insert newline\n"
                "  Shift+Tab       Toggle auto-approve mode\n"
                "  @filename       Auto-complete files and inject content\n"
                "  /command        Slash commands (/help, /clear, /quit)\n"
                "  !command        Run shell commands directly\n\n"
                f"{self._build_command_reference()}\n\n"
                f"Docs: {DOCS_URL}"
            )

        help_text_rich = Text(help_text, style="dim italic")
        if DOCS_URL in help_text:
            help_text_rich.stylize(f"link {DOCS_URL}", help_text.index(DOCS_URL))
        await self._mount_message(AppMessage(help_text_rich))

    async def _handle_resume_command(self, command: str) -> None:
        """Handle `/resume` for fast thread switching.

        Args:
            command: Full slash command text.
        """
        raw_arg = command.strip()[len("/resume") :].strip()
        lowered = raw_arg.lower()

        if lowered in {"list", "browse"}:
            await self._show_thread_selector()
            return

        if not raw_arg or lowered in {"last", "latest", "recent"}:
            from bog_agents_cli.sessions import list_threads

            current_thread = (
                self._session_state.thread_id if self._session_state else None
            )
            threads = await list_threads(limit=10)
            target = next(
                (
                    thread["thread_id"]
                    for thread in threads
                    if thread["thread_id"] != current_thread
                ),
                None,
            )
            if target is None:
                await self._mount_message(
                    AppMessage(
                        "No other saved threads found. "
                        "Use /threads to browse available history."
                    )
                )
                return
            await self._resume_thread(target)
            return

        if lowered.startswith("project "):
            from bog_agents_cli.sessions import find_threads_with_metadata

            project_name = raw_arg[8:].strip()
            if not project_name:
                await self._mount_message(
                    AppMessage("Usage: /resume project <project-name>")
                )
                return
            matches = await find_threads_with_metadata(project=project_name, limit=5)
            if not matches:
                await self._mount_message(
                    AppMessage(f"No saved threads found for project '{project_name}'.")
                )
                return
            await self._resume_thread(matches[0]["thread_id"])
            return

        if lowered.startswith("tag "):
            from bog_agents_cli.sessions import find_threads_with_metadata

            tag = raw_arg[4:].strip()
            if not tag:
                await self._mount_message(AppMessage("Usage: /resume tag <tag>"))
                return
            matches = await find_threads_with_metadata(tag=tag, limit=5)
            if not matches:
                await self._mount_message(
                    AppMessage(f"No saved threads found with tag '{tag}'.")
                )
                return
            await self._resume_thread(matches[0]["thread_id"])
            return

        from bog_agents_cli.sessions import find_similar_threads, thread_exists

        if not await thread_exists(raw_arg):
            matches = await find_similar_threads(raw_arg, limit=5)
            if len(matches) == 1:
                await self._resume_thread(matches[0])
                return
            if matches:
                lines = "\n".join(f"  {match}" for match in matches)
                await self._mount_message(
                    AppMessage(
                        f"No exact thread matched '{raw_arg}'.\n\nClosest thread IDs:\n"
                        f"{lines}\n\nUse `/resume <thread-id>` with one of the "
                        "matches above, or `/resume browse`."
                    )
                )
                return

        await self._resume_thread(raw_arg)

    async def _handle_session_command(self, command: str) -> None:
        """Handle `/session` display and naming actions.

        Args:
            command: Full slash command text.
        """
        await self._mount_message(UserMessage(command))

        from bog_agents_cli.sessions import (
            export_thread,
            format_path,
            get_thread_metadata,
            set_thread_label,
            set_thread_project,
            set_thread_summary,
            set_thread_tags,
        )

        raw_arg = command.strip()[len("/session") :].strip()
        lowered = raw_arg.lower()
        thread_id = self._current_thread_id()
        if thread_id is None:
            await self._mount_message(
                AppMessage("No active thread is available for session metadata.")
            )
            return

        if lowered.startswith(("rename ", "name ")):
            parts = raw_arg.split(maxsplit=1)
            name = parts[1].strip() if len(parts) > 1 else ""
            if not name:
                await self._mount_message(AppMessage("Usage: /session rename <label>"))
                return
            self._session_name = name
            await set_thread_label(thread_id, name)
            await self._mount_message(AppMessage(f"Session label saved: {name}"))
            return

        if lowered in {"clear-name", "name --clear", "rename --clear"}:
            self._session_name = None
            await set_thread_label(thread_id, None)
            await self._mount_message(AppMessage("Session label cleared."))
            return

        metadata = await get_thread_metadata(thread_id)
        metadata_tags = metadata.get("tags")
        current_tags = (
            [tag for tag in metadata_tags if isinstance(tag, str)]
            if isinstance(metadata_tags, list)
            else []
        )

        if lowered.startswith("tag add "):
            new_tags = raw_arg[8:].split()
            if not new_tags:
                await self._mount_message(
                    AppMessage("Usage: /session tag add <tag> [more-tags]")
                )
                return
            updated_tags = await set_thread_tags(thread_id, [*current_tags, *new_tags])
            await self._mount_message(
                AppMessage(f"Session tags: {', '.join(updated_tags)}")
            )
            return

        if lowered.startswith("tag remove "):
            remove_tags = {tag.lower() for tag in raw_arg[11:].split() if tag.strip()}
            if not remove_tags:
                await self._mount_message(
                    AppMessage("Usage: /session tag remove <tag> [more-tags]")
                )
                return
            updated_tags = await set_thread_tags(
                thread_id,
                [tag for tag in current_tags if tag.lower() not in remove_tags],
            )
            tag_text = ", ".join(updated_tags) if updated_tags else "(none)"
            await self._mount_message(AppMessage(f"Session tags: {tag_text}"))
            return

        if lowered in {"tag clear", "tags clear"}:
            await set_thread_tags(thread_id, [])
            await self._mount_message(AppMessage("Session tags cleared."))
            return

        if lowered.startswith("project "):
            project_name = raw_arg[8:].strip()
            if not project_name:
                await self._mount_message(
                    AppMessage("Usage: /session project <project-name>")
                )
                return
            await set_thread_project(thread_id, project_name)
            await self._mount_message(
                AppMessage(f"Session project saved: {project_name}")
            )
            return

        if lowered in {"project clear", "clear-project"}:
            await set_thread_project(thread_id, None)
            await self._mount_message(AppMessage("Session project cleared."))
            return

        if lowered == "summary refresh":
            summary = self._build_session_summary_from_messages()
            await set_thread_summary(thread_id, summary)
            await self._mount_message(AppMessage(f"Session summary saved:\n{summary}"))
            return

        if lowered.startswith("summary "):
            summary = raw_arg[8:].strip()
            if not summary:
                await self._mount_message(
                    AppMessage(
                        "Usage: /session summary <text> | /session summary refresh"
                    )
                )
                return
            await set_thread_summary(thread_id, summary)
            await self._mount_message(AppMessage("Session summary updated."))
            return

        if lowered.startswith("export"):
            export_arg = raw_arg[6:].strip()
            if export_arg:
                raw_export_path = await asyncio.to_thread(
                    self._expand_user_path, export_arg
                )
                export_path = (
                    raw_export_path
                    if raw_export_path.is_absolute()
                    else Path(str(self._cwd)) / raw_export_path
                )
            else:
                export_path = Path(str(self._cwd)) / f"bog-session-{thread_id[:8]}.json"
            payload = await export_thread(thread_id)
            if not payload:
                await self._mount_message(
                    AppMessage(f"Could not export thread '{thread_id}'.")
                )
                return
            export_path.write_text(
                json.dumps(payload, indent=2, ensure_ascii=True),
                encoding="utf-8",
            )
            await self._mount_message(
                AppMessage(f"Session export written to {export_path}")
            )
            return

        if raw_arg and lowered not in {"show", "info", "status", "summary"}:
            await self._mount_message(
                AppMessage(
                    "Usage: /session | /session rename <label> | /session tag add <tag> "
                    "| /session project <name> | /session summary refresh "
                    "| /session export [path]"
                )
            )
            return

        branch = _get_git_branch() or "(not a git repo)"
        current_model = self._model_override or settings.model_name or "auto"
        token_usage = (
            format_token_count(self._token_tracker.current_context)
            if self._token_tracker is not None
            and self._token_tracker.current_context > 0
            else "0"
        )
        metadata = await get_thread_metadata(thread_id)
        label = metadata.get("label")
        if isinstance(label, str) and label.strip():
            self._session_name = label.strip()
        tags_value = metadata.get("tags")
        tags = tags_value if isinstance(tags_value, list) else []
        tag_text = ", ".join(str(tag) for tag in tags) if tags else "(none)"
        project = metadata.get("project") or "(none)"
        summary = metadata.get("summary") or "(none)"
        lines = [
            f"Session: {self._session_name or '(unnamed)'}",
            f"Agent: {self._assistant_id}",
            f"Thread: {thread_id}",
            f"Project: {project}",
            f"Tags: {tag_text}",
            f"Summary: {summary}",
            f"Model: {current_model}",
            f"Profile: {self._active_profile_name or '(none)'}",
            f"Plan mode: {'on' if self._plan_mode_enabled else 'off'}",
            f"Effort: {self._effort_level}",
            f"Auto-approve: {'on' if self._auto_approve else 'off'}",
            f"CWD: {format_path(self._cwd)}",
            f"Git branch: {branch}",
            f"Visible messages: {self._message_store.total_count}",
            f"Requests this session: {self._session_stats.request_count}",
            (
                "Session token totals: "
                f"{format_token_count(self._session_stats.input_tokens)} in / "
                f"{format_token_count(self._session_stats.output_tokens)} out"
            ),
            f"Current context: {token_usage} tokens",
            (
                "Tip: `/session rename`, `/session tag add`, `/session project`, "
                "and `/session summary refresh` persist metadata to the thread."
            ),
        ]
        await self._mount_message(AppMessage("\n".join(lines)))

    async def _handle_rewind_command(self, command: str) -> None:
        """Handle `/rewind` checkpoint browsing and forked recovery."""
        await self._mount_message(UserMessage(command))

        from bog_agents_cli.sessions import (
            format_path,
            format_timestamp,
            get_thread_checkpoint_payload,
            get_thread_metadata,
            list_thread_checkpoints,
            set_thread_label,
            set_thread_project,
            set_thread_tags,
        )

        if self._agent is None:
            await self._mount_message(
                AppMessage("`/rewind` requires an active agent session.")
            )
            return

        thread_id = self._current_thread_id()
        if thread_id is None:
            await self._mount_message(
                AppMessage("No active thread is available to rewind.")
            )
            return

        raw_arg = command.strip()[len("/rewind") :].strip()
        try:
            tokens = shlex.split(raw_arg)
        except ValueError as exc:
            await self._mount_message(
                AppMessage(f"Could not parse /rewind arguments: {exc}")
            )
            return

        action = tokens[0].lower() if tokens else "list"
        checkpoint_token = ""
        if action in {"list", "browse"}:
            checkpoint_token = ""
        elif action in {"show", "preview", "to", "use"}:
            checkpoint_token = tokens[1] if len(tokens) > 1 else ""
        else:
            action = "to"
            checkpoint_token = tokens[0] if tokens else ""

        checkpoints = await list_thread_checkpoints(thread_id, limit=12)
        if not checkpoints:
            await self._mount_message(
                AppMessage(
                    "No rewind checkpoints were found for this thread yet. "
                    "Run another turn and try again."
                )
            )
            return

        if action in {"list", "browse"}:
            lines = [f"Checkpoint history for {thread_id}:"]
            for index, checkpoint in enumerate(checkpoints, start=1):
                timestamp = format_timestamp(checkpoint.get("updated_at"))
                prompt = " ".join((checkpoint.get("initial_prompt") or "").split())
                preview = prompt[:72] + "..." if len(prompt) > 72 else prompt
                if not preview:
                    preview = "(no prompt preview)"
                lines.append(
                    "  "
                    + " | ".join(
                        [
                            self._format_rewind_checkpoint_token(
                                index, checkpoint["checkpoint_id"]
                            ),
                            timestamp or "time unknown",
                            f"{checkpoint['message_count']} msg",
                            preview,
                        ]
                    )
                )
            lines.extend(
                [
                    "",
                    "Usage: /rewind show <checkpoint-id|index>",
                    "Usage: /rewind to <checkpoint-id|index>",
                ]
            )
            await self._mount_message(AppMessage("\n".join(lines)))
            return

        if not checkpoint_token:
            await self._mount_message(
                AppMessage(
                    "Usage: /rewind | /rewind show <checkpoint-id|index> | "
                    "/rewind to <checkpoint-id|index>"
                )
            )
            return

        scored_matches = [
            (
                self._checkpoint_match_score(
                    checkpoint["checkpoint_id"], checkpoint_token, index
                ),
                index,
                checkpoint,
            )
            for index, checkpoint in enumerate(checkpoints, start=1)
        ]
        scored_matches.sort(key=lambda item: (item[0], -item[1]), reverse=True)
        best_score, best_index, best_checkpoint = scored_matches[0]
        if best_score <= 0:
            await self._mount_message(
                AppMessage(
                    f"No checkpoint matched '{checkpoint_token}'. "
                    "Run `/rewind` to browse the available checkpoints."
                )
            )
            return

        checkpoint_id = best_checkpoint["checkpoint_id"]
        if action in {"show", "preview"}:
            lines = [
                f"Checkpoint {checkpoint_id}",
                f"Index: {best_index}",
                f"Thread: {thread_id}",
                f"Captured: {format_timestamp(best_checkpoint.get('updated_at')) or 'time unknown'}",
                f"Messages: {best_checkpoint['message_count']}",
            ]
            if branch := best_checkpoint.get("git_branch"):
                lines.append(f"Branch: {branch}")
            if cwd := best_checkpoint.get("cwd"):
                lines.append(f"Workspace: {format_path(cwd)}")
            if prompt := best_checkpoint.get("initial_prompt"):
                lines.extend(["", "Initial prompt:", prompt])
            lines.extend(
                ["", f"Use `/rewind to {checkpoint_id}` to continue from here."]
            )
            await self._mount_message(AppMessage("\n".join(lines)))
            return

        payload = await get_thread_checkpoint_payload(thread_id, checkpoint_id)
        if payload is None or not payload["messages"]:
            await self._mount_message(
                AppMessage(
                    f"Checkpoint {checkpoint_id} could not be restored into a new thread."
                )
            )
            return

        new_thread_id = _new_thread_id()
        await self._seed_thread_from_checkpoint(new_thread_id, payload)

        metadata = await get_thread_metadata(thread_id)
        label = metadata.get("label")
        project = metadata.get("project")
        tags = metadata.get("tags")
        rewind_label = (
            f"{label.strip()} (rewind)"
            if isinstance(label, str) and label.strip()
            else f"rewind-{thread_id[:8]}"
        )
        with suppress(Exception):
            await set_thread_label(new_thread_id, rewind_label)
        if isinstance(project, str) and project.strip():
            with suppress(Exception):
                await set_thread_project(new_thread_id, project.strip())
        if isinstance(tags, list):
            rewind_tags = [*(str(tag) for tag in tags), "rewind"]
            with suppress(Exception):
                await set_thread_tags(new_thread_id, rewind_tags)

        await self._resume_thread(new_thread_id)
        await self._mount_message(
            AppMessage(
                "\n".join(
                    [
                        f"Forked rewind thread {new_thread_id}",
                        f"Source thread: {thread_id}",
                        f"Checkpoint: {checkpoint_id}",
                        f"Messages restored: {payload['message_count']}",
                    ]
                )
            )
        )

    @staticmethod
    def _format_replay_timestamp(timestamp: float) -> str:
        """Return a compact local timestamp string for replay metadata."""
        from datetime import UTC, datetime

        if timestamp <= 0:
            return "unknown"
        return datetime.fromtimestamp(timestamp, tz=UTC).strftime("%Y-%m-%d %H:%M UTC")

    @staticmethod
    def _replay_match_score(name: str, token: str) -> int:
        """Return a simple match score for resolving replay sessions."""
        normalized_name = name.strip().lower()
        normalized_token = token.strip().lower()
        if normalized_name == normalized_token:
            return 100
        if normalized_name.startswith(normalized_token):
            return 80
        if normalized_token in normalized_name:
            return 50
        return 0

    async def _find_replay_session(self, token: str) -> tuple[Any, Path] | None:
        """Resolve a replay session by session ID, file stem, or name."""
        from bog_agents_cli.replay import list_replay_sessions

        replays_dir = settings.user_agents_dir / "replays"
        sessions = await asyncio.to_thread(
            list_replay_sessions, settings.user_agents_dir
        )
        if not sessions:
            return None

        scored: list[tuple[int, Any, Path]] = []
        for session in sessions:
            file_path = replays_dir / f"{session.session_id}.json"
            best = max(
                self._replay_match_score(session.session_id, token),
                self._replay_match_score(file_path.stem, token),
                self._replay_match_score(session.name or "", token),
            )
            if best:
                scored.append((best, session, file_path))
        if not scored:
            return None
        scored.sort(key=lambda item: (-item[0], item[1].session_id))
        _score, session, file_path = scored[0]
        return session, file_path

    async def _handle_record_command(self, command: str) -> None:
        """Handle `/record` for replay capture management."""
        await self._mount_message(UserMessage(command))

        raw_arg = command.strip()[len("/record") :].strip()
        lowered = raw_arg.lower()
        thread_id = self._current_thread_id()

        if lowered in {"", "status"}:
            if self._recording_state is None:
                await self._mount_message(AppMessage("No replay recording is active."))
                return
            state = self._recording_state
            await self._mount_message(
                AppMessage(
                    "\n".join(
                        [
                            f"Recording: {state.name}",
                            f"Thread: {state.thread_id}",
                            f"Started: {self._format_replay_timestamp(state.started_at)}",
                            f"Baseline messages: {state.baseline_message_count}",
                        ]
                    )
                )
            )
            return

        if lowered.startswith("start"):
            if thread_id is None:
                await self._mount_message(
                    AppMessage("No active thread is available to record.")
                )
                return
            if self._recording_state is not None:
                await self._mount_message(
                    AppMessage(
                        f"Recording already active: {self._recording_state.name}. "
                        "Use `/record stop` before starting another one."
                    )
                )
                return
            from bog_agents_cli.sessions import export_thread

            payload = await export_thread(thread_id)
            transcript = (
                payload.get("transcript", []) if isinstance(payload, dict) else []
            )
            baseline = len(transcript) if isinstance(transcript, list) else 0
            name = raw_arg[5:].strip() or f"Replay {thread_id[:8]}"
            self._recording_state = RecordingSessionState(
                session_id=f"replay-{uuid.uuid4().hex[:8]}",
                name=name,
                thread_id=thread_id,
                cwd=str(self._cwd),
                started_at=time.time(),
                baseline_message_count=baseline,
            )
            await self._mount_message(
                AppMessage(f"Started replay recording `{name}` on thread {thread_id}.")
            )
            return

        if lowered == "stop":
            if self._recording_state is None:
                await self._mount_message(AppMessage("No replay recording is active."))
                return
            from bog_agents_cli.replay import (
                ReplayAction,
                ReplaySession,
                save_replay_session,
            )
            from bog_agents_cli.sessions import export_thread

            state = self._recording_state
            payload = await export_thread(state.thread_id)
            transcript = (
                payload.get("transcript", []) if isinstance(payload, dict) else []
            )
            transcript_list = transcript if isinstance(transcript, list) else []
            entries = transcript_list[state.baseline_message_count :]
            actions: list[Any] = []
            step = 0
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                entry_dict = cast("dict[str, object]", entry)
                role = str(entry_dict.get("role", "")).lower()
                content = str(entry_dict.get("content", "")).strip()
                if not content:
                    continue
                if role == "human":
                    action_type = "user_message"
                elif role == "ai":
                    action_type = "ai_message"
                else:
                    continue
                step += 1
                actions.append(
                    ReplayAction(
                        step=step,
                        action_type=action_type,
                        content=content[:500],
                    )
                )

            if not actions:
                self._recording_state = None
                await self._mount_message(
                    AppMessage(
                        "Recording stopped, but no new replayable conversation steps were captured."
                    )
                )
                return

            session = ReplaySession(
                session_id=state.session_id,
                name=state.name,
                description=f"Recorded from thread {state.thread_id}",
                recorded_at=state.started_at,
                original_context={"cwd": state.cwd, "thread_id": state.thread_id},
                actions=actions,
            )
            file_path = await asyncio.to_thread(
                save_replay_session,
                settings.user_agents_dir,
                session,
            )
            self._recording_state = None
            await self._mount_message(
                AppMessage(
                    f"Saved replay session `{session.name}` with {len(actions)} step(s) to {file_path}"
                )
            )
            return

        await self._mount_message(
            AppMessage("Usage: /record start [name] | /record status | /record stop")
        )

    async def _handle_replay_command(self, command: str) -> None:
        """Handle `/replay` for inspecting and rerunning recorded sessions."""
        from bog_agents_cli.replay import generate_replay_prompt, list_replay_sessions

        await self._mount_message(UserMessage(command))
        raw_arg = command.strip()[len("/replay") :].strip()
        try:
            tokens = shlex.split(raw_arg)
        except ValueError as exc:
            await self._mount_message(
                AppMessage(f"Could not parse /replay arguments: {exc}")
            )
            return

        action = tokens[0].lower() if tokens else "list"
        if action in {"help", "--help", "-h"}:
            await self._mount_message(
                AppMessage(
                    "Usage: /replay | /replay list | /replay show <id-or-name> | "
                    "/replay run <id-or-name> [extra-context]"
                )
            )
            return

        if action in {"", "list"}:
            sessions = await asyncio.to_thread(
                list_replay_sessions, settings.user_agents_dir
            )
            if not sessions:
                await self._mount_message(AppMessage("No replay sessions saved yet."))
                return
            lines = ["Saved replay sessions:"]
            for session in sessions:
                label = session.name or session.session_id
                lines.append(
                    "  "
                    f"{label} ({session.session_id}) - "
                    f"{len(session.actions)} step(s), "
                    f"{self._format_replay_timestamp(session.recorded_at)}"
                )
            await self._mount_message(AppMessage("\n".join(lines)))
            return

        if len(tokens) < 2:
            await self._mount_message(
                AppMessage(
                    "Usage: /replay show <id-or-name> | /replay run <id-or-name> [extra-context]"
                )
            )
            return

        match = await self._find_replay_session(tokens[1])
        if match is None:
            await self._mount_message(
                AppMessage(f"No replay session matched '{tokens[1]}'.")
            )
            return
        session, file_path = match

        if action == "show":
            lines = [
                f"Replay: {session.name or session.session_id}",
                f"ID: {session.session_id}",
                f"Recorded: {self._format_replay_timestamp(session.recorded_at)}",
                f"File: {file_path}",
                f"Steps: {len(session.actions)}",
                "",
            ]
            if session.description:
                lines.append(session.description)
                lines.append("")
            for action_item in session.actions[:12]:
                preview = (
                    action_item.content.strip() or action_item.tool_name or "(empty)"
                )
                lines.append(
                    f"{action_item.step}. {action_item.action_type}: {preview[:120]}"
                )
            if len(session.actions) > 12:
                lines.append("...")
            await self._mount_message(AppMessage("\n".join(lines)))
            return

        if action == "run":
            extra_context = " ".join(tokens[2:]).strip()
            prompt = generate_replay_prompt(
                session,
                {"cwd": str(self._cwd)},
            )
            if extra_context:
                prompt += f"\n\n## Extra Context\n\n{extra_context}\n"
            await self._mount_message(
                AppMessage(
                    f"Replaying session `{session.name or session.session_id}`..."
                )
            )
            await self._send_prompt_to_agent(prompt)
            return

        await self._mount_message(
            AppMessage(
                "Usage: /replay | /replay list | /replay show <id-or-name> | "
                "/replay run <id-or-name> [extra-context]"
            )
        )

    async def _handle_profile_command(self, command: str) -> None:
        """Handle `/profile` for runtime workflow presets."""
        await self._mount_message(UserMessage(command))

        from bog_agents_cli.profiles import load_profiles

        profiles = load_profiles(settings.user_agents_dir)
        raw_arg = command.strip()[len("/profile") :].strip()
        lowered = raw_arg.lower()

        if not raw_arg or lowered in {"list", "show", "status"}:
            active = self._active_profile_name or "(none)"
            lines = [
                f"Active profile: {active}",
                f"Plan mode: {'on' if self._plan_mode_enabled else 'off'}",
                f"Effort: {self._effort_level}",
                f"Auto-approve: {'on' if self._auto_approve else 'off'}",
                "",
                "Available profiles:",
            ]
            for name, profile in sorted(profiles.items()):
                marker = " (active)" if name == self._active_profile_name else ""
                summary = profile.description or "No description"
                lines.append(f"  {name}{marker} - {summary}")
            lines.append("")
            lines.append("Usage: /profile <name> | /profile clear")
            await self._mount_message(AppMessage("\n".join(lines)))
            return

        if lowered in {"clear", "none", "off"}:
            self._active_profile_name = None
            self._active_profile_prompt = None
            self._plan_mode_enabled = False
            self._effort_level = "high"
            self._auto_approve = self._base_auto_approve
            if self._status_bar:
                self._status_bar.auto_approve = self._auto_approve
            if self._base_model_spec:
                with suppress(Exception):
                    await self._apply_runtime_model_override(self._base_model_spec)
            await self._mount_message(
                AppMessage(
                    "Profile cleared. Session returned to direct controls "
                    "(plan off, effort high, profile prompt removed)."
                )
            )
            return

        target = raw_arg[4:].strip() if lowered.startswith("use ") else raw_arg
        profile = profiles.get(target)
        if profile is None:
            suggestions = ", ".join(
                sorted(name for name in profiles if target.lower() in name.lower())
            )
            suffix = f" Closest matches: {suggestions}" if suggestions else ""
            await self._mount_message(
                AppMessage(f"Unknown profile '{target}'.{suffix}")
            )
            return

        self._active_profile_name = profile.name
        self._active_profile_prompt = profile.system_prompt_append
        if profile.auto_approve is not None:
            self._auto_approve = profile.auto_approve
            if self._status_bar:
                self._status_bar.auto_approve = self._auto_approve
        if profile.plan_mode is not None:
            self._plan_mode_enabled = profile.plan_mode
        if profile.effort_level:
            self._effort_level = profile.effort_level

        lines = [
            f"Profile activated: {profile.name}",
            profile.description or "No description provided.",
            f"Plan mode: {'on' if self._plan_mode_enabled else 'off'}",
            f"Effort: {self._effort_level}",
            f"Auto-approve: {'on' if self._auto_approve else 'off'}",
        ]

        if profile.model:
            try:
                model_display = await self._apply_runtime_model_override(profile.model)
            except Exception as exc:
                lines.append(f"Model override failed: {exc}")
            else:
                lines.append(f"Model: {model_display}")

        if profile.system_prompt_append:
            lines.append("Additional workflow guidance will be applied next turn.")

        await self._mount_message(AppMessage("\n".join(lines)))

    async def _handle_plan_command(self, command: str) -> None:
        """Handle `/plan` runtime read-only mode toggles."""
        await self._mount_message(UserMessage(command))

        raw_arg = command.strip()[len("/plan") :].strip().lower()
        if not raw_arg or raw_arg in {"show", "status"}:
            await self._mount_message(
                AppMessage(
                    "Plan mode is "
                    f"{'ON' if self._plan_mode_enabled else 'OFF'}.\n"
                    "When enabled, mutating tools are hidden from the model and "
                    "the system prompt is constrained to planning-only behavior.\n"
                    "Usage: /plan on | /plan off | /plan toggle"
                )
            )
            return

        if raw_arg == "toggle":
            self._plan_mode_enabled = not self._plan_mode_enabled
        elif raw_arg in {"on", "enable", "enabled"}:
            self._plan_mode_enabled = True
        elif raw_arg in {"off", "disable", "disabled"}:
            self._plan_mode_enabled = False
        else:
            await self._mount_message(
                AppMessage("Usage: /plan on | /plan off | /plan toggle")
            )
            return

        state = "enabled" if self._plan_mode_enabled else "disabled"
        await self._mount_message(
            AppMessage(
                f"Plan mode {state}. The new mode will apply on the next agent turn."
            )
        )

    async def _handle_effort_command(self, command: str) -> None:
        """Handle `/effort` runtime reasoning presets."""
        await self._mount_message(UserMessage(command))

        descriptions = {
            "low": "Quick responses with minimal reasoning overhead.",
            "medium": "Balanced reasoning and speed.",
            "high": "Thorough analysis for most coding tasks.",
            "max": "Maximum reasoning depth for complex work.",
        }
        raw_arg = command.strip()[len("/effort") :].strip().lower()
        if not raw_arg or raw_arg in {"show", "status"}:
            lines = [f"Current effort: {self._effort_level}", ""]
            lines.extend(f"  {level} - {desc}" for level, desc in descriptions.items())
            lines.append("")
            lines.append("Usage: /effort low|medium|high|max")
            await self._mount_message(AppMessage("\n".join(lines)))
            return

        if raw_arg not in descriptions:
            await self._mount_message(AppMessage("Usage: /effort low|medium|high|max"))
            return

        self._effort_level = raw_arg
        await self._mount_message(
            AppMessage(
                f"Effort set to {raw_arg}. {descriptions[raw_arg]} "
                "The new preset will apply on the next agent turn."
            )
        )

    async def _handle_diff_command(self, command: str) -> None:
        """Handle `/diff` for local git change inspection."""
        await self._mount_message(UserMessage(command))

        raw_arg = command.strip()[len("/diff") :].strip()
        if raw_arg in {"help", "--help", "-h"}:
            await self._mount_message(
                AppMessage(
                    "Usage: /diff | /diff --cached | /diff --stat | "
                    "/diff --name-only | /diff <git-diff-args>"
                )
            )
            return

        if raw_arg.lower() in {"cached", "staged"}:
            args = ["diff", "--cached", "--minimal"]
        elif raw_arg.lower() in {"stat", "--stat"}:
            args = ["diff", "--stat"]
        elif raw_arg.lower() in {"names", "name-only", "--name-only"}:
            args = ["diff", "--name-only"]
        elif raw_arg:
            args = ["diff", *shlex.split(raw_arg)]
        else:
            args = ["diff", "--minimal"]

        success, output = await self._run_git(args)
        if not success:
            await self._mount_message(
                AppMessage(
                    "Could not read git diff for this working directory.\n"
                    f"{output or 'Make sure you are inside a git repository.'}"
                )
            )
            return
        if not output.strip():
            await self._mount_message(AppMessage("No pending git changes."))
            return
        await self._mount_message(AppMessage(output))

    async def _handle_branch_command(self, command: str) -> None:
        """Handle `/branch` as a lightweight local git-branch helper."""
        await self._mount_message(UserMessage(command))

        repo_root = await self._get_repo_root()
        if repo_root is None:
            await self._mount_message(
                AppMessage("`/branch` requires a git repository.")
            )
            return

        raw_arg = command.strip()[len("/branch") :].strip()
        try:
            tokens = shlex.split(raw_arg)
        except ValueError as exc:
            await self._mount_message(
                AppMessage(f"Could not parse /branch arguments: {exc}")
            )
            return
        lowered = tokens[0].lower() if tokens else "list"

        if lowered in {"help", "--help", "-h"}:
            await self._mount_message(
                AppMessage(
                    "Usage: /branch | /branch status | /branch create <name> [start-point] | "
                    "/branch switch <name>"
                )
            )
            return

        if not tokens or lowered in {"list", "ls"}:
            success, output = await self._run_git(
                [
                    "branch",
                    "--sort=-committerdate",
                    "--format=%(HEAD) %(refname:short) %(upstream:short) %(subject)",
                ],
                cwd=repo_root,
            )
            if not success:
                await self._mount_message(
                    AppMessage(output or "Could not list branches.")
                )
                return
            await self._mount_message(
                AppMessage(
                    "Branches:\n"
                    + (output.strip() or "(no local branches)")
                    + "\n\nUse `/branch create <name>` or `/branch switch <name>`."
                )
            )
            return

        if lowered == "status":
            success, output = await self._run_git(
                ["status", "--short", "--branch"],
                cwd=repo_root,
            )
            if not success:
                await self._mount_message(
                    AppMessage(output or "Could not read branch status.")
                )
                return
            await self._mount_message(
                AppMessage(output.strip() or "Working tree is clean.")
            )
            return

        if lowered == "create":
            if len(tokens) < 2:
                await self._mount_message(
                    AppMessage("Usage: /branch create <name> [start-point]")
                )
                return
            branch_name = tokens[1]
            args = ["switch", "-c", branch_name]
            if len(tokens) > 2:
                args.append(tokens[2])
            success, output = await self._run_git(args, cwd=repo_root)
            if not success:
                await self._mount_message(
                    AppMessage(output or f"Could not create branch {branch_name}.")
                )
                return
            if self._status_bar:
                self._status_bar.branch = _get_git_branch() or ""
            message = output.strip() or f"Switched to a new branch `{branch_name}`."
            await self._mount_message(AppMessage(message))
            return

        if lowered in {"switch", "checkout"} or len(tokens) == 1:
            branch_name = tokens[1] if lowered in {"switch", "checkout"} else tokens[0]
            if not branch_name:
                await self._mount_message(AppMessage("Usage: /branch switch <name>"))
                return
            success, output = await self._run_git(
                ["switch", branch_name],
                cwd=repo_root,
            )
            if not success:
                await self._mount_message(
                    AppMessage(output or f"Could not switch to branch {branch_name}.")
                )
                return
            if self._status_bar:
                self._status_bar.branch = _get_git_branch() or ""
            message = output.strip() or f"Switched to branch `{branch_name}`."
            await self._mount_message(AppMessage(message))
            return

        await self._mount_message(
            AppMessage(
                "Usage: /branch | /branch status | /branch create <name> [start-point] | "
                "/branch switch <name>"
            )
        )

    async def _handle_undo_command(self, command: str) -> None:
        """Handle `/undo` as a safe git-backed restore helper."""
        await self._mount_message(UserMessage(command))

        repo_root = await self._get_repo_root()
        if repo_root is None:
            await self._mount_message(AppMessage("`/undo` requires a git repository."))
            return

        raw_arg = command.strip()[len("/undo") :].strip()
        try:
            tokens = shlex.split(raw_arg)
        except ValueError as exc:
            await self._mount_message(
                AppMessage(f"Could not parse /undo arguments: {exc}")
            )
            return
        lowered = tokens[0].lower() if tokens else "status"

        if lowered in {"help", "--help", "-h"}:
            await self._mount_message(
                AppMessage(
                    "Usage: /undo | /undo status | /undo diff | "
                    "/undo restore <path...> | /undo restore --all\n"
                    "Note: `/undo` only restores tracked changes. Untracked files are left alone."
                )
            )
            return

        if lowered in {"status", ""}:
            success, output = await self._run_git(
                ["status", "--short"],
                cwd=repo_root,
            )
            if not success:
                await self._mount_message(
                    AppMessage(output or "Could not inspect pending changes.")
                )
                return
            if not output.strip():
                await self._mount_message(AppMessage("Working tree is clean."))
                return
            await self._mount_message(
                AppMessage(
                    "Tracked changes pending:\n"
                    f"{output}\n\nUse `/undo restore <path...>` to restore tracked files."
                )
            )
            return

        if lowered == "diff":
            success, output = await self._run_git(
                ["diff", "--stat"],
                cwd=repo_root,
            )
            if not success:
                await self._mount_message(
                    AppMessage(output or "Could not inspect diff stats.")
                )
                return
            await self._mount_message(
                AppMessage(output.strip() or "No tracked diff to undo.")
            )
            return

        if lowered == "restore":
            targets = tokens[1:]
            if not targets:
                await self._mount_message(
                    AppMessage("Usage: /undo restore <path...> | /undo restore --all")
                )
                return
            pathspecs = ["."] if targets == ["--all"] else targets
            args = [
                "restore",
                "--source=HEAD",
                "--staged",
                "--worktree",
                "--",
                *pathspecs,
            ]
            success, output = await self._run_git(args, cwd=repo_root)
            if not success:
                await self._mount_message(
                    AppMessage(output or "Could not restore the requested files.")
                )
                return
            status_success, status_output = await self._run_git(
                ["status", "--short"],
                cwd=repo_root,
            )
            if self._status_bar:
                self._status_bar.branch = _get_git_branch() or ""
            lines = [
                f"Restored tracked changes for: {', '.join(pathspecs)}",
            ]
            if output.strip():
                lines.append(output.strip())
            if status_success:
                lines.extend(
                    [
                        "",
                        "Remaining changes:",
                        status_output.strip() or "(clean)",
                    ]
                )
            await self._mount_message(AppMessage("\n".join(lines)))
            return

        await self._mount_message(
            AppMessage(
                "Usage: /undo | /undo status | /undo diff | "
                "/undo restore <path...> | /undo restore --all"
            )
        )

    async def _handle_agent_command(self, command: str) -> None:
        """Handle `/agent` as a first-class supervisor over managed workers."""
        await self._mount_message(UserMessage(command))
        await self._ensure_background_manager()

        from bog_agents.middleware.worktree import create_worktree

        from bog_agents_cli.remote import (
            RemoteStatus,
            cancel_remote_task,
            format_remote_tasks,
            load_remote_config,
            submit_remote_task,
        )

        raw_arg = command.strip()[len("/agent") :].strip()
        lowered = raw_arg.lower()

        if not raw_arg or lowered in {"list", "status"}:
            await self._refresh_remote_tasks()
            current_thread = self._current_thread_id() or "(none)"
            active_team = self._active_team() or "(none)"
            lines = [
                f"Current thread: {current_thread}",
                f"Active team: {active_team}",
                "",
                self._bg_manager.format_status_table(),
                "",
                format_remote_tasks(list(self._remote_tasks.values())),
                "",
                (
                    "Usage: /agent spawn [--count N] [--label LABEL] [--model MODEL] "
                    "[--team TEAM] [--remote] [--worktree] "
                    "[--branch-prefix PREFIX] <prompt>"
                ),
                "Usage: /agent status <id> | /agent stop <id> | /agent cleanup",
                "Usage: /agent switch <thread-id>",
            ]
            await self._mount_message(AppMessage("\n".join(lines)))
            return

        if lowered == "cleanup":
            local_removed = self._bg_manager.cleanup_completed()
            remote_removed = 0
            remote_done = {
                RemoteStatus.COMPLETED,
                RemoteStatus.FAILED,
                RemoteStatus.CANCELLED,
            }
            for task_id in list(self._remote_tasks):
                if self._remote_tasks[task_id].status in remote_done:
                    remote_removed += 1
            if remote_removed:
                done_ids = [
                    task_id
                    for task_id, task in self._remote_tasks.items()
                    if task.status in remote_done
                ]
                await self._drop_remote_tasks(done_ids)
            await self._mount_message(
                AppMessage(
                    f"Cleaned up {local_removed} local tasks and "
                    f"{remote_removed} remote tasks."
                )
            )
            return

        if lowered.startswith("status "):
            task_id = raw_arg[7:].strip()
            if not task_id:
                await self._mount_message(AppMessage("Usage: /agent status <id>"))
                return
            if local_task := self._bg_manager.get_status(task_id):
                await self._mount_message(
                    AppMessage(self._format_background_task_detail(local_task))
                )
                return
            remote_task = await self._resolve_remote_task(task_id)
            if remote_task is not None:
                await self._refresh_remote_tasks()
                refreshed = self._remote_tasks.get(task_id, remote_task)
                await self._mount_message(AppMessage(format_remote_tasks([refreshed])))
                return
            await self._mount_message(AppMessage(f"Managed task {task_id} not found."))
            return

        if lowered.startswith("stop "):
            task_id = raw_arg[5:].strip()
            if not task_id:
                await self._mount_message(AppMessage("Usage: /agent stop <id>"))
                return
            if self._bg_manager.cancel(task_id):
                await self._mount_message(AppMessage(f"Stop requested for {task_id}."))
                return
            remote_task = await self._resolve_remote_task(task_id)
            if remote_task is not None:
                remote_config = await asyncio.to_thread(
                    load_remote_config, settings.user_agents_dir
                )
                updated = await cancel_remote_task(
                    remote_config,
                    remote_task,
                )
                await self._store_remote_task(updated)
                await self._mount_message(AppMessage(format_remote_tasks([updated])))
                return
            await self._mount_message(
                AppMessage(f"Task {task_id} was not found or is not running.")
            )
            return

        if lowered.startswith("switch "):
            thread_id = raw_arg[7:].strip()
            if not thread_id:
                await self._mount_message(
                    AppMessage("Usage: /agent switch <thread-id>")
                )
                return
            await self._resume_thread(thread_id)
            return

        if lowered.startswith("spawn "):
            raw_spawn = raw_arg[6:].strip()
            try:
                tokens = shlex.split(raw_spawn)
            except ValueError as exc:
                await self._mount_message(
                    AppMessage(f"Could not parse spawn command: {exc}")
                )
                return

            count = 1
            label = ""
            model: str | None = None
            team_name: str | None = self._active_team()
            use_remote = False
            use_worktree = False
            branch_prefix = ""
            idx = 0
            while idx < len(tokens) and tokens[idx].startswith("--"):
                flag = tokens[idx]
                if flag == "--remote":
                    use_remote = True
                    idx += 1
                    continue
                if flag == "--worktree":
                    use_worktree = True
                    idx += 1
                    continue
                if flag in {
                    "--count",
                    "--label",
                    "--model",
                    "--branch-prefix",
                    "--team",
                }:
                    if idx + 1 >= len(tokens):
                        await self._mount_message(
                            AppMessage(f"Missing value for {flag}.")
                        )
                        return
                    value = tokens[idx + 1]
                    if flag == "--count":
                        try:
                            count = int(value)
                        except ValueError:
                            await self._mount_message(
                                AppMessage("--count must be an integer.")
                            )
                            return
                    elif flag == "--label":
                        label = value
                    elif flag == "--model":
                        model = value
                    elif flag == "--team":
                        team_name = value
                    else:
                        branch_prefix = value
                    idx += 2
                    continue
                await self._mount_message(
                    AppMessage(
                        "Usage: /agent spawn [--count N] [--label LABEL] "
                        "[--model MODEL] [--team TEAM] [--remote] [--worktree] "
                        "[--branch-prefix PREFIX] <prompt>"
                    )
                )
                return

            prompt = " ".join(tokens[idx:]).strip()
            if not prompt:
                await self._mount_message(
                    AppMessage(
                        "Usage: /agent spawn [--count N] [--label LABEL] "
                        "[--model MODEL] [--team TEAM] [--remote] [--worktree] "
                        "[--branch-prefix PREFIX] <prompt>"
                    )
                )
                return

            if count < 1 or count > 16:
                await self._mount_message(
                    AppMessage("--count must be between 1 and 16.")
                )
                return
            if use_remote and use_worktree:
                await self._mount_message(
                    AppMessage(
                        "Choose either `--remote` or `--worktree` for a spawn batch, not both."
                    )
                )
                return

            repo_root: Path | None = None
            if use_worktree:
                repo_root = await self._get_repo_root()
                if repo_root is None:
                    await self._mount_message(
                        AppMessage(
                            "`/agent spawn --worktree` requires a git repository."
                        )
                    )
                    return

            remote_config = None
            if use_remote:
                remote_config = await asyncio.to_thread(
                    load_remote_config, settings.user_agents_dir
                )

            lines = [f"Spawned {count} managed agent task(s):"]
            for index in range(1, count + 1):
                task_label = self._build_agent_task_label(
                    prompt,
                    label=label,
                    index=index if count > 1 else 1,
                )
                model_spec = model or self._model_override or settings.model_name or ""
                effective_prompt, team_brief = self._build_team_effective_prompt(
                    prompt,
                    team_name,
                )
                if use_remote:
                    remote_task = await submit_remote_task(
                        remote_config,
                        effective_prompt,
                        model=model_spec,
                        label=task_label,
                        working_dir=Path(self._cwd),
                        assistant_id=self._assistant_id,
                        branch_prefix=branch_prefix,
                    )
                    remote_task.prompt = prompt
                    if team_name:
                        remote_task.metadata["team_name"] = team_name
                    if team_brief:
                        remote_task.metadata["team_brief"] = team_brief
                    await self._store_remote_task(remote_task)
                    lines.append(
                        f"  {remote_task.task_id} [remote]"
                        f"{f' [{team_name}]' if team_name else ''} "
                        f"{task_label or prompt[:24]}"
                    )
                    continue

                working_dir = self._cwd
                strategy = "local"
                worktree_branch = None
                metadata: dict[str, Any] = {}
                if team_name:
                    metadata["team_name"] = team_name
                if team_brief:
                    metadata["team_brief"] = team_brief
                metadata["effective_prompt"] = effective_prompt
                if use_worktree and repo_root is not None:
                    prefix = branch_prefix or label or prompt
                    slug = self._slugify_branch_fragment(prefix)
                    worktree_branch = f"agent/{slug}-{uuid.uuid4().hex[:6]}"
                    worktree = await asyncio.to_thread(
                        create_worktree,
                        repo_root,
                        worktree_branch,
                    )
                    working_dir = str(worktree.path)
                    strategy = "worktree"
                    metadata["worktree_path"] = str(worktree.path)

                try:
                    task_id = await self._submit_managed_local_task(
                        prompt,
                        label=task_label,
                        model=model,
                        working_dir=working_dir,
                        strategy=strategy,
                        worktree_branch=worktree_branch,
                        metadata=metadata,
                    )
                except RuntimeError as exc:
                    lines.append(f"  failed: {exc}")
                    break

                detail = (
                    f"  {task_id} [{strategy}]"
                    f"{f' [{team_name}]' if team_name else ''} "
                    f"{task_label or prompt[:24]}"
                )
                if worktree_branch:
                    detail += f" -> {worktree_branch}"
                lines.append(detail)

            lines.append("")
            lines.append(
                "Use `/agent` to monitor the fleet or `/agent status <id>` for detail."
            )
            await self._mount_message(AppMessage("\n".join(lines)))
            return

        await self._mount_message(
            AppMessage(
                "Usage: /agent | /agent spawn [--count N] [--team TEAM] "
                "[--remote] [--worktree] <prompt> | "
                "/agent status <id> | /agent stop <id> | /agent cleanup | "
                "/agent switch <thread-id>"
            )
        )

    async def _handle_permissions_command(self, command: str) -> None:
        """Handle `/permissions` by showing approval posture and shell policy.

        Args:
            command: Full slash command text.
        """
        await self._mount_message(UserMessage(command))

        from bog_agents_cli.config import RECOMMENDED_SAFE_SHELL_COMMANDS

        allow_list = settings.shell_allow_list
        if allow_list is None:
            shell_summary = "disabled"
            shell_detail = (
                "Start the CLI with `--shell-allow-list recommended` or "
                "`--shell-allow-list all` to enable shell access."
            )
        elif list(allow_list) == ["__ALL__"]:
            shell_summary = "all commands auto-approved"
            shell_detail = "Any shell command can run without an approval prompt."
        elif list(allow_list) == list(RECOMMENDED_SAFE_SHELL_COMMANDS):
            shell_summary = "recommended safe list"
            shell_detail = ", ".join(list(allow_list)[:10])
        else:
            shell_summary = f"{len(allow_list)} auto-approved commands"
            preview = ", ".join(list(allow_list)[:10])
            remainder = len(allow_list) - 10
            suffix = f", +{remainder} more" if remainder > 0 else ""
            shell_detail = preview + suffix

        lines = [
            "Permissions",
            f"Auto-approve: {'on' if self._auto_approve else 'off'}",
            f"Shell allow-list: {shell_summary}",
            f"Shell detail: {shell_detail}",
            "Shift+Tab toggles auto-approve for the current session.",
            (
                "Tool approvals still appear when a command or tool is not "
                "covered by the current policy."
            ),
        ]
        await self._mount_message(AppMessage("\n".join(lines)))

    async def _handle_keybindings_command(self, command: str) -> None:
        """Handle `/keybindings` by showing current keybinding configuration.

        Args:
            command: Full slash command text.
        """
        await self._mount_message(UserMessage(command))

        raw_arg = command.strip()[len("/keybindings") :].strip().lower()
        config_dir = Path.home() / ".bog-agents"
        keybindings_path = config_dir / "keybindings.json"

        if raw_arg == "path":
            await self._mount_message(AppMessage(str(keybindings_path)))
            return

        from bog_agents_cli.keybindings import format_keybindings, load_keybindings

        config = load_keybindings(config_dir)
        message = (
            f"Keybindings file: {keybindings_path}\n\n"
            f"{format_keybindings(config)}\n\n"
            "Edit the JSON file to override defaults."
        )
        await self._mount_message(AppMessage(message))

    async def _handle_skills_command(self, command: str) -> None:
        """Handle `/skills` by summarizing currently discoverable skills.

        Args:
            command: Full slash command text.
        """
        await self._mount_message(UserMessage(command))

        from bog_agents_cli.extensibility import get_extension_skill_dirs
        from bog_agents_cli.skills.load import list_skills

        user_skills_dir = settings.get_user_skills_dir(self._assistant_id)
        project_skills_dir = settings.get_project_skills_dir()
        user_agent_skills_dir = settings.get_user_agent_skills_dir()
        project_agent_skills_dir = settings.get_project_agent_skills_dir()
        built_in_skills_dir = settings.get_built_in_skills_dir()
        extension_skill_dirs = get_extension_skill_dirs(settings.user_agents_dir)

        skills = list_skills(
            built_in_skills_dir=built_in_skills_dir,
            extension_skills_dirs=extension_skill_dirs,
            user_skills_dir=user_skills_dir,
            project_skills_dir=project_skills_dir,
            user_agent_skills_dir=user_agent_skills_dir,
            project_agent_skills_dir=project_agent_skills_dir,
        )

        if not skills:
            await self._mount_message(
                AppMessage(
                    "No skills are currently discoverable.\n\n"
                    "Use `bog-agents skills list` for full CLI management."
                )
            )
            return

        counts = {"project": 0, "user": 0, "built-in": 0, "extension": 0}
        for skill in skills:
            source = skill.get("source", "user")
            if source in counts:
                counts[source] += 1

        names = sorted(str(skill["name"]) for skill in skills)
        preview_names = ", ".join(names[:8])
        if len(names) > 8:
            preview_names += f", +{len(names) - 8} more"

        precedence_paths = [
            (".agents/skills", project_agent_skills_dir),
            (".bog-agents/skills", project_skills_dir),
            ("~/.agents/skills", user_agent_skills_dir),
            (f"~/.bog-agents/{self._assistant_id}/skills", user_skills_dir),
            *[(f"extension:{path.name}", path) for path in extension_skill_dirs],
            ("built-in", built_in_skills_dir),
        ]
        path_lines = [
            f"- {label}: {path}" for label, path in precedence_paths if path is not None
        ]

        message = "\n".join(
            [
                f"Loaded skills: {len(skills)}",
                f"Project skills: {counts['project']}",
                f"User skills: {counts['user']}",
                f"Extension skills: {counts['extension']}",
                f"Built-in skills: {counts['built-in']}",
                f"Examples: {preview_names}",
                "",
                "Search paths:",
                *path_lines,
                "",
                (
                    "Use `bog-agents skills list` for detailed metadata "
                    "and precedence debugging."
                ),
            ]
        )
        await self._mount_message(AppMessage(message))

    async def _handle_plugin_command(self, command: str) -> None:
        """Handle `/plugin` and `/extensions` for extensibility management."""
        await self._mount_message(UserMessage(command))

        from bog_agents_cli.extensibility import (
            describe_extensibility_item,
            disable_extensibility_item,
            enable_extensibility_item,
            find_extensibility_item,
            format_extensibility_list,
            install_extensibility_item,
            uninstall_extensibility_item,
        )

        config_dir = settings.user_agents_dir
        command_name = self._command_name(command)
        raw_arg = command.strip()[len(command_name) :].strip()
        lowered = raw_arg.lower()

        if not raw_arg or lowered in {"list", "status"}:
            listing = await asyncio.to_thread(format_extensibility_list, config_dir)
            message = "\n\n".join(
                [
                    listing,
                    (
                        "Usage: /plugin install <path-or-url> | /plugin info <name> | "
                        "/plugin enable <name> | /plugin disable <name> | "
                        "/plugin uninstall <name>"
                    ),
                ]
            )
            await self._mount_message(AppMessage(message))
            return

        if lowered.startswith("info "):
            name = raw_arg[5:].strip()
            if not name:
                await self._mount_message(AppMessage("Usage: /plugin info <name>"))
                return
            item = await asyncio.to_thread(find_extensibility_item, config_dir, name)
            if item is None:
                await self._mount_message(
                    AppMessage(f"Plugin or extension '{name}' not found.")
                )
                return
            await self._mount_message(AppMessage(describe_extensibility_item(item)))
            return

        if lowered.startswith("install "):
            source = raw_arg[8:].strip()
            if not source:
                await self._mount_message(
                    AppMessage("Usage: /plugin install <path-or-url>")
                )
                return
            try:
                installed = await asyncio.to_thread(
                    install_extensibility_item,
                    config_dir,
                    source,
                )
            except ValueError as exc:
                await self._mount_message(AppMessage(f"Plugin install failed: {exc}"))
                return
            self._refresh_slash_command_cache()
            message = (
                f"Installed {installed.kind} {installed.name} v{installed.version}"
            )
            await self._mount_message(AppMessage(message))
            return

        if lowered.startswith("uninstall "):
            name = raw_arg[10:].strip()
            if not name:
                await self._mount_message(AppMessage("Usage: /plugin uninstall <name>"))
                return
            removed = await asyncio.to_thread(
                uninstall_extensibility_item,
                config_dir,
                name,
            )
            if removed:
                self._refresh_slash_command_cache()
                await self._mount_message(AppMessage(f"Uninstalled '{name}'"))
            else:
                await self._mount_message(
                    AppMessage(f"Plugin or extension '{name}' not found.")
                )
            return

        if lowered.startswith("enable "):
            name = raw_arg[7:].strip()
            if not name:
                await self._mount_message(AppMessage("Usage: /plugin enable <name>"))
                return
            if await asyncio.to_thread(enable_extensibility_item, config_dir, name):
                self._refresh_slash_command_cache()
                await self._mount_message(AppMessage(f"Enabled '{name}'"))
            else:
                await self._mount_message(AppMessage(f"'{name}' was not found."))
            return

        if lowered.startswith("disable "):
            name = raw_arg[8:].strip()
            if not name:
                await self._mount_message(AppMessage("Usage: /plugin disable <name>"))
                return
            if await asyncio.to_thread(disable_extensibility_item, config_dir, name):
                self._refresh_slash_command_cache()
                await self._mount_message(AppMessage(f"Disabled '{name}'"))
            else:
                await self._mount_message(AppMessage(f"'{name}' was not found."))
            return

        await self._mount_message(
            AppMessage(
                "Usage: /plugin | /plugin info <name> | /plugin install <path-or-url> | "
                "/plugin uninstall <name> | /plugin enable <name> | "
                "/plugin disable <name>"
            )
        )

    @staticmethod
    def _format_runtime_age(started_at: float) -> str:
        """Format a short elapsed time string for status displays."""
        elapsed = max(0, int(time.time() - started_at))
        minutes, seconds = divmod(elapsed, 60)
        hours, minutes = divmod(minutes, 60)
        if hours:
            return f"{hours}h {minutes}m"
        if minutes:
            return f"{minutes}m {seconds}s"
        return f"{seconds}s"

    @staticmethod
    def _parse_preview_launch_spec(raw_spec: str) -> tuple[str, int | None]:
        """Split a preview start spec into shell command and optional port."""
        tokens = shlex.split(raw_spec)
        if not tokens:
            return "", None

        port: int | None = None
        normalized: list[str] = []
        idx = 0
        while idx < len(tokens):
            token = tokens[idx]
            if token == "--port" and idx + 1 < len(tokens):
                try:
                    port = int(tokens[idx + 1])
                except ValueError:
                    port = None
                idx += 2
                continue
            normalized.append(token)
            idx += 1

        if port is None and len(normalized) > 1 and normalized[-1].isdigit():
            port = int(normalized[-1])
            normalized = normalized[:-1]

        return shlex.join(normalized), port

    def _preview_target_candidates(self, token: str) -> list[PreviewServerRecord]:
        """Return preview servers matching an ID or port token."""
        if not token:
            return []
        normalized = token.strip().lower()
        matches: list[PreviewServerRecord] = []
        for server in self._preview_servers.values():
            if server.preview_id.lower().startswith(normalized):
                matches.append(server)
                continue
            if server.port is not None and str(server.port) == normalized:
                matches.append(server)
        return matches

    @staticmethod
    async def _stop_preview_server(server: PreviewServerRecord) -> None:
        """Terminate one tracked preview server process."""
        proc = server.process
        if proc is None or proc.returncode is not None:
            return
        with suppress(ProcessLookupError, OSError):
            proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except TimeoutError:
            with suppress(ProcessLookupError, OSError):
                proc.kill()
            with suppress(ProcessLookupError, OSError):
                await proc.wait()

    async def _handle_preview_command(self, command: str) -> None:
        """Handle `/preview` for local preview-server lifecycle management."""
        await self._mount_message(UserMessage(command))

        from bog_agents_cli.browser_cli import parse_preview_command

        raw_arg = command.strip()[len("/preview") :].strip()
        parsed = parse_preview_command(raw_arg)
        action = parsed["action"].lower()
        action_arg = parsed["command"].strip()

        finished = [
            preview_id
            for preview_id, server in self._preview_servers.items()
            if server.process is not None and server.process.returncode is not None
        ]
        for preview_id in finished:
            self._preview_servers.pop(preview_id, None)

        if action in {"", "status"}:
            if not self._preview_servers:
                await self._mount_message(AppMessage("No preview servers are running."))
                return
            lines = ["Preview servers:"]
            for server in self._preview_servers.values():
                url = server.url or "(URL unknown)"
                lines.append(
                    "  "
                    f"{server.preview_id} - {url} - "
                    f"{self._format_runtime_age(server.started_at)} - {server.command}"
                )
            await self._mount_message(AppMessage("\n".join(lines)))
            return

        if action == "start":
            launch_command, port = self._parse_preview_launch_spec(action_arg)
            if not launch_command:
                await self._mount_message(
                    AppMessage(
                        "Usage: /preview start <command> [--port N]\n"
                        "Example: /preview start uv run mkdocs serve --port 8000"
                    )
                )
                return
            proc = await asyncio.create_subprocess_shell(
                launch_command,
                cwd=str(self._cwd),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            preview_id = f"preview-{uuid.uuid4().hex[:6]}"
            url = f"http://127.0.0.1:{port}" if port is not None else None
            server = PreviewServerRecord(
                preview_id=preview_id,
                command=launch_command,
                cwd=str(self._cwd),
                port=port,
                url=url,
                process=proc,
            )
            self._preview_servers[preview_id] = server
            if url:
                with suppress(Exception):
                    webbrowser.open(url)
            lines = [
                f"Started preview server {preview_id}.",
                f"Command: {launch_command}",
                f"CWD: {self._cwd}",
            ]
            if url:
                lines.append(f"URL: {url}")
            await self._mount_message(AppMessage("\n".join(lines)))
            return

        if action == "stop":
            if not self._preview_servers:
                await self._mount_message(AppMessage("No preview servers are running."))
                return
            if action_arg.lower() in {"all", "*"} or (
                not action_arg and len(self._preview_servers) == 1
            ):
                servers = list(self._preview_servers.values())
            else:
                servers = self._preview_target_candidates(action_arg)
            if not servers:
                await self._mount_message(
                    AppMessage(
                        "Usage: /preview stop <preview-id|port>|all\n"
                        "Run `/preview` to see active preview servers."
                    )
                )
                return
            for server in servers:
                await self._stop_preview_server(server)
                self._preview_servers.pop(server.preview_id, None)
            await self._mount_message(
                AppMessage(
                    "Stopped preview server(s): "
                    + ", ".join(server.preview_id for server in servers)
                )
            )
            return

        await self._mount_message(
            AppMessage(
                "Usage: /preview | /preview status | /preview start <command> [--port N] | "
                "/preview stop <preview-id|port>|all"
            )
        )

    async def _handle_remote_command(self, command: str) -> None:
        """Handle `/remote` for remote task submission and inspection."""
        await self._mount_message(UserMessage(command))

        from bog_agents_cli.remote import (
            RemoteStatus,
            cancel_remote_task,
            check_remote_task,
            format_remote_config_summary,
            format_remote_recovery,
            format_remote_tasks,
            load_remote_config,
            submit_remote_task,
        )

        raw_arg = command.strip()[len("/remote") :].strip()
        lowered = raw_arg.lower()
        config = await asyncio.to_thread(load_remote_config, settings.user_agents_dir)

        if not raw_arg or lowered in {"list", "status"}:
            await self._refresh_remote_tasks()
            message = "\n".join(
                [
                    format_remote_config_summary(config),
                    "",
                    format_remote_tasks(list(self._remote_tasks.values())),
                    "",
                    (
                        "Usage: /remote config | /remote refresh | /remote cleanup | "
                        "/remote reattach [id|all]"
                    ),
                    (
                        "Usage: /remote submit [--label LABEL] [--model MODEL] "
                        "[--branch-prefix PREFIX] <prompt> | /remote status <id> | "
                        "/remote stop <id> | /remote recover <id>"
                    ),
                ]
            )
            await self._mount_message(AppMessage(message))
            return

        if lowered == "config":
            lines = [
                format_remote_config_summary(config),
                "",
                (f"Config file: {settings.user_agents_dir / 'remote.json'}"),
            ]
            await self._mount_message(AppMessage("\n".join(lines)))
            return

        if lowered == "refresh":
            await self._refresh_remote_tasks()
            await self._mount_message(
                AppMessage(format_remote_tasks(list(self._remote_tasks.values())))
            )
            return

        if lowered == "cleanup":
            done_ids = [
                task_id
                for task_id, task in self._remote_tasks.items()
                if task.status
                in {
                    RemoteStatus.COMPLETED,
                    RemoteStatus.FAILED,
                    RemoteStatus.CANCELLED,
                }
            ]
            removed = await self._drop_remote_tasks(done_ids)
            await self._mount_message(
                AppMessage(f"Removed {removed} completed remote task(s).")
            )
            return

        if lowered == "reattach" or lowered.startswith("reattach "):
            token = raw_arg[8:].strip()
            if not token or token.lower() == "all":
                added = await self._load_persisted_remote_tasks()
                await self._refresh_remote_tasks()
                await self._mount_message(
                    AppMessage(
                        "\n".join(
                            [
                                f"Reattached {added} persisted remote task(s).",
                                "",
                                format_remote_tasks(list(self._remote_tasks.values())),
                            ]
                        )
                    )
                )
                return
            task = await self._resolve_remote_task(token)
            if task is None:
                await self._mount_message(
                    AppMessage(f"Remote task {token} not found in persisted state.")
                )
                return
            updated = await check_remote_task(config, task)
            await self._store_remote_task(updated)
            await self._mount_message(AppMessage(format_remote_tasks([updated])))
            return

        if lowered.startswith("status "):
            task_id = raw_arg[7:].strip()
            task = await self._resolve_remote_task(task_id)
            if task is None:
                await self._mount_message(
                    AppMessage(f"Remote task {task_id} not found.")
                )
                return
            updated = await check_remote_task(config, task)
            await self._store_remote_task(updated)
            await self._mount_message(AppMessage(format_remote_tasks([updated])))
            return

        if lowered.startswith("stop "):
            task_id = raw_arg[5:].strip()
            task = await self._resolve_remote_task(task_id)
            if task is None:
                await self._mount_message(
                    AppMessage(f"Remote task {task_id} not found.")
                )
                return
            updated = await cancel_remote_task(config, task)
            await self._store_remote_task(updated)
            await self._mount_message(AppMessage(format_remote_tasks([updated])))
            return

        if lowered.startswith("recover "):
            task_id = raw_arg[8:].strip()
            task = await self._resolve_remote_task(task_id)
            if task is None:
                await self._mount_message(
                    AppMessage(f"Remote task {task_id} not found.")
                )
                return
            updated = await check_remote_task(config, task)
            await self._store_remote_task(updated)
            recovery = format_remote_recovery(updated)
            branch = str(updated.metadata.get("branch", "") or "")
            if branch:
                recovery += (
                    "\n\nLocal follow-up:\n"
                    "  If the sandbox pushed its branch to origin, recover it with:\n"
                    f"  git fetch origin {branch}\n"
                    f"  git switch {branch}"
                )
            await self._mount_message(AppMessage(recovery))
            return

        if lowered.startswith("submit "):
            raw_submit = raw_arg[7:].strip()
            try:
                tokens = shlex.split(raw_submit)
            except ValueError as exc:
                await self._mount_message(
                    AppMessage(f"Could not parse remote command: {exc}")
                )
                return

            label = ""
            model: str | None = None
            branch_prefix = ""
            idx = 0
            while idx < len(tokens) and tokens[idx].startswith("--"):
                flag = tokens[idx]
                if flag in {"--label", "--model", "--branch-prefix"}:
                    if idx + 1 >= len(tokens):
                        await self._mount_message(
                            AppMessage(f"Missing value for {flag}.")
                        )
                        return
                    value = tokens[idx + 1]
                    if flag == "--label":
                        label = value
                    elif flag == "--model":
                        model = value
                    else:
                        branch_prefix = value
                    idx += 2
                    continue
                await self._mount_message(
                    AppMessage(
                        "Usage: /remote submit [--label LABEL] [--model MODEL] "
                        "[--branch-prefix PREFIX] <prompt>"
                    )
                )
                return

            prompt = " ".join(tokens[idx:]).strip()
            if not prompt:
                await self._mount_message(
                    AppMessage(
                        "Usage: /remote submit [--label LABEL] [--model MODEL] "
                        "[--branch-prefix PREFIX] <prompt>"
                    )
                )
                return
            task = await submit_remote_task(
                config,
                prompt,
                model=model or self._model_override or settings.model_name or "",
                label=label,
                working_dir=Path(self._cwd),
                assistant_id=self._assistant_id,
                branch_prefix=branch_prefix,
            )
            await self._store_remote_task(task)
            await self._mount_message(AppMessage(format_remote_tasks([task])))
            return

        await self._mount_message(
            AppMessage(
                "Usage: /remote | /remote config | /remote refresh | "
                "/remote cleanup | /remote reattach [id|all] | "
                "/remote submit [--label LABEL] [--model MODEL] "
                "[--branch-prefix PREFIX] <prompt> | /remote status <id> | "
                "/remote stop <id> | /remote recover <id>"
            )
        )

    async def _handle_worktree_command(self, command: str) -> None:
        """Handle `/worktree` git worktree management."""
        await self._mount_message(UserMessage(command))

        from bog_agents.middleware.worktree import (
            create_worktree,
            list_worktrees,
            remove_worktree,
        )

        repo_root = await self._get_repo_root()
        if repo_root is None:
            await self._mount_message(
                AppMessage("`/worktree` requires a git repository.")
            )
            return

        raw_arg = command.strip()[len("/worktree") :].strip()
        lowered = raw_arg.lower()

        if not raw_arg or lowered in {"list", "status"}:
            worktrees = await asyncio.to_thread(list_worktrees, repo_root)
            if not worktrees:
                await self._mount_message(AppMessage("No git worktrees found."))
                return
            lines = [
                "Git worktrees:",
                f"Repository: {repo_root}",
                f"Current cwd: {self._cwd}",
                "",
            ]
            for worktree in worktrees:
                marker = " (main)" if worktree.is_main else ""
                commit = f" @ {worktree.commit[:8]}" if worktree.commit else ""
                lines.append(f"  {worktree.branch}{marker}: {worktree.path}{commit}")
            lines.append("")
            lines.append("Usage: /worktree create <branch> | /worktree status <branch>")
            lines.append(
                "Usage: /worktree merge <source-branch> [target-branch] | "
                "/worktree remove <branch>"
            )
            await self._mount_message(AppMessage("\n".join(lines)))
            return

        if lowered.startswith("create "):
            branch = raw_arg[7:].strip()
            if not branch:
                await self._mount_message(
                    AppMessage("Usage: /worktree create <branch>")
                )
                return
            worktree = await asyncio.to_thread(create_worktree, repo_root, branch)
            await self._mount_message(
                AppMessage(
                    f"Created worktree on branch {worktree.branch}\n"
                    f"Path: {worktree.path}"
                )
            )
            return

        if lowered.startswith("status "):
            branch = raw_arg[7:].strip()
            if not branch:
                await self._mount_message(
                    AppMessage("Usage: /worktree status <branch>")
                )
                return
            worktrees = await asyncio.to_thread(list_worktrees, repo_root)
            target = next((wt for wt in worktrees if wt.branch == branch), None)
            if target is None:
                await self._mount_message(
                    AppMessage(f"Worktree for branch '{branch}' was not found.")
                )
                return
            lines = [
                f"Branch: {target.branch}",
                f"Path: {target.path}",
                f"Commit: {target.commit or '(unknown)'}",
                f"Main worktree: {'yes' if target.is_main else 'no'}",
            ]
            await self._mount_message(AppMessage("\n".join(lines)))
            return

        if lowered.startswith("merge "):
            args = raw_arg[6:].strip().split()
            if not args:
                await self._mount_message(
                    AppMessage("Usage: /worktree merge <source-branch> [target-branch]")
                )
                return
            source_branch = args[0]
            target_branch = args[1] if len(args) > 1 else "main"

            ok_checkout, checkout_output = await self._run_git(
                ["checkout", target_branch],
                cwd=repo_root,
            )
            if not ok_checkout:
                await self._mount_message(
                    AppMessage(
                        f"Failed to checkout {target_branch} before merge:\n{checkout_output}"
                    )
                )
                return

            ok_merge, merge_output = await self._run_git(
                ["merge", source_branch],
                cwd=repo_root,
            )
            status = (
                "Merge complete." if ok_merge else "Merge reported conflicts/errors."
            )
            await self._mount_message(
                AppMessage(
                    "\n".join(
                        [
                            f"Merged {source_branch} into {target_branch}.",
                            status,
                            merge_output or "(no git output)",
                        ]
                    )
                )
            )
            return

        if lowered.startswith("remove "):
            branch = raw_arg[7:].strip()
            if not branch:
                await self._mount_message(
                    AppMessage("Usage: /worktree remove <branch>")
                )
                return
            worktrees = await asyncio.to_thread(list_worktrees, repo_root)
            target = next((wt for wt in worktrees if wt.branch == branch), None)
            if target is None or target.is_main:
                await self._mount_message(
                    AppMessage(
                        f"Could not find removable worktree for branch '{branch}'."
                    )
                )
                return
            result = await asyncio.to_thread(remove_worktree, repo_root, target.path)
            await self._mount_message(AppMessage(result))
            return

        await self._mount_message(
            AppMessage(
                "Usage: /worktree | /worktree create <branch> | "
                "/worktree status <branch> | /worktree merge <source> [target] | "
                "/worktree remove <branch>"
            )
        )

    async def _handle_think_command(self, command: str) -> None:
        """Handle `/think` extended thinking mode toggle.

        Subcommands:
            /think            — Show current status
            /think on         — Enable thinking
            /think off        — Disable thinking
            /think toggle     — Toggle on/off
            /think budget N   — Set budget_tokens to N (int, e.g. 8000)

        Args:
            command: Full command string.
        """
        await self._mount_message(UserMessage(command))

        from bog_agents.middleware.thinking import ThinkingMiddleware

        mw = next(
            (m for m in getattr(self, "_middleware", []) if isinstance(m, ThinkingMiddleware)),
            None,
        )
        if mw is None:
            await self._mount_message(
                AppMessage(
                    "ThinkingMiddleware is not active in this session.\n"
                    "Add `ThinkingMiddleware()` to your middleware stack to enable."
                )
            )
            return

        raw_arg = command.strip()[len("/think"):].strip().lower()

        if not raw_arg or raw_arg == "status":
            state = "enabled" if mw.is_enabled else "disabled"
            await self._mount_message(
                AppMessage(
                    f"Extended thinking: {state}\n"
                    f"Budget tokens: {mw.budget_tokens:,}\n\n"
                    "Usage: /think on | /think off | /think toggle | /think budget <N>"
                )
            )
            return

        if raw_arg == "on":
            mw.set_thinking(True)
            await self._mount_message(
                AppMessage(f"Extended thinking enabled (budget: {mw.budget_tokens:,} tokens).")
            )
            return

        if raw_arg == "off":
            mw.set_thinking(False)
            await self._mount_message(AppMessage("Extended thinking disabled."))
            return

        if raw_arg == "toggle":
            enabled = mw.toggle()
            state = "enabled" if enabled else "disabled"
            await self._mount_message(AppMessage(f"Extended thinking {state}."))
            return

        if raw_arg.startswith("budget "):
            budget_str = raw_arg[7:].strip()
            try:
                budget = int(budget_str)
                if budget < 1024:
                    await self._mount_message(AppMessage("Budget must be at least 1024 tokens."))
                    return
                mw.set_thinking(mw.is_enabled, budget_tokens=budget)
                await self._mount_message(
                    AppMessage(f"Thinking budget set to {budget:,} tokens.")
                )
            except ValueError:
                await self._mount_message(
                    AppMessage(f"Invalid budget value: {budget_str!r}. Usage: /think budget <N>")
                )
            return

        await self._mount_message(
            AppMessage("Usage: /think | /think on | /think off | /think toggle | /think budget <N>")
        )

    async def _handle_rules_command(self, command: str) -> None:
        """Handle `/rules` project rules management.

        Subcommands:
            /rules              — List all rules
            /rules list         — List all rules
            /rules show <name>  — Show rule content
            /rules test <file>  — Test which rules match a file
            /rules add <name>   — Create a new rule file template
            /rules edit <name>  — Show path to edit a rule

        Args:
            command: Full command string.
        """
        await self._mount_message(UserMessage(command))

        from bog_agents.middleware.rules import (
            apply_rules,
            create_rule_file,
            load_rules,
        )

        project_root = Path(self._cwd)
        raw_arg = command.strip()[len("/rules"):].strip()
        lowered = raw_arg.lower()

        if not raw_arg or lowered == "list":
            rules = await asyncio.to_thread(load_rules, project_root)
            if not rules:
                await self._mount_message(
                    AppMessage(
                        "No rules found in .bog-agents/rules/\n\n"
                        "Create one with: /rules add <name>"
                    )
                )
                return
            lines = [f"Project rules ({len(rules)} total):", ""]
            for r in rules:
                glob_str = ", ".join(r.glob) if r.glob else "(no glob)"
                always_str = " [always]" if r.always else ""
                agent_str = f" [agent:{r.agent}]" if r.agent else ""
                lines.append(
                    f"  {r.name} (priority {r.priority}){always_str}{agent_str} — {glob_str}"
                )
            lines.append("")
            lines.append("Use /rules show <name> to view content.")
            await self._mount_message(AppMessage("\n".join(lines)))
            return

        if lowered.startswith("show "):
            name = raw_arg[5:].strip()
            rules = await asyncio.to_thread(load_rules, project_root)
            match = next((r for r in rules if r.name == name or r.name == name.removesuffix(".md")), None)
            if match is None:
                await self._mount_message(AppMessage(f"Rule '{name}' not found."))
                return
            await self._mount_message(
                AppMessage(
                    f"Rule: {match.name}\n"
                    f"File: {match.path}\n"
                    f"Priority: {match.priority} | Always: {match.always} | Agent: {match.agent or '(any)'}\n"
                    f"Glob: {', '.join(match.glob) or '(none)'}\n\n"
                    f"{match.content}"
                )
            )
            return

        if lowered.startswith("test "):
            file_arg = raw_arg[5:].strip()
            context_files = [file_arg] if file_arg else []
            rules = await asyncio.to_thread(load_rules, project_root)
            matching = [r for r in rules if r.matches(context_files, agent_type="")]
            if not matching:
                await self._mount_message(
                    AppMessage(f"No rules match '{file_arg}'.")
                )
                return
            lines = [f"Rules matching '{file_arg}':", ""]
            for r in matching:
                lines.append(f"  {r.name} (priority {r.priority})")
            lines.append("")
            preview = await asyncio.to_thread(apply_rules, rules, context_files, "")
            if preview:
                lines.append("Injected content preview (first 500 chars):")
                lines.append(preview[:500])
            await self._mount_message(AppMessage("\n".join(lines)))
            return

        if lowered.startswith("add "):
            name = raw_arg[4:].strip()
            if not name:
                await self._mount_message(AppMessage("Usage: /rules add <rule-name>"))
                return
            rule_path = await asyncio.to_thread(
                create_rule_file,
                project_root,
                name,
                f"# {name}\n\nDescribe the rule here.",
            )
            await self._mount_message(
                AppMessage(
                    f"Created rule: {rule_path}\n\n"
                    "Edit it to add frontmatter and content:\n"
                    "---\n"
                    "glob: ['**/*.py']\n"
                    "priority: 50\n"
                    "---\n"
                    "Rule content here."
                )
            )
            return

        if lowered.startswith("edit "):
            name = raw_arg[5:].strip()
            rules = await asyncio.to_thread(load_rules, project_root)
            match = next((r for r in rules if r.name == name or r.name == name.removesuffix(".md")), None)
            if match is None:
                await self._mount_message(AppMessage(f"Rule '{name}' not found. Use /rules add <name> to create it."))
                return
            await self._mount_message(
                AppMessage(f"Edit rule at: {match.path}")
            )
            return

        await self._mount_message(
            AppMessage(
                "Usage: /rules | /rules list | /rules show <name> | "
                "/rules test <file> | /rules add <name> | /rules edit <name>"
            )
        )

    async def _handle_search_command(self, command: str) -> None:
        """Handle `/search` hybrid codebase search.

        Subcommands:
            /search <query>         — Hybrid search (ripgrep + fuzzy filename)
            /search index           — Build/rebuild embedding index
            /search index --force   — Force full rebuild of index

        Args:
            command: Full command string.
        """
        await self._mount_message(UserMessage(command))

        from bog_agents.middleware.hybrid_search import (
            HybridSearchMiddleware,
            format_search_results,
            hybrid_search,
        )

        raw_arg = command.strip()[len("/search"):].strip()
        if not raw_arg:
            await self._mount_message(
                AppMessage("Usage: /search <query> | /search index [--force]")
            )
            return

        lowered = raw_arg.lower()

        if lowered in {"index", "index --force"}:
            force = "--force" in lowered
            mw = next(
                (m for m in getattr(self, "_middleware", []) if isinstance(m, HybridSearchMiddleware)),
                None,
            )
            if mw is None:
                await self._mount_message(
                    AppMessage(
                        "HybridSearchMiddleware is not active.\n"
                        "Add it to your middleware stack to use semantic indexing."
                    )
                )
                return
            await self._mount_message(AppMessage("Building embedding index… this may take a minute."))
            try:
                result = await asyncio.to_thread(mw._rebuild_index, force=force)
                await self._mount_message(AppMessage(f"Index built: {result}"))
            except Exception as exc:
                await self._mount_message(AppMessage(f"Index build failed: {exc}"))
            return

        try:
            results = await asyncio.to_thread(
                hybrid_search,
                raw_arg,
                Path(self._cwd),
                max_results=20,
                use_semantic=False,
            )
        except Exception as exc:
            await self._mount_message(AppMessage(f"Search error: {exc}"))
            return

        if not results:
            await self._mount_message(AppMessage(f"No results for `{raw_arg}`."))
            return

        formatted = format_search_results(results)
        await self._mount_message(
            AppMessage(f"Search results for `{raw_arg}` ({len(results)} matches):\n\n{formatted}")
        )

    async def _handle_worktrees_command(self, command: str) -> None:
        """Handle `/worktrees` parallel multi-agent worktree management.

        Subcommands:
            /worktrees              — Show status of all parallel tasks
            /worktrees status       — Show status of all parallel tasks
            /worktrees spawn <JSON> — Spawn parallel tasks (JSON array of {label, prompt})
            /worktrees merge <id>   — Merge a completed task's branch
            /worktrees cancel <id>  — Cancel a running task

        Args:
            command: Full command string.
        """
        await self._mount_message(UserMessage(command))

        from bog_agents.middleware.worktree import (
            ParallelWorktreeMiddleware,
            format_worktree_status,
        )

        mw = next(
            (m for m in getattr(self, "_middleware", []) if isinstance(m, ParallelWorktreeMiddleware)),
            None,
        )
        if mw is None:
            await self._mount_message(
                AppMessage(
                    "ParallelWorktreeMiddleware is not active in this session.\n"
                    "Add `ParallelWorktreeMiddleware(agent_factory=...)` to your middleware stack."
                )
            )
            return

        raw_arg = command.strip()[len("/worktrees"):].strip()
        lowered = raw_arg.lower()

        if not raw_arg or lowered == "status":
            tasks = mw.get_tasks()
            if not tasks:
                await self._mount_message(
                    AppMessage("No parallel worktree tasks running.\n\nUse /worktrees spawn to start tasks.")
                )
                return
            await self._mount_message(AppMessage(format_worktree_status(tasks)))
            return

        if lowered.startswith("cancel "):
            task_id = raw_arg[7:].strip()
            task = mw.get_task(task_id)
            if task is None:
                await self._mount_message(AppMessage(f"Task '{task_id}' not found."))
                return
            if task.status not in ("pending", "running"):
                await self._mount_message(
                    AppMessage(f"Task '{task_id}' is already {task.status}.")
                )
                return
            task.status = "cancelled"
            await self._mount_message(AppMessage(f"Task '{task_id}' cancelled."))
            return

        if lowered.startswith("merge "):
            import json as _json

            task_id = raw_arg[6:].strip()
            task = mw.get_task(task_id)
            if task is None:
                await self._mount_message(AppMessage(f"Task '{task_id}' not found."))
                return
            if task.status != "done":
                await self._mount_message(
                    AppMessage(f"Task '{task_id}' is {task.status}, not done. Cannot merge yet.")
                )
                return
            from bog_agents.middleware.worktree import merge_with_conflict_report

            repo_root = await self._get_repo_root()
            if repo_root is None:
                await self._mount_message(AppMessage("Not in a git repository."))
                return
            report = await asyncio.to_thread(
                merge_with_conflict_report,
                repo_root,
                task.branch,
                "main",
                False,
            )
            conflicts = report.get("conflicts", [])
            merged = report.get("merged", False)
            status_str = "Merged successfully." if merged else f"Conflicts detected in: {', '.join(conflicts)}"
            await self._mount_message(
                AppMessage(f"Merge task '{task_id}' ({task.branch}):\n{status_str}")
            )
            return

        if lowered.startswith("spawn "):
            import json as _json

            json_str = raw_arg[6:].strip()
            try:
                tasks_input = _json.loads(json_str)
            except _json.JSONDecodeError as exc:
                await self._mount_message(
                    AppMessage(
                        f"Invalid JSON: {exc}\n\n"
                        'Usage: /worktrees spawn [{"label": "task1", "prompt": "do X"}, ...]'
                    )
                )
                return
            if not isinstance(tasks_input, list):
                await self._mount_message(AppMessage("Input must be a JSON array of task objects."))
                return

            repo_root = await self._get_repo_root()
            if repo_root is None:
                await self._mount_message(AppMessage("Not in a git repository."))
                return

            task_ids = []
            for item in tasks_input:
                label = item.get("label", "task")
                prompt = item.get("prompt", "")
                task = await mw._create_task(label=label, prompt=prompt, repo_root=repo_root)
                task_ids.append(task.task_id)
                asyncio.create_task(mw._run_task_in_worktree(task))  # noqa: RUF006

            await self._mount_message(
                AppMessage(
                    f"Spawned {len(task_ids)} parallel task(s).\n"
                    f"Task IDs: {', '.join(task_ids)}\n\n"
                    "Use /worktrees status to monitor progress."
                )
            )
            return

        await self._mount_message(
            AppMessage(
                "Usage: /worktrees | /worktrees status | "
                '/worktrees spawn [{"label":"...", "prompt":"..."}] | '
                "/worktrees merge <id> | /worktrees cancel <id>"
            )
        )

    async def _handle_command(self, command: str) -> None:
        """Handle a slash command.

        Args:
            command: The slash command (including /)
        """
        handler = self._resolve_command_handler(self._command_name(command))
        if handler is None:
            await self._handle_unknown_command(command)
        else:
            await handler(command)

        # Scroll to bottom after command output is rendered.
        # Use call_after_refresh so the layout pass completes first;
        # otherwise max_scroll_y is still stale.
        def _scroll_after_command() -> None:
            try:
                chat = self.query_one("#chat", VerticalScroll)
                if chat.max_scroll_y > 0:
                    chat.scroll_end(animate=False)
            except NoMatches:
                pass

        self.call_after_refresh(_scroll_after_command)

    async def _handle_background_command(self, command: str) -> None:
        """Handle /background slash command.

        Subcommands:
            /background <prompt>   — Submit a new background task
            /background list       — Show all tasks
            /background status <id> — Show task detail
            /background cancel <id> — Cancel a running task
            /background cleanup    — Remove finished tasks

        Args:
            command: Full command string.
        """
        await self._ensure_background_manager()

        raw = command.strip()
        if raw.lower() in ("/background", "/background list"):
            await self._mount_message(
                AppMessage(self._bg_manager.format_status_table())
            )
            return

        if raw.lower().startswith("/background cancel "):
            task_id = raw.split()[-1]
            if self._bg_manager.cancel(task_id):
                await self._mount_message(AppMessage(f"Cancel requested for {task_id}"))
            else:
                await self._mount_message(
                    AppMessage(f"Task {task_id} not found or not running")
                )
            return

        if raw.lower().startswith("/background status "):
            task_id = raw.split()[-1]
            task = self._bg_manager.get_status(task_id)
            if task:
                await self._mount_message(
                    AppMessage(self._format_background_task_detail(task))
                )
            else:
                await self._mount_message(AppMessage(f"Task {task_id} not found"))
            return

        if raw.lower() == "/background cleanup":
            removed = self._bg_manager.cleanup_completed()
            await self._mount_message(AppMessage(f"Removed {removed} completed tasks"))
            return

        # Anything else is a prompt to submit
        prompt = raw[len("/background ") :].strip()
        if not prompt:
            await self._mount_message(
                AppMessage(
                    "Usage: /background <prompt> | list | cancel <id> | status <id> | cleanup"
                )
            )
            return

        try:
            team_name = self._active_team()
            effective_prompt, team_brief = self._build_team_effective_prompt(
                prompt,
                team_name,
            )
            metadata: dict[str, Any] = {"command": "/background"}
            if team_name:
                metadata["team_name"] = team_name
            if team_brief:
                metadata["team_brief"] = team_brief
            metadata["effective_prompt"] = effective_prompt
            task_id = await self._submit_managed_local_task(
                prompt,
                label=self._build_agent_task_label(prompt),
                model=self._model_override,
                working_dir=str(self._cwd),
                strategy="background",
                metadata=metadata,
            )
            team_line = f"Team: {team_name}\n" if team_name else ""
            await self._mount_message(
                AppMessage(
                    f"Background task submitted: {task_id}\n"
                    f"{team_line}"
                    "Use /background list to check status."
                )
            )
        except RuntimeError as exc:
            await self._mount_message(AppMessage(f"Error: {exc}"))

    async def _notify_background_complete(self, task: object) -> None:
        """Show a notification when a background task finishes.

        Args:
            task: The completed BackgroundTask.
        """
        from bog_agents_cli.background_agents import BackgroundStatus

        status = getattr(task, "status", "unknown")
        task_id = getattr(task, "task_id", "?")

        if status == BackgroundStatus.COMPLETED:
            result_preview = getattr(task, "result", "") or ""
            if len(result_preview) > 200:
                result_preview = result_preview[:200] + "..."
            await self._mount_message(
                AppMessage(f"Background task {task_id} completed.\n{result_preview}")
            )
        elif status == BackgroundStatus.FAILED:
            error = getattr(task, "error", "unknown error")
            await self._mount_message(
                AppMessage(f"Background task {task_id} failed: {error}")
            )

    async def _handle_dashboard_command(self) -> None:
        """Handle /dashboard slash command.

        Shows a live-updating dashboard of all agents. Use /dashboard again
        or /dashboard stop to stop refreshing.
        """
        from bog_agents_cli.dashboard import DashboardScreen

        # Toggle off if already running
        if hasattr(self, "_dashboard_screen") and self._dashboard_screen.is_running:
            self._dashboard_screen.stop()
            await self._mount_message(AppMessage("Dashboard stopped."))
            return

        def _build_state():
            from bog_agents_cli.dashboard import DashboardState
            from bog_agents_cli.team_orchestration import load_team_registry

            state = DashboardState()
            registry = load_team_registry(Path(self._cwd))
            for team in registry.teams:
                if team.summary:
                    state.team_summaries[team.name] = team.summary

            # Main agent
            main = state.add_agent("main", "Primary Agent")
            main.status = "running"
            if active_team := self._active_team():
                main.team_name = active_team
            if self._token_tracker:
                main.tokens_used = self._token_tracker.current_context

            # Background agents
            if hasattr(self, "_bg_manager"):
                for task in self._bg_manager.all_tasks:
                    panel = state.add_agent(task.task_id, f"BG: {task.prompt[:30]}")
                    panel.status = task.status.value
                    panel.started_at = task.started_at
                    panel.completed_at = task.completed_at
                    panel.team_name = self._task_team_name(task) or ""
                    panel.inbox_count = self._task_inbox_count(task)
                    if task.error:
                        panel.errors = 1

            for task in self._remote_tasks.values():
                panel = state.add_agent(
                    task.task_id, f"RM: {task.label or task.prompt[:30]}"
                )
                panel.status = str(task.status)
                panel.team_name = self._task_team_name(task) or ""
                panel.inbox_count = self._task_inbox_count(task)
                if task.error:
                    panel.errors = 1
                if task.output:
                    panel.add_output(task.output)

            return state

        self._dashboard_screen = DashboardScreen(
            state_builder=_build_state,
            interval=3.0,
        )

        output = self._dashboard_screen.render_once()
        await self._mount_message(
            AppMessage(
                f"{output}\n\nDashboard showing snapshot. Use /dashboard again to refresh."
            )
        )

    async def _handle_team_command(self, command: str) -> None:
        """Handle `/team` coordination and shared-memory workflows."""
        await self._mount_message(UserMessage(command))

        from bog_agents_cli.team_config import (
            TeamSharedConfig,
            add_member as team_add_member,
            format_setup_guide,
            format_team_status,
            get_named_prompt,
            get_shared_context_text,
            init_team_directory,
            load_team_config,
            load_user_identity,
            remove_member as team_remove_member,
            save_team_config,
            save_user_identity,
        )
        from bog_agents_cli.team_orchestration import (
            add_team_member,
            append_team_message,
            ensure_team,
            find_team,
            format_team_profile,
            remove_team_member,
            set_active_team,
            set_team_summary,
            summarize_team_activity,
        )

        raw_arg = command.strip()[len("/team") :].strip()
        try:
            tokens = shlex.split(raw_arg)
        except ValueError as exc:
            await self._mount_message(
                AppMessage(f"Could not parse /team arguments: {exc}")
            )
            return

        project_root = Path(self._cwd)
        action = tokens[0].lower() if tokens else "status"

        # -----------------------------------------------------------------
        # Developer team shared config subcommands
        # -----------------------------------------------------------------

        if action in {"setup", "guide", "help"}:
            await self._mount_message(AppMessage(format_setup_guide()))
            return

        if action == "status" and len(tokens) <= 1:
            cfg = load_team_config(project_root)
            if cfg is None:
                await self._mount_message(
                    AppMessage(
                        "No team config found in this project.\n\n"
                        "Run `/team init <name>` to create one, or "
                        "`/team setup` for the full setup guide."
                    )
                )
            else:
                identity = load_user_identity()
                await self._mount_message(AppMessage(format_team_status(cfg, project_root, identity)))
            return

        if action == "init":
            team_name = " ".join(tokens[1:]).strip() if len(tokens) > 1 else ""
            if not team_name:
                await self._mount_message(AppMessage("Usage: /team init <team-name>"))
                return
            cfg = load_team_config(project_root)
            if cfg is None:
                cfg = TeamSharedConfig(name=team_name)
                cfg.context.always_include = ["context/team-overview.md"]
            else:
                cfg.name = team_name
            created_files = await asyncio.to_thread(init_team_directory, project_root, team_name)
            await asyncio.to_thread(save_team_config, cfg, project_root)
            file_list = "\n".join(f"  {f.relative_to(project_root)}" for f in created_files)
            await self._mount_message(
                AppMessage(
                    f"Team '{team_name}' initialized.\n\n"
                    f"Created:\n"
                    f"  .bog-agents/team/config.json\n"
                    f"{file_list}\n\n"
                    "Next steps:\n"
                    "  1. Run `/team whoami set <name> <email>` to set your identity\n"
                    "  2. Run `/team invite <email> [role] [name]` to add team members\n"
                    "  3. Edit .bog-agents/team/context/team-overview.md with your team's context\n"
                    "  4. Run `git add .bog-agents/team/ && git commit -m 'chore: add team config'`\n\n"
                    "Run `/team setup` for the full guide."
                )
            )
            return

        if action == "whoami":
            identity = load_user_identity()
            sub = tokens[1].lower() if len(tokens) > 1 else "show"
            if sub in {"show", "status"}:
                if identity.name or identity.email:
                    await self._mount_message(
                        AppMessage(f"Identity: {identity.name} <{identity.email}> ({identity.role})")
                    )
                else:
                    await self._mount_message(
                        AppMessage(
                            "Identity not set.\n"
                            "Run: /team whoami set <name> <email>"
                        )
                    )
            elif sub == "set":
                if len(tokens) < 4:
                    await self._mount_message(
                        AppMessage("Usage: /team whoami set <name> <email>")
                    )
                    return
                identity.name = tokens[2]
                identity.email = tokens[3]
                await asyncio.to_thread(save_user_identity, identity)
                await self._mount_message(
                    AppMessage(f"Identity saved: {identity.name} <{identity.email}>")
                )
            else:
                await self._mount_message(
                    AppMessage("Usage: /team whoami | /team whoami set <name> <email>")
                )
            return

        if action == "invite":
            cfg = load_team_config(project_root)
            if cfg is None:
                await self._mount_message(
                    AppMessage("No team config found. Run `/team init <name>` first.")
                )
                return
            if len(tokens) < 2:
                await self._mount_message(
                    AppMessage("Usage: /team invite <email> [role] [name]")
                )
                return
            email = tokens[1]
            role = tokens[2] if len(tokens) > 2 else "member"
            name = " ".join(tokens[3:]) if len(tokens) > 3 else email.split("@")[0]
            member = team_add_member(cfg, name, email, role)
            await asyncio.to_thread(save_team_config, cfg, project_root)
            await self._mount_message(
                AppMessage(
                    f"Invited: {member.name} <{member.email}> [{member.role}]\n\n"
                    "Share this with them:\n"
                    "  1. Clone/pull the repository\n"
                    "  2. Run: bog invite --accept\n"
                    "  Or they can run: /team whoami set <name> <email>"
                )
            )
            return

        if action == "members":
            cfg = load_team_config(project_root)
            if cfg is None:
                await self._mount_message(AppMessage("No team config found."))
                return
            if not cfg.members:
                await self._mount_message(
                    AppMessage("No members. Run `/team invite <email>` to add the first member.")
                )
                return
            lines = [f"Members of '{cfg.name}' ({len(cfg.members)}):"]
            for m in cfg.members:
                lines.append(f"  • {m.name} <{m.email}> [{m.role}]")
            await self._mount_message(AppMessage("\n".join(lines)))
            return

        if action in {"remove-member", "kick"}:
            cfg = load_team_config(project_root)
            if cfg is None:
                await self._mount_message(AppMessage("No team config found."))
                return
            if len(tokens) < 2:
                await self._mount_message(AppMessage("Usage: /team remove-member <email-or-name>"))
                return
            removed = team_remove_member(cfg, tokens[1])
            if removed:
                await asyncio.to_thread(save_team_config, cfg, project_root)
                await self._mount_message(AppMessage(f"Removed '{tokens[1]}' from team."))
            else:
                await self._mount_message(AppMessage(f"Member '{tokens[1]}' not found."))
            return

        if action == "context":
            cfg = load_team_config(project_root)
            if cfg is None:
                await self._mount_message(AppMessage("No team config found. Run `/team init <name>` first."))
                return
            sub = tokens[1].lower() if len(tokens) > 1 else "list"
            if sub == "list":
                if not cfg.context.always_include:
                    await self._mount_message(
                        AppMessage("No shared context files configured.\nRun: /team context add <file>")
                    )
                else:
                    lines = ["Shared context (auto-injected):"]
                    for f in cfg.context.always_include:
                        lines.append(f"  • {f}")
                    await self._mount_message(AppMessage("\n".join(lines)))
            elif sub == "add":
                if len(tokens) < 3:
                    await self._mount_message(AppMessage("Usage: /team context add <file>"))
                    return
                rel = tokens[2]
                if rel not in cfg.context.always_include:
                    cfg.context.always_include.append(rel)
                    await asyncio.to_thread(save_team_config, cfg, project_root)
                    await self._mount_message(AppMessage(f"Added '{rel}' to shared context."))
                else:
                    await self._mount_message(AppMessage(f"'{rel}' is already in shared context."))
            elif sub == "remove":
                if len(tokens) < 3:
                    await self._mount_message(AppMessage("Usage: /team context remove <file>"))
                    return
                rel = tokens[2]
                if rel in cfg.context.always_include:
                    cfg.context.always_include.remove(rel)
                    await asyncio.to_thread(save_team_config, cfg, project_root)
                    await self._mount_message(AppMessage(f"Removed '{rel}' from shared context."))
                else:
                    await self._mount_message(AppMessage(f"'{rel}' not in shared context."))
            elif sub == "show":
                text = await asyncio.to_thread(get_shared_context_text, cfg, project_root)
                await self._mount_message(AppMessage(text or "No context content found."))
            else:
                await self._mount_message(
                    AppMessage("Usage: /team context [list|add <file>|remove <file>|show]")
                )
            return

        if action == "prompt":
            cfg = load_team_config(project_root)
            if cfg is None:
                await self._mount_message(AppMessage("No team config found. Run `/team init <name>` first."))
                return
            sub = tokens[1].lower() if len(tokens) > 1 else "list"
            if sub == "list":
                prompts_dir = Path(self._cwd) / ".bog-agents" / "team" / "prompts"
                file_prompts = [p.stem for p in prompts_dir.glob("*.md")] if prompts_dir.is_dir() else []
                all_names = list(cfg.prompts) + [n for n in file_prompts if n not in cfg.prompts]
                if not all_names:
                    await self._mount_message(
                        AppMessage("No shared prompts. Run: /team prompt add <name> <text>")
                    )
                else:
                    lines = ["Shared prompts:"]
                    for n in all_names:
                        lines.append(f"  • {n}")
                    await self._mount_message(AppMessage("\n".join(lines)))
            elif sub in {"show", "get"}:
                if len(tokens) < 3:
                    await self._mount_message(AppMessage("Usage: /team prompt show <name>"))
                    return
                text = get_named_prompt(cfg, tokens[2], project_root)
                await self._mount_message(AppMessage(text or f"Prompt '{tokens[2]}' not found."))
            elif sub == "add":
                if len(tokens) < 4:
                    await self._mount_message(AppMessage("Usage: /team prompt add <name> <text>"))
                    return
                name = tokens[2]
                text = raw_arg.split(None, 3)[3].strip()
                cfg.prompts[name] = text
                await asyncio.to_thread(save_team_config, cfg, project_root)
                await self._mount_message(AppMessage(f"Saved prompt '{name}'."))
            elif sub == "run":
                if len(tokens) < 3:
                    await self._mount_message(AppMessage("Usage: /team prompt run <name>"))
                    return
                text = get_named_prompt(cfg, tokens[2], project_root)
                if not text:
                    await self._mount_message(AppMessage(f"Prompt '{tokens[2]}' not found."))
                    return
                await self._handle_user_message(text)
            else:
                await self._mount_message(
                    AppMessage("Usage: /team prompt [list|show <name>|add <name> <text>|run <name>]")
                )
            return

        if action == "var":
            cfg = load_team_config(project_root)
            if cfg is None:
                await self._mount_message(AppMessage("No team config found. Run `/team init <name>` first."))
                return
            sub = tokens[1].lower() if len(tokens) > 1 else "list"
            if sub == "list":
                if not cfg.vars:
                    await self._mount_message(
                        AppMessage("No shared vars. Run: /team var set <key> <value>")
                    )
                else:
                    lines = ["Shared vars (non-secret):"]
                    for k, v in cfg.vars.items():
                        lines.append(f"  {k}={v}")
                    await self._mount_message(AppMessage("\n".join(lines)))
            elif sub == "set":
                if len(tokens) < 4:
                    await self._mount_message(AppMessage("Usage: /team var set <key> <value>"))
                    return
                cfg.vars[tokens[2]] = " ".join(tokens[3:])
                await asyncio.to_thread(save_team_config, cfg, project_root)
                await self._mount_message(AppMessage(f"Set {tokens[2]}={cfg.vars[tokens[2]]}"))
            elif sub in {"unset", "remove", "delete"}:
                if len(tokens) < 3:
                    await self._mount_message(AppMessage("Usage: /team var unset <key>"))
                    return
                cfg.vars.pop(tokens[2], None)
                await asyncio.to_thread(save_team_config, cfg, project_root)
                await self._mount_message(AppMessage(f"Removed var '{tokens[2]}'."))
            else:
                await self._mount_message(
                    AppMessage("Usage: /team var [list|set <key> <value>|unset <key>]")
                )
            return

        # -----------------------------------------------------------------
        # Multi-agent orchestration subcommands (existing behavior)
        # -----------------------------------------------------------------

        registry = self._load_team_registry()

        if action in {"list", "show"} and len(tokens) <= 1:
            if not registry.teams:
                await self._mount_message(
                    AppMessage(
                        "No teams configured yet.\n\n"
                        "Usage: /team create <name> | /team use <name>"
                    )
                )
                return
            lines = [f"Active team: {registry.active_team or '(none)'}", ""]
            for team in registry.teams:
                local_tasks, remote_tasks = self._team_task_snapshot(team.name)
                inbox_count = sum(
                    self._task_inbox_count(task)
                    for task in [*local_tasks, *remote_tasks]
                )
                lines.append(
                    format_team_profile(
                        team,
                        active=registry.active_team.lower() == team.name.lower()
                        if registry.active_team
                        else False,
                        local_tasks=len(local_tasks),
                        remote_tasks=len(remote_tasks),
                        inbox_count=inbox_count,
                    )
                )
                lines.append("")
            lines.append(
                "Usage: /team create <name> | /team use <name> | "
                "/team status <name> | /team message <team|task-id> <text>"
            )
            await self._mount_message(AppMessage("\n".join(lines).strip()))
            return

        if action == "create":
            if len(tokens) < 2:
                await self._mount_message(AppMessage("Usage: /team create <name>"))
                return
            team = ensure_team(registry, tokens[1])
            if not registry.active_team:
                set_active_team(registry, team.name)
                self._active_team_name = team.name
            self._save_team_registry(registry)
            await self._mount_message(
                AppMessage(
                    f"Created team `{team.name}`.\n"
                    f"Active team: {registry.active_team or '(unchanged)'}"
                )
            )
            return

        if action in {"use", "activate"}:
            if len(tokens) < 2:
                await self._mount_message(AppMessage("Usage: /team use <name>"))
                return
            team = find_team(registry, tokens[1])
            if team is None:
                await self._mount_message(
                    AppMessage(f"Team '{tokens[1]}' was not found.")
                )
                return
            set_active_team(registry, team.name)
            self._active_team_name = team.name
            self._save_team_registry(registry)
            await self._mount_message(AppMessage(f"Active team set to `{team.name}`."))
            return

        if action in {"clear", "none"}:
            set_active_team(registry, None)
            self._active_team_name = None
            self._save_team_registry(registry)
            await self._mount_message(AppMessage("Active team cleared."))
            return

        if action == "status":
            team_name = tokens[1] if len(tokens) > 1 else registry.active_team
            if not team_name:
                await self._mount_message(
                    AppMessage(
                        "Usage: /team status <name> or set an active team first."
                    )
                )
                return
            await self._mount_message(AppMessage(self._build_team_status(team_name)))
            return

        if action in {"add-member", "member-add"}:
            if len(tokens) < 3:
                await self._mount_message(
                    AppMessage("Usage: /team add-member <team> <member> [role]")
                )
                return
            role = tokens[3] if len(tokens) > 3 else "worker"
            team = add_team_member(registry, tokens[1], tokens[2], role)
            self._save_team_registry(registry)
            await self._mount_message(
                AppMessage(f"Added {tokens[2]} ({role}) to team `{team.name}`.")
            )
            return

        if action in {"remove-member", "member-remove"}:
            if len(tokens) < 3:
                await self._mount_message(
                    AppMessage("Usage: /team remove-member <team> <member>")
                )
                return
            removed = remove_team_member(registry, tokens[1], tokens[2])
            if not removed:
                await self._mount_message(
                    AppMessage(
                        f"Member '{tokens[2]}' was not found on team '{tokens[1]}'."
                    )
                )
                return
            self._save_team_registry(registry)
            await self._mount_message(
                AppMessage(f"Removed {tokens[2]} from team `{tokens[1]}`.")
            )
            return

        if action == "assign":
            if len(tokens) < 3:
                await self._mount_message(
                    AppMessage("Usage: /team assign <task-id> <team>")
                )
                return
            task_id = tokens[1]
            team_name = tokens[2]
            task = (
                self._bg_manager.get_status(task_id)
                if hasattr(self, "_bg_manager")
                else None
            )
            if task is None:
                task = await self._resolve_remote_task(task_id)
            if task is None:
                await self._mount_message(AppMessage(f"Task {task_id} was not found."))
                return
            ensure_team(registry, team_name)
            metadata = getattr(task, "metadata", None)
            if not isinstance(metadata, dict):
                metadata = {}
                task.metadata = metadata
            metadata["team_name"] = team_name
            self._save_team_registry(registry)
            if task_id in self._remote_tasks:
                await self._persist_remote_tasks()
            await self._mount_message(
                AppMessage(f"Assigned task {task_id} to team `{team_name}`.")
            )
            return

        if action == "message":
            if len(tokens) < 3:
                await self._mount_message(
                    AppMessage("Usage: /team message <team|task-id> <text>")
                )
                return
            target = tokens[1]
            body = raw_arg.split(None, 2)[2].strip()
            task = (
                self._bg_manager.get_status(target)
                if hasattr(self, "_bg_manager")
                else None
            )
            if task is None:
                task = await self._resolve_remote_task(target)
            if task is not None:
                metadata = getattr(task, "metadata", None)
                if not isinstance(metadata, dict):
                    metadata = {}
                    task.metadata = metadata
                inbox = metadata.get("inbox")
                if not isinstance(inbox, list):
                    inbox = []
                    metadata["inbox"] = inbox
                inbox.append(
                    {"body": body, "sender": "supervisor", "created_at": time.time()}
                )
                team_name = self._task_team_name(task)
                if team_name:
                    append_team_message(
                        registry,
                        team_name,
                        body,
                        sender=f"to:{target}",
                    )
                    self._save_team_registry(registry)
                if target in self._remote_tasks:
                    await self._persist_remote_tasks()
                await self._mount_message(
                    AppMessage(
                        f"Queued message for {target}."
                        f"{f' Team: {team_name}.' if team_name else ''}"
                    )
                )
                return

            team = ensure_team(registry, target)
            append_team_message(registry, team.name, body, sender="supervisor")
            self._save_team_registry(registry)
            await self._mount_message(AppMessage(f"Saved team note for `{team.name}`."))
            return

        if action == "summary":
            if len(tokens) == 1:
                team_name = registry.active_team
                if not team_name:
                    await self._mount_message(
                        AppMessage("Usage: /team summary <team> [set <text>]")
                    )
                    return
                team = find_team(registry, team_name)
                await self._mount_message(
                    AppMessage(
                        team.summary
                        if team and team.summary
                        else f"No summary set for `{team_name}`."
                    )
                )
                return
            team_name = tokens[1]
            team = find_team(registry, team_name)
            if team is None:
                await self._mount_message(
                    AppMessage(f"Team '{team_name}' was not found.")
                )
                return
            if len(tokens) >= 3 and tokens[2].lower() == "set":
                parts = raw_arg.split(None, 3)
                summary = parts[3].strip() if len(parts) >= 4 else ""
                if not summary:
                    await self._mount_message(
                        AppMessage("Usage: /team summary <team> set <text>")
                    )
                    return
                set_team_summary(registry, team.name, summary)
                self._save_team_registry(registry)
                await self._mount_message(
                    AppMessage(f"Updated summary for `{team.name}`.")
                )
                return
            await self._mount_message(
                AppMessage(team.summary or f"No summary set for `{team.name}`.")
            )
            return

        if action == "sync":
            team_name = tokens[1] if len(tokens) > 1 else registry.active_team
            if not team_name:
                await self._mount_message(
                    AppMessage("Usage: /team sync <name> or set an active team first.")
                )
                return
            team = find_team(registry, team_name)
            if team is None:
                await self._mount_message(
                    AppMessage(f"Team '{team_name}' was not found.")
                )
                return
            local_tasks, remote_tasks = self._team_task_snapshot(team.name)
            task_summaries: list[str] = []
            for task in [*local_tasks, *remote_tasks]:
                result = getattr(task, "result", "") or getattr(task, "output", "")
                if isinstance(result, str) and result.strip():
                    task_summaries.append(result.strip())
            summary = summarize_team_activity(team, task_summaries)
            set_team_summary(registry, team.name, summary)
            self._save_team_registry(registry)
            await self._mount_message(
                AppMessage(f"Synced shared summary for `{team.name}`.\n\n{summary}")
            )
            return

        if action == "show":
            if len(tokens) < 2:
                await self._mount_message(AppMessage("Usage: /team show <name>"))
                return
            await self._mount_message(AppMessage(self._build_team_status(tokens[1])))
            return

        await self._mount_message(
            AppMessage(
                # Developer team config commands
                "Team shared config:\n"
                "  /team status              — show team config & members\n"
                "  /team init <name>         — create team config in this project\n"
                "  /team setup               — show setup guide\n"
                "  /team whoami [set <n> <e>]— view/set your identity\n"
                "  /team invite <email> [role] [name] — add a team member\n"
                "  /team members             — list team members\n"
                "  /team context [list|add|remove|show] — manage shared context\n"
                "  /team prompt [list|show|add|run] — manage shared prompts\n"
                "  /team var [list|set|unset] — manage shared variables\n\n"
                # Multi-agent orchestration commands
                "Multi-agent orchestration:\n"
                "  /team create <name> | /team use <name> | /team assign <task> <team>\n"
                "  /team message <team|task-id> <text> | /team summary <team> [set <text>]\n"
                "  /team add-member <team> <member> [role] | /team sync <team>"
            )
        )

    async def _handle_recommend_command(self, command: str) -> None:
        """Handle /recommend slash command.

        Sends a structured review prompt to the agent based on the provided
        configuration (persona, focus, scope, etc.).

        Args:
            command: Full command string.
        """
        from bog_agents_cli.recommend import (
            build_clarifying_prompt,
            build_review_prompt,
            format_recommend_help,
            parse_recommend_args,
        )

        raw = command.strip()
        args_str = raw[len("/recommend") :].strip()

        # Show help
        if args_str in ("--help", "-h", "help"):
            await self._mount_message(AppMessage(format_recommend_help()))
            return

        # Parse config
        config = parse_recommend_args(args_str)

        if config.num_questions > 0:
            # Phase 1: Ask clarifying questions
            prompt = build_clarifying_prompt(config)
            # Store config for when user answers questions
            self._recommend_config = config
            await self._handle_user_message(f"Please review this codebase.\n\n{prompt}")
        else:
            # Skip questions, go straight to review
            prompt = build_review_prompt(config)
            await self._handle_user_message(f"Please review this codebase.\n\n{prompt}")

    async def _get_conversation_token_count(self) -> int | None:
        """Return the approximate conversation-only token count.

        Returns:
            Token count as an integer, or `None` if state is unavailable.
        """
        if not self._agent:
            return None
        try:
            from langchain_core.messages.utils import (
                count_tokens_approximately,
            )

            config: RunnableConfig = {
                "configurable": {"thread_id": self._lc_thread_id},
            }
            state = await self._agent.aget_state(config)
            if not state or not state.values:
                return None
            messages = state.values.get("messages", [])
            if not messages:
                return None
            return count_tokens_approximately(messages)
        except Exception:  # best-effort for /tokens display
            logger.debug("Failed to retrieve conversation token count", exc_info=True)
            return None

    def _resolve_compact_budget_str(self) -> str | None:
        """Resolve the compaction retention budget as a human-readable string.

        Instantiates a model and computes summarization defaults, so this is
        not a trivial accessor.

        Returns:
            A string like `"20.0K (10% of 200.0K)"` or
            `"last 6 messages"`, or `None` if the budget cannot be determined.
        """
        try:
            from bog_agents.middleware.summarization import (
                compute_summarization_defaults,
            )

            model_spec = f"{settings.model_provider}:{settings.model_name}"
            result = create_model(
                model_spec,
                profile_overrides=self._profile_override,
            )
            defaults = compute_summarization_defaults(result.model)
            return _format_compact_limit(
                defaults["keep"],
                settings.model_context_limit,
            )
        except Exception:  # best-effort for /tokens display
            logger.debug("Failed to compute compaction budget string", exc_info=True)
            return None

    async def _handle_compact(self) -> None:
        """Compact the conversation by summarizing old messages.

        Writes a `_summarization_event` into the agent's checkpointed state.
        On the next model call, `SummarizationMiddleware.wrap_model_call` reads
        this event and presents the summary plus recent messages to the model
        instead of the full history.

        Compaction is a no-op when the conversation's total token count is
        within the `keep` budget. Until that threshold is exceeded the user
        sees "Nothing to compact" with the retention budget and a pointer to
        `/tokens` for a full breakdown.
        """
        if not self._agent or not self._lc_thread_id:
            await self._mount_message(
                AppMessage("Nothing to compact \u2014 start a conversation first")
            )
            return

        if self._agent_running:
            await self._mount_message(
                AppMessage("Cannot compact while agent is running")
            )
            return

        from langchain_core.messages.utils import count_tokens_approximately

        config: RunnableConfig = {"configurable": {"thread_id": self._lc_thread_id}}

        try:
            state_values = await self._get_thread_state_values(self._lc_thread_id)
        except Exception as exc:
            await self._mount_message(ErrorMessage(f"Failed to read state: {exc}"))
            return

        if not state_values:
            await self._mount_message(
                AppMessage("Nothing to compact \u2014 start a conversation first")
            )
            return

        messages = state_values.get("messages", [])

        # Prevent concurrent user input while compaction modifies state
        self._agent_running = True
        try:
            await dispatch_hook("context.compact", {})
            await self._set_spinner("Compacting")

            from bog_agents.middleware.summarization import (
                SummarizationEvent,
                SummarizationMiddleware,
                compute_summarization_defaults,
            )

            try:
                model_spec = f"{settings.model_provider}:{settings.model_name}"
                result = create_model(
                    model_spec,
                    profile_overrides=self._profile_override,
                )
                model = result.model
            except Exception as exc:  # surface model config errors to user
                await self._mount_message(
                    ErrorMessage(
                        f"Compaction requires a working model configuration: {exc}"
                    )
                )
                return

            # create_model() receives --profile-override via self._profile_override,
            # but settings.model_context_limit may have been set by additional
            # runtime logic. Patch it into the fresh model when it differs from
            # the profile value.
            ctx = settings.model_context_limit
            if ctx is not None:
                # Guard against models that lack a profile dict
                # (custom/non-standard providers)
                profile = getattr(model, "profile", None)
                native = (
                    profile.get("max_input_tokens")
                    if isinstance(profile, dict)
                    else None
                )
                if native != ctx:
                    merged = (
                        {**profile, "max_input_tokens": ctx}
                        if isinstance(profile, dict)
                        else {"max_input_tokens": ctx}
                    )
                    with suppress(AttributeError, TypeError, ValueError):
                        model.profile = merged  # type: ignore[union-attr]

            defaults = compute_summarization_defaults(model)
            compact_backend = self._backend
            if compact_backend is None:
                from bog_agents.backends.filesystem import FilesystemBackend

                compact_backend = FilesystemBackend()
                logger.info("Using local FilesystemBackend for compaction")
            middleware = SummarizationMiddleware(
                model=model,
                backend=compact_backend,
                keep=defaults["keep"],
                trim_tokens_to_summarize=None,
            )

            # Rebuild the message list the model would see, accounting for
            # any prior compaction
            event = state_values.get("_summarization_event")
            effective = middleware._apply_event_to_messages(messages, event)

            cutoff = middleware._determine_cutoff_index(effective)
            compact_limit = _format_compact_limit(
                defaults["keep"],
                settings.model_context_limit,
            )

            if cutoff == 0:
                conv_tokens = count_tokens_approximately(effective)
                conv_str = format_token_count(conv_tokens)
                total_context = (
                    self._token_tracker.current_context if self._token_tracker else 0
                )
                context_limit = settings.model_context_limit

                if (
                    total_context > 0
                    and context_limit is not None
                    and total_context > context_limit
                ):
                    # Case A: total context exceeds model limit but
                    # conversation is within keep budget — excess is
                    # system prompt + tool overhead that compaction
                    # cannot reduce
                    total_str = format_token_count(total_context)
                    await self._mount_message(
                        AppMessage(
                            f"Nothing to compact \u2014 conversation is only "
                            f"~{conv_str} tokens.\n\n"
                            f"Total context ({total_str} tokens) is mostly "
                            f"system prompt and tool overhead, which "
                            f"compaction cannot reduce.\n\n"
                            f"Use /tokens for a full breakdown."
                        )
                    )
                else:
                    # Case B: genuinely within budget
                    await self._mount_message(
                        AppMessage(
                            f"Nothing to compact \u2014 conversation "
                            f"(~{conv_str} tokens) is within the "
                            f"retention budget ({compact_limit}).\n\n"
                            f"Use /tokens for a full breakdown."
                        )
                    )
                return

            to_summarize, to_keep = middleware._partition_messages(effective, cutoff)

            tokens_summarized = count_tokens_approximately(to_summarize)
            tokens_kept = count_tokens_approximately(to_keep)
            tokens_before = tokens_summarized + tokens_kept

            # Generate summary first so no side effects occur if the LLM fails
            summary = await middleware._acreate_summary(to_summarize)

            offload_result = await self._offload_messages_for_compact(
                to_summarize,
                middleware,
                backend=compact_backend,
            )
            if offload_result is None:
                # Actual failure (read/write error)
                await self._mount_message(
                    ErrorMessage(
                        "Warning: conversation history could not be saved to "
                        "storage. Older messages will not be recoverable. "
                    )
                )
            # offload_result == "" means nothing to offload (not an error)
            file_path = offload_result or None

            summary_msg = middleware._build_new_messages_with_path(summary, file_path)[
                0
            ]

            # Compute token savings and append to the summary message so the
            # model is aware of how much context was reclaimed.
            tokens_summary = count_tokens_approximately([summary_msg])
            tokens_after = tokens_summary + tokens_kept
            before = format_token_count(tokens_before)
            after = format_token_count(tokens_after)
            pct = (
                round((tokens_before - tokens_after) / tokens_before * 100)
                if tokens_before > 0
                else 0
            )
            summarized_before = format_token_count(tokens_summarized)
            summarized_after = format_token_count(tokens_summary)
            savings_note = (
                f"\n\n{len(to_summarize)} messages were compacted "
                f"({summarized_before} \u2192 {summarized_after} tokens). "
                f"Total context: {before} \u2192 {after} tokens "
                f"({pct}% decrease), "
                f"{len(to_keep)} messages unchanged."
            )
            summary_msg.content += savings_note

            state_cutoff = middleware._compute_state_cutoff(event, cutoff)

            new_event: SummarizationEvent = {
                "cutoff_index": state_cutoff,
                "summary_message": summary_msg,  # ty: ignore[invalid-argument-type]
                "file_path": file_path,
            }

            if remote := self._remote_agent():
                # After a dev-server restart, SQLite checkpoints may exist even
                # when the remote thread record has not yet been re-created.
                # Ensure the HTTP-side thread exists before updating state.
                await remote.aensure_thread(config)  # ty: ignore[invalid-argument-type]

            await self._agent.aupdate_state(config, {"_summarization_event": new_event})

            await self._mount_message(
                AppMessage(
                    "Conversation compacted. "
                    f"Summarized {len(to_summarize)} messages into a concise summary.\n"
                    f"Summarized context: {summarized_before} \u2192 "
                    f"{summarized_after} tokens\n"
                    f"Total context: {before} \u2192 {after} tokens "
                    f"({pct}% decrease), {len(to_keep)} messages unchanged."
                )
            )

            # Approximate token count via count_tokens_approximately (content
            # tokens only; excludes system prompts and tool schemas). The next
            # agent turn replaces this with the real count from usage_metadata.
            if self._token_tracker:
                self._token_tracker.add(tokens_after)

        except Exception as exc:  # surface compaction errors to user
            logger.exception("Compaction failed")
            await self._mount_message(ErrorMessage(f"Compaction failed: {exc}"))
        finally:
            self._agent_running = False
            try:
                await self._set_spinner(None)
            except Exception:  # best-effort spinner cleanup
                logger.exception("Failed to dismiss spinner after compaction")

    async def _offload_messages_for_compact(
        self,
        messages: list[Any],
        middleware: SummarizationMiddleware,
        *,
        backend: BackendProtocol | None = None,
    ) -> str | None:
        """Write messages to backend storage before compaction.

        Appends messages as a timestamped markdown section to the conversation
        history file, matching the `SummarizationMiddleware` offload pattern.

        Filters out prior summary messages using the middleware's
        `_filter_summary_messages` to avoid storing summaries-of-summaries.

        Args:
            messages: Messages to offload.
            middleware: `SummarizationMiddleware` instance for filtering.
            backend: Backend to persist conversation history to. Defaults to
                `self._backend` when omitted.

        Returns:
            File path where history was stored, `""` (empty string) if there
            were no non-summary messages to offload (not an error), or `None`
            if the write failed.
        """
        from datetime import UTC, datetime

        from langchain_core.messages import get_buffer_string

        history_backend = backend or self._backend
        if history_backend is None:
            logger.warning("No backend configured; cannot offload messages")
            return None

        path = f"/conversation_history/{self._lc_thread_id}.md"

        # Exclude prior summaries so the offloaded history contains only
        # original messages
        filtered = middleware._filter_summary_messages(messages)
        if not filtered:
            # Nothing to offload — all messages were summaries. Not an error.
            return ""

        timestamp = datetime.now(UTC).isoformat()
        buf = get_buffer_string(filtered)
        new_section = f"## Compacted at {timestamp}\n\n{buf}\n\n"

        existing_content = ""
        try:
            responses = await history_backend.adownload_files([path])
            resp = responses[0] if responses else None
            if resp and resp.content is not None and resp.error is None:
                existing_content = resp.content.decode("utf-8")
        except Exception as exc:  # abort write on read failure
            logger.warning(
                "Failed to read existing history at %s; aborting offload to "
                "avoid overwriting prior history: %s",
                path,
                exc,
                exc_info=True,
            )
            return None

        combined = existing_content + new_section

        try:
            result = (
                await history_backend.aedit(path, existing_content, combined)
                if existing_content
                else await history_backend.awrite(path, combined)
            )
            if result is None or result.error:
                error_detail = result.error if result else "backend returned None"
                logger.warning(
                    "Failed to offload compact history to %s: %s",
                    path,
                    error_detail,
                )
                return None
        except Exception as exc:  # defensive: surface write failures gracefully
            logger.warning(
                "Exception offloading compact history to %s: %s",
                path,
                exc,
                exc_info=True,
            )
            return None

        logger.debug("Offloaded %d messages to %s", len(filtered), path)
        return path

    async def _send_prompt_to_agent(self, prompt: str) -> None:
        """Send a prompt to the agent without displaying it as a user message.

        Use this for slash commands that construct long internal prompts.
        The calling command should display its own friendly user-facing
        message before calling this method.

        Args:
            prompt: The full prompt to send to the agent.
        """
        # Scroll to bottom
        try:
            chat = self.query_one("#chat", VerticalScroll)
            if chat.max_scroll_y > 0:
                chat.scroll_end(animate=False)
        except NoMatches:
            pass

        if self._agent and self._ui_adapter and self._session_state:
            self._agent_running = True
            if self._chat_input:
                self._chat_input.set_cursor_active(active=False)
            self._agent_worker = self.run_worker(
                self._run_agent_task(prompt),
                exclusive=False,
            )
        else:
            await self._mount_message(
                AppMessage("Agent not configured for this session.")
            )

    async def _handle_user_message(self, message: str) -> None:
        """Handle a user message to send to the agent.

        Args:
            message: The user's message
        """
        # Mount the user message (show original text in UI)
        await self._mount_message(UserMessage(message))

        # Resolve @-mentions before passing to the agent
        effective_message = message
        try:
            from bog_agents_cli.mentions import (
                get_mention_summary,
                has_mentions,
                resolve_mentions,
            )

            if has_mentions(message):
                resolution = await asyncio.to_thread(
                    resolve_mentions, message, cwd=Path(self._cwd)
                )
                effective_message = resolution.augmented
                summary = get_mention_summary(resolution)
                if summary:
                    await self._mount_message(AppMessage(summary))
        except Exception:
            logger.debug("Mention resolution failed", exc_info=True)

        # Scroll to bottom when user sends a new message
        try:
            chat = self.query_one("#chat", VerticalScroll)
            if chat.max_scroll_y > 0:
                chat.scroll_end(animate=False)
        except NoMatches:
            pass

        # Check if agent is available
        if self._agent and self._ui_adapter and self._session_state:
            self._agent_running = True

            if self._chat_input:
                self._chat_input.set_cursor_active(active=False)

            # Use run_worker to avoid blocking the main event loop
            # This allows the UI to remain responsive during agent execution
            self._agent_worker = self.run_worker(
                self._run_agent_task(effective_message),
                exclusive=False,
            )
        else:
            await self._mount_message(
                AppMessage("Agent not configured for this session.")
            )

    async def _run_agent_task(self, message: str) -> None:
        """Run the agent task in a background worker.

        This runs in a Textual worker so the main event loop stays responsive.
        """
        # Caller ensures _ui_adapter is set (checked in _handle_user_message)
        if self._ui_adapter is None:
            return
        turn_stats: SessionStats | None = None
        try:
            turn_stats = await execute_task_textual(
                user_input=message,
                agent=self._agent,
                assistant_id=self._assistant_id,
                session_state=self._session_state,
                adapter=self._ui_adapter,
                backend=self._backend,
                image_tracker=self._image_tracker,
                context=self._build_cli_context(),
            )
        except Exception as e:  # Resilient tool rendering
            logger.exception("Agent execution failed")
            # Ensure any in-flight tool calls don't remain stuck in "Running..."
            # when streaming aborts before tool results arrive.
            if self._ui_adapter:
                self._ui_adapter.finalize_pending_tools_with_error(f"Agent error: {e}")

            # Classify common provider/runtime failures so the recovery hint
            # matches the real problem instead of defaulting to auth advice.
            err_name = type(e).__name__
            err_str = str(e).lower()
            is_tool_capability_error = any(
                keyword in err_str
                for keyword in (
                    "does not support tools",
                    "tool calling is not supported",
                    "tools are not supported",
                )
            )
            is_auth_error = any(
                keyword in err_name.lower() or keyword in err_str
                for keyword in (
                    "token",
                    "credential",
                    "auth",
                    "forbidden",
                    "accessdenied",
                    "expired",
                    "sso",
                )
            )
            if is_tool_capability_error:
                await self._mount_message(
                    ErrorMessage(
                        f"Agent error: {e}\n\n"
                        "This model does not support tool use in the CLI. Try:\n"
                        "  - `/model` to switch to a tool-capable model\n"
                        "  - For local Ollama, prefer coding/chat models with tool support such as `qwen3-coder-next:latest`\n"
                        "  - Use `--doctor` to confirm Ollama is reachable and the selected model is available"
                    )
                )
            elif is_auth_error:
                await self._mount_message(
                    ErrorMessage(
                        f"Agent error: {e}\n\n"
                        "This looks like an authentication/credential error. Try:\n"
                        "  - `/settings` to configure providers and fallbacks\n"
                        "  - `/model` to switch to a different provider\n"
                        "  - For AWS Bedrock: run `aws sso login` to refresh credentials"
                    )
                )
            else:
                await self._mount_message(ErrorMessage(f"Agent error: {e}"))
        finally:
            # Clean up loading widget and agent state
            await self._cleanup_agent_task()

        # Accumulate stats across all turns; printed once at session end
        if isinstance(turn_stats, SessionStats):
            self._session_stats.merge(turn_stats)

        # Auto-commit after each successful agent turn
        if self._auto_commit and turn_stats is not None:
            from bog_agents_cli.auto_commit import run_auto_commit

            sha = await run_auto_commit(cwd=Path(self._cwd))
            if sha:
                await self._mount_message(
                    AppMessage(f"[dim]Auto-committed: {sha} (bog-agent)[/dim]")
                )

    async def _process_next_from_queue(self) -> None:
        """Process the next message from the queue if any exist.

        Dequeues and processes the next pending message in FIFO order.
        Uses the `_processing_pending` flag to prevent reentrant execution.
        """
        if self._processing_pending or not self._pending_messages or self._exit:
            return

        self._processing_pending = True
        try:
            msg = self._pending_messages.popleft()

            # Remove the ephemeral queued-message widget
            if self._queued_widgets:
                widget = self._queued_widgets.popleft()
                await widget.remove()

            await self._process_message(msg.text, msg.mode)
        except Exception:
            logger.exception("Failed to process queued message")
            await self._mount_message(
                ErrorMessage(f"Failed to process queued message: {msg.text[:60]}")
            )
        finally:
            self._processing_pending = False

        # Command mode messages complete synchronously without spawning
        # a worker, so cleanup won't fire again. Continue draining the
        # queue if no worker was started.
        busy = self._agent_running or self._shell_running
        if not busy and self._pending_messages:
            await self._process_next_from_queue()

    async def _cleanup_agent_task(self) -> None:
        """Clean up after agent task completes or is cancelled."""
        self._agent_running = False
        self._agent_worker = None

        # Remove spinner if present
        await self._set_spinner(None)

        if self._chat_input:
            self._chat_input.set_cursor_active(active=True)

        # Ensure token display is restored (in case of early cancellation)
        if self._token_tracker:
            self._token_tracker.show()

        # Process next message from queue if any
        await self._process_next_from_queue()

    @staticmethod
    def _convert_messages_to_data(messages: list[Any]) -> list[MessageData]:
        """Convert LangChain messages into lightweight `MessageData` objects.

        This is a pure function with zero DOM operations. Tool call matching
        happens here: `ToolMessage` results are matched by `tool_call_id` and
        stored directly on the corresponding `MessageData`.

        Args:
            messages: LangChain message objects from a thread checkpoint.

        Returns:
            Ordered list of `MessageData` ready for `MessageStore.bulk_load`.
        """
        from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

        result: list[MessageData] = []
        # Maps tool_call_id -> index into result list
        pending_tool_indices: dict[str, int] = {}

        for msg in messages:
            if isinstance(msg, HumanMessage):
                content = (
                    msg.content if isinstance(msg.content, str) else str(msg.content)
                )
                if content.startswith("[SYSTEM]"):
                    continue
                result.append(MessageData(type=MessageType.USER, content=content))

            elif isinstance(msg, AIMessage):
                # Extract text content
                content = msg.content
                text = ""
                if isinstance(content, str):
                    text = content.strip()
                elif isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            text += block.get("text", "")
                        elif isinstance(block, str):
                            text += block
                    text = text.strip()

                if text:
                    result.append(MessageData(type=MessageType.ASSISTANT, content=text))

                # Track tool calls for later matching
                for tc in getattr(msg, "tool_calls", []):
                    tc_id = tc.get("id")
                    name = tc.get("name", "unknown")
                    args = tc.get("args", {})
                    data = MessageData(
                        type=MessageType.TOOL,
                        content="",
                        tool_name=name,
                        tool_args=args,
                        tool_status=ToolStatus.PENDING,
                    )
                    result.append(data)
                    if tc_id:
                        pending_tool_indices[tc_id] = len(result) - 1
                    else:
                        data.tool_status = ToolStatus.REJECTED

            elif isinstance(msg, ToolMessage):
                tc_id = getattr(msg, "tool_call_id", None)
                if tc_id and tc_id in pending_tool_indices:
                    idx = pending_tool_indices.pop(tc_id)
                    data = result[idx]
                    status = getattr(msg, "status", "success")
                    content = (
                        msg.content
                        if isinstance(msg.content, str)
                        else str(msg.content)
                    )
                    if status == "success":
                        data.tool_status = ToolStatus.SUCCESS
                    else:
                        data.tool_status = ToolStatus.ERROR
                    data.tool_output = content
                else:
                    logger.debug(
                        "ToolMessage with tool_call_id=%r could not be "
                        "matched to a pending tool call",
                        tc_id,
                    )

            else:
                logger.debug(
                    "Skipping unsupported message type %s during history conversion",
                    type(msg).__name__,
                )

        # Mark unmatched tool calls as rejected
        for idx in pending_tool_indices.values():
            result[idx].tool_status = ToolStatus.REJECTED

        return result

    async def _get_thread_state_values(self, thread_id: str) -> dict[str, Any]:
        """Fetch thread state values, with remote checkpointer fallback.

        In server mode the LangGraph dev server can report an empty thread state
        after a restart even when checkpoints exist on disk. When that happens,
        read the latest checkpoint directly so resumed threads can still load
        history and compact correctly.

        Args:
            thread_id: Thread ID to fetch from checkpoint storage.

        Returns:
            Thread state values keyed by channel name. Returns an empty dict
                when no checkpointed values are available.
        """
        if not self._agent:
            return {}

        config: RunnableConfig = {"configurable": {"thread_id": thread_id}}
        state = await self._agent.aget_state(config)

        values: dict[str, Any] = {}
        if state and state.values:
            values = dict(state.values)

        messages = values.get("messages")
        if isinstance(messages, list) and messages:
            return values
        if not self._remote_agent():
            return values

        logger.debug(
            "Remote state empty for thread %s; falling back to local checkpointer",
            thread_id,
        )
        fallback_values = await self._read_channel_values_from_checkpointer(thread_id)
        fallback_messages = fallback_values.get("messages")
        if isinstance(fallback_messages, list) and fallback_messages:
            values["messages"] = fallback_messages
        if (
            values.get("_summarization_event") is None
            and "_summarization_event" in fallback_values
        ):
            values["_summarization_event"] = fallback_values["_summarization_event"]
        return values

    async def _fetch_thread_history_data(self, thread_id: str) -> list[MessageData]:
        """Fetch and convert stored messages for a thread.

        In server mode the LangGraph dev server starts with an empty thread
        store, so `aget_state` via the HTTP API returns no messages even when
        checkpoints exist on disk. We fall back to reading the SQLite
        checkpointer directly to guarantee resumed threads load their history.

        Args:
            thread_id: Thread ID to fetch from checkpoint storage.

        Returns:
            Converted message data ready for bulk loading.
        """
        state_values = await self._get_thread_state_values(thread_id)
        messages = state_values.get("messages", [])

        if not messages:
            return []

        # Server mode / direct checkpointer may return dicts; convert to
        # LangChain message objects so _convert_messages_to_data works.
        if messages and isinstance(messages[0], dict):
            from langchain_core.messages.utils import convert_to_messages

            messages = convert_to_messages(messages)

        # Offload conversion so large histories don't block the UI loop.
        return await asyncio.to_thread(self._convert_messages_to_data, messages)

    @staticmethod
    async def _read_channel_values_from_checkpointer(thread_id: str) -> dict[str, Any]:
        """Read checkpoint channel values directly from the SQLite checkpointer.

        Args:
            thread_id: Thread ID to look up.

        Returns:
            Channel values from the latest checkpoint, or an empty dict on
                failure.
        """
        try:
            from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

            from bog_agents_cli.sessions import get_db_path

            db_path = str(get_db_path())
            config: RunnableConfig = {"configurable": {"thread_id": thread_id}}
            async with AsyncSqliteSaver.from_conn_string(db_path) as saver:
                tup = await saver.aget_tuple(config)
                if tup and tup.checkpoint:
                    channel_values = tup.checkpoint.get("channel_values", {})
                    if isinstance(channel_values, dict):
                        return dict(channel_values)
        except (ImportError, OSError) as exc:
            logger.warning(
                "Failed to read checkpointer directly for %s: %s",
                thread_id,
                exc,
            )
        except Exception:
            logger.warning(
                "Unexpected error reading checkpointer for %s",
                thread_id,
                exc_info=True,
            )
        return {}

    async def _upgrade_thread_message_link(
        self,
        widget: AppMessage,
        *,
        prefix: str,
        thread_id: str,
    ) -> None:
        """Upgrade a plain thread message to a linked one when URL resolves.

        Args:
            widget: The already-mounted app message.
            prefix: Text prefix before thread ID.
            thread_id: Thread ID to resolve.
        """
        try:
            thread_msg = await self._build_thread_message(prefix, thread_id)
            if not isinstance(thread_msg, Text):
                logger.debug(
                    "Skipping thread link upgrade for %s: URL did not resolve",
                    thread_id,
                )
                return
            if widget.parent is None:
                logger.debug(
                    "Skipping thread link upgrade for %s: widget no longer mounted",
                    thread_id,
                )
                return
            # Keep serialized content in sync with the rendered content.
            widget._content = thread_msg
            widget.update(thread_msg)
        except Exception:
            logger.warning(
                "Failed to upgrade thread message link for %s",
                thread_id,
                exc_info=True,
            )

    def _schedule_thread_message_link(
        self,
        widget: AppMessage,
        *,
        prefix: str,
        thread_id: str,
    ) -> None:
        """Schedule thread URL link resolution and apply updates in the background.

        Args:
            widget: The message widget to update.
            prefix: Text prefix before thread ID.
            thread_id: Thread ID to resolve.
        """
        self.run_worker(
            self._upgrade_thread_message_link(
                widget,
                prefix=prefix,
                thread_id=thread_id,
            ),
            exclusive=False,
        )

    async def _load_thread_history(
        self,
        *,
        thread_id: str | None = None,
        preloaded_data: list[MessageData] | None = None,
    ) -> None:
        """Load and render message history when resuming a thread.

        When `preloaded_data` is provided (e.g., from `_resume_thread`), this
        reuses that payload. Otherwise, it fetches checkpoint state from the
        agent and converts stored messages into lightweight `MessageData`
        objects. The method then bulk-loads into the `MessageStore` and mounts
        only the last `WINDOW_SIZE` widgets to reduce DOM operations on large
        threads.

        Args:
            thread_id: Optional explicit thread ID to load.

                Defaults to current.
            preloaded_data: Optional pre-fetched history data for the thread.
        """
        history_thread_id = thread_id or self._lc_thread_id
        if not history_thread_id:
            logger.debug("Skipping history load: no thread ID available")
            return
        if preloaded_data is None and not self._agent:
            logger.debug(
                "Skipping history load for %s: no active agent and no preloaded data",
                history_thread_id,
            )
            return

        try:
            # Fetch + convert, or reuse preloaded payload on thread switch.
            all_data = (
                preloaded_data
                if preloaded_data is not None
                else await self._fetch_thread_history_data(history_thread_id)
            )
            if not all_data:
                return

            # 3. Bulk load into store (sets visible window)
            _archived, visible = self._message_store.bulk_load(all_data)

            # 5. Cache container ref (single query)
            try:
                messages_container = self.query_one("#messages", Container)
            except NoMatches:
                return

            # 6-7. Create and mount only visible widgets (max WINDOW_SIZE)
            widgets = [msg_data.to_widget() for msg_data in visible]
            if widgets:
                await messages_container.mount(*widgets)

            # 8. Render content for AssistantMessage after mount
            assistant_updates = [
                widget.set_content(msg_data.content)
                for widget, msg_data in zip(widgets, visible, strict=False)
                if isinstance(widget, AssistantMessage) and msg_data.content
            ]
            if assistant_updates:
                assistant_results = await asyncio.gather(
                    *assistant_updates,
                    return_exceptions=True,
                )
                for error in assistant_results:
                    if isinstance(error, Exception):
                        logger.warning(
                            "Failed to render assistant history message for %s: %s",
                            history_thread_id,
                            error,
                        )

            # 9. Add footer immediately and resolve link asynchronously
            thread_msg_widget = AppMessage(f"Resumed thread: {history_thread_id}")
            await self._mount_message(thread_msg_widget)
            self._schedule_thread_message_link(
                thread_msg_widget,
                prefix="Resumed thread",
                thread_id=history_thread_id,
            )

            # 10. Scroll once
            def scroll_to_end() -> None:
                with suppress(NoMatches):
                    chat = self.query_one("#chat", VerticalScroll)
                    chat.scroll_end(animate=False, immediate=True)

            self.set_timer(0.1, scroll_to_end)

        except Exception as e:  # Resilient history loading
            logger.exception(
                "Failed to load thread history for %s",
                history_thread_id,
            )
            await self._mount_message(AppMessage(f"Could not load history: {e}"))

    async def _mount_message(
        self, widget: Static | AssistantMessage | ToolCallMessage
    ) -> None:
        """Mount a message widget to the messages area.

        This method also stores the message data and handles pruning
        when the widget count exceeds the maximum.

        If the ``#messages`` container is not present (e.g. the screen has
        been torn down during an interruption), the call is silently skipped
        to avoid cascading `NoMatches` errors.

        Args:
            widget: The message widget to mount
        """
        try:
            messages = self.query_one("#messages", Container)
        except NoMatches:
            return

        # Store message data for virtualization
        message_data = MessageData.from_widget(widget)
        # Ensure the widget's DOM id matches the store id so that
        # features like click-to-show-timestamp can look it up.
        if not widget.id:
            widget.id = message_data.id
        self._message_store.append(message_data)

        # Queued-message widgets must always stay at the bottom so they
        # remain visually anchored below the current agent response.
        if isinstance(widget, QueuedUserMessage):
            await messages.mount(widget)
        else:
            await self._mount_before_queued(messages, widget)

        # Prune old widgets if window exceeded
        await self._prune_old_messages()

        # Scroll to keep input bar visible
        try:
            input_container = self.query_one("#bottom-app-container", Container)
            input_container.scroll_visible()
        except NoMatches:
            pass

    async def _prune_old_messages(self) -> None:
        """Prune oldest message widgets if we exceed the window size.

        This removes widgets from the DOM but keeps data in MessageStore
        for potential re-hydration when scrolling up.
        """
        if not self._message_store.window_exceeded():
            return

        try:
            messages_container = self.query_one("#messages", Container)
        except NoMatches:
            logger.debug("Skipping pruning: #messages container not found")
            return

        to_prune = self._message_store.get_messages_to_prune()
        if not to_prune:
            return

        pruned_ids: list[str] = []
        for msg_data in to_prune:
            try:
                widget = messages_container.query_one(f"#{msg_data.id}")
                await widget.remove()
                pruned_ids.append(msg_data.id)
            except NoMatches:
                # Widget not found -- do NOT mark as pruned to avoid
                # desyncing the store from the actual DOM state
                logger.debug(
                    "Widget %s not found during pruning, skipping",
                    msg_data.id,
                )

        if pruned_ids:
            self._message_store.mark_pruned(pruned_ids)

    def _set_active_message(self, message_id: str | None) -> None:
        """Set the active streaming message (won't be pruned).

        Args:
            message_id: The ID of the active message, or None to clear.
        """
        self._message_store.set_active_message(message_id)

    def _sync_message_content(self, message_id: str, content: str) -> None:
        """Sync final message content back to the store after streaming.

        Called when streaming finishes so the store holds the full text
        instead of the empty string captured at mount time.

        Args:
            message_id: The ID of the message to update.
            content: The final content after streaming.
        """
        self._message_store.update_message(
            message_id,
            content=content,
            is_streaming=False,
        )

    async def _clear_messages(self) -> None:
        """Clear the messages area and message store."""
        # Clear the message store first
        self._message_store.clear()
        try:
            messages = self.query_one("#messages", Container)
            await messages.remove_children()
        except NoMatches:
            logger.warning(
                "Messages container (#messages) not found during clear; "
                "UI may be out of sync with message store"
            )

    def _discard_queue(self) -> None:
        """Clear pending messages and remove queued widgets from the DOM."""
        self._pending_messages.clear()
        for w in self._queued_widgets:
            w.remove()
        self._queued_widgets.clear()

    def _cancel_worker(self, worker: Worker[None] | None) -> None:
        """Discard the message queue and cancel an active worker.

        Args:
            worker: The worker to cancel.
        """
        self._discard_queue()
        if worker is not None:
            worker.cancel()

    def action_quit_or_interrupt(self) -> None:
        """Handle Ctrl+C - interrupt agent, reject approval, or quit on double press.

        Priority order:
        1. If shell command is running, kill it
        2. If approval menu is active, reject it
        3. If agent is running, interrupt it (preserve input)
        4. If double press (quit_pending), quit
        5. Otherwise show quit hint
        """
        # If shell command is running, cancel the worker
        if self._shell_running and self._shell_worker:
            self._cancel_worker(self._shell_worker)
            self._quit_pending = False
            return

        # If approval menu is active, reject it before cancelling the agent worker.
        # During HITL the agent worker remains active while awaiting approval,
        # so this must be checked before the worker cancellation branch to
        # avoid leaving a stale approval widget interactive after interruption.
        if self._pending_approval_widget:
            self._pending_approval_widget.action_select_reject()
            self._quit_pending = False
            return

        # If ask_user menu is active, cancel it before cancelling the agent
        # worker, following the same pattern as the approval widget above.
        if self._pending_ask_user_widget:
            self._pending_ask_user_widget.action_cancel()
            self._quit_pending = False
            return

        # If agent is running, interrupt it and discard queued messages
        if self._agent_running and self._agent_worker:
            self._cancel_worker(self._agent_worker)
            self._quit_pending = False
            return

        # Double Ctrl+C to quit
        if self._quit_pending:
            self.exit()
        else:
            self._arm_quit_pending("Ctrl+C")

    def _arm_quit_pending(self, shortcut: str) -> None:
        """Set the pending-quit flag and show a matching hint.

        Args:
            shortcut: The key chord to show in the quit hint.
        """
        self._quit_pending = True
        quit_timeout = 3
        self.notify(f"Press {shortcut} again to quit", timeout=quit_timeout)
        self.set_timer(quit_timeout, lambda: setattr(self, "_quit_pending", False))

    def action_interrupt(self) -> None:
        """Handle escape key.

        Priority order:
        1. If modal screen is active, dismiss it
        2. If completion popup is open, dismiss it
        3. If input is in command/shell mode, exit to normal mode
        4. If shell command is running, kill it
        5. If approval menu is active, reject it
        6. If agent is running, interrupt it
        """
        if (
            isinstance(self.screen, ThreadSelectorScreen)
            and self.screen.is_delete_confirmation_open
        ):
            self.screen.action_cancel()
            return

        # If a modal screen is active, dismiss it
        if isinstance(self.screen, ModalScreen):
            self.screen.dismiss(None)
            return

        # Close completion popup or exit slash/shell command mode
        if self._chat_input:
            if self._chat_input.dismiss_completion():
                return
            if self._chat_input.exit_mode():
                return

        # If shell command is running, cancel the worker
        if self._shell_running and self._shell_worker:
            self._cancel_worker(self._shell_worker)
            return

        # If approval menu is active, reject it before cancelling the agent worker.
        # During HITL the agent worker remains active while awaiting approval,
        # so this must be checked before the worker cancellation branch to
        # avoid leaving a stale approval widget interactive after interruption.
        if self._pending_approval_widget:
            self._pending_approval_widget.action_select_reject()
            return

        # If ask_user menu is active, cancel it before cancelling the agent
        # worker, following the same pattern as the approval widget above.
        if self._pending_ask_user_widget:
            self._pending_ask_user_widget.action_cancel()
            return

        # If agent is running, interrupt it and discard queued messages
        if self._agent_running and self._agent_worker:
            self._cancel_worker(self._agent_worker)
            return

    def action_quit_app(self) -> None:
        """Handle quit action (Ctrl+D)."""
        if isinstance(self.screen, ThreadSelectorScreen):
            self.screen.action_delete_thread()
            return
        if isinstance(self.screen, DeleteThreadConfirmScreen):
            if self._quit_pending:
                self.exit()
                return
            self._arm_quit_pending("Ctrl+D")
            return
        self.exit()

    def exit(
        self,
        result: Any = None,  # noqa: ANN401  # Dynamic LangGraph stream result type
        return_code: int = 0,
        message: Any = None,  # noqa: ANN401  # Dynamic LangGraph message type
    ) -> None:
        """Exit the app, restoring iTerm2 cursor guide if applicable.

        Overrides parent to restore iTerm2's cursor guide before Textual's
        cleanup. The atexit handler serves as a fallback for abnormal
        termination.

        Args:
            result: Return value passed to the app runner.
            return_code: Exit code (non-zero for errors).
            message: Optional message to display on exit.
        """
        # Discard queued messages so _cleanup_agent_task won't try to
        # process them after the event loop is torn down, and cancel
        # active workers so their subprocesses are terminated
        # (SIGTERM → SIGKILL) instead of being orphaned.
        self._discard_queue()
        if self._shell_running and self._shell_worker:
            self._shell_worker.cancel()
        if self._agent_running and self._agent_worker:
            self._agent_worker.cancel()
        for server in self._preview_servers.values():
            proc = server.process
            if proc is None or proc.returncode is not None:
                continue
            with suppress(ProcessLookupError, OSError):
                proc.terminate()

        # Dispatch synchronously — the event loop is about to be torn down by
        # super().exit(), so an async task would never complete.
        from bog_agents_cli.hooks import _dispatch_hook_sync, _load_hooks

        hooks = _load_hooks()
        if hooks:
            payload = json.dumps(
                {
                    "event": "session.end",
                    "thread_id": getattr(self, "_lc_thread_id", ""),
                }
            ).encode()
            _dispatch_hook_sync("session.end", payload, hooks)

        _write_iterm_escape(_ITERM_CURSOR_GUIDE_ON)
        super().exit(result=result, return_code=return_code, message=message)

    def action_toggle_auto_approve(self) -> None:
        """Toggle auto-approve mode for the current session.

        When enabled, all tool calls (shell execution, file writes/edits,
        web search, URL fetch) run without prompting. Updates the status
        bar indicator and session state.
        """
        if isinstance(self.screen, ThreadSelectorScreen):
            self.screen.action_focus_previous_filter()
            return
        # shift+tab is reused for navigation inside modal screens (e.g.
        # ModelSelectorScreen); skip the toggle so it doesn't fire through.
        if isinstance(self.screen, ModalScreen):
            return
        # Delegate shift+tab to ask_user navigation when interview is active.
        if self._pending_ask_user_widget is not None:
            self._pending_ask_user_widget.action_previous_question()
            return
        self._auto_approve = not self._auto_approve
        if self._status_bar:
            self._status_bar.set_auto_approve(enabled=self._auto_approve)
        if self._session_state:
            self._session_state.auto_approve = self._auto_approve

    def action_toggle_tool_output(self) -> None:
        """Toggle expand/collapse of the most recent tool output."""
        # Find all tool messages with output, get the most recent one
        # NoMatches is raised if no ToolCallMessage widgets exist
        with suppress(NoMatches):
            tool_messages = list(self.query(ToolCallMessage))
            # Find ones with output, toggle the most recent
            for tool_msg in reversed(tool_messages):
                if tool_msg.has_output:
                    tool_msg.toggle_output()
                    return

    # Approval menu action handlers (delegated from App-level bindings)
    # NOTE: These only activate when approval widget is pending
    # AND input is not focused
    def action_approval_up(self) -> None:
        """Handle up arrow in approval menu."""
        # Only handle if approval is active
        # (input handles its own up for history/completion)
        if self._pending_approval_widget and not self._is_input_focused():
            self._pending_approval_widget.action_move_up()

    def action_approval_down(self) -> None:
        """Handle down arrow in approval menu."""
        if self._pending_approval_widget and not self._is_input_focused():
            self._pending_approval_widget.action_move_down()

    def action_approval_select(self) -> None:
        """Handle enter in approval menu."""
        # Only handle if approval is active AND input is not focused
        if self._pending_approval_widget and not self._is_input_focused():
            self._pending_approval_widget.action_select()

    def _is_input_focused(self) -> bool:
        """Check if the chat input (or its text area) has focus.

        Returns:
            True if the input widget has focus, False otherwise.
        """
        if not self._chat_input:
            return False
        focused = self.focused
        if focused is None:
            return False
        # Check if focused widget is the text area inside chat input
        return focused.id == "chat-input" or focused in self._chat_input.walk_children()

    def action_approval_yes(self) -> None:
        """Handle yes/1 in approval menu."""
        if self._pending_approval_widget:
            self._pending_approval_widget.action_select_approve()

    def action_approval_auto(self) -> None:
        """Handle auto/2 in approval menu."""
        if self._pending_approval_widget:
            self._pending_approval_widget.action_select_auto()

    def action_approval_no(self) -> None:
        """Handle no/3 in approval menu."""
        if self._pending_approval_widget:
            self._pending_approval_widget.action_select_reject()

    def action_approval_escape(self) -> None:
        """Handle escape in approval menu - reject."""
        if self._pending_approval_widget:
            self._pending_approval_widget.action_select_reject()

    def on_paste(self, event: Paste) -> None:
        """Route unfocused paste events to chat input for drag/drop reliability."""
        if not self._chat_input:
            return
        if (
            self._pending_approval_widget
            or self._pending_ask_user_widget
            or self._is_input_focused()
        ):
            return
        if self._chat_input.handle_external_paste(event.text):
            event.prevent_default()
            event.stop()

    def action_copy_selection(self) -> None:
        """Copy the current selection to the system clipboard."""
        copy_selection_to_clipboard(self)

    def action_paste_clipboard(self) -> None:
        """Paste clipboard text into the chat input."""
        if not self._chat_input:
            return
        pasted = read_clipboard_text()
        if not pasted:
            self.notify(
                "Clipboard is empty or unavailable",
                severity="warning",
                timeout=2,
            )
            return
        self._chat_input.handle_external_paste(pasted)

    @staticmethod
    def _mouse_position(event: object) -> tuple[int, int] | None:
        """Extract a stable `(x, y)` pair from a Textual mouse event."""
        x = getattr(event, "screen_x", getattr(event, "x", None))
        y = getattr(event, "screen_y", getattr(event, "y", None))
        if not isinstance(x, int) or not isinstance(y, int):
            return None
        return x, y

    @staticmethod
    def _is_widget_within(widget: object, ancestor: object) -> bool:
        """Return whether `widget` is `ancestor` or one of its descendants."""
        current = widget
        while current is not None:
            if current is ancestor:
                return True
            current = getattr(current, "parent", None)
        return False

    def on_mouse_down(self, event: MouseDown) -> None:
        """Track drag distance so selection/copy does not run on every click."""
        self._mouse_down_position = self._mouse_position(event)
        self._mouse_drag_distance = 0

    def on_mouse_move(self, event: MouseMove) -> None:
        """Track the largest drag distance since the last mouse-down."""
        if self._mouse_down_position is None:
            return
        current = self._mouse_position(event)
        if current is None:
            return
        start_x, start_y = self._mouse_down_position
        cur_x, cur_y = current
        self._mouse_drag_distance = max(
            self._mouse_drag_distance,
            abs(cur_x - start_x),
            abs(cur_y - start_y),
        )

    def on_app_focus(self) -> None:
        """Restore chat input focus when the terminal regains OS focus.

        When the user opens a link via `webbrowser.open`, OS focus shifts to
        the browser. On returning to the terminal, Textual fires `AppFocus`
        (requires a terminal that supports FocusIn events). Re-focusing the chat
        input here keeps it ready for typing.
        """
        if not self._chat_input:
            return
        if isinstance(self.screen, ModalScreen):
            return
        if self._pending_approval_widget or self._pending_ask_user_widget:
            return
        self._chat_input.focus_input()

    def on_click(self, event: Click) -> None:
        """Handle clicks anywhere in the terminal to focus on the command line."""
        if not self._chat_input:
            return
        # Don't steal focus from approval or ask_user widgets
        if self._pending_approval_widget or self._pending_ask_user_widget:
            return
        if self._mouse_drag_distance >= self._SELECTION_DRAG_THRESHOLD:
            return
        try:
            messages_container = self.query_one("#messages", Container)
        except NoMatches:
            messages_container = None
        widget = getattr(event, "widget", None)
        if messages_container is not None and self._is_widget_within(
            widget, messages_container
        ):
            return
        self.call_after_refresh(self._chat_input.focus_input)

    def on_mouse_up(self, _event: MouseUp) -> None:
        """Copy selection to clipboard on mouse release."""
        try:
            if self._mouse_drag_distance >= self._SELECTION_DRAG_THRESHOLD:
                copy_selection_to_clipboard(self)
        finally:
            self._mouse_down_position = None
            self._mouse_drag_distance = 0

    # =========================================================================
    # Prompt Library
    # =========================================================================

    async def _handle_prompt_command(self, _command: str) -> None:
        """Open the prompt library picker modal."""
        from bog_agents_cli.widgets.prompt_library_screen import (
            PromptLibraryScreen,
            PromptResult,
        )

        def handle_result(result: PromptResult | None) -> None:
            if result is not None:
                self.call_later(self._send_prompt_to_agent, result.text)
            if self._chat_input:
                self._chat_input.focus_input()

        self.push_screen(PromptLibraryScreen(), handle_result)

    # =========================================================================
    # Pipelines
    # =========================================================================

    async def _handle_pipeline_command(self, _command: str) -> None:
        """Open the pipeline picker modal and execute the selected pipeline."""
        from bog_agents_cli.widgets.pipeline_screen import (
            PipelineScreen,
        )

        def handle_result(result: PipelineRunRequest | None) -> None:
            if result is not None:
                self.call_later(self._run_pipeline_request, result)
            if self._chat_input:
                self._chat_input.focus_input()

        self.push_screen(PipelineScreen(), handle_result)

    async def _run_pipeline_request(self, request: PipelineRunRequest) -> None:
        """Execute a pipeline request step by step."""
        from bog_agents_cli.pipeline import execute_pipeline

        pipeline = request.pipeline
        variable_values = request.variable_values

        await self._mount_message(
            AppMessage(
                f"Running pipeline [bold]{pipeline.name}[/bold] "
                f"({len(pipeline.steps)} step{'s' if len(pipeline.steps) != 1 else ''})…"
            )
        )

        async def on_step(step_index: int, step_id: str, rendered_text: str) -> None:
            await self._mount_message(
                AppMessage(f"Step [{step_index + 1}/{len(pipeline.steps)}]: {step_id}")
            )
            await self._send_prompt_to_agent(rendered_text)
            # Wait for the agent to finish before moving to the next step
            if self._agent_worker is not None:
                await self._agent_worker.wait()

        result = await execute_pipeline(pipeline, variable_values, on_step=on_step)

        if result.errors:
            for error in result.errors:
                await self._mount_message(ErrorMessage(error))
        else:
            await self._mount_message(
                AppMessage(
                    f"Pipeline [bold]{pipeline.name}[/bold] completed "
                    f"({result.completed_steps} step{'s' if result.completed_steps != 1 else ''} run)."
                )
            )

    async def _seed_defaults(self) -> None:  # noqa: PLR6301
        """Seed built-in default prompts and pipelines (runs once per version, additive)."""
        try:
            from bog_agents_cli.defaults_seeder import seed_if_needed

            await asyncio.to_thread(seed_if_needed)
        except Exception:
            logger.debug("Default content seeding failed (non-fatal)", exc_info=True)

    def _init_pipeline_scheduler(self) -> None:
        """Initialize the pipeline scheduler with a callback that queues steps."""
        from bog_agents_cli.pipeline import get_scheduler
        from bog_agents_cli.widgets.pipeline_screen import PipelineRunRequest

        def scheduled_pipeline_callback(
            pipeline: Pipeline, variable_values: dict[str, str]
        ) -> None:
            """Called by the scheduler when a pipeline is due — post to app event loop."""
            req = PipelineRunRequest(pipeline=pipeline, variable_values=variable_values)
            self.call_from_thread(self._run_pipeline_request, req)

        try:
            scheduler = get_scheduler(scheduled_pipeline_callback)
            scheduler.reload()
            logger.info("Pipeline scheduler initialized")
        except Exception:
            logger.warning("Could not initialize pipeline scheduler", exc_info=True)

    # =========================================================================
    # Model Switching
    # =========================================================================

    async def _show_model_selector(
        self,
        *,
        extra_kwargs: dict[str, Any] | None = None,
    ) -> None:
        """Show interactive model selector as a modal screen.

        Args:
            extra_kwargs: Extra constructor kwargs from `--model-params`.
        """
        from functools import partial

        def handle_result(result: tuple[str, str] | None) -> None:
            """Handle the model selector result."""
            if result is not None:
                model_spec, _ = result
                self.call_later(
                    partial(
                        self._switch_model,
                        model_spec,
                        extra_kwargs=extra_kwargs,
                    )
                )
            # Refocus input after modal closes
            if self._chat_input:
                self._chat_input.focus_input()

        screen = ModelSelectorScreen(
            current_model=settings.model_name,
            current_provider=settings.model_provider,
            cli_profile_override=self._profile_override,
        )
        self.push_screen(screen, handle_result)

    async def _show_settings_screen(self) -> None:
        """Show interactive settings screen as a modal."""
        from bog_agents_cli.widgets.settings_screen import SettingsScreen

        def handle_result(changed: bool | None) -> None:
            """Handle the settings screen result."""
            if changed:
                self.call_later(self._reload_after_settings)
            if self._chat_input:
                self._chat_input.focus_input()

        screen = SettingsScreen()
        self.push_screen(screen, handle_result)

    async def _reload_after_settings(self) -> None:
        """Reload config caches after settings changes."""
        from bog_agents_cli.model_config import clear_caches

        clear_caches()
        settings.reload_from_environment()
        await self._mount_message(
            AppMessage("Settings updated. Configuration reloaded.")
        )

    async def _handle_logs_command(self) -> None:
        """Handle /logs — show log file path and recent errors."""
        from bog_agents_cli._debug import get_log_path

        await self._mount_message(UserMessage("/logs"))

        log_path = get_log_path()
        if not log_path.exists():
            await self._mount_message(
                AppMessage(
                    f"Log file: {log_path}\n(No logs yet — file will be created on first warning/error.)"
                )
            )
            return

        # Show path and tail of recent errors
        try:
            lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            # Filter to WARNING/ERROR/CRITICAL for the summary
            error_lines = [
                ln
                for ln in lines
                if any(lvl in ln for lvl in (" WARNING ", " ERROR ", " CRITICAL "))
            ]
            recent = error_lines[-20:] if error_lines else []
        except OSError:
            recent = []

        parts = [f"Log file: {log_path}"]
        if recent:
            parts.append(
                f"\nRecent warnings/errors ({len(recent)} of {len(error_lines)}):"
            )
            parts.append("```")
            parts.extend(recent)
            parts.append("```")
        else:
            parts.append("No warnings or errors recorded.")
        parts.append(
            "\nFor verbose logging, restart with: BOG_AGENTS_DEBUG=1 bog-agents"
        )

        await self._mount_message(AppMessage("\n".join(parts)))

    async def _handle_init_command(self) -> None:
        """Handle /init — generate AGENTS.md for the current repository."""
        from bog_agents_cli.project_utils import find_project_agent_md
        from bog_agents_cli.prompts import get_prompt

        await self._mount_message(UserMessage("/init"))

        project_root = settings.project_root
        if project_root is None:
            await self._mount_message(
                AppMessage(
                    "No project root detected. Run /init from within a git repository."
                )
            )
            return

        existing = find_project_agent_md(project_root)
        agents_md_path = project_root / "AGENTS.md"

        if any(p.name == "AGENTS.md" for p in existing):
            await self._mount_message(
                AppMessage(
                    f"AGENTS.md already exists at: {', '.join(str(p) for p in existing if p.name == 'AGENTS.md')}\n"
                    "Regenerating will overwrite it."
                )
            )

        default_prompt = (
            f"Analyze this repository at `{project_root}` and generate an `AGENTS.md` file.\n\n"
            "The AGENTS.md file provides persistent context that is loaded into your system "
            "prompt on every session. It helps you navigate and work with this codebase.\n\n"
            "Please:\n"
            "1. Scan the directory structure, key files (README, package manifests, config files)\n"
            "2. Identify the language(s), frameworks, build system, and test framework\n"
            "3. Understand the architecture and module organization\n"
            "4. Find coding conventions (linters, formatters, style guides)\n"
            "5. Identify common development commands (build, test, lint, run)\n\n"
            f"Then write the file to `{agents_md_path}` with these sections:\n\n"
            "```markdown\n"
            "# AGENTS.md\n\n"
            "## Project Overview\n"
            "Brief description, purpose, and tech stack.\n\n"
            "## Architecture\n"
            "Key modules/packages and how they relate.\n\n"
            "## Development\n"
            "### Setup\n"
            "How to install dependencies and get started.\n\n"
            "### Common Commands\n"
            "Build, test, lint, format, run commands.\n\n"
            "### Testing\n"
            "Test framework, how to run tests, test conventions.\n\n"
            "## Code Conventions\n"
            "Style guide, linting rules, naming conventions, patterns to follow.\n\n"
            "## Key Files\n"
            "Important entry points and configuration files.\n"
            "```\n\n"
            "Be thorough but concise. This file will be loaded on every session."
        )

        prompt = get_prompt("init", default_prompt)
        await self._mount_message(
            AppMessage(
                f"Analyzing repository and generating AGENTS.md at `{agents_md_path}`..."
            )
        )
        await self._send_prompt_to_agent(prompt)

    async def _handle_onboard_command(self) -> None:
        """Handle /onboard — interactive codebase tour."""
        from bog_agents_cli.code_intelligence_cli import generate_onboard_prompt
        from bog_agents_cli.prompts import get_prompt

        await self._mount_message(UserMessage("/onboard"))
        default_prompt = generate_onboard_prompt()
        prompt = get_prompt("onboard", default_prompt)
        await self._mount_message(AppMessage("Starting interactive codebase tour..."))
        await self._send_prompt_to_agent(prompt)

    # =========================================================================
    # /vars — secret / variable store
    # =========================================================================

    async def _handle_vars_command(self, command: str) -> None:
        """Handle /vars — manage secrets and configuration variables.

        Usage:
          /vars                   — list all variable names
          /vars list              — list all variable names
          /vars set KEY VALUE     — store a variable (prompts if VALUE omitted)
          /vars get KEY           — show the value of a variable (masked)
          /vars delete KEY        — remove a variable
          /vars show KEY          — show the raw value (unmasked; use carefully)

        Variables are referenced in prompts and pipelines as ``{{vars.KEY}}``.

        Args:
            command: Full slash command string.
        """
        from bog_agents_cli.vars_store import (
            delete_var,
            get_var,
            list_var_names,
            set_var,
            var_backend,
        )

        tail = command[len("/vars"):].strip()
        parts = tail.split(maxsplit=2)
        action = parts[0].lower() if parts else "list"

        # ---- list ----
        if action in ("list", ""):
            names = list_var_names()
            if not names:
                await self._mount_message(
                    AppMessage(
                        "No variables stored yet.\n"
                        "Use [bold]/vars set KEY VALUE[/bold] to add one.\n\n"
                        "Example: [dim]/vars set JIRA_API_KEY mytoken123[/dim]"
                    )
                )
                return

            lines = ["[bold]Stored variables[/bold] (values hidden)\n"]
            for name in names:
                backend = var_backend(name)
                icon = "🔒" if backend == "keyring" else "📄"
                lines.append(f"  {icon} [cyan]{name}[/cyan]  [dim]({backend})[/dim]")
            lines.append(
                "\n[dim]Reference in prompts/pipelines as [bold]{{vars.NAME}}[/bold][/dim]"
            )
            await self._mount_message(AppMessage("\n".join(lines)))

        # ---- set ----
        elif action == "set":
            if len(parts) < 2:
                await self._mount_message(
                    AppMessage("Usage: [bold]/vars set KEY VALUE[/bold]")
                )
                return
            key = parts[1]
            value = parts[2] if len(parts) > 2 else ""
            if not value:
                await self._mount_message(
                    AppMessage(
                        f"Usage: [bold]/vars set {key} <value>[/bold]\n"
                        "[dim]Tip: value is everything after the key name[/dim]"
                    )
                )
                return
            try:
                backend = set_var(key, value)
                icon = "🔒" if backend == "keyring" else "📄"
                await self._mount_message(
                    AppMessage(
                        f"{icon} [green]Set[/green] [cyan]{key}[/cyan] "
                        f"[dim]({backend})[/dim]"
                    )
                )
            except ValueError as exc:
                self.notify(str(exc), severity="error", timeout=4)

        # ---- get (masked) ----
        elif action == "get":
            if len(parts) < 2:
                await self._mount_message(AppMessage("Usage: [bold]/vars get KEY[/bold]"))
                return
            key = parts[1]
            try:
                value = get_var(key)
            except ValueError as exc:
                self.notify(str(exc), severity="error", timeout=4)
                return
            if value is None:
                self.notify(f"Variable '{key}' not found.", severity="warning", timeout=3)
                return
            masked = value[:2] + "*" * max(0, len(value) - 4) + value[-2:] if len(value) > 4 else "****"
            backend = var_backend(key)
            await self._mount_message(
                AppMessage(
                    f"[cyan]{key}[/cyan] = [yellow]{masked}[/yellow]  "
                    f"[dim]({backend} — use /vars show {key} to reveal)[/dim]"
                )
            )

        # ---- show (unmasked) ----
        elif action == "show":
            if len(parts) < 2:
                await self._mount_message(AppMessage("Usage: [bold]/vars show KEY[/bold]"))
                return
            key = parts[1]
            try:
                value = get_var(key)
            except ValueError as exc:
                self.notify(str(exc), severity="error", timeout=4)
                return
            if value is None:
                self.notify(f"Variable '{key}' not found.", severity="warning", timeout=3)
                return
            await self._mount_message(
                AppMessage(f"[cyan]{key}[/cyan] = [yellow]{value}[/yellow]")
            )

        # ---- delete ----
        elif action in ("delete", "del", "remove", "rm"):
            if len(parts) < 2:
                await self._mount_message(
                    AppMessage("Usage: [bold]/vars delete KEY[/bold]")
                )
                return
            key = parts[1]
            try:
                deleted = delete_var(key)
            except ValueError as exc:
                self.notify(str(exc), severity="error", timeout=4)
                return
            if deleted:
                await self._mount_message(
                    AppMessage(f"[green]Deleted[/green] [cyan]{key}[/cyan]")
                )
            else:
                self.notify(f"Variable '{key}' not found.", severity="warning", timeout=3)

        # ---- help / unknown ----
        else:
            await self._mount_message(
                AppMessage(
                    "[bold]/vars[/bold] — secret and variable store\n\n"
                    "  [cyan]/vars list[/cyan]            — list all variable names\n"
                    "  [cyan]/vars set KEY VALUE[/cyan]   — store a variable securely\n"
                    "  [cyan]/vars get KEY[/cyan]         — show masked value\n"
                    "  [cyan]/vars show KEY[/cyan]        — show raw value (unmasked)\n"
                    "  [cyan]/vars delete KEY[/cyan]      — remove a variable\n\n"
                    "[dim]Reference in prompts/pipelines: [bold]{{vars.KEY}}[/bold][/dim]"
                )
            )

    # =========================================================================
    # /image — multimodal image input
    # =========================================================================

    async def _handle_image_command(self, command: str) -> None:
        """Handle /image — attach an image file or paste from clipboard.

        Usage:
          /image                     — paste image from clipboard
          /image analyze <path>      — describe/analyze an image file
          /image to-code <path>      — convert screenshot to code
          /image paste               — explicit clipboard paste

        Args:
            command: Full slash command string (e.g. "/image analyze foo.png").
        """
        from bog_agents_cli.image_cli import (
            detect_image_in_input,
            format_image_info,
            is_image_file,
            parse_image_command,
        )

        tail = command[len("/image") :].strip()
        parsed = (
            parse_image_command(tail)
            if tail
            else {"action": "paste", "arg1": "", "arg2": ""}
        )
        action = parsed["action"]
        arg1 = parsed.get("arg1", "")

        # ------------------------------------------------------------------
        # Clipboard paste (default when no args given)
        # ------------------------------------------------------------------
        if action in ("paste", "") or (not tail):
            from bog_agents_cli.clipboard import read_clipboard_text

            clip = read_clipboard_text()
            if clip and detect_image_in_input(clip):
                # Clipboard contains a path reference to an image
                img_path = detect_image_in_input(clip)
                await self._submit_image_file(
                    img_path, "Analyze this image from clipboard"
                )
                return

            # Try reading raw image bytes from clipboard (platform-specific)
            img_bytes = _read_clipboard_image()
            if img_bytes:
                await self._submit_image_bytes(
                    img_bytes, "image/png", "Describe and analyze this image"
                )
                return

            self.notify(
                "No image found in clipboard. Use `/image analyze <path>` to attach a file.",
                severity="warning",
                timeout=4,
            )
            return

        # ------------------------------------------------------------------
        # File-based actions
        # ------------------------------------------------------------------
        path_str = arg1 or detect_image_in_input(tail) or ""
        if not path_str:
            await self._mount_message(
                AppMessage(
                    "Usage: [bold]/image analyze <path>[/bold] or [bold]/image to-code <path>[/bold]"
                )
            )
            return

        if not is_image_file(path_str):
            self.notify(
                f"Not a supported image: {path_str}", severity="warning", timeout=3
            )
            return

        info = format_image_info(path_str)
        extra = parsed.get("arg2", "")

        if action == "to-code":
            framework = extra or "appropriate framework"
            user_prompt = f"Convert this screenshot to working {framework} code. Output only the code."
        else:
            user_prompt = (
                f"Analyze this image and describe what you see in detail. "
                f"{'Focus on: ' + extra if extra else ''}"
            ).strip()

        await self._submit_image_file(path_str, user_prompt, info_line=info)

    async def _submit_image_file(
        self,
        path: str,
        user_prompt: str,
        info_line: str = "",
    ) -> None:
        """Load an image file and send it to the agent with a text prompt.

        Args:
            path: Filesystem path to the image.
            user_prompt: Text instruction for the agent.
            info_line: Optional human-readable info shown before sending.
        """
        import mimetypes
        from pathlib import Path

        p = Path(path)
        if not p.exists():  # noqa: ASYNC240  # sync stat is fast; no async Path available in this Textual context
            self.notify(f"Image not found: {path}", severity="error", timeout=3)
            return

        mime_type, _ = mimetypes.guess_type(str(p))
        mime_type = mime_type or "image/png"
        img_bytes = await asyncio.to_thread(p.read_bytes)
        await self._submit_image_bytes(
            img_bytes, mime_type, user_prompt, info_line=info_line or str(p)
        )

    async def _submit_image_bytes(
        self,
        img_bytes: bytes,
        mime_type: str,
        user_prompt: str,
        info_line: str = "image",
    ) -> None:
        """Encode image bytes as base64 and send a multimodal message to the agent.

        Args:
            img_bytes: Raw image bytes.
            mime_type: MIME type string (e.g. 'image/png').
            user_prompt: Accompanying text prompt.
            info_line: Display label shown in the chat.
        """
        import base64  # deferred import keeps startup fast

        b64 = base64.standard_b64encode(img_bytes).decode()

        await self._mount_message(UserMessage(f"[Image: {info_line}] {user_prompt}"))

        # Build Anthropic-compatible vision content block
        content = [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": mime_type,
                    "data": b64,
                },
            },
            {"type": "text", "text": user_prompt},
        ]

        # _send_prompt_to_agent accepts str; for multimodal we serialize as a
        # structured marker the agent layer recognises when present.
        import json

        structured = json.dumps({"__multimodal__": True, "content": content})
        await self._send_prompt_to_agent(structured)

    # =========================================================================
    # /pr — pull request management
    # =========================================================================

    async def _handle_pr_command(self, command: str) -> None:
        """Handle /pr — create, list, or review pull requests.

        Usage:
          /pr              — list open PRs
          /pr list         — list open PRs
          /pr create [title] — create a PR from the current branch
          /pr review <num>  — show review comments for PR #num
          /pr describe      — auto-generate a PR description from git log
          /pr conflicts     — show and help resolve merge conflicts

        Args:
            command: Full slash command string.
        """
        from bog_agents_cli.pr_cli import (
            PRInfo,
            generate_conflict_resolution_prompt,
            generate_pr_prompt,
            parse_pr_command,
        )

        tail = command[len("/pr") :].strip()
        parsed = parse_pr_command(tail)
        action = parsed["action"]
        argument = parsed.get("argument", "")

        if action in ("list", ""):
            prompt = (
                "List all open pull requests for the current repository. "
                "Show the PR number, title, author, and status. "
                "Use `gh pr list` or the GitHub API."
            )
            await self._mount_message(UserMessage("/pr list"))

        elif action == "create":
            title = argument or "auto-generated title"
            # Get current branch name
            branch = await _get_current_git_branch()
            info = PRInfo(
                number=0,
                title=title,
                head_branch=branch or "",
            )
            prompt = generate_pr_prompt(info)
            await self._mount_message(UserMessage(f"/pr create {title}".strip()))

        elif action == "review":
            if not argument:
                self.notify("Usage: /pr review <number>", severity="warning", timeout=3)
                return
            prompt = (
                f"Show the review comments and status for pull request #{argument}. "
                "Summarize the feedback and list any unresolved threads."
            )
            await self._mount_message(UserMessage(f"/pr review {argument}"))

        elif action == "describe":
            prompt = (
                "Auto-generate a clear, concise pull request description based on "
                "the current git log and staged diff. Use conventional commit style. "
                "Output: title line, blank line, bullet-point summary of changes."
            )
            await self._mount_message(UserMessage("/pr describe"))

        elif action in ("conflicts", "conflict"):
            prompt = generate_conflict_resolution_prompt()
            await self._mount_message(UserMessage("/pr conflicts"))

        else:
            prompt = (
                f"Handle the following pull request action: {action} {argument}. "
                "Use git and GitHub CLI (`gh`) commands as needed."
            )
            await self._mount_message(UserMessage(f"/pr {action} {argument}".strip()))

        await self._send_prompt_to_agent(prompt)

    async def _show_mcp_viewer(self) -> None:
        """Show read-only MCP server/tool viewer as a modal screen."""
        from bog_agents_cli.widgets.mcp_viewer import MCPViewerScreen

        screen = MCPViewerScreen(server_info=self._mcp_server_info or [])

        def handle_result(result: None) -> None:  # noqa: ARG001
            if self._chat_input:
                self._chat_input.focus_input()

        self.push_screen(screen, handle_result)

    async def _show_thread_selector(self) -> None:
        """Show interactive thread selector as a modal screen."""
        from bog_agents_cli.sessions import get_cached_threads, get_thread_limit

        current = self._session_state.thread_id if self._session_state else None
        thread_limit = get_thread_limit()

        initial_threads = get_cached_threads(limit=thread_limit)

        def handle_result(result: str | None) -> None:
            """Handle the thread selector result."""
            if result is not None:
                self.call_later(self._resume_thread, result)
            if self._chat_input:
                self._chat_input.focus_input()

        screen = ThreadSelectorScreen(
            current_thread=current,
            thread_limit=thread_limit,
            initial_threads=initial_threads,
        )
        self.push_screen(screen, handle_result)

    def _update_welcome_banner(
        self,
        thread_id: str,
        *,
        missing_message: str,
        warn_if_missing: bool,
    ) -> None:
        """Update the welcome banner thread ID when the banner is mounted.

        Args:
            thread_id: Thread ID to display on the banner.
            missing_message: Log message template when banner is missing.
            warn_if_missing: Whether to log missing-banner cases at warning level.
        """
        try:
            banner = self.query_one("#welcome-banner", WelcomeBanner)
            banner.update_thread_id(thread_id)
        except NoMatches:
            if warn_if_missing:
                logger.warning(missing_message, thread_id)
            else:
                logger.debug(missing_message, thread_id)

    async def _resume_thread(self, thread_id: str) -> None:
        """Resume a previously saved thread.

        Fetches the selected thread history, then atomically switches UI state.
        Prefetching first avoids clearing the active chat when history loading
        fails.

        Args:
            thread_id: The thread ID to resume.
        """
        if not self._agent:
            await self._mount_message(
                AppMessage("Cannot switch threads: no active agent")
            )
            return

        if not self._session_state:
            await self._mount_message(
                AppMessage("Cannot switch threads: no active session")
            )
            return

        # Skip if already on this thread
        if self._session_state.thread_id == thread_id:
            await self._mount_message(AppMessage(f"Already on thread: {thread_id}"))
            return

        if self._thread_switching:
            await self._mount_message(AppMessage("Thread switch already in progress."))
            return

        # Save previous state for rollback on failure
        prev_thread_id = self._lc_thread_id
        prev_session_thread = self._session_state.thread_id
        self._thread_switching = True
        if self._chat_input:
            self._chat_input.set_cursor_active(active=False)

        prefetched_history: list[MessageData] | None = None
        try:
            self._update_status(f"Loading thread: {thread_id}")
            prefetched_history = await self._fetch_thread_history_data(thread_id)

            # Clear conversation (similar to /clear, without creating a new thread)
            self._pending_messages.clear()
            self._queued_widgets.clear()
            await self._clear_messages()
            if self._token_tracker:
                self._token_tracker.reset()
            self._update_status("")

            # Switch to the selected thread
            self._session_state.thread_id = thread_id
            self._lc_thread_id = thread_id
            metadata = await self._current_thread_metadata()
            label = metadata.get("label")
            if not isinstance(label, str) or not label.strip():
                self._session_name = None

            self._update_welcome_banner(
                thread_id,
                missing_message="Welcome banner not found during thread switch to %s",
                warn_if_missing=False,
            )

            # Load thread history
            await self._load_thread_history(
                thread_id=thread_id,
                preloaded_data=prefetched_history,
            )
        except Exception as exc:
            if prefetched_history is None:
                logger.exception("Failed to prefetch history for thread %s", thread_id)
                await self._mount_message(
                    AppMessage(
                        f"Failed to switch to thread {thread_id}: {exc}. "
                        "Use /threads to try again."
                    )
                )
                return
            logger.exception("Failed to switch to thread %s", thread_id)
            # Restore previous thread IDs so the user can retry
            self._session_state.thread_id = prev_session_thread
            self._lc_thread_id = prev_thread_id
            self._update_welcome_banner(
                prev_session_thread,
                missing_message=(
                    "Welcome banner not found during rollback to thread %s; "
                    "banner may display stale thread ID"
                ),
                warn_if_missing=True,
            )
            rollback_restore_failed = False
            # Attempt to restore the previous thread's visible history
            try:
                await self._clear_messages()
                await self._load_thread_history(thread_id=prev_session_thread)
            except Exception:  # Resilient session state saving
                rollback_restore_failed = True
                msg = (
                    "Could not restore previous thread history after failed "
                    "switch to %s"
                )
                logger.warning(msg, thread_id, exc_info=True)
            error_message = f"Failed to switch to thread {thread_id}: {exc}."
            if rollback_restore_failed:
                error_message += " Previous thread history could not be restored."
            error_message += " Use /threads to try again."
            await self._mount_message(AppMessage(error_message))
        finally:
            self._thread_switching = False
            self._update_status("")
            if self._chat_input:
                self._chat_input.set_cursor_active(active=not self._agent_running)

    async def _switch_model(
        self,
        model_spec: str,
        *,
        extra_kwargs: dict[str, Any] | None = None,
    ) -> None:
        """Switch to a new model, preserving conversation history.

        This requires a server-backed interactive session. It sets a model
        override that `ConfigurableModelMiddleware` picks up on the next
        invocation, so the conversation thread stays intact and no server
        restart is required.

        Args:
            model_spec: The model specification to switch to.

                Can be in `provider:model` format
                (e.g., `'anthropic:claude-sonnet-4-5'`) or just the model name
                for auto-detection.
            extra_kwargs: Extra constructor kwargs from `--model-params`.
        """
        logger.info("Switching model to %s", model_spec)

        if self._model_switching:
            await self._mount_message(AppMessage("Model switch already in progress."))
            return

        from bog_agents_cli.model_config import (
            get_credential_env_var,
            has_provider_credentials,
        )

        self._model_switching = True
        try:
            # Defensively strip leading colon in case of empty provider,
            # treat ":claude-opus-4-6" as "claude-opus-4-6"
            model_spec = model_spec.removeprefix(":")

            if not self._remote_agent():
                await self._mount_message(
                    ErrorMessage("Model switching requires a server-backed session.")
                )
                return

            parsed = ModelSpec.try_parse(model_spec)
            if parsed:
                provider: str | None = parsed.provider
                model_name = parsed.model
            else:
                model_name = model_spec
                provider = detect_provider(model_spec)

            # Check credentials
            has_creds = has_provider_credentials(provider) if provider else None
            if has_creds is False and provider is not None:
                env_var = get_credential_env_var(provider)
                detail = (
                    f"{env_var} is not set or is empty"
                    if env_var
                    else (
                        f"provider '{provider}' is not recognized. "
                        "Add it to ~/.bog-agents/config.toml with an "
                        "api_key_env field"
                    )
                )
                await self._mount_message(
                    ErrorMessage(f"Missing credentials: {detail}")
                )
                return
            if has_creds is None and provider:
                logger.debug(
                    "Credentials for provider '%s' cannot be verified;"
                    " proceeding anyway",
                    provider,
                )

            # Check if already using this exact model
            if model_name == settings.model_name and (
                not provider or provider == settings.model_provider
            ):
                current = f"{settings.model_provider}:{settings.model_name}"
                await self._mount_message(AppMessage(f"Already using {current}"))
                return

            # Build the provider:model spec for the configurable middleware.
            display = model_spec
            if provider and not parsed:
                display = f"{provider}:{model_name}"

            try:
                create_model(
                    display,
                    extra_kwargs=extra_kwargs,
                    profile_overrides=self._profile_override,
                ).apply_to_settings()
            except Exception as exc:
                logger.exception("Failed to resolve model metadata for %s", display)
                await self._mount_message(
                    ErrorMessage(f"Failed to switch model: {exc}")
                )
                return

            # Set the model override for ConfigurableModelMiddleware.
            # The next stream call passes CLIContext via context= and the
            # middleware swaps the model per-invocation — no graph recreation.
            self._model_override = display
            self._model_params_override = extra_kwargs

            if self._status_bar:
                self._status_bar.set_model(
                    provider=settings.model_provider or "",
                    model=settings.model_name or "",
                )

            if not await asyncio.to_thread(save_recent_model, display):
                await self._mount_message(
                    ErrorMessage(
                        "Model switched for this session, but could not save "
                        "preference. Check permissions for ~/.bog-agents/"
                    )
                )
            else:
                await self._mount_message(AppMessage(f"Switched to {display}"))
            logger.info("Model switched to %s (via configurable middleware)", display)

            # Scroll to bottom so the confirmation message is visible
            def _scroll_after_switch() -> None:
                try:
                    chat = self.query_one("#chat", VerticalScroll)
                    if chat.max_scroll_y > 0:
                        chat.scroll_end(animate=False)
                except NoMatches:
                    pass

            self.call_after_refresh(_scroll_after_switch)
        finally:
            self._model_switching = False

    async def _set_default_model(self, model_spec: str) -> None:
        """Set the default model in config without switching the current session.

        Updates `[models].default` in `~/.bog-agents/config.toml` so that
        future CLI launches use this model. Does not affect the running session.

        Args:
            model_spec: The model specification (e.g., `'anthropic:claude-opus-4-6'`).
        """
        from bog_agents_cli.model_config import save_default_model

        model_spec = model_spec.removeprefix(":")

        parsed = ModelSpec.try_parse(model_spec)
        if not parsed:
            provider = detect_provider(model_spec)
            if provider:
                model_spec = f"{provider}:{model_spec}"

        if await asyncio.to_thread(save_default_model, model_spec):
            await self._mount_message(AppMessage(f"Default model set to {model_spec}"))
        else:
            await self._mount_message(
                ErrorMessage(
                    "Could not save default model. Check permissions for ~/.bog-agents/"
                )
            )

    async def _clear_default_model(self) -> None:
        """Remove the default model from config.

        After clearing, future launches fall back to `[models].recent` or
        environment auto-detection.
        """
        from bog_agents_cli.model_config import clear_default_model

        if await asyncio.to_thread(clear_default_model):
            await self._mount_message(
                AppMessage(
                    "Default model cleared. "
                    "Future launches will use recent model or auto-detect."
                )
            )
        else:
            await self._mount_message(
                ErrorMessage(
                    "Could not clear default model. "
                    "Check permissions for ~/.bog-agents/"
                )
            )


@dataclass(frozen=True)
class AppResult:
    """Result from running the Textual application.

    Attributes:
        return_code: Exit code (0 for success, non-zero for error).
        thread_id: The final thread ID at shutdown. May differ from the
            initial thread ID if the user switched threads via `/threads`.
        session_stats: Cumulative usage stats across all turns in the session.
    """

    return_code: int
    thread_id: str | None
    session_stats: SessionStats = field(default_factory=SessionStats)


async def run_textual_app(
    *,
    agent: Any = None,  # noqa: ANN401
    assistant_id: str | None = None,
    backend: CompositeBackend | None = None,
    auto_approve: bool = False,
    auto_commit: bool = False,
    cwd: str | Path | None = None,
    thread_id: str | None = None,
    initial_prompt: str | None = None,
    mcp_server_info: list[MCPServerInfo] | None = None,
    profile_override: dict[str, Any] | None = None,
    server_proc: ServerProcess | None = None,
    server_kwargs: dict[str, Any] | None = None,
    mcp_preload_kwargs: dict[str, Any] | None = None,
) -> AppResult:
    """Run the Textual application.

    When `server_kwargs` is provided (and `agent` is `None`), the app starts
    immediately with a "Connecting..." banner and launches the server in the
    background.  Server cleanup is handled automatically after the app exits.

    Args:
        agent: Pre-configured LangGraph agent (optional).
        assistant_id: Agent identifier for memory storage.
        backend: Backend for file operations.
        auto_approve: Whether to start with auto-approve enabled.
        auto_commit: Whether to auto-commit git changes after each agent turn.
        cwd: Current working directory to display.
        thread_id: Optional thread ID for session persistence.
        initial_prompt: Optional prompt to auto-submit when session starts.
        mcp_server_info: MCP server metadata for the `/mcp` viewer.
        profile_override: Extra profile fields from `--profile-override`,
            retained so later profile-aware behavior stays consistent with
            the CLI override, including model selection details, compaction
            budget display, and on-demand `create_model()` calls such as
            `/compact`.
        server_proc: LangGraph server process for the interactive session.
        server_kwargs: Kwargs for deferred `start_server_and_get_agent` call.
        mcp_preload_kwargs: Kwargs for concurrent MCP metadata preload.

    Returns:
        An `AppResult` with the return code and final thread ID.
    """
    app = BogAgentsApp(
        agent=agent,
        assistant_id=assistant_id,
        backend=backend,
        auto_approve=auto_approve,
        auto_commit=auto_commit,
        cwd=cwd,
        thread_id=thread_id,
        initial_prompt=initial_prompt,
        mcp_server_info=mcp_server_info,
        profile_override=profile_override,
        server_proc=server_proc,
        server_kwargs=server_kwargs,
        mcp_preload_kwargs=mcp_preload_kwargs,
    )
    try:
        await app.run_async()
    finally:
        # Guarantee server cleanup regardless of how the app exits.
        # Covers both the pre-started server_proc path and the deferred
        # server_kwargs path (where the background worker sets _server_proc).
        if app._server_proc is not None:
            app._server_proc.stop()

    return AppResult(
        return_code=app.return_code or 0,
        thread_id=app._lc_thread_id,
        session_stats=app._session_stats,
    )
