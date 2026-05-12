"""Tests for the thinking-config resolver in `bog_agents_cli.agent`.

`_resolve_thinking_config` is the bridge between the user's config.toml
(or env vars) and the SDK's `ThinkingMiddleware`. The CLI's agent wiring
calls it at agent-build time to decide whether to start a session with
thinking on/off and at what budget.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest  # noqa: TC002 — runtime use via pytest.MonkeyPatch type

from bog_agents_cli.agent import _resolve_thinking_config

if TYPE_CHECKING:
    from pathlib import Path


class TestResolveThinkingConfig:
    """Env / config / default precedence."""

    def test_default_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("BOG_AGENTS_THINKING", raising=False)
        monkeypatch.delenv("BOG_AGENTS_THINKING_BUDGET", raising=False)
        enabled, budget = _resolve_thinking_config()
        assert enabled is False
        assert budget == 8_000

    def test_env_enable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BOG_AGENTS_THINKING", "1")
        monkeypatch.delenv("BOG_AGENTS_THINKING_BUDGET", raising=False)
        enabled, budget = _resolve_thinking_config()
        assert enabled is True
        assert budget == 8_000

    def test_env_disable_explicit_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BOG_AGENTS_THINKING", "false")
        enabled, _ = _resolve_thinking_config()
        assert enabled is False

    def test_env_budget_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BOG_AGENTS_THINKING", "1")
        monkeypatch.setenv("BOG_AGENTS_THINKING_BUDGET", "16000")
        enabled, budget = _resolve_thinking_config()
        assert enabled is True
        assert budget == 16_000

    def test_env_budget_clamped_to_minimum(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Budgets below 1000 should be clamped to keep providers happy.
        monkeypatch.setenv("BOG_AGENTS_THINKING", "1")
        monkeypatch.setenv("BOG_AGENTS_THINKING_BUDGET", "10")
        _, budget = _resolve_thinking_config()
        assert budget >= 1_000

    def test_env_invalid_budget_falls_back_to_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BOG_AGENTS_THINKING", "1")
        monkeypatch.setenv("BOG_AGENTS_THINKING_BUDGET", "not-an-int")
        _, budget = _resolve_thinking_config()
        assert budget == 8_000

    def test_config_toml_enables_thinking(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """When env vars are absent, config.toml drives the verdict."""
        # Build a temporary config.toml with thinking_enabled for anthropic.
        config_path = tmp_path / "config.toml"
        config_path.write_text(
            """
[models]

[models.providers.anthropic]
api_key_env = "ANTHROPIC_API_KEY"

[models.providers.anthropic.params]
thinking_enabled = true
thinking_budget_tokens = 12000
""",
            encoding="utf-8",
        )

        monkeypatch.delenv("BOG_AGENTS_THINKING", raising=False)
        monkeypatch.delenv("BOG_AGENTS_THINKING_BUDGET", raising=False)
        monkeypatch.setattr(
            "bog_agents_cli.model_config.DEFAULT_CONFIG_PATH", config_path
        )

        # Settings must report the matching provider for config lookup.
        from bog_agents_cli.config import settings

        original_provider = getattr(settings, "model_provider", "")
        try:
            settings.model_provider = "anthropic"
            from bog_agents_cli.model_config import clear_caches

            clear_caches()
            enabled, budget = _resolve_thinking_config()
            assert enabled is True
            assert budget == 12_000
        finally:
            settings.model_provider = original_provider
            from bog_agents_cli.model_config import clear_caches

            clear_caches()


class TestThinkingKwargsStrippedFromModelConstructor:
    """Middleware keys must not be forwarded to ``init_chat_model``."""

    def test_thinking_keys_dropped(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        config_path = tmp_path / "config.toml"
        config_path.write_text(
            """
[models]

[models.providers.anthropic]
api_key_env = "ANTHROPIC_API_KEY"

[models.providers.anthropic.params]
temperature = 0.7
thinking_enabled = true
thinking_budget_tokens = 8000
""",
            encoding="utf-8",
        )
        monkeypatch.setattr(
            "bog_agents_cli.model_config.DEFAULT_CONFIG_PATH", config_path
        )
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

        from bog_agents_cli.config import _get_provider_kwargs
        from bog_agents_cli.model_config import clear_caches

        clear_caches()
        try:
            kwargs = _get_provider_kwargs("anthropic", model_name="claude-sonnet-4-6")
        finally:
            clear_caches()

        # The non-thinking key must survive; thinking-* must be removed.
        assert kwargs.get("temperature") == 0.7
        assert "thinking_enabled" not in kwargs
        assert "thinking_budget_tokens" not in kwargs
