"""Hardening tests for repo_map source scanning (S6).

Verifies that ``build_repo_map`` and ``build_repo_map_cached`` decode source
files as UTF-8 regardless of the platform default text encoding. On non-en-US
Windows ``Path.read_text()`` without ``encoding=`` decodes through cp1252 /
cp932 / cp949, which mangles non-ASCII identifiers/comments and caches the
garbled symbol index to ``repomap.json``. Both build functions must pass
``encoding="utf-8"``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from bog_agents.middleware.repo_map import build_repo_map, build_repo_map_cached

if TYPE_CHECKING:
    from pathlib import Path


# A Python source file whose docstring/comment contains characters that would
# decode differently (or raise) under cp1252 / cp932. The symbol names stay
# ASCII so extraction succeeds; the point is that reading must not corrupt or
# crash on the non-ASCII bytes.
_UTF8_SOURCE = (
    '"""Módulo de ejemplo — café, naïve, 日本語, 🚀."""\n\n\nclass Café:\n    pass\n\n\ndef greeting() -> str:\n    return "¡Hola, señor! — 日本語"\n'
)


def _write_utf8_source(root: Path) -> Path:
    src = root / "module.py"
    src.write_text(_UTF8_SOURCE, encoding="utf-8")
    return src


def test_build_repo_map_decodes_utf8(tmp_path: Path) -> None:
    """``build_repo_map`` reads non-ASCII source as UTF-8 without corruption."""
    _write_utf8_source(tmp_path)

    result = build_repo_map(tmp_path)

    # Symbol extraction succeeded and the non-ASCII class name round-trips.
    assert "Café" in result
    assert "greeting" in result


def test_build_repo_map_cached_decodes_utf8(tmp_path: Path) -> None:
    """``build_repo_map_cached`` reads non-ASCII source as UTF-8 without corruption."""
    _write_utf8_source(tmp_path)

    result = build_repo_map_cached(tmp_path)

    assert "Café" in result
    assert "greeting" in result


def test_build_repo_map_handles_invalid_utf8_bytes(tmp_path: Path) -> None:
    """A file with bytes that are not valid UTF-8 is replaced, not fatal.

    ``errors="replace"`` must keep the scan resilient: a single undecodable
    file should not abort the whole repo map build.
    """
    good = tmp_path / "good.py"
    good.write_text("class Widget:\n    pass\n", encoding="utf-8")

    # Lone 0x80 byte: invalid as standalone UTF-8.
    bad = tmp_path / "bad.py"
    bad.write_bytes(b"class Broken:\n    x = '\x80'\n")

    result = build_repo_map(tmp_path)

    # The valid file is still indexed even though a sibling has bad bytes.
    assert "Widget" in result
