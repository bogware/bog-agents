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


# ---------------------------------------------------------------------------
# P31: dotted-dir exclusion was too broad — legitimate source under non-noise
# dotted dirs (.github scripts, etc.) was silently dropped. The exclusion is
# now narrowed to a curated noise allowlist; .git is still skipped.
# ---------------------------------------------------------------------------


def _write_source(path: Path, class_name: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"class {class_name}:\n    pass\n", encoding="utf-8")


def test_source_under_non_noise_dotted_dir_is_indexed(tmp_path: Path) -> None:
    """A source file under ``.github`` (a non-noise dotted dir) is now indexed (P31)."""
    _write_source(tmp_path / ".github" / "scripts" / "release.py", "ReleaseTool")

    result = build_repo_map(tmp_path)

    assert "ReleaseTool" in result


def test_source_under_non_noise_dotted_dir_is_indexed_cached(tmp_path: Path) -> None:
    """Cached build also indexes source under a non-noise dotted dir (P31)."""
    _write_source(tmp_path / ".github" / "scripts" / "release.py", "ReleaseTool")

    result = build_repo_map_cached(tmp_path)

    assert "ReleaseTool" in result


def test_git_dir_is_still_skipped(tmp_path: Path) -> None:
    """``.git`` contents must never be indexed even with the narrowed filter."""
    # A file that *looks* like indexable Python living inside the object store.
    _write_source(tmp_path / ".git" / "hooks" / "evil.py", "GitInternal")
    # A real source file so the map is non-empty.
    _write_source(tmp_path / "app.py", "RealCode")

    result = build_repo_map(tmp_path)

    assert "RealCode" in result
    assert "GitInternal" not in result


def test_git_dir_is_still_skipped_cached(tmp_path: Path) -> None:
    """Cached build also excludes ``.git`` contents (P31)."""
    _write_source(tmp_path / ".git" / "hooks" / "evil.py", "GitInternal")
    _write_source(tmp_path / "app.py", "RealCode")

    result = build_repo_map_cached(tmp_path)

    assert "RealCode" in result
    assert "GitInternal" not in result


def test_known_noise_dotted_dir_still_skipped(tmp_path: Path) -> None:
    """A curated-noise dotted dir (e.g. ``.venv``) remains excluded."""
    _write_source(tmp_path / ".venv" / "lib" / "pkg.py", "VendoredPkg")
    _write_source(tmp_path / "main.py", "AppMain")

    result = build_repo_map(tmp_path)

    assert "AppMain" in result
    assert "VendoredPkg" not in result
