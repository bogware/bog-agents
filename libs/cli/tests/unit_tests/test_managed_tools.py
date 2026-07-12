"""Unit tests for the checksum-verified managed ripgrep installer.

These tests never touch the network: `urllib.request.urlopen` and the
connectivity probe are mocked. They assert the security-critical invariants:
a checksum mismatch never installs, an unsupported platform never installs,
and the install gate (`auto_install` / offline / connectivity) short-circuits
before any download.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any, Self
from unittest.mock import MagicMock, patch

import pytest

from bog_agents_cli import managed_tools
from bog_agents_cli.managed_tools import (
    RIPGREP_ASSETS,
    RIPGREP_VERSION,
    ChecksumMismatchError,
    _verify_sha256,
    describe_ripgrep,
    ensure_ripgrep,
    is_offline,
    managed_install_allowed,
    managed_rg_path,
    prepend_managed_bin_to_path,
)
from bog_agents_cli.model_config import tools_auto_install


@pytest.fixture
def bin_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the managed bin dir at an isolated temp directory."""
    managed_bin = tmp_path / "bin"
    monkeypatch.setattr(managed_tools, "BIN_DIR", managed_bin)
    # Ensure the offline flag never leaks in from the host environment.
    monkeypatch.delenv(managed_tools.OFFLINE_ENV, raising=False)
    return managed_bin


class _FakeResponse:
    """Minimal urlopen response context manager yielding fixed bytes."""

    def __init__(self, payload: bytes, *, status: int = 200) -> None:
        self._payload = payload
        self.status = status
        self._read = False

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def read(self, _size: int = -1) -> bytes:
        if self._read:
            return b""
        self._read = True
        return self._payload


def _fake_urlopen(payload: bytes) -> Callable[..., _FakeResponse]:
    """Return a urlopen replacement that always yields `payload`."""

    def _open(_url: str, *_args: Any, **_kwargs: Any) -> _FakeResponse:
        return _FakeResponse(payload)

    return _open


class TestPrependManagedBinToPath:
    """Tests for prepend_managed_bin_to_path()."""

    def test_noop_when_dir_absent(
        self, bin_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No PATH change when the managed rg binary does not exist."""
        monkeypatch.setenv("PATH", "/usr/bin")
        prepend_managed_bin_to_path()
        assert os.environ["PATH"] == "/usr/bin"

    def test_prepends_when_binary_present(
        self, bin_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Managed bin dir is prepended once the rg binary exists."""
        bin_dir.mkdir(parents=True)
        managed_rg_path().write_text("stub", encoding="utf-8")
        monkeypatch.setenv("PATH", "/usr/bin")

        prepend_managed_bin_to_path()

        parts = os.environ["PATH"].split(os.pathsep)
        assert parts[0] == str(bin_dir)
        assert "/usr/bin" in parts

    def test_idempotent(self, bin_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Repeated calls do not duplicate the entry or reorder it."""
        bin_dir.mkdir(parents=True)
        managed_rg_path().write_text("stub", encoding="utf-8")
        monkeypatch.setenv("PATH", "/usr/bin")

        prepend_managed_bin_to_path()
        first = os.environ["PATH"]
        prepend_managed_bin_to_path()
        second = os.environ["PATH"]

        assert first == second
        assert os.environ["PATH"].split(os.pathsep).count(str(bin_dir)) == 1


class TestIsOffline:
    """Tests for the BOG_AGENTS_OFFLINE flag parsing."""

    def test_unset_is_online(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Unset env var means not offline."""
        monkeypatch.delenv(managed_tools.OFFLINE_ENV, raising=False)
        assert is_offline() is False

    @pytest.mark.parametrize("value", ["1", "true", "YES", "on"])
    def test_truthy_values_are_offline(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        """Recognized truthy tokens enable offline mode."""
        monkeypatch.setenv(managed_tools.OFFLINE_ENV, value)
        assert is_offline() is True

    @pytest.mark.parametrize("value", ["0", "false", "no", "", "maybe"])
    def test_falsy_values_are_online(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        """Falsy / unrecognized tokens keep online mode."""
        monkeypatch.setenv(managed_tools.OFFLINE_ENV, value)
        assert is_offline() is False


class TestManagedInstallAllowed:
    """Tests for the composite install gate."""

    def test_all_open(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Gate opens when auto_install on, online, and connectivity OK."""
        monkeypatch.delenv(managed_tools.OFFLINE_ENV, raising=False)
        monkeypatch.setattr(
            "bog_agents_cli.model_config.tools_auto_install", lambda *_a, **_k: True
        )
        monkeypatch.setattr(managed_tools, "_has_connectivity", lambda: True)
        assert managed_install_allowed() is True

    def test_auto_install_false_short_circuits(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Disabled auto_install closes the gate before any probe."""
        monkeypatch.setattr(
            "bog_agents_cli.model_config.tools_auto_install", lambda *_a, **_k: False
        )
        probe = MagicMock()
        monkeypatch.setattr(managed_tools, "_has_connectivity", probe)
        assert managed_install_allowed() is False
        probe.assert_not_called()

    def test_offline_short_circuits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Offline flag closes the gate before the connectivity probe."""
        monkeypatch.setattr(
            "bog_agents_cli.model_config.tools_auto_install", lambda *_a, **_k: True
        )
        monkeypatch.setenv(managed_tools.OFFLINE_ENV, "1")
        probe = MagicMock()
        monkeypatch.setattr(managed_tools, "_has_connectivity", probe)
        assert managed_install_allowed() is False
        probe.assert_not_called()

    def test_no_connectivity_short_circuits(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failed connectivity probe closes the gate."""
        monkeypatch.delenv(managed_tools.OFFLINE_ENV, raising=False)
        monkeypatch.setattr(
            "bog_agents_cli.model_config.tools_auto_install", lambda *_a, **_k: True
        )
        monkeypatch.setattr(managed_tools, "_has_connectivity", lambda: False)
        assert managed_install_allowed() is False


class TestVerifySha256:
    """Tests for the SHA-256 verifier."""

    def test_match_passes(self, tmp_path: Path) -> None:
        """A matching digest does not raise."""
        import hashlib

        payload = b"hello ripgrep"
        f = tmp_path / "archive"
        f.write_bytes(payload)
        _verify_sha256(f, hashlib.sha256(payload).hexdigest())

    def test_mismatch_raises(self, tmp_path: Path) -> None:
        """A mismatched digest raises ChecksumMismatchError."""
        f = tmp_path / "archive"
        f.write_bytes(b"tampered bytes")
        with pytest.raises(ChecksumMismatchError):
            _verify_sha256(f, "0" * 64)


class TestEnsureRipgrepGate:
    """Tests for ensure_ripgrep() gate short-circuits (no network)."""

    @pytest.fixture(autouse=True)
    def _force_install_path(
        self, bin_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Make ensure_ripgrep reach the gate: no managed and no system rg."""
        monkeypatch.setattr("shutil.which", lambda _name: None)

    def test_auto_install_false_returns_none_without_download(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Disabled auto_install returns None and never downloads."""
        monkeypatch.setattr(
            "bog_agents_cli.model_config.tools_auto_install", lambda *_a, **_k: False
        )
        urlopen = MagicMock()
        monkeypatch.setattr("urllib.request.urlopen", urlopen)
        assert ensure_ripgrep() is None
        urlopen.assert_not_called()

    def test_offline_returns_none_without_download(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Offline flag returns None and never downloads."""
        monkeypatch.setattr(
            "bog_agents_cli.model_config.tools_auto_install", lambda *_a, **_k: True
        )
        monkeypatch.setenv(managed_tools.OFFLINE_ENV, "1")
        urlopen = MagicMock()
        monkeypatch.setattr("urllib.request.urlopen", urlopen)
        assert ensure_ripgrep() is None
        urlopen.assert_not_called()

    def test_no_connectivity_returns_none_without_download(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failed connectivity probe returns None and never downloads."""
        monkeypatch.setattr(
            "bog_agents_cli.model_config.tools_auto_install", lambda *_a, **_k: True
        )
        monkeypatch.setattr(managed_tools, "_has_connectivity", lambda: False)
        urlopen = MagicMock()
        monkeypatch.setattr("urllib.request.urlopen", urlopen)
        assert ensure_ripgrep() is None
        urlopen.assert_not_called()

    def test_unsupported_arch_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unsupported architecture returns None without downloading."""
        monkeypatch.setattr(
            "bog_agents_cli.model_config.tools_auto_install", lambda *_a, **_k: True
        )
        monkeypatch.setattr(managed_tools, "_has_connectivity", lambda: True)
        monkeypatch.setattr(managed_tools, "_normalized_arch", lambda: None)
        urlopen = MagicMock()
        monkeypatch.setattr("urllib.request.urlopen", urlopen)
        assert ensure_ripgrep() is None
        urlopen.assert_not_called()

    def test_missing_platform_asset_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A platform/arch with no pinned asset returns None."""
        monkeypatch.setattr(
            "bog_agents_cli.model_config.tools_auto_install", lambda *_a, **_k: True
        )
        monkeypatch.setattr(managed_tools, "_has_connectivity", lambda: True)
        monkeypatch.setattr(managed_tools, "_normalized_arch", lambda: "x86_64")
        monkeypatch.setattr(managed_tools, "RIPGREP_ASSETS", {})
        urlopen = MagicMock()
        monkeypatch.setattr("urllib.request.urlopen", urlopen)
        assert ensure_ripgrep() is None
        urlopen.assert_not_called()


class TestEnsureRipgrepChecksum:
    """Tests that a checksum mismatch never installs."""

    def test_checksum_mismatch_does_not_install(
        self, bin_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A tampered download fails verification and installs nothing."""
        monkeypatch.setattr("shutil.which", lambda _name: None)
        monkeypatch.setattr(
            "bog_agents_cli.model_config.tools_auto_install", lambda *_a, **_k: True
        )
        monkeypatch.setattr(managed_tools, "_has_connectivity", lambda: True)
        monkeypatch.setattr(managed_tools, "_normalized_arch", lambda: "x86_64")
        # A single supported entry whose sha will never match the payload.
        monkeypatch.setattr(
            managed_tools,
            "RIPGREP_ASSETS",
            {(managed_tools.sys.platform, "x86_64"): ("rg-asset.tar.gz", "0" * 64)},
        )
        # Serve bytes that cannot match the pinned checksum.
        monkeypatch.setattr(
            "urllib.request.urlopen",
            _fake_urlopen(b"this is not a real ripgrep archive"),
        )

        result = ensure_ripgrep()

        assert result is None
        # Nothing was installed at the managed path.
        assert not managed_rg_path().exists()


class TestExistingBinaryShortCircuits:
    """Tests that ensure_ripgrep returns early when rg is already available."""

    def test_returns_managed_when_present(
        self, bin_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An existing managed binary is returned without any download."""
        bin_dir.mkdir(parents=True)
        managed_rg_path().write_text("stub", encoding="utf-8")
        urlopen = MagicMock()
        monkeypatch.setattr("urllib.request.urlopen", urlopen)

        assert ensure_ripgrep() == managed_rg_path()
        urlopen.assert_not_called()

    def test_returns_system_when_present(
        self, bin_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A system rg on PATH is returned without any download."""
        monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/rg")
        urlopen = MagicMock()
        monkeypatch.setattr("urllib.request.urlopen", urlopen)

        assert ensure_ripgrep() == Path("/usr/bin/rg")
        urlopen.assert_not_called()


class TestDescribeRipgrep:
    """Tests for the doctor-facing describe_ripgrep()."""

    def test_managed(self, bin_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Reports 'managed' when the managed binary exists."""
        bin_dir.mkdir(parents=True)
        managed_rg_path().write_text("stub", encoding="utf-8")
        status, detail = describe_ripgrep()
        assert status == "managed"
        assert RIPGREP_VERSION in detail

    def test_system(self, bin_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Reports 'system' when only a PATH rg exists."""
        monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/rg")
        status, detail = describe_ripgrep()
        assert status == "system"
        assert "/usr/bin/rg" in detail

    def test_absent(self, bin_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Reports 'absent' when no rg is available anywhere."""
        monkeypatch.setattr("shutil.which", lambda _name: None)
        status, _detail = describe_ripgrep()
        assert status == "absent"


class TestToolsAutoInstallConfig:
    """Tests for the [tools].auto_install config reader."""

    def test_default_true_when_missing(self, tmp_path: Path) -> None:
        """Defaults to True when the config file does not exist."""
        assert tools_auto_install(tmp_path / "nope.toml") is True

    def test_reads_false(self, tmp_path: Path) -> None:
        """Reads an explicit auto_install = false."""
        cfg = tmp_path / "config.toml"
        cfg.write_text("[tools]\nauto_install = false\n", encoding="utf-8")
        assert tools_auto_install(cfg) is False

    def test_reads_true(self, tmp_path: Path) -> None:
        """Reads an explicit auto_install = true."""
        cfg = tmp_path / "config.toml"
        cfg.write_text("[tools]\nauto_install = true\n", encoding="utf-8")
        assert tools_auto_install(cfg) is True

    def test_non_bool_defaults_true(self, tmp_path: Path) -> None:
        """A non-boolean value degrades to the default (True)."""
        cfg = tmp_path / "config.toml"
        cfg.write_text('[tools]\nauto_install = "yes"\n', encoding="utf-8")
        assert tools_auto_install(cfg) is True

    def test_malformed_toml_defaults_true(self, tmp_path: Path) -> None:
        """Malformed TOML degrades to the default (True) instead of crashing."""
        cfg = tmp_path / "config.toml"
        cfg.write_text("this is not valid toml [[[", encoding="utf-8")
        assert tools_auto_install(cfg) is True


def test_assets_table_covers_expected_platforms() -> None:
    """The pinned asset table covers the three supported OSes on both arches."""
    expected = {
        ("darwin", "arm64"),
        ("darwin", "x86_64"),
        ("linux", "arm64"),
        ("linux", "x86_64"),
        ("win32", "arm64"),
        ("win32", "x86_64"),
    }
    assert expected <= set(RIPGREP_ASSETS)
    # Every entry is a (filename, 64-char hex sha256) pair.
    for asset, sha in RIPGREP_ASSETS.values():
        assert asset.startswith("ripgrep-")
        assert len(sha) == 64
        int(sha, 16)  # hex-decodable


def test_patch_target_exists() -> None:
    """Guard: the shutil.which patch target used by other tests is importable."""
    with patch("shutil.which", return_value=None):
        assert managed_tools is not None
