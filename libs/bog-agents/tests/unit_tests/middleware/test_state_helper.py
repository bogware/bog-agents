"""Unit tests for ``bog_agents.middleware._state.MiddlewareState``."""

from __future__ import annotations

import threading

from bog_agents.middleware._state import MiddlewareState


def test_get_returns_initial_value() -> None:
    s: MiddlewareState[int] = MiddlewareState(0)
    assert s.get() == 0


def test_update_replaces_value() -> None:
    s: MiddlewareState[int] = MiddlewareState(0)
    new = s.update(lambda v: v + 5)
    assert new == 5
    assert s.get() == 5


def test_mutate_in_place() -> None:
    s: MiddlewareState[list[int]] = MiddlewareState([])
    s.mutate(lambda lst: lst.append(1))
    s.mutate(lambda lst: lst.append(2))
    assert s.get() == [1, 2]


def test_concurrent_increments_are_atomic() -> None:
    """Many threads incrementing in update() must produce the right total."""
    s: MiddlewareState[int] = MiddlewareState(0)
    iterations = 200
    threads = 16

    def worker() -> None:
        for _ in range(iterations):
            s.update(lambda v: v + 1)

    workers = [threading.Thread(target=worker) for _ in range(threads)]
    for t in workers:
        t.start()
    for t in workers:
        t.join()

    assert s.get() == threads * iterations
