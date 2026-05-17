"""Vendor exporters that emit TraceFile v1 frames from other agent CLIs.

Each exporter is a small adapter that takes the foreign CLI's
hook-event payload and turns it into a TraceFrame stream. The actual
TraceFile assembly + signing still happens through
:mod:`bog_agents_cli.tracefile.spec` so signing keys stay in one
place.

Modules
-------

* :mod:`.claude_code` — adapter for Claude Code's PostToolUse hook
  payload. Documented in our README as the canonical "how to make a
  third-party CLI write TraceFile v1" pattern.
"""

from __future__ import annotations

from bog_agents_cli.tracefile.exporters.claude_code import (
    ClaudeCodeExportError,
    claude_code_hook_to_frames,
    claude_code_session_to_tracefile,
    parse_claude_code_session_log,
)

__all__ = [
    "ClaudeCodeExportError",
    "claude_code_hook_to_frames",
    "claude_code_session_to_tracefile",
    "parse_claude_code_session_log",
]
