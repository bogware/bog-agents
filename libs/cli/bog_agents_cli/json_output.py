"""Enhanced JSON output modes for headless/programmatic usage.

Feature #6: Structured JSON output — provides JSON and streaming-JSON
output formats for CI/CD, scripting, and programmatic integration.
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, TextIO


class OutputMode(StrEnum):
    """Output mode for the CLI."""

    TEXT = "text"
    JSON = "json"
    STREAM_JSON = "stream-json"


@dataclass
class StreamEvent:
    """A single event in the stream-json output."""

    event_type: str
    """Event type: 'message', 'tool_call', 'tool_result', 'error', 'done'."""

    data: dict[str, Any] = field(default_factory=dict)
    """Event payload."""

    timestamp: float = 0.0
    """Event timestamp."""

    def __post_init__(self) -> None:
        """Set timestamp if not provided."""
        if self.timestamp == 0:
            self.timestamp = time.time()


class JSONOutputStream:
    """Writes structured JSON events to a stream.

    Supports both full-JSON (collect all, output at end) and
    stream-JSON (newline-delimited JSON events) modes.
    """

    def __init__(
        self,
        mode: OutputMode = OutputMode.STREAM_JSON,
        stream: TextIO | None = None,
    ) -> None:
        """Initialize the JSON output stream.

        Args:
            mode: Output mode.
            stream: Output stream (defaults to stdout).
        """
        self._mode = mode
        self._stream = stream or sys.stdout
        self._events: list[StreamEvent] = []

    def emit(self, event_type: str, data: dict[str, Any] | None = None) -> None:
        """Emit an event.

        Args:
            event_type: Type of event.
            data: Event payload.
        """
        event = StreamEvent(event_type=event_type, data=data or {})
        self._events.append(event)

        if self._mode == OutputMode.STREAM_JSON:
            self._write_event(event)

    def _write_event(self, event: StreamEvent) -> None:
        """Write a single event as NDJSON.

        Args:
            event: Event to write.
        """
        line = json.dumps(
            {
                "type": event.event_type,
                "data": event.data,
                "timestamp": event.timestamp,
            }
        )
        self._stream.write(line + "\n")
        self._stream.flush()

    def finalize(self) -> str:
        """Finalize and return/write the collected output.

        Returns:
            JSON string of all events (for JSON mode).
        """
        if self._mode == OutputMode.JSON:
            result = json.dumps(
                {
                    "events": [
                        {
                            "type": e.event_type,
                            "data": e.data,
                            "timestamp": e.timestamp,
                        }
                        for e in self._events
                    ],
                    "total_events": len(self._events),
                },
                indent=2,
            )
            self._stream.write(result + "\n")
            self._stream.flush()
            return result
        return ""

    def emit_message(self, role: str, content: str) -> None:
        """Emit a message event.

        Args:
            role: Message role ('ai' or 'user').
            content: Message content.
        """
        self.emit("message", {"role": role, "content": content})

    def emit_tool_call(
        self, tool_name: str, args: dict[str, Any], call_id: str = ""
    ) -> None:
        """Emit a tool call event.

        Args:
            tool_name: Tool name.
            args: Tool arguments.
            call_id: Optional tool call ID.
        """
        self.emit(
            "tool_call",
            {
                "tool_name": tool_name,
                "arguments": args,
                "call_id": call_id,
            },
        )

    def emit_tool_result(self, tool_name: str, result: str, call_id: str = "") -> None:
        """Emit a tool result event.

        Args:
            tool_name: Tool name.
            result: Tool result.
            call_id: Optional tool call ID.
        """
        self.emit(
            "tool_result",
            {
                "tool_name": tool_name,
                "result": result[:2000],
                "call_id": call_id,
            },
        )

    def emit_error(self, error: str, code: str = "") -> None:
        """Emit an error event.

        Args:
            error: Error message.
            code: Optional error code.
        """
        self.emit("error", {"message": error, "code": code})

    def emit_done(self, summary: dict[str, Any] | None = None) -> None:
        """Emit a completion event.

        Args:
            summary: Optional session summary.
        """
        self.emit("done", summary or {})
