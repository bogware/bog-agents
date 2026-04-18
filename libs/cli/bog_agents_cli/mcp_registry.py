"""Curated MCP server registry for the bog-agents-cli marketplace.

Provides a catalog of well-known MCP servers that can be installed into
``~/.bog-agents/.mcp.json`` with a single ``/mcp install <id>`` command.

Each entry declares:

- How to run the server (command/args for stdio, url for HTTP/SSE)
- Which environment variables are required or optional
- Which /vars keys to suggest (so ``{{vars.JIRA_URL}}`` refs work)
- Category for grouping in search results
- Install notes for any prerequisite setup

Official servers use ``npx -y @modelcontextprotocol/...`` (no local install).
Community servers document their install method in ``install_notes``.

Remote catalog support
----------------------
An optional remote catalog is fetched, disk-cached (24-hour TTL), and merged
at runtime. Network failures fall back to cache; if no cache exists the remote
contribution is silently skipped. Local entries always win over remote ones.
Use `refresh_catalog()` to force a re-fetch.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class RegistryEntry:
    """A single entry in the MCP server registry."""

    id: str
    display_name: str
    description: str
    category: str  # "dev" | "productivity" | "data" | "ai" | "infra" | "search"
    transport: str  # "stdio" | "sse" | "http"
    # stdio fields
    command: str = ""
    args: list[str] = field(default_factory=list)
    # sse/http fields
    url: str = ""
    # env vars that MUST be set for the server to work
    required_env: list[str] = field(default_factory=list)
    # env vars that are optional but useful
    optional_env: list[str] = field(default_factory=list)
    # suggested /vars key names (one-to-one with required_env by convention)
    vars_hints: dict[str, str] = field(default_factory=dict)
    # shown during /mcp install and /mcp info
    install_notes: str = ""
    # "official" | "community" | "vendor"
    source: str = "official"


# ---------------------------------------------------------------------------
# The registry — ordered alphabetically by id
# ---------------------------------------------------------------------------

_REGISTRY: dict[str, RegistryEntry] = {
    # ------------------------------------------------------------------
    # AI / reasoning
    # ------------------------------------------------------------------
    "sequential-thinking": RegistryEntry(
        id="sequential-thinking",
        display_name="Sequential Thinking",
        description="Structured step-by-step reasoning — breaks complex problems into manageable thought chains",
        category="ai",
        transport="stdio",
        command="npx",
        args=["-y", "@modelcontextprotocol/server-sequential-thinking"],
        source="official",
    ),
    # ------------------------------------------------------------------
    # Data / databases
    # ------------------------------------------------------------------
    "postgres": RegistryEntry(
        id="postgres",
        display_name="PostgreSQL",
        description="Read-only Postgres access — query tables, inspect schemas, run SELECT statements",
        category="data",
        transport="stdio",
        command="npx",
        args=[
            "-y",
            "@modelcontextprotocol/server-postgres",
            "{{POSTGRES_CONNECTION_STRING}}",
        ],
        required_env=["POSTGRES_CONNECTION_STRING"],
        vars_hints={
            "POSTGRES_CONNECTION_STRING": "Postgres connection string (postgresql://user:pass@host/db)"
        },
        install_notes="Requires Node.js. The connection string is passed as a CLI argument.",
        source="official",
    ),
    "sqlite": RegistryEntry(
        id="sqlite",
        display_name="SQLite",
        description="Local SQLite database access — read, query, and inspect .db files",
        category="data",
        transport="stdio",
        command="uvx",
        args=["mcp-server-sqlite", "--db-path", "{{DB_PATH}}"],
        required_env=["DB_PATH"],
        vars_hints={"DB_PATH": "Path to the SQLite .db file"},
        install_notes="Requires `uv` (pip install uv). Set DB_PATH to your .db file.",
        source="official",
    ),
    # ------------------------------------------------------------------
    # Developer tools
    # ------------------------------------------------------------------
    "filesystem": RegistryEntry(
        id="filesystem",
        display_name="Filesystem",
        description="Safe read/write access to a local directory subtree",
        category="dev",
        transport="stdio",
        command="npx",
        args=["-y", "@modelcontextprotocol/server-filesystem", "/"],
        install_notes=(
            "The last argument is the root path to expose. Replace '/' with the "
            "directory you want to give the agent access to, e.g. '/home/user/projects'."
        ),
        source="official",
    ),
    "fetch": RegistryEntry(
        id="fetch",
        display_name="Fetch",
        description="Fetch web pages and APIs — returns cleaned Markdown or raw HTML",
        category="dev",
        transport="stdio",
        command="uvx",
        args=["mcp-server-fetch"],
        install_notes="Requires `uv` (pip install uv). No API key needed.",
        source="official",
    ),
    "git": RegistryEntry(
        id="git",
        display_name="Git",
        description="Git repository operations — log, diff, blame, branch management",
        category="dev",
        transport="stdio",
        command="uvx",
        args=["mcp-server-git", "--repository", "."],
        install_notes="Requires `uv`. Change '.' to the path of the git repo to expose.",
        source="official",
    ),
    "github": RegistryEntry(
        id="github",
        display_name="GitHub",
        description="GitHub API — search repos, manage issues, PRs, files, and gists",
        category="dev",
        transport="stdio",
        command="npx",
        args=["-y", "@modelcontextprotocol/server-github"],
        required_env=["GITHUB_PERSONAL_ACCESS_TOKEN"],
        vars_hints={
            "GITHUB_PERSONAL_ACCESS_TOKEN": "GitHub personal access token (repo, read:org scopes)"
        },
        install_notes=(
            "Create a token at github.com/settings/tokens (classic). "
            "Grant 'repo' and 'read:org' scopes."
        ),
        source="official",
    ),
    "gitlab": RegistryEntry(
        id="gitlab",
        display_name="GitLab",
        description="GitLab API — issues, MRs, pipelines, repository browsing",
        category="dev",
        transport="stdio",
        command="npx",
        args=["-y", "@modelcontextprotocol/server-gitlab"],
        required_env=["GITLAB_PERSONAL_ACCESS_TOKEN"],
        optional_env=["GITLAB_API_URL"],
        vars_hints={
            "GITLAB_PERSONAL_ACCESS_TOKEN": "GitLab personal access token",
            "GITLAB_API_URL": "GitLab instance URL (defaults to gitlab.com)",
        },
        source="official",
    ),
    "puppeteer": RegistryEntry(
        id="puppeteer",
        display_name="Puppeteer",
        description="Headless Chromium browser — screenshot, scrape, and interact with web pages",
        category="dev",
        transport="stdio",
        command="npx",
        args=["-y", "@modelcontextprotocol/server-puppeteer"],
        install_notes="Requires Node.js. Chromium is downloaded automatically on first run.",
        source="official",
    ),
    "sentry": RegistryEntry(
        id="sentry",
        display_name="Sentry",
        description="Sentry error tracking — search issues, view stack traces, manage projects",
        category="dev",
        transport="stdio",
        command="uvx",
        args=["mcp-server-sentry", "--auth-token", "{{SENTRY_AUTH_TOKEN}}"],
        required_env=["SENTRY_AUTH_TOKEN"],
        vars_hints={"SENTRY_AUTH_TOKEN": "Sentry auth token (Settings > Auth Tokens)"},
        install_notes="Create a token at sentry.io under Settings → Auth Tokens.",
        source="official",
    ),
    # ------------------------------------------------------------------
    # Infrastructure / cloud
    # ------------------------------------------------------------------
    "azure-devops": RegistryEntry(
        id="azure-devops",
        display_name="Azure DevOps",
        description="Azure DevOps — work items, boards, pipelines, repos, and wikis",
        category="infra",
        transport="stdio",
        command="npx",
        args=["-y", "@tiberriver256/mcp-server-azure-devops"],
        required_env=["AZURE_DEVOPS_ORG_URL", "AZURE_DEVOPS_AUTH_TOKEN"],
        optional_env=["AZURE_DEVOPS_DEFAULT_PROJECT"],
        vars_hints={
            "AZURE_DEVOPS_ORG_URL": "Azure DevOps org URL (https://dev.azure.com/your-org)",
            "AZURE_DEVOPS_AUTH_TOKEN": "Azure DevOps Personal Access Token",
            "AZURE_DEVOPS_DEFAULT_PROJECT": "Default project name (optional)",
        },
        install_notes=(
            "Create a PAT at dev.azure.com → User Settings → Personal Access Tokens. "
            "Grant Work Items (read/write), Code (read), Build (read) scopes."
        ),
        source="community",
    ),
    "terraform": RegistryEntry(
        id="terraform",
        display_name="Terraform / HCP",
        description="HashiCorp Terraform Cloud — workspaces, runs, variables, state, and registry modules",
        category="infra",
        transport="stdio",
        command="npx",
        args=["-y", "@hashicorp/terraform-mcp-server"],
        required_env=["TFC_TOKEN"],
        optional_env=["TFC_HOSTNAME"],
        vars_hints={
            "TFC_TOKEN": "Terraform Cloud API token (app.terraform.io → User Settings → Tokens)",
            "TFC_HOSTNAME": "Terraform Enterprise hostname (leave empty for Terraform Cloud)",
        },
        install_notes=(
            "Create an API token at app.terraform.io → User Settings → Tokens. "
            "Set TFC_HOSTNAME only for Terraform Enterprise deployments."
        ),
        source="vendor",
    ),
    "aws-kb": RegistryEntry(
        id="aws-kb",
        display_name="AWS Knowledge Base (Bedrock)",
        description="Query Amazon Bedrock Knowledge Bases with RAG for your internal docs",
        category="infra",
        transport="stdio",
        command="npx",
        args=["-y", "@aws/aws-bedrock-kb-retrieval-mcp-server"],
        required_env=[
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
            "AWS_REGION",
            "BEDROCK_KB_ID",
        ],
        vars_hints={
            "AWS_ACCESS_KEY_ID": "AWS access key ID",
            "AWS_SECRET_ACCESS_KEY": "AWS secret access key",
            "AWS_REGION": "AWS region (e.g. us-east-1)",
            "BEDROCK_KB_ID": "Bedrock Knowledge Base ID",
        },
        source="vendor",
    ),
    # ------------------------------------------------------------------
    # Productivity / project management
    # ------------------------------------------------------------------
    "jira": RegistryEntry(
        id="jira",
        display_name="Jira (Atlassian)",
        description="Jira + Confluence — search issues, create tickets, update fields, browse spaces",
        category="productivity",
        transport="stdio",
        command="uvx",
        args=["mcp-atlassian"],
        required_env=["JIRA_URL", "JIRA_USERNAME", "JIRA_API_TOKEN"],
        optional_env=["CONFLUENCE_URL", "CONFLUENCE_USERNAME", "CONFLUENCE_API_TOKEN"],
        vars_hints={
            "JIRA_URL": "Jira base URL (https://your-org.atlassian.net)",
            "JIRA_USERNAME": "Jira login email",
            "JIRA_API_TOKEN": "Jira API token (id.atlassian.com → Security → API tokens)",
        },
        install_notes=(
            "Requires `uv` (pip install uv). Uses the `mcp-atlassian` package by sooperset.\n"
            "Create an API token at id.atlassian.com → Security → Create and manage API tokens.\n"
            "For Confluence access set the CONFLUENCE_* vars to the same values."
        ),
        source="community",
    ),
    "linear": RegistryEntry(
        id="linear",
        display_name="Linear",
        description="Linear project management — issues, projects, cycles, teams, and comments",
        category="productivity",
        transport="stdio",
        command="npx",
        args=["-y", "@linear/mcp-server"],
        required_env=["LINEAR_API_KEY"],
        vars_hints={
            "LINEAR_API_KEY": "Linear API key (Settings → API → Personal API keys)"
        },
        install_notes="Create a key at linear.app → Settings → API → Personal API keys.",
        source="vendor",
    ),
    "notion": RegistryEntry(
        id="notion",
        display_name="Notion",
        description="Notion workspace — read/write pages, databases, blocks, and search content",
        category="productivity",
        transport="stdio",
        command="npx",
        args=["-y", "@notionhq/notion-mcp-server"],
        required_env=["NOTION_API_KEY"],
        vars_hints={
            "NOTION_API_KEY": "Notion integration token (notion.so/my-integrations)"
        },
        install_notes=(
            "Create an internal integration at notion.so/my-integrations. "
            "Share the pages/databases you want accessible with the integration."
        ),
        source="vendor",
    ),
    "slack": RegistryEntry(
        id="slack",
        display_name="Slack",
        description="Slack — send messages, search channels, read history, manage threads",
        category="productivity",
        transport="stdio",
        command="npx",
        args=["-y", "@modelcontextprotocol/server-slack"],
        required_env=["SLACK_BOT_TOKEN", "SLACK_TEAM_ID"],
        vars_hints={
            "SLACK_BOT_TOKEN": "Slack bot token starting with xoxb-",
            "SLACK_TEAM_ID": "Slack workspace/team ID (found in workspace URL)",
        },
        install_notes=(
            "Create a Slack app at api.slack.com/apps. Add the Bot Token Scopes: "
            "channels:history, channels:read, chat:write, groups:read, users:read. "
            "Install to workspace and copy the Bot User OAuth Token."
        ),
        source="official",
    ),
    # ------------------------------------------------------------------
    # Search / knowledge
    # ------------------------------------------------------------------
    "brave-search": RegistryEntry(
        id="brave-search",
        display_name="Brave Search",
        description="Web + local search via Brave Search API — privacy-focused, no tracking",
        category="search",
        transport="stdio",
        command="npx",
        args=["-y", "@modelcontextprotocol/server-brave-search"],
        required_env=["BRAVE_API_KEY"],
        vars_hints={"BRAVE_API_KEY": "Brave Search API key (api.search.brave.com)"},
        install_notes="Get a free API key at api.search.brave.com (up to 2000 queries/month free).",
        source="official",
    ),
    "memory": RegistryEntry(
        id="memory",
        display_name="Memory",
        description="Persistent in-session memory store — remember facts across tool calls",
        category="ai",
        transport="stdio",
        command="npx",
        args=["-y", "@modelcontextprotocol/server-memory"],
        install_notes="No configuration required. Memory is stored in a local JSON file.",
        source="official",
    ),
    "google-maps": RegistryEntry(
        id="google-maps",
        display_name="Google Maps",
        description="Geocoding, directions, place search, and route planning via Google Maps API",
        category="search",
        transport="stdio",
        command="npx",
        args=["-y", "@modelcontextprotocol/server-google-maps"],
        required_env=["GOOGLE_MAPS_API_KEY"],
        vars_hints={
            "GOOGLE_MAPS_API_KEY": "Google Maps API key (console.cloud.google.com)"
        },
        install_notes="Enable the Maps JavaScript API + Places API in Google Cloud Console.",
        source="official",
    ),
    "figma": RegistryEntry(
        id="figma",
        display_name="Figma",
        description="Figma design files — inspect components, styles, variables, and dev-mode data",
        category="dev",
        transport="stdio",
        command="npx",
        args=["-y", "figma-developer-mcp", "--figma-api-key={{FIGMA_API_KEY}}"],
        required_env=["FIGMA_API_KEY"],
        vars_hints={
            "FIGMA_API_KEY": "Figma personal access token (Settings → Account → Personal access tokens)"
        },
        install_notes=(
            "Generate a token at figma.com → Settings → Account → Personal access tokens. "
            "This MCP provides the same data as Figma's dev mode panel."
        ),
        source="community",
    ),
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_entry(server_id: str) -> RegistryEntry | None:
    """Return the registry entry for *server_id*, or None if not found.

    Checks both the local registry and any remote catalog entries.

    Args:
        server_id: Kebab-case server identifier.

    Returns:
        Registry entry, or None.
    """
    return get_merged_registry().get(server_id)


def list_entries(*, category: str | None = None) -> list[RegistryEntry]:
    """Return all registry entries (local + remote), optionally filtered by category.

    Args:
        category: When provided, only entries with this category are returned.

    Returns:
        Sorted list of entries.
    """
    entries = list(get_merged_registry().values())
    if category:
        entries = [e for e in entries if e.category == category]
    return sorted(entries, key=lambda e: e.id)


def search_entries(query: str) -> list[RegistryEntry]:
    """Full-text search over id, display_name, description, and category.

    Args:
        query: Search string (case-insensitive, space-separated terms).

    Returns:
        Matching entries sorted by relevance (exact id match first).
    """
    terms = query.lower().split()
    results: list[tuple[int, RegistryEntry]] = []
    for entry in get_merged_registry().values():
        haystack = " ".join(
            [entry.id, entry.display_name, entry.description, entry.category]
        ).lower()
        score = 0
        for term in terms:
            if entry.id == term:
                score += 100
            elif entry.id.startswith(term):
                score += 50
            elif term in entry.id:
                score += 30
            if term in haystack:
                score += 10
        if score > 0:
            results.append((score, entry))
    return [e for _, e in sorted(results, key=lambda x: -x[0])]


def list_categories() -> list[str]:
    """Return all distinct category names in the registry.

    Returns:
        Sorted list of category strings.
    """
    return sorted({e.category for e in get_merged_registry().values()})


def build_server_config(entry: RegistryEntry, env_values: dict[str, str]) -> dict:
    """Build the mcpServers config dict for an entry with resolved env vars.

    Substitutes ``{{VAR_NAME}}`` placeholders in args with values from
    *env_values*, and populates the ``env`` field with any provided
    required/optional env vars.

    Args:
        entry: Registry entry.
        env_values: Mapping of env var name → value (from /vars or user input).

    Returns:
        Server config dict suitable for adding to an .mcp.json file.
    """
    if entry.transport in {"sse", "http"}:
        cfg: dict = {"type": entry.transport, "url": entry.url}
        return cfg

    # Substitute {{VAR}} placeholders in args
    resolved_args = []
    for arg in entry.args:
        resolved = arg
        for var_name, value in env_values.items():
            resolved = resolved.replace(f"{{{{{var_name}}}}}", value)
        resolved_args.append(resolved)

    cfg = {"command": entry.command, "args": resolved_args}

    # Collect env vars that were provided
    env_section: dict[str, str] = {}
    for var in entry.required_env + entry.optional_env:
        # Skip vars that are already inlined into args
        if any(f"{{{{{var}}}}}" in arg for arg in entry.args):
            continue
        if var in env_values:
            env_section[var] = env_values[var]

    if env_section:
        cfg["env"] = env_section

    return cfg


# ---------------------------------------------------------------------------
# Remote catalog (stdlib-only, 24-hour disk cache, offline-safe)
# ---------------------------------------------------------------------------

_DEFAULT_CATALOG_URL = (
    "https://raw.githubusercontent.com/bogware/bog-agents/main/catalog/mcp-servers.json"
)
_CACHE_TTL_SECONDS = 24 * 60 * 60  # 24 hours
_CACHE_PATH = Path.home() / ".bog-agents" / ".mcp-catalog-cache.json"

# Module-level in-process cache (avoids repeated disk reads within one session).
_remote_cache: dict[str, Any] | None = None
_remote_fetched_at: float = 0.0


def fetch_remote_catalog(url: str | None = None, *, timeout: int = 5) -> dict[str, Any]:
    """Fetch the remote MCP server catalog with 24-hour disk-backed caching.

    Uses only stdlib (``urllib.request``). On any failure the function returns
    ``{}`` so callers always have the local registry as a safe fallback.

    Args:
        url: Override the default catalog URL.
        timeout: Network request timeout in seconds.

    Returns:
        Parsed JSON dict from the catalog, or ``{}`` on failure.
    """
    global _remote_cache, _remote_fetched_at  # noqa: PLW0603

    catalog_url = url or _DEFAULT_CATALOG_URL
    now = time.monotonic()

    if _remote_cache is not None and (now - _remote_fetched_at) < _CACHE_TTL_SECONDS:
        return _remote_cache

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
            logger.debug("MCP catalog disk cache invalid; re-fetching.", exc_info=True)

    try:
        req = urllib.request.Request(
            catalog_url,
            headers={"User-Agent": "bog-agents-cli/mcp-catalog"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            data = json.loads(resp.read().decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"Expected dict, got {type(data).__name__}")  # noqa: TRY301
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError, ValueError) as exc:
        logger.debug("Could not fetch MCP remote catalog: %s", exc)
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
    """Invalidate the remote catalog cache and re-fetch.

    Args:
        force: When True, also delete the on-disk cache before re-fetching.
    """
    global _remote_cache, _remote_fetched_at  # noqa: PLW0603
    _remote_cache = None
    _remote_fetched_at = 0.0
    if force and _CACHE_PATH.exists():
        try:
            _CACHE_PATH.unlink()
        except OSError:
            logger.debug("Could not delete MCP catalog cache at %s.", _CACHE_PATH, exc_info=True)
    fetch_remote_catalog()


def get_merged_registry() -> dict[str, RegistryEntry]:
    """Return the local registry merged with any valid remote entries.

    Local entries always win over remote ones. Remote entries missing required
    fields (``id``, ``display_name``, ``command``) are skipped.

    Returns:
        Merged registry dict keyed by server ``id``.
    """
    merged: dict[str, RegistryEntry] = dict(_REGISTRY)

    for key, raw in fetch_remote_catalog().items():
        if not isinstance(raw, dict):
            continue
        entry_id = str(raw.get("id") or key)
        if not entry_id or entry_id in merged:
            continue
        if not raw.get("display_name") or not raw.get("command"):
            continue
        merged[entry_id] = RegistryEntry(
            id=entry_id,
            display_name=str(raw["display_name"]),
            description=str(raw.get("description", "")),
            category=str(raw.get("category", "community")),
            transport=str(raw.get("transport", "stdio")),
            command=str(raw["command"]),
            args=[str(a) for a in raw.get("args", [])],
            url=str(raw.get("url", "")),
            required_env=[str(v) for v in raw.get("required_env", [])],
            optional_env=[str(v) for v in raw.get("optional_env", [])],
            vars_hints={str(k): str(v) for k, v in raw.get("vars_hints", {}).items()},
            install_notes=str(raw.get("install_notes", "")),
            source="remote",
        )

    return merged
