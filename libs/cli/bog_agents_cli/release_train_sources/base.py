"""Enrichment sources for ``/release-train``.

Each source (Jira, Halo) implements two transports:

* **MCP** — when a matching MCP server is registered in the user's
  MCP config, we spawn it via ``langchain_mcp_adapters``, find a
  matching tool, and invoke it per issue key. Deterministic but
  startup-heavy (one subprocess spawn).

* **REST API** — direct ``httpx`` calls to the service's REST
  endpoints. Fast, no subprocess, but requires the user to populate
  credential env vars.

The ``"auto"`` mode prefers MCP (since it implies the user has
already vetted the MCP server) and falls back to REST when MCP
isn't configured or fails to spawn.

This module is import-light: heavy deps (``httpx``,
``langchain_mcp_adapters``) are deferred to call sites. Importing
the orchestrator costs nothing.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bog_agents_cli.release_train import CommitEntry
    from bog_agents_cli.release_train_config import (
        HaloSourceConfig,
        JiraSourceConfig,
        ReleaseTrainConfig,
    )

logger = logging.getLogger(__name__)


@dataclass
class ResolvedTicket:
    """One enriched ticket/issue, ready to inject into the prompt."""

    key: str
    """The external key as it appeared in commits — e.g. ``"ABC-123"``, ``"INC456"``."""

    source: str
    """``"jira"`` or ``"halo"``."""

    summary: str = ""
    status: str = ""
    issue_type: str = ""
    url: str = ""
    fix_versions: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)
    """Anything else the source wants to expose (assignee, priority, ...)."""

    def render(self) -> str:
        """Render as a single line for the model prompt body."""
        bits = [f"[{self.key}]"]
        if self.issue_type:
            bits.append(f"({self.issue_type})")
        if self.summary:
            bits.append(self.summary)
        if self.status:
            bits.append(f"<{self.status}>")
        if self.fix_versions:
            bits.append(f"fix={','.join(self.fix_versions)}")
        return " ".join(bits)


@dataclass
class SourceResolution:
    """How a source resolved at runtime — for ``/release-train config``."""

    source: str
    transport: str
    """``"mcp"`` | ``"api"`` | ``"off"`` | ``"error"``."""

    detail: str = ""
    """Human-readable explanation: 'MCP server "atlassian" found',
    'JIRA_API_TOKEN missing', etc."""

    keys_extracted: int = 0
    keys_resolved: int = 0


# ---------------------------------------------------------------------------
# Key extraction
# ---------------------------------------------------------------------------


def extract_keys(
    commits: list[CommitEntry],
    pattern: str,
    *,
    project_filter: list[str] | None = None,
    max_keys: int = 50,
) -> list[str]:
    """Pull unique keys matching ``pattern`` from commit subjects + PR titles.

    Args:
        commits: Parsed commits.
        pattern: Regex with the key as the first captured group.
            When no group is captured, the full match is used.
        project_filter: When non-empty, only keys whose prefix (the
            substring before the first ``-``) matches one of these is
            kept. Case-sensitive. Empty/None disables filtering.
        max_keys: Cap on returned list length.

    Returns:
        A list of unique keys preserving first-seen order, capped at
        ``max_keys``.
    """
    try:
        rx = re.compile(pattern)
    except re.error as exc:
        logger.warning("release-train: invalid regex %r (%s); skipping", pattern, exc)
        return []

    project_set = {p.upper() for p in (project_filter or []) if p}
    seen: dict[str, None] = {}
    for commit in commits:
        haystacks = [commit.subject]
        if commit.pr_title and commit.pr_title != commit.subject:
            haystacks.append(commit.pr_title)
        for haystack in haystacks:
            for m in rx.finditer(haystack):
                key = m.group(1) if m.groups() else m.group(0)
                if not key:
                    continue
                if project_set:
                    prefix = key.split("-", 1)[0].upper()
                    if prefix not in project_set:
                        continue
                if key not in seen:
                    seen[key] = None
                    if len(seen) >= max_keys:
                        return list(seen.keys())
    return list(seen.keys())


# ---------------------------------------------------------------------------
# Transport resolution
# ---------------------------------------------------------------------------


def detect_mcp_server(server_name: str) -> bool:
    """Return True when an MCP server with ``server_name`` is registered.

    Reads the user's MCP configs via ``mcp_tools.discover_mcp_configs``
    and checks for a matching entry. Silent False on any error.
    """
    if not server_name:
        return False
    try:
        from bog_agents_cli.mcp_tools import (
            discover_mcp_configs,
            load_mcp_config_lenient,
        )

        for path in discover_mcp_configs():
            cfg = load_mcp_config_lenient(path)
            if not cfg:
                continue
            servers = cfg.get("mcpServers") or {}
            if isinstance(servers, dict) and server_name in servers:
                return True
    except (ImportError, OSError):
        return False
    return False


def resolve_jira_transport(
    cfg: JiraSourceConfig, env: dict[str, str] | None = None
) -> tuple[str, str]:
    """Decide which transport to use for Jira.

    Returns ``(transport, detail)`` where ``transport`` is one of
    ``"mcp"``, ``"api"``, ``"off"``.
    """
    import os

    e = env if env is not None else dict(os.environ)
    mode = (cfg.mode or "auto").lower()
    if not cfg.enabled or mode == "off":
        return ("off", "jira source disabled")

    has_mcp = detect_mcp_server(cfg.mcp_server)
    has_api = bool(cfg.api_base_url) and bool(e.get(cfg.api_token_env, ""))

    if mode == "mcp":
        if has_mcp:
            return ("mcp", f"MCP server {cfg.mcp_server!r} detected")
        return ("off", f"mode=mcp but MCP server {cfg.mcp_server!r} not configured")
    if mode == "api":
        if has_api:
            return ("api", "REST API credentials present")
        return ("off", f"mode=api but {_jira_api_missing(cfg, e)}")
    # auto
    if has_mcp:
        return ("mcp", f"MCP server {cfg.mcp_server!r} detected (auto)")
    if has_api:
        return ("api", "REST API credentials present (auto)")
    return (
        "off",
        f"auto: MCP server {cfg.mcp_server!r} not configured; "
        f"{_jira_api_missing(cfg, e)}",
    )


def _jira_api_missing(cfg: JiraSourceConfig, env: dict[str, str]) -> str:
    """Return a precise diagnostic naming exactly which Jira API piece is missing."""
    missing: list[str] = []
    if not cfg.api_base_url:
        missing.append("api_base_url empty")
    if not env.get(cfg.api_token_env):
        missing.append(f"{cfg.api_token_env} unset")
    if not missing:
        return "api credentials present (auto)"
    return " and ".join(missing)


def resolve_halo_transport(
    cfg: HaloSourceConfig, env: dict[str, str] | None = None
) -> tuple[str, str]:
    """Decide which transport to use for Halo. Same shape as ``resolve_jira_transport``."""
    import os

    e = env if env is not None else dict(os.environ)
    mode = (cfg.mode or "auto").lower()
    if not cfg.enabled or mode == "off":
        return ("off", "halo source disabled")

    has_mcp = detect_mcp_server(cfg.mcp_server)
    has_api = (
        bool(cfg.api_base_url)
        and bool(e.get(cfg.api_client_id_env, ""))
        and bool(e.get(cfg.api_client_secret_env, ""))
    )

    if mode == "mcp":
        if has_mcp:
            return ("mcp", f"MCP server {cfg.mcp_server!r} detected")
        return ("off", f"mode=mcp but MCP server {cfg.mcp_server!r} not configured")
    if mode == "api":
        if has_api:
            return ("api", "REST API credentials present")
        return ("off", f"mode=api but {_halo_api_missing(cfg, e)}")
    # auto
    if has_mcp:
        return ("mcp", f"MCP server {cfg.mcp_server!r} detected (auto)")
    if has_api:
        return ("api", "REST API credentials present (auto)")
    return (
        "off",
        f"auto: MCP server {cfg.mcp_server!r} not configured; "
        f"{_halo_api_missing(cfg, e)}",
    )


def _halo_api_missing(cfg: HaloSourceConfig, env: dict[str, str]) -> str:
    """Return a precise diagnostic naming exactly which Halo API piece is missing."""
    missing: list[str] = []
    if not cfg.api_base_url:
        missing.append("api_base_url empty")
    if not env.get(cfg.api_client_id_env):
        missing.append(f"{cfg.api_client_id_env} unset")
    if not env.get(cfg.api_client_secret_env):
        missing.append(f"{cfg.api_client_secret_env} unset")
    if not missing:
        return "api credentials present (auto)"
    return " and ".join(missing)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


async def enrich_commits(
    commits: list[CommitEntry], cfg: ReleaseTrainConfig
) -> list[SourceResolution]:
    """Run every enabled source, attach resolved tickets to commits.

    Mutates each ``CommitEntry`` to add:
      * ``jira_tickets`` — list[ResolvedTicket]
      * ``halo_tickets`` — list[ResolvedTicket]

    Returns one :class:`SourceResolution` per enabled source describing
    how it resolved (for ``/release-train config``).
    """
    resolutions: list[SourceResolution] = []

    if cfg.jira.enabled:
        from bog_agents_cli.release_train_sources.jira import resolve_jira

        try:
            resolution = await resolve_jira(commits, cfg.jira)
        except Exception as exc:
            logger.warning("release-train: jira enrichment failed: %s", exc)
            resolution = SourceResolution(
                source="jira",
                transport="error",
                detail=f"{type(exc).__name__}: {exc}",
            )
        resolutions.append(resolution)

    if cfg.halo.enabled:
        from bog_agents_cli.release_train_sources.halo import resolve_halo

        try:
            resolution = await resolve_halo(commits, cfg.halo)
        except Exception as exc:
            logger.warning("release-train: halo enrichment failed: %s", exc)
            resolution = SourceResolution(
                source="halo",
                transport="error",
                detail=f"{type(exc).__name__}: {exc}",
            )
        resolutions.append(resolution)

    return resolutions


def attach_tickets(
    commits: list[CommitEntry], tickets: list[ResolvedTicket], *, attr: str
) -> int:
    """Attach resolved tickets to commits whose subject contains the ticket key.

    Sets ``commit.<attr>`` to a list of matching tickets. Returns the
    number of commit→ticket links established.
    """
    by_key: dict[str, ResolvedTicket] = {t.key: t for t in tickets}
    if not by_key:
        return 0
    links = 0
    for commit in commits:
        haystack = f"{commit.subject} {commit.pr_title}"
        attached: list[ResolvedTicket] = []
        for key, ticket in by_key.items():
            if key in haystack:
                attached.append(ticket)
        if attached:
            setattr(commit, attr, attached)
            links += len(attached)
    return links
