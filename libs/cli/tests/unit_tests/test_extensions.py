"""Baseline tests for bog_agents_cli.extensions.

We test the safe, non-network parts of the extensions surface — manifest
parsing, local-path installation, listing, enable/disable, uninstall.
The git-clone-then-uv-install path is NOT exercised here (it shells out
to ``git`` and ``uv`` and would be a real integration test).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bog_agents_cli.extensions import (
    MANIFEST_FILENAME,
    InstalledExtension,
    disable_extension,
    enable_extension,
    format_extensions_list,
    get_extensions_dir,
    install_extension,
    list_extensions,
    parse_manifest,
    uninstall_extension,
)


def _write_extension_source(tmp_path: Path, name: str, version: str = "0.1.0", **extra: object) -> Path:
    """Create a minimal extension source directory and return its path."""
    src = tmp_path / f"src-{name}"
    src.mkdir(parents=True, exist_ok=True)
    manifest = {"name": name, "version": version, **extra}
    (src / MANIFEST_FILENAME).write_text(json.dumps(manifest))
    return src


class TestParseManifest:
    def test_minimal_valid(self, tmp_path: Path):
        path = tmp_path / MANIFEST_FILENAME
        path.write_text(json.dumps({"name": "foo", "version": "1.0.0"}))
        m = parse_manifest(path)
        assert m.name == "foo"
        assert m.version == "1.0.0"
        assert m.skills == []
        assert m.dependencies == []

    def test_full_fields(self, tmp_path: Path):
        path = tmp_path / MANIFEST_FILENAME
        path.write_text(
            json.dumps(
                {
                    "name": "full",
                    "version": "2.3.4",
                    "description": "test ext",
                    "author": "alice",
                    "license": "MIT",
                    "homepage": "https://example.com",
                    "skills": ["a.md", "b.md"],
                    "hooks": [{"event": "on_start"}],
                    "mcp_servers": [{"name": "s"}],
                    "commands": [{"name": "/x"}],
                    "agents": [{"name": "a"}],
                    "settings": {"k": "v"},
                    "dependencies": ["pkg1>=1"],
                    "compatibility": "0.8.0",
                }
            )
        )
        m = parse_manifest(path)
        assert m.skills == ["a.md", "b.md"]
        assert m.dependencies == ["pkg1>=1"]
        assert m.settings == {"k": "v"}

    def test_missing_required_fields(self, tmp_path: Path):
        path = tmp_path / MANIFEST_FILENAME
        path.write_text(json.dumps({"version": "1.0.0"}))  # no 'name'
        with pytest.raises(ValueError, match="name"):
            parse_manifest(path)

    def test_invalid_json(self, tmp_path: Path):
        path = tmp_path / MANIFEST_FILENAME
        path.write_text("not json {{")
        with pytest.raises(ValueError, match="Failed to read manifest"):
            parse_manifest(path)


class TestGetExtensionsDir:
    def test_creates_dir_by_default(self, tmp_path: Path):
        d = get_extensions_dir(tmp_path)
        assert d.is_dir()
        assert d == tmp_path / "extensions"

    def test_no_create(self, tmp_path: Path):
        d = get_extensions_dir(tmp_path, create=False)
        assert not d.exists()


class TestListExtensions:
    def test_returns_empty_when_config_dir_none(self):
        assert list_extensions(None) == []

    def test_returns_empty_when_no_extensions(self, tmp_path: Path):
        assert list_extensions(tmp_path) == []

    def test_lists_installed(self, tmp_path: Path):
        src = _write_extension_source(tmp_path, "alpha")
        install_extension(tmp_path, str(src))
        listed = list_extensions(tmp_path)
        assert len(listed) == 1
        assert listed[0].manifest.name == "alpha"
        assert listed[0].enabled is True

    def test_disabled_marker_reflected(self, tmp_path: Path):
        src = _write_extension_source(tmp_path, "alpha")
        install_extension(tmp_path, str(src))
        disable_extension(tmp_path, "alpha")
        listed = list_extensions(tmp_path)
        assert listed[0].enabled is False

    def test_skips_invalid_manifest(self, tmp_path: Path, caplog):
        ext_root = get_extensions_dir(tmp_path)
        broken = ext_root / "broken"
        broken.mkdir()
        (broken / MANIFEST_FILENAME).write_text("not json")
        # Should not raise; should log a warning.
        listed = list_extensions(tmp_path)
        assert listed == []


class TestInstallExtension:
    def test_local_path_install(self, tmp_path: Path):
        src = _write_extension_source(tmp_path, "alpha", description="local install")
        installed = install_extension(tmp_path, str(src))
        assert installed.manifest.name == "alpha"
        assert installed.install_path.is_dir()
        # Manifest should be copied to the install path.
        copied = installed.install_path / MANIFEST_FILENAME
        assert copied.exists()

    def test_local_path_with_no_manifest_rejected(self, tmp_path: Path):
        empty = tmp_path / "empty"
        empty.mkdir()
        with pytest.raises(ValueError, match="No bog-agents-extension"):
            install_extension(tmp_path, str(empty))

    def test_install_overwrites_existing(self, tmp_path: Path):
        src1 = _write_extension_source(tmp_path, "alpha", "1.0.0")
        install_extension(tmp_path, str(src1))
        # Re-install with a different version.
        src2 = _write_extension_source(tmp_path, "alpha", "2.0.0")
        # Different src dir, same extension name; should replace.
        new_src = tmp_path / "src2"
        new_src.mkdir()
        (new_src / MANIFEST_FILENAME).write_text(
            json.dumps({"name": "alpha", "version": "2.0.0"})
        )
        installed = install_extension(tmp_path, str(new_src))
        assert installed.manifest.version == "2.0.0"

    def test_unknown_source_rejected(self, tmp_path: Path):
        with pytest.raises(ValueError):
            install_extension(tmp_path, "/path/that/does/not/exist")


class TestEnableDisable:
    def test_enable_already_enabled_returns_false(self, tmp_path: Path):
        # Documented behavior: enable returns True only when there was a
        # .disabled marker to remove. Enabling an already-enabled
        # extension is a no-op that returns False.
        src = _write_extension_source(tmp_path, "alpha")
        install_extension(tmp_path, str(src))
        assert enable_extension(tmp_path, "alpha") is False

    def test_disable_then_enable(self, tmp_path: Path):
        src = _write_extension_source(tmp_path, "alpha")
        install_extension(tmp_path, str(src))
        assert disable_extension(tmp_path, "alpha") is True
        listed = list_extensions(tmp_path)
        assert listed[0].enabled is False
        assert enable_extension(tmp_path, "alpha") is True
        listed = list_extensions(tmp_path)
        assert listed[0].enabled is True

    def test_enable_unknown_returns_false(self, tmp_path: Path):
        assert enable_extension(tmp_path, "nonexistent") is False

    def test_disable_unknown_returns_false(self, tmp_path: Path):
        assert disable_extension(tmp_path, "nonexistent") is False


class TestUninstall:
    def test_uninstall_existing(self, tmp_path: Path):
        src = _write_extension_source(tmp_path, "alpha")
        install_extension(tmp_path, str(src))
        assert uninstall_extension(tmp_path, "alpha") is True
        assert list_extensions(tmp_path) == []

    def test_uninstall_unknown(self, tmp_path: Path):
        assert uninstall_extension(tmp_path, "nonexistent") is False


class TestFormatExtensionsList:
    def test_empty_message(self):
        out = format_extensions_list([])
        assert out  # non-empty
        assert "no" in out.lower() or "none" in out.lower() or "empty" in out.lower()

    def test_lists_each_extension(self, tmp_path: Path):
        src = _write_extension_source(tmp_path, "alpha", description="hello there")
        install_extension(tmp_path, str(src))
        listed = list_extensions(tmp_path)
        out = format_extensions_list(listed)
        assert "alpha" in out
