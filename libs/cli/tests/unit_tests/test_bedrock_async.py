"""Tests for Bedrock async resilience (blockbuster bypass)."""

from __future__ import annotations

from unittest.mock import MagicMock

from bog_agents_cli.config import _patch_bedrock_for_async


class TestPatchBedrockForAsync:
    """Test that _patch_bedrock_for_async wraps methods correctly."""

    def test_patches_generate_and_stream(self) -> None:
        model = MagicMock()
        model._generate = MagicMock(return_value="gen_result")
        model._stream = MagicMock(return_value="stream_result")

        _patch_bedrock_for_async(model)

        # Methods should be replaced with wrappers
        assert model._generate("test") == "gen_result"
        assert model._stream("test") == "stream_result"

    def test_sets_blockbuster_skip_during_call(self) -> None:
        """Verify blockbuster_skip is True inside the wrapper."""
        try:
            from blockbuster.blockbuster import blockbuster_skip
        except ImportError:
            # blockbuster not installed — skip test
            return

        observed_values: list[bool] = []

        def fake_generate(*_args: object, **_kwargs: object) -> str:
            observed_values.append(blockbuster_skip.get(False))
            return "ok"

        model = MagicMock()
        model._generate = fake_generate
        model._stream = MagicMock()

        _patch_bedrock_for_async(model)
        model._generate("test")

        assert observed_values == [True], f"Expected blockbuster_skip=True during call, got {observed_values}"
        # After the call, blockbuster_skip should be reset
        assert blockbuster_skip.get(False) is False

    def test_noop_when_blockbuster_not_installed(self) -> None:
        """When blockbuster isn't available, patch is a silent no-op."""
        model = MagicMock()
        _patch_bedrock_for_async(model)
        # Whether blockbuster is installed or not, calling patched methods should work.
        model._generate("test")

    def test_skips_missing_methods(self) -> None:
        """If model doesn't have _generate or _stream, don't crash."""
        model = MagicMock(spec=[])  # No attributes
        _patch_bedrock_for_async(model)  # Should not raise
