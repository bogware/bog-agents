"""Shared async/thread-safe state helper for middleware.

Many bog-agents middleware (``audit_trail``, ``dlp``, ``cost_tracker``,
``hot_reload_skills``, ...) maintain mutable state that ``wrap_model_call``
and ``awrap_model_call`` touch on every turn. Once parallel-worktree or
multi-agent middleware spawns concurrent tasks across event loops, these
in-place mutations can race.

``MiddlewareState`` is a tiny holder that serializes mutations through a
``threading.Lock``. The lock is cheap inside one event loop (asyncio is
single-threaded per loop) and *correct* across the multi-loop / threaded
boundary that ``ParallelWorktreeMiddleware`` introduces.

Usage::

    from bog_agents.middleware._state import MiddlewareState

    class MyMiddleware:
        def __init__(self):
            self._state = MiddlewareState({"calls": 0})

        def wrap_model_call(self, request, call_next):
            self._state.update(lambda d: {**d, "calls": d["calls"] + 1})
            ...
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Generic, TypeVar

T = TypeVar("T")


class MiddlewareState(Generic[T]):
    """Lock-protected wrapper around a single mutable value.

    The wrapped value is replaced atomically via :meth:`update`; callers
    receive a copy via :meth:`get`. The lock is re-entrant so an
    ``update`` callback may call back into ``get`` on the same thread.
    """

    __slots__ = ("_lock", "_value")

    def __init__(self, initial: T) -> None:
        self._value = initial
        self._lock = threading.RLock()

    def get(self) -> T:
        """Return the current value (a snapshot)."""
        with self._lock:
            return self._value

    def update(self, fn: Callable[[T], T]) -> T:
        """Replace the value with ``fn(current)`` atomically.

        Args:
            fn: Pure function from the current value to the next value.
                Must NOT raise — exceptions propagate and the value is
                left unchanged.

        Returns:
            The new value after replacement.
        """
        with self._lock:
            self._value = fn(self._value)
            return self._value

    def mutate(self, fn: Callable[[T], None]) -> T:
        """Apply an in-place mutation under the lock.

        For container types where building a fresh copy on every update is
        too expensive (e.g. appending to a list of audit entries), use
        ``mutate`` to mutate in place while still serializing access.

        Args:
            fn: Callable that mutates the current value in place.

        Returns:
            The (mutated) current value.
        """
        with self._lock:
            fn(self._value)
            return self._value
