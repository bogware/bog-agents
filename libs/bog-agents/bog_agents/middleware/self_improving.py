"""Self-Improving Agent Loop middleware for autonomous skill improvement.

After each session, the agent evaluates its own performance and updates
skills, memory, and routing preferences to perform better next time.
Connects the memory, skills, and eval subsystems into a closed loop.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ContextT,
    ModelRequest,
    ModelResponse,
    ResponseT,
)

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class OutcomeRating(StrEnum):
    """Self-assessment ratings for session outcomes."""

    EXCELLENT = "excellent"
    GOOD = "good"
    FAIR = "fair"
    POOR = "poor"
    FAILED = "failed"


@dataclass
class SessionMetrics:
    """Metrics collected during an agent session."""

    session_id: str
    start_time: float = field(default_factory=time.time)
    end_time: float | None = None
    total_turns: int = 0
    tool_calls: int = 0
    tool_errors: int = 0
    model_calls: int = 0
    total_tokens: int = 0
    files_modified: int = 0
    tests_passed: int | None = None
    tests_failed: int | None = None
    lint_errors: int | None = None
    user_corrections: int = 0
    undos_performed: int = 0

    @property
    def duration_seconds(self) -> float:
        """Session duration in seconds."""
        end = self.end_time or time.time()
        return end - self.start_time

    @property
    def error_rate(self) -> float:
        """Fraction of tool calls that errored."""
        if self.tool_calls == 0:
            return 0.0
        return self.tool_errors / self.tool_calls

    @property
    def efficiency_score(self) -> float:
        """Score from 0-1 measuring session efficiency.

        Higher is better. Accounts for error rate, corrections, and undos.
        """
        if self.total_turns == 0:
            return 0.5

        # Penalize errors, corrections, and undos
        error_penalty = self.error_rate * 0.3
        correction_penalty = min(self.user_corrections / max(self.total_turns, 1), 1.0) * 0.3
        undo_penalty = min(self.undos_performed / max(self.total_turns, 1), 1.0) * 0.2

        # Reward test success
        test_bonus = 0.0
        if self.tests_passed is not None and self.tests_failed is not None:
            total_tests = self.tests_passed + self.tests_failed
            if total_tests > 0:
                test_bonus = (self.tests_passed / total_tests) * 0.2

        return max(0.0, min(1.0, 1.0 - error_penalty - correction_penalty - undo_penalty + test_bonus))


@dataclass
class SelfAssessment:
    """Agent's self-assessment of a session."""

    session_id: str
    rating: OutcomeRating
    efficiency_score: float
    lessons_learned: list[str]
    suggested_improvements: list[str]
    patterns_to_remember: list[str]
    patterns_to_avoid: list[str]
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "session_id": self.session_id,
            "rating": self.rating,
            "efficiency_score": self.efficiency_score,
            "lessons_learned": self.lessons_learned,
            "suggested_improvements": self.suggested_improvements,
            "patterns_to_remember": self.patterns_to_remember,
            "patterns_to_avoid": self.patterns_to_avoid,
            "timestamp": self.timestamp,
        }


@dataclass
class ImprovementRecord:
    """Persistent record of improvements applied."""

    assessments: list[SelfAssessment] = field(default_factory=list)
    total_sessions: int = 0
    average_efficiency: float = 0.5
    top_patterns: list[str] = field(default_factory=list)
    anti_patterns: list[str] = field(default_factory=list)

    def add_assessment(self, assessment: SelfAssessment) -> None:
        """Add a new assessment and update aggregate stats.

        Args:
            assessment: Session self-assessment.
        """
        self.assessments.append(assessment)
        self.total_sessions += 1

        # Update running average efficiency
        scores = [a.efficiency_score for a in self.assessments[-20:]]
        self.average_efficiency = sum(scores) / len(scores) if scores else 0.5

        # Aggregate patterns (keep top 20)
        all_patterns: dict[str, int] = {}
        for a in self.assessments[-50:]:
            for p in a.patterns_to_remember:
                all_patterns[p] = all_patterns.get(p, 0) + 1
        self.top_patterns = sorted(all_patterns, key=all_patterns.get, reverse=True)[:20]

        all_anti: dict[str, int] = {}
        for a in self.assessments[-50:]:
            for p in a.patterns_to_avoid:
                all_anti[p] = all_anti.get(p, 0) + 1
        self.anti_patterns = sorted(all_anti, key=all_anti.get, reverse=True)[:20]


def assess_session(metrics: SessionMetrics) -> SelfAssessment:
    """Generate a self-assessment from session metrics.

    Args:
        metrics: Collected session metrics.

    Returns:
        SelfAssessment with analysis and recommendations.
    """
    efficiency = metrics.efficiency_score

    # Determine rating
    if efficiency >= 0.9:
        rating = OutcomeRating.EXCELLENT
    elif efficiency >= 0.7:
        rating = OutcomeRating.GOOD
    elif efficiency >= 0.5:
        rating = OutcomeRating.FAIR
    elif efficiency >= 0.3:
        rating = OutcomeRating.POOR
    else:
        rating = OutcomeRating.FAILED

    lessons: list[str] = []
    improvements: list[str] = []
    patterns_good: list[str] = []
    patterns_bad: list[str] = []

    # Analyze error patterns
    if metrics.error_rate > 0.2:
        lessons.append(f"High tool error rate ({metrics.error_rate:.0%}) — review tool usage patterns")
        improvements.append("Validate tool arguments before execution")
        patterns_bad.append("Executing tools without argument validation")

    if metrics.user_corrections > 3:
        lessons.append(f"{metrics.user_corrections} user corrections — need better initial understanding")
        improvements.append("Ask clarifying questions before starting complex tasks")
        patterns_bad.append("Assuming intent without confirming with user")

    if metrics.undos_performed > 2:
        lessons.append(f"{metrics.undos_performed} undos — being too aggressive with changes")
        improvements.append("Preview changes before applying them")
        patterns_bad.append("Making large changes without incremental verification")

    # Positive patterns
    if metrics.tests_passed and metrics.tests_failed == 0:
        patterns_good.append("Running tests after changes confirms correctness")
    if metrics.error_rate < 0.05 and metrics.total_turns > 5:
        patterns_good.append("Careful tool usage with low error rate")
    if metrics.user_corrections == 0 and metrics.total_turns > 3:
        patterns_good.append("Good first-attempt accuracy without user corrections")

    # Test-related insights
    if metrics.tests_failed and metrics.tests_failed > 0:
        lessons.append(f"{metrics.tests_failed} tests failed — run tests earlier in the process")
        improvements.append("Run tests after each significant change, not just at the end")

    if metrics.lint_errors and metrics.lint_errors > 0:
        lessons.append(f"{metrics.lint_errors} lint errors — check formatting before completing")
        improvements.append("Run linter before declaring task complete")

    return SelfAssessment(
        session_id=metrics.session_id,
        rating=rating,
        efficiency_score=efficiency,
        lessons_learned=lessons,
        suggested_improvements=improvements,
        patterns_to_remember=patterns_good,
        patterns_to_avoid=patterns_bad,
    )


def generate_improvement_prompt(record: ImprovementRecord) -> str:
    """Generate a system prompt addition based on accumulated improvements.

    Args:
        record: Accumulated improvement data.

    Returns:
        Markdown string to inject into the system prompt.
    """
    if not record.assessments:
        return ""

    sections: list[str] = []
    sections.append("## Self-Improvement Notes")
    sections.append(f"Sessions analyzed: {record.total_sessions}")
    sections.append(f"Average efficiency: {record.average_efficiency:.0%}")

    if record.top_patterns:
        sections.append("\n### Effective Patterns")
        for p in record.top_patterns[:10]:
            sections.append(f"- {p}")

    if record.anti_patterns:
        sections.append("\n### Patterns to Avoid")
        for p in record.anti_patterns[:10]:
            sections.append(f"- {p}")

    # Recent lessons from last 5 sessions
    recent = record.assessments[-5:]
    all_lessons: list[str] = []
    for a in recent:
        all_lessons.extend(a.lessons_learned)
    if all_lessons:
        sections.append("\n### Recent Lessons")
        for lesson in dict.fromkeys(all_lessons):  # deduplicate preserving order
            sections.append(f"- {lesson}")

    return "\n".join(sections)


class SelfImprovingMiddleware(AgentMiddleware):
    """Middleware for autonomous agent self-improvement.

    Tracks session metrics, generates self-assessments after each session,
    and injects improvement insights into future sessions' system prompts.

    Example:
        ```python
        from bog_agents.middleware.self_improving import SelfImprovingMiddleware

        middleware = SelfImprovingMiddleware(
            store_path="~/.bog-agents/improvements.json",
        )
        ```
    """

    record: ImprovementRecord
    current_metrics: SessionMetrics | None
    _store_path: Path

    def __init__(
        self,
        *,
        store_path: str | None = None,
    ) -> None:
        """Initialize self-improving middleware.

        Args:
            store_path: Path for persisting improvement data.
        """
        if store_path is None:
            store_path = "~/.bog-agents/improvements.json"
        self._store_path = Path(store_path).expanduser()
        self.record = ImprovementRecord()
        self.current_metrics: SessionMetrics | None = None
        self._load()
        # Auto-start a default session so callers don't need to call start_session()
        if self.current_metrics is None:
            self.start_session("default")

    def _load(self) -> None:
        """Load improvement data from disk."""
        if not self._store_path.exists():
            return
        try:
            data = json.loads(self._store_path.read_text())
            self.record.total_sessions = data.get("total_sessions", 0)
            self.record.average_efficiency = data.get("average_efficiency", 0.5)
            self.record.top_patterns = data.get("top_patterns", [])
            self.record.anti_patterns = data.get("anti_patterns", [])
            logger.info(
                "Loaded self-improvement data: %d sessions, %.0f%% avg efficiency",
                self.record.total_sessions,
                self.record.average_efficiency * 100,
            )
        except (json.JSONDecodeError, KeyError):
            logger.debug("Failed to load improvement data", exc_info=True)

    def _save(self) -> None:
        """Save improvement data to disk."""
        self._store_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "total_sessions": self.record.total_sessions,
            "average_efficiency": self.record.average_efficiency,
            "top_patterns": self.record.top_patterns,
            "anti_patterns": self.record.anti_patterns,
            "last_updated": time.time(),
        }
        self._store_path.write_text(json.dumps(data, indent=2))

    def start_session(self, session_id: str) -> None:
        """Start tracking a new session.

        Args:
            session_id: Unique session identifier.
        """
        self.current_metrics = SessionMetrics(session_id=session_id)

    def record_tool_call(self, *, error: bool = False) -> None:
        """Record a tool call event.

        Args:
            error: Whether the tool call errored.
        """
        if self.current_metrics:
            self.current_metrics.tool_calls += 1
            if error:
                self.current_metrics.tool_errors += 1

    def record_user_correction(self) -> None:
        """Record a user correction/override."""
        if self.current_metrics:
            self.current_metrics.user_corrections += 1

    def record_undo(self) -> None:
        """Record an undo operation."""
        if self.current_metrics:
            self.current_metrics.undos_performed += 1

    def record_test_results(self, passed: int, failed: int) -> None:
        """Record test results.

        Args:
            passed: Number of passing tests.
            failed: Number of failing tests.
        """
        if self.current_metrics:
            self.current_metrics.tests_passed = passed
            self.current_metrics.tests_failed = failed

    def end_session(self) -> SelfAssessment | None:
        """End the current session and generate a self-assessment.

        Returns:
            SelfAssessment or None if no session was active.
        """
        if not self.current_metrics:
            return None

        self.current_metrics.end_time = time.time()
        assessment = assess_session(self.current_metrics)
        self.record.add_assessment(assessment)
        self._save()

        logger.info(
            "Session %s: rating=%s, efficiency=%.0f%%",
            self.current_metrics.session_id,
            assessment.rating,
            assessment.efficiency_score * 100,
        )

        self.current_metrics = None
        return assessment

    def get_improvement_prompt(self) -> str:
        """Get the improvement prompt for injection into the system message.

        Returns:
            Markdown string with improvement insights, or empty string.
        """
        return generate_improvement_prompt(self.record)

    async def wrap_model_call(
        self,
        request: ModelRequest[ContextT],
        call_next: Any,
        runtime: Any,
    ) -> ModelResponse[ResponseT]:
        """Track model calls and increment turn count."""
        if self.current_metrics:
            self.current_metrics.model_calls += 1
            self.current_metrics.total_turns += 1
        return await call_next(request, runtime)
