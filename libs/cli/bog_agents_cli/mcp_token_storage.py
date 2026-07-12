"""File-backed OAuth token storage for remote MCP servers.

Implements the MCP SDK's `TokenStorage` protocol against a per-server JSON
file under `~/.bog-agents/mcp-oauth/<safe-server-name>.json`. The SDK's
`OAuthClientProvider` calls into this storage to persist the dynamic client
registration and the issued/refreshed tokens; this module owns nothing about
the OAuth handshake itself.

Security notes:

- The persisted file holds bearer + refresh tokens. Writes go through
    `io_utils.atomic_write_text` (mode 0600 on POSIX) followed by
    `vars_store._secure_owner_only` so the file is owner-only on Windows too
    (a bare `chmod` is a no-op there).
- `mcp.shared.auth.OAuthToken` is a pydantic model whose default `repr`
    includes the access and refresh token strings verbatim. Never log one via
    `%r`, `str()`, an f-string, or `logger.exception`. This module logs only
    structural facts (server name, expiry seconds, field type names).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from mcp.client.auth import TokenStorage
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import ValidationError

from bog_agents_cli.io_utils import atomic_write_text
from bog_agents_cli.vars_store import _secure_owner_only

logger = logging.getLogger(__name__)

_STORAGE_VERSION = 1
"""Schema version stamped into each token file.

Bump on an incompatible envelope change so `_read` rejects (fail-closed) any
file written by an older/newer layout instead of mis-parsing it.
"""

_MAX_TOKEN_FILE_BYTES = 256 * 1024
"""Reject token files larger than this before parsing (fail-closed).

A real envelope is a few kilobytes; anything this large is corrupt or hostile,
so it is treated as absent rather than fed to `json.loads`.
"""


def default_oauth_dir() -> Path:
    """Return the directory that holds per-server MCP OAuth token files.

    Split out as a module function so tests can monkeypatch it to redirect
    storage into a temporary directory without touching the real home dir.

    Returns:
        The `~/.bog-agents/mcp-oauth/` directory path.
    """
    return Path.home() / ".bog-agents" / "mcp-oauth"


def is_safe_server_name(server_name: str) -> bool:
    """Return whether `server_name` is safe to embed in a token filename.

    A safe name is a non-empty run of ASCII letters, digits, underscores,
    hyphens, and dots that is neither `.` nor `..` and contains no path
    separator. This keeps the on-disk path inside `default_oauth_dir()` and
    blocks traversal payloads like `../../etc/x`.

    Args:
        server_name: Configured MCP server name.

    Returns:
        `True` when the name can be used verbatim as a filename stem.
    """
    if not server_name or server_name in {".", ".."}:
        return False
    if "/" in server_name or "\\" in server_name:
        return False
    return all(ch.isalnum() or ch in {"_", "-", "."} for ch in server_name)


class FileTokenStorage(TokenStorage):
    """Per-server `TokenStorage` backed by a single owner-only JSON file.

    The on-disk envelope is:

        {"version": 1, "tokens": {...}, "client_info": {...},
         "expires_at": <unix-epoch-float>}

    where `tokens` / `client_info` are the JSON dumps of the SDK's
    `OAuthToken` / `OAuthClientInformationFull` models. `expires_at` is an
    optional sidecar recording the absolute access-token expiry so a cold
    start can report/refresh without decoding the token.
    """

    def __init__(self, server_name: str, *, base_dir: Path | None = None) -> None:
        """Bind this storage to one MCP server identity.

        Args:
            server_name: Configured MCP server name. Used as the filename stem.
            base_dir: Directory to store the token file in. Defaults to
                `default_oauth_dir()`; overridable for tests.

        Raises:
            ValueError: If `server_name` is not filename-safe (would allow the
                token file to escape the storage directory).
        """
        if not is_safe_server_name(server_name):
            msg = (
                f"Invalid MCP server name {server_name!r}: token-storage names "
                "must contain only letters, digits, '_', '-', '.' (and not be "
                "'.' or '..') so the on-disk path stays inside the MCP OAuth "
                "directory."
            )
            raise ValueError(msg)
        self._server_name = server_name
        self._base_dir = base_dir

    @property
    def server_name(self) -> str:
        """Configured MCP server name this storage is bound to."""
        return self._server_name

    @property
    def path(self) -> Path:
        """On-disk token file path for this server."""
        base = self._base_dir if self._base_dir is not None else default_oauth_dir()
        return base / f"{self._server_name}.json"

    async def get_tokens(self) -> OAuthToken | None:
        """Return the stored `OAuthToken`, or `None` if none is persisted."""
        data = self._read()
        raw = None if data is None else data.get("tokens")
        if raw is None:
            return None
        try:
            return OAuthToken.model_validate(raw)
        except ValidationError:
            logger.warning(
                "MCP token file for %s has an unparseable 'tokens' payload; "
                "treating as logged out. Log in again to re-authenticate.",
                self._server_name,
            )
            return None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        """Persist `tokens`, preserving any stored client info.

        Records the absolute Unix-epoch expiry as a sidecar when the token
        advertises `expires_in`, and clears a stale sidecar otherwise.
        """
        import time

        data = self._read() or {}
        data["tokens"] = tokens.model_dump(mode="json")
        self._set_expiry(data, tokens, now=time.time())
        self._write(data)

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        """Return the stored client registration, or `None` if unavailable."""
        data = self._read()
        raw = None if data is None else data.get("client_info")
        if raw is None:
            return None
        try:
            return OAuthClientInformationFull.model_validate(raw)
        except ValidationError:
            logger.warning(
                "MCP token file for %s has an unparseable 'client_info' "
                "payload; treating as unregistered. Log in again to "
                "re-register the OAuth client.",
                self._server_name,
            )
            return None

    async def set_client_info(
        self,
        client_info: OAuthClientInformationFull,
    ) -> None:
        """Persist `client_info`, preserving any stored tokens."""
        data = self._read() or {}
        data["client_info"] = client_info.model_dump(mode="json")
        self._write(data)

    def stored_token_present(self) -> bool:
        """Return whether a token payload is persisted (synchronous).

        A convenience for status/UX code that must not decode the token.
        Fail-closed: an unreadable or malformed file reports `False`.

        Returns:
            `True` when the envelope holds a `tokens` payload.
        """
        data = self._read()
        return data is not None and data.get("tokens") is not None

    def stored_expires_at(self) -> float | None:
        """Return the persisted absolute token expiry (Unix epoch), or `None`.

        Returns `None` when no token file exists, when it is unreadable
        (fail-closed), when the token had no advertised expiry, or when the
        sidecar value is non-numeric.

        Returns:
            The absolute expiry timestamp, or `None` when unknown.
        """
        data = self._read()
        raw = None if data is None else data.get("expires_at")
        if raw is None:
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            # Log the value's type, never the value — the sidecar shares the
            # envelope with the bearer token.
            logger.warning(
                "MCP token sidecar 'expires_at' for %s is not numeric (%s); "
                "treating expiry as unknown.",
                self._server_name,
                type(raw).__name__,
            )
            return None

    def delete(self) -> bool:
        """Delete the stored token file (secure logout).

        Returns:
            `True` if a file was removed, `False` if there was nothing to
                remove or the unlink failed (logged).
        """
        try:
            self.path.unlink()
        except FileNotFoundError:
            return False
        except OSError as exc:
            logger.warning(
                "Could not delete MCP token file for %s: %s",
                self._server_name,
                exc,
            )
            return False
        return True

    @staticmethod
    def _set_expiry(
        data: dict[str, Any],
        tokens: OAuthToken,
        *,
        now: float,
    ) -> None:
        """Set or clear the `expires_at` sidecar from a token's `expires_in`."""
        if tokens.expires_in is not None:
            data["expires_at"] = now + tokens.expires_in
        else:
            data.pop("expires_at", None)

    def _read(self) -> dict[str, Any] | None:
        """Load and validate the envelope, or `None` (fail-closed).

        Any error — missing file, oversized file, malformed JSON, wrong
        version, non-object payload — returns `None` and logs a warning
        (never the file contents), so a corrupt file reads as "logged out"
        rather than raising into the OAuth flow.
        """
        path = self.path
        try:
            if not path.exists():
                return None
            if path.stat().st_size > _MAX_TOKEN_FILE_BYTES:
                logger.warning(
                    "MCP token file for %s is larger than %d bytes; ignoring it "
                    "as corrupt. Log in again to re-create it.",
                    self._server_name,
                    _MAX_TOKEN_FILE_BYTES,
                )
                return None
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning(
                "Could not read MCP token file for %s (%s); treating as logged out.",
                self._server_name,
                type(exc).__name__,
            )
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning(
                "MCP token file for %s is not valid JSON; treating as logged "
                "out. Log in again to re-create it.",
                self._server_name,
            )
            return None
        if not isinstance(data, dict):
            logger.warning(
                "MCP token file for %s is not a JSON object; treating as logged out.",
                self._server_name,
            )
            return None
        if data.get("version") != _STORAGE_VERSION:
            logger.warning(
                "MCP token file for %s has unsupported version %r (expected "
                "%d); treating as logged out. Log in again to re-create it.",
                self._server_name,
                data.get("version"),
                _STORAGE_VERSION,
            )
            return None
        return data

    def _write(self, data: dict[str, Any]) -> None:
        """Atomically write the envelope with owner-only permissions."""
        data["version"] = _STORAGE_VERSION
        path = self.path
        payload = json.dumps(data, separators=(",", ":"))
        atomic_write_text(path, payload, mode=0o600)
        # POSIX 0600 is already applied by atomic_write_text's mode=; this call
        # is the cross-platform guarantee (Windows icacls), where mode= is a
        # no-op. Warn-only inside; a failure leaves the file at the default ACL.
        _secure_owner_only(path)
