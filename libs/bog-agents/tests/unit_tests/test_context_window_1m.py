"""Regression tests for 1M-context model support.

The audit (docs/PRINCIPAL_REVIEW.md §1, item 3) flagged that
``adaptive_context.MODEL_CONTEXT_WINDOWS`` and
``cost_tracker._CONTEXT_WINDOWS`` hardcoded 200K and silently truncated
1M-capable models (Opus 4.7) to the legacy default. The fix is twofold:

1. Both dicts now carry explicit ``claude-opus-4-7: 1_000_000`` entries.
2. ``detect_context_window`` consults the installed provider package's
   ``_PROFILES`` module first, so new 1M+ models added upstream don't
   need a code change here.

These tests pin both halves so the regression cannot return silently.
"""

from __future__ import annotations

from bog_agents.middleware.adaptive_context import (
    MODEL_CONTEXT_WINDOWS,
    detect_context_window,
)
from bog_agents.middleware.cost_tracker import _CONTEXT_WINDOWS, CostTracker


class TestCuratedTableHasOneMillionEntries:
    """The fallback dicts know about claude-opus-4-7."""

    def test_adaptive_context_lists_opus_4_7_at_1m(self) -> None:
        assert MODEL_CONTEXT_WINDOWS["claude-opus-4-7"] == 1_000_000

    def test_cost_tracker_lists_opus_4_7_at_1m(self) -> None:
        assert _CONTEXT_WINDOWS["claude-opus-4-7"] == 1_000_000

    def test_partial_match_still_works_for_versioned_ids(self) -> None:
        """``claude-opus-4-7-20250219`` resolves via partial match."""
        window = detect_context_window("claude-opus-4-7-20250219", default=128_000)
        assert window == 1_000_000


class TestDetectContextWindow:
    """``detect_context_window`` resolves correctly across the lookup chain."""

    def test_direct_hit_in_curated_table(self) -> None:
        # Upstream langchain-anthropic now reports 1M for sonnet-4-6
        # since Anthropic flipped the 1M beta on by default. Haiku 4.5
        # is the current-generation 200K-window canary — swap to
        # whichever Claude generation is still 200K if upstream moves
        # this one too. (Don't use claude-3-haiku — that's deprecated.)
        assert detect_context_window("claude-haiku-4-5") == 200_000

    def test_opus_4_7_resolves_to_at_least_1m(self) -> None:
        """The flagged regression: opus-4-7 must not silently degrade to 200K.

        Pinned loosely (``>= 1M``) because the upstream
        ``langchain_anthropic`` profile is the source of truth and
        could legitimately bump higher in a future release. The audit
        flagged the *truncation* bug — that's what we're guarding.
        """
        assert detect_context_window("claude-opus-4-7") >= 1_000_000

    def test_gemini_pro_is_large_context(self) -> None:
        """Generic large-context check — exact value depends on installed profile."""
        # Whatever the installed profile says, gemini 1.5/2.5 pro is
        # decisively not 200K. We don't care whether the upstream
        # profile lists 1M or 2M — only that it's >= 1M.
        assert detect_context_window("gemini-1.5-pro") >= 1_000_000
        assert detect_context_window("gemini-2.5-pro") >= 1_000_000

    def test_gpt5_resolves_above_legacy_200k(self) -> None:
        """GPT-5 is in the curated fallback at 1M when no upstream profile exists."""
        # langchain_openai may or may not have gpt-5 in its profile;
        # either way the value must be much larger than the legacy
        # 200K hardcode the audit flagged.
        assert detect_context_window("gpt-5") > 200_000

    def test_provider_prefix_is_stripped(self) -> None:
        assert detect_context_window("anthropic:claude-opus-4-7") >= 1_000_000
        # Haiku 4.5 is a current-generation 200K-window model.
        # Demonstrates that the provider prefix is stripped before
        # the curated/profile lookup.
        assert detect_context_window("anthropic:claude-haiku-4-5") == 200_000

    def test_unknown_model_uses_default(self) -> None:
        assert detect_context_window("fictional-mega-llm-9000", default=42) == 42


class TestCostTrackerRoutesThroughDetect:
    """``CostTracker.context_window_size`` calls ``detect_context_window``.

    Without this routing, a 1M-context model whose entry is in the
    upstream provider profile but not in cost_tracker's curated table
    would silently fall back to 200K — exactly the bug the audit flagged.
    """

    def test_one_million_model_resolves_to_one_million(self) -> None:
        tracker = CostTracker(model_name="claude-opus-4-7")
        assert tracker.context_window_size == 1_000_000

    def test_known_200k_model_resolves_to_200k(self) -> None:
        # See note on test_direct_hit_in_curated_table — sonnet-4-6
        # moved to 1M upstream. Haiku 4.5 is the current canary for
        # the "routing handles 200K" path.
        tracker = CostTracker(model_name="claude-haiku-4-5")
        assert tracker.context_window_size == 200_000

    def test_unknown_model_uses_curated_fallback_default(self) -> None:
        tracker = CostTracker(model_name="totally-unknown-model")
        # No curated entry, no upstream profile → 200_000 (the legacy
        # hardcoded default, preserved for callers that don't have a
        # provider package installed).
        assert tracker.context_window_size == 200_000
