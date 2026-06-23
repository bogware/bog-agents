"""SSRF hardening tests for the shared fetch guard (P4 / P5).

Covers the SSRF guard that is applied to:

* ``/web`` (:func:`bog_agents_cli.web_fetch.fetch_url`)
* the agent ``fetch_url`` tool (:func:`bog_agents_cli.tools.fetch_url`)
* the agent ``http_request`` tool (:func:`bog_agents_cli.tools.http_request`)

The guard is intentionally separate from the unicode/confusable gate
(``check_url_safety``) — see ``test_unicode_security`` which still asserts
IP literals are "safe" for *that* gate. Here we prove the SSRF gate blocks
metadata / loopback / private targets and public→private redirects, while
allowing normal public https.
"""

from __future__ import annotations

import ipaddress

import pytest
import responses

from bog_agents_cli import tools, web_fetch
from bog_agents_cli.tools import fetch_url as tool_fetch_url, http_request
from bog_agents_cli.web_fetch import SsrfError, WebFetchError, assert_fetch_allowed

_PUBLIC_IP = "93.184.216.34"  # example.com


def _fake_dns(monkeypatch, mapping: dict[str, str]) -> None:
    """Stub host→IP resolution for the SSRF guard.

    Args:
        monkeypatch: pytest fixture.
        mapping: hostname → IP-literal string. Hosts that are already IP
            literals resolve to themselves; unknown hosts raise.
    """

    def _resolve(host: str):
        if host in mapping:
            return [ipaddress.ip_address(mapping[host])]
        try:
            return [ipaddress.ip_address(host)]
        except ValueError as exc:  # pragma: no cover - defensive
            msg = f"unexpected host in test: {host!r}"
            raise SsrfError(msg) from exc

    monkeypatch.setattr(web_fetch, "_resolve_host_addresses", _resolve)


# ---------------------------------------------------------------------------
# assert_fetch_allowed — the shared guard
# ---------------------------------------------------------------------------


class TestAssertFetchAllowed:
    def test_metadata_ip_blocked(self):
        # 169.254.169.254 is link-local (cloud metadata endpoint).
        with pytest.raises(SsrfError, match="non-public"):
            assert_fetch_allowed("http://169.254.169.254/latest/meta-data/")

    def test_loopback_ip_blocked(self):
        with pytest.raises(SsrfError, match="non-public"):
            assert_fetch_allowed("http://127.0.0.1:8080/admin")

    def test_ipv6_loopback_blocked(self):
        with pytest.raises(SsrfError, match="non-public"):
            assert_fetch_allowed("http://[::1]/")

    def test_localhost_blocked(self, monkeypatch):
        _fake_dns(monkeypatch, {"localhost": "127.0.0.1"})
        with pytest.raises(SsrfError, match="non-public"):
            assert_fetch_allowed("http://localhost/")

    def test_private_ip_blocked(self):
        for addr in ("http://10.0.0.5/", "http://192.168.1.1/", "http://172.16.0.1/"):
            with pytest.raises(SsrfError, match="non-public"):
                assert_fetch_allowed(addr)

    def test_ipv4_mapped_ipv6_metadata_blocked(self):
        # ::ffff:169.254.169.254 must not tunnel past the IPv4 checks.
        with pytest.raises(SsrfError, match="non-public"):
            assert_fetch_allowed("http://[::ffff:169.254.169.254]/")

    def test_non_http_scheme_blocked(self):
        for url in ("file:///etc/passwd", "ftp://example.com/x", "gopher://x/"):
            with pytest.raises(SsrfError, match="http and https"):
                assert_fetch_allowed(url)

    def test_missing_host_blocked(self):
        with pytest.raises(SsrfError, match="no host"):
            assert_fetch_allowed("http:///path")

    def test_public_host_allowed(self, monkeypatch):
        _fake_dns(monkeypatch, {"example.com": _PUBLIC_IP})
        # Should not raise.
        assert_fetch_allowed("https://example.com/page")

    def test_public_ip_literal_allowed(self):
        # A routable public IP literal is fine.
        assert_fetch_allowed("https://93.184.216.34/")

    def test_unresolvable_host_blocked(self, monkeypatch):
        def _boom(_host: str):
            msg = "could not resolve"
            raise SsrfError(msg)

        monkeypatch.setattr(web_fetch, "_resolve_host_addresses", _boom)
        with pytest.raises(SsrfError):
            assert_fetch_allowed("https://nope.invalid/")


# ---------------------------------------------------------------------------
# /web fetch_url — urllib path
# ---------------------------------------------------------------------------


class TestWebFetchSsrf:
    def test_metadata_blocked_before_network(self):
        # No network stub: if the guard let this through, the test would try
        # to hit the metadata endpoint. It must raise instead.
        with pytest.raises(WebFetchError):
            web_fetch.fetch_url("http://169.254.169.254/latest/meta-data/")

    def test_localhost_blocked(self, monkeypatch):
        _fake_dns(monkeypatch, {"localhost": "127.0.0.1"})
        with pytest.raises(WebFetchError):
            web_fetch.fetch_url("http://localhost:9000/")

    def test_redirect_to_private_blocked(self, monkeypatch):
        """A public host that 302-redirects to a private host is blocked."""
        from unittest.mock import MagicMock

        _fake_dns(
            monkeypatch,
            {"public.example": _PUBLIC_IP, "evil.example": "169.254.169.254"},
        )

        # First (and only) hop returns a 302 whose Location points at a
        # private host; the per-hop guard must reject it before connecting.
        resp = MagicMock()
        resp.status = 302
        resp.headers.get.return_value = "http://evil.example/latest/meta-data/"
        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=resp)
        ctx.__exit__ = MagicMock(return_value=False)

        monkeypatch.setattr(
            web_fetch.urllib.request.OpenerDirector,
            "open",
            lambda _self, _req, timeout=None: ctx,
        )

        with pytest.raises(WebFetchError, match="non-public"):
            web_fetch.fetch_url("http://public.example/")

    def test_normal_https_allowed(self, monkeypatch):
        _fake_dns(monkeypatch, {"example.com": _PUBLIC_IP})

        from unittest.mock import MagicMock

        resp = MagicMock()
        resp.status = 200
        resp.geturl.return_value = "https://example.com/page"
        resp.headers.get.return_value = None
        resp.headers.get_content_type.return_value = "text/html"
        resp.read.return_value = b"<html><body>ok</body></html>"
        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=resp)
        ctx.__exit__ = MagicMock(return_value=False)

        monkeypatch.setattr(
            web_fetch.urllib.request.OpenerDirector,
            "open",
            lambda _self, _req, timeout=None: ctx,
        )
        result = web_fetch.fetch_url("https://example.com/page")
        assert result.status_code == 200
        assert "ok" in result.body


# ---------------------------------------------------------------------------
# Agent tools — requests path
# ---------------------------------------------------------------------------


class TestToolsFetchUrlSsrf:
    def test_metadata_blocked(self):
        result = tool_fetch_url("http://169.254.169.254/latest/meta-data/")
        assert "error" in result
        assert "blocked" in result["error"].lower()

    def test_non_http_scheme_blocked(self):
        result = tool_fetch_url("file:///etc/passwd")
        assert "error" in result
        assert "blocked" in result["error"].lower()

    @responses.activate
    def test_redirect_to_private_blocked(self, monkeypatch):
        _fake_dns(
            monkeypatch,
            {"public.example": _PUBLIC_IP, "evil.example": "127.0.0.1"},
        )
        responses.add(
            responses.GET,
            "http://public.example/",
            status=302,
            headers={"Location": "http://evil.example/secret"},
        )
        result = tool_fetch_url("http://public.example/")
        assert "error" in result
        assert "blocked" in result["error"].lower()

    @responses.activate
    def test_normal_fetch_allowed(self, monkeypatch):
        _fake_dns(monkeypatch, {"public.example": _PUBLIC_IP})
        responses.add(
            responses.GET,
            "http://public.example/",
            body="<html><body><h1>ok</h1></body></html>",
            status=200,
        )
        result = tool_fetch_url("http://public.example/")
        assert result.get("status_code") == 200
        assert "ok" in result["markdown_content"]


class TestHttpRequestSsrf:
    def test_metadata_blocked(self):
        result = http_request("http://169.254.169.254/latest/meta-data/")
        assert result["success"] is False
        assert "blocked" in result["content"].lower()

    def test_loopback_blocked(self, monkeypatch):
        _fake_dns(monkeypatch, {"localhost": "127.0.0.1"})
        result = http_request("http://localhost:8000/")
        assert result["success"] is False
        assert "blocked" in result["content"].lower()

    @responses.activate
    def test_redirect_to_private_blocked(self, monkeypatch):
        _fake_dns(
            monkeypatch,
            {"public.example": _PUBLIC_IP, "evil.example": "10.0.0.1"},
        )
        responses.add(
            responses.GET,
            "http://public.example/",
            status=302,
            headers={"Location": "http://evil.example/"},
        )
        result = http_request("http://public.example/")
        assert result["success"] is False
        assert "blocked" in result["content"].lower()

    @responses.activate
    def test_normal_request_allowed(self, monkeypatch):
        _fake_dns(monkeypatch, {"public.example": _PUBLIC_IP})
        responses.add(
            responses.GET,
            "http://public.example/api",
            json={"ok": True},
            status=200,
        )
        result = http_request("http://public.example/api")
        assert result["success"] is True
        assert result["status_code"] == 200
        assert result["content"] == {"ok": True}


def test_tools_module_imports_guard():
    """The guard helper is reachable from tools (smoke)."""
    assert callable(tools._request_with_guarded_redirects)
