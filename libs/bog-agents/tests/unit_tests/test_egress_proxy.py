"""Tests for the allowlist egress proxy (#22).

The pure decision helpers (`host_allowed`, `parse_connect_target`,
`egress_env_for`) are exercised without any sockets. Two loopback integration
tests bind a real 127.0.0.1 socket to prove CONNECT is tunnelled for an
allowlisted host and refused with 403 otherwise; those re-enable sockets the
same way the MCP loopback tests do (CI runs `--disable-socket`).
"""

from __future__ import annotations

import socket
import threading

import pytest

from bog_agents.sandbox.egress_proxy import (
    AllowlistEgressProxy,
    egress_env_for,
    host_allowed,
    parse_connect_target,
)


class TestHostAllowed:
    def test_exact_match(self) -> None:
        assert host_allowed("github.com", ["github.com"]) is True

    def test_subdomain_allowed(self) -> None:
        assert host_allowed("api.github.com", ["github.com"]) is True

    def test_lookalike_denied(self) -> None:
        # Suffix must be on a label boundary — "notgithub.com" is not allowed.
        assert host_allowed("notgithub.com", ["github.com"]) is False

    def test_empty_allowlist_denies_all(self) -> None:
        assert host_allowed("github.com", []) is False

    def test_case_insensitive(self) -> None:
        assert host_allowed("API.GitHub.CoM", ["github.com"]) is True

    def test_trailing_dot_normalized(self) -> None:
        assert host_allowed("github.com.", ["github.com"]) is True

    def test_empty_entries_skipped(self) -> None:
        assert host_allowed("github.com", ["", "  ", "github.com"]) is True


class TestParseConnectTarget:
    def test_host_and_port(self) -> None:
        assert parse_connect_target("CONNECT github.com:443 HTTP/1.1") == ("github.com", 443)

    def test_default_port_443(self) -> None:
        assert parse_connect_target("CONNECT github.com HTTP/1.1") == ("github.com", 443)

    def test_ipv6_literal(self) -> None:
        assert parse_connect_target("CONNECT [::1]:8443 HTTP/1.1") == ("::1", 8443)

    def test_non_connect_method(self) -> None:
        assert parse_connect_target("GET http://x/ HTTP/1.1") is None

    def test_empty_line(self) -> None:
        assert parse_connect_target("") is None

    def test_bad_port(self) -> None:
        assert parse_connect_target("CONNECT github.com:notaport HTTP/1.1") is None


class TestEgressEnv:
    def test_sets_both_cases_and_no_proxy(self) -> None:
        env = egress_env_for("http://127.0.0.1:8888")
        assert env["HTTP_PROXY"] == "http://127.0.0.1:8888"
        assert env["HTTPS_PROXY"] == "http://127.0.0.1:8888"
        assert env["http_proxy"] == "http://127.0.0.1:8888"
        assert env["https_proxy"] == "http://127.0.0.1:8888"
        assert "127.0.0.1" in env["NO_PROXY"]
        assert "localhost" in env["no_proxy"]


@pytest.fixture(autouse=True)
def _allow_real_sockets():
    """Permit real sockets — the loopback tests bind a real 127.0.0.1 socket.

    Mirrors the MCP loopback tests: CI's Linux step runs `pytest
    --disable-socket --allow-unix-socket`, which blocks inet sockets; this
    re-enables them per test and is a no-op where nothing is blocking.
    """
    try:
        import pytest_socket
    except ImportError:
        yield
        return
    pytest_socket.enable_socket()
    yield


def _connect(address: tuple[str, int]) -> socket.socket:
    return socket.create_connection(address, timeout=5)


class TestProxyLoopback:
    def test_denied_host_gets_403(self) -> None:
        proxy = AllowlistEgressProxy(["github.com"])
        proxy.start()
        try:
            client = _connect(proxy.address)
            client.sendall(b"CONNECT evil.example:443 HTTP/1.1\r\nHost: evil.example\r\n\r\n")
            resp = client.recv(1024)
            client.close()
            assert b"403" in resp
            assert proxy.stats.denied == 1
        finally:
            proxy.stop()

    def test_non_connect_gets_405(self) -> None:
        proxy = AllowlistEgressProxy(["github.com"])
        proxy.start()
        try:
            client = _connect(proxy.address)
            client.sendall(b"GET http://github.com/ HTTP/1.1\r\nHost: github.com\r\n\r\n")
            resp = client.recv(1024)
            client.close()
            assert b"405" in resp
        finally:
            proxy.stop()

    def test_allowed_host_tunnels(self) -> None:
        # Stand up a tiny loopback echo server and allowlist 127.0.0.1, then
        # verify CONNECT establishes a tunnel that relays bytes end to end.
        echo = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        echo.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        echo.bind(("127.0.0.1", 0))
        echo.listen(1)
        echo_host, echo_port = echo.getsockname()

        def _echo_once() -> None:
            conn, _ = echo.accept()
            data = conn.recv(64)
            conn.sendall(data)
            conn.close()

        threading.Thread(target=_echo_once, daemon=True).start()

        proxy = AllowlistEgressProxy(["127.0.0.1"])
        proxy.start()
        try:
            client = _connect(proxy.address)
            client.sendall(
                f"CONNECT {echo_host}:{echo_port} HTTP/1.1\r\n\r\n".encode("latin-1")
            )
            established = client.recv(1024)
            assert b"200" in established
            client.sendall(b"ping")
            assert client.recv(16) == b"ping"
            client.close()
            assert proxy.stats.allowed == 1
        finally:
            proxy.stop()
            echo.close()
