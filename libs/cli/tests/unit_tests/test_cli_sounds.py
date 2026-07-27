"""Tests for the notification-sound toggle — off by default (no beep on response)."""

from __future__ import annotations

import importlib

import pytest


def _reload_with_env(monkeypatch: pytest.MonkeyPatch, value: str | None) -> object:
    """Reimport `cli_sounds` with BOG_AGENTS_SOUNDS set (or unset) to `value`."""
    if value is None:
        monkeypatch.delenv("BOG_AGENTS_SOUNDS", raising=False)
    else:
        monkeypatch.setenv("BOG_AGENTS_SOUNDS", value)
    from bog_agents_cli import cli_sounds

    return importlib.reload(cli_sounds)


def test_sounds_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """With BOG_AGENTS_SOUNDS unset, sounds are disabled — no beep on every response."""
    mod = _reload_with_env(monkeypatch, None)
    assert mod.is_sound_enabled() is False


@pytest.mark.parametrize("truthy", ["1", "true", "yes", "on", "TRUE", "On"])
def test_explicit_opt_in_enables(monkeypatch: pytest.MonkeyPatch, truthy: str) -> None:
    mod = _reload_with_env(monkeypatch, truthy)
    assert mod.is_sound_enabled() is True


@pytest.mark.parametrize("falsy", ["0", "false", "no", "off", "", "garbage"])
def test_non_truthy_stays_disabled(monkeypatch: pytest.MonkeyPatch, falsy: str) -> None:
    mod = _reload_with_env(monkeypatch, falsy)
    assert mod.is_sound_enabled() is False


def test_toggle_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _reload_with_env(monkeypatch, None)
    assert mod.is_sound_enabled() is False
    mod.toggle_sounds(True)
    assert mod.is_sound_enabled() is True
    mod.toggle_sounds(False)
    assert mod.is_sound_enabled() is False
