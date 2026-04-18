"""MCP server registry for bog-agents CLI.

This module provides a curated local registry of 19 well-known MCP servers plus
an optional remote catalog that is fetched, cached, and merged at runtime.

Remote catalog design
---------------------
- Fetched from a public URL using stdlib only (no extra deps).
- Cached in ``~/.bog-agents/.mcp-catalog-cache.json`` with a 24-hour TTL.
- On network failure the cache is used as a fallback; if no cache exists the
  remote contribution is silently skipped — local entries are always available.
- Local entries always win: a remote entry whose ``id`` matches a local entry
  is ignored.

Usage::

    from bog_agents_cli.mcp_registry import list_entries, get_entry, search_entries

    for entry in list_entries():
        print(entry["id"], entry["display_name"])
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, TypedDict

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


class RegistryEntry(TypedDict, total=False):
    """A single MCP server registry entry."""

    id: str
    """Unique identifier (slug) for this server, e.g. ``'filesystem'``."""

    display_name: str
    """Human-readable name, e.g. ``'Filesystem'``."""

    description: str
    """Short description shown in the registry listing."""

    command: str
    """Executable to launch the server, e.g. ``'npx'``."""

    args: list[str]
    """Default CLI arguments for the command."""

    env: dict[str, str]
    """Default environment variables (may reference shell variables)."""

    tags: list[str]
    """Searchable tags, e.g. ``['filesystem', 'files']``."""


# ---------------------------------------------------------------------------
# Local registry — 19 hardcoded entries
# ---------------------------------------------------------------------------

_REGISTRY: dict[str, RegistryEntry] = {
    "filesystem": {
        "id": "filesystem",
        "display_name": "Filesystem",
        "description": "Read and write files on the local filesystem.",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", "."],
        "env": {},
        "tags": ["filesystem", "files", "read", "write"],
    },
    "github": {
        "id": "github",
        "display_name": "GitHub",
        "description": "Interact with GitHub repos, issues, and PRs via the GitHub API.",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-github"],
        "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": ""},
        "tags": ["github", "git", "issues", "pull-requests"],
    },
    "gitlab": {
        "id": "gitlab",
        "display_name": "GitLab",
        "description": "Interact with GitLab repos, issues, and merge requests.",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-gitlab"],
        "env": {"GITLAB_PERSONAL_ACCESS_TOKEN": "", "GITLAB_API_URL": ""},
        "tags": ["gitlab", "git", "issues", "merge-requests"],
    },
    "postgres": {
        "id": "postgres",
        "display_name": "PostgreSQL",
        "description": "Read-only access to PostgreSQL databases.",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-postgres", "${DATABASE_URL}"],
        "env": {"DATABASE_URL": ""},
        "tags": ["database", "postgres", "sql"],
    },
    "sqlite": {
        "id": "sqlite",
        "display_name": "SQLite",
        "description": "Read and write SQLite databases.",
        "command": "uvx",
        "args": ["mcp-server-sqlite", "--db-path", "database.db"],
        "env": {},
        "tags": ["database", "sqlite", "sql"],
    },
    "brave-search": {
        "id": "brave-search",
        "display_name": "Brave Search",
        "description": "Web and local search via the Brave Search API.",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-brave-search"],
        "env": {"BRAVE_API_KEY": ""},
        "tags": ["search", "web", "brave"],
    },
    "fetch": {
        "id": "fetch",
        "display_name": "Fetch",
        "description": "Fetch web pages and convert to Markdown for the LLM.",
        "command": "uvx",
        "args": ["mcp-server-fetch"],
        "env": {},
        "tags": ["web", "fetch", "http", "scraping"],
    },
    "memory": {
        "id": "memory",
        "display_name": "Memory",
        "description": "Persistent key-value memory store across sessions.",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-memory"],
        "env": {},
        "tags": ["memory", "knowledge", "persistence"],
    },
    "sequential-thinking": {
        "id": "sequential-thinking",
        "display_name": "Sequential Thinking",
        "description": "Step-by-step reasoning and dynamic problem decomposition.",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-sequential-thinking"],
        "env": {},
        "tags": ["reasoning", "thinking", "planning"],
    },
    "puppeteer": {
        "id": "puppeteer",
        "display_name": "Puppeteer",
        "description": "Browser automation and web scraping with Puppeteer.",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-puppeteer"],
        "env": {},
        "tags": ["browser", "automation", "puppeteer", "scraping"],
    },
    "slack": {
        "id": "slack",
        "display_name": "Slack",
        "description": "Read and post messages in Slack workspaces.",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-slack"],
        "env": {"SLACK_BOT_TOKEN": "", "SLACK_TEAM_ID": ""},
        "tags": ["slack", "messaging", "communication"],
    },
    "google-maps": {
        "id": "google-maps",
        "display_name": "Google Maps",
        "description": "Location search, routing, and place details via Google Maps API.",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-google-maps"],
        "env": {"GOOGLE_MAPS_API_KEY": ""},
        "tags": ["maps", "location", "google", "directions"],
    },
    "aws-kb-retrieval": {
        "id": "aws-kb-retrieval",
        "display_name": "AWS Knowledge Base Retrieval",
        "description": "Retrieve data from AWS Knowledge Base using Bedrock Agent Runtime.",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-aws-kb-retrieval"],
        "env": {
            "AWS_ACCESS_KEY_ID": "",
            "AWS_SECRET_ACCESS_KEY": "",
            "AWS_REGION": "",
        },
        "tags": ["aws", "knowledge-base", "bedrock", "retrieval"],
    },
    "everything": {
        "id": "everything",
        "display_name": "Everything (Demo)",
        "description": "Demo server that exercises all MCP protocol features.",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-everything"],
        "env": {},
        "tags": ["demo", "test", "everything"],
    },
    "sentry": {
        "id": "sentry",
        "display_name": "Sentry",
        "description": "Retrieve and analyse error events from Sentry.",
        "command": "uvx",
        "args": ["mcp-server-sentry", "--auth-token", "${SENTRY_AUTH_TOKEN}"],
        "env": {"SENTRY_AUTH_TOKEN": ""},
        "tags": ["sentry", "errors", "monitoring"],
    },
    "linear": {
        "id": "linear",
        "display_name": "Linear",
        "description": "Manage Linear issues, projects, and cycles.",
        "command": "npx",
        "args": ["-y", "@linear/mcp-server"],
        "env": {"LINEAR_API_KEY": ""},
        "tags": ["linear", "project-management", "issues"],
    },
    "notion": {
        "id": "notion",
        "display_name": "Notion",
        "description": "Read and write Notion pages and databases.",
        "command": "npx",
        "args": ["-y", "@notionhq/mcp"],
        "env": {"NOTION_API_KEY": ""},
        "tags": ["notion", "notes", "knowledge-base"],
    },
    "jira": {
        "id": "jira",
        "display_name": "Jira",
        "description": "Read and update Jira issues and projects.",
        "command": "npx",
        "args": ["-y", "mcp-server-jira"],
        "env": {"JIRA_HOST": "", "JIRA_EMAIL": "", "JIRA_API_TOKEN": ""},
        "tags": ["jira", "project-management", "issues", "atlassian"],
    },
    "datadog": {
        "id": "datadog",
        "display_name": "Datadog",
        "description": "Query Datadog metrics, logs, and monitors.",
        "command": "npx",
        "args": ["-y", "mcp-server-datadog"],
        "env": {"DD_API_KEY": "", "DD_APP_KEY": "", "DD_SITE": "datadoghq.com"},
        "tags": ["datadog", "monitoring", "metrics", "logs", "observability"],
    },
}

# ---------------------------------------------------------------------------
# Remote catalog constants
# ---------------------------------------------------------------------------

_DEFAULT_CATALOG_URL = (
    "https://raw.githubusercontent.com/bogware/bog-agents/main/catalog/mcp-servers.json"
)
_CACHE_TTL_SECONDS = 24 * 60 * 60  # 24 hours
_CACHE_PATH = Path.home() / ".bog-agents" / ".mcp-catalog-cache.json"

# Module-level cache so we only hit disk once per process.
_remote_cache: dict[str, Any] | None = None
_remote_fetched_at: float = 0.0


# ---------------------------------------------------------------------------
# Remote catalog helpers
# ---------------------------------------------------------------------------


def fetch_remote_catalog(url: str | None = None, *, timeout: int = 5) -> dict[str, Any]:
    """Fetch the remote MCP server catalog, with disk-backed 24-hour caching.

    Uses only stdlib (``urllib.request``) — no extra runtime dependencies.

    On any network or parse failure the function returns an empty dict so that
    the calling code can always fall back to the local registry gracefully.

    Args:
        url: Override the default catalog URL. Defaults to the bogware GitHub
            raw URL.
        timeout: Network request timeout in seconds. Defaults to 5.

    Returns:
        Parsed JSON dict from the catalog, or ``{}`` on failure.
    """
    global _remote_cache, _remote_fetched_at  # noqa: PLW0603  # module-level cache

    catalog_url = url or _DEFAULT_CATALOG_URL

    # Return in-process cache if fresh.
    now = time.monotonic()
    if _remote_cache is not None and (now - _remote_fetched_at) < _CACHE_TTL_SECONDS:
        return _remote_cache

    # Try to load from disk cache if it's fresh enough.
    if _CACHE_PATH.exists():
        try:
            cached = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
            cached_at = cached.get("_fetched_at", 0)
            if (time.time() - cached_at) < _CACHE_TTL_SECONDS:
                payload = cached.get("entries", {})
                if isinstance(payload, dict):
                    _remote_cache = payload
                    _remote_fetched_at = now
                    return payload
        except (OSError, json.JSONDecodeError, ValueError):
            logger.debug("MCP catalog disk cache is invalid; will re-fetch.", exc_info=True)

    # Try to fetch from network.
    try:
        req = urllib.request.Request(  # noqa: S310  # URL is a known safe constant
            catalog_url,
            headers={"User-Agent": "bog-agents-cli/mcp-catalog"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            raw = resp.read().decode("utf-8")
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError(f"Expected dict, got {type(data).__name__}")  # noqa: TRY301
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError, ValueError) as exc:
        logger.debug("Could not fetch MCP remote catalog from %s: %s", catalog_url, exc)
        # Fall back to stale disk cache if available.
        if _CACHE_PATH.exists():
            try:
                cached = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
                payload = cached.get("entries", {})
                if isinstance(payload, dict):
                    _remote_cache = payload
                    _remote_fetched_at = now
                    return payload
            except (OSError, json.JSONDecodeError):
                pass
        return {}

    # Persist to disk cache.
    try:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_PATH.write_text(
            json.dumps({"_fetched_at": time.time(), "entries": data}, indent=2),
            encoding="utf-8",
        )
    except OSError:
        logger.debug("Could not write MCP catalog cache to %s.", _CACHE_PATH, exc_info=True)

    _remote_cache = data
    _remote_fetched_at = now
    return data


def refresh_catalog(*, force: bool = False) -> None:
    """Invalidate the remote catalog cache and trigger a fresh fetch.

    Args:
        force: When ``True``, also delete the on-disk cache file before
            re-fetching so that a stale cache cannot be used as a fallback.
    """
    global _remote_cache, _remote_fetched_at  # noqa: PLW0603
    _remote_cache = None
    _remote_fetched_at = 0.0
    if force and _CACHE_PATH.exists():
        try:
            _CACHE_PATH.unlink()
        except OSError:
            logger.debug("Could not delete MCP catalog cache at %s.", _CACHE_PATH, exc_info=True)
    # Eagerly re-fetch so callers get a warm cache after calling refresh.
    fetch_remote_catalog()


def get_merged_registry() -> dict[str, RegistryEntry]:
    """Return the local registry merged with any valid remote entries.

    Local entries always win: a remote entry whose ``id`` matches a local entry
    is ignored. Remote entries that are missing any of the required fields
    (``id``, ``display_name``, ``command``) are also skipped.

    Returns:
        Merged registry dict keyed by server ``id``.
    """
    merged: dict[str, RegistryEntry] = dict(_REGISTRY)

    remote = fetch_remote_catalog()
    for key, raw in remote.items():
        if not isinstance(raw, dict):
            continue
        entry_id = raw.get("id") or key
        if not isinstance(entry_id, str) or not entry_id:
            continue
        # Local wins.
        if entry_id in merged:
            continue
        # Require minimum fields.
        if not raw.get("display_name") or not raw.get("command"):
            continue
        entry: RegistryEntry = {
            "id": entry_id,
            "display_name": str(raw["display_name"]),
            "command": str(raw["command"]),
        }
        if "description" in raw and isinstance(raw["description"], str):
            entry["description"] = raw["description"]
        if "args" in raw and isinstance(raw["args"], list):
            entry["args"] = [str(a) for a in raw["args"]]
        if "env" in raw and isinstance(raw["env"], dict):
            entry["env"] = {str(k): str(v) for k, v in raw["env"].items()}
        if "tags" in raw and isinstance(raw["tags"], list):
            entry["tags"] = [str(t) for t in raw["tags"]]
        merged[entry_id] = entry

    return merged


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def list_entries() -> list[RegistryEntry]:
    """Return all registry entries (local + remote) sorted by display name.

    Returns:
        Sorted list of `RegistryEntry` dicts.
    """
    return sorted(get_merged_registry().values(), key=lambda e: e.get("display_name", "").lower())


def get_entry(server_id: str) -> RegistryEntry | None:
    """Look up a registry entry by its unique ID.

    Args:
        server_id: The unique slug for the server (e.g. ``'filesystem'``).

    Returns:
        The `RegistryEntry` dict, or ``None`` if not found.
    """
    return get_merged_registry().get(server_id)


def search_entries(query: str) -> list[RegistryEntry]:
    """Search registry entries by display name, description, or tags.

    The search is case-insensitive and matches any substring.

    Args:
        query: Search string.

    Returns:
        Matching `RegistryEntry` dicts sorted by display name.
    """
    q = query.lower()
    results: list[RegistryEntry] = []
    for entry in get_merged_registry().values():
        haystack = " ".join(
            [
                entry.get("id", ""),
                entry.get("display_name", ""),
                entry.get("description", ""),
                " ".join(entry.get("tags", [])),
            ]
        ).lower()
        if q in haystack:
            results.append(entry)
    return sorted(results, key=lambda e: e.get("display_name", "").lower())
