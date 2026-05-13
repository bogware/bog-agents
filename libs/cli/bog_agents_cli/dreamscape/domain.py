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
# Domain → seed-category preferences
# ---------------------------------------------------------------------------


# Order matters: categories listed first get more weight. The
# ``pick_seeds`` flow consumes categories as a flat pool, but a future
# refinement could weight per-category. For now the simple "prefer
# these N categories first" pattern reduces creative drift on
# engineering agents without removing categories entirely.

_DOMAIN_SEED_PREFERENCES: dict[Domain, tuple[str, ...]] = {
    "engineering": (
        "computing-history",  # primary — these resonate with engineers
        "history",  # secondary — engineering's longer tail
        "space",  # tertiary — large-scale systems metaphor
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
    """
    with suppress(Exception):
        return classify_agent_domain(load_agent_profile(agent_id))
    return "general"
