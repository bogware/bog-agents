"""Unit tests for Computer Use / browser tools (Gap 6).

Playwright is not invoked. A stub factory supplies a fake page that
records the calls so we can assert the tool surface translates them
correctly without launching Chromium.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from bog_agents_cli import computer_use as cu

# ---------------------------------------------------------------------------
# Fake Playwright stack
# ---------------------------------------------------------------------------


@dataclass
class _FakePage:
    """Records the calls the tools make. ``url`` is settable for navigate."""

    url: str = "about:blank"
    title_text: str = "fake"
    screenshot_bytes: bytes = b"\x89PNG\r\n\x1a\nfake"
    eval_result: Any = "fake-eval-result"
    selector_text: str = "hello   world"

    calls: list[tuple[str, tuple, dict]] = field(default_factory=list)

    async def goto(self, url, **kw):
        self.calls.append(("goto", (url,), kw))
        self.url = url

    async def title(self):
        self.calls.append(("title", (), {}))
        return self.title_text

    async def screenshot(self, **kw):
        self.calls.append(("screenshot", (), kw))
        return self.screenshot_bytes

    async def click(self, selector, **kw):
        self.calls.append(("click", (selector,), kw))

    async def fill(self, selector, text, **kw):
        self.calls.append(("fill", (selector, text), kw))

    async def evaluate(self, script, **kw):
        self.calls.append(("evaluate", (script,), kw))
        return self.eval_result

    async def query_selector(self, selector, **kw):
        self.calls.append(("query_selector", (selector,), kw))
        if selector == "missing":
            return None

        class _Handle:
            async def inner_text(_self) -> str:  # noqa: N805
                return self.selector_text

        return _Handle()


@dataclass
class _FakeBrowser:
    closed: bool = False

    async def close(self) -> None:
        self.closed = True


@dataclass
class _FakeContext:
    closed: bool = False

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake_page() -> _FakePage:
    page = _FakePage()
    browser = _FakeBrowser()
    context = _FakeContext()

    async def factory():
        return browser, context, page

    cu.set_browser_factory(factory)
    cu.BrowserSession.reset_for_tests()
    yield page
    cu.set_browser_factory(None)
    cu.BrowserSession.reset_for_tests()


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


class TestSession:
    @pytest.mark.asyncio
    async def test_ensure_started_idempotent(self, fake_page: _FakePage):
        session = cu.BrowserSession.instance()
        assert not session.is_started
        await session.ensure_started()
        assert session.is_started
        # Second call is a no-op.
        await session.ensure_started()
        assert session.is_started

    @pytest.mark.asyncio
    async def test_close_releases(self, fake_page: _FakePage):
        session = cu.BrowserSession.instance()
        await session.ensure_started()
        assert session.is_started
        await session.close()
        assert not session.is_started

    @pytest.mark.asyncio
    async def test_shutdown_browser_module_function(self, fake_page: _FakePage):
        session = cu.BrowserSession.instance()
        await session.ensure_started()
        await cu.shutdown_browser()
        assert not session.is_started

    def test_render_status_when_idle(self):
        cu.BrowserSession.reset_for_tests()
        out = cu.render_browser_status()
        assert "not started" in out.lower()


# ---------------------------------------------------------------------------
# Individual tools
# ---------------------------------------------------------------------------


class TestTools:
    @pytest.mark.asyncio
    async def test_navigate_records_call(self, fake_page: _FakePage):
        result = await cu.browser_navigate.ainvoke({"url": "https://example.com"})
        assert "Navigated to" in result
        assert "https://example.com" in result
        assert ("goto", ("https://example.com",), {}) in fake_page.calls

    @pytest.mark.asyncio
    async def test_screenshot_returns_base64(self, fake_page: _FakePage):
        out = await cu.browser_screenshot.ainvoke({})
        assert out.startswith("base64:image/png:")
        assert len(out) > len("base64:image/png:")

    @pytest.mark.asyncio
    async def test_click_records_selector(self, fake_page: _FakePage):
        out = await cu.browser_click.ainvoke({"selector": "button.submit"})
        assert "button.submit" in out
        assert any(name == "click" for name, *_ in fake_page.calls)

    @pytest.mark.asyncio
    async def test_type_records_chars(self, fake_page: _FakePage):
        out = await cu.browser_type.ainvoke(
            {"selector": "#email", "text": "scott@example.com"}
        )
        assert "17 chars" in out
        assert ("fill", ("#email", "scott@example.com"), {}) in fake_page.calls

    @pytest.mark.asyncio
    async def test_eval_returns_string_for_primitives(self, fake_page: _FakePage):
        fake_page.eval_result = 42
        out = await cu.browser_eval_js.ainvoke({"script": "1 + 41"})
        assert out == "42"

    @pytest.mark.asyncio
    async def test_eval_returns_json_for_complex(self, fake_page: _FakePage):
        fake_page.eval_result = {"a": 1, "b": [2, 3]}
        out = await cu.browser_eval_js.ainvoke({"script": "({a:1, b:[2,3]})"})
        assert '"a":1' in out
        assert '"b":[2,3]' in out

    @pytest.mark.asyncio
    async def test_read_text_default_body(self, fake_page: _FakePage):
        out = await cu.browser_read_text.ainvoke({})
        assert "hello world" in out  # whitespace collapsed

    @pytest.mark.asyncio
    async def test_read_text_missing_selector(self, fake_page: _FakePage):
        out = await cu.browser_read_text.ainvoke({"selector": "missing"})
        assert "No element matched" in out

    @pytest.mark.asyncio
    async def test_close_tool_shuts_down(self, fake_page: _FakePage):
        # Start the session first by issuing a tool call.
        await cu.browser_navigate.ainvoke({"url": "https://example.com"})
        assert cu.BrowserSession.instance().is_started
        out = await cu.browser_close.ainvoke({})
        assert "closed" in out.lower()
        assert not cu.BrowserSession.instance().is_started


class TestStats:
    @pytest.mark.asyncio
    async def test_stats_increment(self, fake_page: _FakePage):
        await cu.browser_navigate.ainvoke({"url": "https://x"})
        await cu.browser_screenshot.ainvoke({})
        await cu.browser_click.ainvoke({"selector": ".a"})
        await cu.browser_type.ainvoke({"selector": ".a", "text": "hi"})
        await cu.browser_eval_js.ainvoke({"script": "1"})
        s = cu.BrowserSession.instance().stats
        assert s.navigations == 1
        assert s.screenshots == 1
        assert s.clicks == 1
        assert s.typed_chars == 2
        assert s.evals == 1


class TestToolWiring:
    def test_build_browser_tools_returns_expected_set(self):
        tools = cu.build_browser_tools()
        names = {t.name for t in tools}
        assert {
            "browser_navigate",
            "browser_screenshot",
            "browser_click",
            "browser_type",
            "browser_eval_js",
            "browser_read_text",
            "browser_close",
        } == names


class TestErrorPaths:
    @pytest.mark.asyncio
    async def test_default_factory_raises_when_playwright_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # Force ImportError from inside _default_factory.
        import builtins as _b

        real_import = _b.__import__

        def fake_import(name, *a, **kw):
            if name.startswith("playwright"):
                msg = "stub no playwright"
                raise ImportError(msg)
            return real_import(name, *a, **kw)

        monkeypatch.setattr(_b, "__import__", fake_import)
        cu.set_browser_factory(None)
        cu.BrowserSession.reset_for_tests()
        with pytest.raises(cu.BrowserUnavailableError):
            await cu._default_factory()
