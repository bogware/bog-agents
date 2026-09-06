"""Cache-bust diagnostics (ROADMAP #52): name the segment that broke the cached prefix.

Provider prompt caching only pays off while the request prefix — the system
prompt and the leading messages — is byte-identical to the previous call.
When a middleware injects a segment that changes every turn (a timestamp, a
re-rendered memory block, a todo list) or when the history is rewritten
(summarization, the street sweeper), every call misses and nobody can tell
which layer did it. This middleware sits **innermost** (closest to the model),
fingerprints the prefix on every call and, when it changes, records *where*:
the markdown section of the system prompt that diverged, or the message index
at which the history stopped being a prefix of the previous one.

It is a pure observer: it never edits the request, never raises, and keeps
its own bounded in-memory event list plus an optional JSONL sink so the CLI
can render `/cost cache`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware, ModelRequest, ModelResponse
from langgraph.config import get_config

logger = logging.getLogger(__name__)

_MAX_EVENTS = 200
_HEADER_RE = re.compile(r"^(#{1,6}\s+.+|-{3,}|={3,}|<[a-z_]+>)\s*$", re.MULTILINE)


def _text_of(value: Any) -> str:
    """Flatten a prompt or message content (str or content blocks) to text."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for block in value:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                parts.append(str(block.get("text") or block.get("content") or json.dumps(block, sort_keys=True, default=str)))
            else:
                parts.append(str(block))
        return "\n".join(parts)
    return str(value)


def _digest(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:12]


def message_fingerprints(messages: list[Any]) -> list[str]:
    """Return one stable fingerprint per message (type + text + tool calls)."""
    out: list[str] = []
    for msg in messages:
        kind = getattr(msg, "type", None) or type(msg).__name__
        text = _text_of(getattr(msg, "content", msg))
        tool_calls = getattr(msg, "tool_calls", None)
        extra = json.dumps(tool_calls, sort_keys=True, default=str) if tool_calls else ""
        out.append(_digest(f"{kind}\x00{text}\x00{extra}"))
    return out


def divergence_offset(previous: str, current: str) -> int:
    """Index of the first differing character (`len` of the shorter when one is a prefix)."""
    limit = min(len(previous), len(current))
    for i in range(limit):
        if previous[i] != current[i]:
            return i
    return limit


def section_at(text: str, offset: int) -> str:
    """Name the markdown section (nearest preceding header or divider) that contains `offset`."""
    header = "(start of system prompt)"
    for match in _HEADER_RE.finditer(text):
        if match.start() > offset:
            break
        header = match.group(1).strip()
    return header[:80]


def compare_prefix(
    previous_prompt: str | None,
    previous_messages: list[str] | None,
    prompt: str,
    messages: list[str],
) -> list[dict[str, Any]]:
    """Diff two consecutive prefixes and describe every bust (pure; used by the middleware).

    Args:
        previous_prompt: The last call's system prompt text (`None` on the first call).
        previous_messages: The last call's message fingerprints.
        prompt: This call's system prompt text.
        messages: This call's message fingerprints.

    Returns:
        Zero or more event dicts (`kind`, `detail`, plus position fields).
    """
    events: list[dict[str, Any]] = []
    if previous_prompt is not None and previous_prompt != prompt:
        offset = divergence_offset(previous_prompt, prompt)
        events.append(
            {
                "kind": "system_prompt",
                "segment": section_at(prompt, offset),
                "offset": offset,
                "delta_chars": len(prompt) - len(previous_prompt),
                "detail": "a system-prompt segment changed between calls; everything after it is a cache miss",
            }
        )
    if previous_messages is not None:
        shared = 0
        for a, b in zip(previous_messages, messages, strict=False):
            if a != b:
                break
            shared += 1
        if shared < len(previous_messages):
            if len(messages) < len(previous_messages):
                cause = "history compacted (summarization) — the cached prefix was replaced"
                kind = "history_compacted"
            else:
                cause = "an earlier message was rewritten in place (street sweeper / patch) — the prefix diverged"
                kind = "history_rewritten"
            events.append(
                {
                    "kind": kind,
                    "index": shared,
                    "previous_len": len(previous_messages),
                    "current_len": len(messages),
                    "detail": cause,
                }
            )
    return events


class CacheBustDetectorMiddleware(AgentMiddleware):
    """Innermost prefix fingerprinting that names the cache-busting segment (ROADMAP #52).

    Args:
        sink: Optional callable receiving each event dict (the CLI appends
            JSONL); `None` keeps events in memory only.
        events_dir: Optional directory; when set and `sink` is `None`, events
            are appended to `<events_dir>/<thread_id>.jsonl`.
        clock: Injectable timestamp source (tests).
    """

    def __init__(
        self,
        *,
        sink: Callable[[dict[str, Any]], None] | None = None,
        events_dir: str | Path | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._sink = sink
        self._events_dir = Path(events_dir) if events_dir else None
        self._clock = clock
        self._previous_prompt: str | None = None
        self._previous_messages: list[str] | None = None
        self.calls = 0
        self.stable_calls = 0
        self.events: list[dict[str, Any]] = []

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _thread_id() -> str:
        try:
            thread_id = get_config().get("configurable", {}).get("thread_id")
        except RuntimeError:
            thread_id = None
        return str(thread_id) if thread_id else "session"

    def _emit(self, event: dict[str, Any]) -> None:
        self.events.append(event)
        if len(self.events) > _MAX_EVENTS:
            del self.events[: len(self.events) - _MAX_EVENTS]
        try:
            if self._sink is not None:
                self._sink(event)
            elif self._events_dir is not None:
                self._events_dir.mkdir(parents=True, exist_ok=True)
                safe = re.sub(r"[^A-Za-z0-9._-]", "_", event.get("thread_id", "session"))[:120] or "session"
                with (self._events_dir / f"{safe}.jsonl").open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(event, sort_keys=True, default=str) + "\n")
        except Exception:
            logger.debug("cache diagnostics: sink failed", exc_info=True)

    def observe(self, request: ModelRequest) -> list[dict[str, Any]]:
        """Fingerprint `request` against the previous call and record any bust.

        Args:
            request: The model request about to be sent.

        Returns:
            The events recorded for this call (empty when the prefix was stable).
        """
        try:
            prompt = _text_of(getattr(request, "system_prompt", None))
            fingerprints = message_fingerprints(list(getattr(request, "messages", None) or []))
            found = compare_prefix(self._previous_prompt, self._previous_messages, prompt, fingerprints)
            self.calls += 1
            if self._previous_prompt is not None and not found:
                self.stable_calls += 1
            if found:
                thread_id = self._thread_id()
                stamped = []
                for event in found:
                    event = {"ts": self._clock(), "thread_id": thread_id, "call": self.calls, **event}
                    self._emit(event)
                    stamped.append(event)
                found = stamped
            self._previous_prompt = prompt
            self._previous_messages = fingerprints
        except Exception:
            logger.debug("cache diagnostics: observe failed", exc_info=True)
            return []
        return found

    def summary(self) -> dict[str, Any]:
        """Counts for a status line: calls, stable calls, busts by kind."""
        by_kind: dict[str, int] = {}
        for event in self.events:
            by_kind[event.get("kind", "?")] = by_kind.get(event.get("kind", "?"), 0) + 1
        return {"calls": self.calls, "stable_calls": self.stable_calls, "busts": by_kind}

    # ------------------------------------------------------------------ hooks

    def wrap_model_call(self, request: ModelRequest, handler: Callable[[ModelRequest], ModelResponse]) -> ModelResponse:
        """Observe, then forward unchanged."""
        self.observe(request)
        return handler(request)

    async def awrap_model_call(self, request: ModelRequest, handler: Callable[[ModelRequest], Awaitable[ModelResponse]]) -> ModelResponse:
        """Observe, then forward unchanged (async)."""
        self.observe(request)
        return await handler(request)


def read_cache_events(events_dir: str | Path, thread_id: str) -> list[dict[str, Any]]:
    """Load the events recorded for `thread_id` under `events_dir` (missing file → empty)."""
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", thread_id)[:120] or "session"
    path = Path(events_dir) / f"{safe}.jsonl"
    if not path.is_file():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def format_cache_report(events: list[dict[str, Any]]) -> str:
    """Render cache-bust events as a short human report."""
    if not events:
        return "No cache busts recorded for this thread: every model call reused the previous prefix."
    by_segment: dict[str, int] = {}
    lines = [f"Cache busts recorded: {len(events)}"]
    for event in events:
        kind = event.get("kind", "?")
        if kind == "system_prompt":
            key = f"system prompt segment {event.get('segment')!r}"
        else:
            key = f"{kind} at message #{event.get('index')}"
        by_segment[key] = by_segment.get(key, 0) + 1
    for key, count in sorted(by_segment.items(), key=lambda kv: kv[1], reverse=True):
        lines.append(f"  {count:>3}x  {key}")
    lines.append("Fix the top entry first: a segment that changes every call defeats prompt caching for everything after it.")
    return "\n".join(lines)
