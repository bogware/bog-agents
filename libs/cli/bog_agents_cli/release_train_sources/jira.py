"""Jira enrichment source for ``/release-train``.

Two transports:

* **MCP** — looks up an MCP server matching ``cfg.mcp_server`` in the
  user's MCP config, spawns it, finds the issue-fetching tool by name
  (configurable, with heuristic defaults), and invokes it once per
  extracted key.

* **REST API** — basic auth (email + token) to
  ``{api_base_url}/rest/api/3/issue/{key}``. Atlassian Cloud is the
  primary target; Server/Data Center deployments should also work
  with the same path under ``/rest/api/2/``.

All network calls have explicit timeouts and never raise into the
release-notes path. Failures degrade to "fewer tickets resolved" with
a logged warning.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any

from bog_agents_cli.release_train_sources import (
    ResolvedTicket,
    SourceResolution,
    attach_tickets,
    extract_keys,
    resolve_jira_transport,
)

if TYPE_CHECKING:
    from bog_agents_cli.release_train import CommitEntry
    from bog_agents_cli.release_train_config import JiraSourceConfig

logger = logging.getLogger(__name__)


_JIRA_TOOL_HINTS = (
    "get_jira_issue",
    "jira_get_issue",
    "jira_issue_get",
    "getJiraIssue",
    "get_issue",
)


async def resolve_jira(
    commits: list[CommitEntry], cfg: JiraSourceConfig
) -> SourceResolution:
    """End-to-end Jira enrichment.

    1. Extract keys via regex (filtered by ``project_keys`` if set).
    2. Decide transport (MCP / API / off).
    3. Fetch one ticket per unique key.
    4. Attach matching tickets to each commit's ``jira_tickets``.
    """
    keys = extract_keys(
        commits,
        pattern=cfg.issue_key_regex,
        project_filter=cfg.project_keys,
        max_keys=cfg.max_keys,
    )
    if not keys:
        return SourceResolution(
            source="jira",
            transport="off",
            detail="no Jira keys matched in commit subjects",
            keys_extracted=0,
            keys_resolved=0,
        )

    transport, detail = resolve_jira_transport(cfg)
    if transport == "off":
        return SourceResolution(
            source="jira",
            transport="off",
            detail=detail,
            keys_extracted=len(keys),
            keys_resolved=0,
        )

    tickets: list[ResolvedTicket] = []
    if transport == "mcp":
        try:
            tickets = await _fetch_via_mcp(keys, cfg)
        except Exception as exc:
            logger.warning(
                "release-train jira: MCP transport failed (%s); falling back to API",
                exc,
            )
            api_transport, api_detail = (
                ("api", "API fallback after MCP failure")
                if _has_api_creds(cfg)
                else ("off", f"MCP failed and no API creds: {exc}")
            )
            if api_transport == "api":
                tickets = await _fetch_via_api(keys, cfg)
                transport = "api"
                detail = api_detail
            else:
                return SourceResolution(
                    source="jira",
                    transport="error",
                    detail=f"MCP failed: {exc}",
                    keys_extracted=len(keys),
                    keys_resolved=0,
                )
    elif transport == "api":
        tickets = await _fetch_via_api(keys, cfg)

    attach_tickets(commits, tickets, attr="jira_tickets")
    return SourceResolution(
        source="jira",
        transport=transport,
        detail=detail,
        keys_extracted=len(keys),
        keys_resolved=len(tickets),
    )


def _has_api_creds(cfg: JiraSourceConfig) -> bool:
    return bool(cfg.api_base_url) and bool(os.environ.get(cfg.api_token_env, ""))


# ---------------------------------------------------------------------------
# REST API transport
# ---------------------------------------------------------------------------


async def _fetch_via_api(
    keys: list[str], cfg: JiraSourceConfig
) -> list[ResolvedTicket]:
    """Fetch Jira issues via the REST API. Returns one ticket per resolved key."""
    import httpx

    email = os.environ.get(cfg.api_email_env, "")
    token = os.environ.get(cfg.api_token_env, "")
    if not token:
        logger.warning(
            "release-train jira: %s unset — no auth available", cfg.api_token_env
        )
        return []
    auth = (email, token)
    base = cfg.api_base_url.rstrip("/")
    fields_param = ",".join(cfg.fields) if cfg.fields else ""

    tickets: list[ResolvedTicket] = []
    async with httpx.AsyncClient(timeout=cfg.timeout_seconds) as client:
        for key in keys:
            url = f"{base}/rest/api/3/issue/{key}"
            params = {"fields": fields_param} if fields_param else None
            try:
                resp = await client.get(
                    url,
                    auth=auth,
                    params=params,
                    headers={"Accept": "application/json"},
                )
            except (httpx.HTTPError, OSError) as exc:
                logger.warning("release-train jira: GET %s failed: %s", url, exc)
                continue
            if resp.status_code == 404:
                logger.debug("release-train jira: %s not found", key)
                continue
            if resp.status_code >= 400:
                logger.warning(
                    "release-train jira: %s returned %s", key, resp.status_code
                )
                continue
            try:
                data = resp.json()
            except ValueError:
                logger.warning("release-train jira: %s returned non-JSON", key)
                continue
            ticket = _parse_jira_issue(key, data, base)
            if ticket:
                tickets.append(ticket)
    return tickets


def _parse_jira_issue(
    key: str, data: dict[str, Any], base_url: str
) -> ResolvedTicket | None:
    """Convert a Jira REST issue payload into a :class:`ResolvedTicket`."""
    if not isinstance(data, dict):
        return None
    fields_obj = data.get("fields") or {}
    if not isinstance(fields_obj, dict):
        fields_obj = {}
    summary = _str_field(fields_obj.get("summary"))
    status_obj = fields_obj.get("status") or {}
    status = _str_field(status_obj.get("name")) if isinstance(status_obj, dict) else ""
    issuetype_obj = fields_obj.get("issuetype") or {}
    issuetype = (
        _str_field(issuetype_obj.get("name")) if isinstance(issuetype_obj, dict) else ""
    )
    fix_versions_raw = fields_obj.get("fixVersions") or []
    fix_versions: list[str] = []
    if isinstance(fix_versions_raw, list):
        for entry in fix_versions_raw:
            if isinstance(entry, dict):
                name = _str_field(entry.get("name"))
                if name:
                    fix_versions.append(name)
    return ResolvedTicket(
        key=key,
        source="jira",
        summary=summary,
        status=status,
        issue_type=issuetype,
        url=f"{base_url}/browse/{key}",
        fix_versions=fix_versions,
    )


def _str_field(value: Any) -> str:  # noqa: ANN401
    return value.strip() if isinstance(value, str) else ""


# ---------------------------------------------------------------------------
# MCP transport
# ---------------------------------------------------------------------------


async def _fetch_via_mcp(
    keys: list[str], cfg: JiraSourceConfig
) -> list[ResolvedTicket]:
    """Fetch Jira issues via an MCP server. Returns resolved tickets.

    Spawns the MCP server once, calls the issue-fetching tool for each
    key, then tears the session down. Tool name is taken from
    ``cfg.mcp_tool_name`` when set, otherwise auto-detected via
    :data:`_JIRA_TOOL_HINTS`.
    """
    server = await _open_mcp_session(cfg.mcp_server)
    if server is None:
        return []
    client, tools = server

    tool = _pick_tool(tools, cfg.mcp_tool_name, _JIRA_TOOL_HINTS)
    if tool is None:
        logger.warning(
            "release-train jira: MCP server %r exposes no issue-fetching tool "
            "matching %s; skipping",
            cfg.mcp_server,
            cfg.mcp_tool_name or list(_JIRA_TOOL_HINTS),
        )
        await _close_mcp_session(client)
        return []

    tickets: list[ResolvedTicket] = []
    try:
        for key in keys:
            payload = await _invoke_mcp_tool(tool, key)
            ticket = _ticket_from_mcp_payload(key, payload, cfg.api_base_url)
            if ticket:
                tickets.append(ticket)
    finally:
        await _close_mcp_session(client)
    return tickets


async def _open_mcp_session(server_name: str) -> tuple[Any, list[Any]] | None:
    """Spawn the MCP server and return ``(client, tools)`` for the named server.

    Returns ``None`` when the server is not configured, ``langchain_mcp_adapters``
    is missing, or the session fails to open.
    """
    if not server_name:
        return None
    try:
        from langchain_mcp_adapters.client import (
            MultiServerMCPClient,  # type: ignore[import-not-found]
        )
    except ImportError:
        logger.warning(
            "release-train: langchain_mcp_adapters not installed; MCP transport unavailable"
        )
        return None

    from bog_agents_cli.mcp_tools import (
        discover_mcp_configs,
        load_mcp_config_lenient,
        merge_mcp_configs,
    )

    raw_configs = [load_mcp_config_lenient(p) for p in discover_mcp_configs()]
    configs = [c for c in raw_configs if c]
    if not configs:
        return None
    merged = merge_mcp_configs(configs)
    servers = merged.get("mcpServers") or {}
    if server_name not in servers:
        return None
    connection_cfg = {server_name: servers[server_name]}
    try:
        client = MultiServerMCPClient(connections=connection_cfg)
        tools = await client.get_tools()
    except Exception as exc:
        logger.warning(
            "release-train: MCP server %r failed to start: %s", server_name, exc
        )
        return None
    return (client, list(tools))


async def _close_mcp_session(client: Any) -> None:  # noqa: ANN401
    close_fn = getattr(client, "aclose", None) or getattr(client, "close", None)
    if close_fn is None:
        return
    try:
        result = close_fn()
        if hasattr(result, "__await__"):
            await result
    except Exception as exc:
        logger.debug("release-train: MCP client close raised: %s", exc)


def _pick_tool(
    tools: list[Any],
    explicit_name: str,
    hints: tuple[str, ...],
) -> Any:  # noqa: ANN401
    """Choose a tool by explicit name or substring match against hints."""
    if explicit_name:
        for t in tools:
            if getattr(t, "name", "") == explicit_name:
                return t
        # Fall through to fuzzy if explicit didn't match.
    for hint in hints:
        for t in tools:
            name = getattr(t, "name", "")
            if name and hint.lower() in name.lower():
                return t
    return None


async def _invoke_mcp_tool(tool: Any, key: str) -> Any:  # noqa: ANN401
    """Invoke an MCP tool with whichever argument shape it accepts.

    MCP tools vary in their input schema — some expect ``issue_key``,
    some ``key``, some ``issue_id``. We try a few candidates.
    """
    candidates: tuple[dict[str, str], ...] = (
        {"issue_key": key},
        {"key": key},
        {"issue_id": key},
        {"id": key},
    )
    last_exc: Exception | None = None
    for payload in candidates:
        try:
            return await tool.ainvoke(payload)
        except Exception as exc:
            last_exc = exc
            continue
    if last_exc:
        logger.debug(
            "release-train jira: all MCP arg shapes failed for %s: %s", key, last_exc
        )
    return None


def _ticket_from_mcp_payload(
    key: str,
    payload: Any,  # noqa: ANN401
    base_url: str,
) -> ResolvedTicket | None:
    """Best-effort parse of an MCP tool result into a ticket.

    MCP tool results are unstructured (often markdown or JSON-as-string).
    We attempt JSON-parsing strings and fall back to surfacing the raw
    text as the summary.
    """
    if payload is None:
        return None

    data: Any = payload
    if isinstance(payload, str):
        text = payload.strip()
        if text.startswith("{"):
            import json

            try:
                data = json.loads(text)
            except (json.JSONDecodeError, ValueError):
                data = text
        else:
            data = text

    if isinstance(data, dict):
        # Try Jira-shaped fields first; fall back to flat keys.
        if "fields" in data:
            return _parse_jira_issue(key, data, base_url or "")
        return ResolvedTicket(
            key=key,
            source="jira",
            summary=_str_field(data.get("summary")) or _str_field(data.get("title")),
            status=_str_field(data.get("status")),
            issue_type=_str_field(data.get("issuetype"))
            or _str_field(data.get("type")),
            url=_str_field(data.get("url"))
            or (f"{base_url}/browse/{key}" if base_url else ""),
            extra={k: v for k, v in data.items() if k not in {"summary", "status"}},
        )
    if isinstance(data, str):
        return ResolvedTicket(
            key=key,
            source="jira",
            summary=data[:280],
            url=f"{base_url}/browse/{key}" if base_url else "",
        )
    return None
