"""Prompt-cache awareness for conversation-reset commands.

Providers bill the large static prefix (system prompt + earlier turns) at a
heavily discounted "cache read" rate. ``/compact`` and ``/clear`` rewrite the
message history the next request sees, so the provider-side cache key no
longer matches and the next turn is billed at the full uncached input rate.
This module decides when that is worth surfacing and renders the note.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bog_agents_cli.widgets.messages import AppMessage

# Providers whose SDK stack installs prompt-caching middleware (see
# `bog_agents/graph.py` -> `_append_prompt_caching_middleware`).
_PROMPT_CACHING_PROVIDERS: frozenset[str] = frozenset(
    {"anthropic", "bedrock", "bedrock_converse", "vertex"}
)

# Markup note appended to reset confirmations. Leading blank line keeps the
# note on its own paragraph; `[dim]` styling matches other AppMessage hints.
_CACHE_BREAK_NOTE = (
    "\n\n[dim]Note: prompt cache cleared \u2014 the next turn is billed at "
    "the full uncached input rate.[/dim]"
)


def provider_supports_prompt_caching(provider: str | None) -> bool:
    """Return whether `provider` bills cached-prefix reads.

    Args:
        provider: Normalized provider identifier (e.g. ``"anthropic"``), or
            `None` when no provider is configured.

    Returns:
        `True` when the provider's SDK stack enables prompt caching.
    """
    return (provider or "").strip().lower() in _PROMPT_CACHING_PROVIDERS


def cache_break_note(provider: str | None) -> str:
    """Return the prompt-cache-break note for `provider`, or an empty string.

    Args:
        provider: Normalized provider identifier used to decide whether prompt
            caching is active.

    Returns:
        The markup note when the provider supports prompt caching, and an
        empty string otherwise so callers stay silent for non-caching
        providers.
    """
    if not provider_supports_prompt_caching(provider):
        return ""
    return _CACHE_BREAK_NOTE


def thread_reset_message(thread_id: str, provider: str | None) -> AppMessage:
    """Build the `/clear` confirmation, noting any prompt-cache break.

    Args:
        thread_id: The freshly allocated thread identifier.
        provider: Normalized provider identifier used to decide whether prompt
            caching is active.

    Returns:
        An `AppMessage` confirming the thread reset, with the cache-break
        note appended when the active provider supports prompt caching.
    """
    from bog_agents_cli.widgets.messages import AppMessage

    return AppMessage(f"Started new thread: {thread_id}{cache_break_note(provider)}")
