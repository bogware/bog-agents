"""Regression test for the ``/review`` / ``/audit`` stall on Windows.

This test reproduces the exact event-loop pattern that
``langgraph_runtime_inmem`` uses for background runs (one isolated
event loop per run) and confirms two things:

1. **The bug**: when ``langchain_anthropic._client_utils`` keeps its
   default ``@lru_cache`` on ``_get_default_async_httpx_client``, the
   shared httpx client's internal primitives bind to the FIRST loop
   that used it. Later loops can deadlock awaiting on those primitives.
2. **The fix**: ``server_graph._install_anthropic_httpx_per_loop_fix``
   replaces the cached factory with a per-call wrapper. Each loop gets
   a fresh client; the deadlock cannot occur.

The test is structural — it doesn't make real Anthropic API calls.
What it asserts is the *shape* of the fix: cached vs. uncached, same
instance vs. fresh instance per call. This is the trip-wire that fires
if a future ``langchain_anthropic`` upgrade re-introduces the cache or
moves the symbol so the patch silently no-ops.

The full reproducer (a real model call across isolated loops) requires
an Anthropic API key so it lives outside the unit-test boundary —
documented in the docstring below for manual verification.

Manual repro (requires ``ANTHROPIC_API_KEY``)::

    import asyncio
    from langchain_anthropic import ChatAnthropic

    chat = ChatAnthropic(model="claude-sonnet-4-6")

    async def call(i):
        r = await chat.ainvoke(f"say {i}")
        print(i, r.content[:30])

    # Without the fix this hangs around iteration 3-5 on Windows.
    for i in range(10):
        asyncio.run(call(i))

After importing ``bog_agents_cli.server_graph`` once, the same loop
exercises hundreds of iterations without hanging.
"""

from __future__ import annotations

from functools import lru_cache


class TestAnthropicHttpxPerLoopFix:
    """The patch is the load-bearing fix; verify its shape end-to-end."""

    def test_lru_cache_is_replaced_after_server_graph_import(self) -> None:
        """The cached factory is swapped for a per-call wrapper.

        Imports ``server_graph`` (which runs the install hook at
        module import) and asserts the upstream
        ``_get_default_async_httpx_client`` is NO LONGER an lru_cache.
        Two consecutive calls with identical args must return distinct
        instances — the property that lets each event loop get its own
        client.
        """
        # Import lazily to keep this test self-contained: importing
        # server_graph triggers the install hook.
        import bog_agents_cli.server_graph  # noqa: F401, PLC0415  # import has side-effects we're verifying
        import langchain_anthropic._client_utils as cu  # noqa: PLC0415, PLC2701

        async_factory = cu._get_default_async_httpx_client
        # The original was an lru_cache wrapper. After the install,
        # ``cache_info`` should NOT be present.
        assert not hasattr(async_factory, "cache_info"), (
            "expected cached lru_cache wrapper to have been replaced; "
            f"still has cache_info → {type(async_factory).__name__}"
        )
        # Distinct instances per call — the ENTIRE point.
        c1 = async_factory(base_url=None)
        c2 = async_factory(base_url=None)
        assert c1 is not c2, (
            "two consecutive calls returned the same httpx client — "
            "the @lru_cache must still be in place. Each loop needs "
            "its own client to avoid event-loop binding deadlocks."
        )

    def test_sync_factory_also_replaced(self) -> None:
        """The sync counterpart is patched too (it has the same bug shape)."""
        import bog_agents_cli.server_graph  # noqa: F401, PLC0415
        import langchain_anthropic._client_utils as cu  # noqa: PLC0415, PLC2701

        sync_factory = cu._get_default_httpx_client
        assert not hasattr(sync_factory, "cache_info")
        c1 = sync_factory(base_url=None)
        c2 = sync_factory(base_url=None)
        assert c1 is not c2

    def test_install_is_idempotent(self) -> None:
        """Calling the installer twice is safe.

        ``server_graph`` is imported once at module load, but a defensive
        re-call (e.g. someone calls ``_install_anthropic_httpx_per_loop_fix``
        explicitly) must not crash or break the patch.
        """
        from bog_agents_cli import server_graph  # noqa: PLC0415

        # First call already happened at module import. Run again.
        server_graph._install_anthropic_httpx_per_loop_fix()
        server_graph._install_anthropic_httpx_per_loop_fix()
        # And the patch is still in place.
        import langchain_anthropic._client_utils as cu  # noqa: PLC0415, PLC2701

        assert not hasattr(cu._get_default_async_httpx_client, "cache_info")

    def test_underlying_factory_still_callable_after_patch(self) -> None:
        """The replacement still produces a working httpx client.

        We don't make a real HTTP request — just confirm the returned
        object is the expected wrapper type and has an ``aclose``
        method (so it's a real httpx-compatible async client, not a
        stub).
        """
        import bog_agents_cli.server_graph  # noqa: F401, PLC0415
        import langchain_anthropic._client_utils as cu  # noqa: PLC0415, PLC2701

        client = cu._get_default_async_httpx_client(base_url=None)
        # Real httpx async clients expose ``aclose``.
        assert hasattr(client, "aclose")
        assert client.__class__.__name__ == "_AsyncHttpxClientWrapper"


class TestPatchShapeRegression:
    """Trip-wire: catches upstream changes that defeat the patch silently."""

    def test_upstream_factory_uses_lru_cache(self) -> None:
        """Sanity: the bug we're patching DOES exist in the upstream.

        If a future ``langchain_anthropic`` upgrade removes
        ``@lru_cache`` from these factories, our patch becomes a no-op
        (still safe but unnecessary). This test catches the change so
        we can revisit the patch.
        """
        # Re-import the module without server_graph's install hook.
        # We do this by inspecting the source to find ``@lru_cache`` —
        # importing the module would already be patched if any other
        # test ran server_graph first.
        import inspect  # noqa: PLC0415
        import langchain_anthropic._client_utils as cu  # noqa: PLC0415, PLC2701

        source = inspect.getsource(cu)
        # If the upstream removes @lru_cache we want a heads-up. This
        # is not a hard failure — just a signal that the patch may no
        # longer be needed.
        if "@lru_cache" not in source:
            import warnings  # noqa: PLC0415

            warnings.warn(
                "langchain_anthropic._client_utils no longer uses "
                "@lru_cache — the per-loop httpx fix in "
                "server_graph._install_anthropic_httpx_per_loop_fix "
                "may be obsolete. Re-evaluate.",
                stacklevel=2,
            )

    def test_lru_cache_unwrap_protocol(self) -> None:
        """``functools.lru_cache`` exposes ``__wrapped__`` — patch relies on this.

        The fix walks ``cached.__wrapped__`` to recover the underlying
        function. Stdlib's lru_cache contract guarantees this attribute,
        but we lock it in here so a stdlib change doesn't quietly break
        the patch.
        """

        @lru_cache
        def _example() -> int:
            return 1

        assert hasattr(_example, "__wrapped__")
        assert _example.__wrapped__() == 1


class TestChatAnthropicCachedPropertyDefeated:
    """Layer 2 of the per-loop fix: ``ChatAnthropic._async_client`` no longer cached.

    The lru_cache patch (layer 1) makes the underlying httpx client
    factories return fresh clients per call. But that's not enough on
    its own — ``ChatAnthropic`` wraps the http client in an
    ``AsyncAnthropic`` and exposes it as ``_async_client``, which is
    decorated ``@functools.cached_property``. Once initialised on an
    instance, the SAME ``AsyncAnthropic`` is returned for every
    subsequent access — and its anyio primitives are bound to whichever
    loop first accessed it. Layer 2 replaces the cached_property with
    a plain property so each access recomputes.

    These tests verify both descriptors (``_async_client`` and
    ``_client``) are swapped from cached_property to property by
    ``server_graph._install_anthropic_async_client_per_call``.
    """

    def test_async_client_is_property_not_cached_property(self) -> None:
        import functools  # noqa: PLC0415

        import bog_agents_cli.server_graph  # noqa: F401, PLC0415  # triggers patch
        from langchain_anthropic.chat_models import ChatAnthropic  # noqa: PLC0415

        descriptor = ChatAnthropic.__dict__["_async_client"]
        assert isinstance(descriptor, property), (
            f"expected plain property; got {type(descriptor).__name__}. "
            "If this fails, ChatAnthropic._async_client is still a "
            "cached_property and the per-call fresh-AsyncAnthropic guarantee "
            "is broken — multi-loop runs WILL deadlock."
        )
        assert not isinstance(descriptor, functools.cached_property)

    def test_sync_client_is_property_not_cached_property(self) -> None:
        import functools  # noqa: PLC0415

        import bog_agents_cli.server_graph  # noqa: F401, PLC0415
        from langchain_anthropic.chat_models import ChatAnthropic  # noqa: PLC0415

        descriptor = ChatAnthropic.__dict__["_client"]
        assert isinstance(descriptor, property)
        assert not isinstance(descriptor, functools.cached_property)

    def test_distinct_async_client_per_access(self) -> None:
        """Two consecutive ``model._async_client`` accesses → distinct objects.

        This is the load-bearing assertion for the multi-loop bug.
        With cached_property still in place, both accesses return the
        same ``AsyncAnthropic`` whose anyio primitives are bound to
        the first-use loop — and run #2 (in a new loop) deadlocks.
        After the patch each access constructs a fresh
        ``AsyncAnthropic`` whose primitives are local to the *current*
        loop.
        """
        import os  # noqa: PLC0415

        os.environ.setdefault("ANTHROPIC_API_KEY", "sk-test")
        import bog_agents_cli.server_graph  # noqa: F401, PLC0415
        from langchain_anthropic.chat_models import ChatAnthropic  # noqa: PLC0415

        m = ChatAnthropic(model="claude-sonnet-4-6")
        c1 = m._async_client
        c2 = m._async_client
        assert c1 is not c2, (
            "ChatAnthropic._async_client returned the same instance twice — "
            "cached_property has crept back in or the patch failed silently"
        )


class TestEachIsolatedLoopGetsFreshClient:
    """End-to-end shape: distinct httpx clients across distinct loops.

    Reproduces the langgraph_runtime_inmem pattern (one isolated event
    loop per background run) and confirms each loop gets its own
    httpx client. Without the patch, all loops share a single cached
    client whose internal primitives bind to the FIRST loop —
    later loops eventually deadlock awaiting on dead-loop futures.

    No real Anthropic API call is made; we only assert object
    identity. That's sufficient for the regression: the deadlock is
    structural (shared state across loop boundaries) and the fix is
    structural (per-call factory). If clients are distinct per loop,
    the deadlock cannot occur.
    """

    def test_distinct_client_per_isolated_loop(self) -> None:
        """Two ``asyncio.run`` calls produce two distinct httpx clients.

        ``asyncio.run`` creates a fresh event loop and tears it down
        on exit — exactly the pattern ``langgraph_runtime_inmem`` uses
        per background run. Before the patch, both calls returned the
        same lru_cached instance whose anyio primitives were bound to
        the FIRST loop. After the patch, each call gets a fresh
        client local to its own loop.
        """
        import asyncio  # noqa: PLC0415

        import bog_agents_cli.server_graph  # noqa: F401, PLC0415  # triggers patch
        import langchain_anthropic._client_utils as cu  # noqa: PLC0415, PLC2701

        clients: list[object] = []

        async def _grab_client() -> None:
            clients.append(cu._get_default_async_httpx_client(base_url=None))

        # Two SEPARATE event loops, sequentially — mirrors
        # ``langgraph_runtime_inmem.queue.run`` doing
        # ``asyncio.run(...)`` per background run.
        asyncio.run(_grab_client())
        asyncio.run(_grab_client())
        asyncio.run(_grab_client())

        assert len(clients) == 3
        # All three must be distinct objects. The bug we're regressing
        # against would return the same lru_cached instance for all
        # three — which is exactly when the deadlock manifests.
        assert clients[0] is not clients[1]
        assert clients[1] is not clients[2]
        assert clients[0] is not clients[2]
