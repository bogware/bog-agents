"""MCP config file manager — read/write ~/.bog-agents/.mcp.json.

Manages the user-level MCP server configuration. Servers added here apply to
all sessions (user-global scope). Project-level configs live at
``<project-root>/.mcp.json`` and are managed separately.

Format is Claude Desktop / Claude Code compatible::

    {
        "mcpServers": {
            "jira": {
                "command": "uvx",
                "args": ["mcp-atlassian"],
                "env": {
                    "JIRA_URL": "https://your-org.atlassian.net",
                    "JIRA_USERNAME": "you@example.com",
                    "JIRA_API_TOKEN": "your-token",
                },
            }
        }
    }
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from bog_agents_cli._env_vars import bog_agents_home

logger = logging.getLogger(__name__)

# Honors BOG_AGENTS_HOME (CT-3); resolved once at import time.
_USER_MCP_CONFIG = bog_agents_home() / ".mcp.json"


# ---------------------------------------------------------------------------
# Low-level I/O
# ---------------------------------------------------------------------------


def get_user_mcp_config_path() -> Path:
    """Return the path to the user-level MCP config file.

    Returns:
        Absolute path to ``~/.bog-agents/.mcp.json``.
    """
    return _USER_MCP_CONFIG


def load_user_mcp_config() -> dict[str, Any]:
    """Load the user-level MCP config, returning an empty structure on error.

    Returns:
        Parsed JSON dict with ``mcpServers`` key, or ``{"mcpServers": {}}``
        when the file is missing or invalid.
    """
    if not _USER_MCP_CONFIG.exists():
        return {"mcpServers": {}}
    try:
        with _USER_MCP_CONFIG.open(encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            logger.warning(
                "MCP config %s is not a JSON object; resetting", _USER_MCP_CONFIG
            )
            return {"mcpServers": {}}
        data.setdefault("mcpServers", {})
        return data
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Could not read MCP config %s: %s", _USER_MCP_CONFIG, exc)
        return {"mcpServers": {}}


def save_user_mcp_config(data: dict[str, Any]) -> bool:
    """Atomically write *data* to the user-level MCP config.

    Args:
        data: Full config dict (must contain ``mcpServers`` key).

    Returns:
        True on success, False on I/O error.
    """
    try:
        _USER_MCP_CONFIG.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=_USER_MCP_CONFIG.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
                fh.write("\n")
            # The config can embed resolved secret env values (API tokens
            # pulled from the vault), so lock it down owner-only BEFORE the
            # rename — never leave it briefly world-readable. (REVIEW.md v2
            # P1-22.) Cross-platform per CLAUDE.md P0-E (chmod 0600 / icacls).
            from bog_agents_cli.vars_store import _secure_owner_only

            _secure_owner_only(Path(tmp))
            Path(tmp).replace(_USER_MCP_CONFIG)
            _secure_owner_only(_USER_MCP_CONFIG)
        except BaseException:
            import contextlib

            with contextlib.suppress(OSError):
                Path(tmp).unlink()
            raise
    except OSError:
        logger.exception("Failed to save MCP config")
        return False
    logger.debug("Saved MCP config to %s", _USER_MCP_CONFIG)
    return True


# ---------------------------------------------------------------------------
# Server management
# ---------------------------------------------------------------------------


def list_servers() -> dict[str, dict[str, Any]]:
    """Return all servers in the user-level config.

    Returns:
        Mapping of server name → server config dict.
    """
    return dict(load_user_mcp_config().get("mcpServers", {}))


def get_server(name: str) -> dict[str, Any] | None:
    """Return config for a specific server, or None if not found.

    Args:
        name: Server name.

    Returns:
        Server config dict, or None.
    """
    return load_user_mcp_config().get("mcpServers", {}).get(name)


def add_server(
    name: str, server_config: dict[str, Any], *, overwrite: bool = False
) -> bool:
    """Add or update a server entry in the user-level config.

    Args:
        name: Server name (key in ``mcpServers``).
        server_config: Server configuration dict.
        overwrite: When False (default), returns False if a server with
            *name* already exists.

    Returns:
        True if the server was added/updated, False if *name* already exists
        and *overwrite* is False.
    """
    data = load_user_mcp_config()
    servers = data.setdefault("mcpServers", {})
    if name in servers and not overwrite:
        logger.debug("Server %r already exists; use overwrite=True to replace", name)
        return False
    servers[name] = server_config
    return save_user_mcp_config(data)


def remove_server(name: str) -> bool:
    """Remove a server from the user-level config.

    Args:
        name: Server name.

    Returns:
        True if removed, False if not found.
    """
    data = load_user_mcp_config()
    servers = data.get("mcpServers", {})
    if name not in servers:
        return False
    del servers[name]
    return save_user_mcp_config(data)


def server_exists(name: str) -> bool:
    """Check whether *name* is already in the user-level config.

    Args:
        name: Server name.

    Returns:
        True if present.
    """
    return name in load_user_mcp_config().get("mcpServers", {})


# ---------------------------------------------------------------------------
# Env-var resolution for install
# ---------------------------------------------------------------------------


def resolve_env_values(
    required_env: list[str],
    optional_env: list[str],
    *,
    from_vars_store: bool = True,
) -> dict[str, str]:
    """Build an env-var map from the vars store and process environment.

    Checks (in order): ``/vars`` store → ``os.environ``.

    Args:
        required_env: Env var names that must be set.
        optional_env: Env var names that are nice to have.
        from_vars_store: When True, check the vars store first.

    Returns:
        Mapping of var name → value for any var that could be resolved.
    """
    values: dict[str, str] = {}
    all_vars = required_env + optional_env

    if from_vars_store:
        try:
            from bog_agents_cli.vars_store import get_var

            for var in all_vars:
                val = get_var(var)
                if val is not None:
                    values[var] = val
        except Exception:
            logger.debug(
                "vars_store lookup failed; falling back to environ", exc_info=True
            )

    for var in all_vars:
        if var not in values and var in os.environ:
            values[var] = os.environ[var]

    return values


def missing_required(
    entry_required_env: list[str],
    resolved: dict[str, str],
) -> list[str]:
    """Return required env vars not present in *resolved*.

    Args:
        entry_required_env: List of required var names from the registry entry.
        resolved: Already-resolved env vars.

    Returns:
        List of var names still needed.
    """
    return [v for v in entry_required_env if v not in resolved]
