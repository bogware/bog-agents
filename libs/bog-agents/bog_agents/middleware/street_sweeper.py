"""Street-sweeper middleware — continuous, lossless-first context pruning.

Feature: Street Sweeper. Unlike `SummarizationMiddleware` (which fires a single
LLM-based compaction once the context window is ~85% full), the street sweeper
runs on *every* model call and mechanically removes dead weight from the
messages *as they are sent to the model* — before the litter ever accumulates.

The sweep is a **view transformation**: the canonical full history stays in
LangGraph state untouched, and the sweeper only reshapes the per-call request
via `request.override(messages=...)`. This means:

- Token savings land on every API call (the provider only bills the swept view).
- Nothing is destroyed — the full originals remain in state, and any content the
    sweeper *drops from the view* (stale reads, duplicates, truncated tails) is
    additionally offloaded to the backend so the model can pull it back with the
    `recall_swept` tool.
- The transformation is deterministic and **never changes message count or
    order** — only the text *content* of individual messages — so it composes
    safely with `SummarizationMiddleware` (whose cutoff indices stay aligned) and
    keeps `AnthropicPromptCachingMiddleware`'s prefix stable across turns.

## Tiers

The sweep is organized into tiers matching the feature's configuration:

- **Tier 0 — lossless mechanical** (always on): strip ANSI escapes, trailing
    whitespace, collapse blank-line runs, and collapse long runs of identical
    lines (e.g. repeated stack frames) into a single line with a count marker.
- **Tier 1 — superseded content** (always on): replace a tool result with a
    one-line stub when it is byte-identical to a later result (`dedup`), or when
    a file read has been superseded by a later read/edit of the same path
    (`stale_read`).
- **Tier 2 — heuristic relevance** (`aggressive=True`): truncate large, old tool
    outputs to a head + tail window with an elision marker.

Recent messages (the last `keep_recent`) and all `HumanMessage`/system content
are never swept, so live working context and user intent stay intact.

## Observability

Every sweep records structured `SweepAction` entries into an in-memory
`SweepLog` (cumulative tokens saved, per-technique breakdown, recent actions),
exposed via the `sweep_log` property for the CLI `/sweep status` / `/sweep log`
surface and for tests.
"""

from __future__ import annotations

import hashlib
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from langchain.agents.middleware.types import AgentMiddleware
from langchain.tools import ToolRuntime
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, ToolMessage
from langchain_core.messages.utils import count_tokens_approximately
from langgraph.config import get_config
from typing_extensions import TypedDict

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from langchain.agents.middleware.types import ModelRequest, ModelResponse
    from langchain_core.runnables.config import RunnableConfig
    from langchain_core.tools import BaseTool
    from langgraph.runtime import Runtime

    from bog_agents.backends.protocol import BACKEND_TYPES, BackendProtocol

logger = logging.getLogger(__name__)

__all__ = [
    "StreetSweeperMiddleware",
    "SweepAction",
    "SweepLog",
]

# A conservative default set of tool names. ``read`` / ``read_file`` produce
# file content; the mutating set invalidates any earlier read of the same path.
# These match the canonical bog-agents tool names (see ``summarization.py``'s
# ``write_file`` / ``edit_file`` truncation logic).
_DEFAULT_READ_TOOLS = frozenset({"read_file", "read"})
_DEFAULT_MUTATE_TOOLS = frozenset({"write_file", "edit_file", "multi_edit", "apply_patch"})

# Common path argument keys across the filesystem tools.
_PATH_ARG_KEYS = ("file_path", "path", "filename", "target_file")

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_BLANK_RUN_RE = re.compile(r"\n{3,}")

# Collapse this many or more consecutive identical lines into one + a marker.
_IDENTICAL_LINE_RUN = 4


class SweepAction(TypedDict):
    """One pruning action taken during a sweep.

    Attributes:
        technique: The sweep technique applied — one of `whitespace`, `dedup`,
            `stale_read`, or `truncate`.
        tool_name: The originating tool name (or `""` for non-tool messages).
        tokens_before: Approximate token count of the message content before the sweep.
        tokens_after: Approximate token count after the sweep.
    """

    technique: str
    tool_name: str
    tokens_before: int
    tokens_after: int


def _input_usd_per_token(model_name: str | None) -> float:
    """Estimate the USD cost of a single input token for `model_name`.

    Reuses `cost_tracker._MODEL_COSTS` as the single source of truth for pricing
    so the sweeper's dollar figures stay consistent with the cost tracker. The
    table is keyed by bare model name, so we strip any provider prefix
    (`anthropic:`) and fall back to a substring match, then to a conservative
    default for unknown models.

    Args:
        model_name: The model spec (e.g. `anthropic:claude-sonnet-4-6`), or `None`.

    Returns:
        Estimated USD per input token.
    """
    default = 5.0 / 1_000_000
    if not model_name:
        return default
    try:
        from bog_agents.middleware.cost_tracker import _MODEL_COSTS
    except Exception:  # pragma: no cover - defensive: pricing is best-effort
        return default
    name = model_name.split(":")[-1]
    costs = _MODEL_COSTS.get(name)
    if costs is None:
        for key, candidate in _MODEL_COSTS.items():
            if key in name or name in key:
                costs = candidate
                break
    if costs is None:
        costs = (5.0, 15.0)
    return costs[0] / 1_000_000


@dataclass
class SweepLog:
    """Cumulative record of everything the street sweeper has removed.

    Token totals accumulate *per model call* — a message swept on three
    successive calls is counted three times, because the swept tokens are not
    billed on each of those three requests. That per-call accumulation is what
    makes `tokens_saved` (and `dollars_saved`) map directly to money not spent.

    Attributes:
        actions_total: Total number of sweep actions applied across the session.
        calls_swept: Number of model calls on which at least one action fired.
        tokens_before_total: Summed pre-sweep tokens across all swept messages.
        tokens_after_total: Summed post-sweep tokens across all swept messages.
        by_technique: Tokens saved, keyed by technique name.
        counts_by_technique: Action counts, keyed by technique name.
        recent: A capped ring of the most recent `SweepAction` entries.
        usd_per_input_token: Price used to convert saved tokens to dollars.
    """

    actions_total: int = 0
    calls_swept: int = 0
    tokens_before_total: int = 0
    tokens_after_total: int = 0
    by_technique: dict[str, int] = field(default_factory=dict)
    counts_by_technique: dict[str, int] = field(default_factory=dict)
    recent: list[SweepAction] = field(default_factory=list)
    usd_per_input_token: float = 0.0
    _recent_cap: int = 200

    @property
    def tokens_saved(self) -> int:
        """Approximate total tokens removed from model requests so far."""
        return max(0, self.tokens_before_total - self.tokens_after_total)

    @property
    def dollars_saved(self) -> float:
        """Estimated USD not spent — `tokens_saved` priced at the input rate.

        This is a pre-cache-discount upper bound: tokens that would have been
        served from the prompt cache are billed at a fraction of the input rate,
        so the realized saving is somewhat lower.
        """
        return self.tokens_saved * self.usd_per_input_token

    @property
    def reduction_pct(self) -> float:
        """Tokens saved as a percentage of the swept content's original size."""
        if self.tokens_before_total <= 0:
            return 0.0
        return 100.0 * self.tokens_saved / self.tokens_before_total

    def record(self, action: SweepAction) -> None:
        """Accumulate a single sweep action into the running totals.

        Args:
            action: The action to record.
        """
        self.actions_total += 1
        self.tokens_before_total += action["tokens_before"]
        self.tokens_after_total += action["tokens_after"]
        saved = max(0, action["tokens_before"] - action["tokens_after"])
        technique = action["technique"]
        self.by_technique[technique] = self.by_technique.get(technique, 0) + saved
        self.counts_by_technique[technique] = self.counts_by_technique.get(technique, 0) + 1
        self.recent.append(action)
        if len(self.recent) > self._recent_cap:
            del self.recent[: len(self.recent) - self._recent_cap]

    def record_call(self) -> None:
        """Mark that one model call was swept (>= 1 action)."""
        self.calls_swept += 1

    def to_dict(self) -> dict[str, Any]:
        """Return a serializable snapshot of the metrics (for export/persistence).

        Returns:
            A plain dict of all cumulative metrics.
        """
        return {
            "actions_total": self.actions_total,
            "calls_swept": self.calls_swept,
            "tokens_before_total": self.tokens_before_total,
            "tokens_after_total": self.tokens_after_total,
            "tokens_saved": self.tokens_saved,
            "dollars_saved": self.dollars_saved,
            "reduction_pct": self.reduction_pct,
            "by_technique": dict(self.by_technique),
            "counts_by_technique": dict(self.counts_by_technique),
        }

    def format_summary(self) -> str:
        """Render a human-readable summary of cumulative savings.

        Returns:
            A multi-line string suitable for the CLI `/sweep status` output.
        """
        if self.actions_total == 0:
            return "Street sweeper: no context pruned yet this session."
        lines = [
            f"Tokens removed: ~{self.tokens_saved:,} ({self.reduction_pct:.0f}% of swept content) over {self.calls_swept} model calls, {self.actions_total} actions.",
        ]
        if self.usd_per_input_token > 0:
            lines.append(f"Estimated savings: ~${self.dollars_saved:,.4f} (input-rate, pre cache-discount).")
        for technique, saved in sorted(self.by_technique.items(), key=lambda kv: kv[1], reverse=True):
            count = self.counts_by_technique.get(technique, 0)
            lines.append(f"  - {technique}: ~{saved:,} tokens ({count} actions)")
        return "\n".join(lines)


@dataclass
class _Plan:
    """Result of planning a sweep over a message list.

    Attributes:
        messages: The reshaped message list (same length/order as the input).
        actions: The sweep actions that were applied.
        offloads: `(marker, header, original_content)` tuples for dropped content.
    """

    messages: list[AnyMessage]
    actions: list[SweepAction]
    offloads: list[tuple[str, str, str]]


def _content_to_text(content: Any) -> str | None:
    """Return plain text for message content, or `None` if not safely sweepable.

    Only `str` content is swept. List/structured content (multimodal blocks,
    tool-use blocks) is left untouched to avoid corrupting non-text payloads.

    Args:
        content: The raw `message.content` value.

    Returns:
        The text to sweep, or `None` when the content should be skipped.
    """
    if isinstance(content, str):
        return content
    return None


def _collapse_identical_runs(lines: list[str]) -> list[str]:
    """Collapse runs of >= `_IDENTICAL_LINE_RUN` identical lines into one + a marker.

    Targets repeated stack frames and repeated log lines. The replacement notes
    how many lines were collapsed so the signal ("this repeated a lot") is kept.

    Args:
        lines: The already-rstripped lines.

    Returns:
        The collapsed line list.
    """
    out: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        j = i
        while j < n and lines[j] == lines[i]:
            j += 1
        run = j - i
        if run >= _IDENTICAL_LINE_RUN and lines[i].strip():
            out.append(lines[i])
            out.append(f"... (x{run} identical lines)")
        else:
            out.extend(lines[i:j])
        i = j
    return out


def _normalize_text(text: str) -> str:
    """Apply Tier 0 lossless mechanical cleanup to a block of text.

    Strips ANSI escapes and trailing per-line whitespace, collapses long runs of
    identical lines, and squeezes 3+ blank lines down to one. Deterministic and
    idempotent — running it twice yields the same result, which keeps the prompt
    cache prefix stable.

    Args:
        text: The text to normalize.

    Returns:
        The normalized text.
    """
    text = _ANSI_RE.sub("", text)
    lines = [ln.rstrip() for ln in text.split("\n")]
    lines = _collapse_identical_runs(lines)
    out = "\n".join(lines)
    return _BLANK_RUN_RE.sub("\n\n", out)


def _truncate_head_tail(text: str, head_lines: int, tail_lines: int) -> str:
    """Keep the first `head_lines` and last `tail_lines`, eliding the middle.

    Args:
        text: The text to truncate.
        head_lines: Number of leading lines to keep.
        tail_lines: Number of trailing lines to keep.

    Returns:
        The truncated text with an elision marker noting how many lines were cut.
    """
    lines = text.split("\n")
    if len(lines) <= head_lines + tail_lines:
        return text
    cut = len(lines) - head_lines - tail_lines
    head = lines[:head_lines]
    tail = lines[-tail_lines:]
    marker = f"... ({cut} lines swept - use recall_swept to retrieve the full output) ..."
    return "\n".join([*head, marker, *tail])


class _SweepTextMemo:
    """Two-generation, identity-checked memo for per-text derived values.

    Planning re-derives the same values (SHA-1, Tier-0 normalization, Tier-2
    truncation) from the same canonical message texts on every model call,
    because the sweep is by design a fresh per-call view transformation. The
    texts themselves are immutable and long-lived (they sit in LangGraph
    state), so this memo caches the derivations across calls (v5 PERF-3).

    Entries are keyed by `id(text)` with the text object strongly referenced
    inside the entry and verified with `is` on every hit, so a recycled id can
    never alias a different string. Entries not touched during a planning pass
    are dropped when the pass ends (`rotate`), so the memo only ever holds
    references to strings in the live history — near-zero extra memory — and
    cannot grow unboundedly.
    """

    def __init__(self) -> None:
        self._live: dict[int, dict[str, Any]] = {}
        self._next: dict[int, dict[str, Any]] = {}

    def entry(self, text: str) -> dict[str, Any]:
        """Return the (possibly new) memo entry for `text`, marking it live.

        Args:
            text: The exact text object to memoize derivations for.

        Returns:
            A mutable per-text dict; callers stash derived values under their
            own keys (`sha` / `norm` / `trunc`).
        """
        key = id(text)
        entry = self._next.get(key)
        if entry is not None and entry["text"] is text:
            return entry
        entry = self._live.get(key)
        if entry is None or entry["text"] is not text:
            entry = {"text": text}
        self._next[key] = entry
        return entry

    def rotate(self) -> None:
        """End a planning pass: keep entries touched this pass, drop the rest."""
        self._live = self._next
        self._next = {}


class StreetSweeperMiddleware(AgentMiddleware):
    """Continuously prune dead weight from each model request.

    See the module docstring for the full design. This middleware preserves
    message count and order, mutating only the text content of `ToolMessage` and
    `AIMessage` objects outside the protected recent window.

    Args:
        enabled: When `False`, the middleware is a transparent pass-through.
        aggressive: Enable Tier 2 (head/tail truncation of large old outputs).
        keep_recent: Number of trailing messages never swept.
        head_lines: Leading lines kept when truncating (Tier 2).
        tail_lines: Trailing lines kept when truncating (Tier 2).
        truncate_min_lines: Minimum line count before a tool output is eligible
            for Tier 2 truncation.
        backend: Backend (instance or factory) used to offload dropped content
            for `recall_swept`. When `None`, offload is disabled and dropped
            content is recoverable only from raw LangGraph state.
        history_path_prefix: Path prefix for the per-thread offload file.
        read_tools: Tool names whose results are file reads (for stale-read detection).
        mutate_tools: Tool names that invalidate earlier reads of the same path.
    """

    def __init__(
        self,
        *,
        enabled: bool = True,
        aggressive: bool = True,
        keep_recent: int = 6,
        head_lines: int = 16,
        tail_lines: int = 16,
        truncate_min_lines: int = 40,
        backend: BACKEND_TYPES | None = None,
        model_name: str | None = None,
        on_commit: Callable[[dict[str, Any]], None] | None = None,
        history_path_prefix: str = "/swept_context",
        read_tools: frozenset[str] = _DEFAULT_READ_TOOLS,
        mutate_tools: frozenset[str] = _DEFAULT_MUTATE_TOOLS,
    ) -> None:
        self.enabled = enabled
        self.aggressive = aggressive
        self.keep_recent = max(0, keep_recent)
        self.head_lines = max(1, head_lines)
        self.tail_lines = max(1, tail_lines)
        self.truncate_min_lines = max(head_lines + tail_lines + 1, truncate_min_lines)
        self._backend = backend
        self._model_name = model_name
        # Fired once per swept model call with a per-turn delta dict; used by the
        # CLI to accumulate a persistent, cross-session savings ledger.
        self.on_commit = on_commit
        self._history_path_prefix = history_path_prefix
        self._read_tools = read_tools
        self._mutate_tools = mutate_tools
        self.sweep_log = SweepLog(usd_per_input_token=_input_usd_per_token(model_name))
        # tool_call_ids whose original has been successfully offloaded this
        # session, so the same content is never written twice. Markers are added
        # only after the backend write succeeds, so a failed write is retried on
        # the next model call instead of being lost.
        self._offloaded: set[str] = set()
        # Cross-call memo for per-text SHA-1 / normalization / truncation.
        self._memo = _SweepTextMemo()
        self.tools: list[BaseTool] = [self._build_recall_tool()]

    def set_backend(self, backend: BACKEND_TYPES | None) -> None:
        """Point the sweeper at a backend for offload/recall.

        Used by long-lived (singleton) instances that are constructed before the
        agent's backend exists and re-attached on each agent rebuild.

        Args:
            backend: The backend instance or factory, or `None` to disable offload.
        """
        self._backend = backend

    def set_pricing(self, model_name: str | None) -> None:
        """Update the model used to price saved tokens into dollars.

        Args:
            model_name: The model spec to price against (e.g. `anthropic:claude-sonnet-4-6`).
        """
        self._model_name = model_name
        self.sweep_log.usd_per_input_token = _input_usd_per_token(model_name)

    # ------------------------------------------------------------------ planning

    @staticmethod
    def _tool_call_index(messages: list[AnyMessage]) -> dict[str, tuple[str, dict[str, Any]]]:
        """Map each tool_call_id to its (tool_name, args) from AIMessage tool calls.

        Args:
            messages: The full message list.

        Returns:
            A dict keyed by tool_call_id.
        """
        index: dict[str, tuple[str, dict[str, Any]]] = {}
        for msg in messages:
            if isinstance(msg, AIMessage):
                for call in msg.tool_calls or []:
                    call_id = call.get("id")
                    if call_id:
                        index[call_id] = (call.get("name", ""), call.get("args", {}) or {})
        return index

    @staticmethod
    def _path_of(args: dict[str, Any]) -> str | None:
        """Extract a file path from tool-call args, trying common key names.

        Args:
            args: The tool call argument dict.

        Returns:
            The path string, or `None` if no recognizable path key is present.
        """
        for key in _PATH_ARG_KEYS:
            value = args.get(key)
            if isinstance(value, str) and value:
                return value
        return None

    def _superseded_read_indices(
        self,
        messages: list[AnyMessage],
        call_index: dict[str, tuple[str, dict[str, Any]]],
    ) -> set[int]:
        """Find message indices of file reads that a later read/edit has superseded.

        A read of path X at index i is stale if any later message (j > i) is a
        read of X (re-read) or a mutation of X (write/edit). The most recent read
        of each path is always kept.

        Args:
            messages: The full message list.
            call_index: Map from tool_call_id to (tool_name, args).

        Returns:
            The set of message indices whose content should be replaced with a stub.
        """
        # Collect (index, path, is_read) events in order.
        events: list[tuple[int, str, bool]] = []
        for i, msg in enumerate(messages):
            if not isinstance(msg, ToolMessage):
                continue
            name, args = call_index.get(msg.tool_call_id, ("", {}))
            tool_name = msg.name or name
            path = self._path_of(args)
            if path is None:
                continue
            if tool_name in self._read_tools:
                events.append((i, path, True))
            elif tool_name in self._mutate_tools:
                events.append((i, path, False))

        # Single reverse pass (v5 PERF-3): a read is stale iff its path appears
        # in any later event, so walking backwards with a seen-later set gives
        # the same answer as the old per-event tail scan in O(E) not O(E^2).
        stale: set[int] = set()
        seen_later: set[str] = set()
        for idx, path, is_read in reversed(events):
            if is_read and path in seen_later:
                stale.add(idx)
            seen_later.add(path)
        return stale

    # ------------------------------------------------------------------ memoized derivations

    def _sha_of(self, text: str) -> str:
        """Return (and memoize) the SHA-1 hex digest of `text`."""
        entry = self._memo.entry(text)
        digest = entry.get("sha")
        if digest is None:
            digest = hashlib.sha1(text.encode("utf-8", "ignore")).hexdigest()
            entry["sha"] = digest
        return digest

    def _normalized_of(self, text: str) -> str:
        """Return (and memoize) the Tier-0 normalization of `text`."""
        entry = self._memo.entry(text)
        normalized = entry.get("norm")
        if normalized is None:
            normalized = _normalize_text(text)
            entry["norm"] = normalized
        return normalized

    def _truncated_of(self, text: str) -> str:
        """Return (and memoize) the Tier-2 head/tail truncation of `text`.

        The memoized value is keyed to the head/tail parameters it was built
        with, so a reconfigured instance never serves a stale truncation.
        """
        entry = self._memo.entry(text)
        params = (self.head_lines, self.tail_lines)
        if entry.get("trunc_params") != params:
            entry["trunc"] = _normalize_text(_truncate_head_tail(text, self.head_lines, self.tail_lines))
            entry["trunc_params"] = params
        return entry["trunc"]

    def _plan(self, messages: list[AnyMessage]) -> _Plan:
        """Plan a sweep over `messages` without mutating the input.

        Args:
            messages: The full message list from the request.

        Returns:
            A `_Plan` with the reshaped messages, applied actions, and offloads.
        """
        new_messages = list(messages)
        actions: list[SweepAction] = []
        offloads: list[tuple[str, str, str]] = []

        eligible_end = len(messages) - self.keep_recent
        if eligible_end <= 0:
            return _Plan(new_messages, actions, offloads)

        try:
            call_index = self._tool_call_index(messages)
            stale_indices = self._superseded_read_indices(messages, call_index)

            # Dedup: keep the LAST occurrence of each identical tool-output hash;
            # stub earlier eligible duplicates. Hash over all messages so a recent
            # copy outside the window can still "win". The per-index hashes are
            # kept so `_plan_one` never hashes the same text twice.
            last_seen: dict[str, int] = {}
            hash_by_index: dict[int, str] = {}
            for i, msg in enumerate(messages):
                if isinstance(msg, ToolMessage):
                    text = _content_to_text(msg.content)
                    if text is not None and text.strip():
                        digest = self._sha_of(text)
                        hash_by_index[i] = digest
                        last_seen[digest] = i

            for i in range(eligible_end):
                msg = messages[i]
                try:
                    planned = self._plan_one(msg, i, stale_indices, last_seen, hash_by_index)
                except Exception:  # never let planning break the model call
                    logger.debug("street_sweeper: planning failed for message %d", i, exc_info=True)
                    continue
                if planned is None:
                    continue
                new_msg, action, offload = planned
                new_messages[i] = new_msg
                actions.append(action)
                if offload is not None:
                    offloads.append(offload)
        finally:
            self._memo.rotate()

        return _Plan(new_messages, actions, offloads)

    def _plan_one(
        self,
        msg: AnyMessage,
        index: int,
        stale_indices: set[int],
        last_seen: dict[str, int],
        hash_by_index: dict[int, str],
    ) -> tuple[AnyMessage, SweepAction, tuple[str, str, str] | None] | None:
        """Plan the sweep for a single message.

        Resolution priority: stale_read > dedup > truncate > whitespace.
        Stubbing techniques (stale_read/dedup) offload the dropped original;
        whitespace cleanup is lossless and offloads nothing.

        Args:
            msg: The message to consider.
            index: Its index in the full list.
            stale_indices: Indices flagged as superseded reads.
            last_seen: Map from content hash to the last index it appears at.
            hash_by_index: Map from message index to its content hash (computed
                once in `_plan`'s dedup pass).

        Returns:
            A `(new_message, action, offload_or_none)` tuple, or `None` if the
            message is left unchanged.
        """
        # Human input and summaries are sacred; only tool output and AI text.
        if isinstance(msg, HumanMessage):
            return None
        text = _content_to_text(msg.content)
        if text is None or not text.strip():
            return None

        tool_name = msg.name or "" if isinstance(msg, ToolMessage) else ""
        before = count_tokens_approximately([msg])

        if isinstance(msg, ToolMessage):
            marker = msg.tool_call_id or f"idx-{index}"
            content_hash = hash_by_index.get(index) or self._sha_of(text)

            if index in stale_indices:
                stub = f'[stale: this file read was superseded by a later read/edit. Use recall_swept("{marker}") for the original.]'
                return self._stub(msg, stub, "stale_read", tool_name, before, marker, text)

            if last_seen.get(content_hash, index) > index:
                stub = f'[duplicate: identical to a later tool result. Use recall_swept("{marker}") for the original.]'
                return self._stub(msg, stub, "dedup", tool_name, before, marker, text)

            if self.aggressive and text.count("\n") + 1 >= self.truncate_min_lines:
                truncated = self._truncated_of(text)
                if truncated != text:
                    new_msg = msg.model_copy(update={"content": truncated})
                    after = count_tokens_approximately([new_msg])
                    action: SweepAction = {"technique": "truncate", "tool_name": tool_name, "tokens_before": before, "tokens_after": after}
                    header = self._offload_header(marker, tool_name, "truncate")
                    return new_msg, action, (marker, header, text)

        # Tier 0 lossless cleanup (tool output or AI text).
        normalized = self._normalized_of(text)
        if normalized == text:
            return None
        new_msg = msg.model_copy(update={"content": normalized})
        after = count_tokens_approximately([new_msg])
        action = {"technique": "whitespace", "tool_name": tool_name, "tokens_before": before, "tokens_after": after}
        return new_msg, action, None

    def _stub(
        self,
        msg: AnyMessage,
        stub: str,
        technique: str,
        tool_name: str,
        before: int,
        marker: str,
        original: str,
    ) -> tuple[AnyMessage, SweepAction, tuple[str, str, str]]:
        """Build the replacement message + action + offload for a stubbing technique.

        Args:
            msg: The original message.
            stub: The one-line replacement content.
            technique: The sweep technique name.
            tool_name: The originating tool name.
            before: Pre-sweep token count.
            marker: The recall marker (tool_call_id).
            original: The original content to offload.

        Returns:
            A `(new_message, action, offload)` tuple.
        """
        new_msg = msg.model_copy(update={"content": stub})
        after = count_tokens_approximately([new_msg])
        action: SweepAction = {"technique": technique, "tool_name": tool_name, "tokens_before": before, "tokens_after": after}
        return new_msg, action, (marker, self._offload_header(marker, tool_name, technique), original)

    @staticmethod
    def _offload_header(marker: str, tool_name: str, technique: str) -> str:
        """Build the markdown section header for an offloaded original.

        Args:
            marker: The recall marker (tool_call_id).
            tool_name: The originating tool name.
            technique: The sweep technique that dropped the content.

        Returns:
            A markdown `###` header line including a UTC timestamp.
        """
        timestamp = datetime.now(UTC).isoformat()
        return f"### swept {marker} | {tool_name or 'tool'} | {technique} | {timestamp}"

    # ------------------------------------------------------------------ offload

    def _get_thread_id(self) -> str:
        """Return the current langgraph `thread_id`, or a generated session id.

        Returns:
            The thread id string.
        """
        try:
            config = get_config()
            thread_id = config.get("configurable", {}).get("thread_id")
            if thread_id is not None:
                return str(thread_id)
        except RuntimeError:
            pass
        return f"session_{uuid.uuid4().hex[:8]}"

    def _history_path(self) -> str:
        """Return the per-thread offload file path."""
        return f"{self._history_path_prefix}/{self._get_thread_id()}.md"

    def _resolve_backend(self, state: Any, runtime: Runtime) -> BackendProtocol | None:
        """Resolve the backend from an instance or factory, mirroring summarization.

        Args:
            state: The current agent state.
            runtime: The runtime context (for factory backends).

        Returns:
            A resolved backend, or `None` when offload is disabled.
        """
        if self._backend is None:
            return None
        if callable(self._backend):
            config = cast("RunnableConfig", getattr(runtime, "config", {}))
            tool_runtime = ToolRuntime(
                state=state,
                context=runtime.context,
                stream_writer=runtime.stream_writer,
                store=runtime.store,
                config=config,
                tool_call_id=None,
            )
            return self._backend(tool_runtime)  # ty: ignore[call-top-callable]
        return self._backend

    def _pending_offloads(self, offloads: list[tuple[str, str, str]]) -> tuple[str, list[str]]:
        """Render not-yet-offloaded sections; do NOT mark them written yet.

        Markers are marked offloaded only once the backend write succeeds
        (see `_offload`/`_aoffload`), so a failed write is retried on the next
        model call instead of leaving a stub whose original can never be
        recalled (v5 CTX-4).

        Args:
            offloads: All offloads planned this turn.

        Returns:
            A `(addition, markers)` pair: the concatenated markdown for sections
            not previously offloaded (empty when there is nothing new) and the
            markers those sections cover, in order.
        """
        sections: list[str] = []
        markers: list[str] = []
        seen: set[str] = set()
        for marker, header, content in offloads:
            if marker in self._offloaded or marker in seen:
                continue
            seen.add(marker)
            markers.append(marker)
            sections.append(f"{header}\n\n{content}\n\n")
        return "".join(sections), markers

    def _offload(self, backend: BackendProtocol, addition: str, markers: list[str]) -> None:
        """Append rendered sections to the per-thread offload file (sync, best-effort).

        Args:
            backend: The resolved backend.
            addition: The markdown to append.
            markers: The markers `addition` covers; recorded as offloaded only
                on a successful write.
        """
        path = self._history_path()
        existing = ""
        try:
            responses = backend.download_files([path])
            if responses and responses[0].content is not None and responses[0].error is None:
                existing = responses[0].content.decode("utf-8")
        except Exception:
            logger.debug("street_sweeper: no existing offload file at %s", path, exc_info=True)
        try:
            combined = existing + addition
            if existing:
                backend.edit(path, existing, combined)
            else:
                backend.write(path, combined)
        except Exception:
            logger.warning("street_sweeper: failed to offload swept content to %s", path, exc_info=True)
            return
        self._offloaded.update(markers)

    async def _aoffload(self, backend: BackendProtocol, addition: str, markers: list[str]) -> None:
        """Append rendered sections to the per-thread offload file (async, best-effort).

        Args:
            backend: The resolved backend.
            addition: The markdown to append.
            markers: The markers `addition` covers; recorded as offloaded only
                on a successful write.
        """
        path = self._history_path()
        existing = ""
        try:
            responses = await backend.adownload_files([path])
            if responses and responses[0].content is not None and responses[0].error is None:
                existing = responses[0].content.decode("utf-8")
        except Exception:
            logger.debug("street_sweeper: no existing offload file at %s", path, exc_info=True)
        try:
            combined = existing + addition
            if existing:
                await backend.aedit(path, existing, combined)
            else:
                await backend.awrite(path, combined)
        except Exception:
            logger.warning("street_sweeper: failed to offload swept content to %s", path, exc_info=True)
            return
        self._offloaded.update(markers)

    def _commit_actions(self, actions: list[SweepAction]) -> None:
        """Record actions to the sweep log and emit a debug line.

        Args:
            actions: The actions applied this turn.
        """
        if not actions:
            return
        for action in actions:
            self.sweep_log.record(action)
        self.sweep_log.record_call()
        before = sum(a["tokens_before"] for a in actions)
        after = sum(a["tokens_after"] for a in actions)
        saved = max(0, before - after)
        logger.debug("street_sweeper: %d actions this turn, ~%d tokens removed", len(actions), saved)
        if self.on_commit is not None:
            delta = {
                "tokens_before": before,
                "tokens_after": after,
                "tokens_saved": saved,
                "actions": len(actions),
                "dollars_saved": saved * self.sweep_log.usd_per_input_token,
            }
            try:
                self.on_commit(delta)
            except Exception:  # a metrics sink must never break the model call
                logger.debug("street_sweeper: on_commit hook failed", exc_info=True)

    # ------------------------------------------------------------------ hooks

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        """Sweep the request's messages before the model is called (sync).

        Args:
            request: The model request to reshape.
            handler: The downstream handler to invoke with the swept request.

        Returns:
            The model response from the handler.
        """
        if not self.enabled:
            return handler(request)
        try:
            plan = self._plan(request.messages)
        except Exception:
            logger.warning("street_sweeper: sweep planning failed; passing request through unchanged", exc_info=True)
            return handler(request)
        if not plan.actions:
            return handler(request)

        backend = self._resolve_backend(request.state, request.runtime)
        if backend is not None and plan.offloads:
            addition, markers = self._pending_offloads(plan.offloads)
            if addition:
                self._offload(backend, addition, markers)
        self._commit_actions(plan.actions)
        return handler(request.override(messages=plan.messages))

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        """Sweep the request's messages before the model is called (async).

        Args:
            request: The model request to reshape.
            handler: The downstream async handler to invoke with the swept request.

        Returns:
            The model response from the handler.
        """
        if not self.enabled:
            return await handler(request)
        try:
            plan = self._plan(request.messages)
        except Exception:
            logger.warning("street_sweeper: sweep planning failed; passing request through unchanged", exc_info=True)
            return await handler(request)
        if not plan.actions:
            return await handler(request)

        backend = self._resolve_backend(request.state, request.runtime)
        if backend is not None and plan.offloads:
            addition, markers = self._pending_offloads(plan.offloads)
            if addition:
                await self._aoffload(backend, addition, markers)
        self._commit_actions(plan.actions)
        return await handler(request.override(messages=plan.messages))

    # ------------------------------------------------------------------ recall tool

    def _build_recall_tool(self) -> BaseTool:
        """Build the `recall_swept` tool that pulls offloaded originals back.

        Returns:
            A `StructuredTool` exposing the offloaded content for retrieval.
        """
        from langchain_core.tools import StructuredTool

        mw = self

        def _read(runtime: ToolRuntime, marker: str) -> str:
            backend = mw._resolve_backend(runtime.state, runtime)
            if backend is None:
                return "Street sweeper offload is disabled; no swept content is available to recall."
            path = mw._history_path()
            try:
                responses = backend.download_files([path])
            except Exception:
                logger.debug("recall_swept: download failed for %s", path, exc_info=True)
                responses = None
            if not responses or responses[0].content is None or responses[0].error is not None:
                return "No swept context has been offloaded yet."
            content = responses[0].content.decode("utf-8", "ignore")
            return mw._extract_section(content, marker)

        def sync_recall(runtime: ToolRuntime, marker: str = "") -> str:
            return _read(runtime, marker)

        async def async_recall(runtime: ToolRuntime, marker: str = "") -> str:
            backend = mw._resolve_backend(runtime.state, runtime)
            if backend is None:
                return "Street sweeper offload is disabled; no swept content is available to recall."
            path = mw._history_path()
            try:
                responses = await backend.adownload_files([path])
            except Exception:
                logger.debug("recall_swept: async download failed for %s", path, exc_info=True)
                responses = None
            if not responses or responses[0].content is None or responses[0].error is not None:
                return "No swept context has been offloaded yet."
            content = responses[0].content.decode("utf-8", "ignore")
            return mw._extract_section(content, marker)

        return StructuredTool.from_function(
            name="recall_swept",
            description=(
                "Retrieve content the street sweeper pruned from your context. "
                "Call with no argument to list available markers, or pass a marker "
                "(the id shown in a [stale]/[duplicate]/elision stub) to get the full original output."
            ),
            func=sync_recall,
            coroutine=async_recall,
        )

    @staticmethod
    def _extract_section(content: str, marker: str) -> str:
        """Return one offloaded section by marker, or an index of all markers.

        Args:
            content: The full offload file content.
            marker: The marker to extract, or `""` to list available markers.

        Returns:
            The requested section, a marker index, or a not-found message.
        """
        sections = content.split("### swept ")
        entries: list[tuple[str, str]] = []
        for raw in sections[1:]:
            header, _, body = raw.partition("\n")
            entry_marker = header.split(" | ", 1)[0].strip()
            entries.append((entry_marker, body.strip()))

        if not marker:
            if not entries:
                return "No swept context has been offloaded yet."
            listed = "\n".join(f"  - {m}" for m, _ in entries)
            return f"Available swept markers (pass one to recall_swept):\n{listed}"

        for entry_marker, body in entries:
            if entry_marker == marker:
                return body
        return f"No swept content found for marker '{marker}'."
