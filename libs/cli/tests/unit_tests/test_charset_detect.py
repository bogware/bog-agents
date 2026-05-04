"""Unit tests for ``_detect_charset_mode``.

The policy decides whether the welcome banner uses unicode
block-drawing or the ASCII fallback. Pre-0.8.0 the auto-detect biased
toward ASCII any time stdout's encoding wasn't UTF-*. That silently
downgraded most Windows users (where the default reported encoding is
``cp1252``) to the boring banner. The policy now defaults to UNICODE
unless the encoding is on a known legacy DOS allow-list.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import patch

import pytest

from bog_agents_cli.config import CharsetMode, _detect_charset_mode


def _no_lang_env() -> dict[str, str]:
    """Env without LANG/LC_ALL/UI_CHARSET_MODE so the encoding path is exercised."""
    return {k: v for k, v in os.environ.items() if k not in {"LANG", "LC_ALL", "UI_CHARSET_MODE"}}


class _FakeStdout:  # noqa: B903 — explicit class clarifies intent in tests
    def __init__(self, encoding: str) -> None:
        self.encoding = encoding


@pytest.mark.parametrize("encoding", ["utf-8", "UTF-8", "utf-16"])
def test_utf_encoding_wins(encoding: str):
    with (
        patch.object(sys, "stdout", _FakeStdout(encoding)),
        patch.dict(os.environ, _no_lang_env(), clear=True),
    ):
        assert _detect_charset_mode() == CharsetMode.UNICODE


def test_cp1252_now_returns_unicode():
    """Regression: Windows pwsh reports cp1252 even on modern terminals."""
    with (
        patch.object(sys, "stdout", _FakeStdout("cp1252")),
        patch.dict(os.environ, _no_lang_env(), clear=True),
    ):
        assert _detect_charset_mode() == CharsetMode.UNICODE


@pytest.mark.parametrize(
    "legacy_encoding",
    ["cp437", "cp850", "cp866", "ibm437", "ibm850", "ibm866", "CP437", "Cp850"],
)
def test_legacy_dos_codepages_use_ascii(legacy_encoding: str):
    with (
        patch.object(sys, "stdout", _FakeStdout(legacy_encoding)),
        patch.dict(os.environ, _no_lang_env(), clear=True),
    ):
        assert _detect_charset_mode() == CharsetMode.ASCII


def test_explicit_unicode_env_wins_over_legacy_codepage():
    with (
        patch.object(sys, "stdout", _FakeStdout("cp437")),
        patch.dict(os.environ, {"UI_CHARSET_MODE": "unicode"}, clear=True),
    ):
        assert _detect_charset_mode() == CharsetMode.UNICODE


def test_explicit_ascii_env_wins_over_utf():
    with (
        patch.object(sys, "stdout", _FakeStdout("utf-8")),
        patch.dict(os.environ, {"UI_CHARSET_MODE": "ascii"}, clear=True),
    ):
        assert _detect_charset_mode() == CharsetMode.ASCII


def test_lang_with_utf_picks_unicode():
    with (
        patch.object(sys, "stdout", _FakeStdout("ascii")),
        patch.dict(os.environ, {"LANG": "en_US.UTF-8"}, clear=True),
    ):
        assert _detect_charset_mode() == CharsetMode.UNICODE


def test_unknown_encoding_defaults_to_unicode():
    """The policy default — modern terminals get the cool banner."""
    with (
        patch.object(sys, "stdout", _FakeStdout("ascii")),
        patch.dict(os.environ, _no_lang_env(), clear=True),
    ):
        assert _detect_charset_mode() == CharsetMode.UNICODE


def test_missing_encoding_attribute_defaults_to_unicode():
    """A stdout that doesn't have ``.encoding`` shouldn't crash and shouldn't downgrade."""
    class _NoEncoding:
        pass

    with (
        patch.object(sys, "stdout", _NoEncoding()),
        patch.dict(os.environ, _no_lang_env(), clear=True),
    ):
        assert _detect_charset_mode() == CharsetMode.UNICODE
