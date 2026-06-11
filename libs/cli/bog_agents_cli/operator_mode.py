"""Operator mode — a judge model routes each prompt to a mapped model tier.

The user picks an *operator* (judge) model. When the mode is on, every
plain user prompt is first shown to the judge, which classifies the work
as ``easy`` / ``medium`` / ``hard`` / ``max`` and (optionally) escalates
to a specialised handler (``butcher`` decomposition or ``jtbd``). The
classified tier maps to a model + effort level via the active *preset* —
built-ins favour Anthropic API models (default), Bedrock, local Ollama,
or a hybrid — and users can define their own presets in
``~/.bog-agents/operator.toml``.

Design notes:

* The routed model rides the existing per-call override rail
  (``ConfigurableModelMiddleware`` reads ``CLIContext.model`` /
  ``CLIContext.effort_level``) — no new SDK middleware, no graph rebuild.
* The judge must never break a turn: any judge failure (timeout, bad
  JSON, missing model) falls through to the user's current model.
* Pure-logic module: the judge call is injected so unit tests drive it
  without a live LLM. CLI wiring stays in ``handle_operator_subcommand``
  and the ``apply_operator_routing`` seam helper.

Resolution order for the master switch (highest priority first):

1. ``BOG_AGENTS_OPERATOR_DISABLE=1`` — emergency kill, beats everything.
2. ``/operator on`` / ``/operator off`` — session toggle.
3. ``BOG_AGENTS_OPERATOR=1|0`` — env default.
4. ``enabled`` in ``~/.bog-agents/operator.toml``.
5. Built-in default (off).
"""

from __future__ import annotations

import logging
import os
import re
import time
import tomllib
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bog_agents_cli.feature_helpers import (
    extract_json_object,
    feature_state_dir,
    invoke_model,
    resolve_active_model_spec,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)


TIER_NAMES: tuple[str, ...] = ("easy", "medium", "hard", "max")
"""Tier vocabulary, cheapest first. Order matters for escalation ladders."""

ROUTE_NAMES: tuple[str, ...] = ("direct", "butcher", "jtbd")
"""Where the judge may send a prompt. ``direct`` = normal agent turn."""

_CONFIG_NAME = "operator.toml"
_ENV_MASTER = "BOG_AGENTS_OPERATOR"
_ENV_DISABLE = "BOG_AGENTS_OPERATOR_DISABLE"
_JUDGE_TIMEOUT_SECONDS = 25.0
_PROMPT_PREVIEW_CHARS = 6_000
_DECISION_LOG_SIZE = 50


def is_emergency_disabled() -> bool:
    """Cheap kill-switch check, safe for the per-prompt hot path."""
    return os.environ.get(_ENV_DISABLE, "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


# ---------------------------------------------------------------------------
# Tiers and presets
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TierSpec:
    """One tier's destination: a model spec plus an effort level.

    Attributes:
        model: ``provider:model`` spec (e.g. ``anthropic:claude-haiku-4-5``).
        effort: One of ``low`` / ``medium`` / ``high`` / ``max`` — applied
            through the existing ``CLIContext.effort_level`` rail.
    """

    model: str
    effort: str = "medium"


BUILTIN_PRESETS: dict[str, dict[str, TierSpec]] = {
    "anthropic": {
        "easy": TierSpec("anthropic:claude-haiku-4-5", "low"),
        "medium": TierSpec("anthropic:claude-sonnet-4-6", "medium"),
        "hard": TierSpec("anthropic:claude-opus-4-6", "high"),
        "max": TierSpec("anthropic:claude-opus-4-6", "max"),
    },
    "bedrock": {
        # Bare ids — the SDK's bedrock normaliser adds the regional
        # inference-profile prefix (us./eu./…) from AWS_REGION at resolve time.
        "easy": TierSpec("bedrock:anthropic.claude-haiku-4-5", "low"),
        "medium": TierSpec("bedrock:anthropic.claude-sonnet-4-6", "medium"),
        "hard": TierSpec("bedrock:anthropic.claude-opus-4-6", "high"),
        "max": TierSpec("bedrock:anthropic.claude-opus-4-6", "max"),
    },
    "local": {
        # Tool-capable local defaults; users with bigger cards override these.
        "easy": TierSpec("ollama:llama3.2", "low"),
        "medium": TierSpec("ollama:qwen3-coder-next:latest", "medium"),
        "hard": TierSpec("ollama:qwen3-coder-next:latest", "high"),
        "max": TierSpec("ollama:qwen3-coder-next:latest", "max"),
    },
    "hybrid": {
        # Local for the cheap end, cloud where it counts.
        "easy": TierSpec("ollama:llama3.2", "low"),
        "medium": TierSpec("ollama:qwen3-coder-next:latest", "medium"),
        "hard": TierSpec("anthropic:claude-sonnet-4-6", "high"),
        "max": TierSpec("anthropic:claude-opus-4-6", "max"),
    },
}

DEFAULT_PRESET = "anthropic"


@dataclass
class OperatorConfig:
    """Persisted operator settings (``~/.bog-agents/operator.toml``)."""

    enabled: bool = False
    """Start sessions with the operator on. ``/operator on|off`` overrides."""

    judge_model: str = ""
    """Model spec for the judge. Empty = the active preset's ``easy`` model."""

    preset: str = DEFAULT_PRESET
    """Which preset's tier map to use (built-in or ``[presets.<name>]``)."""

    routes: bool = True
    """Allow the judge to escalate to ``butcher`` / ``jtbd``."""

    custom_presets: dict[str, dict[str, TierSpec]] = field(default_factory=dict)
    """User-defined presets from ``[presets.<name>.<tier>]`` tables."""

    tier_overrides: dict[str, TierSpec] = field(default_factory=dict)
    """Per-tier overrides from ``[tiers.<tier>]`` applied on top of the preset."""


def operator_config_path() -> Path:
    """Return ``~/.bog-agents/operator.toml``."""
    return feature_state_dir() / _CONFIG_NAME


def _parse_tier_table(raw: object) -> TierSpec | None:
    """Coerce one ``{model = …, effort = …}`` table into a TierSpec."""
    if not isinstance(raw, dict):
        return None
    model = raw.get("model")
    if not isinstance(model, str) or not model.strip():
        return None
    effort = raw.get("effort", "medium")
    if not isinstance(effort, str) or effort not in {"low", "medium", "high", "max"}:
        effort = "medium"
    return TierSpec(model=model.strip(), effort=effort)


def load_operator_config(path: Path | None = None) -> OperatorConfig:
    """Load operator config, falling back to defaults on missing/malformed."""
    target = path or operator_config_path()
    cfg = OperatorConfig()
    data: dict[str, Any] = {}
    if target.exists():
        try:
            data = tomllib.loads(target.read_text(encoding="utf-8"))
        except (tomllib.TOMLDecodeError, OSError):
            logger.warning(
                "Failed to parse operator.toml; using defaults", exc_info=True
            )
            data = {}
    if isinstance(data.get("enabled"), bool):
        cfg.enabled = data["enabled"]
    if isinstance(data.get("judge_model"), str):
        cfg.judge_model = data["judge_model"].strip()
    if isinstance(data.get("preset"), str) and data["preset"].strip():
        cfg.preset = data["preset"].strip()
    if isinstance(data.get("routes"), bool):
        cfg.routes = data["routes"]
    presets = data.get("presets")
    if isinstance(presets, dict):
        for name, tiers in presets.items():
            if not isinstance(tiers, dict):
                continue
            parsed = {
                t: spec
                for t in TIER_NAMES
                if (spec := _parse_tier_table(tiers.get(t))) is not None
            }
            if parsed:
                cfg.custom_presets[str(name)] = parsed
    tiers_raw = data.get("tiers")
    if isinstance(tiers_raw, dict):
        for t in TIER_NAMES:
            spec = _parse_tier_table(tiers_raw.get(t))
            if spec is not None:
                cfg.tier_overrides[t] = spec
    # Env default beats the file but loses to the session toggle (handled by caller).
    env = os.environ.get(_ENV_MASTER, "").strip().lower()
    if env in {"1", "true", "yes", "on"}:
        cfg.enabled = True
    elif env in {"0", "false", "no", "off"}:
        cfg.enabled = False
    return cfg


def write_default_config(path: Path | None = None) -> Path:
    """Bootstrap a commented starter ``operator.toml``. Returns the path."""
    target = path or operator_config_path()
    if target.exists():
        return target
    body = (
        "# Operator mode — judge-model prompt routing.\n"
        "# Docs: /operator in the CLI. Tier vocabulary: easy / medium / hard / max.\n\n"
        "enabled = false\n"
        'judge_model = ""   # empty = the active preset\'s easy-tier model\n'
        f'preset = "{DEFAULT_PRESET}"  # anthropic | bedrock | local | hybrid | <your own>\n'
        "routes = true        # judge may escalate to butcher / jtbd\n\n"
        "# Define your own preset:\n"
        "# [presets.mine.easy]\n"
        '# model = "ollama:llama3.2"\n'
        '# effort = "low"\n'
        "# [presets.mine.max]\n"
        '# model = "anthropic:claude-opus-4-6"\n'
        '# effort = "max"\n\n'
        "# Or override single tiers of the active preset:\n"
        "# [tiers.hard]\n"
        '# model = "anthropic:claude-sonnet-4-6"\n'
        '# effort = "high"\n'
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(body, encoding="utf-8")
    tmp.replace(target)
    return target


def resolve_tiers(config: OperatorConfig) -> dict[str, TierSpec]:
    """Resolve the effective tier map: preset base + per-tier overrides.

    Unknown preset names fall back to the default preset (with a log line)
    so a typo in the TOML degrades gracefully instead of breaking turns.
    A custom preset only needs to define the tiers it changes; missing
    tiers inherit from the default preset.
    """
    base = dict(BUILTIN_PRESETS[DEFAULT_PRESET])
    source = config.custom_presets.get(config.preset) or BUILTIN_PRESETS.get(
        config.preset
    )
    if source is None:
        logger.warning(
            "Unknown operator preset %r; using %r", config.preset, DEFAULT_PRESET
        )
    else:
        base.update(source)
    base.update(config.tier_overrides)
    return base


# ---------------------------------------------------------------------------
# The judge
# ---------------------------------------------------------------------------


JUDGE_SYSTEM_PROMPT = """You are the OPERATOR — a dispatcher that routes an
incoming request to the right worker model. You do NOT answer the request.

Classify the request into exactly one tier:

* easy   — trivial lookups, one-liners, renames, quick questions, formatting.
* medium — ordinary single-file changes, straightforward bugs, simple scripts.
* hard   — multi-file changes, debugging across layers, design with trade-offs.
* max    — architecture work, gnarly concurrency/correctness problems, large
           refactors, anything where a wrong answer is expensive.

{routes_block}

Reply with STRICT JSON only — no prose, no markdown fence:

{{"tier": "<easy|medium|hard|max>", "route": "<direct|butcher|jtbd>", "reason": "<one short sentence>"}}
"""

_ROUTES_ENABLED_BLOCK = """You may also pick a route:

* direct  — normal handling by the tier's model (the default).
* butcher — the request is a big buildable chunk of work that would benefit
            from being sliced into small, foolproof subtasks first.
* jtbd    — the request is fuzzy about its real goal; understanding the
            underlying job-to-be-done should come before any work.
"""

_ROUTES_DISABLED_BLOCK = 'Always set "route" to "direct".'


@dataclass(frozen=True)
class OperatorDecision:
    """One routing decision, kept in the session log for ``/operator status``."""

    tier: str
    route: str
    reason: str
    model: str
    effort: str
    judge_ms: int
    prompt_preview: str
    forced: bool = False


def parse_judge_response(text: str) -> tuple[str, str, str] | None:
    """Extract ``(tier, route, reason)`` from a judge reply, tolerantly.

    Accepts strict JSON, JSON inside surrounding prose, or — as a last
    resort — a bare tier word anywhere in the reply. Returns None when
    no tier can be recovered (the caller falls through to no routing).
    """
    body = text.strip()
    candidate = extract_json_object(body)
    if candidate is not None:
        tier = str(candidate.get("tier", "")).strip().lower()
        route = str(candidate.get("route", "direct")).strip().lower()
        reason = str(candidate.get("reason", "")).strip()
        if tier in TIER_NAMES:
            if route not in ROUTE_NAMES:
                route = "direct"
            return tier, route, reason
    # Last resort: a bare tier word.
    lowered = body.lower()
    for tier in TIER_NAMES:
        if re.search(rf"\b{tier}\b", lowered):
            return tier, "direct", ""
    return None


async def judge_prompt(
    prompt: str,
    tiers: dict[str, TierSpec],
    *,
    invoke: Callable[[str, str], Awaitable[str]],
    routes_enabled: bool = True,
    forced_tier: str | None = None,
) -> OperatorDecision | None:
    """Run the judge over ``prompt`` and return a decision, or None on failure.

    Args:
        prompt: The user's message (truncated for the judge).
        tiers: Resolved tier map (see :func:`resolve_tiers`).
        invoke: ``async (system_prompt, user_prompt) -> str`` — injected so
            tests run without a live model. The CLI passes a thin wrapper
            around :func:`bog_agents_cli.feature_helpers.invoke_model`.
        routes_enabled: Whether the judge may pick butcher/jtbd.
        forced_tier: Skip the judge entirely and use this tier
            (``/operator force <tier>``).

    Returns:
        An :class:`OperatorDecision`, or None when judging failed — the
        caller must treat None as "no routing this turn".
    """
    preview = prompt.strip()[:_PROMPT_PREVIEW_CHARS]
    if forced_tier is not None:
        if forced_tier not in tiers:
            return None
        spec = tiers[forced_tier]
        return OperatorDecision(
            tier=forced_tier,
            route="direct",
            reason="forced via /operator force",
            model=spec.model,
            effort=spec.effort,
            judge_ms=0,
            prompt_preview=preview[:120],
            forced=True,
        )
    system = JUDGE_SYSTEM_PROMPT.format(
        routes_block=_ROUTES_ENABLED_BLOCK if routes_enabled else _ROUTES_DISABLED_BLOCK
    )
    start = time.monotonic()
    try:
        reply = await invoke(system, preview)
    except Exception:
        logger.warning("Operator judge call failed; routing skipped", exc_info=True)
        return None
    parsed = parse_judge_response(reply)
    if parsed is None:
        logger.warning(
            "Operator judge reply unparseable; routing skipped: %r", reply[:200]
        )
        return None
    tier, route, reason = parsed
    if not routes_enabled:
        route = "direct"
    spec = tiers[tier]
    return OperatorDecision(
        tier=tier,
        route=route,
        reason=reason,
        model=spec.model,
        effort=spec.effort,
        judge_ms=int((time.monotonic() - start) * 1000),
        prompt_preview=preview[:120],
    )


# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------


@dataclass
class OperatorSession:
    """Runtime operator state held on the app for the life of the session."""

    config: OperatorConfig
    tiers: dict[str, TierSpec]
    active: bool
    forced_tier: str | None = None
    decisions: deque[OperatorDecision] = field(
        default_factory=lambda: deque(maxlen=_DECISION_LOG_SIZE)
    )


def ensure_session(app: object) -> OperatorSession:
    """Return the app's operator session, creating it from config on first use."""
    session = getattr(app, "_operator_session", None)
    if isinstance(session, OperatorSession):
        return session
    config = load_operator_config()
    session = OperatorSession(
        config=config, tiers=resolve_tiers(config), active=config.enabled
    )
    app._operator_session = session  # type: ignore[attr-defined]
    return session


def _judge_model_spec(session: OperatorSession, app: object) -> str:
    """The judge's model spec: explicit config, else easy tier, else active model."""
    return (
        session.config.judge_model
        or session.tiers["easy"].model
        or resolve_active_model_spec(app)
    )


async def apply_operator_routing(app: object, message: str) -> OperatorDecision | None:
    """The per-prompt seam: judge ``message`` and stage the turn override.

    Called from ``_handle_user_message`` for plain (non-command) prompts.
    On a ``direct`` decision this sets ``app._operator_turn_model`` /
    ``app._operator_turn_effort`` (consumed by ``_build_cli_context`` and
    cleared after the turn). For ``butcher`` / ``jtbd`` routes the caller
    is responsible for dispatching; this function only decides.

    Never raises; any failure returns None and the turn proceeds untouched.
    """
    if is_emergency_disabled():
        return None
    session = ensure_session(app)
    if not session.active:
        return None
    forced = session.forced_tier
    session.forced_tier = None  # one-shot
    decision: OperatorDecision | None
    if forced is not None:
        decision = await judge_prompt(
            message, session.tiers, invoke=_noop_invoke, forced_tier=forced
        )
    else:
        spec = _judge_model_spec(session, app)
        if not spec:
            return None
        try:
            from bog_agents_cli.config import create_model_with_fallback

            model_result = create_model_with_fallback(
                spec, profile_overrides=getattr(app, "_profile_override", None)
            )
        except Exception:
            logger.warning(
                "Operator judge model %r unavailable; routing skipped",
                spec,
                exc_info=True,
            )
            return None

        async def _invoke(system: str, user: str) -> str:
            return await invoke_model(
                model_result.model, system, user, timeout_seconds=_JUDGE_TIMEOUT_SECONDS
            )

        decision = await judge_prompt(
            message, session.tiers, invoke=_invoke, routes_enabled=session.config.routes
        )
    if decision is None:
        return None
    session.decisions.append(decision)
    if decision.route == "direct":
        app._operator_turn_model = decision.model  # type: ignore[attr-defined]
        app._operator_turn_effort = decision.effort  # type: ignore[attr-defined]
    return decision


async def _noop_invoke(_system: str, _user: str) -> str:  # noqa: RUF029 — must match the async invoke contract
    """Placeholder invoke for forced-tier decisions (never called)."""
    return ""


# ---------------------------------------------------------------------------
# /operator subcommand handling
# ---------------------------------------------------------------------------


def render_status(session: OperatorSession) -> str:
    """Render ``/operator status`` output."""
    lines = [
        "[bold]Operator mode[/bold]",
        f"  state:    {'[green]on[/green]' if session.active else '[red]off[/red]'}"
        + (
            "  [red](emergency-disabled via env)[/red]"
            if is_emergency_disabled()
            else ""
        ),
        f"  preset:   {session.config.preset}",
        f"  judge:    {session.config.judge_model or session.tiers['easy'].model + ' (easy tier)'}",
        f"  routes:   {'butcher/jtbd allowed' if session.config.routes else 'direct only'}",
        "",
        "  [bold]Tier map[/bold]",
    ]
    lines.extend(
        f"    {t:<7}→ {session.tiers[t].model}  (effort: {session.tiers[t].effort})"
        for t in TIER_NAMES
    )
    if session.forced_tier:
        lines.append(f"\n  next turn forced to: [bold]{session.forced_tier}[/bold]")
    if session.decisions:
        lines.append("\n  [bold]Recent decisions[/bold] (newest last)")
        for d in list(session.decisions)[-10:]:
            forced = " [dim](forced)[/dim]" if d.forced else ""
            reason = f" — {d.reason}" if d.reason else ""
            lines.append(
                f"    {d.tier:<7}→ {d.route:<8} {d.model}  {d.judge_ms}ms{forced}{reason}"
            )
    else:
        lines.append("\n  [dim]No routing decisions yet this session.[/dim]")
    return "\n".join(lines)


async def handle_operator_subcommand(app: object, raw_arg: str) -> None:
    """Dispatch ``/operator <sub>``: on, off, status, preset, force, test, config."""
    from bog_agents_cli.widgets.messages import AppMessage, ErrorMessage

    session = ensure_session(app)
    arg = raw_arg.strip()
    head, _, rest = arg.partition(" ")
    head = head.lower()
    rest = rest.strip()

    if head in {"", "status"}:
        await app._mount_message(AppMessage(render_status(session)))  # type: ignore[attr-defined]
        return

    if head == "on":
        # Re-read the TOML so config edits take effect without a restart.
        session.config = load_operator_config()
        session.tiers = resolve_tiers(session.config)
        session.active = True
        await app._mount_message(  # type: ignore[attr-defined]
            AppMessage(
                f"Operator mode [green]on[/green] — preset [bold]{session.config.preset}[/bold]. "
                "Each prompt is judged and routed; /operator status shows decisions."
            )
        )
        return

    if head == "off":
        session.active = False
        await app._mount_message(
            AppMessage(
                "Operator mode [red]off[/red] — prompts go to your active model."
            )
        )  # type: ignore[attr-defined]
        return

    if head == "preset":
        available = sorted(set(BUILTIN_PRESETS) | set(session.config.custom_presets))
        if not rest:
            await app._mount_message(
                AppMessage(
                    "Available presets: "
                    + ", ".join(available)
                    + f"\nActive: {session.config.preset}"
                )
            )  # type: ignore[attr-defined]
            return
        if rest not in available:
            await app._mount_message(
                ErrorMessage(
                    f"Unknown preset {rest!r}. Available: {', '.join(available)}"
                )
            )  # type: ignore[attr-defined]
            return
        session.config.preset = rest
        session.tiers = resolve_tiers(session.config)
        await app._mount_message(
            AppMessage(
                f"Operator preset → [bold]{rest}[/bold]\n\n{render_status(session)}"
            )
        )  # type: ignore[attr-defined]
        return

    if head == "force":
        if rest not in TIER_NAMES:
            await app._mount_message(
                ErrorMessage(f"Usage: /operator force <{'|'.join(TIER_NAMES)}>")
            )  # type: ignore[attr-defined]
            return
        session.forced_tier = rest
        spec = session.tiers[rest]
        await app._mount_message(
            AppMessage(
                f"Next prompt forced to [bold]{rest}[/bold] → {spec.model} (effort: {spec.effort})."
            )
        )  # type: ignore[attr-defined]
        return

    if head == "test":
        if not rest:
            await app._mount_message(
                ErrorMessage(
                    "Usage: /operator test <prompt> — dry-run the judge, no agent turn."
                )
            )  # type: ignore[attr-defined]
            return
        spec = _judge_model_spec(session, app)
        if not spec:
            await app._mount_message(
                ErrorMessage(
                    "No judge model available — run /model first or set judge_model in operator.toml."
                )
            )  # type: ignore[attr-defined]
            return
        try:
            from bog_agents_cli.config import create_model_with_fallback

            model_result = create_model_with_fallback(
                spec, profile_overrides=getattr(app, "_profile_override", None)
            )

            async def _invoke(system: str, user: str) -> str:
                return await invoke_model(
                    model_result.model,
                    system,
                    user,
                    timeout_seconds=_JUDGE_TIMEOUT_SECONDS,
                )

            decision = await judge_prompt(
                rest,
                session.tiers,
                invoke=_invoke,
                routes_enabled=session.config.routes,
            )
        except Exception as exc:
            await app._mount_message(ErrorMessage(f"/operator test failed: {exc}"))  # type: ignore[attr-defined]
            return
        if decision is None:
            await app._mount_message(
                ErrorMessage("Judge could not classify that prompt (see logs).")
            )  # type: ignore[attr-defined]
            return
        reason = f"\n  reason: {decision.reason}" if decision.reason else ""
        await app._mount_message(  # type: ignore[attr-defined]
            AppMessage(
                f"[bold]Dry run[/bold] (no agent turn)\n"
                f"  tier:   {decision.tier}\n"
                f"  route:  {decision.route}\n"
                f"  model:  {decision.model}  (effort: {decision.effort})\n"
                f"  judge:  {decision.judge_ms}ms{reason}"
            )
        )
        return

    if head == "config":
        path = write_default_config()
        await app._mount_message(  # type: ignore[attr-defined]
            AppMessage(
                f"Operator config: [cyan]{path}[/cyan]\n"
                "Edit the TOML, then [bold]/operator on[/bold] to reload it."
            )
        )
        return

    await app._mount_message(  # type: ignore[attr-defined]
        ErrorMessage(
            "Usage: /operator [on|off|status|preset <name>|force <tier>|test <prompt>|config]"
        )
    )
