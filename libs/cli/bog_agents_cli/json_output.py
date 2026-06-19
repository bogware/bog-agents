"""Enhanced JSON output modes for headless/programmatic usage.

Feature #6: Structured JSON output — provides JSON and streaming-JSON
output formats for CI/CD, scripting, and programmatic integration.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


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
