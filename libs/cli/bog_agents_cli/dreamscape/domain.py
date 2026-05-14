"""Context-aware dreaming: classify the agent's domain, weight seeds accordingly.

Phases 10 + 11 + 12 established that imagination injection's effect is
**domain-conditional**: dreams help on creative / design prompts and
either hurt or are noisy on technical-debugging prompts. The seed
library itself is already mixed (computing-history, myth, nature,
space, history) — but every dream draws across all five categories
uniformly, which produces creative-leaning content regardless of who
the agent is.

This module makes dreaming context-aware. At dream-fire time, the
engine reads the agent's profile (its system prompt as captured by
``capture_agent_profile``) and classifies the agent's working domain.
The classification then steers seed selection so an engineering-focused
agent dreams in language closer to its own work, and a
narrative-focused agent gets the evocative imagery the Phase 11
result depends on.

Classification is **keyword-based** by design: cheap, deterministic,
testable, no hidden LLM call on every dream. The trade-off is
brittleness — a system prompt with no recognizable signal falls back
to ``"general"`` and dreams uniformly across categories. That's a
safer failure mode than a misclassification.

Public API:

* ``capture_agent_profile(agent_id, profile_text)`` — call at agent
  build time. Persists ``~/.bog-agents/agents/<id>/agent_profile.txt``.
* ``classify_agent_domain(profile)`` — pure function, returns one of
  ``"engineering" | "creative" | "research" | "general"``.
* ``preferred_seed_categories(domain, available)`` — returns the seed
  categories to prefer, ordered. Falls back to ``available`` when
  the domain is ``"general"``.
* ``recommended_injection_style(domain)`` — recommends ``"dreams"`` or
  ``"neutral"`` based on Phase 10-12 data.
"""

from __future__ import annotations

import logging
import re
from contextlib import suppress
from pathlib import Path
from typing import Literal

from bog_agents_cli.dreamscape.lifecycle import agent_state_dir

logger = logging.getLogger(__name__)


Domain = Literal["engineering", "creative", "research", "general"]


_PROFILE_FILENAME = "agent_profile.txt"
_MAX_PROFILE_CHARS = 8_000  # bound the on-disk profile; classifier only needs a sample


# ---------------------------------------------------------------------------
# Keyword vocabularies
# ---------------------------------------------------------------------------
#
# These were hand-curated from the system prompts of the bog-agents
# CLI, Claude Code, the harbor evaluation suite, and a handful of
# example creative-coding agents in the wild. They are not exhaustive
# — they're intended as a baseline that a future PR can extend by
# appending to ``_DOMAIN_KEYWORDS`` without other code changes.


_DOMAIN_KEYWORDS: dict[Domain, tuple[str, ...]] = {
    "engineering": (
        # Code-adjacent verbs
        "code",
        "coding",
        "compile",
        "debug",
        "debugging",
        "refactor",
        "refactoring",
        "implement",
        "implementation",
        "lint",
        "test",
        "testing",
        "unit test",
        "integration test",
        "stack trace",
        "traceback",
        "exception",
        "error message",
        "merge conflict",
        "pull request",
        "code review",
        # Concrete software nouns
        "function",
        "class",
        "module",
        "package",
        "dependency",
        "branch",
        "commit",
        "repository",
        "repo",
        "build",
        "compile",
        "compiler",
        "interpreter",
        "runtime",
        "binary",
        "library",
        # Engineering practices
        "ci",
        "cd",
        "deploy",
        "deployment",
        "rollback",
        "feature flag",
        "telemetry",
        "metrics",
        "logging",
        "observability",
        # Specific languages / tools (sparingly)
        "python",
        "typescript",
        "rust",
        "golang",
        "make",
        "pytest",
        "ruff",
        "mypy",
        "git ",
    ),
    "creative": (
        # Output-as-artifact
        "design",
        "designer",
        "designing",
        "ux",
        "ui",
        "user experience",
        "story",
        "narrative",
        "voice",
        "tone",
        "copywriting",
        "copy",
        "microcopy",
        "naming",
        "metaphor",
        # Soft skills / emotional vocabulary
        "elegant",
        "evocative",
        "delightful",
        "playful",
        "memorable",
        "audience",
        "user-facing",
        "personality",
        "character",
        # Domains the creative class typically inhabits
        "game",
        "branding",
        "marketing",
        "illustration",
        "writing",
        "fiction",
        "screenplay",
        "songwriting",
    ),
    "research": (
        "research",
        "study",
        "experiment",
        "evaluation",
        "evaluate",
        "compare",
        "comparison",
        "ablation",
        "hypothesis",
        "literature",
        "survey",
        "benchmark",
        "benchmarking",
        "measure",
        "measurement",
        "dataset",
        "data analysis",
        "statistical",
        "regression",
        "correlation",
        "causal",
    ),
}

# Minimum signal strength to commit to a domain. Below this the
# classifier prefers ``"general"`` (the safest fallback).
_MIN_KEYWORD_HITS = 2


# ---------------------------------------------------------------------------
# Profile persistence
# ---------------------------------------------------------------------------


def profile_path(agent_id: str) -> Path:
    """Return the on-disk path of the agent profile file."""
    return agent_state_dir(agent_id) / _PROFILE_FILENAME


def capture_agent_profile(agent_id: str, profile_text: str) -> bool:
    """Persist a snippet of the agent's system prompt for later classification.

    Called once at agent build time. The on-disk file is a plain text
    snapshot truncated to ``_MAX_PROFILE_CHARS`` characters; the
    classifier reads it on every dream-fire (cheap — local disk).

    Returns whether the write succeeded. Never raises.
    """
    if not isinstance(profile_text, str):
        return False
    try:
        path = profile_path(agent_id)
        path.write_text(profile_text[:_MAX_PROFILE_CHARS], encoding="utf-8")
    except OSError as exc:
        logger.warning("dreamscape: agent-profile write failed (%s): %s", agent_id, exc)
        return False
    return True


def load_agent_profile(agent_id: str) -> str:
    """Read the persisted agent profile. Returns ``""`` if absent or unreadable."""
    path = profile_path(agent_id)
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("dreamscape: agent-profile read failed (%s): %s", agent_id, exc)
        return ""


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


_WORD_RE = re.compile(r"\b[\w-]+\b", re.UNICODE)


def classify_agent_domain(profile: str) -> Domain:
    """Classify an agent's working domain from its system prompt / profile.

    Pure function — no I/O, no LLM calls. Keyword counts per domain,
    then pick the strongest. Below ``_MIN_KEYWORD_HITS`` of total
    domain signal we fall back to ``"general"`` (the seed library's
    uniform mode).

    Args:
        profile: Text to classify. Usually the agent's resolved system
            prompt; empty string is valid input.

    Returns:
        ``"engineering" | "creative" | "research" | "general"``.
    """
    if not profile:
        return "general"
    text = profile.lower()
    # Substring scan is fine here — keywords are short, the profile is
    # bounded. The result is the count of distinct keyword *types* hit
    # per domain, not raw occurrences, so a profile that says "code" 50
    # times doesn't dominate one that says "code, refactor, debug".
    scores: dict[Domain, int] = dict.fromkeys(_DOMAIN_KEYWORDS, 0)
    for domain, words in _DOMAIN_KEYWORDS.items():
        seen: set[str] = set()
        for word in words:
            if word in text and word not in seen:
                scores[domain] += 1
                seen.add(word)
    total_signal = sum(scores.values())
    if total_signal < _MIN_KEYWORD_HITS:
        return "general"
    winner = max(scores.items(), key=lambda kv: kv[1])[0]
    if scores[winner] == 0:
        return "general"
    # Margin check — if the top-2 are within 1 hit of each other, the
    # signal is ambiguous; prefer the safer general fallback. This
    # avoids brittleness on profiles like "engineering creative" that
    # tie cleanly.
    second = sorted(scores.values(), reverse=True)[1]
    if scores[winner] - second < 1:
        return "general"
    return winner


# ---------------------------------------------------------------------------
# Phase 17 — per-prompt classification
# ---------------------------------------------------------------------------

# A user prompt can have engineering surface vocabulary but be
# decision-shaped underneath ("should I extract subclasses or rewrite
# behind a feature flag?" reads as engineering but is a judgment
# question that benefits from creative routing). Phase 14 showed
# `legacy-deletion` at 55% treatment-win vs ~25% for the other
# technical scenarios — almost certainly because it's decision-shaped.
# This pattern set catches the lexical signal.

_DECISION_PATTERNS: tuple[str, ...] = (
    "should i",
    "would you call",
    "would you recommend",
    "what would you",
    "what's the right way",
    "what is the right way",
    "right way to",
    "decide between",
    "decision",
    "which approach",
    "which one",
    "is it better",
    "is it worth",
    "trade-off",
    "tradeoff",
    "trade off",
    "which lens",
    "name for",
    "what would you call",
)


def _has_decision_signal(prompt: str) -> bool:
    """Detect 'decision-shaped' phrasing in a user prompt.

    Returns True if any pattern from ``_DECISION_PATTERNS`` is a
    substring of the prompt (case-insensitive). The signal is
    deliberately conservative — overlapping rather than alternative
    interpretations of the same prompt.
    """
    if not prompt:
        return False
    text = prompt.lower()
    return any(pat in text for pat in _DECISION_PATTERNS)


def classify_prompt_domain(prompt: str) -> Domain:
    """Classify a user PROMPT (not a system prompt).

    Reuses the agent-profile classifier vocabulary, then applies a
    decision-signal override: prompts asking "what should I" or
    "which approach" benefit from creative routing even when their
    surface vocabulary is technical.

    Phase 17 hypothesis: this per-prompt routing fixes the
    ``legacy-deletion`` Phase 14 outlier — a technical-classified
    scenario whose 55% treatment-win rate strongly hints it's
    actually decision-shaped.

    Args:
        prompt: The user's prompt text. Empty input → ``"general"``.

    Returns:
        ``"engineering" | "creative" | "research" | "general"``.
        Specifically: ``"creative"`` if a decision-signal is present
        AND the base classification is engineering or general; the
        base classification otherwise.
    """
    base = classify_agent_domain(prompt)
    if _has_decision_signal(prompt) and base in ("engineering", "general"):
        return "creative"
    return base


# ---------------------------------------------------------------------------
# Domain → seed-category preferences
# ---------------------------------------------------------------------------


# Order matters: categories listed first get more weight. The
# ``pick_seeds`` flow consumes categories as a flat pool, but a future
# refinement could weight per-category. For now the simple "prefer
# these N categories first" pattern reduces creative drift on
# engineering agents without removing categories entirely.

_DOMAIN_SEED_PREFERENCES: dict[Domain, tuple[str, ...]] = {
    "engineering": (
        # engineering-craft (added Phase 15) leads — the day-to-day
        # "bug live for six years" texture resonates with engineering
        # work in a way the historical figures don't. computing-history
        # stays as the secondary mix.
        "engineering-craft",  # primary — Phase 15 seeds
        "computing-history",  # secondary — Hopper, Hamilton, Engelbart
        "history",  # tertiary — engineering's longer arc
        "space",  # quaternary — large-scale-systems metaphor
        # "myth", "nature" are intentionally de-emphasized
    ),
    "creative": (
        "myth",  # primary — evocative imagery wins
        "nature",
        "history",
        "space",
        "computing-history",  # tertiary — the original Phase-1 mix
    ),
    "research": (
        "history",  # primary — research has long memory
        "computing-history",
        "engineering-craft",  # secondary — research is craft-adjacent
        "space",
        "nature",
    ),
    "general": (
        "nature",
        "space",
        "history",
        "myth",
        "computing-history",
    ),
}


def preferred_seed_categories(
    domain: Domain, *, available: list[str] | None = None
) -> list[str]:
    """Return the preferred seed categories for a domain, in priority order.

    Args:
        domain: Classification output from :func:`classify_agent_domain`.
        available: Optional whitelist of categories the seed library
            actually has. When provided, the result is filtered.

    Returns:
        Ordered list of category names. Empty list means "fall back
        to draw from every available category."
    """
    prefs = list(
        _DOMAIN_SEED_PREFERENCES.get(domain, _DOMAIN_SEED_PREFERENCES["general"])
    )
    if available is None:
        return prefs
    avail_set = set(available)
    return [c for c in prefs if c in avail_set]


def recommended_injection_style(domain: Domain) -> Literal["dreams", "neutral"]:
    """Pick the injection wrapper to use for a given domain.

    Based on Phases 10-12:
    * Creative agents → ``"dreams"`` (Phase 11 saw 6/7 treatment wins
      on creative prompts with the evocative wrapper).
    * Engineering / research / general → ``"neutral"`` (Phase 10
      saw the dreams wrapper penalized as "filler metaphor"; Phase 12
      saw the neutral wrapper roughly even with control).
    """
    if domain == "creative":
        return "dreams"
    return "neutral"


# ---------------------------------------------------------------------------
# Convenience: end-to-end domain lookup for a given agent_id
# ---------------------------------------------------------------------------


def resolve_agent_domain(agent_id: str) -> Domain:
    """End-to-end: read the persisted profile and classify the domain.

    Returns ``"general"`` if no profile is on disk yet — that matches
    the dreamscape's existing "fall back gracefully" pattern.

    Phase 19 — also consults the LLM-classification cache on disk
    (``agent_state_dir(agent_id) / "domain_llm.txt"``) when the
    keyword classifier returns ``"general"``. The cache is populated
    by :func:`classify_agent_domain_llm_async` at agent build time
    for profiles whose keyword signal is too weak to commit to a
    specific domain.
    """
    with suppress(Exception):
        profile = load_agent_profile(agent_id)
        keyword_result = classify_agent_domain(profile)
        if keyword_result != "general":
            return keyword_result
        # Try the LLM-classifier cache as the fallback.
        cached = _load_cached_llm_domain(agent_id)
        if cached is not None:
            return cached
        return keyword_result
    return "general"


# ---------------------------------------------------------------------------
# Phase 19 — LLM classifier fallback
# ---------------------------------------------------------------------------
#
# The keyword classifier is fast + deterministic, but it falls back
# to ``"general"`` on profiles where no domain has a margin of >= 2
# keyword hits over its nearest competitor. That fallback is the
# safer failure mode (the seed library's uniform mode), but it
# leaves a long tail of plausibly-classifiable profiles on the
# table.
#
# Phase 19 adds an optional **LLM-based** classifier that only fires
# on the long tail. One Haiku call at agent build time, cached to
# disk, then read for free on every subsequent dream/injection
# cycle. The cache means we pay roughly $0.001 per agent build, not
# per dream.


_LLM_CACHE_FILENAME = "domain_llm.txt"

_LLM_CLASSIFIER_SYSTEM = (
    "You classify an AI coding assistant's working domain from a short "
    "system-prompt excerpt. Read the excerpt and pick exactly one label:"
    "\n\n"
    "* engineering — debugging, refactoring, writing tests, reasoning about "
    "tools and runtime. Picks specific tool names; lists concrete commands."
    "\n* creative — design, naming, error-message copy, voice and tone, "
    "metaphor, microcopy, illustration. Picks evocative framings."
    "\n* research — literature surveys, experiment design, statistical "
    "analysis, benchmark comparisons. Picks measurement methodologies."
    "\n* general — none of the above clearly dominates, or the profile is "
    "intentionally broad."
    "\n\nOutput STRICT JSON with no preamble:"
    '\n{"domain": "engineering" | "creative" | "research" | "general", '
    '"reasoning": "one short sentence"}'
)


def llm_cache_path(agent_id: str) -> Path:
    """Return the on-disk path of the cached LLM-classifier verdict."""
    return agent_state_dir(agent_id) / _LLM_CACHE_FILENAME


def _load_cached_llm_domain(agent_id: str) -> Domain | None:
    """Read the cached LLM classification. Returns None if absent or invalid."""
    path = llm_cache_path(agent_id)
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if text in ("engineering", "creative", "research", "general"):
        return text  # type: ignore[return-value]
    return None


def _save_cached_llm_domain(agent_id: str, domain: Domain) -> bool:
    """Persist the LLM classification verdict so future dreams skip the LLM call."""
    path = llm_cache_path(agent_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(domain, encoding="utf-8")
    except OSError as exc:
        logger.warning(
            "dreamscape: llm-classification cache write failed (%s): %s",
            agent_id,
            exc,
        )
        return False
    return True


async def classify_agent_domain_llm_async(profile: str, model: object) -> Domain:
    """Ask an LLM to classify the agent's domain.

    Used only as a fallback when the keyword classifier returns
    ``"general"`` — typically because the profile is intentionally
    short or its vocabulary doesn't intersect the keyword
    dictionaries. The classifier returns ``"general"`` itself when
    the LLM's verdict isn't one of the known labels.

    Args:
        profile: The agent's system-prompt excerpt.
        model: A LangChain ``BaseChatModel``. Pass ``Haiku 4.5`` or
            any cheap-and-fast model; the classification is a single
            short call.

    Returns:
        One of ``"engineering" | "creative" | "research" | "general"``.
        Never raises.
    """
    if not profile or not profile.strip():
        return "general"
    try:
        from langchain_core.messages import HumanMessage, SystemMessage

        resp = await model.ainvoke(  # type: ignore[attr-defined]
            [
                SystemMessage(content=_LLM_CLASSIFIER_SYSTEM),
                HumanMessage(content=f"## Excerpt\n\n{profile[:_MAX_PROFILE_CHARS]}"),
            ]
        )
    except Exception as exc:
        logger.warning("dreamscape: llm classifier call failed: %s", exc)
        return "general"

    text = str(getattr(resp, "content", "")).strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text
        if text.endswith("```"):
            text = text.rsplit("```", 1)[0]
    text = text.strip()
    import json as _json

    try:
        parsed = _json.loads(text)
    except (_json.JSONDecodeError, ValueError):
        return "general"
    label = parsed.get("domain", "") if isinstance(parsed, dict) else ""
    if label in ("engineering", "creative", "research", "general"):
        return label  # type: ignore[return-value]
    return "general"


async def classify_with_fallback_async(profile: str, model: object) -> Domain:
    """Keyword classifier first, LLM fallback only when keyword returns 'general'.

    The shipped pattern: cheap-and-deterministic for the modal case,
    one Haiku call for the long tail. Cache the LLM result with
    :func:`_save_cached_llm_domain` so subsequent dream cycles skip
    the call entirely.
    """
    keyword_result = classify_agent_domain(profile)
    if keyword_result != "general":
        return keyword_result
    return await classify_agent_domain_llm_async(profile, model)


_PROMPT_CLASSIFIER_SYSTEM = (
    "You classify a developer's question by its underlying shape. "
    "Pick exactly one label:"
    "\n\n"
    "* engineering — debug a stack trace, fix a specific tool, name a "
    "concrete command, diagnose a runtime issue. The asker wants a "
    "specific tool or step."
    "\n* creative — name something, choose a metaphor, draft user-facing "
    "copy, design API shape, decide between options where multiple are "
    "valid. The asker wants an evocative reframe or judgment."
    "\n* research — survey, compare benchmarks, design an experiment, "
    "analyze data."
    "\n* general — none of the above clearly dominates."
    "\n\n"
    "Surface vocabulary can mislead. 'I'm designing the retry policy' "
    "looks creative because of 'designing' but is engineering if the "
    "asker wants concrete retry-policy mechanics. Look at what the "
    "asker is actually asking for, not what words they use."
    "\n\n"
    "Output STRICT JSON with no preamble:"
    '\n{"domain": "engineering" | "creative" | "research" | "general", '
    '"reasoning": "one short sentence"}'
)


async def classify_prompt_domain_llm_async(prompt: str, model: object) -> Domain:
    """Phase 27 — classify a user PROMPT (not a profile) via an LLM call.

    Like :func:`classify_agent_domain_llm_async` but tuned for a
    user's short question rather than a system prompt. The classifier
    is instructed to look past surface vocabulary (the
    "designing the retry policy" Phase-17 false-positive) and judge
    the prompt's underlying SHAPE.

    Returns ``"general"`` on empty input, unparseable response,
    provider error, or unknown label. Never raises.
    """
    if not prompt or not prompt.strip():
        return "general"
    try:
        from langchain_core.messages import HumanMessage, SystemMessage

        resp = await model.ainvoke(  # type: ignore[attr-defined]
            [
                SystemMessage(content=_PROMPT_CLASSIFIER_SYSTEM),
                HumanMessage(content=f"## Prompt\n\n{prompt[:_MAX_PROFILE_CHARS]}"),
            ]
        )
    except Exception as exc:
        logger.warning("dreamscape: llm prompt classifier call failed: %s", exc)
        return "general"

    text = str(getattr(resp, "content", "")).strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text
        if text.endswith("```"):
            text = text.rsplit("```", 1)[0]
    text = text.strip()
    import json as _json

    try:
        parsed = _json.loads(text)
    except (_json.JSONDecodeError, ValueError):
        return "general"
    label = parsed.get("domain", "") if isinstance(parsed, dict) else ""
    if label in ("engineering", "creative", "research", "general"):
        return label  # type: ignore[return-value]
    return "general"
