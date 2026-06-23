"""Hardening tests for the bog-agents HTTP server (``serve.py``).

Covers the resiliency + auth fixes:
- P33: constant-time API-key comparison (accepts the right key, rejects wrong).
- P16: per-request timeout returns a 504-style error (no hang), concurrency is
  gated by a bounded semaphore, and the ``_threads`` map is LRU-bounded.
"""

from __future__ import annotations

import asyncio

import pytest

from bog_agents.serve import AgentServer, ServerConfig, _timeboxed_aiter


class _FakeAgent:
    """Minimal stand-in for a compiled LangGraph agent.

    ``ainvoke`` optionally sleeps ``delay`` seconds before returning, and tracks
    how many invocations are concurrently in-flight (to verify the semaphore).
    """

    def __init__(self, *, delay: float = 0.0, response: str = "ok") -> None:
        self.delay = delay
        self.response = response
        self.in_flight = 0
        self.max_in_flight = 0

    async def ainvoke(self, _input_data: dict, *, config: dict) -> dict:
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            return {"messages": [{"role": "assistant", "content": self.response}]}
        finally:
            self.in_flight -= 1


# --------------------------------------------------------------------------- #
# P33 — constant-time API-key comparison
# --------------------------------------------------------------------------- #


def test_api_key_accepts_correct_key() -> None:
    server = AgentServer(_FakeAgent(), config=ServerConfig(api_key="s3cret-key"))
    assert server._check_api_key("s3cret-key") is True


def test_api_key_rejects_wrong_key() -> None:
    server = AgentServer(_FakeAgent(), config=ServerConfig(api_key="s3cret-key"))
    assert server._check_api_key("wrong") is False
    # Matching prefix must still be rejected (the timing-leak case).
    assert server._check_api_key("s3cret-keyX") is False
    assert server._check_api_key("s3cret-ke") is False


def test_api_key_rejects_none_when_required() -> None:
    server = AgentServer(_FakeAgent(), config=ServerConfig(api_key="s3cret-key"))
    assert server._check_api_key(None) is False


def test_api_key_allows_all_when_unconfigured() -> None:
    server = AgentServer(_FakeAgent(), config=ServerConfig(api_key=None, host="127.0.0.1"))
    assert server.config.api_key is None
    assert server._check_api_key(None) is True
    assert server._check_api_key("anything") is True


def test_api_key_unicode_does_not_crash() -> None:
    # secrets.compare_digest requires same-type byte strings; non-ASCII keys
    # must encode cleanly rather than raise.
    server = AgentServer(_FakeAgent(), config=ServerConfig(api_key="ключ-é"))
    assert server._check_api_key("ключ-é") is True
    assert server._check_api_key("ascii") is False


# --------------------------------------------------------------------------- #
# P16 — per-request timeout
# --------------------------------------------------------------------------- #


async def test_invoke_times_out_returns_504_not_hang() -> None:
    agent = _FakeAgent(delay=10.0)
    server = AgentServer(agent, config=ServerConfig(request_timeout=0.05))
    # If the timeout were not applied this would hang for ~10s; wait_for guards.
    result = await asyncio.wait_for(server.invoke("hi"), timeout=2.0)
    assert result.get("status_code") == 504
    assert "error" in result
    assert "response" not in result


async def test_invoke_succeeds_within_timeout() -> None:
    agent = _FakeAgent(delay=0.0, response="hello")
    server = AgentServer(agent, config=ServerConfig(request_timeout=5.0))
    result = await server.invoke("hi")
    assert result["response"] == "hello"
    assert "status_code" not in result


# --------------------------------------------------------------------------- #
# P16 — concurrency gating
# --------------------------------------------------------------------------- #


async def test_concurrency_is_capped_by_semaphore() -> None:
    agent = _FakeAgent(delay=0.1)
    server = AgentServer(agent, config=ServerConfig(max_concurrent_requests=2, request_timeout=5.0))
    # Fire more requests than the cap; max in-flight must not exceed the cap.
    await asyncio.gather(*(server.invoke(f"msg-{i}", thread_id=f"t-{i}") for i in range(6)))
    assert agent.max_in_flight <= 2
    assert agent.max_in_flight >= 1


async def test_semaphore_is_lazily_created_and_reused() -> None:
    server = AgentServer(_FakeAgent(), config=ServerConfig(max_concurrent_requests=3))
    assert server._request_semaphore is None
    sem1 = server._get_request_semaphore()
    sem2 = server._get_request_semaphore()
    assert sem1 is sem2


# --------------------------------------------------------------------------- #
# P16 — bounded _threads map
# --------------------------------------------------------------------------- #


def test_threads_map_is_lru_bounded() -> None:
    server = AgentServer(_FakeAgent(), config=ServerConfig(max_tracked_threads=3))
    for i in range(10):
        server._get_or_create_thread(f"thread-{i}")
    assert len(server._threads) == 3
    # The most recent three survive; the oldest are evicted.
    assert set(server._threads) == {"thread-7", "thread-8", "thread-9"}


def test_threads_map_recency_protects_active_thread() -> None:
    server = AgentServer(_FakeAgent(), config=ServerConfig(max_tracked_threads=3))
    server._get_or_create_thread("keep")
    server._get_or_create_thread("a")
    server._get_or_create_thread("b")
    # Touch "keep" so it becomes most-recently-used.
    server._get_or_create_thread("keep")
    # Add two more, evicting the two LRU ("a", "b") but not "keep".
    server._get_or_create_thread("c")
    server._get_or_create_thread("d")
    assert "keep" in server._threads
    assert len(server._threads) == 3


def test_threads_map_zero_ceiling_clamped_to_one() -> None:
    # A non-positive ceiling is clamped to 1 so the active thread is always
    # returnable; the map still stays bounded.
    server = AgentServer(_FakeAgent(), config=ServerConfig(max_tracked_threads=0))
    t = server._get_or_create_thread("x")
    assert t.thread_id == "x"
    server._get_or_create_thread("y")
    assert len(server._threads) == 1
    assert "y" in server._threads


# --------------------------------------------------------------------------- #
# _timeboxed_aiter helper
# --------------------------------------------------------------------------- #


async def test_timeboxed_aiter_passes_through_fast_items() -> None:
    async def gen() -> object:
        for i in range(3):
            yield {"i": i}

    items = [item async for item in _timeboxed_aiter(gen(), timeout=1.0)]
    assert items == [{"i": 0}, {"i": 1}, {"i": 2}]


async def test_timeboxed_aiter_raises_on_stall() -> None:
    async def stalling_gen() -> object:
        yield {"first": True}
        await asyncio.sleep(10.0)
        yield {"never": True}

    async def _drain() -> None:
        async for _item in _timeboxed_aiter(stalling_gen(), timeout=0.05):
            pass

    with pytest.raises(asyncio.TimeoutError):
        await _drain()
