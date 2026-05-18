"""Typed fact store for the expert rule engine.

Facts are indexed by ``fact_type`` for O(1) candidate retrieval. Insertion
order is preserved within each type so the engine's recency tie-breaker in
conflict resolution is deterministic. Retraction is by stable ``Fact.id``.

The fact store is intentionally tiny — no persistence, no schema validation
beyond the fact_type discriminator. Persistence is the middleware's job
(it can serialise / replay facts to ``~/.bog-agents/expert-memory/``).
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Iterator
from dataclasses import replace
from typing import Any

from bog_agents.middleware.expert_engine.types import Fact


class WorkingMemory:
    """Holds the currently-asserted facts.

    The memory hands every newly-asserted fact a monotonically-increasing
    ``id`` (starting at 1; ``0`` is reserved for "not yet asserted").
    Retraction by id is O(1).

    Example::

        wm = WorkingMemory()
        f = wm.assert_fact(Fact(fact_type="tool_call", data={"name": "shell"}))
        for tool_call in wm.by_type("tool_call"):
            ...
        wm.retract(f.id)
    """

    def __init__(self) -> None:
        self._facts: dict[int, Fact] = {}
        self._by_type: dict[str, list[int]] = {}
        self._next_id = 1

    # ------------------------------------------------------------------
    # Assertion / retraction
    # ------------------------------------------------------------------

    def assert_fact(self, fact: Fact) -> Fact:
        """Add a fact and return the stored copy (with id + timestamp set).

        Args:
            fact: The fact to store. Its existing ``id`` / ``asserted_at``
                are ignored — the memory assigns fresh values.

        Returns:
            The stored fact with ``id`` populated. The original is
            unchanged (Fact is frozen).
        """
        stored = replace(fact, id=self._next_id, asserted_at=time.monotonic())
        self._next_id += 1
        self._facts[stored.id] = stored
        self._by_type.setdefault(stored.fact_type, []).append(stored.id)
        return stored

    def retract(self, fact_id: int) -> Fact | None:
        """Remove a fact by id. Returns the removed fact, or ``None`` if absent."""
        fact = self._facts.pop(fact_id, None)
        if fact is None:
            return None
        ids = self._by_type.get(fact.fact_type)
        if ids is not None:
            try:
                ids.remove(fact_id)
            except ValueError:
                pass
            if not ids:
                self._by_type.pop(fact.fact_type, None)
        return fact

    def retract_matching(
        self,
        fact_type: str,
        predicate: Any | None = None,
    ) -> list[Fact]:
        """Retract every fact of *fact_type* (optionally filtered).

        Args:
            fact_type: Discriminator to match.
            predicate: Optional callable ``Fact -> bool``. Only matching
                facts are retracted.

        Returns:
            The retracted facts.
        """
        removed: list[Fact] = []
        for fid in list(self._by_type.get(fact_type, [])):
            fact = self._facts.get(fid)
            if fact is None:
                continue
            if predicate is None or predicate(fact):
                self.retract(fid)
                removed.append(fact)
        return removed

    def clear(self) -> None:
        """Drop every fact and reset the id counter."""
        self._facts.clear()
        self._by_type.clear()
        self._next_id = 1

    # ------------------------------------------------------------------
    # Retrieval
    # ------------------------------------------------------------------

    def by_type(self, fact_type: str) -> Iterator[Fact]:
        """Iterate facts of a given type in insertion (recency) order."""
        for fid in self._by_type.get(fact_type, ()):
            fact = self._facts.get(fid)
            if fact is not None:
                yield fact

    def get(self, fact_id: int) -> Fact | None:
        """Return the fact with this id, or ``None``."""
        return self._facts.get(fact_id)

    def __len__(self) -> int:
        """Total number of currently-asserted facts."""
        return len(self._facts)

    def __iter__(self) -> Iterator[Fact]:
        """Iterate every fact in insertion order."""
        return iter(self._facts.values())

    def __contains__(self, fact_id: object) -> bool:
        """Test membership by id."""
        return isinstance(fact_id, int) and fact_id in self._facts

    def types(self) -> Iterable[str]:
        """Iterate the distinct fact types currently present."""
        return list(self._by_type.keys())

    # ------------------------------------------------------------------
    # Diagnostic
    # ------------------------------------------------------------------

    def stats(self) -> dict[str, int]:
        """Return ``{fact_type: count}`` for ``/expert status``."""
        return {ft: len(ids) for ft, ids in self._by_type.items()}
