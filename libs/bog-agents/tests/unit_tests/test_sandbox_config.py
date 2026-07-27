"""Tests for the SDK-level sandbox spec loader (relocated in #27)."""

from __future__ import annotations

from pathlib import Path

from bog_agents.sandbox_config import (
    load_sandbox_config,
    resolve_sandbox_setup,
)

_SAMPLE = """
[sandbox]
base_image = "python:3.11-slim"
preinstall = ["uv sync", "apt-get install -y ripgrep"]
network_allowlist = ["pypi.org", "github.com"]
"""


def _write(root: Path, content: str = _SAMPLE) -> None:
    d = root / ".bog-agents"
    d.mkdir(parents=True, exist_ok=True)
    (d / "sandbox.toml").write_text(content, encoding="utf-8")


def test_load_and_summary(tmp_path: Path) -> None:
    _write(tmp_path)
    cfg = load_sandbox_config(tmp_path)
    assert cfg is not None
    assert cfg.base_image == "python:3.11-slim"
    assert cfg.network_allowlist == ["pypi.org", "github.com"]
    assert "2 allowed host(s)" in cfg.summary()


def test_missing_returns_none(tmp_path: Path) -> None:
    assert load_sandbox_config(tmp_path) is None


def test_resolve_materializes_preinstall(tmp_path: Path) -> None:
    _write(tmp_path)
    setup = resolve_sandbox_setup(tmp_path, tmp_dir=tmp_path / "gen")
    assert setup.setup_script_path is not None
    assert "uv sync" in Path(setup.setup_script_path).read_text(encoding="utf-8")
    assert setup.network_allowlist == ["pypi.org", "github.com"]


def test_resolve_no_spec_is_noop(tmp_path: Path) -> None:
    setup = resolve_sandbox_setup(tmp_path)
    assert setup.setup_script_path is None
    assert setup.config is None
