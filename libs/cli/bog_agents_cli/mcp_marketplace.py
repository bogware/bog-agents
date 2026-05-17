"""MCP marketplace — ``/mcp install <name>`` and friends (Gap 5).

A small, curated marketplace of known-good MCP servers. The user types
``/mcp install jira`` and we:

1. Look up the server by name in a built-in catalog.
2. Prompt for any required environment variables (API keys, URLs).
3. Write the server entry into the user-level MCP config via
   :mod:`bog_agents_cli.mcp_config_manager`.
4. Tell the user to restart the session so the new server is mounted.

Why a built-in catalog rather than a live registry call:

* Quality signal: 17k+ MCP servers exist in the wild as of mid-2026,
  but only ~13% pass a basic "would I trust this in production" bar.
  A curated catalog filters that for us.
* Offline-friendly: ``/mcp install`` works without an internet
  fetch for the registry index.
* Auditability: the catalog ships with the CLI so users can read what
  each entry installs *before* running it.

Extending the catalog is intentionally a one-line code change here —
no plugin loader, no eval, no signed-bundle dance yet. When the
marketplace grows past a dozen entries we'll split it into a YAML
file under ``bog_agents_cli/data/`` and load it lazily.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from bog_agents_cli.mcp_config_manager import (
    add_server,
    get_server,
    remove_server,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class MarketplaceEntry:
    """One curated MCP server in the marketplace.

    Attributes:
        name: Slug the user types (``/mcp install <name>``). Must be
            unique within the catalog.
        title: Human-readable title for the listing.
        summary: One-line description shown in ``/mcp marketplace``.
        category: Loose grouping (``git``, ``cloud``, ``data``,
            ``docs``, etc.) so listings can be filtered.
        command: Executable used to start the server (``uvx``, ``npx``,
            ``python``, etc.).
        args: Arguments for *command*.
        env_required: Names of env vars the user MUST supply. The
            installer prompts for each.
        env_optional: Names of env vars the user MAY supply. Skipped
            when blank.
        homepage: Where to read more / report bugs.
    """

    name: str
    title: str
    summary: str
    category: str
    command: str
    args: tuple[str, ...]
    env_required: tuple[str, ...] = ()
    env_optional: tuple[str, ...] = ()
    homepage: str = ""
    tags: tuple[str, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# The curated catalog
# ---------------------------------------------------------------------------


CATALOG: tuple[MarketplaceEntry, ...] = (
    MarketplaceEntry(
        name="filesystem",
        title="Filesystem (read-only)",
        summary="Read files from an allow-listed directory. Useful for "
        "scoping the agent to a sub-tree without exposing your home dir.",
        category="local",
        command="npx",
        args=("-y", "@modelcontextprotocol/server-filesystem"),
        env_required=("FILESYSTEM_ROOT",),
        homepage="https://github.com/modelcontextprotocol/servers",
        tags=("fs", "read", "local"),
    ),
    MarketplaceEntry(
        name="github",
        title="GitHub",
        summary="Search code, read PRs, create issues, comment on PRs.",
        category="git",
        command="npx",
        args=("-y", "@modelcontextprotocol/server-github"),
        env_required=("GITHUB_PERSONAL_ACCESS_TOKEN",),
        homepage="https://github.com/modelcontextprotocol/servers",
        tags=("git", "vcs", "code-review"),
    ),
    MarketplaceEntry(
        name="git",
        title="Git (local)",
        summary="Local git operations — log, blame, diff, show. Read-only.",
        category="git",
        command="uvx",
        args=("mcp-server-git",),
        env_required=(),
        env_optional=("GIT_REPO_PATH",),
        homepage="https://github.com/modelcontextprotocol/servers",
        tags=("git", "vcs", "local"),
    ),
    MarketplaceEntry(
        name="jira",
        title="Jira (Atlassian)",
        summary="Read + comment on tickets, search issues, update status.",
        category="ticketing",
        command="uvx",
        args=("mcp-atlassian",),
        env_required=("JIRA_URL", "JIRA_USERNAME", "JIRA_API_TOKEN"),
        homepage="https://github.com/sooperset/mcp-atlassian",
        tags=("jira", "atlassian", "ticketing"),
    ),
    MarketplaceEntry(
        name="confluence",
        title="Confluence",
        summary="Read + search pages, post comments.",
        category="docs",
        command="uvx",
        args=("mcp-atlassian",),
        env_required=("CONFLUENCE_URL", "CONFLUENCE_USERNAME", "CONFLUENCE_API_TOKEN"),
        homepage="https://github.com/sooperset/mcp-atlassian",
        tags=("confluence", "atlassian", "docs", "wiki"),
    ),
    MarketplaceEntry(
        name="slack",
        title="Slack",
        summary="Post messages, read channels, search history.",
        category="chat",
        command="npx",
        args=("-y", "@modelcontextprotocol/server-slack"),
        env_required=("SLACK_BOT_TOKEN", "SLACK_TEAM_ID"),
        homepage="https://github.com/modelcontextprotocol/servers",
        tags=("slack", "chat", "team"),
    ),
    MarketplaceEntry(
        name="postgres",
        title="PostgreSQL (read-only)",
        summary="Run read-only queries against a Postgres database.",
        category="data",
        command="npx",
        args=("-y", "@modelcontextprotocol/server-postgres"),
        env_required=("POSTGRES_CONNECTION_STRING",),
        homepage="https://github.com/modelcontextprotocol/servers",
        tags=("postgres", "database", "sql"),
    ),
    MarketplaceEntry(
        name="sqlite",
        title="SQLite",
        summary="Query a local SQLite database.",
        category="data",
        command="uvx",
        args=("mcp-server-sqlite",),
        env_required=("SQLITE_DB_PATH",),
        homepage="https://github.com/modelcontextprotocol/servers",
        tags=("sqlite", "database", "sql", "local"),
    ),
    MarketplaceEntry(
        name="fetch",
        title="Fetch (HTTP)",
        summary="Fetch arbitrary URLs into the agent context. "
        "Use sparingly — the built-in /web command is usually a better fit.",
        category="web",
        command="uvx",
        args=("mcp-server-fetch",),
        env_required=(),
        homepage="https://github.com/modelcontextprotocol/servers",
        tags=("http", "web", "fetch"),
    ),
    MarketplaceEntry(
        name="puppeteer",
        title="Puppeteer (browser)",
        summary="Browser automation — navigate, click, screenshot, "
        "evaluate JS. Requires Node + Chromium.",
        category="web",
        command="npx",
        args=("-y", "@modelcontextprotocol/server-puppeteer"),
        env_required=(),
        homepage="https://github.com/modelcontextprotocol/servers",
        tags=("browser", "automation", "scraping"),
    ),
)


# ---------------------------------------------------------------------------
# Look-up + listing
# ---------------------------------------------------------------------------


def find_entry(name: str) -> MarketplaceEntry | None:
    """Return the catalog entry by exact name match, or ``None``."""
    needle = name.strip().lower()
    for entry in CATALOG:
        if entry.name == needle:
            return entry
    return None


def search_entries(query: str) -> list[MarketplaceEntry]:
    """Return entries matching *query* in name / title / tags / category."""
    q = query.strip().lower()
    if not q:
        return list(CATALOG)
    out: list[MarketplaceEntry] = []
    for entry in CATALOG:
        if (
            q in entry.name
            or q in entry.title.lower()
            or q in entry.summary.lower()
            or q in entry.category.lower()
            or any(q in t for t in entry.tags)
        ):
            out.append(entry)
    return out


def render_marketplace_listing(entries: list[MarketplaceEntry] | None = None) -> str:
    """Format a flat listing of catalog entries grouped by category."""
    selected = entries if entries is not None else list(CATALOG)
    if not selected:
        return "No marketplace entries match that query."
    by_cat: dict[str, list[MarketplaceEntry]] = {}
    for entry in selected:
        by_cat.setdefault(entry.category, []).append(entry)
    lines = [f"{len(selected)} marketplace entries:", ""]
    for cat in sorted(by_cat):
        lines.append(f"[{cat}]")
        for entry in by_cat[cat]:
            lines.append(f"  {entry.name:<14} — {entry.title}")
            lines.append(f"  {' ' * 14}   {entry.summary}")
            if entry.env_required:
                lines.append(
                    f"  {' ' * 14}   requires: {', '.join(entry.env_required)}"
                )
        lines.append("")
    lines.append("Install:  /mcp install <name>")
    lines.append("Details:  /mcp show <name>")
    return "\n".join(lines).rstrip()


def render_entry_detail(entry: MarketplaceEntry) -> str:
    """Verbose detail view used by ``/mcp show <name>``."""
    cmd_preview = entry.command + " " + " ".join(entry.args)
    lines = [
        f"== {entry.title} ({entry.name}) ==",
        f"Category: {entry.category}",
        f"Tags:     {', '.join(entry.tags) or '(none)'}",
        f"Command:  {cmd_preview}",
        f"Homepage: {entry.homepage or '(n/a)'}",
        "",
        entry.summary,
    ]
    if entry.env_required:
        lines.extend(
            [
                "",
                "Required environment variables:",
                *[f"  - {name}" for name in entry.env_required],
            ]
        )
    if entry.env_optional:
        lines.extend(
            [
                "",
                "Optional environment variables:",
                *[f"  - {name}" for name in entry.env_optional],
            ]
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Installation
# ---------------------------------------------------------------------------


class CredentialPrompt:
    """Protocol-shaped callable for prompting the user for env values.

    Kept as a small class rather than ``typing.Protocol`` so tests can
    pass either a plain function or a stateful prompter (e.g. a TUI
    modal). Calling convention: ``prompt(var_name, required) -> str``.
    Returning an empty string means "skip" (only valid for optional
    vars; the installer enforces required ones).
    """

    def __call__(self, env_var: str, *, required: bool) -> str:  # pragma: no cover — protocol-only
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class InstallResult:
    """Outcome of one ``/mcp install`` call."""

    entry: MarketplaceEntry
    server_name: str
    """The name the server was registered under in mcp.json."""
    was_overwritten: bool
    missing_required: tuple[str, ...] = ()
    """When non-empty, the install was aborted because the user
    declined to supply the listed required env vars."""


def install(
    name: str,
    *,
    prompt: CredentialPrompt | None = None,
    install_as: str | None = None,
    overwrite: bool = False,
    env_overrides: dict[str, str] | None = None,
) -> InstallResult:
    """Install the catalog entry *name* into the user MCP config.

    Args:
        name: Catalog slug.
        prompt: Callable invoked once per required/optional env var
            to ask the user. When ``None`` and required env vars
            exist, the install aborts and returns
            ``missing_required`` populated. (Tests + scripts pass a
            stub; the TUI handler passes a Textual prompter.)
        install_as: Optional override for the registered server name
            (useful when the user wants two configurations of the
            same catalog entry — e.g. ``jira-prod`` and ``jira-staging``).
        overwrite: Replace an existing entry with the same registered
            name. Default False: raises ``ValueError`` to force the
            user to confirm.
        env_overrides: Pre-supplied env values; the prompter is only
            called for vars not present in this dict. Lets the slash
            command accept ``KEY=value`` pairs inline.

    Raises:
        ValueError: When *name* isn't in the catalog, or when the
            registered server name already exists and ``overwrite``
            is False.
    """
    entry = find_entry(name)
    if entry is None:
        msg = f"No marketplace entry named {name!r}. Try /mcp marketplace."
        raise ValueError(msg)

    server_name = (install_as or entry.name).strip()
    if not server_name:
        msg = "Server name cannot be empty."
        raise ValueError(msg)
    existing = get_server(server_name)
    if existing is not None and not overwrite:
        msg = (
            f"Server {server_name!r} is already registered. "
            "Pass overwrite=True or use a different --as name."
        )
        raise ValueError(msg)

    overrides = dict(env_overrides or {})
    env: dict[str, str] = {}
    missing_required: list[str] = []
    for var in entry.env_required:
        if overrides.get(var):
            env[var] = overrides[var]
            continue
        if prompt is None:
            missing_required.append(var)
            continue
        value = (prompt(var, required=True) or "").strip()
        if not value:
            missing_required.append(var)
            continue
        env[var] = value
    if missing_required:
        return InstallResult(
            entry=entry,
            server_name=server_name,
            was_overwritten=False,
            missing_required=tuple(missing_required),
        )
    for var in entry.env_optional:
        if var in overrides:
            value = overrides[var]
        elif prompt is None:
            value = ""
        else:
            value = (prompt(var, required=False) or "").strip()
        if value:
            env[var] = value

    server_config: dict[str, Any] = {
        "command": entry.command,
        "args": list(entry.args),
    }
    if env:
        server_config["env"] = env

    ok = add_server(server_name, server_config, overwrite=overwrite)
    if not ok:
        msg = "Failed to write MCP config — see logs for the underlying error."
        raise RuntimeError(msg)
    return InstallResult(
        entry=entry,
        server_name=server_name,
        was_overwritten=existing is not None,
    )


def uninstall(server_name: str) -> bool:
    """Remove a server from the user MCP config.

    Returns:
        True when an entry was removed, False when none was found.
    """
    return remove_server(server_name)


def render_install_outcome(result: InstallResult) -> str:
    """User-facing message returned by ``/mcp install``."""
    if result.missing_required:
        return (
            f"Aborted install of {result.entry.title}: missing required "
            f"environment variables: {', '.join(result.missing_required)}. "
            "Run `/mcp install <name>` again with the values inline as "
            "KEY=value pairs, or set them in your shell first."
        )
    state = "Reinstalled" if result.was_overwritten else "Installed"
    return (
        f"{state} {result.entry.title!r} as MCP server "
        f"`{result.server_name}`.\n"
        f"Command: {result.entry.command} {' '.join(result.entry.args)}\n"
        "Restart this session (or run `/mcp reload`) to mount the new server."
    )


__all__ = [
    "CATALOG",
    "CredentialPrompt",
    "InstallResult",
    "MarketplaceEntry",
    "find_entry",
    "install",
    "render_entry_detail",
    "render_install_outcome",
    "render_marketplace_listing",
    "search_entries",
    "uninstall",
]
