"""MCP (Model Context Protocol) tools loader for bog-agents CLI.

This module provides async functions to load and manage MCP servers using
`langchain-mcp-adapters`, supporting Claude Desktop style JSON configs.
It also supports automatic discovery of `.mcp.json` files from user-level
and project-level locations.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

_MCP_STARTUP_TIMEOUT_ENV = "BOG_AGENTS_MCP_STARTUP_TIMEOUT"
_DEFAULT_MCP_STARTUP_TIMEOUT_SECONDS = 15.0
"""How long ``load_mcp_tools`` may spend on one server before giving up.

A stale or misbehaving ``npx -y …`` or OAuth-pending SSE server used to
hang first-paint indefinitely. We now wrap each per-server call in
``asyncio.wait_for`` and skip the offender on timeout. Override via the
``BOG_AGENTS_MCP_STARTUP_TIMEOUT`` env var. Fixes P0-D.
"""


def _mcp_startup_timeout_seconds() -> float:
    """Return the configured per-server startup timeout in seconds."""
    raw = os.environ.get(_MCP_STARTUP_TIMEOUT_ENV)
    if not raw:
        return _DEFAULT_MCP_STARTUP_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "Invalid %s=%r; using default %.1fs",
            _MCP_STARTUP_TIMEOUT_ENV,
            raw,
            _DEFAULT_MCP_STARTUP_TIMEOUT_SECONDS,
        )
        return _DEFAULT_MCP_STARTUP_TIMEOUT_SECONDS
    if value <= 0:
        return _DEFAULT_MCP_STARTUP_TIMEOUT_SECONDS
    return value


if TYPE_CHECKING:
    from langchain_core.tools import BaseTool
    from langchain_mcp_adapters.client import Connection, MultiServerMCPClient

    from bog_agents_cli.project_utils import ProjectContext

logger = logging.getLogger(__name__)


@dataclass
class MCPToolInfo:
    """Metadata for a single MCP tool."""

    name: str
    description: str


@dataclass
class MCPServerInfo:
    """Metadata for a connected MCP server and its tools.

    Attributes:
        name: Configured server name.
        transport: ``"stdio"`` | ``"sse"`` | ``"http"``.
        tools: Tools discovered from the server.
        error: Human-readable failure reason. Empty for healthy servers.
            Populated when the server times out or raises during startup
            so the welcome banner / ``/doctor`` can surface it without
            blocking other servers from loading.
    """

    name: str
    transport: str
    tools: list[MCPToolInfo] = field(default_factory=list)
    error: str = ""


_SUPPORTED_REMOTE_TYPES = {"sse", "http"}
"""Supported transport types for remote MCP servers (SSE and HTTP)."""


def _resolve_server_type(server_config: dict[str, Any]) -> str:
    """Determine the transport type for a server config.

    Supports both `type` and `transport` field names, defaulting to `stdio`.

    Args:
        server_config: Server configuration dictionary.

    Returns:
        Transport type string (`stdio`, `sse`, or `http`).
    """
    t = server_config.get("type")
    if t is not None:
        return t
    return server_config.get("transport", "stdio")


def _validate_server_config(server_name: str, server_config: dict[str, Any]) -> None:
    """Validate a single server configuration.

    Args:
        server_name: Name of the server.
        server_config: Server configuration dictionary.

    Raises:
        TypeError: If config fields have wrong types.
        ValueError: If required fields are missing or server type is unsupported.
    """
    if not isinstance(server_config, dict):
        error_msg = f"Server '{server_name}' config must be a dictionary"
        raise TypeError(error_msg)

    server_type = _resolve_server_type(server_config)

    if server_type in _SUPPORTED_REMOTE_TYPES:
        # SSE/HTTP server validation - requires url field
        if "url" not in server_config:
            error_msg = (
                f"Server '{server_name}' with type '{server_type}'"
                " missing required 'url' field"
            )
            raise ValueError(error_msg)

        # headers is optional but must be correct type if present
        headers = server_config.get("headers")
        if headers is not None and not isinstance(headers, dict):
            error_msg = f"Server '{server_name}' 'headers' must be a dictionary"
            raise TypeError(error_msg)
    elif server_type == "stdio":
        # stdio server validation
        if "command" not in server_config:
            error_msg = f"Server '{server_name}' missing required 'command' field"
            raise ValueError(error_msg)

        # args and env are optional but must be correct type if present
        if "args" in server_config and not isinstance(server_config["args"], list):
            error_msg = f"Server '{server_name}' 'args' must be a list"
            raise TypeError(error_msg)

        if "env" in server_config and not isinstance(server_config["env"], dict):
            error_msg = f"Server '{server_name}' 'env' must be a dictionary"
            raise TypeError(error_msg)
    else:
        error_msg = (
            f"Server '{server_name}' has unsupported transport type '{server_type}'. "
            "Supported types: stdio, sse, http"
        )
        raise ValueError(error_msg)


def load_mcp_config(config_path: str) -> dict[str, Any]:
    """Load and validate MCP configuration from JSON file.

    Supports multiple server types:

    - stdio: Process-based servers with `command`, `args`, `env` fields (default)
    - sse: Server-Sent Events servers with `type: "sse"`, `url`, and optional `headers`
    - http: HTTP-based servers with `type: "http"`, `url`, and optional `headers`

    Args:
        config_path: Path to MCP JSON configuration file (Claude Desktop format).

    Returns:
        Parsed configuration dictionary.

    Raises:
        FileNotFoundError: If config file doesn't exist.
        json.JSONDecodeError: If config file contains invalid JSON.
        TypeError: If config fields have wrong types.
        ValueError: If config is missing required fields.
    """
    path = Path(config_path)

    if not path.exists():
        error_msg = f"MCP config file not found: {config_path}"
        raise FileNotFoundError(error_msg)

    try:
        with path.open(encoding="utf-8") as f:
            config = json.load(f)
    except json.JSONDecodeError as e:
        error_msg = f"Invalid JSON in MCP config file: {e.msg}"
        raise json.JSONDecodeError(error_msg, e.doc, e.pos) from e

    # Validate required fields
    if "mcpServers" not in config:
        error_msg = (
            "MCP config must contain 'mcpServers' field. "
            'Expected format: {"mcpServers": {"server-name": {...}}}'
        )
        raise ValueError(error_msg)

    if not isinstance(config["mcpServers"], dict):
        error_msg = "'mcpServers' field must be a dictionary"
        raise TypeError(error_msg)

    if not config["mcpServers"]:
        error_msg = "'mcpServers' field is empty - no servers configured"
        raise ValueError(error_msg)

    # Validate each server config
    for server_name, server_config in config["mcpServers"].items():
        _validate_server_config(server_name, server_config)

    return config


def _resolve_project_config_base(project_context: ProjectContext | None) -> Path:
    """Resolve the base directory for project-level MCP configuration lookup.

    Args:
        project_context: Explicit project path context, if available.

    Returns:
        Project root when one exists, otherwise the user working directory.
    """
    if project_context is not None:
        return project_context.project_root or project_context.user_cwd

    from bog_agents_cli.project_utils import find_project_root

    return find_project_root() or Path.cwd()


def discover_mcp_configs(
    *, project_context: ProjectContext | None = None
) -> list[Path]:
    """Find MCP config files from standard locations.

    Checks three paths in precedence order (lowest to highest):

    1. `~/.bog-agents/.mcp.json` (user-level global)
    2. `<project-root>/.bog-agents/.mcp.json` (project subdir)
    3. `<project-root>/.mcp.json` (project root, Claude Code compat)

    Project root is determined from `project_context` when provided, otherwise
    by `find_project_root()`, falling back to CWD.

    Returns:
        List of existing config file paths, ordered lowest-to-highest precedence.
    """
    from bog_agents_cli._env_vars import bog_agents_home

    user_dir = bog_agents_home()
    project_root = _resolve_project_config_base(project_context)

    candidates = [
        user_dir / ".mcp.json",
        project_root / ".bog-agents" / ".mcp.json",
        project_root / ".mcp.json",
    ]

    found: list[Path] = []
    for path in candidates:
        try:
            if path.is_file():
                found.append(path)
        except OSError:
            logger.warning("Could not check MCP config %s", path, exc_info=True)
    return found


def classify_discovered_configs(
    config_paths: list[Path],
) -> tuple[list[Path], list[Path]]:
    """Split discovered config paths into user-level and project-level.

    User-level configs live under `~/.bog-agents/`. Everything else is
    considered project-level.

    Args:
        config_paths: Paths returned by `discover_mcp_configs`.

    Returns:
        Tuple of `(user_configs, project_configs)`.
    """
    from bog_agents_cli._env_vars import bog_agents_home

    user_dir = bog_agents_home()
    user: list[Path] = []
    project: list[Path] = []
    for path in config_paths:
        try:
            if path.resolve().is_relative_to(user_dir.resolve()):
                user.append(path)
            else:
                project.append(path)
        except (OSError, ValueError):
            project.append(path)
    return user, project


def extract_stdio_server_commands(
    config: dict[str, Any],
) -> list[tuple[str, str, list[str]]]:
    """Extract stdio server entries from a parsed MCP config.

    Args:
        config: Parsed MCP config dict with `mcpServers` key.

    Returns:
        List of `(server_name, command, args)` for each stdio server.
    """
    results: list[tuple[str, str, list[str]]] = []
    servers = config.get("mcpServers", {})
    if not isinstance(servers, dict):
        return results
    for name, srv in servers.items():
        if not isinstance(srv, dict):
            continue
        if _resolve_server_type(srv) == "stdio":
            results.append((name, srv.get("command", ""), srv.get("args", [])))
    return results


def _filter_project_stdio_servers(config: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of *config* with stdio servers removed.

    Remote (SSE/HTTP) servers are kept because they don't execute local code.

    Args:
        config: Parsed MCP config dict.

    Returns:
        Filtered config dict.
    """
    servers = config.get("mcpServers", {})
    if not isinstance(servers, dict):
        return config
    filtered = {
        name: srv
        for name, srv in servers.items()
        if isinstance(srv, dict) and _resolve_server_type(srv) != "stdio"
    }
    return {"mcpServers": filtered}


def merge_mcp_configs(configs: list[dict[str, Any]]) -> dict[str, Any]:
    """Merge multiple MCP config dicts by server name.

    Later entries override earlier ones for the same server name
    (simple `dict.update` on `mcpServers`).

    Args:
        configs: Ordered list of parsed config dicts (each with `mcpServers` key).

    Returns:
        Merged config with combined `mcpServers`.
    """
    merged: dict[str, Any] = {}
    for cfg in configs:
        servers = cfg.get("mcpServers")
        if isinstance(servers, dict):
            merged.update(servers)
    return {"mcpServers": merged}


def load_mcp_config_lenient(config_path: Path) -> dict[str, Any] | None:
    """Load an MCP config file, returning None on any error.

    Wraps `load_mcp_config` with lenient error handling suitable for
    auto-discovery. Missing files are skipped silently; parse and validation
    errors are logged as warnings.

    Args:
        config_path: Path to the MCP config file.

    Returns:
        Parsed config dict, or None if the file is missing or invalid.
    """
    try:
        return load_mcp_config(str(config_path))
    except FileNotFoundError:
        return None
    except OSError as e:
        logger.warning("Skipping unreadable MCP config %s: %s", config_path, e)
        return None
    except (json.JSONDecodeError, ValueError, TypeError) as e:
        logger.warning("Skipping invalid MCP config %s: %s", config_path, e)
        return None


class MCPSessionManager:
    """Manages persistent MCP sessions for stateful stdio servers.

    This manager creates and maintains persistent sessions for stdio MCP
    servers, preventing server restarts on every tool call. Sessions are kept
    alive until explicitly cleaned up.
    """

    def __init__(self) -> None:
        """Initialize the session manager."""
        self.client: MultiServerMCPClient | None = None
        self.exit_stack = AsyncExitStack()

    async def cleanup(self) -> None:
        """Tear down all managed sessions and close connections.

        Best-effort: swallows errors raised during teardown so the
        original failure (if any) reaches the caller intact. Also
        resets ``self.client`` to ``None`` so a partially-initialized
        client whose anyio task group is in a closed state cannot be
        used by callers that hold a stale reference to this manager.
        Without that reset, the agent's tool registry could probe a
        broken client and surface a confusing
        ``ClosedResourceError: An internal error occurred`` mid-stream
        instead of the actual MCP failure.
        """
        try:
            await self.exit_stack.aclose()
        except Exception:  # cleanup must never re-raise
            logger.warning(
                "MCP session manager exit_stack cleanup raised; "
                "continuing with client=None",
                exc_info=True,
            )
        finally:
            self.client = None


_HEADER_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
"""Match ``${VAR}`` and ``${VAR:-default}`` placeholders in header values.

The name production mirrors ``vars_store._NAME_RE`` (letter/underscore start,
alphanumerics/underscore after). The optional ``:-default`` suffix (POSIX
parameter-expansion syntax) supplies a fallback when the variable is unset.

Deliberately distinct from bog's install-time ``{{VAR}}`` templating — remote
MCP headers use ``${VAR}`` for Claude Code parity. Keep the two syntaxes
separate.
"""


def _interpolate_headers(headers: dict[str, Any], server_name: str) -> dict[str, Any]:
    """Resolve ``${VAR}`` references in remote-MCP header values.

    Scans each header *value* for ``${VAR}`` (and ``${VAR:-default}``)
    placeholders and substitutes them, resolving each name via the CLI secret
    vault (`vars_store.get_var`) first, then the process environment. This lets
    a user write `Authorization: Bearer ${GITHUB_TOKEN}` in `.mcp.json` instead
    of committing a raw token.

    Header *keys*, non-string values, and values without a `${...}` placeholder
    are passed through unchanged. This uses `${VAR}` syntax (Claude Code parity)
    and is intentionally separate from bog's install-time `{{VAR}}` templating.

    Args:
        headers: Header mapping from a remote (SSE/HTTP) server config.
        server_name: Name of the server, used to make errors actionable.

    Returns:
        A new dict with the same keys and interpolated string values. Non-string
        values are returned untouched.

    Raises:
        RuntimeError: If a referenced variable has no default and is unset in
            both the vault and the environment. The message names the server
            and the missing variable.
    """  # noqa: DOC502 — raised by the _resolve substitution callback
    from bog_agents_cli import vars_store

    def _resolve(match: re.Match[str]) -> str:
        name = match.group(1)
        default = match.group(2)
        try:
            value = vars_store.get_var(name)
        except ValueError:
            # Name failed vault validation; treat as "not in vault" and let the
            # environment (or default) answer instead of crashing.
            value = None
        if value is None:
            value = os.environ.get(name)
        if value is None:
            if default is not None:
                return default
            error_msg = (
                f"MCP server '{server_name}' header references undefined "
                f"variable ${{{name}}}. Set it with `/vars set {name} <value>` "
                f"(stored in the secret vault) or export {name} in your "
                "environment before starting bog-agents."
            )
            raise RuntimeError(error_msg)
        return value

    resolved: dict[str, Any] = {}
    for key, value in headers.items():
        if isinstance(value, str):
            resolved[key] = _HEADER_VAR_RE.sub(_resolve, value)
        else:
            resolved[key] = value
    return resolved


async def _load_tools_from_config(
    config: dict[str, Any],
) -> tuple[list[BaseTool], MCPSessionManager, list[MCPServerInfo]]:
    """Build MCP connections from a validated config and load tools.

    This is the shared implementation used by both `get_mcp_tools` (explicit
    path) and `resolve_and_load_mcp_tools` (auto-discovery).

    Args:
        config: Validated MCP configuration dict with `mcpServers` key.

    Returns:
        Tuple of `(tools_list, session_manager, server_infos)`.

    Raises:
        RuntimeError: If MCP server fails to spawn or connect.
    """
    from langchain_mcp_adapters.client import MultiServerMCPClient
    from langchain_mcp_adapters.sessions import (
        SSEConnection,
        StdioConnection,
        StreamableHttpConnection,
    )
    from langchain_mcp_adapters.tools import load_mcp_tools

    # Create connections dict for MultiServerMCPClient
    # Convert Claude Desktop format to langchain-mcp-adapters format
    connections: dict[str, Connection] = {}
    # CT-4: per-server config failures (an undefined ${VAR} in one server's
    # headers) must not disable every other server — record the error here and
    # surface it on that server's MCPServerInfo instead of raising out of the
    # whole load.
    failed_servers: dict[str, str] = {}
    for server_name, server_config in config["mcpServers"].items():
        server_type = _resolve_server_type(server_config)

        if server_type in _SUPPORTED_REMOTE_TYPES:
            # langchain-mcp-adapters uses "streamable_http" for HTTP transport
            if server_type == "http":
                conn: Connection = StreamableHttpConnection(
                    transport="streamable_http",
                    url=server_config["url"],
                )
            else:
                conn = SSEConnection(
                    transport="sse",
                    url=server_config["url"],
                )
            if "headers" in server_config:
                try:
                    conn["headers"] = _interpolate_headers(
                        server_config["headers"], server_name
                    )
                except RuntimeError as exc:
                    failed_servers[server_name] = str(exc)
                    logger.warning("MCP server %r skipped: %s", server_name, exc)
                    continue
            # Attach a spec-compliant OAuth provider when the server opted into
            # OAuth or already has stored tokens (None for stdio, static-header,
            # or no-auth servers — those connections are unchanged).
            from bog_agents_cli.mcp_auth import _resolve_mcp_auth

            auth = _resolve_mcp_auth(server_name, server_config)
            if auth is not None:
                conn["auth"] = auth  # ty: ignore[invalid-assignment]
            connections[server_name] = conn
        else:
            # stdio server connection (default)
            connections[server_name] = StdioConnection(
                command=server_config["command"],
                args=server_config.get("args", []),
                env=server_config.get("env") or None,
                transport="stdio",
            )

    # Session manager retained as a no-op handle for API compatibility
    # (callers expect a 3-tuple). Tools returned below are
    # *connection-bound* (per-call sessions), so there is no persistent
    # session whose lifetime needs managing — the manager's ``cleanup``
    # is effectively a no-op now.
    manager = MCPSessionManager()

    try:
        client = MultiServerMCPClient(connections=connections)
        manager.client = client
    except Exception as e:
        await manager.cleanup()
        error_msg = f"Failed to initialize MCP client: {e}"
        raise RuntimeError(error_msg) from e

    # ``mcp.client.stdio.stdio_client`` defaults its ``errlog`` parameter
    # to ``sys.stderr``, which on Windows is fed straight into
    # ``subprocess.Popen`` → ``msvcrt.get_osfhandle(stderr.fileno())``.
    # When the parent process's stderr lacks a usable OS handle (Python
    # 3.13 Windows quirks, terminal wrappers that pipe stderr, certain
    # TUI environments) the spawn dies with ``OSError: [Errno 9] Bad
    # file descriptor`` before the MCP server even runs. We install a
    # permanent override of MCP's default ``errlog`` here so EVERY spawn
    # — the load-time one below AND the per-call ones triggered each
    # time the agent invokes an MCP tool — inherits a valid fd. (The
    # context-manager flavour was wrong for per-call sessions because
    # the patch would be off when the agent's tool invocation actually
    # spawns the subprocess.)
    from bog_agents_cli._subprocess_stderr import (
        install_safe_subprocess_stderr_default,
    )

    install_safe_subprocess_stderr_default()

    timeout_s = _mcp_startup_timeout_seconds()

    try:
        all_tools: list[BaseTool] = []
        server_infos: list[MCPServerInfo] = []
        for server_name, server_config in config["mcpServers"].items():
            # Connection-bound tools: ``load_mcp_tools`` opens a one-shot
            # session via the connection to list tool metadata, then
            # returns tools that each open their OWN fresh session
            # ``async with create_session(connection)`` per invocation.
            # This is the only pattern that survives the build-time
            # ``asyncio.run(...)`` loop closing — a session held on a
            # closed loop turns every later tool call into an indefinite
            # hang on a dead anyio task group, which is exactly the
            # ``/init`` symptom we just chased.
            transport = _resolve_server_type(server_config)
            # CT-4: a server whose connection could not be built (unresolvable
            # ${VAR} header) reports its own error; the others still load.
            failed = failed_servers.get(server_name)
            if failed is not None:
                server_infos.append(
                    MCPServerInfo(name=server_name, transport=transport, error=failed)
                )
                continue
            connection = connections[server_name]
            # An OAuth-opt-in server with no stored token cannot connect
            # non-interactively — attaching a provider would drive a browser
            # (blocking) or time out opaquely. Skip it with an actionable hint
            # so the user knows to run ``/mcp login <server>`` first.
            from bog_agents_cli.mcp_auth import auth_login_hint, needs_oauth_login

            if needs_oauth_login(server_name, server_config):
                err = auth_login_hint(server_name)
                logger.info("MCP server %r skipped: %s", server_name, err)
                server_infos.append(
                    MCPServerInfo(name=server_name, transport=transport, error=err)
                )
                continue
            try:
                tools = await asyncio.wait_for(
                    load_mcp_tools(
                        None,
                        connection=connection,
                        server_name=server_name,
                        tool_name_prefix=True,
                    ),
                    timeout=timeout_s,
                )
            except TimeoutError:
                # A misbehaving stdio server (e.g. ``npx -y …`` doing a
                # cold install, OAuth-pending SSE server) used to freeze
                # the entire welcome banner. We log + record the failure
                # on ``MCPServerInfo`` and move on — the agent can still
                # use the servers that did come up. Fixes P0-D.
                err = (
                    f"timed out after {timeout_s:.1f}s "
                    f"(override via {_MCP_STARTUP_TIMEOUT_ENV})"
                )
                logger.warning(
                    "MCP server %r failed to start: %s",
                    server_name,
                    err,
                )
                server_infos.append(
                    MCPServerInfo(
                        name=server_name,
                        transport=transport,
                        error=err,
                    )
                )
                continue
            except Exception as per_server_exc:
                # Same isolation strategy: a single broken server should
                # never brick the rest of the rulebook. A 401 challenge means
                # the server wants OAuth — surface an actionable login hint
                # instead of the opaque underlying error.
                from bog_agents_cli.mcp_auth import auth_login_hint, is_auth_challenge

                if is_auth_challenge(per_server_exc):
                    err = auth_login_hint(server_name)
                else:
                    err = f"startup failed: {per_server_exc}"
                logger.warning(
                    "MCP server %r failed to start: %s",
                    server_name,
                    per_server_exc,
                )
                server_infos.append(
                    MCPServerInfo(
                        name=server_name,
                        transport=transport,
                        error=err,
                    )
                )
                continue
            all_tools.extend(tools)
            server_infos.append(
                MCPServerInfo(
                    name=server_name,
                    transport=transport,
                    tools=[
                        MCPToolInfo(name=t.name, description=t.description or "")
                        for t in tools
                    ],
                )
            )
    except Exception as e:
        await manager.cleanup()
        from bog_agents_cli._subprocess_stderr import (
            diagnostic_info,
            tail_mcp_stderr_log,
        )

        hint = ""
        err_str = str(e).lower()
        # Tail the MCP stderr log when present — when the child process
        # exited with a usable error message (most common cause of
        # downstream ClosedResourceError when the parent tries to read
        # after the child died), the log tail saves the user from
        # running ``npx ...`` manually to reproduce.
        log_tail = tail_mcp_stderr_log(4000)
        if log_tail:
            hint += (
                "\n\nMCP server stderr "
                f"(tail of {diagnostic_info()['log_path']}):\n"
                f"----\n{log_tail[-2000:]}\n----"
            )

        if "bad file descriptor" in err_str or "errno 9" in err_str:
            info = diagnostic_info()
            hint += (
                f"\n\nDiagnostic: stderr_usable={info['stderr_usable']}, "
                f"stderr_class={info['stderr_class']}, "
                f"platform={info['platform']}. "
                f"MCP stderr log: {info['log_path']}\n"
                "If you're seeing this on Windows, try running "
                "bog-agents from a fresh terminal (not a wrapped/piped one)."
            )

        # ``ClosedResourceError`` from anyio means the MCP child closed
        # its pipe before the handshake completed. Almost always the
        # child crashed on startup (bad config, missing creds, wrong
        # binary). Point the user at the stderr log we just tailed.
        if "closedresourceerror" in err_str or "closed resource" in err_str:
            hint += (
                "\n\nClosedResourceError suggests the MCP child process "
                "exited before the handshake completed. Common causes:\n"
                "  - Missing credentials/config the server expects "
                "(e.g. JIRA_URL, GITHUB_TOKEN).\n"
                "  - The command/binary isn't installed (e.g. ``uvx`` or "
                "``npx`` not on PATH).\n"
                "  - The server crashed on a config validation error.\n"
                "Run the command manually with the same args to see the "
                "actual error, or check the stderr tail above."
            )

        error_msg = (
            f"Failed to load tools from MCP server '{server_name}': {e}\n"
            "For stdio servers: Check that the command and args are correct,"
            " and that the MCP server is installed"
            " (e.g., run 'npx -y <package>' manually to test).\n"
            "For sse/http servers: Check that the URL is correct"
            " and the server is running." + hint
        )
        raise RuntimeError(error_msg) from e

    return all_tools, manager, server_infos


async def get_mcp_tools(
    config_path: str,
) -> tuple[list[BaseTool], MCPSessionManager, list[MCPServerInfo]]:
    """Load MCP tools from configuration file with stateful sessions.

    Supports multiple server types:
    - stdio: Spawns MCP servers as subprocesses with persistent sessions
    - sse/http: Connects to remote MCP servers via URL

    For stdio servers, this creates persistent sessions that remain active
    across tool calls, avoiding server restarts. Sessions are managed by
    `MCPSessionManager` and should be cleaned up with
    `session_manager.cleanup()` when done.

    Args:
        config_path: Path to MCP JSON configuration file.

    Returns:
        Tuple of `(tools_list, session_manager, server_infos)` where:
            - tools_list: List of LangChain `BaseTool` objects
            - session_manager: `MCPSessionManager` instance
                (call `cleanup()` when done)
            - server_infos: List of `MCPServerInfo` with per-server metadata
    """
    config = load_mcp_config(config_path)
    return await _load_tools_from_config(config)


async def resolve_and_load_mcp_tools(
    *,
    explicit_config_path: str | None = None,
    no_mcp: bool = False,
    trust_project_mcp: bool | None = None,
    project_context: ProjectContext | None = None,
) -> tuple[list[BaseTool], MCPSessionManager | None, list[MCPServerInfo]]:
    """Resolve MCP config and load tools.

    Auto-discovers configs from standard locations and merges them.
    When `explicit_config_path` is provided it is added as the
    highest-precedence source (errors in that file are fatal).

    Args:
        explicit_config_path: Extra config file to layer on top of
            auto-discovered configs (highest precedence). Errors are
            fatal.
        no_mcp: If True, disable all MCP loading.
        trust_project_mcp: Controls project-level stdio server trust:

            - `True`: allow all project stdio servers (flag/prompt approved).
            - `False`: filter out project stdio servers, log warning.
            - `None` (default): check the persistent trust store; if the
                fingerprint matches, allow; otherwise filter + warn.
        project_context: Explicit project path context for config discovery
            and trust resolution.

    Returns:
        Tuple of `(tools_list, session_manager, server_infos)`.

            When no tools are loaded, returns `([], None, [])`.

    Raises:
        RuntimeError: If an MCP server config is invalid or fails to
            spawn/connect.
    """
    if no_mcp:
        return [], None, []

    # Auto-discovery
    try:
        config_paths = discover_mcp_configs(project_context=project_context)
    except (OSError, RuntimeError):
        logger.warning("MCP config auto-discovery failed", exc_info=True)
        config_paths = []

    # Classify discovered configs and apply trust filtering
    user_configs, project_configs = classify_discovered_configs(config_paths)

    configs: list[dict[str, Any]] = []

    # User-level configs are always trusted
    for path in user_configs:
        cfg = load_mcp_config_lenient(path)
        if cfg is not None:
            configs.append(cfg)

    # Project-level configs need trust gating. The gate covers EVERY project
    # server — stdio AND remote (SSE/HTTP). A remote server in a cloned repo
    # would otherwise auto-load and stream the conversation (plus any
    # configured Authorization header) to an attacker-controlled URL with no
    # consent. When untrusted, ALL project servers are dropped, not just
    # stdio. (REVIEW.md v2 P1-49; the earlier gate only covered stdio.)
    for path in project_configs:
        cfg = load_mcp_config_lenient(path)
        if cfg is None:
            continue

        servers = cfg.get("mcpServers", {})
        if not isinstance(servers, dict) or not servers:
            # No servers to gate.
            continue

        if trust_project_mcp is True:
            trusted = True
        elif trust_project_mcp is False:
            trusted = False
        else:
            # None — consult the persistent trust store.
            from bog_agents_cli.mcp_trust import (
                compute_config_fingerprint,
                is_project_mcp_trusted,
            )

            project_root = str(_resolve_project_config_base(project_context).resolve())
            fingerprint = compute_config_fingerprint(project_configs)
            trusted = is_project_mcp_trusted(project_root, fingerprint)

        if trusted:
            configs.append(cfg)
        else:
            skipped = [
                f"{name} ({_resolve_server_type(srv) if isinstance(srv, dict) else '?'})"
                for name, srv in servers.items()
            ]
            logger.warning(
                "Skipped %d untrusted project MCP server(s) — stdio and remote alike — "
                "until the project is trusted (config changed or not yet approved): %s",
                len(skipped),
                "; ".join(skipped),
            )

    # Explicit path is highest precedence — errors are fatal
    if explicit_config_path:
        config_path = (
            str(project_context.resolve_user_path(explicit_config_path))
            if project_context is not None
            else explicit_config_path
        )
        configs.append(load_mcp_config(config_path))

    if not configs:
        return [], None, []

    merged = merge_mcp_configs(configs)
    if not merged.get("mcpServers"):
        return [], None, []

    # Validate each server in the merged config
    try:
        for server_name, server_config in merged["mcpServers"].items():
            _validate_server_config(server_name, server_config)
    except (TypeError, ValueError) as e:
        msg = f"Invalid MCP server configuration: {e}"
        raise RuntimeError(msg) from e

    return await _load_tools_from_config(merged)
