"""Data Loss Prevention (DLP) middleware.

Feature #25: Scans all agent inputs/outputs for PII, account numbers, SSNs,
and other sensitive data. Redacts before sending to cloud LLMs.

## Overview

The DLP middleware intercepts every LLM call and scans messages for sensitive
data patterns. It can operate in two modes:

- **warn**: Logs a warning but allows the data through
- **redact**: Replaces sensitive data with placeholders before sending

## Detected Patterns

- Social Security Numbers (SSN)
- Credit card numbers
- US phone numbers
- Email addresses
- Account numbers (configurable pattern)
- API keys / tokens (common patterns)

## Usage

```python
from bog_agents.middleware.dlp import DLPMiddleware

middleware = DLPMiddleware(mode="redact")
```
"""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Annotated

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ContextT,
    ModelRequest,
    ModelResponse,
    ResponseT,
)
from langchain.tools import ToolRuntime
from langchain_core.tools import BaseTool, StructuredTool
from typing_extensions import TypedDict

logger = logging.getLogger(__name__)


@dataclass
class DLPPattern:
    """A pattern for detecting sensitive data.

    Attributes:
        name: Human-readable name for the pattern.
        pattern: Compiled regex pattern.
        replacement: Replacement string for redaction.
        category: Classification category.
    """

    name: str
    pattern: re.Pattern[str]
    replacement: str
    category: str = "pii"


# Default DLP patterns
DEFAULT_PATTERNS: list[DLPPattern] = [
    DLPPattern(
        name="SSN",
        pattern=re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
        replacement="[SSN-REDACTED]",
        category="pii",
    ),
    DLPPattern(
        name="Credit Card",
        pattern=re.compile(r"\b\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}\b"),
        replacement="[CC-REDACTED]",
        category="financial",
    ),
    DLPPattern(
        name="US Phone",
        pattern=re.compile(r"\b(?:\+1[\s-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}\b"),
        replacement="[PHONE-REDACTED]",
        category="pii",
    ),
    DLPPattern(
        name="Email",
        pattern=re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b"),
        replacement="[EMAIL-REDACTED]",
        category="pii",
    ),
    DLPPattern(
        name="API Key",
        pattern=re.compile(r"\b(?:sk|pk|api[_-]?key)[_-][A-Za-z0-9]{20,}\b", re.IGNORECASE),
        replacement="[API-KEY-REDACTED]",
        category="credential",
    ),
    DLPPattern(
        name="Bearer Token",
        pattern=re.compile(r"Bearer\s+[A-Za-z0-9\-._~+/]+=*", re.IGNORECASE),
        replacement="Bearer [TOKEN-REDACTED]",
        category="credential",
    ),
]


@dataclass
class DLPEvent:
    """A record of a DLP detection event."""

    pattern_name: str
    category: str
    action: str  # "warn" or "redact"
    message_index: int = 0
    count: int = 1


@dataclass
class DLPLog:
    """Log of all DLP events in a session."""

    events: list[DLPEvent] = field(default_factory=list)
    # Guards ``events.append`` so concurrent middleware turns can't tear
    # the list. Held only during append; readers may iterate the list
    # without holding it (Python list iteration is atomic).
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    @property
    def total_detections(self) -> int:
        """Total number of sensitive data detections."""
        return sum(e.count for e in self.events)

    @property
    def total_redactions(self) -> int:
        """Total number of redactions performed."""
        return sum(e.count for e in self.events if e.action == "redact")

    def format_summary(self) -> str:
        """Format a summary of DLP activity."""
        if not self.events:
            return "DLP: No sensitive data detected in this session."

        lines = [
            "## Data Loss Prevention Report",
            f"Total detections: {self.total_detections}",
            f"Total redactions: {self.total_redactions}",
            "",
            "### Detections by Category",
        ]

        by_cat: dict[str, int] = {}
        for e in self.events:
            by_cat[e.category] = by_cat.get(e.category, 0) + e.count

        for cat, count in sorted(by_cat.items(), key=lambda x: -x[1]):
            lines.append(f"  {cat}: {count}")

        lines.append("")
        lines.append("### Detections by Pattern")
        by_name: dict[str, int] = {}
        for e in self.events:
            by_name[e.pattern_name] = by_name.get(e.pattern_name, 0) + e.count

        for name, count in sorted(by_name.items(), key=lambda x: -x[1]):
            lines.append(f"  {name}: {count}")

        return "\n".join(lines)


def _scan_text(text: str, patterns: list[DLPPattern]) -> list[tuple[DLPPattern, int]]:
    """Scan text for sensitive data patterns.

    Args:
        text: Text to scan.
        patterns: Patterns to match against.

    Returns:
        List of (pattern, match_count) tuples.
    """
    results = []
    for p in patterns:
        matches = p.pattern.findall(text)
        if matches:
            results.append((p, len(matches)))
    return results


def _redact_text(text: str, patterns: list[DLPPattern]) -> str:
    """Redact sensitive data from text.

    Args:
        text: Text to redact.
        patterns: Patterns to apply.

    Returns:
        Redacted text.
    """
    for p in patterns:
        text = p.pattern.sub(p.replacement, text)
    return text


class DLPState(TypedDict):
    """State for DLP middleware."""


class DLPMiddleware(AgentMiddleware[DLPState, ContextT, ResponseT]):
    """Middleware for Data Loss Prevention.

    Scans agent messages for sensitive data (PII, credentials, account numbers)
    and either warns or redacts depending on the configured mode.

    Args:
        mode: Operating mode — "warn" (log only) or "redact" (replace).
        additional_patterns: Extra patterns to detect beyond defaults.
    """

    state_schema = DLPState

    def __init__(
        self,
        *,
        mode: str = "redact",
        additional_patterns: list[DLPPattern] | None = None,
    ) -> None:
        self._mode = mode
        self._patterns = list(DEFAULT_PATTERNS)
        if additional_patterns:
            self._patterns.extend(additional_patterns)
        self.log = DLPLog()
        self.tools: list[BaseTool] = self._build_tools()

    def _build_tools(self) -> list[BaseTool]:
        """Build DLP tools."""
        mw = self

        def dlp_report(
            runtime: ToolRuntime[None, DLPState],
        ) -> str:
            """Show the Data Loss Prevention activity report for this session."""
            return mw.log.format_summary()

        def scan_text(
            runtime: ToolRuntime[None, DLPState],
            text: Annotated[str, "Text to scan for sensitive data"],
        ) -> str:
            """Manually scan text for sensitive data patterns."""
            results = _scan_text(text, mw._patterns)
            if not results:
                return "No sensitive data detected."
            lines = ["Sensitive data detected:"]
            for pattern, count in results:
                lines.append(f"  - {pattern.name} ({pattern.category}): {count} occurrence(s)")
            return "\n".join(lines)

        def redact_text(
            runtime: ToolRuntime[None, DLPState],
            text: Annotated[str, "Text to redact sensitive data from"],
        ) -> str:
            """Manually redact sensitive data from text."""
            return _redact_text(text, mw._patterns)

        return [
            StructuredTool.from_function(name="dlp_report", description="Show the Data Loss Prevention activity report.", func=dlp_report),
            StructuredTool.from_function(name="scan_text", description="Scan text for sensitive data (PII, credentials, etc.).", func=scan_text),
            StructuredTool.from_function(name="redact_text", description="Redact sensitive data from text.", func=redact_text),
        ]

    def _process_messages(self, request: ModelRequest[ContextT]) -> ModelRequest[ContextT]:
        """Scan and optionally redact messages.

        In ``redact`` mode the message content is rewritten in place so that
        sensitive data never reaches the underlying model.

        Args:
            request: Model request.

        Returns:
            Possibly modified request (mutated in place).
        """
        redact = self._mode == "redact"
        for i, msg in enumerate(request.messages):
            content = getattr(msg, "content", "")
            if isinstance(content, str):
                results = _scan_text(content, self._patterns)
                if not results:
                    continue
                with self.log._lock:
                    for pattern, count in results:
                        self.log.events.append(
                            DLPEvent(
                                pattern_name=pattern.name,
                                category=pattern.category,
                                action=self._mode,
                                message_index=i,
                                count=count,
                            )
                        )
                    logger.log(
                        logging.WARNING if self._mode == "warn" else logging.INFO,
                        "DLP: %s detected %d occurrence(s) of %s in message %d (mode=%s)",
                        pattern.name,
                        count,
                        pattern.category,
                        i,
                        self._mode,
                    )
                if redact:
                    msg.content = _redact_text(content, self._patterns)
            elif isinstance(content, list):
                # Multimodal content: redact text parts in place.
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        text = part.get("text", "")
                        if not isinstance(text, str):
                            continue
                        results = _scan_text(text, self._patterns)
                        if not results:
                            continue
                        with self.log._lock:
                            for pattern, count in results:
                                self.log.events.append(
                                    DLPEvent(
                                        pattern_name=pattern.name,
                                        category=pattern.category,
                                        action=self._mode,
                                        message_index=i,
                                        count=count,
                                    )
                                )
                        if redact:
                            part["text"] = _redact_text(text, self._patterns)
        return request

    def wrap_model_call(
        self,
        request: ModelRequest[ContextT],
        call_next: Callable[[ModelRequest[ContextT]], ModelResponse[ResponseT]],
    ) -> ModelResponse[ResponseT]:
        """Scan messages for sensitive data before LLM call.

        Args:
            request: Model request.
            call_next: Handler.

        Returns:
            Model response.
        """
        self._process_messages(request)
        return call_next(request)

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        call_next: Callable[[ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]],
    ) -> ModelResponse[ResponseT]:
        """Async version.

        Args:
            request: Model request.
            call_next: Async handler.

        Returns:
            Model response.
        """
        self._process_messages(request)
        return await call_next(request)


__all__ = ["DLPLog", "DLPMiddleware", "DLPPattern"]
