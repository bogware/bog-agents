"""Enrichment sources for ``/release-train`` (Jira, Halo).

Public API is re-exported from :mod:`.base`. Source-specific modules
(:mod:`.jira`, :mod:`.halo`) are imported lazily by the orchestrator
in :mod:`.base` so importing this package is cheap.
"""

from __future__ import annotations

from bog_agents_cli.release_train_sources.base import (
    ResolvedTicket,
    SourceResolution,
    attach_tickets,
    detect_mcp_server,
    enrich_commits,
    extract_keys,
    resolve_halo_transport,
    resolve_jira_transport,
)

__all__ = [
    "ResolvedTicket",
    "SourceResolution",
    "attach_tickets",
    "detect_mcp_server",
    "enrich_commits",
    "extract_keys",
    "resolve_halo_transport",
    "resolve_jira_transport",
]
