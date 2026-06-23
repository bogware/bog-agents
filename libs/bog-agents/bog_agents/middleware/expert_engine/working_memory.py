"""Typed fact store for the expert rule engine.

Facts are indexed by ``fact_type`` for O(1) candidate retrieval. Insertion
order is preserved within each type so the engine's recency tie-breaker in
conflict resolution is deterministic. Retraction is by stable ``Fact.id``.

The fact store is intentionally tiny — no persistence, no schema validation
beyond the fact_type discriminator. Persistence is the middleware's job
(it can serialise / replay facts to ``~/.bog-agents/expert-memory/``).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable, Iterator
from dataclasses import replace
from typing import Any

from bog_agents.middleware.expert_engine.types import Fact

logger = logging.getLogger(__name__)

# Default soft cap on the number of *derived* facts (facts whose ``fact_type``
# is not in ``protected_fact_types``). Rule ``assert_fact`` actions accumulate
# in shared memory for the engine's lifetime — only the per-call ``tool_call``
# fact is retracted each turn — so a cumulative-cost / rate-limit rulebook can
# leak memory and compound matcher latency (O(P x F^k)) over a long daemon
# session. Crossing the cap emits a one-time warning; running far past it
# FIFO-evicts the oldest derived facts. ``5000`` is well above any realistic
# policy rulebook's working set yet bounds pathological growth.
_DEFAULT_MAX_WORKING_FACTS = 5000

# Fact types that are never auto-evicted. ``tool_call`` is the per-call
# structural fact the matcher keys every rule off of (it is asserted then
# retracted each turn, so it is never the leak source). Only derived facts
# are subject to the cap.
_DEFAULT_PROTECTED_FACT_TYPES = ("tool_call",)


class WorkingMemory:
    """Holds the currently-asserted facts.

    The memory hands every newly-asserted fact a monotonically-increasing
    ``id`` (starting at 1; ``0`` is reserved for "not yet asserted").
    Retraction by id is O(1).

    A soft cap (``max_working_facts``) bounds the number of *derived* facts —
    those whose ``fact_type`` is not in ``protected_fact_types``. Crossing the
    cap logs a one-time warning; exceeding it by the FIFO-eviction margin drops
    the oldest derived facts (never structural / ``tool_call`` facts) so the
    cross-call rule semantics the feature relies on (rate-limit, cumulative
    cost) keep working while pathological growth stays bounded.

    Example::

        wm = WorkingMemory()
        f = wm.assert_fact(Fact(fact_type="tool_call", data={"name": "shell"}))
        for tool_call in wm.by_type("tool_call"):
            ...
        wm.retract(f.id)
    """

    def __init__(
        self,
        *,
        max_working_facts: int = _DEFAULT_MAX_WORKING_FACTS,
        protected_fact_types: Iterable[str] = _DEFAULT_PROTECTED_FACT_TYPES,
    ) -> None:
        """Initialise an empty working memory.

        Args:
            max_working_facts: Soft cap on the number of derived (non-protected)
                facts. Crossing it logs a one-time warning. ``0`` or negative
                disables the cap entirely. Keyword-only.
            protected_fact_types: Fact types exempt from the cap and from FIFO
                eviction (structural facts the engine relies on, e.g.
                ``tool_call``). Keyword-only.
        """
        self._facts: dict[int, Fact] = {}
        self._by_type: dict[str, list[int]] = {}
        self._next_id = 1
        self._max_working_facts = int(max_working_facts)
        self._protected_fact_types: frozenset[str] = frozenset(protected_fact_types)
        # One-time latch so the soft-cap warning fires once per crossing, not
        # on every subsequent assertion above the threshold.
        self._cap_warned = False

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
        self._enforce_cap()
        return stored

    def _derived_fact_count(self) -> int:
        """Count facts whose type is not protected (subject to the cap)."""
        total = 0
        for fact_type, ids in self._by_type.items():
            if fact_type not in self._protected_fact_types:
                total += len(ids)
        return total

    def _enforce_cap(self) -> None:
        """Warn (once) and FIFO-evict oldest derived facts when over the cap.

        Structural / protected facts (``protected_fact_types``) are never
        counted toward the cap nor evicted, so the cross-call rule semantics
        the feature relies on stay intact. The cap is a *soft* bound: the
        first crossing logs a single warning; only when the derived-fact
        count runs the eviction margin (2x the cap) past the cap do we drop
        the oldest derived facts to keep matcher latency bounded.
        """
        if self._max_working_facts <= 0:
            return
        derived = self._derived_fact_count()
        if derived <= self._max_working_facts:
            # Re-arm the latch once we drop back under the cap so a later
            # crossing warns again.
            self._cap_warned = False
            return
        if not self._cap_warned:
            self._cap_warned = True
            logger.warning(
                "expert_engine working memory exceeded soft cap: %d derived "
                "fact(s) over max_working_facts=%d. assert_fact-derived facts "
                "accumulate across tool calls; review the rulebook for facts "
                "that should be retracted. Oldest derived facts will be "
                "FIFO-evicted past %d.",
                derived,
                self._max_working_facts,
                self._max_working_facts * 2,
            )
        # FIFO-evict only when we run well past the cap, so a steady-state
        # workload that sits just over the cap keeps all its facts (and the
        # warning) without churn. Evict oldest (lowest-id) derived facts first.
        eviction_ceiling = self._max_working_facts * 2
        if derived <= eviction_ceiling:
            return
        to_evict = derived - self._max_working_facts
        for fid in self._oldest_derived_ids(to_evict):
            self.retract(fid)

    def _oldest_derived_ids(self, count: int) -> list[int]:
        """Return up to *count* lowest-id (oldest) non-protected fact ids."""
        if count <= 0:
            return []
        derived_ids: list[int] = []
        for fact_type, ids in self._by_type.items():
            if fact_type not in self._protected_fact_types:
                derived_ids.extend(ids)
        derived_ids.sort()
        return derived_ids[:count]

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
        self._cap_warned = False

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
