"""Install Agent Plugins 1.0 packages (ROADMAP #62): local dir, zip, URL, git, marketplace.

Every path lands in `<config_dir>/plugins/<name>/` next to a
`.bog-plugin-lock.json` recording the source and the SHA-256 that was
verified (or computed). A `--sha256` pin is compared against the archive
bytes for zips/URLs and against a directory digest for dirs/git checkouts;
a mismatch installs nothing. Network and git are injectable so the logic is
unit-testable offline.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import shutil
import subprocess  # noqa: S404 - git clone with a fixed argv
import tempfile
import time
import urllib.request
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bog_agents.git_env import hardened_git_env

from bog_agents_cli.plugin_spec import (
    PLUGIN_JSON,
    PluginSpec,
    load_plugin_spec,
    safe_plugin_name,
)

logger = logging.getLogger(__name__)

LOCK_FILE = ".bog-plugin-lock.json"
_MAX_ARCHIVE_BYTES = 64 * 1024 * 1024


class PluginInstallError(ValueError):
    """A plugin could not be installed (bad source, pin mismatch, unsafe archive)."""


@dataclass(frozen=True)
class InstallResult:
    """Where a plugin landed and what was verified."""

    spec: PluginSpec
    path: Path
    source: str
    sha256: str


def sha256_bytes(data: bytes) -> str:
    """Hex SHA-256 of `data`."""
    return hashlib.sha256(data).hexdigest()


def directory_digest(root: Path) -> str:
    """Deterministic SHA-256 over a directory's relative paths and contents."""
    digest = hashlib.sha256()
    for path in sorted(
        p for p in root.rglob("*") if p.is_file() and ".git" not in p.parts
    ):
        rel = str(path.relative_to(root)).replace("\\", "/")
        digest.update(rel.encode("utf-8") + b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _default_fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=60) as resp:
        return resp.read(_MAX_ARCHIVE_BYTES + 1)


def _default_git_clone(url: str, dest: Path) -> None:
    """Shallow-clone `url` into `dest`.

    Raises:
        PluginInstallError: When git is missing or the clone fails.
    """
    git = shutil.which("git")
    if git is None:
        msg = "git is required to install a plugin from a git URL"
        raise PluginInstallError(msg)
    result = subprocess.run(  # noqa: S603 - fixed argv, user-supplied URL
        [git, "clone", "--depth=1", url, str(dest)],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
        env=hardened_git_env(),
    )
    if result.returncode != 0:
        msg = f"git clone failed: {result.stderr.strip()[:300]}"
        raise PluginInstallError(msg)


def _safe_extract(archive: bytes, dest: Path) -> None:
    """Extract a zip into `dest`, refusing entries that escape it.

    Raises:
        PluginInstallError: When an entry would escape `dest`.
    """
    with zipfile.ZipFile(io.BytesIO(archive)) as zf:
        for info in zf.infolist():
            name = info.filename.replace("\\", "/")
            if (
                name.startswith("/")
                or ".." in Path(name).parts
                or ":" in name.split("/")[0]
            ):
                msg = f"unsafe path in archive: {info.filename}"
                raise PluginInstallError(msg)
        zf.extractall(dest)


def _find_plugin_root(extracted: Path) -> Path:
    """Locate the directory holding `plugin.json` (allow one wrapping folder).

    Raises:
        PluginInstallError: When no `plugin.json` is found.
    """
    if (extracted / PLUGIN_JSON).is_file():
        return extracted
    children = [p for p in extracted.iterdir() if p.is_dir()]
    for child in children:
        if (child / PLUGIN_JSON).is_file():
            return child
    msg = f"no {PLUGIN_JSON} found in the archive"
    raise PluginInstallError(msg)


def resolve_marketplace_entry(
    marketplace: str | Path, name: str, *, fetch: Callable[[str], bytes] | None = None
) -> dict[str, Any]:
    """Look `name` up in a `marketplace.json` (`{"plugins": [{"name", "source", "sha256"?}]}`).

    Raises:
        PluginInstallError: When the file is not JSON or `name` is absent.
    """
    text = (
        (fetch or _default_fetch)(str(marketplace)).decode("utf-8")
        if str(marketplace).startswith(("http://", "https://"))
        else Path(marketplace).read_text(encoding="utf-8")
    )
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        msg = f"marketplace.json is not valid JSON: {exc}"
        raise PluginInstallError(msg) from exc
    for entry in data.get("plugins", []) if isinstance(data, dict) else []:
        if (
            isinstance(entry, dict)
            and str(entry.get("name", "")).lower() == name.lower()
        ):
            if not entry.get("source"):
                msg = f"marketplace entry {name!r} has no source"
                raise PluginInstallError(msg)
            return entry
    msg = f"plugin {name!r} is not in the marketplace"
    raise PluginInstallError(msg)


def _is_url(source: str) -> bool:
    return source.startswith(("http://", "https://"))


def _is_git(source: str) -> bool:
    return (
        source.startswith("git@")
        or source.endswith(".git")
        or (_is_url(source) and not source.lower().endswith(".zip"))
    )


def install_plugin(
    source: str,
    *,
    dest_root: Path,
    sha256: str | None = None,
    marketplace: str | Path | None = None,
    fetch: Callable[[str], bytes] | None = None,
    git_clone: Callable[[str, Path], None] | None = None,
) -> InstallResult:
    """Install a plugin from a directory, zip, URL, git repo, or marketplace name.

    Args:
        source: A local directory or `.zip`, an `http(s)` zip URL, a git URL,
            or a plugin name when `marketplace` is given.
        dest_root: The plugins directory (usually `<config_dir>/plugins`).
        sha256: Optional pin; archives are checked byte-for-byte, directories
            and git checkouts through `directory_digest`.
        marketplace: A `marketplace.json` path or URL used to resolve `source`
            by name (its `sha256` becomes the pin when none was given).
        fetch: URL reader (tests); defaults to urllib.
        git_clone: `(url, dest)` cloner (tests); defaults to `git clone --depth=1`.

    Returns:
        The `InstallResult`.

    Raises:
        PluginInstallError: On a bad source, an unsafe archive, or a pin mismatch.
    """
    fetch = fetch or _default_fetch
    git_clone = git_clone or _default_git_clone
    origin = source
    if marketplace is not None and not (_is_url(source) or Path(source).exists()):
        entry = resolve_marketplace_entry(marketplace, source, fetch=fetch)
        source = str(entry["source"])
        sha256 = sha256 or (str(entry["sha256"]) if entry.get("sha256") else None)
        origin = f"{marketplace}#{entry.get('name', source)}"

    with tempfile.TemporaryDirectory(prefix="bog-plugin-") as tmp:
        staging = Path(tmp) / "stage"
        staging.mkdir()
        digest: str
        local = Path(source).expanduser()
        if local.is_dir():
            shutil.copytree(local, staging / "plugin")
            root = _find_plugin_root(staging / "plugin")
            digest = directory_digest(root)
        elif local.is_file() and local.suffix.lower() == ".zip":
            data = local.read_bytes()
            digest = sha256_bytes(data)
            _safe_extract(data, staging / "plugin")
            root = _find_plugin_root(staging / "plugin")
        elif _is_url(source) and source.lower().endswith(".zip"):
            data = fetch(source)
            if len(data) > _MAX_ARCHIVE_BYTES:
                msg = "plugin archive exceeds the 64 MB limit"
                raise PluginInstallError(msg)
            digest = sha256_bytes(data)
            _safe_extract(data, staging / "plugin")
            root = _find_plugin_root(staging / "plugin")
        elif _is_git(source):
            git_clone(source, staging / "plugin")
            root = _find_plugin_root(staging / "plugin")
            digest = directory_digest(root)
        else:
            msg = f"unsupported plugin source: {source}"
            raise PluginInstallError(msg)
        if sha256 and digest.lower() != sha256.lower():
            msg = f"SHA-256 mismatch for {source}: expected {sha256}, got {digest}"
            raise PluginInstallError(msg)
        spec = load_plugin_spec(root)
        if spec is None:
            msg = f"{PLUGIN_JSON} is missing or has no name"
            raise PluginInstallError(msg)
        dest = dest_root / safe_plugin_name(spec.name)
        if dest.exists():
            shutil.rmtree(dest)
        dest_root.mkdir(parents=True, exist_ok=True)
        shutil.copytree(root, dest)
    lock = {
        "name": spec.name,
        "version": spec.version,
        "source": origin,
        "sha256": digest,
        "installed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    (dest / LOCK_FILE).write_text(json.dumps(lock, indent=2), encoding="utf-8")
    installed = load_plugin_spec(dest) or spec
    return InstallResult(spec=installed, path=dest, source=origin, sha256=digest)
