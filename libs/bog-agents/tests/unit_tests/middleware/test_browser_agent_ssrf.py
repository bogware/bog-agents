"""SSRF gate regression tests for :class:`BrowserAgentMiddleware`.

Guards P0-C in REVIEW.md. The previous implementation passed every URL
through ``urllib.request.urlopen`` with no scheme/host allowlist, letting
a steered LLM read ``file:///home/user/.aws/credentials`` or fetch
``http://169.254.169.254/`` (cloud metadata IMDS) and exfiltrate via a
second tool call. The new gate blocks all non-http(s) schemes plus
loopback, RFC1918, link-local, and ULA — link-local is blocked
unconditionally even when the caller opts into private IPs.
"""

from __future__ import annotations

from typing import ClassVar, Self
from unittest.mock import patch

import pytest

from bog_agents.middleware.browser_agent import (
    BrowserAgentMiddleware,
    _is_url_safe,
)

# ---------------------------------------------------------------------------
# Direct gate tests
# ---------------------------------------------------------------------------


class TestUrlSafeGate:
    """Unit tests for the ``_is_url_safe`` predicate."""

    @pytest.mark.parametrize(
        "url",
        [
            "https://example.com/",
            "https://example.com/path?q=1",
            "http://api.openai.com/",
            "https://1.1.1.1/",  # public IP literal
            "https://[2606:4700:4700::1111]/",  # Cloudflare DNS IPv6
        ],
    )
    def test_public_urls_pass(self, url: str) -> None:
        safe, reason = _is_url_safe(url)
        assert safe, f"expected safe URL {url!r} to pass: {reason}"

    @pytest.mark.parametrize(
        "url",
        [
            "file:///etc/passwd",
            "file:///C:/Windows/System32/drivers/etc/hosts",
            "ftp://example.com/",
            "data:text/plain;base64,SGVsbG8=",
            "gopher://example.com/",
            "javascript:alert(1)",
        ],
    )
    def test_non_http_schemes_blocked(self, url: str) -> None:
        safe, reason = _is_url_safe(url)
        assert not safe, f"expected {url!r} to be blocked"
        assert "scheme" in reason.lower() or "refusing" in reason.lower()

    @pytest.mark.parametrize(
        "url",
        [
            "http://169.254.169.254/latest/meta-data/",  # AWS IMDS
            "http://169.254.169.254/computeMetadata/v1/",  # GCP IMDS
            "http://169.254.169.254/metadata/instance",  # Azure IMDS
            "http://[fe80::1]/",  # IPv6 link-local
        ],
    )
    def test_link_local_blocked_always(self, url: str) -> None:
        # Even with allow_private_ips=True the cloud-metadata IPs must stay blocked.
        for allow in (False, True):
            safe, reason = _is_url_safe(url, allow_private_ips=allow)
            assert not safe, f"link-local URL {url!r} must be blocked (allow_private_ips={allow})"
            assert "link-local" in reason.lower() or "metadata" in reason.lower()

    @pytest.mark.parametrize(
        "url",
        [
            "http://127.0.0.1/",
            "http://localhost/",  # resolves to 127.0.0.1 or ::1
            "http://10.0.0.1/",  # RFC1918
            "http://192.168.1.1/",  # RFC1918
            "http://172.16.0.1/",  # RFC1918
            "http://[::1]/",  # IPv6 loopback
            "http://[fc00::1]/",  # IPv6 ULA
        ],
    )
    def test_private_blocked_by_default(self, url: str) -> None:
        safe, reason = _is_url_safe(url)
        assert not safe, f"private URL {url!r} should be blocked by default"
        assert "loopback" in reason.lower() or "private" in reason.lower() or "DNS" in reason

    @pytest.mark.parametrize(
        "url",
        [
            "http://127.0.0.1:8000/",
            "http://10.0.0.5:5432/",
            "http://192.168.1.50/",
        ],
    )
    def test_private_allowed_when_opted_in(self, url: str) -> None:
        safe, _reason = _is_url_safe(url, allow_private_ips=True)
        assert safe, f"private URL {url!r} should pass with allow_private_ips=True"

    def test_no_host_blocked(self) -> None:
        safe, reason = _is_url_safe("http://")
        assert not safe
        assert "host" in reason.lower() or "resolve" in reason.lower()

    def test_unresolvable_hostname_blocked(self) -> None:
        safe, reason = _is_url_safe("https://this-host-does-not-exist-zzzz-test-12345.invalid/")
        assert not safe
        assert "DNS" in reason or "resolve" in reason.lower()


# ---------------------------------------------------------------------------
# Integration through web_fetch / api_request tools
# ---------------------------------------------------------------------------


class TestMiddlewareIntegration:
    """Invoke the underlying tool callable directly to confirm the gate
    short-circuits before ``urlopen`` is ever called. We bypass the
    ``StructuredTool`` wrapper because it requires a real ``ToolRuntime``
    that's a chore to construct for this test's purpose.
    """

    def _get_callable(self, mw: BrowserAgentMiddleware, name: str):
        for tool in mw.tools:
            if tool.name == name:
                return tool.func
        raise AssertionError(f"tool {name!r} not found")

    def test_web_fetch_blocks_file_scheme(self, tmp_path) -> None:
        secret = tmp_path / "secret.txt"
        secret.write_text("DO NOT LEAK", encoding="utf-8")
        mw = BrowserAgentMiddleware()
        fn = self._get_callable(mw, "web_fetch")

        with patch("urllib.request.urlopen") as fake:
            result = fn(None, url=secret.as_uri())  # type: ignore[arg-type]
        assert "Error" in result and "scheme" in result.lower()
        fake.assert_not_called()

    def test_web_fetch_blocks_imds(self) -> None:
        mw = BrowserAgentMiddleware()
        fn = self._get_callable(mw, "web_fetch")
        with patch("urllib.request.urlopen") as fake:
            result = fn(None, url="http://169.254.169.254/latest/meta-data/")  # type: ignore[arg-type]
        assert "Error" in result
        assert "link-local" in result.lower() or "metadata" in result.lower()
        fake.assert_not_called()

    def test_web_fetch_blocks_loopback_by_default(self) -> None:
        mw = BrowserAgentMiddleware()
        fn = self._get_callable(mw, "web_fetch")
        with patch("urllib.request.urlopen") as fake:
            result = fn(None, url="http://127.0.0.1:8000/")  # type: ignore[arg-type]
        assert "Error" in result
        fake.assert_not_called()

    def test_web_fetch_allows_loopback_when_opted_in(self) -> None:
        mw = BrowserAgentMiddleware(allow_private_ips=True)
        fn = self._get_callable(mw, "web_fetch")

        class _FakeResponse:
            status: ClassVar[int] = 200
            headers: ClassVar[dict[str, str]] = {"Content-Type": "text/plain"}

            def __enter__(self) -> Self:
                return self

            def __exit__(self, *args: object) -> bool:
                return False

            def read(self) -> bytes:
                return b"hello"

        with patch("urllib.request.urlopen", return_value=_FakeResponse()):
            result = fn(None, url="http://127.0.0.1:8000/health")  # type: ignore[arg-type]
        assert "Error" not in result
        assert "hello" in result

    def test_api_request_blocks_imds(self) -> None:
        mw = BrowserAgentMiddleware()
        fn = self._get_callable(mw, "api_request")
        with patch("urllib.request.urlopen") as fake:
            result = fn(
                None,  # type: ignore[arg-type]
                url="http://169.254.169.254/latest/meta-data/iam/security-credentials/",
            )
        assert "Error" in result
        fake.assert_not_called()
