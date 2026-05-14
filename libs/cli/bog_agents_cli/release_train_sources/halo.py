"""Halo PSA / Halo ITSM enrichment source for ``/release-train``.

Two transports:

* **MCP** — same shape as Jira's MCP transport. Looks up a server
  matching ``cfg.mcp_server``, finds a ticket-fetching tool, invokes
  per key.

* **REST API** — OAuth2 client-credentials flow:
    1. POST ``{api_base_url}/auth/token`` with
       ``grant_type=client_credentials`` to mint an access token.
    2. GET ``{api_base_url}/api/Tickets/{id}`` per ticket.

Halo's ticket keys can be bare numeric ids (``12345``), prefixed
(``INC-123``, ``CHG-456``), or fully namespaced (``HALO-789``).
The default regex captures the trailing numeric id; we look up by
that id since Halo's REST API addresses tickets by their integer
primary key.
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
    resolve_halo_transport,
)
from bog_agents_cli.release_train_sources.jira import (
    _close_mcp_session,
    _open_mcp_session,
    _pick_tool,
    _str_field,
)

if TYPE_CHECKING:
    from bog_agents_cli.release_train import CommitEntry
    from bog_agents_cli.release_train_config import HaloSourceConfig

logger = logging.getLogger(__name__)


_HALO_TOOL_HINTS = (
    "get_halo_ticket",
    "halo_get_ticket",
    "halo_ticket_get",
    "get_ticket",
)


async def resolve_halo(
    commits: list[CommitEntry], cfg: HaloSourceConfig
) -> SourceResolution:
    """End-to-end Halo enrichment. Mirrors :func:`jira.resolve_jira`."""
    keys = extract_keys(
        commits,
        pattern=cfg.ticket_key_regex,
        project_filter=None,
        max_keys=cfg.max_keys,
    )
    if not keys:
        return SourceResolution(
            source="halo",
            transport="off",
            detail="no Halo ticket keys matched in commit subjects",
            keys_extracted=0,
            keys_resolved=0,
        )

    transport, detail = resolve_halo_transport(cfg)
    if transport == "off":
        return SourceResolution(
            source="halo",
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
                "release-train halo: MCP transport failed (%s); falling back to API",
                exc,
            )
            if _has_api_creds(cfg):
                tickets = await _fetch_via_api(keys, cfg)
                transport = "api"
                detail = "API fallback after MCP failure"
            else:
                return SourceResolution(
                    source="halo",
                    transport="error",
                    detail=f"MCP failed: {exc}",
                    keys_extracted=len(keys),
                    keys_resolved=0,
                )
    elif transport == "api":
        tickets = await _fetch_via_api(keys, cfg)

    if cfg.ticket_types:
        wanted = {t.lower() for t in cfg.ticket_types}
        tickets = [t for t in tickets if t.issue_type.lower() in wanted]

    attach_tickets(commits, tickets, attr="halo_tickets")
    return SourceResolution(
        source="halo",
        transport=transport,
        detail=detail,
        keys_extracted=len(keys),
        keys_resolved=len(tickets),
    )


def _has_api_creds(cfg: HaloSourceConfig) -> bool:
    return (
        bool(cfg.api_base_url)
        and bool(os.environ.get(cfg.api_client_id_env, ""))
        and bool(os.environ.get(cfg.api_client_secret_env, ""))
    )


# ---------------------------------------------------------------------------
# REST API transport
# ---------------------------------------------------------------------------


async def _fetch_via_api(
    keys: list[str], cfg: HaloSourceConfig
) -> list[ResolvedTicket]:
    """Fetch Halo tickets via REST. Mints an OAuth2 token, then GETs each ticket."""
    import httpx

    client_id = os.environ.get(cfg.api_client_id_env, "")
    client_secret = os.environ.get(cfg.api_client_secret_env, "")
    if not client_id or not client_secret:
        logger.warning(
            "release-train halo: %s/%s unset — no OAuth2 creds available",
            cfg.api_client_id_env,
            cfg.api_client_secret_env,
        )
        return []
    base = cfg.api_base_url.rstrip("/")

    async with httpx.AsyncClient(timeout=cfg.timeout_seconds) as client:
        token = await _mint_halo_token(client, base, client_id, client_secret, cfg)
        if not token:
            return []
        tickets: list[ResolvedTicket] = []
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }
        for key in keys:
            ticket_id = _key_to_ticket_id(key)
            if not ticket_id:
                continue
            url = f"{base}/api/Tickets/{ticket_id}"
            try:
                resp = await client.get(url, headers=headers)
            except (httpx.HTTPError, OSError) as exc:
                logger.warning("release-train halo: GET %s failed: %s", url, exc)
                continue
            if resp.status_code == 404:
                continue
            if resp.status_code >= 400:
                logger.warning(
                    "release-train halo: ticket %s returned %s",
                    ticket_id,
                    resp.status_code,
                )
                continue
            try:
                data = resp.json()
            except ValueError:
                continue
            ticket = _parse_halo_ticket(key, data, base)
            if ticket:
                tickets.append(ticket)
    return tickets


async def _mint_halo_token(
    client: Any,  # noqa: ANN401
    base: str,
    client_id: str,
    client_secret: str,
    cfg: HaloSourceConfig,
) -> str:
    """Run the OAuth2 client-credentials flow and return the access token."""
    import httpx

    url = f"{base}/auth/token"
    body: dict[str, str] = {
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
        "scope": cfg.api_scope or "all",
    }
    if cfg.api_tenant:
        body["tenant"] = cfg.api_tenant
    try:
        resp = await client.post(
            url,
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    except (httpx.HTTPError, OSError) as exc:
        logger.warning("release-train halo: token mint failed: %s", exc)
        return ""
    if resp.status_code >= 400:
        logger.warning("release-train halo: token mint returned %s", resp.status_code)
        return ""
    try:
        data = resp.json()
    except ValueError:
        return ""
    token = data.get("access_token") if isinstance(data, dict) else None
    return token if isinstance(token, str) else ""


def _key_to_ticket_id(key: str) -> str:
    """Extract the numeric ticket id from a key like ``"INC-123"`` or ``"123"``."""
    if not key:
        return ""
    # Last sequence of digits in the key.
    digits = ""
    for ch in key:
        if ch.isdigit():
            digits += ch
        elif digits:
            # Reset so we keep only the trailing run.
            digits = ""
    # If the loop produced no trailing digits, scan again for any digit run.
    if not digits:
        run = ""
        for ch in key:
            if ch.isdigit():
                run += ch
            elif run:
                break
        digits = run
    return digits


def _parse_halo_ticket(
    key: str, data: dict[str, Any], base_url: str
) -> ResolvedTicket | None:
    """Convert a Halo REST ticket payload into a :class:`ResolvedTicket`."""
    if not isinstance(data, dict):
        return None
    summary = _str_field(data.get("summary")) or _str_field(data.get("title"))
    status_obj = data.get("status") or {}
    if isinstance(status_obj, dict):
        status = _str_field(status_obj.get("name"))
    else:
        status = _str_field(status_obj)
    type_obj = data.get("tickettype") or data.get("type") or {}
    if isinstance(type_obj, dict):
        issue_type = _str_field(type_obj.get("name"))
    else:
        issue_type = _str_field(type_obj)
    ticket_id = (
        str(data.get("id"))
        if isinstance(data.get("id"), (int, str))
        else _key_to_ticket_id(key)
    )
    return ResolvedTicket(
        key=key,
        source="halo",
        summary=summary,
        status=status,
        issue_type=issue_type,
        url=f"{base_url}/tickets/{ticket_id}" if ticket_id else "",
        extra={
            k: v
            for k, v in data.items()
            if k in {"priority", "user", "agent", "client"}
        },
    )


# ---------------------------------------------------------------------------
# MCP transport
# ---------------------------------------------------------------------------


async def _fetch_via_mcp(
    keys: list[str], cfg: HaloSourceConfig
) -> list[ResolvedTicket]:
    """Fetch Halo tickets via an MCP server. Reuses Jira's session helpers."""
    server = await _open_mcp_session(cfg.mcp_server)
    if server is None:
        return []
    client, tools = server

    tool = _pick_tool(tools, cfg.mcp_tool_name, _HALO_TOOL_HINTS)
    if tool is None:
        logger.warning(
            "release-train halo: MCP server %r exposes no ticket-fetching tool "
            "matching %s; skipping",
            cfg.mcp_server,
            cfg.mcp_tool_name or list(_HALO_TOOL_HINTS),
        )
        await _close_mcp_session(client)
        return []

    tickets: list[ResolvedTicket] = []
    try:
        for key in keys:
            payload = await _invoke_halo_mcp_tool(tool, key)
            ticket = _ticket_from_mcp_payload(key, payload, cfg.api_base_url)
            if ticket:
                tickets.append(ticket)
    finally:
        await _close_mcp_session(client)
    return tickets


async def _invoke_halo_mcp_tool(tool: Any, key: str) -> Any:  # noqa: ANN401
    """Invoke a Halo MCP tool with several argument shapes."""
    ticket_id = _key_to_ticket_id(key) or key
    candidates: tuple[dict[str, str], ...] = (
        {"ticket_id": ticket_id},
        {"id": ticket_id},
        {"ticket": ticket_id},
        {"key": key},
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
            "release-train halo: all MCP arg shapes failed for %s: %s", key, last_exc
        )
    return None


def _ticket_from_mcp_payload(
    key: str,
    payload: Any,  # noqa: ANN401
    base_url: str,
) -> ResolvedTicket | None:
    """Best-effort parse of an MCP tool result into a Halo ticket."""
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
        return _parse_halo_ticket(key, data, base_url or "")
    if isinstance(data, str):
        return ResolvedTicket(
            key=key,
            source="halo",
            summary=data[:280],
            url=f"{base_url}/tickets/{_key_to_ticket_id(key)}" if base_url else "",
        )
    return None
