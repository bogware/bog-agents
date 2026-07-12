"""Tests for `bog_agents_cli.mcp_loopback`.

Drives the loopback callback server with a real localhost GET (no external
network), and checks single-use behavior, timeout, error callbacks, and the
headless paste-back parser.
"""

from __future__ import annotations

import urllib.error
import urllib.request

import pytest

from bog_agents_cli.mcp_loopback import (
    LoopbackCallbackError,
    LoopbackCallbackServer,
    LoopbackTimeoutError,
    parse_callback_url,
)


@pytest.fixture(autouse=True)
def _allow_real_sockets():
    """Permit real sockets for these tests.

    The loopback callback server binds a real `127.0.0.1` (inet) socket, which
    CI's Linux `pytest --disable-socket --allow-unix-socket` step blocks. This
    re-enables sockets per test. It is a no-op on the Windows CI step (which runs
    `-p no:socket`, so nothing is blocking in the first place).
    """
    try:
        import pytest_socket
    except ImportError:
        yield
        return
    pytest_socket.enable_socket()
    yield


def _get(url: str) -> tuple[int, str]:
    """Perform a blocking localhost GET, returning (status, body).

    Targets 127.0.0.1 explicitly (the server binds IPv4 only) to avoid a slow
    IPv6 connection-refused retry, and treats an HTTP error status as a normal
    response rather than an exception.
    """
    url = url.replace("http://localhost:", "http://127.0.0.1:")
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:  # localhost only
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


async def test_captures_code_and_state(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """A well-formed callback GET yields the code and state to `wait_for_code`."""
    del tmp_path_factory
    server = LoopbackCallbackServer()
    try:
        # 127.0.0.1 literal (not `localhost`) to avoid IPv6-first resolution on
        # dual-stack hosts (RFC 8252 s7.3).
        assert server.redirect_uri == f"http://127.0.0.1:{server.port}/callback"
        status, body = _get(f"{server.redirect_uri}?code=abc123&state=xyz")
        assert status == 200
        assert "signed in" in body.lower()
        code, state = await server.wait_for_code(timeout=5)
        assert code == "abc123"
        assert state == "xyz"
    finally:
        server.close()


async def test_single_use_ignores_second_callback() -> None:
    """After the first capture, a second GET does not overwrite the result."""
    server = LoopbackCallbackServer()
    try:
        _get(f"{server.redirect_uri}?code=first&state=s1")
        code, _ = await server.wait_for_code(timeout=5)
        assert code == "first"
        # A duplicate request still gets a page but the captured code is stable.
        status, _ = _get(f"{server.redirect_uri}?code=second&state=s2")
        assert status == 200
        code_again, _ = await server.wait_for_code(timeout=5)
        assert code_again == "first"
    finally:
        server.close()


async def test_timeout_when_no_callback() -> None:
    """`wait_for_code` raises `LoopbackTimeoutError` if nothing arrives."""
    server = LoopbackCallbackServer()
    try:
        with pytest.raises(LoopbackTimeoutError):
            await server.wait_for_code(timeout=0.2)
    finally:
        server.close()


async def test_provider_error_callback_raises() -> None:
    """An `error=` callback surfaces as `LoopbackCallbackError`."""
    server = LoopbackCallbackServer()
    try:
        status, _ = _get(
            f"{server.redirect_uri}?error=access_denied&error_description=nope"
        )
        assert status == 400
        with pytest.raises(LoopbackCallbackError, match="access_denied"):
            await server.wait_for_code(timeout=5)
    finally:
        server.close()


async def test_missing_code_callback_raises() -> None:
    """A callback without a `code` param surfaces as `LoopbackCallbackError`."""
    server = LoopbackCallbackServer()
    try:
        status, _ = _get(f"{server.redirect_uri}?state=only")
        assert status == 400
        with pytest.raises(LoopbackCallbackError, match="missing the 'code'"):
            await server.wait_for_code(timeout=5)
    finally:
        server.close()


def test_context_manager_closes() -> None:
    """The context manager binds a port and closes cleanly."""
    with LoopbackCallbackServer() as server:
        assert server.port > 0
    # After exit the server is closed; closing again is a no-op.
    server.close()


def test_parse_callback_url_ok() -> None:
    """The paste-back parser extracts code and state from a full URL."""
    code, state = parse_callback_url("http://localhost:9/callback?code=C&state=S")
    assert code == "C"
    assert state == "S"


def test_parse_callback_url_error() -> None:
    """The paste-back parser rejects an error redirect."""
    with pytest.raises(LoopbackCallbackError, match="denied"):
        parse_callback_url("http://localhost:9/callback?error=bad")


def test_parse_callback_url_missing_code() -> None:
    """The paste-back parser rejects a URL with no code."""
    with pytest.raises(LoopbackCallbackError, match="missing the 'code'"):
        parse_callback_url("http://localhost:9/callback?state=S")
