"""Textual UI adapter for agent execution."""
# This module has complex streaming logic ported from execution.py

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from rich.console import Console

    from bog_agents_cli.ask_user import AskUserWidgetResult, Question

from langchain.agents.middleware.human_in_the_loop import (
    ApproveDecision,
    EditDecision,
    HITLRequest,
    RejectDecision,
)
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.types import Command, Interrupt
from pydantic import TypeAdapter, ValidationError
from rich.text import Text

from bog_agents_cli._debug import configure_debug_logging
from bog_agents_cli.ask_user import AskUserRequest
from bog_agents_cli.config import get_glyphs, settings
from bog_agents_cli.configurable_model import CLIContext  # noqa: TC001
from bog_agents_cli.file_ops import FileOpTracker
from bog_agents_cli.hooks import dispatch_hook
from bog_agents_cli.input import MediaTracker, parse_file_mentions
from bog_agents_cli.media_utils import create_multimodal_content
from bog_agents_cli.tool_display import format_tool_message_content
from bog_agents_cli.widgets.messages import (
    AppMessage,
    AssistantMessage,
    DiffMessage,
    SummarizationMessage,
    ToolCallMessage,
)

logger = logging.getLogger(__name__)
configure_debug_logging(logger)

_git_branch_cache: dict[str, tuple[float, str | None]] = {}
"""Cache git-branch lookups by cwd as `(monotonic_ts, branch)` with a short TTL.

The TTL exists because a mid-session branch switch (an explicit `/branch`, a
`WorktreeMiddleware` switch, or a shell-tool `git checkout`) does not change the
process cwd, so a permanently-memoized entry would misattribute every later
checkpoint's `git_branch` metadata to the original branch.
"""

_GIT_BRANCH_CACHE_TTL_SECONDS = 2.5
"""Max age of a cached branch before `_get_git_branch` re-runs `git rev-parse`."""


def _scan_streamed_json(buffer: dict[str, Any], text: str) -> None:
    """Advance the streamed-args JSON structure scanner over one fragment.

    Tracks bracket depth and string/escape state across fragments so
    completeness of a streamed JSON object/array is detected in O(len(text))
    per fragment instead of re-parsing the whole accumulated prefix. Inside
    strings (the common case — file content in a `write_file` call) it jumps
    between structural characters with `str.find`, so scanning runs at
    near-C speed.

    Args:
        buffer: The tool-call accumulation buffer holding scanner state
            (`scan_depth` / `scan_in_string` / `scan_escape` keys).
        text: The new fragment to scan.
    """
    depth: int = buffer.get("scan_depth", 0)
    in_string: bool = buffer.get("scan_in_string", False)
    escape: bool = buffer.get("scan_escape", False)
    i = 0
    n = len(text)
    while i < n:
        if escape:
            escape = False
            i += 1
            continue
        if in_string:
            quote = text.find('"', i)
            backslash = text.find("\\", i)
            if quote == -1 and backslash == -1:
                i = n
                continue
            if backslash == -1 or (quote != -1 and quote < backslash):
                in_string = False
                i = quote + 1
            else:
                escape = True
                i = backslash + 1
            continue
        ch = text[i]
        if ch == '"':
            in_string = True
        elif ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
            if depth <= 0:
                # Top-level value closed: the args are structurally complete.
                # Anything after this point is left for json.loads to judge.
                buffer["args_complete"] = True
                break
        i += 1
    buffer["scan_depth"] = depth
    buffer["scan_in_string"] = in_string
    buffer["scan_escape"] = escape


def _append_streamed_args(buffer: dict[str, Any], chunk_args: str) -> None:
    """Accumulate one streamed tool-call args fragment into `buffer`.

    Providers stream tool args as many small `input_json_delta` fragments; the
    historical behavior re-joined every accumulated fragment and attempted a
    full `json.loads` per fragment — O(n^2) in args size, which froze the UI
    for seconds on a large `write_file` (v5 PERF-2). This helper keeps the
    fragments as a list and materializes `buffer["args"]` (the joined string
    the parse site consumes) only when an incremental structure scanner says
    the JSON value is complete, making the whole accumulation O(n).

    Two conservative fallbacks preserve the historical behavior exactly:

    - Args whose first non-whitespace character is not `{`/`[` (a bare JSON
      scalar) can't be depth-scanned, so they revert to join-and-expose per
      fragment. Scalar args are tiny, so the quadratic cost is irrelevant.
    - If a structurally complete value fails to parse (malformed JSON — the
      parse site leaves the buffer in place), any further fragment also
      reverts to join-and-expose per fragment.

    Args:
        buffer: The tool-call accumulation buffer (from `tool_call_buffers`).
        chunk_args: The non-empty args fragment from this chunk.
    """
    parts: list[str] = buffer.setdefault("args_parts", [])
    # Some providers resend the same delta on reconnect; dropping consecutive
    # duplicates matches the pre-existing accumulation behavior.
    if parts and chunk_args == parts[-1]:
        return
    if buffer.get("args_complete") and buffer.get("scan_mode") == "structure":
        # Extra data after a complete value means the earlier parse failed
        # (a successful parse pops the buffer). Fall back to legacy mode.
        buffer["scan_mode"] = "scalar"
    parts.append(chunk_args)

    mode = buffer.get("scan_mode")
    if mode is None:
        # Decide how to detect completeness from the first non-whitespace
        # character. Until one arrives, the args stay unset (nothing to parse).
        stripped = chunk_args.lstrip()
        if not stripped:
            return
        mode = "structure" if stripped[0] in "{[" else "scalar"
        buffer["scan_mode"] = mode

    if mode == "structure":
        _scan_streamed_json(buffer, chunk_args)
        if buffer.get("args_complete"):
            buffer["args"] = "".join(parts)
        return
    # Scalar/fallback mode: legacy join-and-expose per fragment.
    buffer["args"] = "".join(parts)


def _find_todos_payload(node: object) -> list[object] | None:
    """Find a todo list payload within a streamed update object.

    Args:
        node: Arbitrary update payload node.

    Returns:
        Todo list payload if present, else `None`.
    """
    if isinstance(node, dict):
        todos = node.get("todos")
        if isinstance(todos, list):
            return todos
        for value in node.values():
            nested = _find_todos_payload(value)
            if nested is not None:
                return nested
    if isinstance(node, list):
        for value in node:
            nested = _find_todos_payload(value)
            if nested is not None:
                return nested
    return None


def _render_todos_text(todos: list[object]) -> Text:
    """Render streamed todo-state updates as Rich text.

    Args:
        todos: Todo items from streamed graph state.

    Returns:
        Styled text suitable for an `AppMessage`.
    """
    glyphs = get_glyphs()
    completed = 0
    active = 0
    pending = 0
    lines: list[Text] = []

    for raw_item in todos:
        if isinstance(raw_item, dict):
            todo_item = cast("dict[str, object]", raw_item)
            raw_content = todo_item.get("content")
            raw_status = todo_item.get("status")
            content = str(raw_content if raw_content is not None else todo_item).strip()
            content = content or "(empty todo)"
            status = (
                str(raw_status if raw_status is not None else "pending").strip().lower()
            )
        else:
            content = str(raw_item).strip() or "(empty todo)"
            status = "pending"

        if status == "completed":
            completed += 1
            prefix = Text(f"{glyphs.checkmark} done  ", style="green")
            # Strike-through + dim so the item visibly "clears" out of the
            # active reading order without being deleted from the scrollback.
            body = Text(content, style="dim strike")
        elif status == "in_progress":
            active += 1
            prefix = Text(f"{glyphs.circle_filled} active ", style="yellow")
            body = Text(content)
        else:
            pending += 1
            prefix = Text(f"{glyphs.circle_empty} todo  ", style="dim")
            body = Text(content)

        line = Text("    ")
        line.append_text(prefix)
        line.append_text(body)
        lines.append(line)

    # When everything is done, collapse into a one-liner. Keeps the chat
    # readable instead of leaving a stale 10-line list pinned in view after
    # the agent has finished a long task.
    if todos and active == 0 and pending == 0 and completed > 0:
        rendered = Text()
        rendered.append(f"{glyphs.checkmark} ", style="green")
        rendered.append(
            f"All {completed} todo{'s' if completed != 1 else ''} complete",
            style="green",
        )
        return rendered

    header = Text("Todo list", style="bold cyan")
    stats = Text("  ")
    parts: list[tuple[str, str | None]] = []
    if active:
        parts.append((f"{active} active", "yellow"))
    if pending:
        parts.append((f"{pending} pending", None))
    if completed:
        parts.append((f"{completed} done", "green"))
    if not parts:
        parts.append(("no items", "dim"))
    for index, (label, style) in enumerate(parts):
        if index:
            stats.append(" | ", style="dim")
        stats.append(label, style=style)

    rendered = Text()
    rendered.append_text(header)
    rendered.append("\n")
    rendered.append_text(stats)
    if lines:
        rendered.append("\n\n")
        for index, line in enumerate(lines):
            if index:
                rendered.append("\n")
            rendered.append_text(line)
    return rendered


@dataclass
class ModelStats:
    """Token stats for a single model within a session.

    Attributes:
        request_count: Number of LLM API requests made to this model.
        input_tokens: Cumulative input tokens sent to this model.
        output_tokens: Cumulative output tokens received from this model.
    """

    request_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class SessionStats:
    """Stats accumulated over a single agent turn (or full session).

    Attributes:
        request_count: Total LLM API requests made (each chunk with
            usage_metadata counts as one completed request).
        input_tokens: Cumulative input tokens across all LLM requests.
        output_tokens: Cumulative output tokens across all LLM requests.
        wall_time_seconds: Wall-clock duration from stream start to end.
        per_model: Per-model breakdown keyed by model name.
            Populated only when `record_request` receives a non-empty
            `model_name`. Empty dict means no named-model requests were
            recorded; `print_usage_table` omits the model table in that case and
            shows only the wall-time line (if applicable).
    """

    request_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    wall_time_seconds: float = 0.0
    per_model: dict[str, ModelStats] = field(default_factory=dict)
    # ROADMAP #66: the turn's file operation records (live reference to the
    # tracker's completed list), consumed by the changes tray at turn end.
    file_records: list[Any] = field(default_factory=list)

    def record_request(
        self,
        model_name: str,
        input_toks: int,
        output_toks: int,
    ) -> None:
        """Accumulate token counts for one completed LLM request.

        Updates both the session totals and the per-model breakdown.

        Args:
            model_name: The model that served this request (used as the
                per-model key). Pass an empty string to skip the per-model
                breakdown for this request.
            input_toks: Input tokens for this request.
            output_toks: Output tokens for this request.
        """
        self.request_count += 1
        self.input_tokens += input_toks
        self.output_tokens += output_toks
        if model_name:
            entry = self.per_model.setdefault(model_name, ModelStats())
            entry.request_count += 1
            entry.input_tokens += input_toks
            entry.output_tokens += output_toks

    def merge(self, other: SessionStats) -> None:
        """Merge another `SessionStats` into this one (mutates *self*).

        Used to accumulate per-turn stats into a session-level total.

        Args:
            other: The stats to fold in.
        """
        self.request_count += other.request_count
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.wall_time_seconds += other.wall_time_seconds
        for model, ms in other.per_model.items():
            entry = self.per_model.setdefault(model, ModelStats())
            entry.request_count += ms.request_count
            entry.input_tokens += ms.input_tokens
            entry.output_tokens += ms.output_tokens


def format_token_count(count: int) -> str:
    """Format a token count into a human-readable short string.

    Args:
        count: Number of tokens.

    Returns:
        Formatted string like `"12.5K"`, `"1.2M"`, or `"500"`.
    """
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    if count >= 1000:
        return f"{count / 1000:.1f}K"
    return str(count)


def print_usage_table(
    stats: SessionStats,
    wall_time: float,
    console: Console,
) -> None:
    """Print a model-usage stats table to a Rich console.

    When the session spans multiple models each gets its own row with a
    totals row appended; single-model sessions show one row.

    Args:
        stats: Cumulative session stats.
        wall_time: Total wall-clock time in seconds.
        console: Rich console for output.
    """
    from rich.table import Table

    has_time = wall_time >= 0.1
    if not (stats.request_count or stats.input_tokens or has_time):
        return

    if stats.per_model:
        multi_model = len(stats.per_model) > 1

        table = Table(
            show_header=True,
            header_style="bold",
            box=None,
            padding=(0, 2, 0, 0),
            show_edge=False,
        )
        table.add_column("Model", style="dim")
        table.add_column("Reqs", justify="right", style="dim")
        table.add_column("InputTok", justify="right", style="dim")
        table.add_column("OutputTok", justify="right", style="dim")

        if multi_model:
            for model_name, ms in stats.per_model.items():
                table.add_row(
                    model_name,
                    str(ms.request_count),
                    format_token_count(ms.input_tokens),
                    format_token_count(ms.output_tokens),
                )
            table.add_row(
                "Total",
                str(stats.request_count),
                format_token_count(stats.input_tokens),
                format_token_count(stats.output_tokens),
            )
        else:
            model_label = next(iter(stats.per_model))
            table.add_row(
                model_label,
                str(stats.request_count),
                format_token_count(stats.input_tokens),
                format_token_count(stats.output_tokens),
            )

        console.print()
        console.print("[bold]Usage Stats[/bold]")
        console.print(table)
    if has_time:
        console.print()
        console.print(f"[dim]Agent active  {wall_time:.1f}s[/dim]")


# Type alias matching HITLResponse["decisions"] element type
HITLDecision = ApproveDecision | EditDecision | RejectDecision

_HITL_REQUEST_ADAPTER = TypeAdapter(HITLRequest)
_ASK_USER_INTERRUPT_ADAPTER = TypeAdapter(AskUserRequest)
"""Validator for incoming `ask_user` interrupt payloads."""


def _get_git_branch() -> str | None:
    """Return the current git branch name, or None if not in a repo.

    Results are cached per-cwd for a short TTL (`_GIT_BRANCH_CACHE_TTL_SECONDS`)
    so a mid-session branch switch that never changes the process cwd (a shell
    `git checkout`, a `WorktreeMiddleware` switch) is picked up within a few
    seconds rather than being memoized for the life of the process.

    Returns:
        The current branch name, or None if not in a git repo / git is
        unavailable.
    """
    import subprocess  # noqa: S404

    try:
        cwd = str(Path.cwd())
    except OSError:
        logger.debug("Could not determine cwd for git branch lookup", exc_info=True)
        return None
    now = time.monotonic()
    cached = _git_branch_cache.get(cwd)
    if cached is not None and now - cached[0] < _GIT_BRANCH_CACHE_TTL_SECONDS:
        return cached[1]

    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        if result.returncode == 0:
            branch = result.stdout.strip() or None
            _git_branch_cache[cwd] = (now, branch)
            return branch
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        logger.debug("Could not determine git branch", exc_info=True)
    _git_branch_cache[cwd] = (now, None)
    return None


def _invalidate_git_branch_cache() -> None:
    """Drop the cached branch for the current cwd so the next lookup re-runs git.

    Called after an explicit in-CLI branch switch (`/branch create|switch`) so the
    status bar reflects the new branch immediately rather than waiting out the TTL.
    """
    try:
        cwd = str(Path.cwd())
    except OSError:
        _git_branch_cache.clear()
        return
    _git_branch_cache.pop(cwd, None)


def _build_stream_config(
    thread_id: str,
    assistant_id: str | None,
) -> dict[str, Any]:
    """Build the LangGraph stream config dict.

    The `thread_id` in `configurable` is automatically propagated as run
    metadata by LangGraph, so it can be used for LangSmith filtering without
    a separate metadata key. Includes the current working directory (`cwd`)
    and git branch in metadata when available.

    Args:
        thread_id: The CLI session thread identifier.
        assistant_id: The agent/assistant identifier, if any.

    Returns:
        Config dict with `configurable` and `metadata` keys.
    """
    try:
        cwd = str(Path.cwd())
    except OSError:
        logger.warning("Could not determine working directory", exc_info=True)
        cwd = ""
    metadata: dict[str, str] = {"cwd": cwd} if cwd else {}
    if assistant_id:
        metadata.update(
            {
                "assistant_id": assistant_id,
                "agent_name": assistant_id,
                "updated_at": datetime.now(UTC).isoformat(),
            }
        )
    branch = _get_git_branch()
    if branch:
        metadata["git_branch"] = branch
    return {
        "configurable": {"thread_id": thread_id},
        "metadata": metadata,
    }


def _is_summarization_chunk(metadata: dict | None) -> bool:
    """Check if a message chunk is from summarization middleware.

    The summarization model is invoked with
    `config={"metadata": {"lc_source": "summarization"}}`
    (see `langchain.agents.middleware.summarization`), which
    LangChain's callback system merges into the stream metadata dict.

    Args:
        metadata: The metadata dict from the stream chunk.

    Returns:
        Whether the chunk is from summarization and should be filtered.
    """
    if metadata is None:
        return False
    return metadata.get("lc_source") == "summarization"


class TextualUIAdapter:
    """Adapter for rendering agent output to Textual widgets.

    This adapter provides an abstraction layer between the agent execution and the
    Textual UI, allowing streaming output to be rendered as widgets.
    """

    _mount_message: Callable[..., Awaitable[None]]
    """Async callback to mount a message widget to the chat."""

    _update_status: Callable[[str], None]
    """Callback to update the status bar text."""

    _request_approval: Callable[..., Awaitable[Any]]
    """Async callback that returns a Future for HITL approval."""

    _on_auto_approve_enabled: Callable[[], None] | None
    """Callback invoked when auto-approve is enabled via the HITL approval menu.

    Fired when the user selects "Auto-approve all" from an approval dialog,
    allowing the app to sync its status bar and session state.
    """

    _request_ask_user: (
        Callable[
            [list[Question]],
            Awaitable[asyncio.Future[AskUserWidgetResult] | None],
        ]
        | None
    )
    """Async callback for `ask_user` interrupts.

    When awaited, returns a `Future` that resolves to user answers.
    """

    _scroll_to_bottom: Callable[[], None] | None
    """Callback to scroll chat to bottom."""

    _set_spinner: Callable[[str | None], Awaitable[None]] | None
    """Callback to show/hide loading spinner.

    Pass `None` to hide, or a status string to show.
    """

    _set_active_message: Callable[[str | None], None] | None
    """Callback to set the active streaming message ID (pass `None` to clear)."""

    _sync_message_content: Callable[[str, str], None] | None
    """Callback to sync final message content back to the store after streaming."""

    _current_tool_messages: dict[str, ToolCallMessage]
    """Map of tool call IDs to their message widgets."""

    _token_tracker: Any
    """Token usage tracker for displaying counts."""

    silent_tool_output: bool
    """When True, render tool calls as a single dim line in the chat instead
    of a full ToolCallMessage widget. Tool details still flow through the
    log file (`~/.bog-agents/cli.log`). Toggle via `/silent` and `/verbose`.
    """

    def __init__(
        self,
        mount_message: Callable[..., Awaitable[None]],
        update_status: Callable[[str], None],
        request_approval: Callable[..., Awaitable[Any]],
        on_auto_approve_enabled: Callable[[], None] | None = None,
        scroll_to_bottom: Callable[[], None] | None = None,
        set_spinner: Callable[[str | None], Awaitable[None]] | None = None,
        set_active_message: Callable[[str | None], None] | None = None,
        sync_message_content: Callable[[str, str], None] | None = None,
        request_ask_user: (
            Callable[
                [list[Question]],
                Awaitable[asyncio.Future[AskUserWidgetResult] | None],
            ]
            | None
        ) = None,
    ) -> None:
        """Initialize the adapter.

        Args:
            mount_message: Async callable to mount a message widget.
            update_status: Callable to update the status bar message.
            request_approval: Async callable that returns a Future for HITL approval.
            on_auto_approve_enabled: Callback fired when the user selects
                "Auto-approve all" from an approval dialog.

                Used by the app to sync the status bar indicator and session state.
            scroll_to_bottom: Callback to scroll chat to bottom.
            set_spinner: Callback to show/hide loading spinner (pass `None` to hide).
            set_active_message: Callback to set the active streaming message ID.
            sync_message_content: Callback to sync final content back to the
                message store after streaming completes.
            request_ask_user: Async callable that displays an `ask_user` widget
                and returns a `Future` resolving to user answers.
        """
        self._mount_message = mount_message
        self._update_status = update_status
        self._request_approval = request_approval
        self._on_auto_approve_enabled = on_auto_approve_enabled
        self._scroll_to_bottom = scroll_to_bottom
        self._set_spinner = set_spinner
        self._set_active_message = set_active_message
        self._sync_message_content = sync_message_content
        self._request_ask_user = request_ask_user

        # State tracking
        self._current_tool_messages: dict[str, ToolCallMessage] = {}
        self._token_tracker: Any = None
        # ROADMAP #52: per-response usage sink (the app's UsageLedger).
        self._usage_sink: Any = None
        # Persist todo widgets across turns so they are updated in-place rather
        # than mounting duplicate widgets.  Keyed by namespace tuple.
        self._active_todo_messages: dict[tuple, Any] = {}
        # Silent mode is opt-in; verbose ToolCallMessage widgets are the default.
        self.silent_tool_output = False

    def set_usage_sink(self, sink: Any) -> None:  # noqa: ANN401  # Callable[[UsageRecord], None]
        """Register the per-response usage sink (ROADMAP #52)."""
        self._usage_sink = sink

    def set_token_tracker(self, tracker: Any) -> None:  # noqa: ANN401  # Dynamic tracker type from Textual
        """Set the token tracker for usage tracking."""
        self._token_tracker = tracker

    def finalize_pending_tools_with_error(self, error: str) -> None:
        """Mark all pending/running tool widgets as error and clear tracking.

        This is used as a safety net when an unexpected exception aborts
        streaming before matching `ToolMessage` results are received.

        Args:
            error: Error text to display in each pending tool widget.
        """
        for tool_msg in list(self._current_tool_messages.values()):
            tool_msg.set_error(error)
        self._current_tool_messages.clear()

        # Clear active streaming message to avoid stale "active" state in the store.
        if self._set_active_message:
            self._set_active_message(None)


def _build_interrupted_ai_message(
    pending_text_by_namespace: dict[tuple, str],
    current_tool_messages: dict[str, Any],
) -> AIMessage | None:
    """Build an AIMessage capturing interrupted state (text + tool calls).

    Args:
        pending_text_by_namespace: Dict of accumulated text by namespace
        current_tool_messages: Dict of tool_id -> ToolCallMessage widget

    Returns:
        AIMessage with accumulated content and tool calls, or None if empty.
    """
    main_ns_key = ()
    accumulated_text = pending_text_by_namespace.get(main_ns_key, "").strip()

    # Reconstruct tool_calls from displayed tool messages
    tool_calls = []
    for tool_id, tool_widget in list(current_tool_messages.items()):
        tool_calls.append(
            {
                "id": tool_id,
                "name": tool_widget._tool_name,
                "args": tool_widget._args,
            }
        )

    if not accumulated_text and not tool_calls:
        return None

    return AIMessage(
        content=accumulated_text,
        tool_calls=tool_calls or [],
    )


async def _evaluate_auto_mode_batch(
    action_requests: list[dict],
    *,
    goal: str = "",
    notify: Callable[[Any], Awaitable[None]] | None = None,
    working_dir: Path | str | None = None,
) -> bool:
    """Decide whether a turn's tool calls may run without a dialog (v6 #47).

    Governed Auto Mode, in order: (1) the deterministic rule engine
    (ask-list → git classifier → exec-risk → hygiene → allow-list) decides
    what it can; (2) everything it left `default` is graded by ONE batched
    review-model call against the user's stated goal; (3) every decision is
    written to the approval ledger and asserted as an `approval_decision`
    fact so `/auto why` and `/why` can explain it; (4) `breaker_threshold`
    consecutive risky verdicts pause auto mode for the session.

    Args:
        action_requests: Tool-call request dicts (`name`, `args`).
        goal: The user's latest prompt.
        notify: Async mount callable for the one-time "paused" notice.
        working_dir: Project root for fact assertion (defaults to cwd).

    Returns:
        True when every request may be auto-approved, False when any must
        surface an approval dialog.
    """
    from bog_agents_cli.auto_mode import (
        ApprovalDecision,
        AutoDecision,
        AutoModeRuleEngine,
        _format_tool_repr,
        batch_risk_eval,
        get_auto_mode_breaker,
        load_auto_mode_settings,
        record_approval_decisions,
        resolve_risk_judge,
    )

    am_settings = load_auto_mode_settings()
    engine = AutoModeRuleEngine(am_settings)
    breaker = get_auto_mode_breaker(am_settings.breaker_threshold)
    decisions: list[ApprovalDecision] = []
    pending: list[tuple[int, str, dict[str, Any]]] = []
    approve_all = True

    for index, req in enumerate(action_requests):
        t_name = req.get("name", "")
        t_args = req.get("args", {}) or {}
        if not isinstance(t_args, dict):
            t_args = {}
        call = _format_tool_repr(t_name, t_args)
        verdict = engine.evaluate(t_name, t_args)
        if verdict.decision == AutoDecision.ASK:
            approve_all = False
            decisions.append(
                ApprovalDecision(
                    t_name, call, "ask", verdict.rule_source, verdict.reason
                )
            )
        elif verdict.rule_source == "default" and am_settings.haiku_eval.enabled:
            pending.append((index, t_name, t_args))
        else:
            decisions.append(
                ApprovalDecision(
                    t_name, call, "auto-approved", verdict.rule_source, verdict.reason
                )
            )

    if pending and breaker.tripped:
        approve_all = False
        for _index, t_name, t_args in pending:
            decisions.append(
                ApprovalDecision(
                    t_name,
                    _format_tool_repr(t_name, t_args),
                    "paused",
                    "breaker",
                    breaker.status(),
                )
            )
    elif pending:
        judge, judge_desc = resolve_risk_judge(am_settings)
        if judge is None:
            approve_all = False
            for _index, t_name, t_args in pending:
                decisions.append(
                    ApprovalDecision(
                        t_name,
                        _format_tool_repr(t_name, t_args),
                        "ask",
                        "review_model",
                        f"no review model ({judge_desc})",
                    )
                )
        else:
            assessments = await batch_risk_eval(pending, goal=goal, invoke=judge)
            tripped_now = False
            for (_index, t_name, t_args), assessment in zip(
                pending, assessments, strict=True
            ):
                risky = assessment.risky
                if risky:
                    approve_all = False
                tripped_now = breaker.record(risky) or tripped_now
                decisions.append(
                    ApprovalDecision(
                        t_name,
                        _format_tool_repr(t_name, t_args),
                        "ask" if risky else "auto-approved",
                        "review_model",
                        assessment.reason,
                        risk=assessment.risk,
                        judge=judge_desc,
                    )
                )
            if tripped_now and notify is not None and not breaker.notified:
                breaker.notified = True
                from bog_agents_cli.widgets.messages import AppMessage

                await notify(
                    AppMessage(
                        f"Auto mode paused: {breaker.threshold} consecutive tool calls were graded risky, "
                        "so every call will ask until you re-arm with /auto on. "
                        "See /auto why for the decisions."
                    )
                )

    record_approval_decisions(decisions, working_dir or Path.cwd())
    return approve_all


async def execute_task_textual(
    user_input: str,
    agent: Any,  # noqa: ANN401  # Dynamic agent graph type
    assistant_id: str | None,
    session_state: Any,  # noqa: ANN401  # Dynamic session state type
    adapter: TextualUIAdapter,
    backend: Any = None,  # noqa: ANN401  # Dynamic backend type
    image_tracker: MediaTracker | None = None,
    context: CLIContext | None = None,
) -> SessionStats:
    """Execute a task with output directed to Textual UI.

    This is the Textual-compatible version of execute_task() that uses
    the TextualUIAdapter for all UI operations.

    Args:
        user_input: The user's input message
        agent: The LangGraph agent to execute
        assistant_id: The agent identifier
        session_state: Session state with auto_approve flag
        adapter: The TextualUIAdapter for UI operations
        backend: Optional backend for file operations
        image_tracker: Optional tracker for images
        context: Optional `CLIContext` with model override and params, passed
            to the graph via `context=`.

    Returns:
        Stats accumulated over this turn (request count, token counts,
            wall-clock time).

    Raises:
        ValidationError: If HITL request validation fails (re-raised).
    """
    # Parse file mentions and inject content if any
    prompt_text, mentioned_files = parse_file_mentions(user_input)

    # Max file size to embed inline (256KB, matching mistral-vibe)
    # Larger files get a reference instead - use read_file tool to view them
    max_embed_bytes = 256 * 1024

    if mentioned_files:
        context_parts = [prompt_text, "\n\n## Referenced Files\n"]
        for file_path in mentioned_files:
            try:
                file_size = file_path.stat().st_size
                if file_size > max_embed_bytes:
                    # File too large - include reference instead of content
                    size_kb = file_size // 1024
                    context_parts.append(
                        f"\n### {file_path.name}\n"
                        f"Path: `{file_path}`\n"
                        f"Size: {size_kb}KB (too large to embed, "
                        "use read_file tool to view)"
                    )
                else:
                    content = file_path.read_text(encoding="utf-8")
                    context_parts.append(
                        f"\n### {file_path.name}\n"
                        f"Path: `{file_path}`\n```\n{content}\n```"
                    )
            except Exception as e:  # Resilient adapter error handling
                context_parts.append(
                    f"\n### {file_path.name}\n[Error reading file: {e}]"
                )
        final_input = "\n".join(context_parts)
    else:
        final_input = prompt_text

    # Include images and videos in the message content
    images_to_send = []
    videos_to_send = []
    if image_tracker:
        images_to_send = image_tracker.get_images()
        videos_to_send = image_tracker.get_videos()
    if images_to_send or videos_to_send:
        message_content = create_multimodal_content(
            final_input, images_to_send, videos_to_send
        )
    else:
        message_content = final_input

    thread_id = session_state.thread_id
    config = _build_stream_config(thread_id, assistant_id)

    await dispatch_hook("session.start", {"thread_id": thread_id})

    captured_input_tokens = 0
    captured_output_tokens = 0
    turn_stats = SessionStats()
    start_time = time.monotonic()

    # Show spinner + mirror the same state into the status bar so the user
    # has feedback both above the input (spinner) and at the bottom of the
    # screen (status bar) while the agent works.
    if adapter._set_spinner:
        await adapter._set_spinner("Thinking")
    adapter._update_status("Thinking…")

    # Hide token display during streaming (will be shown with accurate count at end)
    if adapter._token_tracker:
        adapter._token_tracker.hide()

    file_op_tracker = FileOpTracker(assistant_id=assistant_id, backend=backend)
    turn_stats.file_records = file_op_tracker.completed
    # ROADMAP #52: per-namespace request timing for TTFT / tok/s on the usage strip.
    request_started_at: dict[tuple, float] = {(): start_time}
    first_text_at: dict[tuple, float] = {}
    displayed_tool_ids: set[str] = set()
    tool_call_buffers: dict[str | int, dict] = {}

    # Track pending text and assistant messages PER NAMESPACE to avoid interleaving
    # when multiple subagents stream in parallel
    pending_text_by_namespace: dict[tuple, str] = {}
    assistant_message_by_namespace: dict[tuple, Any] = {}
    todo_message_by_namespace: dict[tuple, AppMessage] = {}

    # Finalize todos from the previous turn: add a dim "turn ended" footer so
    # users know the displayed state reflects the end of that turn, not the
    # current one.  We then clear the adapter's dict so this turn can either
    # update the widget in-place (if write_todos fires again) or leave it as is.
    for _ns_key, prev_todo_msg in list(adapter._active_todo_messages.items()):
        try:
            existing = str(getattr(prev_todo_msg, "_content", ""))
            prev_todo_msg._content = existing
            prev_todo_msg.update(existing + "\n    [dim]─── previous turn ───[/dim]")
        except Exception:  # noqa: S110  # Never crash on stale-widget cleanup
            pass
    adapter._active_todo_messages.clear()

    # Alias for readability — todos now live on the adapter so they survive
    # across loop iterations and can be updated in-place.
    todo_message_by_namespace = adapter._active_todo_messages

    # Clear media from tracker after creating the message
    if image_tracker:
        image_tracker.clear()

    stream_input: dict | Command = {
        "messages": [{"role": "user", "content": message_content}]
    }

    # Track summarization lifecycle so spinner status and notification stay in sync.
    summarization_in_progress = False

    try:
        while True:
            interrupt_occurred = False
            suppress_resumed_output = False
            pending_interrupts: dict[str, HITLRequest] = {}
            pending_ask_user: dict[str, AskUserRequest] = {}
            pending_budget: dict[str, dict[str, Any]] = {}

            async for chunk in agent.astream(
                stream_input,
                stream_mode=["messages", "updates"],
                subgraphs=True,
                config=config,
                context=context,
                durability="exit",
            ):
                if (
                    not isinstance(chunk, tuple) or len(chunk) != 3
                ):  # stream chunk is a 3-tuple (namespace, mode, data)
                    logger.debug("Skipping non-3-tuple chunk: %s", type(chunk).__name__)
                    continue

                namespace, current_stream_mode, data = chunk

                # Convert namespace to hashable tuple for dict keys
                ns_key = tuple(namespace) if namespace else ()

                # Filter out subagent outputs - only show main agent (empty
                # namespace). Subagents run via Task tool and should only
                # report back to the main agent
                is_main_agent = ns_key == ()

                # Handle UPDATES stream - for interrupts and todos
                if current_stream_mode == "updates":
                    if not isinstance(data, dict):
                        continue

                    # Check for interrupts
                    if "__interrupt__" in data:
                        interrupts: list[Interrupt] = data["__interrupt__"]
                        if interrupts:
                            for interrupt_obj in interrupts:
                                iv = interrupt_obj.value
                                if (
                                    isinstance(iv, dict)
                                    and iv.get("type") == "ask_user"
                                ):
                                    try:
                                        validated_ask_user = (
                                            _ASK_USER_INTERRUPT_ADAPTER.validate_python(
                                                iv
                                            )
                                        )
                                        pending_ask_user[interrupt_obj.id] = (
                                            validated_ask_user
                                        )
                                        interrupt_occurred = True
                                        await dispatch_hook("input.required", {})
                                    except ValidationError:
                                        logger.exception(
                                            "Invalid ask_user interrupt payload"
                                        )
                                        raise
                                elif (
                                    isinstance(iv, dict)
                                    and iv.get("type") == "budget_reached"
                                ):
                                    # ROADMAP #51: the cost tracker paused the
                                    # graph; ask for a raise-cap resume below.
                                    pending_budget[interrupt_obj.id] = iv
                                    interrupt_occurred = True
                                    await dispatch_hook("input.required", {})
                                else:
                                    try:
                                        validated_request = (
                                            _HITL_REQUEST_ADAPTER.validate_python(iv)
                                        )
                                        pending_interrupts[interrupt_obj.id] = (
                                            validated_request
                                        )
                                        interrupt_occurred = True
                                        await dispatch_hook("input.required", {})
                                    except ValidationError:  # noqa: TRY203  # Re-raise preserves exception context in handler
                                        raise

                    todo_items = _find_todos_payload(data)
                    if todo_items is not None:
                        pending_text = pending_text_by_namespace.get(ns_key, "")
                        if pending_text:
                            await _flush_assistant_text_ns(
                                adapter,
                                pending_text,
                                ns_key,
                                assistant_message_by_namespace,
                            )
                            pending_text_by_namespace[ns_key] = ""

                        todo_text = _render_todos_text(todo_items)
                        current_todo_message = todo_message_by_namespace.get(ns_key)
                        if current_todo_message is None:
                            current_todo_message = AppMessage(todo_text)
                            await adapter._mount_message(current_todo_message)
                            todo_message_by_namespace[ns_key] = current_todo_message
                        else:
                            current_todo_message._content = todo_text
                            try:
                                current_todo_message.update(todo_text)
                                # `update()` only schedules a re-render when
                                # Textual detects the renderable changed.
                                # Force a refresh so transitions like "all
                                # done" reliably collapse into the one-liner
                                # even when the previous Text happens to
                                # compare equal.
                                current_todo_message.refresh()
                            except Exception:
                                logger.debug(
                                    "Failed to refresh mounted todo widget",
                                    exc_info=True,
                                )

                # Handle MESSAGES stream - for content and tool calls
                elif current_stream_mode == "messages":
                    # Skip subagent outputs - only render main agent content in chat
                    if not is_main_agent:
                        logger.debug("Skipping subagent message ns=%s", ns_key)
                        continue

                    if (
                        not isinstance(data, tuple) or len(data) != 2
                    ):  # message stream data is a 2-tuple (message, metadata)
                        logger.debug(
                            "Skipping non-2-tuple message data: type=%s",
                            type(data).__name__,
                        )
                        continue

                    message, metadata = data
                    logger.debug(
                        "Processing message: type=%s id=%s has_content_blocks=%s",
                        type(message).__name__,
                        getattr(message, "id", None),
                        hasattr(message, "content_blocks"),
                    )

                    # Filter out summarization model output, but keep UI feedback.
                    # The summarization model streams AIMessage chunks tagged
                    # with lc_source="summarization" in the callback metadata.
                    # These are hidden from the user; only the spinner and a
                    # notification widget provide feedback.
                    if _is_summarization_chunk(metadata):
                        if not summarization_in_progress:
                            summarization_in_progress = True
                            if adapter._set_spinner:
                                await adapter._set_spinner("Summarizing")
                        continue

                    # Regular (non-summarization) chunks resumed — summarization
                    # has finished. Mount the notification and reset the spinner.
                    if summarization_in_progress:
                        summarization_in_progress = False
                        try:
                            await adapter._mount_message(SummarizationMessage())
                        except Exception:
                            logger.debug(
                                "Failed to mount summarization notification",
                                exc_info=True,
                            )
                        if adapter._set_spinner:
                            await adapter._set_spinner("Thinking")

                    if isinstance(message, HumanMessage):
                        content = message.text
                        # Flush pending text for this namespace
                        pending_text = pending_text_by_namespace.get(ns_key, "")
                        if content and pending_text:
                            await _flush_assistant_text_ns(
                                adapter,
                                pending_text,
                                ns_key,
                                assistant_message_by_namespace,
                            )
                            pending_text_by_namespace[ns_key] = ""
                        continue

                    if isinstance(message, ToolMessage):
                        tool_name = getattr(message, "name", "")
                        tool_status = getattr(message, "status", "success")
                        tool_content = format_tool_message_content(message.content)
                        record = file_op_tracker.complete_with_message(message)

                        # Reshow spinner after tool result
                        if adapter._set_spinner:
                            await adapter._set_spinner("Thinking")

                        # Update tool call status with output
                        tool_id = getattr(message, "tool_call_id", None)
                        if tool_id and tool_id in adapter._current_tool_messages:
                            tool_msg = adapter._current_tool_messages[tool_id]
                            output_str = str(tool_content) if tool_content else ""
                            if tool_status == "success":
                                tool_msg.set_success(output_str)
                            else:
                                tool_msg.set_error(output_str or "Error")
                                await dispatch_hook(
                                    "tool.error",
                                    {"tool_names": [tool_msg._tool_name]},
                                )
                            # Clean up - remove from tracking dict after status update
                            adapter._current_tool_messages.pop(tool_id, None)

                        # Show file operation results - always show diffs in chat
                        if record:
                            pending_text = pending_text_by_namespace.get(ns_key, "")
                            if pending_text:
                                await _flush_assistant_text_ns(
                                    adapter,
                                    pending_text,
                                    ns_key,
                                    assistant_message_by_namespace,
                                )
                                pending_text_by_namespace[ns_key] = ""
                            if record.diff:
                                await adapter._mount_message(
                                    DiffMessage(record.diff, record.display_path)
                                )
                        continue

                    # Extract token usage (before content_blocks check
                    # - usage may be on any chunk)
                    if hasattr(message, "usage_metadata"):
                        usage = message.usage_metadata
                        if usage:
                            input_toks = usage.get("input_tokens", 0)
                            output_toks = usage.get("output_tokens", 0)
                            total_toks = usage.get("total_tokens", 0)
                            active_model = settings.model_name or ""
                            if input_toks or output_toks:
                                # Model gives split counts — preferred path
                                turn_stats.record_request(
                                    active_model, input_toks, output_toks
                                )
                                captured_input_tokens = max(
                                    captured_input_tokens, input_toks + output_toks
                                )
                            elif total_toks:
                                # Fallback: model gives only total (no split)
                                turn_stats.record_request(active_model, total_toks, 0)
                                captured_input_tokens = max(
                                    captured_input_tokens, total_toks
                                )
                            if input_toks or output_toks or total_toks:
                                # ROADMAP #52: price + time this response, feed
                                # the session ledger, and hang the strip under
                                # the message it belongs to.
                                from bog_agents_cli.usage_controller import (
                                    record_stream_usage,
                                )

                                now = time.monotonic()
                                started = request_started_at.get(ns_key, start_time)
                                first = first_text_at.get(ns_key)
                                await record_stream_usage(
                                    adapter._usage_sink,
                                    usage,
                                    model=active_model,
                                    category="subagent" if ns_key else "main",
                                    ttft_s=(first - started) if first else None,
                                    duration_s=now - started,
                                    message_widget=assistant_message_by_namespace.get(
                                        ns_key
                                    ),
                                )
                                request_started_at[ns_key] = now
                                first_text_at.pop(ns_key, None)

                    # Check if this is an AIMessageChunk with content
                    if not hasattr(message, "content_blocks"):
                        logger.debug(
                            "Message has no content_blocks: type=%s",
                            type(message).__name__,
                        )
                        continue

                    # Process content blocks
                    blocks = message.content_blocks
                    logger.debug(
                        "content_blocks count=%d blocks=%s",
                        len(blocks),
                        repr(blocks)[:500],
                    )
                    for block in blocks:
                        block_type = block.get("type")

                        if block_type == "text":
                            text = block.get("text", "")
                            if text:
                                # Track accumulated text for reference
                                pending_text = pending_text_by_namespace.get(ns_key, "")
                                pending_text += text
                                pending_text_by_namespace[ns_key] = pending_text
                                first_text_at.setdefault(ns_key, time.monotonic())

                                # Get or create assistant message for this namespace
                                current_msg = assistant_message_by_namespace.get(ns_key)
                                if current_msg is None:
                                    # Hide spinner when assistant starts responding
                                    if adapter._set_spinner:
                                        await adapter._set_spinner(None)
                                    msg_id = f"asst-{uuid.uuid4().hex[:8]}"
                                    # Mark active BEFORE mounting so pruning
                                    # (triggered by mount) won't remove it
                                    # (_mount_message can trigger
                                    # _prune_old_messages if the window exceeds
                                    # WINDOW_SIZE.)
                                    if adapter._set_active_message:
                                        adapter._set_active_message(msg_id)
                                    current_msg = AssistantMessage(id=msg_id)
                                    await adapter._mount_message(current_msg)
                                    assistant_message_by_namespace[ns_key] = current_msg

                                # Append just the new text chunk for smoother
                                # streaming (uses MarkdownStream internally for
                                # better performance)
                                await current_msg.append_content(text)

                                # Sticky scroll: scroll to bottom only if user is
                                # near bottom. This lets users scroll away and
                                # stay where they are
                                if adapter._scroll_to_bottom:
                                    adapter._scroll_to_bottom()

                        elif block_type in {"tool_call_chunk", "tool_call"}:
                            chunk_name = block.get("name")
                            chunk_args = block.get("args")
                            chunk_id = block.get("id")
                            chunk_index = block.get("index")

                            buffer_key: str | int
                            if chunk_index is not None:
                                buffer_key = chunk_index
                            elif chunk_id is not None:
                                buffer_key = chunk_id
                            else:
                                buffer_key = f"unknown-{len(tool_call_buffers)}"

                            buffer = tool_call_buffers.setdefault(
                                buffer_key,
                                {
                                    "name": None,
                                    "id": None,
                                    "args": None,
                                    "args_parts": [],
                                },
                            )

                            if chunk_name:
                                buffer["name"] = chunk_name
                            if chunk_id:
                                buffer["id"] = chunk_id

                            if isinstance(chunk_args, dict):
                                buffer["args"] = chunk_args
                                buffer["args_parts"] = []
                            elif isinstance(chunk_args, str):
                                if chunk_args:
                                    # O(n) accumulation: the joined string is
                                    # materialized (and parsed, below) only
                                    # once the fragment scanner reports the
                                    # JSON value complete (v5 PERF-2).
                                    _append_streamed_args(buffer, chunk_args)
                            elif chunk_args is not None:
                                buffer["args"] = chunk_args

                            buffer_name = buffer.get("name")
                            buffer_id = buffer.get("id")
                            if buffer_name is None:
                                continue

                            parsed_args = buffer.get("args")
                            if isinstance(parsed_args, str):
                                if not parsed_args:
                                    continue
                                try:
                                    parsed_args = json.loads(parsed_args)
                                except json.JSONDecodeError:
                                    continue
                            elif parsed_args is None:
                                continue

                            if not isinstance(parsed_args, dict):
                                parsed_args = {"value": parsed_args}

                            # Flush pending text before tool call
                            pending_text = pending_text_by_namespace.get(ns_key, "")
                            if pending_text:
                                await _flush_assistant_text_ns(
                                    adapter,
                                    pending_text,
                                    ns_key,
                                    assistant_message_by_namespace,
                                )
                                pending_text_by_namespace[ns_key] = ""
                                assistant_message_by_namespace.pop(ns_key, None)

                            logger.debug(
                                "Tool call buffer: name=%s id=%s args=%s",
                                buffer_name,
                                buffer_id,
                                repr(parsed_args)[:200],
                            )
                            if (
                                buffer_id is not None
                                and buffer_id not in displayed_tool_ids
                            ):
                                displayed_tool_ids.add(buffer_id)
                                file_op_tracker.start_operation(
                                    buffer_name, parsed_args, buffer_id
                                )

                                # Hide spinner before showing tool call;
                                # surface the running tool's name in the
                                # status bar so the user sees activity even
                                # while the spinner is hidden.
                                if adapter._set_spinner:
                                    await adapter._set_spinner(None)
                                adapter._update_status(f"Running {buffer_name}…")

                                # Mount tool call message
                                logger.debug(
                                    "Mounting ToolCallMessage: %s(%s)",
                                    buffer_name,
                                    repr(parsed_args)[:200],
                                )
                                if adapter.silent_tool_output:
                                    # In silent mode the chat shows a one-line
                                    # marker; the full args/result are still
                                    # written to the debug log so the user can
                                    # `tail -f ~/.bog-agents/cli.log` if they
                                    # want full detail.
                                    silent_marker = AppMessage(
                                        Text(
                                            f"  {get_glyphs().bullet} {buffer_name}",
                                            style="dim",
                                        )
                                    )
                                    await adapter._mount_message(silent_marker)
                                    # Still create a ToolCallMessage instance so
                                    # the result-arrival path can call set_error
                                    # / set_output on it; just don't mount it.
                                    tool_msg = ToolCallMessage(buffer_name, parsed_args)
                                else:
                                    tool_msg = ToolCallMessage(buffer_name, parsed_args)
                                    await adapter._mount_message(tool_msg)
                                adapter._current_tool_messages[buffer_id] = tool_msg

                                # Sticky scroll after tool call is shown
                                if adapter._scroll_to_bottom:
                                    adapter._scroll_to_bottom()

                            tool_call_buffers.pop(buffer_key, None)

                    if getattr(message, "chunk_position", None) == "last":
                        pending_text = pending_text_by_namespace.get(ns_key, "")
                        if pending_text:
                            await _flush_assistant_text_ns(
                                adapter,
                                pending_text,
                                ns_key,
                                assistant_message_by_namespace,
                            )
                            pending_text_by_namespace[ns_key] = ""
                            assistant_message_by_namespace.pop(ns_key, None)

            # Reset summarization state if stream ended mid-summarization
            # (e.g. middleware error, stream exhausted before regular chunks).
            if summarization_in_progress:
                summarization_in_progress = False
                try:
                    await adapter._mount_message(SummarizationMessage())
                except Exception:
                    logger.debug(
                        "Failed to mount summarization notification",
                        exc_info=True,
                    )
                if adapter._set_spinner:
                    await adapter._set_spinner("Thinking")
                adapter._update_status("Thinking…")

            # Flush any remaining text from all namespaces
            for ns_key, pending_text in list(pending_text_by_namespace.items()):
                if pending_text:
                    await _flush_assistant_text_ns(
                        adapter, pending_text, ns_key, assistant_message_by_namespace
                    )
            pending_text_by_namespace.clear()
            assistant_message_by_namespace.clear()

            # Handle HITL after stream completes
            if interrupt_occurred:
                any_rejected = False
                resume_payload: dict[str, Any] = {}

                for interrupt_id, budget_payload in list(pending_budget.items()):
                    from bog_agents_cli.cost_controller import (
                        ask_budget_raise,
                        budget_stop_message,
                    )

                    new_cap = await ask_budget_raise(
                        adapter._request_ask_user, budget_payload
                    )
                    if new_cap is None:
                        await adapter._mount_message(
                            AppMessage(budget_stop_message(budget_payload))
                        )
                        turn_stats.wall_time_seconds = time.monotonic() - start_time
                        return turn_stats
                    resume_payload[interrupt_id] = {"budget_usd": new_cap}

                for interrupt_id, ask_req in list(pending_ask_user.items()):
                    questions = ask_req["questions"]

                    if adapter._request_ask_user:
                        if adapter._set_spinner:
                            await adapter._set_spinner(None)
                        result: dict[str, Any] = {
                            "type": "error",
                            "error": "ask_user callback returned no response",
                        }
                        try:
                            future = await adapter._request_ask_user(questions)
                        except Exception:
                            logger.exception("Failed to mount ask_user widget")
                            result = {
                                "type": "error",
                                "error": "failed to display ask_user prompt",
                            }
                            future = None

                        if future is None:
                            logger.error(
                                "ask_user callback returned no Future; "
                                "reporting as error"
                            )
                        else:
                            try:
                                future_result = await future
                                if isinstance(future_result, dict):
                                    result = future_result
                                else:
                                    logger.error(
                                        "ask_user future returned non-dict result: %s",
                                        type(future_result).__name__,
                                    )
                                    result = {
                                        "type": "error",
                                        "error": "invalid ask_user widget result",
                                    }
                            except Exception:
                                logger.exception(
                                    "ask_user future resolution failed; "
                                    "reporting as error"
                                )
                                result = {
                                    "type": "error",
                                    "error": "failed to receive ask_user response",
                                }

                        result_type = result.get("type")
                        if result_type == "answered":
                            answers = result.get("answers", [])
                            if isinstance(answers, list):
                                resume_payload[interrupt_id] = {"answers": answers}
                                tool_id = ask_req["tool_call_id"]
                                if tool_id in adapter._current_tool_messages:
                                    tool_msg = adapter._current_tool_messages[tool_id]
                                    tool_msg.set_success("User answered")
                                    adapter._current_tool_messages.pop(tool_id, None)
                            else:
                                logger.error(
                                    "ask_user answered payload had non-list "
                                    "answers: %s",
                                    type(answers).__name__,
                                )
                                resume_payload[interrupt_id] = {
                                    "status": "error",
                                    "error": "invalid ask_user answers payload",
                                    "answers": ["" for _ in questions],
                                }
                                any_rejected = True
                        elif result_type == "cancelled":
                            resume_payload[interrupt_id] = {
                                "status": "cancelled",
                                "answers": ["" for _ in questions],
                            }
                            any_rejected = True
                        else:
                            error_text = result.get("error")
                            if not isinstance(error_text, str) or not error_text:
                                error_text = "ask_user interaction failed"
                            resume_payload[interrupt_id] = {
                                "status": "error",
                                "error": error_text,
                                "answers": ["" for _ in questions],
                            }
                            any_rejected = True
                    else:
                        logger.warning(
                            "ask_user interrupt received but no UI callback is "
                            "registered; reporting as error"
                        )
                        resume_payload[interrupt_id] = {
                            "status": "error",
                            "error": "ask_user not supported by this UI",
                            "answers": ["" for _ in questions],
                        }

                for interrupt_id, hitl_request in list(pending_interrupts.items()):
                    action_requests = hitl_request["action_requests"]

                    # Project-local pre-tool harness hooks run BEFORE any
                    # approval logic. A hook can block a tool call (we
                    # synthesize a rejection decision) or rewrite the
                    # tool args (the modified args replace what the user
                    # sees in the approval menu).
                    from bog_agents_cli.hooks import dispatch_tool_pre_hook

                    blocked_indexes: list[int] = []
                    for i, req in enumerate(list(action_requests)):
                        decision_dict = await dispatch_tool_pre_hook(
                            req.get("name", ""),
                            req.get("args", {}) or {},
                        )
                        if not isinstance(decision_dict, dict):
                            continue
                        action = decision_dict.get("action")
                        if action == "block":
                            blocked_indexes.append(i)
                        elif action == "modify":
                            new_args = decision_dict.get("args")
                            if isinstance(new_args, dict):
                                req["args"] = new_args

                    # P1-75: a hook blocking ONE call in a parallel batch must
                    # not auto-approve its siblings. If every call is blocked we
                    # can short-circuit; otherwise fall through to the normal
                    # approval flow and force the blocked indexes to reject after
                    # the user/auto decision is made (via _force_blocked below).
                    def _force_blocked(
                        decisions: list[HITLDecision],
                        _blocked: list[int] = blocked_indexes,
                    ) -> list[HITLDecision]:
                        for i in _blocked:
                            if 0 <= i < len(decisions):
                                decisions[i] = RejectDecision(
                                    type="reject",
                                    message="blocked by .bog-agents/hooks/pre-tool",
                                )
                        return decisions

                    if blocked_indexes and len(blocked_indexes) >= len(action_requests):
                        decisions = [
                            RejectDecision(
                                type="reject",
                                message="blocked by .bog-agents/hooks/pre-tool",
                            )
                            for _ in action_requests
                        ]
                        resume_payload[interrupt_id] = {"decisions": decisions}
                        continue

                    # ``always_ask`` is the paranoid-mode flag — when set it
                    # overrides ``auto_approve`` so EVERY tool call still
                    # surfaces an approval menu. Used for high-stakes
                    # sessions where the user wants to inspect each action.
                    always_ask = bool(getattr(session_state, "always_ask", False))
                    auto_mode = bool(getattr(session_state, "auto_mode", False))

                    # Determine whether to skip the approval dialog entirely.
                    # Priority: always_ask > auto_mode > auto_approve > ask.
                    should_auto_approve = False
                    if session_state.auto_approve and not always_ask:
                        should_auto_approve = True
                    elif auto_mode and not always_ask:
                        # Smart auto-mode: rule engine + optional Haiku eval.
                        should_auto_approve = await _evaluate_auto_mode_batch(
                            action_requests,
                            goal=user_input,
                            notify=adapter._mount_message,
                        )

                    if should_auto_approve:
                        decisions: list[HITLDecision] = [
                            ApproveDecision(type="approve") for _ in action_requests
                        ]
                        # P1-75: hook-blocked calls still reject even under auto-approve.
                        decisions = _force_blocked(decisions)
                        resume_payload[interrupt_id] = {"decisions": decisions}
                        for tool_msg in list(adapter._current_tool_messages.values()):
                            tool_msg.set_running()
                    else:
                        # Batch approval - one dialog for all parallel tool calls
                        await dispatch_hook(
                            "permission.request",
                            {
                                "tool_names": [
                                    r.get("name", "") for r in action_requests
                                ]
                            },
                        )
                        future = await adapter._request_approval(
                            action_requests, assistant_id
                        )
                        decision = await future

                        if isinstance(decision, dict):
                            decision_type = decision.get("type")

                            if decision_type == "auto_approve_all":
                                session_state.auto_approve = True
                                if adapter._on_auto_approve_enabled:
                                    adapter._on_auto_approve_enabled()
                                decisions = [
                                    ApproveDecision(type="approve")
                                    for _ in action_requests
                                ]
                                tool_msgs = list(
                                    adapter._current_tool_messages.values()
                                )
                                for tool_msg in tool_msgs:
                                    tool_msg.set_running()
                                for action_request in action_requests:
                                    tool_name = action_request.get("name")
                                    if tool_name in {
                                        "write_file",
                                        "edit_file",
                                    }:
                                        args = action_request.get("args", {})
                                        if isinstance(args, dict):
                                            file_op_tracker.mark_hitl_approved(
                                                tool_name, args
                                            )

                            elif decision_type == "approve":
                                decisions = [
                                    ApproveDecision(type="approve")
                                    for _ in action_requests
                                ]
                                tool_msgs = list(
                                    adapter._current_tool_messages.values()
                                )
                                for tool_msg in tool_msgs:
                                    tool_msg.set_running()
                                for action_request in action_requests:
                                    tool_name = action_request.get("name")
                                    if tool_name in {
                                        "write_file",
                                        "edit_file",
                                    }:
                                        args = action_request.get("args", {})
                                        if isinstance(args, dict):
                                            file_op_tracker.mark_hitl_approved(
                                                tool_name, args
                                            )

                            elif decision_type == "reject":
                                decisions = [
                                    RejectDecision(type="reject")
                                    for _ in action_requests
                                ]
                                tool_msgs = list(
                                    adapter._current_tool_messages.values()
                                )
                                for tool_msg in tool_msgs:
                                    tool_msg.set_rejected()
                                adapter._current_tool_messages.clear()
                                any_rejected = True
                            else:
                                logger.warning(
                                    "Unexpected HITL decision type: %s",
                                    decision_type,
                                )
                                decisions = [
                                    RejectDecision(type="reject")
                                    for _ in action_requests
                                ]
                                for tool_msg in list(
                                    adapter._current_tool_messages.values()
                                ):
                                    tool_msg.set_rejected()
                                adapter._current_tool_messages.clear()
                                any_rejected = True
                        else:
                            logger.warning(
                                "HITL decision was not a dict: %s",
                                type(decision).__name__,
                            )
                            decisions = [
                                RejectDecision(type="reject") for _ in action_requests
                            ]
                            for tool_msg in list(
                                adapter._current_tool_messages.values()
                            ):
                                tool_msg.set_rejected()
                            adapter._current_tool_messages.clear()
                            any_rejected = True

                        # P1-75: whatever the user/auto decided, hook-blocked
                        # calls in this batch are forced to reject.
                        decisions = _force_blocked(decisions)
                        if blocked_indexes:
                            any_rejected = True
                        resume_payload[interrupt_id] = {"decisions": decisions}

                        if any_rejected:
                            break

                suppress_resumed_output = any_rejected

            if interrupt_occurred and resume_payload:
                if suppress_resumed_output and not pending_ask_user:
                    await adapter._mount_message(
                        AppMessage(
                            "Command rejected. Tell the agent what you'd like instead."
                        )
                    )
                    turn_stats.wall_time_seconds = time.monotonic() - start_time
                    return turn_stats

                stream_input = Command(resume=resume_payload)
            else:
                await dispatch_hook("task.complete", {"thread_id": thread_id})
                break

    except asyncio.CancelledError:
        # Clear active message immediately so it won't block pruning
        # If we don't do this, the store still thinks it's actice and protects
        # from pruning, which breaks get_messages_to_prune(), potentially
        # blocking all future pruning
        if adapter._set_active_message:
            adapter._set_active_message(None)

        # Hide spinner (may still show "Summarizing" if interrupted mid-summary)
        if adapter._set_spinner:
            await adapter._set_spinner(None)

        await adapter._mount_message(AppMessage("Interrupted by user"))

        # Save accumulated state before marking tools as rejected (best-effort)
        # State update failures shouldn't prevent cleanup
        try:
            interrupted_msg = _build_interrupted_ai_message(
                pending_text_by_namespace,
                adapter._current_tool_messages,
            )
            if interrupted_msg:
                await agent.aupdate_state(config, {"messages": [interrupted_msg]})

            cancellation_msg = HumanMessage(
                content="[SYSTEM] Task interrupted by user. "
                "Previous operation was cancelled."
            )
            await agent.aupdate_state(config, {"messages": [cancellation_msg]})
        except Exception:
            logger.debug("Failed to save interrupted state", exc_info=True)

        # Mark tools as rejected AFTER saving state
        for tool_msg in list(adapter._current_tool_messages.values()):
            tool_msg.set_rejected()
        adapter._current_tool_messages.clear()

        # Report tokens even on interrupt (or restore display if none captured)
        turn_stats.wall_time_seconds = time.monotonic() - start_time
        if adapter._token_tracker:
            if captured_input_tokens or captured_output_tokens:
                adapter._token_tracker.add(
                    captured_input_tokens, captured_output_tokens
                )
            else:
                adapter._token_tracker.show()  # Restore previous value
        return turn_stats

    except KeyboardInterrupt:
        # Clear active message immediately so it won't block pruning
        # If we don't do this, the store still thinks it's actice and protects
        # from pruning, which breaks get_messages_to_prune(), potentially
        # blocking all future pruning
        if adapter._set_active_message:
            adapter._set_active_message(None)

        # Hide spinner (may still show "Summarizing" if interrupted mid-summary)
        if adapter._set_spinner:
            await adapter._set_spinner(None)

        await adapter._mount_message(AppMessage("Interrupted by user"))

        # Save accumulated state before marking tools as rejected (best-effort)
        # State update failures shouldn't prevent cleanup
        try:
            interrupted_msg = _build_interrupted_ai_message(
                pending_text_by_namespace,
                adapter._current_tool_messages,
            )
            if interrupted_msg:
                await agent.aupdate_state(config, {"messages": [interrupted_msg]})

            cancellation_msg = HumanMessage(
                content="[SYSTEM] Task interrupted by user. "
                "Previous operation was cancelled."
            )
            await agent.aupdate_state(config, {"messages": [cancellation_msg]})
        except Exception:
            logger.debug("Failed to save interrupted state", exc_info=True)

        # Mark tools as rejected AFTER saving state
        for tool_msg in list(adapter._current_tool_messages.values()):
            tool_msg.set_rejected()
        adapter._current_tool_messages.clear()

        # Report tokens even on interrupt (or restore display if none captured)
        turn_stats.wall_time_seconds = time.monotonic() - start_time
        if adapter._token_tracker:
            if captured_input_tokens or captured_output_tokens:
                adapter._token_tracker.add(
                    captured_input_tokens, captured_output_tokens
                )
            else:
                adapter._token_tracker.show()  # Restore previous value
        return turn_stats

    # Update token tracker and return stats
    turn_stats.wall_time_seconds = time.monotonic() - start_time
    if adapter._token_tracker and (captured_input_tokens or captured_output_tokens):
        adapter._token_tracker.add(captured_input_tokens, captured_output_tokens)
    # Clear the working status now that the turn is complete; the status
    # bar reverts to its idle text on the app side via set_status_message("").
    adapter._update_status("")
    return turn_stats


async def _flush_assistant_text_ns(
    adapter: TextualUIAdapter,
    text: str,
    ns_key: tuple,
    assistant_message_by_namespace: dict[tuple, Any],
) -> None:
    """Flush accumulated assistant text for a specific namespace.

    Finalizes the streaming by stopping the MarkdownStream.
    If no message exists yet, creates one with the full content.
    """
    if not text.strip():
        return

    current_msg = assistant_message_by_namespace.get(ns_key)
    if current_msg is None:
        # No message was created during streaming - create one with full content
        msg_id = f"asst-{uuid.uuid4().hex[:8]}"
        current_msg = AssistantMessage(text, id=msg_id)
        await adapter._mount_message(current_msg)
        await current_msg.write_initial_content()
        assistant_message_by_namespace[ns_key] = current_msg
    else:
        # Stop the stream to finalize the content
        await current_msg.stop_stream()

    # When the AssistantMessage was first mounted and recorded in the
    # MessageStore, it had empty content (streaming hadn't started yet).
    # Now that streaming is done, the widget holds the full text in
    # `_content`, but the store's MessageData still has `content=""`.
    # If the message is later pruned and re-hydrated, `to_widget()` would
    # recreate it from that stale empty string. This call copies the
    # widget's final content back into the store so re-hydration works.
    if adapter._sync_message_content and current_msg.id:
        adapter._sync_message_content(current_msg.id, current_msg._content)

    # Clear active message since streaming is done
    if adapter._set_active_message:
        adapter._set_active_message(None)
