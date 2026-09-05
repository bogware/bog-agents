"""Cost certainty (ROADMAP #51): caps, budgets that pause, daily ceilings, pre-flight.

Pure-logic controller behind `/cost`, the turn gate and the parallel-run
confirmations, kept out of `app.py` so it unit-tests without the TUI:

- `load_cost_caps` reads the `cost.*` manifest keys (config.toml / env).
- `build_cost_ledger` turns them into the SDK `CostLedger` every CLI agent gets.
- `record_turn_spend` / `daily_gate` keep the durable `SpendLedger`
  (`~/.bog-agents/spend.db`, beside `sessions.db`) and enforce the daily ceiling.
- `ask_budget_raise` resolves a `budget_reached` pause through the existing
  ask-user widget; `handle_cost_subcommand` implements `/cost budget|caps|today`.
- `preflight_start` shows the projected bracket for `/team run`,
  `/butcher` and `/best-of-n` before spawning when it crosses the threshold.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bog_agents.cost_ledger import CostLedger, RunawayCaps, estimate_run_cost
from bog_agents.middleware.cost_tracker import price_for_model
from bog_agents.spend_ledger import (
    SCOPE_USER,
    CeilingState,
    SpendLedger,
    check_ceiling,
    project_scope,
)

from bog_agents_cli.config_manifest import resolve_option

if TYPE_CHECKING:
    from bog_agents_cli.textual_adapter import SessionStats

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CostCaps:
    """The effective `cost.*` settings for this process.

    Attributes:
        budget_usd: Session cap; `None` is unlimited.
        warn_at_percent: Warn threshold for budget / ceiling.
        daily_ceiling_usd: Per-user daily ceiling; `None` is unlimited.
        max_subagents: Session spawn cap; `None` is unlimited.
        max_web_searches: Session web-search cap; `None` is unlimited.
        preflight_threshold_usd: Confirm threshold for bursts; `None` disables.
    """

    budget_usd: float | None = None
    warn_at_percent: int = 80
    daily_ceiling_usd: float | None = None
    max_subagents: int | None = 8
    max_web_searches: int | None = 50
    preflight_threshold_usd: float | None = 1.0


def _opt(key: str, default: Any) -> Any:  # noqa: ANN401 - typed per option
    try:
        return resolve_option(key)
    except Exception:
        logger.debug(
            "cost: could not resolve %s; using %r", key, default, exc_info=True
        )
        return default


def load_cost_caps() -> CostCaps:
    """Resolve the `cost.*` manifest keys into a `CostCaps`."""
    defaults = CostCaps()
    return CostCaps(
        budget_usd=_opt("cost.budget_usd", defaults.budget_usd),
        warn_at_percent=int(
            _opt("cost.warn_at_percent", defaults.warn_at_percent) or 0
        ),
        daily_ceiling_usd=_opt("cost.daily_ceiling_usd", defaults.daily_ceiling_usd),
        max_subagents=_opt("cost.max_subagents", defaults.max_subagents),
        max_web_searches=_opt("cost.max_web_searches", defaults.max_web_searches),
        preflight_threshold_usd=_opt(
            "cost.preflight_threshold_usd", defaults.preflight_threshold_usd
        ),
    )


def build_cost_ledger(caps: CostCaps) -> CostLedger:
    """Build the session `CostLedger` whose runaway caps gate spawns, searches and spend."""
    return CostLedger(
        caps=RunawayCaps(
            max_subagents=caps.max_subagents,
            max_web_searches=caps.max_web_searches,
            max_cost_usd=caps.budget_usd,
        )
    )


# ------------------------------------------------------------------ spend ledger

_SPEND_LEDGER: SpendLedger | None = None
_LAST_GATE_STATE: dict[str, CeilingState] = {}


def spend_db_path() -> Path:
    """Path of the durable spend ledger, beside the checkpointer's `sessions.db`."""
    from bog_agents_cli._env_vars import bog_agents_home

    return bog_agents_home() / "spend.db"


def get_spend_ledger() -> SpendLedger:
    """Return the process-wide `SpendLedger` (opened lazily)."""
    global _SPEND_LEDGER  # noqa: PLW0603 - one handle per process
    if _SPEND_LEDGER is None:
        _SPEND_LEDGER = SpendLedger(spend_db_path())
    return _SPEND_LEDGER


def reset_spend_ledger() -> None:
    """Drop the cached ledger handle and gate memory (tests)."""
    global _SPEND_LEDGER  # noqa: PLW0603
    if _SPEND_LEDGER is not None:
        _SPEND_LEDGER.close()
    _SPEND_LEDGER = None
    _LAST_GATE_STATE.clear()


def project_key(cwd: str | Path) -> str:
    """Stable per-project key (hash of the resolved path)."""
    return hashlib.sha256(str(Path(cwd).resolve()).encode("utf-8")).hexdigest()[:12]


def turn_cost_usd(stats: SessionStats) -> float:
    """Price a turn's per-model token counts; unpriced models count as `0`."""
    total = 0.0
    for model_name, model_stats in stats.per_model.items():
        price = price_for_model(model_name)
        if price is None:
            continue
        total += (
            model_stats.input_tokens * price[0] + model_stats.output_tokens * price[1]
        ) / 1_000_000
    return total


def record_turn_spend(
    stats: SessionStats,
    *,
    cwd: str | Path,
    ledger: SpendLedger | None = None,
    now: float | None = None,
) -> float:
    """Record one finished turn in the spend ledger under the user and project scopes.

    Args:
        stats: The turn's `SessionStats` (per-model tokens).
        cwd: The project the turn ran in.
        ledger: Override ledger (tests); defaults to the process ledger.
        now: Timestamp override (tests).

    Returns:
        The dollars recorded (`0.0` when nothing was priced or recording failed).
    """
    try:
        usd = turn_cost_usd(stats)
        if usd <= 0:
            return 0.0
        target = ledger or get_spend_ledger()
        models = ",".join(sorted(stats.per_model)) if stats.per_model else ""
        for scope in (SCOPE_USER, project_scope(project_key(cwd))):
            target.record(
                scope,
                usd,
                model=models,
                input_tokens=stats.input_tokens,
                output_tokens=stats.output_tokens,
                now=now,
            )
    except Exception:
        logger.debug("cost: failed to record turn spend", exc_info=True)
        return 0.0
    return usd


def daily_gate(
    *,
    cwd: str | Path,
    caps: CostCaps | None = None,
    ledger: SpendLedger | None = None,
    now: float | None = None,
) -> tuple[bool, str | None]:
    """Decide whether a new turn may start under the daily ceiling.

    Args:
        cwd: The project (only the user scope is gated today; the project
            scope is recorded for `/cost today`).
        caps: Override caps (tests); defaults to `load_cost_caps()`.
        ledger: Override ledger (tests).
        now: Timestamp override (tests).

    Returns:
        `(blocked, note)`: `blocked` is `True` once the ceiling is reached;
        `note` carries a warning the first time a state is entered (so a
        warning shows once, not on every turn), or the refusal text.
    """
    del cwd  # reserved for a per-project ceiling
    caps = caps or load_cost_caps()
    if not caps.daily_ceiling_usd:
        return False, None
    try:
        spent = (ledger or get_spend_ledger()).total_usd(SCOPE_USER, now=now)
    except Exception:
        logger.debug("cost: could not read the spend ledger; gate open", exc_info=True)
        return False, None
    status = check_ceiling(
        spent,
        caps.daily_ceiling_usd,
        warn_at_percent=caps.warn_at_percent,
        label="daily",
    )
    if status.state == "reached":
        return True, (
            f"{status.message}. New turns are paused for today; raise `cost.daily_ceiling_usd` "
            "(config.toml or BOG_AGENTS_DAILY_CEILING_USD) to continue."
        )
    previous = _LAST_GATE_STATE.get(SCOPE_USER)
    _LAST_GATE_STATE[SCOPE_USER] = status.state
    if status.state == "warn" and previous != "warn":
        return False, f"Heads-up: {status.message}."
    return False, None


async def gate_turn(app: Any) -> bool:  # noqa: ANN401 - the App
    """Apply `daily_gate` for the app: mount any note, return `True` when the turn must not start."""
    from bog_agents_cli.widgets.messages import AppMessage

    blocked, note = daily_gate(cwd=getattr(app, "_cwd", "."))
    if note:
        await app._mount_message(AppMessage(note))
    return blocked


# ------------------------------------------------------------------ budget_reached pause


def budget_prompt_questions(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Build the ask-user question for a `budget_reached` pause."""
    spent = float(payload.get("spent_usd") or 0.0)
    budget = payload.get("budget_usd")
    budget_text = (
        f"${float(budget):.2f}" if isinstance(budget, (int, float)) else "the budget"
    )
    return [
        {
            "question": (
                f"Budget reached: ${spent:.4f} spent of {budget_text}. "
                "Enter a new budget in USD to continue this turn (e.g. 5), or cancel to stop here."
            ),
            "type": "text",
            "required": False,
        }
    ]


def parse_budget_answer(result: Any) -> float | None:  # noqa: ANN401 - widget result dict
    """Turn an ask-user widget result into a raised budget, or `None` to stop."""
    from bog_agents.middleware.cost_tracker import parse_budget_resume

    if not isinstance(result, dict) or result.get("type") != "answered":
        return None
    answers = result.get("answers")
    if not isinstance(answers, list) or not answers:
        return None
    return parse_budget_resume(answers[0])


async def ask_budget_raise(
    request_ask_user: Callable[[list[Any]], Awaitable[Any]] | None,
    payload: dict[str, Any],
) -> float | None:
    """Ask the user for a raised budget through the ask-user widget.

    Args:
        request_ask_user: The adapter's `_request_ask_user` callback (returns a
            Future), or `None` in UIs without one.
        payload: The `budget_reached` interrupt payload.

    Returns:
        The new budget in USD, or `None` when the user declined / no UI.
    """
    if request_ask_user is None:
        return None
    try:
        future = await request_ask_user(budget_prompt_questions(payload))
        result = await future if future is not None else None
    except Exception:
        logger.debug("cost: budget prompt failed", exc_info=True)
        return None
    return parse_budget_answer(result)


def budget_stop_message(payload: dict[str, Any]) -> str:
    """Message shown when a budget pause is not raised."""
    spent = float(payload.get("spent_usd") or 0.0)
    return (
        f"Budget reached (${spent:.4f} spent); the turn was stopped. "
        "Use `/cost budget <N>` to raise it for the next turn, or `/cost budget off` to lift it."
    )


# ------------------------------------------------------------------ /cost


async def run_cost_command(app: Any, command: str) -> None:  # noqa: ANN401 - the App
    """Body of `/tokens` / `/cost`: explain (tracked), subcommands, or the usage report."""
    from bog_agents_cli.widgets.messages import AppMessage

    if await maybe_run_cost_explain(app, command):
        return
    handled = handle_cost_subcommand(app, command)
    if handled is not None:
        await app._mount_message(AppMessage(handled))
        return
    tracker = getattr(app, "_token_tracker", None)
    has_usage = bool(tracker and getattr(tracker, "current_context", 0) > 0)
    conv_tokens = await app._get_conversation_token_count() if has_usage else None
    await app._mount_message(AppMessage(render_tokens_report(app, conv_tokens)))


async def maybe_run_cost_explain(app: Any, command: str) -> bool:  # noqa: ANN401 - the App
    """Run `/cost explain <question>` as a tracked model command; `False` for every other verb."""
    if command.lower().split()[1:2] != ["explain"]:
        return False
    from bog_agents_cli.usage_controller import run_cost_explain

    await app._start_model_command(run_cost_explain(app, command), name="/cost explain")
    return True


def handle_cost_subcommand(app: Any, command: str) -> str | None:  # noqa: ANN401 - the App
    """Handle `/cost budget <N|off>`, `/cost caps`, `/cost today`; `None` for the plain report."""
    from bog_agents_cli.config import settings

    parts = command.strip().split()
    if len(parts) < 2:
        return None
    verb = parts[1].lower()
    if verb == "budget":
        if len(parts) < 3:
            current = getattr(app, "_budget_override", None)
            return f"Session budget: {'unlimited' if not current else f'${current:.2f}'}. Usage: /cost budget <USD> | off"
        raw = parts[2].lower()
        if raw in {"off", "none", "unlimited", "0"}:
            app._budget_override = 0.0
            return "Session budget lifted (takes effect from the next turn)."
        try:
            value = float(raw.lstrip("$"))
        except ValueError:
            return f"Invalid budget {parts[2]!r}. Usage: /cost budget <USD> | off"
        if value <= 0:
            return "Budget must be positive; use `/cost budget off` to lift it."
        app._budget_override = value
        return f"Session budget set to ${value:.2f} (takes effect from the next turn; a paused turn asks you to raise it inline)."
    if verb == "caps":
        caps = load_cost_caps()
        lines = ["Cost caps (config.toml [cost] / BOG_AGENTS_* env):"]
        lines.append(f"  budget_usd:              {caps.budget_usd or 'unlimited'}")
        lines.append(
            f"  daily_ceiling_usd:       {caps.daily_ceiling_usd or 'unlimited'}"
        )
        lines.append(f"  warn_at_percent:         {caps.warn_at_percent}")
        lines.append(
            f"  max_subagents:           {caps.max_subagents if caps.max_subagents is not None else 'unlimited'}"
        )
        lines.append(
            f"  max_web_searches:        {caps.max_web_searches if caps.max_web_searches is not None else 'unlimited'}"
        )
        lines.append(
            f"  preflight_threshold_usd: {caps.preflight_threshold_usd if caps.preflight_threshold_usd is not None else 'off'}"
        )
        lines.append(
            f"  active model:            {getattr(app, '_model_override', None) or settings.model_name or '(unset)'}"
        )
        return "\n".join(lines)
    if verb == "tree":
        from bog_agents_cli.usage_controller import get_usage_ledger

        return get_usage_ledger(app).format_tree()
    if verb == "cache":
        from bog_agents_cli.usage_controller import cache_report_for_app

        return cache_report_for_app(app)
    if verb == "today":
        try:
            totals = get_spend_ledger().totals_by_scope()
        except Exception:
            return "Spend ledger unavailable."
        if not totals:
            return "No spend recorded today."
        rows = "\n".join(
            f"  {scope}: ${usd:.4f}" for scope, usd in sorted(totals.items())
        )
        return f"Spend today:\n{rows}"
    return None


def render_tokens_report(app: Any, conv_tokens: int | None) -> str:  # noqa: ANN401 - the App
    """Render the `/tokens` (`/cost`) report: context usage, session spend, budget, ceiling."""
    from bog_agents_cli.config import settings
    from bog_agents_cli.textual_adapter import format_token_count

    tracker = getattr(app, "_token_tracker", None)
    model_name = settings.model_name
    context_limit = settings.model_context_limit
    if tracker and tracker.current_context > 0:
        count = tracker.current_context
        formatted = format_token_count(count)
        if context_limit is not None:
            limit_str = format_token_count(context_limit)
            usage = (
                f"{formatted} / {limit_str} tokens ({count / context_limit * 100:.0f}%)"
            )
        else:
            usage = f"{formatted} tokens used"
        msg = f"{usage} | {model_name}" if model_name else usage
        if conv_tokens is not None:
            overhead = max(0, count - conv_tokens)
            overhead_unit = " tokens" if overhead < 1000 else ""
            conv_unit = " tokens" if conv_tokens < 1000 else ""
            msg += (
                f"\n|- System prompt + tools: ~{format_token_count(overhead)}{overhead_unit} (fixed)"
                f"\n`- Conversation: ~{format_token_count(conv_tokens)}{conv_unit}"
            )
    else:
        parts: list[str] = ["No token usage yet"]
        if context_limit is not None:
            parts.append(f"{format_token_count(context_limit)} token context window")
        if model_name:
            parts.append(model_name)
        msg = " | ".join(parts)
    msg += "\n" + _spend_lines(app)
    return msg


def _spend_lines(app: Any) -> str:  # noqa: ANN401 - the App
    stats = getattr(app, "_session_stats", None)
    session_usd = (
        turn_cost_usd(stats)
        if stats is not None and hasattr(stats, "per_model")
        else 0.0
    )
    caps = load_cost_caps()
    override = getattr(app, "_budget_override", None)
    if override is not None:
        budget_text = (
            "unlimited (/cost budget)"
            if override <= 0
            else f"${override:.2f} (/cost budget)"
        )
    elif caps.budget_usd:
        budget_text = f"${caps.budget_usd:.2f} (cost.budget_usd)"
    else:
        budget_text = "unlimited"
    lines = [f"Session spend: ${session_usd:.4f} | budget: {budget_text}"]
    from bog_agents_cli.usage_controller import UsageLedger

    ledger = getattr(app, "_usage_ledger", None)
    if isinstance(ledger, UsageLedger) and ledger.records:
        ratio = ledger.cache_hit_ratio
        lines.append(
            f"Responses: {len(ledger.records)} | priced ${ledger.usd:.4f} | cache hit "
            f"{'n/a' if ratio is None else f'{ratio * 100:.0f}%'} (/cost tree, /cost cache, /cost explain <q>)"
        )
    if caps.daily_ceiling_usd:
        try:
            spent_today = get_spend_ledger().total_usd(SCOPE_USER)
        except Exception:
            spent_today = 0.0
        status = check_ceiling(
            spent_today, caps.daily_ceiling_usd, warn_at_percent=caps.warn_at_percent
        )
        lines.append(
            f"Today: ${spent_today:.2f} of ${caps.daily_ceiling_usd:.2f} daily ceiling ({status.state})"
        )
    lines.append(
        "/cost budget <N|off> | /cost caps | /cost today | /cost tree | /cost cache | /cost explain <question>"
    )
    return "\n".join(lines)


# ------------------------------------------------------------------ pre-flight


def preflight_message(agents: int, model_spec: str, caps: CostCaps) -> list[str] | None:
    """Return the confirmation lines when a burst's projected spend crosses the threshold, else `None`."""
    if caps.preflight_threshold_usd is None:
        return None
    est = estimate_run_cost(agents, model_spec)
    if not est.priced or est.high_usd < caps.preflight_threshold_usd:
        return None
    lines = [est.format()]
    if caps.budget_usd:
        lines.append(
            f"Session budget: ${caps.budget_usd:.2f} (a run that hits it pauses and asks)."
        )
    if caps.max_subagents is not None:
        lines.append(
            f"Spawn cap this session: {caps.max_subagents} (cost.max_subagents)."
        )
    lines.append(
        f"Threshold: ${caps.preflight_threshold_usd:.2f} (cost.preflight_threshold_usd; 'off' disables this prompt)."
    )
    return lines


def preflight_start(
    app: Any,  # noqa: ANN401 - the App
    *,
    agents: int,
    name: str,
    start: Callable[[], Coroutine[Any, Any, None]],
    caps: CostCaps | None = None,
) -> None:
    """Start a tracked parallel session, confirming first when the projection crosses the threshold.

    Args:
        app: The `BogAgentsApp` (needs `_start_tracked_session`, `push_screen`, `_mount_message`).
        agents: How many full agent runs the session will spawn.
        name: The tracked-session name (`/team run`, `/butcher`, `/best-of-n`).
        start: Factory for the session coroutine (created only when confirmed,
            so a cancelled prompt never leaves an un-awaited coroutine).
        caps: Override caps (tests).
    """
    from bog_agents_cli.config import settings

    model_spec = getattr(app, "_model_override", None) or settings.model_name or ""
    lines = preflight_message(agents, model_spec, caps or load_cost_caps())
    if lines is None:
        app._start_tracked_session(start(), name=name)
        return
    from bog_agents_cli.widgets.messages import AppMessage
    from bog_agents_cli.widgets.preflight_confirm import PreflightConfirmScreen

    def _on_decision(confirmed: bool | None) -> None:
        if confirmed:
            app._start_tracked_session(start(), name=name)
        else:
            app.run_worker(
                app._mount_message(
                    AppMessage(f"{name} cancelled at the cost pre-flight.")
                ),
                exclusive=False,
            )

    app.push_screen(PreflightConfirmScreen(name, lines), _on_decision)
