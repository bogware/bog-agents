"""Usage you can read (ROADMAP #52): per-message strip, status-bar spend, `/cost tree|cache|explain`.

Everything here is client-side. The TUI already sees every model response's
`usage_metadata` while streaming; this module turns each one into a
`UsageRecord` (tokens in/out, cache read/write, dollars, time to first token,
tokens per second, main vs subagent), keeps a session `UsageLedger`, renders
the dim strip under each assistant message and the spend / cache-hit figure
in the status bar, and answers `/cost explain <question>` over the serialized
ledger with an injected cheap model. Cache-bust events come from the SDK's
`CacheBustDetectorMiddleware`, which the CLI turns on for every agent.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from bog_agents.middleware.cost_tracker import price_for_model

logger = logging.getLogger(__name__)

CATEGORY_MAIN = "main"
CATEGORY_SUBAGENT = "subagent"


@dataclass
class UsageRecord:
    """One model response's usage, priced and timed.

    Attributes:
        model: Model spec the request went to.
        category: `main`, `subagent`, `team`, `worktree`, `web`, `mcp`.
        input_tokens: Uncached input tokens.
        output_tokens: Output tokens.
        cache_read: Input tokens served from the provider cache.
        cache_write: Input tokens written to the provider cache.
        usd: Estimated dollars for this response.
        ttft_s: Seconds from request start to the first streamed text, if known.
        duration_s: Seconds from request start to the usage report, if known.
    """

    model: str
    category: str = CATEGORY_MAIN
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_write: int = 0
    usd: float = 0.0
    ttft_s: float | None = None
    duration_s: float | None = None

    @property
    def tokens_per_second(self) -> float | None:
        """Output throughput over the generation window (after first token)."""
        if self.duration_s is None or self.output_tokens <= 0:
            return None
        window = self.duration_s - (self.ttft_s or 0.0)
        if window <= 0.05:
            return None
        return self.output_tokens / window


def price_usd(
    model: str,
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read: int = 0,
    cache_write: int = 0,
) -> float:
    """Price one response; cache reads at 10% and cache writes at 125% of input price (Anthropic's ratio)."""
    price = price_for_model(model or "")
    if price is None:
        return 0.0
    per_in, per_out = price
    return (
        input_tokens * per_in
        + cache_read * per_in * 0.1
        + cache_write * per_in * 1.25
        + output_tokens * per_out
    ) / 1_000_000


def usage_from_metadata(
    usage: Mapping[str, Any],
    *,
    model: str,
    category: str = CATEGORY_MAIN,
    ttft_s: float | None = None,
    duration_s: float | None = None,
) -> UsageRecord:
    """Build a `UsageRecord` from a LangChain `usage_metadata` dict.

    Args:
        usage: The message's `usage_metadata` (`input_tokens`, `output_tokens`,
            optional `input_token_details.cache_read` / `.cache_creation`).
        model: Model spec for pricing.
        category: Attribution bucket.
        ttft_s: Time to first token, if measured.
        duration_s: Request duration, if measured.

    Returns:
        The priced record.
    """
    details = usage.get("input_token_details") or {}
    cache_read = int(details.get("cache_read") or 0)
    cache_write = int(details.get("cache_creation") or details.get("cache_write") or 0)
    input_tokens = int(usage.get("input_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or 0)
    if not (input_tokens or output_tokens):
        input_tokens = int(usage.get("total_tokens") or 0)
    # Some providers report cached tokens inside input_tokens; keep the uncached share.
    uncached = (
        max(0, input_tokens - cache_read - cache_write)
        if input_tokens >= cache_read + cache_write
        else input_tokens
    )
    return UsageRecord(
        model=model,
        category=category,
        input_tokens=uncached,
        output_tokens=output_tokens,
        cache_read=cache_read,
        cache_write=cache_write,
        usd=price_usd(
            model,
            input_tokens=uncached,
            output_tokens=output_tokens,
            cache_read=cache_read,
            cache_write=cache_write,
        ),
        ttft_s=ttft_s,
        duration_s=duration_s,
    )


def _k(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def format_usage_strip(rec: UsageRecord) -> str:
    """One dim line for under an assistant message."""
    parts = [f"{_k(rec.input_tokens)}→ {_k(rec.output_tokens)}←"]
    if rec.cache_read or rec.cache_write:
        cache = []
        if rec.cache_read:
            cache.append(f"{_k(rec.cache_read)} read")
        if rec.cache_write:
            cache.append(f"{_k(rec.cache_write)} written")
        parts.append("cache " + ", ".join(cache))
    parts.append(f"${rec.usd:.4f}" if rec.usd else "unpriced")
    if rec.ttft_s is not None:
        parts.append(f"TTFT {rec.ttft_s:.1f}s")
    tps = rec.tokens_per_second
    if tps is not None:
        parts.append(f"{tps:.0f} tok/s")
    if rec.category != CATEGORY_MAIN:
        parts.append(rec.category)
    return " · ".join(parts)


@dataclass
class UsageLedger:
    """Session-wide usage records with category and cache roll-ups."""

    records: list[UsageRecord] = field(default_factory=list)

    def add(self, rec: UsageRecord) -> None:
        """Append one record."""
        self.records.append(rec)

    @property
    def usd(self) -> float:
        """Total estimated spend."""
        return sum(r.usd for r in self.records)

    @property
    def cache_hit_ratio(self) -> float | None:
        """Share of input that came from the cache (`None` before any input)."""
        read = sum(r.cache_read for r in self.records)
        total = read + sum(r.input_tokens + r.cache_write for r in self.records)
        return None if total == 0 else read / total

    def by_category(self) -> dict[str, dict[str, float]]:
        """Per-category totals: requests, input, output, cache_read, usd."""
        out: dict[str, dict[str, float]] = {}
        for rec in self.records:
            row = out.setdefault(
                rec.category,
                {"requests": 0, "input": 0, "output": 0, "cache_read": 0, "usd": 0.0},
            )
            row["requests"] += 1
            row["input"] += rec.input_tokens
            row["output"] += rec.output_tokens
            row["cache_read"] += rec.cache_read
            row["usd"] += rec.usd
        return out

    def format_tree(self) -> str:
        """Render `/cost tree`: spend by category, most expensive first."""
        rows = self.by_category()
        if not rows:
            return "No model requests recorded this session yet."
        lines = ["## Cost by category", ""]
        for name, row in sorted(
            rows.items(), key=lambda kv: kv[1]["usd"], reverse=True
        ):
            lines.append(
                f"- {name}: ${row['usd']:.4f}  ({int(row['requests'])} req, {int(row['input']):,} in / {int(row['output']):,} out, "
                f"{int(row['cache_read']):,} cached)"
            )
        ratio = self.cache_hit_ratio
        lines.append("")
        lines.append(
            f"**Total: ${self.usd:.4f}** · cache hit {'n/a' if ratio is None else f'{ratio * 100:.0f}%'}"
        )
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        """Serialize for `/cost explain`."""
        return {
            "total_usd": round(self.usd, 6),
            "cache_hit_ratio": self.cache_hit_ratio,
            "by_category": self.by_category(),
            "requests": [
                asdict(r) | {"tokens_per_second": r.tokens_per_second}
                for r in self.records[-40:]
            ],
        }


def status_bar_text(ledger: UsageLedger) -> str:
    """`$0.0412 · cache 62%` for the status bar (empty before the first response)."""
    if not ledger.records:
        return ""
    ratio = ledger.cache_hit_ratio
    cache = "" if ratio is None else f" · cache {ratio * 100:.0f}%"
    return f"${ledger.usd:.4f}{cache}"


# ------------------------------------------------------------------ app wiring


def get_usage_ledger(app: Any) -> UsageLedger:  # noqa: ANN401 - the App
    """Return (creating) the app's session ledger."""
    ledger = getattr(app, "_usage_ledger", None)
    if not isinstance(ledger, UsageLedger):
        ledger = UsageLedger()
        app._usage_ledger = ledger
    return ledger


def on_usage(app: Any, rec: UsageRecord) -> None:  # noqa: ANN401 - the App
    """Record one response and refresh the status bar spend figure."""
    ledger = get_usage_ledger(app)
    ledger.add(rec)
    bar = getattr(app, "_status_bar", None)
    setter = getattr(bar, "set_spend", None)
    if callable(setter):
        try:
            setter(status_bar_text(ledger))
        except Exception:
            logger.debug("usage: status bar update failed", exc_info=True)


def install_usage_tracking(app: Any) -> None:  # noqa: ANN401 - the App
    """Give the adapter a sink that feeds the app's ledger and status bar."""
    get_usage_ledger(app)
    adapter = getattr(app, "_ui_adapter", None)
    setter = getattr(adapter, "set_usage_sink", None)
    if callable(setter):
        setter(lambda rec: on_usage(app, rec))


async def record_stream_usage(
    sink: Callable[[UsageRecord], None] | None,
    usage: Mapping[str, Any],
    *,
    model: str,
    category: str,
    ttft_s: float | None,
    duration_s: float | None,
    message_widget: Any = None,  # noqa: ANN401 - AssistantMessage or None
) -> UsageRecord:
    """Adapter hook: build the record, feed the sink, attach the strip to the message."""
    rec = usage_from_metadata(
        usage, model=model, category=category, ttft_s=ttft_s, duration_s=duration_s
    )
    if sink is not None:
        try:
            sink(rec)
        except Exception:
            logger.debug("usage: sink failed", exc_info=True)
    set_usage = getattr(message_widget, "set_usage", None)
    if callable(set_usage):
        try:
            await set_usage(format_usage_strip(rec))
        except Exception:
            logger.debug("usage: strip mount failed", exc_info=True)
    return rec


# ------------------------------------------------------------------ /cost explain


def build_explain_prompt(
    question: str, ledger: UsageLedger, extra: Mapping[str, Any] | None = None
) -> str:
    """Prompt for the cheap model: the serialized ledger plus the user's question."""
    payload = ledger.to_dict()
    if extra:
        payload["context"] = dict(extra)
    return (
        "You are explaining a coding agent's token usage to its user. Use only the ledger below; "
        "be concrete (numbers, categories, cache behaviour) and suggest at most three actions.\n\n"
        f"LEDGER (JSON):\n{json.dumps(payload, indent=2, default=str)}\n\n"
        f"QUESTION: {question.strip() or 'Where did the money go, and what would cut it?'}"
    )


async def explain_usage(
    question: str,
    ledger: UsageLedger,
    invoke: Callable[[str], Awaitable[str]],
    *,
    extra: Mapping[str, Any] | None = None,
) -> str:
    """Answer `question` over the ledger with an injected model call (failures become text)."""
    try:
        answer = await invoke(build_explain_prompt(question, ledger, extra))
    except Exception as exc:
        return f"Could not explain usage: {exc}"
    return (answer or "").strip() or "The model returned no explanation."


async def run_cost_explain(app: Any, command: str) -> None:  # noqa: ANN401 - the App
    """Body of `/cost explain <question>` (runs under `_start_model_command`)."""
    from bog_agents_cli.auto_mode import resolve_risk_judge
    from bog_agents_cli.widgets.messages import AppMessage

    question = command.split(None, 2)[2] if len(command.split(None, 2)) > 2 else ""
    judge, desc = resolve_risk_judge()
    if judge is None:
        await app._mount_message(
            AppMessage(
                "No review model is available for `/cost explain` (configure a provider first)."
            )
        )
        return
    ledger = get_usage_ledger(app)
    extra = {"thread_cache_report": cache_report_for_app(app)}
    text = await explain_usage(question, ledger, judge, extra=extra)
    await app._mount_message(AppMessage(f"[dim]via {desc}[/dim]\n{text}"))


# ------------------------------------------------------------------ cache diagnostics


def cache_events_dir() -> Path:
    """Where the SDK's `CacheBustDetectorMiddleware` writes per-thread JSONL for CLI agents."""
    from bog_agents_cli._env_vars import bog_agents_home

    return bog_agents_home() / "cache_diagnostics"


def cache_report_for_app(app: Any) -> str:  # noqa: ANN401 - the App
    """`/cost cache` for the current thread."""
    from bog_agents.middleware.cache_diagnostics import (
        format_cache_report,
        read_cache_events,
    )

    state = getattr(app, "_session_state", None)
    thread_id = str(getattr(state, "thread_id", "") or "session")
    try:
        events = read_cache_events(cache_events_dir(), thread_id)
    except Exception:
        logger.debug("usage: cache events unreadable", exc_info=True)
        events = []
    return format_cache_report(events)
