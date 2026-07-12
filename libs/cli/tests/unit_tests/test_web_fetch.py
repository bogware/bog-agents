"""Unit tests for /web URL fetch (Gap 10).

Exercises the URL-validation gate, the HTML→text pipeline, and the
prompt-block rendering. Network access is stubbed via
``monkeypatch.setattr`` on the urllib calls — no real fetches.
"""

from __future__ import annotations

import io
from typing import Any
from unittest.mock import MagicMock

import pytest

from bog_agents_cli.web_fetch import (
    FetchResult,
    WebFetchError,
    _clean_text,
    _decode_body,
    _html_to_text,
    fetch_url,
    render_prompt_block,
)


class TestValidation:
    """URL validation runs before any network call."""

    def test_rejects_file_scheme(self):
        with pytest.raises(WebFetchError, match="http and https"):
            fetch_url("file:///etc/passwd")

    def test_rejects_ftp_scheme(self):
        with pytest.raises(WebFetchError, match="http and https"):
            fetch_url("ftp://example.com/x")

    def test_rejects_empty_host(self):
        with pytest.raises(WebFetchError, match="no host"):
            fetch_url("http://")

    def test_unicode_spoofing_blocked(self):
        # check_url_safety rejects mixed-script lookalike domains; pass
        # a homograph whose host mixes Latin + Cyrillic letters.
        cyrillic_a = "а"
        spoof = f"http://ex{cyrillic_a}mple.com/"
        with pytest.raises(WebFetchError):
            fetch_url(spoof)


class TestHtmlToText:
    """HTML stripping and entity decoding."""

    def test_strips_tags(self):
        out = _html_to_text("<p>hello <b>world</b></p>")
        assert "hello" in out
        assert "world" in out
        assert "<" not in out

    def test_removes_script_and_style(self):
        html_in = (
            "<html><head><style>.a{}</style>"
            "<script>alert(1)</script></head>"
            "<body>visible</body></html>"
        )
        out = _html_to_text(html_in)
        assert "alert" not in out
        assert ".a{}" not in out
        assert "visible" in out

    def test_decodes_entities(self):
        out = _html_to_text("a &amp; b &lt;3 &#39;quote&#39;")
        assert "a & b <3 'quote'" in out


class TestCleanText:
    def test_collapses_whitespace(self):
        out = _clean_text("hello    world\n\n\n\n  next")
        assert "hello world" in out
        assert "\n\n\n" not in out


class TestDecodeBody:
    def test_honors_charset_header(self):
        raw = "café".encode("latin-1")
        out = _decode_body(raw, "text/plain; charset=latin-1")
        assert "café" in out

    def test_falls_back_on_unknown_charset(self):
        raw = b"hello"
        out = _decode_body(raw, "text/plain; charset=this-is-not-real")
        assert "hello" in out


def _stub_open(
    *,
    body: bytes,
    status: int = 200,
    content_type: str = "text/html; charset=utf-8",
    final_url: str = "https://example.com/page",
):
    """Build a stub `opener.open` return value (a context-manager response)."""
    resp = MagicMock()
    resp.geturl.return_value = final_url
    resp.status = status
    resp.headers.get_content_type.return_value = content_type.split(";", 1)[0].strip()
    resp.headers.get.return_value = None  # no Location header
    resp.headers.__contains__ = lambda _self, _k: False
    resp.read.return_value = body
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=resp)
    ctx.__exit__ = MagicMock(return_value=False)
    return ctx


def _patch_open(monkeypatch, ctx) -> None:
    """Stub the urllib opener so no real network call is made.

    Also stubs the SSRF resolution so tests stay hermetic (no real DNS).
    """
    import urllib.request

    import bog_agents_cli.web_fetch as wf

    monkeypatch.setattr(
        urllib.request.OpenerDirector,
        "open",
        lambda _self, _req, timeout=None: ctx,
    )
    # Treat the test host as a public address — exercise the fetch path, not
    # real DNS. SSRF-blocking behaviour is covered by the dedicated tests.
    monkeypatch.setattr(wf, "assert_fetch_allowed", lambda _url: None)


class TestFetchHappyPath:
    """fetch_url's interaction with urllib stubbed end-to-end."""

    def test_basic_html_fetch(self, monkeypatch):
        body = b"<html><body><p>hello world</p></body></html>"
        _patch_open(monkeypatch, _stub_open(body=body))
        result = fetch_url("https://example.com/page")
        assert result.status_code == 200
        assert "hello world" in result.body
        assert result.truncated is False
        assert result.final_url == "https://example.com/page"

    def test_truncation_flagged(self, monkeypatch):
        # Make a body larger than the per-call cap.
        body = b"a" * 20
        _patch_open(monkeypatch, _stub_open(body=body))
        result = fetch_url("https://example.com/page", max_bytes=10)
        assert result.truncated is True
        # Body holds the truncated, cleaned text — 10 'a's stripped of tags.
        assert "a" in result.body

    def test_non_html_kept_verbatim(self, monkeypatch):
        body = b"raw plain content"
        _patch_open(
            monkeypatch,
            _stub_open(body=body, content_type="text/plain; charset=utf-8"),
        )
        result = fetch_url("https://example.com/page")
        assert "raw plain content" in result.body


class TestRenderPromptBlock:
    def test_includes_url_and_status(self):
        r = FetchResult(
            url="https://example.com/x",
            final_url="https://example.com/canonical",
            status_code=200,
            content_type="text/html",
            body="hello",
            truncated=False,
        )
        out = render_prompt_block(r, intent="summarize this")
        assert "https://example.com/canonical" in out
        assert "200" in out
        assert "summarize this" in out
        assert "hello" in out

    def test_truncated_marker_when_relevant(self):
        r = FetchResult(
            url="https://example.com/x",
            final_url="https://example.com/x",
            status_code=200,
            content_type="text/html",
            body="…",
            truncated=True,
        )
        out = render_prompt_block(r)
        assert "truncated" in out.lower()


class TestErrorPaths:
    def test_http_error_surfaces_as_result_not_exception(self, monkeypatch):
        import urllib.error
        import urllib.request

        import bog_agents_cli.web_fetch as wf

        def raise_404(*_a, **_kw):
            err = urllib.error.HTTPError(
                "https://example.com/x", 404, "Not Found", {}, fp=io.BytesIO(b"missing")
            )
            err.headers = None  # exercise the None-headers branch
            raise err

        monkeypatch.setattr(urllib.request.OpenerDirector, "open", raise_404)
        monkeypatch.setattr(wf, "assert_fetch_allowed", lambda _url: None)
        result = fetch_url("https://example.com/x")
        # 4xx still yields a usable FetchResult, not an exception.
        assert result.status_code == 404
        assert "missing" in result.body or result.body == ""

    def test_network_error_raises(self, monkeypatch):
        import urllib.error
        import urllib.request

        import bog_agents_cli.web_fetch as wf

        def raise_dns(*_a, **_kw):
            msg = "nodename nor servname provided"
            raise urllib.error.URLError(msg)

        monkeypatch.setattr(urllib.request.OpenerDirector, "open", raise_dns)
        monkeypatch.setattr(wf, "assert_fetch_allowed", lambda _url: None)
        with pytest.raises(WebFetchError, match="Network error"):
            fetch_url("https://example.com/x")
