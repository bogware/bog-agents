"""Per-agent cost ledger + runaway caps (#25).

Attributes token cost per sub-context — subagent, worktree, or teammate — so a
parallel run shows *where the money went*, not just a session total; and
enforces session-wide caps on subagent spawns, web searches, and total spend.
As bog leans into parallelism (best-of-N, agent teams), this is a liability
shield, not polish: one runaway orchestrator shouldn't be able to fan out
unbounded paid work.

Pure and thread-safe: a `CostLedger` owns one `CostTracker` per labelled agent
(so pricing goes through the CTX-3-fixed `price_for_model`), aggregates totals,
and answers cap questions. Wiring — subagent middleware calling
`register_subagent_spawn`, the web-search tool calling `register_web_search`,
`wrap_model_call` consulting `check_cost` — lives at the call sites.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from bog_agents.middleware.cost_tracker import CostTracker, price_for_model


@dataclass
class RunawayCaps:
    """Session-wide ceilings. `None` on a field means that dimension is uncapped.

    Attributes:
        max_subagents: Total subagent/teammate spawns allowed this session.
        max_web_searches: Total web searches allowed this session.
        max_cost_usd: Total estimated spend allowed across all agents.
    """

    max_subagents: int | None = None
    max_web_searches: int | None = None
    max_cost_usd: float | None = None


@dataclass(frozen=True)
class CostEstimate:
    """A pre-flight projection for a burst of `agents` full agent runs (ROADMAP #51).

    Attributes:
        agents: Number of agent runs projected.
        model: The model spec the projection was priced against.
        low_usd: Optimistic total (light context, short answers).
        high_usd: Pessimistic total (heavy context, long answers).
        priced: `False` when the model has no price on file — the dollar
            figures are then zero and the caller should say so.
    """

    agents: int
    model: str
    low_usd: float
    high_usd: float
    priced: bool

    def format(self) -> str:
        """Render a one-line summary for a confirmation prompt."""
        label = self.model or "the active model"
        if not self.priced:
            return f"{self.agents} agent run(s) on {label}: no price on file for this model"
        return f"{self.agents} agent run(s) on {label}: projected ${self.low_usd:.2f}-${self.high_usd:.2f}"


def estimate_run_cost(
    agents: int,
    model_name: str,
    *,
    input_tokens_per_agent: tuple[int, int] = (60_000, 250_000),
    output_tokens_per_agent: tuple[int, int] = (4_000, 20_000),
) -> CostEstimate:
    """Project the cost of `agents` full agent runs on `model_name`.

    The per-agent token bands are deliberately wide — a coding agent run
    spans a few reads to a long tool loop — so the result is a bracket to
    confirm against, not an invoice.

    Args:
        agents: Number of agent runs (a team's workers, `/best-of-n`'s N, …).
        model_name: Model spec; priced through `price_for_model`.
        input_tokens_per_agent: `(low, high)` input tokens per run.
        output_tokens_per_agent: `(low, high)` output tokens per run.

    Returns:
        The `CostEstimate`.
    """
    agents = max(0, int(agents))
    price = price_for_model(model_name or "")
    if price is None or agents == 0:
        return CostEstimate(agents=agents, model=model_name, low_usd=0.0, high_usd=0.0, priced=price is not None)
    per_in, per_out = price
    low = agents * (input_tokens_per_agent[0] * per_in + output_tokens_per_agent[0] * per_out) / 1_000_000
    high = agents * (input_tokens_per_agent[1] * per_in + output_tokens_per_agent[1] * per_out) / 1_000_000
    return CostEstimate(agents=agents, model=model_name, low_usd=round(low, 4), high_usd=round(high, 4), priced=True)


@dataclass(frozen=True)
class CapDecision:
    """The result of a cap check: allow the action, or deny it with a reason."""

    allowed: bool
    reason: str = ""


class CostLedger:
    """Aggregates per-agent `CostTracker`s and enforces `RunawayCaps`.

    Thread-safe so parallel worktrees/teammates can record and check caps
    concurrently. Cap counters (spawns, searches) are monotonic for the session;
    cost is derived live from the sub-trackers.
    """

    def __init__(self, *, caps: RunawayCaps | None = None) -> None:
        """Create a ledger, optionally with runaway `caps` (uncapped by default)."""
        self.caps = caps or RunawayCaps()
        self._trackers: dict[str, CostTracker] = {}
        self._subagent_spawns = 0
        self._web_searches = 0
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # Attribution
    # ------------------------------------------------------------------ #

    def tracker_for(self, label: str, *, model_name: str = "") -> CostTracker:
        """Get (or lazily create) the cost tracker for agent `label`.

        Args:
            label: A stable name for the sub-context, e.g. ``"main"``,
                ``"worker-1"``, ``"reviewer"``.
            model_name: Model this agent runs (used for pricing); only applied
                when the tracker is first created.

        Returns:
            The shared `CostTracker` for `label`.
        """
        with self._lock:
            tracker = self._trackers.get(label)
            if tracker is None:
                tracker = CostTracker(model_name=model_name)
                self._trackers[label] = tracker
            return tracker

    @property
    def total_cost_usd(self) -> float:
        """Summed estimated spend across every tracked agent."""
        with self._lock:
            return sum(t.estimated_cost_usd for t in self._trackers.values())

    @property
    def total_tokens(self) -> int:
        """Summed token usage across every tracked agent."""
        with self._lock:
            return sum(t.total_tokens for t in self._trackers.values())

    @property
    def subagent_spawns(self) -> int:
        """How many subagent/teammate spawns have been registered."""
        return self._subagent_spawns

    @property
    def web_searches(self) -> int:
        """How many web searches have been registered."""
        return self._web_searches

    # ------------------------------------------------------------------ #
    # Caps
    # ------------------------------------------------------------------ #

    def register_subagent_spawn(self) -> CapDecision:
        """Count a subagent spawn; deny (without counting) once the cap is hit."""
        with self._lock:
            cap = self.caps.max_subagents
            if cap is not None and self._subagent_spawns >= cap:
                return CapDecision(False, f"subagent spawn cap reached ({cap} this session)")
            self._subagent_spawns += 1
            return CapDecision(True)

    def register_web_search(self) -> CapDecision:
        """Count a web search; deny (without counting) once the cap is hit."""
        with self._lock:
            cap = self.caps.max_web_searches
            if cap is not None and self._web_searches >= cap:
                return CapDecision(False, f"web-search cap reached ({cap} this session)")
            self._web_searches += 1
            return CapDecision(True)

    def check_cost(self) -> CapDecision:
        """Allow/deny further paid work based on the total-cost cap (no counter)."""
        cap = self.caps.max_cost_usd
        if cap is not None and self.total_cost_usd >= cap:
            return CapDecision(False, f"cost cap reached (${self.total_cost_usd:.4f} of ${cap:.2f})")
        return CapDecision(True)

    # ------------------------------------------------------------------ #
    # Rendering
    # ------------------------------------------------------------------ #

    def format_tree(self) -> str:
        """Render a per-agent cost/token breakdown, most expensive first."""
        with self._lock:
            rows = sorted(self._trackers.items(), key=lambda kv: kv[1].estimated_cost_usd, reverse=True)
            total_cost = sum(t.estimated_cost_usd for t in self._trackers.values())
            total_tokens = sum(t.total_tokens for t in self._trackers.values())
        lines = ["## Cost by agent", ""]
        for label, tracker in rows:
            lines.append(f"- {label}: ${tracker.estimated_cost_usd:.4f}  ({tracker.total_tokens:,} tok, {tracker.total_requests} req)")
        lines.append("")
        lines.append(f"**Total: ${total_cost:.4f} across {total_tokens:,} tokens**")
        caps = []
        if self.caps.max_subagents is not None:
            caps.append(f"subagents {self._subagent_spawns}/{self.caps.max_subagents}")
        if self.caps.max_web_searches is not None:
            caps.append(f"web searches {self._web_searches}/{self.caps.max_web_searches}")
        if self.caps.max_cost_usd is not None:
            caps.append(f"spend ${total_cost:.2f}/${self.caps.max_cost_usd:.2f}")
        if caps:
            lines.append("")
            lines.append("Caps: " + ", ".join(caps))
        return "\n".join(lines)
