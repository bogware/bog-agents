"""Claude Code → TraceFile v1 exporter (Wave S, S4).

Claude Code exposes hook events through a JSON payload it passes to
any executable registered under ``hooks.PostToolUse`` (and the
related PreToolUse / UserPromptSubmit / SubagentStop /
SessionStart events). The payload shape, as of Claude Code v0.8.x::

    {
      "session_id": "abc123",
      "transcript_path": "/.../session.jsonl",
      "hook_event_name": "PostToolUse",
      "tool_name": "Bash",
      "tool_input": {"command": "ls"},
      "tool_response": {"output": "file.txt\\n", "is_error": false}
    }

This module converts those payloads (one per hook fire) into
TraceFrame objects, and offers a higher-level helper that walks an
entire ``transcript_path`` JSONL file and produces a signed
TraceFile.

The exporter is pure — no shell-out, no LLM calls. It is intended to
be invoked either:

* From a tiny shim script the user registers under their Claude
  Code hooks config (we ship an example under
  ``examples/claude_code_hook.py``); or
* From a one-off CLI flow that walks a finished session log.

Reading a transcript
--------------------

Claude Code's transcript is one JSON object per line, with a ``type``
field discriminating between user messages, assistant messages, and
tool invocations. We map them to TraceFile's ``event_kind``
vocabulary using a small lookup table.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

from bog_agents_cli.tracefile.signing import KeyMaterial
from bog_agents_cli.tracefile.spec import (
    TraceFile,
    TraceFrame,
    build_tracefile,
)

logger = logging.getLogger(__name__)


class ClaudeCodeExportError(RuntimeError):
    """Raised on shape failures specific to the Claude Code adapter."""


# Map Claude Code's transcript ``type`` field to TraceFile's
# event_kind vocabulary. The set is deliberately a strict subset of
# the bog-agents EventKind vocabulary so consumers can use the
# standard renderers without translation.
_TRANSCRIPT_KIND_MAP = {
    "user": "user_message",
    "user_message": "user_message",
    "human": "user_message",
    "assistant": "model_call",
    "ai": "model_call",
    "tool_use": "tool_call",
    "tool_call": "tool_call",
    "tool_result": "tool_result",
    "tool_response": "tool_result",
    "final": "final_answer",
    "result": "final_answer",
    "note": "note",
    "system": "note",
}


# ---------------------------------------------------------------------------
# Per-hook adapter
# ---------------------------------------------------------------------------


def claude_code_hook_to_frames(
    payload: dict[str, Any],
    *,
    next_id: int,
) -> list[TraceFrame]:
    """Convert ONE PostToolUse-shaped payload into 1-2 TraceFrames.

    Args:
        payload: The Claude Code hook JSON payload.
        next_id: The id to assign to the first frame produced.
            Subsequent frames increment from there.

    Returns:
        Up to two frames: a ``tool_call`` and a ``tool_result``
        when both are present, otherwise just the one that's
        meaningful. Callers chain frames across hook fires by
        feeding ``next_id += len(returned)``.
    """
    if not isinstance(payload, dict):
        msg = f"Hook payload must be a dict, got {type(payload).__name__}."
        raise ClaudeCodeExportError(msg)
    event_name = (payload.get("hook_event_name") or "").strip()
    if not event_name:
        msg = "Hook payload missing 'hook_event_name'."
        raise ClaudeCodeExportError(msg)

    now = time.time()
    tool_name = str(payload.get("tool_name") or "<tool>")
    tool_input = payload.get("tool_input") or {}
    tool_response = payload.get("tool_response") or {}
    session_id = str(payload.get("session_id") or "")

    frames: list[TraceFrame] = []
    if event_name in ("PreToolUse", "PostToolUse"):
        frames.append(
            TraceFrame(
                id=next_id,
                event_kind="tool_call",
                actor=tool_name,
                summary=_summarise_tool_input(tool_input),
                timestamp=now,
                parents=(),
                payload={
                    "source": "claude-code",
                    "session_id": session_id,
                    "args_keys": sorted(
                        tool_input.keys() if isinstance(tool_input, dict) else []
                    ),
                    "hook_event": event_name,
                },
            )
        )
    if event_name == "PostToolUse" and tool_response:
        is_error = bool(tool_response.get("is_error", False)) if isinstance(tool_response, dict) else False
        output_text = (
            tool_response.get("output", "") if isinstance(tool_response, dict) else str(tool_response)
        )
        if not isinstance(output_text, str):
            output_text = json.dumps(output_text, default=str)
        frames.append(
            TraceFrame(
                id=next_id + len(frames),
                event_kind="tool_result",
                actor=tool_name,
                summary=output_text[:240].replace("\n", " ").strip(),
                timestamp=now,
                parents=(next_id,) if frames else (),
                payload={
                    "source": "claude-code",
                    "session_id": session_id,
                    "is_error": is_error,
                },
            )
        )
    if event_name == "UserPromptSubmit":
        prompt = str(payload.get("prompt") or "")
        frames.append(
            TraceFrame(
                id=next_id,
                event_kind="user_message",
                actor="user",
                summary=prompt[:240],
                timestamp=now,
                parents=(),
                payload={"source": "claude-code", "session_id": session_id},
            )
        )
    return frames


# ---------------------------------------------------------------------------
# Whole-transcript walker
# ---------------------------------------------------------------------------


def parse_claude_code_session_log(path: Path | str) -> list[TraceFrame]:
    """Parse a Claude Code transcript file into TraceFrames.

    Tolerant of malformed lines — a corrupt JSON line is skipped and
    logged at debug rather than failing the whole export. Frame ids
    are assigned in iteration order so the Merkle chain is stable
    across re-exports of the same transcript.
    """
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        msg = f"Could not read Claude Code transcript {p}: {exc}"
        raise ClaudeCodeExportError(msg) from exc
    return list(_iter_transcript(text))


def _iter_transcript(text: str) -> Iterator[TraceFrame]:
    next_id = 1
    prev_id: int | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            logger.debug("claude-code: skipping unparseable line")
            continue
        frame = _entry_to_frame(obj, next_id, prev_id)
        if frame is None:
            continue
        yield frame
        prev_id = frame.id
        next_id += 1


def _entry_to_frame(
    entry: Any, next_id: int, prev_id: int | None,
) -> TraceFrame | None:
    if not isinstance(entry, dict):
        return None
    raw_kind = (entry.get("type") or entry.get("kind") or "").lower()
    event_kind = _TRANSCRIPT_KIND_MAP.get(raw_kind)
    if event_kind is None:
        return None
    actor = str(
        entry.get("model")
        or entry.get("tool_name")
        or entry.get("role")
        or event_kind
    )
    summary = _entry_summary(entry, event_kind)
    timestamp = entry.get("timestamp")
    if not isinstance(timestamp, (int, float)):
        timestamp = time.time()
    parents = (prev_id,) if prev_id is not None else ()
    payload: dict[str, Any] = {"source": "claude-code"}
    # Preserve a couple of useful fields without copying the entire
    # transcript line (which can be very large).
    for key in ("tool_use_id", "tool_call_id", "session_id"):
        value = entry.get(key)
        if value:
            payload[key] = value
    if event_kind == "tool_result" and isinstance(entry.get("content"), dict):
        payload["is_error"] = bool(entry["content"].get("is_error", False))
    return TraceFrame(
        id=next_id,
        event_kind=event_kind,
        actor=actor[:80],
        summary=summary[:240],
        timestamp=float(timestamp),
        parents=parents,
        payload=payload,
    )


def _entry_summary(entry: dict[str, Any], event_kind: str) -> str:
    """Build a short summary for one transcript line."""
    if event_kind == "user_message":
        return str(entry.get("text") or entry.get("content") or "")
    if event_kind == "model_call":
        return str(entry.get("text") or entry.get("content") or "<thinking>")
    if event_kind == "tool_call":
        args = entry.get("input") or entry.get("tool_input") or {}
        return _summarise_tool_input(args)
    if event_kind == "tool_result":
        content = entry.get("content") or entry.get("output") or ""
        return content if isinstance(content, str) else json.dumps(content, default=str)
    if event_kind == "final_answer":
        return str(entry.get("text") or entry.get("content") or "")
    return str(entry.get("text") or entry.get("content") or "")


def _summarise_tool_input(args: Any) -> str:
    if isinstance(args, dict):
        parts = [f"{k}={v!r}" for k, v in args.items()]
        joined = " ".join(parts)
        return joined[:240]
    if isinstance(args, str):
        return args[:240]
    return json.dumps(args, default=str)[:240]


# ---------------------------------------------------------------------------
# High-level: whole session → signed TraceFile
# ---------------------------------------------------------------------------


def claude_code_session_to_tracefile(
    transcript_path: Path | str,
    *,
    key: KeyMaterial,
    session_id: str | None = None,
    producer: str = "claude-code-hook/0.x",
) -> TraceFile:
    """End-to-end: parse a Claude Code transcript and sign the result.

    Args:
        transcript_path: Path to the Claude Code session JSONL file.
        key: Signing material (must have a private key).
        session_id: Override the embedded session id. When ``None``
            we derive one from the file stem so re-exports of the
            same file produce identical signed messages.
        producer: User-agent string for the TraceFile header.
    """
    p = Path(transcript_path)
    frames = parse_claude_code_session_log(p)
    if not frames:
        msg = (
            f"No usable events in transcript {p}. The file may be empty or "
            "not in the Claude Code JSONL shape."
        )
        raise ClaudeCodeExportError(msg)
    sid = session_id if session_id is not None else p.stem
    return build_tracefile(
        frames,
        key=key,
        session_id=sid,
        producer=producer,
        notes=(f"adapter=claude-code transcript={p.name}",),
    )


def hook_payload_stream_to_frames(
    payloads: Iterable[dict[str, Any]],
) -> list[TraceFrame]:
    """Walk an iterable of hook payloads, threading frame ids forward."""
    next_id = 1
    out: list[TraceFrame] = []
    for payload in payloads:
        frames = claude_code_hook_to_frames(payload, next_id=next_id)
        out.extend(frames)
        next_id += max(1, len(frames))
    return out


__all__ = [
    "ClaudeCodeExportError",
    "claude_code_hook_to_frames",
    "claude_code_session_to_tracefile",
    "hook_payload_stream_to_frames",
    "parse_claude_code_session_log",
]
