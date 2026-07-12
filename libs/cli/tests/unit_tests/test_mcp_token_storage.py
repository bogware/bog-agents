"""Tests for `bog_agents_cli.mcp_token_storage.FileTokenStorage`.

Covers the versioned round-trip, owner-only permissions, fail-closed reads on
corrupt/oversized/wrong-version files, and rejection of traversal server names.
No token value is ever logged or returned in cleartext outside the model.
"""

from __future__ import annotations

import json
import os
import stat
import time
from pathlib import Path

import pytest
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from bog_agents_cli.mcp_token_storage import (
    _MAX_TOKEN_FILE_BYTES,
    _STORAGE_VERSION,
    FileTokenStorage,
    is_safe_server_name,
)


def _make_token() -> OAuthToken:
    return OAuthToken(
        access_token="access-secret-xyz",  # test fixture, not a real credential
        token_type="Bearer",
        expires_in=3600,
        refresh_token="refresh-secret-xyz",  # test fixture, not a real credential
    )


def _make_client_info() -> OAuthClientInformationFull:
    return OAuthClientInformationFull(
        client_id="client-123",
        redirect_uris=["http://localhost:12345/callback"],  # ty: ignore[invalid-argument-type]
        token_endpoint_auth_method="none",
    )


async def test_round_trip_tokens_and_client_info(tmp_path: Path) -> None:
    """Tokens and client info persist and reconstruct through the envelope."""
    storage = FileTokenStorage("srv", base_dir=tmp_path)
    token = _make_token()
    client_info = _make_client_info()

    await storage.set_client_info(client_info)
    await storage.set_tokens(token)

    loaded_token = await storage.get_tokens()
    loaded_info = await storage.get_client_info()
    assert loaded_token is not None
    assert loaded_token.access_token == token.access_token
    assert loaded_token.refresh_token == token.refresh_token
    assert loaded_info is not None
    assert loaded_info.client_id == "client-123"

    # Setting tokens must not clobber the previously stored client info.
    envelope = json.loads(storage.path.read_text(encoding="utf-8"))
    assert envelope["version"] == _STORAGE_VERSION
    assert envelope["tokens"]["access_token"] == token.access_token
    assert envelope["client_info"]["client_id"] == "client-123"


async def test_expires_at_sidecar_and_present_helpers(tmp_path: Path) -> None:
    """`set_tokens` records an absolute expiry; sync helpers read it back."""
    storage = FileTokenStorage("srv", base_dir=tmp_path)
    before = time.time()
    await storage.set_tokens(_make_token())

    assert storage.stored_token_present() is True
    expires_at = storage.stored_expires_at()
    assert expires_at is not None
    # 3600s expiry, allow generous slack for slow CI.
    assert before + 3000 < expires_at < before + 4200


async def test_expires_at_cleared_when_no_expiry(tmp_path: Path) -> None:
    """A token without `expires_in` clears any stale sidecar."""
    storage = FileTokenStorage("srv", base_dir=tmp_path)
    await storage.set_tokens(_make_token())
    assert storage.stored_expires_at() is not None

    no_expiry = OAuthToken(access_token="a2", token_type="Bearer")  # test fixture
    await storage.set_tokens(no_expiry)
    assert storage.stored_expires_at() is None


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits not meaningful on Windows")
async def test_token_file_is_owner_only(tmp_path: Path) -> None:
    """The persisted token file is mode 0600 on POSIX."""
    storage = FileTokenStorage("srv", base_dir=tmp_path)
    await storage.set_tokens(_make_token())
    mode = stat.S_IMODE(storage.path.stat().st_mode)
    assert mode == 0o600


async def test_corrupt_json_fails_closed(tmp_path: Path) -> None:
    """A malformed file reads as logged out rather than raising."""
    storage = FileTokenStorage("srv", base_dir=tmp_path)
    storage.path.parent.mkdir(parents=True, exist_ok=True)
    storage.path.write_text("{ not json", encoding="utf-8")

    assert await storage.get_tokens() is None
    assert await storage.get_client_info() is None
    assert storage.stored_token_present() is False
    assert storage.stored_expires_at() is None


async def test_wrong_version_fails_closed(tmp_path: Path) -> None:
    """A file stamped with an unsupported version reads as logged out."""
    storage = FileTokenStorage("srv", base_dir=tmp_path)
    storage.path.parent.mkdir(parents=True, exist_ok=True)
    storage.path.write_text(
        json.dumps({"version": 999, "tokens": {"access_token": "x"}}),
        encoding="utf-8",
    )
    assert await storage.get_tokens() is None
    assert storage.stored_token_present() is False


async def test_oversized_file_fails_closed(tmp_path: Path) -> None:
    """A file larger than the cap is ignored without parsing."""
    storage = FileTokenStorage("srv", base_dir=tmp_path)
    storage.path.parent.mkdir(parents=True, exist_ok=True)
    padding = "x" * (_MAX_TOKEN_FILE_BYTES + 10)
    storage.path.write_text(
        json.dumps({"version": _STORAGE_VERSION, "pad": padding}),
        encoding="utf-8",
    )
    assert await storage.get_tokens() is None


async def test_non_object_payload_fails_closed(tmp_path: Path) -> None:
    """A valid-JSON-but-non-object payload reads as logged out."""
    storage = FileTokenStorage("srv", base_dir=tmp_path)
    storage.path.parent.mkdir(parents=True, exist_ok=True)
    storage.path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    assert await storage.get_tokens() is None


@pytest.mark.parametrize(
    "bad_name",
    [
        "../evil",
        "..",
        ".",
        "a/b",
        "a\\b",
        "",
        "spaces here",
        "semi;colon",
    ],
)
def test_rejects_unsafe_server_names(bad_name: str, tmp_path: Path) -> None:
    """Names that could escape the storage dir are rejected at construction."""
    assert is_safe_server_name(bad_name) is False
    with pytest.raises(ValueError, match="Invalid MCP server name"):
        FileTokenStorage(bad_name, base_dir=tmp_path)


@pytest.mark.parametrize("good_name", ["github", "my-server_1", "a.b.c", "X"])
def test_accepts_safe_server_names(good_name: str, tmp_path: Path) -> None:
    """Filename-safe names are accepted and stay inside the storage dir."""
    assert is_safe_server_name(good_name) is True
    storage = FileTokenStorage(good_name, base_dir=tmp_path)
    assert storage.path.parent == tmp_path
    assert storage.path.name == f"{good_name}.json"


async def test_delete_removes_file(tmp_path: Path) -> None:
    """`delete` removes the token file and reports removal state."""
    storage = FileTokenStorage("srv", base_dir=tmp_path)
    await storage.set_tokens(_make_token())
    assert storage.path.exists()
    assert storage.delete() is True
    assert not storage.path.exists()
    # Second delete is a no-op that reports False.
    assert storage.delete() is False


async def test_corrupt_file_does_not_log_token_value(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A fail-closed warning never contains the token material on disk."""
    storage = FileTokenStorage("srv", base_dir=tmp_path)
    storage.path.parent.mkdir(parents=True, exist_ok=True)
    # A token-shaped but wrong-version file: the warning must not echo it.
    storage.path.write_text(
        json.dumps({"version": 2, "tokens": {"access_token": "LEAK-TOKEN-1"}}),
        encoding="utf-8",
    )
    with caplog.at_level("WARNING"):
        assert await storage.get_tokens() is None
    assert "LEAK-TOKEN-1" not in caplog.text
