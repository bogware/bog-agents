"""Causal-replay subsystem (trace-mind).

Records the *causal graph* of an agent run — which tool calls produced
which outputs, which rules fired on which facts, which dreams biased
which decisions — so the user can answer "why did the agent do that?"
after the fact. The pieces are arranged so the data layer is usable
without the middleware or the slash commands, and the middleware is
usable without the rendering layer.

Modules
-------

* :mod:`.ledger` — pure data model + JSONL persistence.
* :mod:`.middleware` — LangChain middleware that records causal events
  during a normal agent run.
* :mod:`.controller` — slash-command facing facade
  (``/causal status``, ``/causal last``, ``/causal why <id>``).
* :mod:`.render` — text rendering for the TUI.

The on-disk format is one JSON object per line under
``<cwd>/.bog-agents/causal/<session_id>.jsonl``. Sessions are
self-contained so a user who archives an old session can still replay
it after pruning the rest.
"""

from __future__ import annotations

from bog_agents_cli.causal.ledger import (
    CausalEvent,
    CausalLedger,
    EventKind,
    load_session,
    open_session,
)

__all__ = [
    "CausalEvent",
    "CausalLedger",
    "EventKind",
    "load_session",
    "open_session",
]
