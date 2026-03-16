"""Browser and web interaction CLI interface.

Feature #24: Browser agent.
Feature #25: Live preview server.
Feature #26: API testing tool.
Feature #27: Authenticated web fetching.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class APIRequest:
    """An API request configuration."""

    url: str
    method: str = "GET"
    headers: dict[str, str] = field(default_factory=dict)
    body: str = ""
    auth_type: str = ""  # bearer, basic, api_key


@dataclass
class APIResponse:
    """An API response summary."""

    status_code: int
    elapsed_ms: float
    content_type: str = ""
    body_preview: str = ""
    headers: dict[str, str] = field(default_factory=dict)


def parse_api_command(text: str) -> APIRequest:
    """Parse an /api command into a request.

    Formats:
    - /api GET https://api.example.com/endpoint
    - /api POST https://api.example.com/data -H "Auth: Bearer token" -d '{"key": "value"}'

    Args:
        text: Command text after /api.

    Returns:
        Parsed API request.
    """
    parts = text.strip().split()
    if not parts:
        return APIRequest(url="")

    # Check if first part is a method
    methods = {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"}
    if parts[0].upper() in methods:
        method = parts[0].upper()
        url = parts[1] if len(parts) > 1 else ""
        rest = parts[2:]
    else:
        method = "GET"
        url = parts[0]
        rest = parts[1:]

    headers: dict[str, str] = {}
    body = ""

    i = 0
    while i < len(rest):
        if rest[i] == "-H" and i + 1 < len(rest):
            header = rest[i + 1].strip("\"'")
            if ":" in header:
                key, _, value = header.partition(":")
                headers[key.strip()] = value.strip()
            i += 2
        elif rest[i] == "-d" and i + 1 < len(rest):
            body = rest[i + 1].strip("\"'")
            i += 2
        else:
            i += 1

    return APIRequest(url=url, method=method, headers=headers, body=body)


def format_api_response(response: APIResponse) -> str:
    """Format an API response for display.

    Args:
        response: API response to format.

    Returns:
        Formatted string.
    """
    lines = [
        f"HTTP {response.status_code} ({response.elapsed_ms:.0f}ms)",
        f"Content-Type: {response.content_type}",
    ]
    if response.body_preview:
        # Try to pretty-print JSON
        try:
            parsed = json.loads(response.body_preview)
            lines.append(json.dumps(parsed, indent=2)[:2000])
        except json.JSONDecodeError:
            lines.append(response.body_preview[:2000])
    return "\n".join(lines)


def parse_preview_command(text: str) -> dict[str, str]:
    """Parse a /preview command.

    Formats:
    - /preview start [command] [port] — start dev server
    - /preview stop [port] — stop dev server
    - /preview status — show running servers

    Args:
        text: Command text.

    Returns:
        Parsed command dict.
    """
    parts = text.strip().split()
    action = parts[0] if parts else "status"
    command = " ".join(parts[1:]) if len(parts) > 1 else ""
    return {"action": action, "command": command}
