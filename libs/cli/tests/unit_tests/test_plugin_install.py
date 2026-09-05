"""ROADMAP #62: pinned plugin installs from dir / zip / URL / git / marketplace."""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest

from bog_agents_cli.plugin_install import (
    LOCK_FILE,
    PluginInstallError,
    directory_digest,
    install_plugin,
    resolve_marketplace_entry,
    sha256_bytes,
)
from tests.unit_tests.test_plugin_spec import make_plugin


def _zip_bytes(root: Path, *, prefix: str = "") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for path in sorted(root.rglob("*")):
            if path.is_file():
                zf.write(path, prefix + str(path.relative_to(root)).replace("\\", "/"))
    return buf.getvalue()


def test_install_from_dir_writes_lock(tmp_path: Path) -> None:
    src = make_plugin(tmp_path / "src" / "demo")
    result = install_plugin(str(src), dest_root=tmp_path / "plugins")
    assert result.path == tmp_path / "plugins" / "demo"
    assert (result.path / "plugin.json").is_file()
    lock = json.loads((result.path / LOCK_FILE).read_text(encoding="utf-8"))
    assert lock["sha256"] == result.sha256 == directory_digest(src)
    # Pin mismatch installs nothing.
    with pytest.raises(PluginInstallError, match="SHA-256 mismatch"):
        install_plugin(str(src), dest_root=tmp_path / "other", sha256="0" * 64)
    assert not (tmp_path / "other").exists()


def test_install_from_zip_and_url_with_pin(tmp_path: Path) -> None:
    src = make_plugin(tmp_path / "src" / "demo")
    data = _zip_bytes(src, prefix="demo-1.2.3/")
    archive = tmp_path / "demo.zip"
    archive.write_bytes(data)
    pin = sha256_bytes(data)
    result = install_plugin(str(archive), dest_root=tmp_path / "plugins", sha256=pin)
    assert result.spec.name == "demo"
    assert (result.path / "skills" / "greet" / "SKILL.md").is_file()

    fetched: list[str] = []

    def fetch(url: str) -> bytes:
        fetched.append(url)
        return data

    result = install_plugin(
        "https://example.com/demo.zip",
        dest_root=tmp_path / "plugins2",
        sha256=pin,
        fetch=fetch,
    )
    assert fetched == ["https://example.com/demo.zip"]
    assert result.source == "https://example.com/demo.zip"
    with pytest.raises(PluginInstallError, match="mismatch"):
        install_plugin(
            "https://example.com/demo.zip",
            dest_root=tmp_path / "plugins3",
            sha256="f" * 64,
            fetch=fetch,
        )


def test_zip_path_traversal_is_refused(tmp_path: Path) -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("plugin.json", json.dumps({"name": "evil"}))
        zf.writestr("../escape.txt", "x")
    archive = tmp_path / "evil.zip"
    archive.write_bytes(buf.getvalue())
    with pytest.raises(PluginInstallError, match="unsafe path"):
        install_plugin(str(archive), dest_root=tmp_path / "plugins")


def test_git_and_marketplace(tmp_path: Path) -> None:
    src = make_plugin(tmp_path / "src" / "demo")

    def clone(url: str, dest: Path) -> None:
        import shutil

        shutil.copytree(src, dest)

    result = install_plugin(
        "https://github.com/acme/demo.git",
        dest_root=tmp_path / "plugins",
        git_clone=clone,
    )
    assert result.spec.name == "demo"
    market = tmp_path / "marketplace.json"
    market.write_text(
        json.dumps(
            {
                "plugins": [
                    {
                        "name": "demo",
                        "source": str(src),
                        "sha256": directory_digest(src),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    assert resolve_marketplace_entry(market, "DEMO")["source"] == str(src)
    result = install_plugin("demo", dest_root=tmp_path / "plugins2", marketplace=market)
    assert result.source.endswith("#demo")
    with pytest.raises(PluginInstallError, match="not in the marketplace"):
        install_plugin("nope", dest_root=tmp_path / "plugins3", marketplace=market)
    with pytest.raises(PluginInstallError, match="unsupported"):
        install_plugin("not-a-thing", dest_root=tmp_path / "plugins4")
