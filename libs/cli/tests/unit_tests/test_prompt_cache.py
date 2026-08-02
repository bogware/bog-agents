"""Unit tests for the prompt-cache-break awareness helpers.

The CLI's SDK stack installs prompt-caching middleware for Anthropic and
Bedrock providers (see `bog_agents/graph.py`). ``/compact`` and ``/clear``
rewrite the message history the next request sees, breaking the provider-side
cache key; these helpers decide when to surface that and render the note.
"""

from __future__ import annotations

import pytest

from bog_agents_cli.prompt_cache import (
    cache_break_note,
    provider_supports_prompt_caching,
    thread_reset_message,
)


class TestProviderSupportsPromptCaching:
    @pytest.mark.parametrize(
        "provider",
        [
            "anthropic",
            "ANTHROPIC",
            "bedrock",
            "bedrock_converse",
            "vertex",
            " Bedrock ",
        ],
    )
    def test_caching_providers(self, provider: str) -> None:
        assert provider_supports_prompt_caching(provider) is True

    @pytest.mark.parametrize(
        "provider",
        ["openai", "google_genai", "fireworks", "xai", "ollama", "nonexistent", ""],
    )
    def test_non_caching_providers(self, provider: str) -> None:
        assert provider_supports_prompt_caching(provider) is False

    def test_none_provider(self) -> None:
        assert provider_supports_prompt_caching(None) is False


class TestCacheBreakNote:
    def test_caching_provider_returns_note(self) -> None:
        note = cache_break_note("anthropic")
        assert "prompt cache cleared" in note
        assert "uncached input rate" in note

    def test_non_caching_provider_returns_empty(self) -> None:
        assert cache_break_note("openai") == ""

    def test_none_provider_returns_empty(self) -> None:
        assert cache_break_note(None) == ""


class TestThreadResetMessage:
    def test_returns_app_message(self) -> None:
        from bog_agents_cli.widgets.messages import AppMessage

        msg = thread_reset_message("thread-1", "anthropic")
        assert isinstance(msg, AppMessage)
        assert "Started new thread: thread-1" in msg._content

    def test_appends_note_for_caching_provider(self) -> None:
        msg = thread_reset_message("thread-2", "anthropic")
        assert "prompt cache cleared" in msg._content

    def test_omits_note_for_non_caching_provider(self) -> None:
        msg = thread_reset_message("thread-3", "openai")
        assert "Started new thread: thread-3" in msg._content
        assert "prompt cache cleared" not in msg._content
