"""OAuth 2.0 authentication support for remote MCP servers.

Feature #31: OAuth for MCP servers — enables authenticated connections
to remote MCP servers using OAuth 2.0 flows.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class OAuthConfig:
    """OAuth 2.0 configuration for an MCP server."""

    server_name: str
    """MCP server identifier."""

    client_id: str
    """OAuth client ID."""

    client_secret: str = ""
    """OAuth client secret (optional for PKCE flows)."""

    authorization_url: str = ""
    """Authorization endpoint URL."""

    token_url: str = ""
    """Token endpoint URL."""

    scopes: list[str] = field(default_factory=list)
    """Requested OAuth scopes."""

    redirect_uri: str = "http://localhost:8085/callback"
    """Redirect URI for the OAuth flow."""

    use_pkce: bool = True
    """Whether to use PKCE (Proof Key for Code Exchange)."""


@dataclass
class OAuthToken:
    """Stored OAuth token."""

    access_token: str
    """The access token."""

    refresh_token: str = ""
    """Optional refresh token."""

    token_type: str = "Bearer"
    """Token type."""

    expires_at: float = 0.0
    """Expiration timestamp."""

    scopes: list[str] = field(default_factory=list)
    """Granted scopes."""

    @property
    def expired(self) -> bool:
        """Whether the token has expired."""
        return self.expires_at > 0 and time.time() >= self.expires_at


def _get_token_store_path(config_dir: Path) -> Path:
    """Get the path for storing OAuth tokens.

    Args:
        config_dir: Base config directory.

    Returns:
        Path to token store file.
    """
    tokens_dir = config_dir / "oauth"
    tokens_dir.mkdir(parents=True, exist_ok=True)
    return tokens_dir / "tokens.json"


def load_oauth_configs(config_dir: Path) -> dict[str, OAuthConfig]:
    """Load OAuth configurations for MCP servers.

    Args:
        config_dir: Base config directory.

    Returns:
        Dict mapping server names to OAuth configs.
    """
    oauth_file = config_dir / "oauth" / "config.json"
    if not oauth_file.exists():
        return {}

    try:
        data = json.loads(oauth_file.read_text())
        configs = {}
        for name, cfg in data.items():
            configs[name] = OAuthConfig(
                server_name=name,
                client_id=cfg.get("client_id", ""),
                client_secret=cfg.get("client_secret", ""),
                authorization_url=cfg.get("authorization_url", ""),
                token_url=cfg.get("token_url", ""),
                scopes=cfg.get("scopes", []),
                redirect_uri=cfg.get("redirect_uri", "http://localhost:8085/callback"),
                use_pkce=cfg.get("use_pkce", True),
            )
        return configs
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to load OAuth configs: %s", e)
        return {}


def load_stored_token(config_dir: Path, server_name: str) -> OAuthToken | None:
    """Load a stored OAuth token for a server.

    Args:
        config_dir: Base config directory.
        server_name: MCP server name.

    Returns:
        Stored token if available and not expired, None otherwise.
    """
    token_path = _get_token_store_path(config_dir)
    if not token_path.exists():
        return None

    try:
        data = json.loads(token_path.read_text())
        token_data = data.get(server_name)
        if not token_data:
            return None

        token = OAuthToken(
            access_token=token_data.get("access_token", ""),
            refresh_token=token_data.get("refresh_token", ""),
            token_type=token_data.get("token_type", "Bearer"),
            expires_at=token_data.get("expires_at", 0),
            scopes=token_data.get("scopes", []),
        )

        if token.expired and not token.refresh_token:
            return None

        return token
    except (json.JSONDecodeError, OSError):
        return None


def save_token(config_dir: Path, server_name: str, token: OAuthToken) -> None:
    """Save an OAuth token.

    Args:
        config_dir: Base config directory.
        server_name: MCP server name.
        token: Token to save.
    """
    token_path = _get_token_store_path(config_dir)
    existing: dict[str, Any] = {}

    if token_path.exists():
        try:
            existing = json.loads(token_path.read_text())
        except (json.JSONDecodeError, OSError):
            pass

    existing[server_name] = {
        "access_token": token.access_token,
        "refresh_token": token.refresh_token,
        "token_type": token.token_type,
        "expires_at": token.expires_at,
        "scopes": token.scopes,
    }

    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(json.dumps(existing, indent=2))
    # Restrict file permissions
    token_path.chmod(0o600)


def generate_pkce_pair() -> tuple[str, str]:
    """Generate a PKCE code verifier and challenge pair.

    Returns:
        Tuple of (code_verifier, code_challenge).
    """
    code_verifier = secrets.token_urlsafe(64)
    code_challenge = hashlib.sha256(code_verifier.encode()).digest()
    # Base64url encode without padding
    import base64

    code_challenge_b64 = base64.urlsafe_b64encode(code_challenge).rstrip(b"=").decode()
    return code_verifier, code_challenge_b64


def build_authorization_url(config: OAuthConfig) -> tuple[str, str, str]:
    """Build the OAuth authorization URL.

    Args:
        config: OAuth configuration.

    Returns:
        Tuple of (authorization_url, state, code_verifier).
    """
    state = secrets.token_urlsafe(32)
    code_verifier, code_challenge = generate_pkce_pair()

    params = {
        "client_id": config.client_id,
        "redirect_uri": config.redirect_uri,
        "response_type": "code",
        "state": state,
        "scope": " ".join(config.scopes),
    }

    if config.use_pkce:
        params["code_challenge"] = code_challenge
        params["code_challenge_method"] = "S256"

    url = f"{config.authorization_url}?{urllib.parse.urlencode(params)}"
    return url, state, code_verifier


async def exchange_code_for_token(
    config: OAuthConfig,
    code: str,
    code_verifier: str = "",
) -> OAuthToken:
    """Exchange an authorization code for an access token.

    Args:
        config: OAuth configuration.
        code: Authorization code from the callback.
        code_verifier: PKCE code verifier.

    Returns:
        OAuthToken with the access token.

    Raises:
        ValueError: If token exchange fails.
    """
    import urllib.request

    data = {
        "grant_type": "authorization_code",
        "client_id": config.client_id,
        "code": code,
        "redirect_uri": config.redirect_uri,
    }

    if config.client_secret:
        data["client_secret"] = config.client_secret
    if code_verifier and config.use_pkce:
        data["code_verifier"] = code_verifier

    encoded = urllib.parse.urlencode(data).encode()

    req = urllib.request.Request(
        config.token_url,
        data=encoded,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )

    try:
        response = await asyncio.to_thread(urllib.request.urlopen, req, timeout=30)
        result = json.loads(response.read())

        expires_in = result.get("expires_in", 0)
        expires_at = time.time() + expires_in if expires_in else 0

        return OAuthToken(
            access_token=result["access_token"],
            refresh_token=result.get("refresh_token", ""),
            token_type=result.get("token_type", "Bearer"),
            expires_at=expires_at,
            scopes=result.get("scope", "").split(),
        )
    except Exception as e:
        msg = f"Token exchange failed: {e}"
        raise ValueError(msg) from e
