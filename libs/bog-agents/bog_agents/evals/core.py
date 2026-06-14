"""Core eval primitives: Case, Dataset, Score, run_evals (ROADMAP #9).

`bog_agents.evals` makes evaluation a first-class, importable SDK primitive —
the thing teams gate releases on — rather than something only reachable through
the harbor benchmark harness. You bring a :class:`Dataset` of :class:`Case`s and
a list of :class:`Scorer`s (rule-based or LLM-as-judge), point :func:`run_evals`
at any callable that maps an input to an output (an agent's invoke, a tool, a
plain function), and get back an :class:`EvalReport` with a pass rate you can
assert on in CI.

Everything here is task-agnostic: the runner only needs ``task(input) -> output``
(sync or async). Scorers likewise may be sync or async.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable, Iterator, Sequence


@dataclass
class Case:
    """A single evaluation case.

    Attributes:
        input: The input passed to the task under evaluation.
        expected: The reference/expected output (optional; scorers decide how
            to use it).
        metadata: Arbitrary per-case metadata (tags, difficulty, ...).
        name: Optional human-readable case name (defaults to its index).
    """

    input: Any
    expected: Any = None
    metadata: dict[str, Any] = field(default_factory=dict)
    name: str = ""


@dataclass
class Dataset:
    """An ordered collection of :class:`Case`s."""

    cases: list[Case]
    name: str = "dataset"

    def __iter__(self) -> Iterator[Case]:
        return iter(self.cases)

    def __len__(self) -> int:
        return len(self.cases)

    @classmethod
    def from_list(cls, items: Iterable[dict[str, Any] | Case], *, name: str = "dataset") -> Dataset:
        """Build a dataset from dicts (``input``/``expected``/``metadata``/``name``) or Cases."""
        cases: list[Case] = []
        for item in items:
            if isinstance(item, Case):
                cases.append(item)
            else:
                cases.append(
                    Case(
                        input=item.get("input"),
                        expected=item.get("expected"),
                        metadata=dict(item.get("metadata", {})),
                        name=str(item.get("name", "")),
                    )
                )
        return cls(cases=cases, name=name)

    @classmethod
    def from_json(cls, path: str | Path, *, name: str | None = None) -> Dataset:
        """Load a dataset from a JSON file (a list of case objects)."""
        p = Path(path)
        data = json.loads(p.read_text(encoding="utf-8"))
        return cls.from_list(data, name=name or p.stem)


@dataclass
class Score:
    """The result of one scorer on one case.

    Attributes:
        name: Scorer name.
        value: Normalized score in ``[0.0, 1.0]``.
        passed: Whether this scorer considers the case a pass.
        detail: Optional human-readable explanation.
    """

    name: str
    value: float
    passed: bool
    detail: str = ""


@runtime_checkable
class Scorer(Protocol):
    """Scores a (case, output) pair. ``score`` may be sync or async."""

    name: str

    def score(self, case: Case, output: Any) -> Score | Awaitable[Score]: ...


@dataclass
class CaseResult:
    """All scores for a single case, plus the produced output."""

    case: Case
    output: Any
    scores: list[Score] = field(default_factory=list)
    error: str = ""

    @property
    def passed(self) -> bool:
        """True if there was no error and every scorer passed."""
        return not self.error and bool(self.scores) and all(s.passed for s in self.scores)


@dataclass
class EvalReport:
    """Aggregate result of running a dataset through the scorers."""

    dataset: str
    results: list[CaseResult]

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def pass_rate(self) -> float:
        return self.passed / self.total if self.total else 0.0

    def scorer_averages(self) -> dict[str, float]:
        """Mean value per scorer across all cases."""
        sums: dict[str, float] = {}
        counts: dict[str, int] = {}
        for r in self.results:
            for s in r.scores:
                sums[s.name] = sums.get(s.name, 0.0) + s.value
                counts[s.name] = counts.get(s.name, 0) + 1
        return {k: sums[k] / counts[k] for k in sums if counts[k]}

    def assert_pass_rate(self, threshold: float) -> None:
        """Raise AssertionError if the pass rate is below ``threshold`` (for CI)."""
        if self.pass_rate < threshold:
            raise AssertionError(f"{self.dataset}: pass rate {self.pass_rate:.1%} < required {threshold:.1%} ({self.passed}/{self.total} passed)")

    def summary(self) -> str:
        """A compact text summary of the report."""
        lines = [f"Eval '{self.dataset}': {self.passed}/{self.total} passed ({self.pass_rate:.1%})"]
        for name, avg in sorted(self.scorer_averages().items()):
            lines.append(f"  - {name}: avg {avg:.2f}")
        failed = [r for r in self.results if not r.passed]
        if failed:
            lines.append(f"  {len(failed)} failing case(s):")
            for r in failed[:10]:
                label = r.case.name or repr(r.case.input)[:40]
                reason = r.error or ", ".join(f"{s.name}={s.value:.2f}" for s in r.scores if not s.passed)
                lines.append(f"    * {label}: {reason}")
        return "\n".join(lines)


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def _run_case(
    task: Callable[[Any], Any],
    case: Case,
    scorers: Sequence[Scorer],
) -> CaseResult:
    try:
        output = await _maybe_await(task(case.input))
    except Exception as exc:  # a task crash is a failed case, not a runner crash
        return CaseResult(case=case, output=None, error=f"{type(exc).__name__}: {exc}")

    scores: list[Score] = []
    for scorer in scorers:
        try:
            scores.append(await _maybe_await(scorer.score(case, output)))
        except Exception as exc:
            scores.append(
                Score(
                    name=getattr(scorer, "name", type(scorer).__name__),
                    value=0.0,
                    passed=False,
                    detail=f"scorer error: {type(exc).__name__}: {exc}",
                )
            )
    return CaseResult(case=case, output=output, scores=scores)


async def run_evals(
    task: Callable[[Any], Any],
    dataset: Dataset | Sequence[Case],
    scorers: Sequence[Scorer],
    *,
    concurrency: int = 4,
) -> EvalReport:
    """Run every case through ``task`` and apply ``scorers``.

    Args:
        task: Callable mapping a case input to an output. May be sync or async
            (e.g. ``lambda x: agent.invoke(...)`` or an ``async def``).
        dataset: A :class:`Dataset` or a sequence of :class:`Case`s.
        scorers: Scorers to apply to each (case, output).
        concurrency: Max cases evaluated in parallel.

    Returns:
        An :class:`EvalReport`.
    """
    cases = list(dataset)
    name = dataset.name if isinstance(dataset, Dataset) else "dataset"
    sem = asyncio.Semaphore(max(1, concurrency))

    async def _bounded(case: Case) -> CaseResult:
        async with sem:
            return await _run_case(task, case, scorers)

    results = await asyncio.gather(*(_bounded(c) for c in cases))
    return EvalReport(dataset=name, results=list(results))
