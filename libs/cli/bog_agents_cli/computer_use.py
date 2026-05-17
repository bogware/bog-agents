"""Computer Use / browser tools (Gap 6).

Exposes a small, well-bounded set of browser-automation tools the agent
can call: navigate, screenshot, click, type, evaluate-js, close. Backed
by Playwright (async) but designed so the tools accept *or* a real
Playwright runtime *or* a swappable factory for tests.

Why a thin shim around Playwright rather than the full surface:

* The agent only needs a handful of verbs to do useful work
  (navigate / read / click / type). Exposing the full Playwright API
  invites the model to do something exotic and surprising.
* The shim normalises return shapes — Playwright returns rich objects;
  the tools return short strings the model can reason about.
* Substituting a fake browser for tests is trivial — set the
  ``_BROWSER_FACTORY`` module attribute, no monkeypatching needed in
  the tools themselves.

This module is **opt-in**. Importing it does *not* start Playwright;
``BrowserSession.start()`` (called lazily by the first tool) does.
Closing is the user's responsibility — call ``shutdown_browser()`` in
TUI teardown or on the user's ``/browser close``.

Safety notes
------------

* All tools route through one process-wide ``BrowserSession``. Two
  agent turns in the same session share the page; opening a fresh
  browser per turn is wasteful and slow.
* ``browser_eval_js`` is the one tool with full power. Gate behind
  HITL when shipped to untrusted users.
* Screenshots are returned as base64 PNG strings — the model has to
  ask for them; we never auto-attach.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from langchain_core.tools import tool

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Browser session — singleton per process
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class BrowserStats:
    """Lightweight counters exposed to /browser status."""

    navigations: int = 0
    screenshots: int = 0
    clicks: int = 0
    typed_chars: int = 0
    evals: int = 0


# Factory: zero-arg callable returning an awaitable that yields a
# (browser, context, page) triple. Real impl uses Playwright; tests
# substitute a fake. Held module-level so tests can override without
# threading a parameter through every tool.
BrowserFactory = Callable[[], Awaitable[tuple[Any, Any, Any]]]
_BROWSER_FACTORY: BrowserFactory | None = None


async def _default_factory() -> tuple[Any, Any, Any]:
    """Build a fresh chromium page using the installed Playwright runtime.

    Imported lazily so the module loads cleanly on systems without
    Playwright (the user just can't *use* the tools until they
    ``pip install playwright`` and ``playwright install chromium``).
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        msg = (
            "Computer Use tools require playwright. Install with:\n"
            "  pip install playwright\n"
            "  playwright install chromium"
        )
        raise BrowserUnavailableError(msg) from exc

    runtime = await async_playwright().start()
    browser = await runtime.chromium.launch(headless=True)
    context = await browser.new_context()
    page = await context.new_page()
    # Stash the runtime on the browser so we can stop it in close().
    browser._bog_runtime = runtime  # type: ignore[attr-defined]
    return browser, context, page


def set_browser_factory(factory: BrowserFactory | None) -> None:
    """Override the browser factory. Tests use this to inject a fake."""
    global _BROWSER_FACTORY  # noqa: PLW0603 — module-level singleton override
    _BROWSER_FACTORY = factory


class BrowserUnavailableError(RuntimeError):
    """Raised when no Playwright runtime is available."""


class BrowserSession:
    """Process-wide singleton wrapping one Playwright browser.

    Constructed lazily — ``ensure_started()`` brings up the browser on
    first use, subsequent calls return the existing instance. Thread-
    safe via a coarse-grained lock; the underlying Playwright APIs
    are async-only, so the lock just serialises the lazy init.
    """

    _instance: BrowserSession | None = None
    _lock = threading.Lock()

    def __init__(self) -> None:
        self._browser: Any = None
        self._context: Any = None
        self._page: Any = None
        self._stats = BrowserStats()
        self._init_lock = asyncio.Lock()

    @classmethod
    def instance(cls) -> BrowserSession:
        """Return the singleton, constructing it on first call."""
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    @classmethod
    def reset_for_tests(cls) -> None:
        """Drop the singleton without closing it. Tests-only."""
        with cls._lock:
            cls._instance = None

    @property
    def stats(self) -> BrowserStats:
        return self._stats

    @property
    def is_started(self) -> bool:
        return self._browser is not None

    async def ensure_started(self) -> None:
        """Idempotent — open Chromium + new context + new page if not yet."""
        if self._browser is not None:
            return
        async with self._init_lock:
            if self._browser is not None:
                return
            factory = _BROWSER_FACTORY or _default_factory
            browser, context, page = await factory()
            self._browser = browser
            self._context = context
            self._page = page
            logger.info("Browser session started")

    async def page(self) -> Any:
        """Return the live Page, opening the browser if needed."""
        await self.ensure_started()
        return self._page

    async def close(self) -> None:
        """Close page, context, browser, and the underlying runtime."""
        if self._browser is None:
            return
        with_suppress = []
        if self._context is not None:
            with_suppress.append(self._context.close())
        if self._browser is not None:
            with_suppress.append(self._browser.close())
        for coro in with_suppress:
            try:
                await coro
            except Exception:
                logger.debug("browser close: ignored exception", exc_info=True)
        runtime = getattr(self._browser, "_bog_runtime", None)
        if runtime is not None:
            try:
                await runtime.stop()
            except Exception:
                logger.debug("playwright runtime stop failed", exc_info=True)
        self._browser = None
        self._context = None
        self._page = None
        logger.info("Browser session closed")


async def shutdown_browser() -> None:
    """Tear down the process-wide browser singleton."""
    session = BrowserSession.instance()
    await session.close()


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@tool
async def browser_navigate(url: str) -> str:
    """Open *url* in the agent's browser session.

    Use this to read documentation pages, follow links from search
    results, or inspect a deployed UI. Returns the final URL after
    redirects and the page title.
    """
    session = BrowserSession.instance()
    page = await session.page()
    await page.goto(url)
    session.stats.navigations += 1
    final_url = getattr(page, "url", url)
    title = ""
    try:
        title = await page.title()
    except Exception:
        logger.debug("browser.title() failed", exc_info=True)
    return f"Navigated to {final_url}\nTitle: {title}"


@tool
async def browser_screenshot() -> str:
    """Capture a PNG screenshot of the current page.

    Returns a base64-encoded PNG. Useful when you need visual
    confirmation a layout looks right or when a page renders text via
    JavaScript that isn't in the DOM.
    """
    session = BrowserSession.instance()
    page = await session.page()
    raw = await page.screenshot()
    session.stats.screenshots += 1
    encoded = base64.b64encode(raw).decode("ascii")
    return f"base64:image/png:{encoded}"


@tool
async def browser_click(selector: str) -> str:
    """Click the element matching the CSS *selector*.

    Examples: ``"button.submit"``, ``"#login"``, ``"a:has-text('Next')"``.
    """
    session = BrowserSession.instance()
    page = await session.page()
    await page.click(selector)
    session.stats.clicks += 1
    return f"Clicked {selector!r}"


@tool
async def browser_type(selector: str, text: str) -> str:
    """Type *text* into the input matching the CSS *selector*.

    The agent should usually click first to focus the input, then
    call this. We delegate to Playwright's ``fill`` which clears any
    existing value before typing — matching what the model
    typically wants when filling forms.
    """
    session = BrowserSession.instance()
    page = await session.page()
    await page.fill(selector, text)
    session.stats.typed_chars += len(text)
    return f"Typed {len(text)} chars into {selector!r}"


@tool
async def browser_eval_js(script: str) -> str:
    """Evaluate a JavaScript expression and return the result as text.

    Use sparingly — gives the agent full control over the page. The
    return value is best-effort stringified; complex objects come
    back as their JSON encoding.
    """
    session = BrowserSession.instance()
    page = await session.page()
    result = await page.evaluate(script)
    session.stats.evals += 1
    if isinstance(result, (dict, list)):
        import json

        return json.dumps(result, separators=(",", ":"), default=str)
    return str(result)


@tool
async def browser_read_text(selector: str = "body") -> str:
    """Return the visible text under *selector* (default: whole body).

    A safer alternative to ``browser_eval_js`` when the agent just
    wants to read content. We collapse whitespace to keep the
    response compact.
    """
    session = BrowserSession.instance()
    page = await session.page()
    handle = await page.query_selector(selector)
    if handle is None:
        return f"No element matched selector {selector!r}."
    text = await handle.inner_text()
    return " ".join((text or "").split())


@tool
async def browser_close() -> str:
    """Close the agent's browser session (releases the Chromium process)."""
    await shutdown_browser()
    return "Browser session closed."


# ---------------------------------------------------------------------------
# Wiring helpers
# ---------------------------------------------------------------------------


def build_browser_tools() -> list[Any]:
    """Return the list of browser tools to merge into the agent's tool set.

    Importers call this when constructing the agent so the tool list
    is explicit (rather than auto-injected via middleware). Keeps the
    tool surface visible in code review.
    """
    return [
        browser_navigate,
        browser_screenshot,
        browser_click,
        browser_type,
        browser_eval_js,
        browser_read_text,
        browser_close,
    ]


def render_browser_status() -> str:
    """User-facing status text shown by ``/browser`` with no args."""
    session = BrowserSession.instance()
    if not session.is_started:
        return (
            "Browser session: not started.\n"
            "First call to a browser tool will start one. "
            "Install Playwright (`pip install playwright && playwright install chromium`) "
            "before enabling Computer Use in production."
        )
    s = session.stats
    return (
        "Browser session: running\n"
        f"  Navigations: {s.navigations}\n"
        f"  Screenshots: {s.screenshots}\n"
        f"  Clicks:      {s.clicks}\n"
        f"  Typed chars: {s.typed_chars}\n"
        f"  JS evals:    {s.evals}\n"
        "Close with /browser close."
    )


__all__ = [
    "BrowserFactory",
    "BrowserSession",
    "BrowserStats",
    "BrowserUnavailableError",
    "browser_click",
    "browser_close",
    "browser_eval_js",
    "browser_navigate",
    "browser_read_text",
    "browser_screenshot",
    "browser_type",
    "build_browser_tools",
    "render_browser_status",
    "set_browser_factory",
    "shutdown_browser",
]
