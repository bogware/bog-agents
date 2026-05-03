"""``/race`` — fan a prompt out to N models and pick the winner.

The smart-merge worktree fleet: spawn ``N`` agent runs (different models,
optionally different prompt twists) on the same task, then surface a
side-by-side comparison so the user can pick the winning answer or keep
the diff with the highest jury score.

For 0.8.0 we ship the multi-model execution + comparison flow. The
worktree-isolation pass that lets each runner mutate files independently
is built on top of ``ParallelWorktreeMiddleware`` from the SDK; this
module only orchestrates the LLM fan-out, gathers responses, and
optionally invokes :mod:`bog_agents_cli.jury` to score them.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from langchain_core.language_models import BaseChatModel

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Racer:
    """One participant in a /race fan-out."""

    label: str
    model: BaseChatModel
    system_prompt: str | None = None


@dataclass(frozen=True, slots=True)
class RaceResult:
    """One racer's completed run."""

    label: str
    output: str
    duration_seconds: float
    error: str | None = None

    @property
    def succeeded(self) -> bool:
        """Whether the run produced a usable response."""
        return self.error is None and bool(self.output)


@dataclass(frozen=True, slots=True)
class RaceReport:
    """Aggregate result of a /race round."""

    prompt: str
    results: tuple[RaceResult, ...]

    @property
    def successes(self) -> tuple[RaceResult, ...]:
        """Subset of results that completed without error."""
        return tuple(r for r in self.results if r.succeeded)

    def format_summary(self, *, max_chars: int = 600) -> str:
        """Render a side-by-side preview of every runner's response."""
        if not self.results:
            return "No racers completed — nothing to show."
        lines: list[str] = [
            f"[bold]/race results[/bold] — {len(self.successes)}/{len(self.results)} succeeded\n"
        ]
        for r in self.results:
            header = f"[cyan]{r.label}[/cyan]  ({r.duration_seconds:.1f}s)"
            if r.error:
                lines.append(f"{header}  [red]ERROR[/red]")
                lines.append(f"  [dim]{r.error}[/dim]")
                continue
            preview = (r.output or "").strip()
            if len(preview) > max_chars:
                preview = preview[:max_chars] + "…"
            lines.append(header)
            lines.append("  " + preview.replace("\n", "\n  "))
            lines.append("")
        return "\n".join(lines).rstrip()


def load_race_specs(config_path: Path | None = None) -> list[str]:
    """Read ``[race].models`` from the config TOML.

    Returns an empty list if no race lineup is configured.
    """
    import tomllib

    from bog_agents_cli.model_config import DEFAULT_CONFIG_PATH

    path = config_path or DEFAULT_CONFIG_PATH
    if not path.exists():
        return []
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return []
    section = data.get("race", {})
    if not isinstance(section, dict):
        return []
    raw = section.get("models", [])
    if not isinstance(raw, list):
        return []
    return [str(spec) for spec in raw if isinstance(spec, (str, int, float))]


async def _run_one(
    racer: Racer,
    prompt: str,
) -> RaceResult:
    """Invoke one racer against the prompt and time it."""
    from langchain_core.messages import HumanMessage, SystemMessage

    started = asyncio.get_event_loop().time()
    messages: list[object] = []
    if racer.system_prompt:
        messages.append(SystemMessage(content=racer.system_prompt))
    messages.append(HumanMessage(content=prompt))

    try:
        response = await racer.model.ainvoke(messages)  # type: ignore[arg-type]
    except Exception as exc:  # pragma: no cover — provider failures are runtime
        elapsed = asyncio.get_event_loop().time() - started
        return RaceResult(
            label=racer.label,
            output="",
            duration_seconds=elapsed,
            error=str(exc),
        )

    elapsed = asyncio.get_event_loop().time() - started
    content = getattr(response, "content", "") or ""
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                value = part.get("text")
                if isinstance(value, str):
                    parts.append(value)
            elif isinstance(part, str):
                parts.append(part)
        content = "".join(parts)
    return RaceResult(
        label=racer.label,
        output=str(content).strip(),
        duration_seconds=elapsed,
    )


async def run_race(prompt: str, racers: list[Racer]) -> RaceReport:
    """Fan ``prompt`` out to every racer in parallel and gather results.

    Args:
        prompt: The user task to distribute.
        racers: Ordered list of racers; each is a (label, model, optional
            system-prompt twist) bundle.

    Returns:
        A :class:`RaceReport` with one :class:`RaceResult` per racer.

    Raises:
        ValueError: If ``prompt`` is empty or ``racers`` is empty.
    """
    if not prompt or not prompt.strip():
        msg = "run_race() requires a non-empty prompt"
        raise ValueError(msg)
    if not racers:
        msg = "run_race() requires at least one racer"
        raise ValueError(msg)

    tasks = [_run_one(r, prompt) for r in racers]
    results = tuple(await asyncio.gather(*tasks))
    return RaceReport(prompt=prompt, results=results)


def pick_winner(report: RaceReport) -> RaceResult | None:
    """Naive winner selection: shortest successful response wins ties broken by speed.

    "Shortest wins" is the cynical-but-effective heuristic that the
    response with the least padding is usually the closest to the user's
    real ask. Real teams will configure ``[race].judge`` to defer to a
    jury vote (todo); for now the shortest-by-tokens proxy keeps the
    flagship demoable without an extra model call.
    """
    successes = report.successes
    if not successes:
        return None
    return min(successes, key=lambda r: (len(r.output), r.duration_seconds))


__all__ = [
    "RaceReport",
    "RaceResult",
    "Racer",
    "load_race_specs",
    "pick_winner",
    "run_race",
]
