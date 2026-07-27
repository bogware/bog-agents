"""Tests for the declarative sandbox config loader (ROADMAP #16)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from bog_agents_cli.sandbox_config import (
    SandboxConfig,
    load_sandbox_config,
    resolve_sandbox_setup,
)

_SAMPLE = """
[sandbox]
base_image = "python:3.11-slim"
runner_size = "large"
snapshot = "ci-base"
preinstall = ["uv sync --all-groups", "apt-get install -y ripgrep"]
network_allowlist = ["pypi.org", "github.com"]
"""


def _write(root: Path, content: str) -> None:
    d = root / ".bog-agents"
    d.mkdir(parents=True, exist_ok=True)
    (d / "sandbox.toml").write_text(content, encoding="utf-8")


class TestLoad:
    def test_full_config(self, tmp_path: Path) -> None:
        _write(tmp_path, _SAMPLE)
        cfg = load_sandbox_config(tmp_path)
        assert cfg is not None
        assert cfg.base_image == "python:3.11-slim"
        assert cfg.runner_size == "large"
        assert cfg.snapshot == "ci-base"
        assert cfg.preinstall == ["uv sync --all-groups", "apt-get install -y ripgrep"]
        assert cfg.network_allowlist == ["pypi.org", "github.com"]

    def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        assert load_sandbox_config(tmp_path) is None

    def test_malformed_returns_none(self, tmp_path: Path) -> None:
        _write(tmp_path, "this = = not valid toml")
        assert load_sandbox_config(tmp_path) is None

    def test_defaults_and_bad_size(self, tmp_path: Path) -> None:
        _write(tmp_path, '[sandbox]\nrunner_size = "huge"\n')
        cfg = load_sandbox_config(tmp_path)
        assert cfg is not None
        assert cfg.runner_size == "medium"  # invalid size -> default
        assert cfg.preinstall == []
        assert cfg.network_allowlist == []
        assert cfg.base_image is None

    def test_string_preinstall_coerced_to_list(self, tmp_path: Path) -> None:
        _write(tmp_path, '[sandbox]\npreinstall = "make setup"\n')
        cfg = load_sandbox_config(tmp_path)
        assert cfg is not None
        assert cfg.preinstall == ["make setup"]


class TestMaterialize:
    def test_setup_script_contents(self, tmp_path: Path) -> None:
        cfg = SandboxConfig(preinstall=["echo a", "echo b"])
        dest = tmp_path / "setup.sh"
        out = cfg.materialize_setup_script(dest)
        assert out == dest
        text = dest.read_text(encoding="utf-8")
        assert text.startswith("#!/usr/bin/env bash")
        assert "set -euo pipefail" in text
        assert "echo a" in text
        assert "echo b" in text

    def test_summary(self) -> None:
        cfg = SandboxConfig(
            base_image="img", preinstall=["x"], network_allowlist=["a", "b"]
        )
        s = cfg.summary()
        assert "img" in s
        assert "2 allowed host(s)" in s
        assert "1 preinstall step(s)" in s


class TestResolveSetup:
    """#27: the spec resolves into a runnable setup + surfaced allowlist."""

    def test_no_spec_is_noop(self, tmp_path: Path) -> None:
        setup = resolve_sandbox_setup(tmp_path)
        assert setup.setup_script_path is None
        assert setup.config is None
        assert setup.network_allowlist == []

    def test_explicit_script_wins_but_spec_still_loaded(self, tmp_path: Path) -> None:
        _write(tmp_path, _SAMPLE)
        setup = resolve_sandbox_setup(tmp_path, explicit_setup_script="/my/setup.sh")
        assert setup.setup_script_path == "/my/setup.sh"
        # The spec is still loaded so its allowlist is surfaced.
        assert setup.network_allowlist == ["pypi.org", "github.com"]

    def test_preinstall_materialized_to_script(self, tmp_path: Path) -> None:
        _write(tmp_path, _SAMPLE)
        setup = resolve_sandbox_setup(tmp_path, tmp_dir=tmp_path / "gen")
        assert setup.setup_script_path is not None
        script = Path(setup.setup_script_path).read_text(encoding="utf-8")
        assert "uv sync --all-groups" in script
        assert "apt-get install -y ripgrep" in script

    def test_spec_without_preinstall_has_no_script(self, tmp_path: Path) -> None:
        _write(tmp_path, '[sandbox]\nnetwork_allowlist = ["pypi.org"]\n')
        setup = resolve_sandbox_setup(tmp_path)
        assert setup.setup_script_path is None
        assert setup.network_allowlist == ["pypi.org"]


class TestFactoryAppliesSpec:
    """#27: create_sandbox consumes the spec (runs preinstall, surfaces allowlist)."""

    def test_runs_preinstall_and_surfaces_allowlist(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        import bog_agents_cli.integrations.sandbox_factory as sf

        _write(tmp_path, _SAMPLE)
        executed: list[str] = []

        class _FakeBackend:
            id = "sbx-1"

            def execute(self, cmd: str):
                executed.append(cmd)
                return SimpleNamespace(exit_code=0, output="")

        class _FakeProvider:
            def get_or_create(self, *, sandbox_id=None):
                return _FakeBackend()

            def delete(self, *, sandbox_id=None) -> None:
                pass

        monkeypatch.setattr(sf, "_get_provider", lambda _name: _FakeProvider())

        with sf.create_sandbox("docker", cwd=tmp_path) as backend:
            assert backend.network_allowlist == ["pypi.org", "github.com"]

        assert any("uv sync --all-groups" in c for c in executed)
