"""Checksum-verified, auto-installed upstream binaries for optional CLI tools.

Today this only manages `ripgrep`. The SDK shells out to `rg` via `PATH`,
so installing into `~/.bog-agents/bin/` and prepending that directory to
`os.environ["PATH"]` is sufficient — no SDK change required.

The pinned `RIPGREP_VERSION` and `RIPGREP_ASSETS` table is the single
source of truth for what gets downloaded and verified. When bumping the
version, refresh both the version and the SHA-256 entries together.

This module is intentionally pure-logic (no Textual imports) so it can run
during the earliest startup phase and be unit-tested without spinning up
the TUI. Every download is gated behind `managed_install_allowed()`, which
honors the `[tools].auto_install` config key, the `BOG_AGENTS_OFFLINE`
environment flag, and a fast connectivity probe — so a sealed / air-gapped
box never hangs at startup and an unverified archive is never installed.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import tarfile
    import zipfile

logger = logging.getLogger(__name__)

RIPGREP_VERSION = "14.1.1"
"""Pinned upstream ripgrep release. Bump alongside `RIPGREP_ASSETS`."""

_RELEASE_URL_PREFIX = (
    "https://github.com/BurntSushi/ripgrep/releases/download/" + RIPGREP_VERSION
)

RIPGREP_ASSETS: dict[tuple[str, str], tuple[str, str]] = {
    ("darwin", "arm64"): (
        f"ripgrep-{RIPGREP_VERSION}-aarch64-apple-darwin.tar.gz",
        "24ad76777745fbff131c8fbc466742b011f925bfa4fffa2ded6def23b5b937be",
    ),
    ("darwin", "x86_64"): (
        f"ripgrep-{RIPGREP_VERSION}-x86_64-apple-darwin.tar.gz",
        "fc87e78f7cb3fea12d69072e7ef3b21509754717b746368fd40d88963630e2b3",
    ),
    ("linux", "arm64"): (
        f"ripgrep-{RIPGREP_VERSION}-aarch64-unknown-linux-gnu.tar.gz",
        "c827481c4ff4ea10c9dc7a4022c8de5db34a5737cb74484d62eb94a95841ab2f",
    ),
    ("linux", "x86_64"): (
        f"ripgrep-{RIPGREP_VERSION}-x86_64-unknown-linux-musl.tar.gz",
        "4cf9f2741e6c465ffdb7c26f38056a59e2a2544b51f7cc128ef28337eeae4d8e",
    ),
    # Windows on ARM runs x64 binaries via emulation; upstream does not
    # ship an arm64-windows build for ripgrep, so both Windows entries
    # point at the same x86_64 MSVC asset.
    ("win32", "arm64"): (
        f"ripgrep-{RIPGREP_VERSION}-x86_64-pc-windows-msvc.zip",
        "d0f534024c42afd6cb4d38907c25cd2b249b79bbe6cc1dbee8e3e37c2b6e25a1",
    ),
    ("win32", "x86_64"): (
        f"ripgrep-{RIPGREP_VERSION}-x86_64-pc-windows-msvc.zip",
        "d0f534024c42afd6cb4d38907c25cd2b249b79bbe6cc1dbee8e3e37c2b6e25a1",
    ),
}
"""`(sys.platform, normalized arch) -> (asset filename, sha256 hex)`."""

BIN_DIR: Path = Path.home() / ".bog-agents" / "bin"
"""Directory holding managed binaries. Prepended to `PATH` on startup."""

OFFLINE_ENV = "BOG_AGENTS_OFFLINE"
"""Environment flag that disables all managed-tool network downloads.

Parsed by `is_offline`: `1`/`true`/`yes`/`on` (case-insensitive) count as
enabled. Set this on air-gapped / sealed machines so startup never touches
the network.
"""

_DOWNLOAD_TIMEOUT_SECONDS = 120
_DOWNLOAD_CHUNK_BYTES = 1 << 16
_CONNECTIVITY_HOST = "github.com"
_CONNECTIVITY_PORT = 443
_CONNECTIVITY_TIMEOUT_SECONDS = 1.5
_HTTP_OK = 200

_ARCH_ALIASES = {
    "aarch64": "arm64",
    "arm64": "arm64",
    "amd64": "x86_64",
    "x86_64": "x86_64",
    "x64": "x86_64",
}

_TRUTHY_VALUES = frozenset({"1", "true", "yes", "on"})


class ChecksumMismatchError(Exception):
    """Raised when a downloaded archive fails SHA-256 verification.

    Distinct from generic install failure so callers can tell a supply-chain
    anomaly (CDN poisoning, MITM, tampered mirror) apart from a plain network
    error. An install must never proceed past this.
    """


def _normalized_arch() -> str | None:
    """Return a normalized arch key matching `RIPGREP_ASSETS`.

    Returns:
        The normalized architecture key (e.g. `"arm64"`, `"x86_64"`), or
            `None` for unsupported architectures (e.g. 32-bit, ppc, s390x).
    """
    import platform

    raw = platform.machine().lower()
    return _ARCH_ALIASES.get(raw)


def managed_rg_path() -> Path:
    """Return the managed ripgrep binary path (`.exe` on Windows).

    Returns:
        Absolute path to where the managed `rg` binary lives (whether or
            not it currently exists on disk).
    """
    name = "rg.exe" if sys.platform == "win32" else "rg"
    return BIN_DIR / name


def is_offline() -> bool:
    """Return whether managed-tool downloads are disabled via env var.

    Returns:
        `True` when `BOG_AGENTS_OFFLINE` is set to a truthy value.
    """
    raw = os.environ.get(OFFLINE_ENV)
    if raw is None:
        return False
    return raw.strip().lower() in _TRUTHY_VALUES


def _has_connectivity() -> bool:
    """Return whether a fast TCP probe to the release host succeeds.

    Uses a short, bounded TCP connect so a sealed / air-gapped box fails
    within `_CONNECTIVITY_TIMEOUT_SECONDS` rather than stalling startup on a
    full download attempt. Any socket error (no route, DNS failure, refused,
    timeout) is treated as "no connectivity".

    Returns:
        `True` when the probe host is reachable, `False` otherwise.
    """
    import socket

    try:
        with socket.create_connection(
            (_CONNECTIVITY_HOST, _CONNECTIVITY_PORT),
            timeout=_CONNECTIVITY_TIMEOUT_SECONDS,
        ):
            return True
    except OSError:
        logger.debug("Connectivity probe to %s failed", _CONNECTIVITY_HOST)
        return False


def managed_install_allowed(*, config_path: Path | None = None) -> bool:
    """Return whether a managed-tool download is permitted right now.

    This is the single gate every download path must pass. It short-circuits
    (in cheap-first order) when:

    1. `[tools].auto_install` is disabled in `config.toml`.
    2. `BOG_AGENTS_OFFLINE` is set to a truthy value.
    3. A fast connectivity probe to the release host fails.

    Args:
        config_path: Optional override for the config file location.

            Defaults to `~/.bog-agents/config.toml`.

    Returns:
        `True` only when all three checks pass and a download may proceed.
    """
    from bog_agents_cli.model_config import tools_auto_install

    if not tools_auto_install(config_path):
        logger.debug("Managed install skipped: [tools].auto_install is false")
        return False
    if is_offline():
        logger.debug("Managed install skipped: %s is set", OFFLINE_ENV)
        return False
    if not _has_connectivity():
        logger.debug("Managed install skipped: no connectivity")
        return False
    return True


def prepend_managed_bin_to_path() -> None:
    """Prepend `BIN_DIR` to `os.environ["PATH"]` when a managed `rg` exists.

    Idempotent and network-free — safe to call on every startup, and the
    first thing startup should do so a subsequently-launched `rg` resolves to
    the managed copy. No-ops when the managed binary is absent (nothing to
    add) or when `BIN_DIR` is already the first `PATH` entry.
    """
    if not managed_rg_path().exists():
        return
    bin_str = str(BIN_DIR)
    current = os.environ.get("PATH", "")
    parts = current.split(os.pathsep) if current else []
    if parts and parts[0] == bin_str:
        return
    parts = [bin_str, *(p for p in parts if p != bin_str)]
    os.environ["PATH"] = os.pathsep.join(parts)


def _download_to(url: str, dest: Path) -> None:
    """Stream `url` to `dest`, bounded by a wall-clock deadline.

    `urlopen(timeout=...)` only bounds per-operation socket waits, so a slow
    trickle of bytes from a flaky peer could otherwise stretch the transfer
    well beyond the configured timeout. The chunked read here enforces an
    end-to-end deadline, checked between chunk reads. A non-200 response is
    rejected before any bytes are written so a proxy interstitial or an
    unfollowed redirect cannot masquerade downstream as a SHA-256 failure.

    Args:
        url: Fully-qualified HTTPS URL to fetch.
        dest: Destination path to stream the response body into.

    Raises:
        TimeoutError: When total transfer time exceeds the deadline.
        urllib.error.URLError: When the response status is not 200.
    """
    import time
    import urllib.error
    import urllib.request

    deadline = time.monotonic() + _DOWNLOAD_TIMEOUT_SECONDS
    with (
        urllib.request.urlopen(url, timeout=_DOWNLOAD_TIMEOUT_SECONDS) as resp,
        dest.open("wb") as fh,
    ):
        status = getattr(resp, "status", None)
        if status is not None and status != _HTTP_OK:
            msg = f"Unexpected HTTP {status} response fetching {url}"
            raise urllib.error.URLError(msg)
        while True:
            if time.monotonic() > deadline:
                msg = (
                    f"Download of {url} exceeded {_DOWNLOAD_TIMEOUT_SECONDS}s deadline"
                )
                raise TimeoutError(msg)
            chunk = resp.read(_DOWNLOAD_CHUNK_BYTES)
            if not chunk:
                break
            fh.write(chunk)


def _verify_sha256(path: Path, expected_hex: str) -> None:
    """Verify `path` matches `expected_hex`.

    Args:
        path: File whose SHA-256 digest is checked.
        expected_hex: The pinned, expected hex digest.

    Raises:
        ChecksumMismatchError: When the SHA-256 of `path` differs from
            `expected_hex`.
    """
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    actual = digest.hexdigest()
    if actual != expected_hex:
        msg = (
            f"Checksum mismatch for {path.name}: expected {expected_hex}, got {actual}"
        )
        raise ChecksumMismatchError(msg)


def _validate_legacy_tar_member(member: tarfile.TarInfo, extract_root: Path) -> None:
    """Reject tar members that cannot be safely extracted without filters.

    Args:
        member: Tar member to validate.
        extract_root: Directory members must resolve inside of.

    Raises:
        tarfile.TarError: If a member would extract outside `extract_root`
            or uses a tar entry type this fallback does not support.
    """
    import tarfile

    target = extract_root / member.name
    try:
        target.resolve().relative_to(extract_root.resolve())
    except ValueError as exc:
        msg = f"Refusing to extract unsafe tar member {member.name!r}"
        raise tarfile.TarError(msg) from exc

    if not (member.isfile() or member.isdir()):
        msg = f"Refusing to extract unsupported tar member {member.name!r}"
        raise tarfile.TarError(msg)


def _extract_tar_data(tf: tarfile.TarFile, extract_root: Path) -> None:
    """Extract a tar archive with `data` filtering when available.

    Python versions before the PEP 706 backport (3.11.0-3.11.3) lack the
    `filter` keyword on `extractall`. The fallback validates every member of
    the pinned release archive before using the legacy API.

    Args:
        tf: Open tar archive.
        extract_root: Directory to extract into.

    Raises:
        TypeError: Re-raised when `extractall` rejects a non-`filter`
            keyword (i.e. an unrelated `TypeError` we should not swallow).
    """
    try:
        tf.extractall(extract_root, filter="data")
    except TypeError as exc:
        if "filter" not in str(exc):
            raise
        members = tf.getmembers()
        for member in members:
            _validate_legacy_tar_member(member, extract_root)
        tf.extractall(extract_root, members=members)  # noqa: S202  # validated above


def _extract_zip_validated(zf: zipfile.ZipFile, extract_root: Path) -> None:
    """Extract a zip archive after validating each member's path.

    `ZipFile.extractall` sanitizes absolute paths and parent-relative
    components on modern Python, but defense-in-depth here keeps the
    SHA-256-verified archive from being the only line of defense against a
    zip-slip variant in a future upstream archive.

    Args:
        zf: Open zip archive.
        extract_root: Directory to extract into.

    Raises:
        zipfile.BadZipFile: If a member would extract outside `extract_root`.
    """
    import zipfile

    extract_root.mkdir(parents=True, exist_ok=True)
    root = extract_root.resolve()
    for member in zf.infolist():
        target = (extract_root / member.filename).resolve()
        try:
            target.relative_to(root)
        except ValueError as exc:
            msg = f"Refusing to extract unsafe zip member {member.filename!r}"
            raise zipfile.BadZipFile(msg) from exc
    zf.extractall(extract_root)  # noqa: S202  # validated above


def _extract_rg(archive: Path, extract_root: Path) -> Path:
    """Extract `archive` and locate the `rg` binary inside.

    Handles both `.tar.gz` and `.zip` archives. Release archives nest the
    binary under `ripgrep-<ver>-<triple>/`, so we walk the tree to find it
    rather than hard-coding the prefix.

    Args:
        archive: Downloaded archive file.
        extract_root: Directory to extract into.

    Returns:
        Absolute path to the extracted `rg` (or `rg.exe`) binary.

    Raises:
        FileNotFoundError: When the archive does not contain an `rg` binary.
    """
    import tarfile
    import zipfile

    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as zf:
            _extract_zip_validated(zf, extract_root)
    else:
        with tarfile.open(archive, mode="r:*") as tf:
            _extract_tar_data(tf, extract_root)

    target_name = "rg.exe" if sys.platform == "win32" else "rg"
    for path in extract_root.rglob(target_name):
        if path.is_file():
            return path
    msg = f"Could not find {target_name} inside {archive.name}"
    raise FileNotFoundError(msg)


def _install_ripgrep_sync(asset: str, sha256: str) -> Path:
    """Download, verify, extract, and install ripgrep atomically.

    Staging happens *inside* `BIN_DIR` so the final rename is on the same
    filesystem and therefore atomic. `_verify_sha256` propagates
    `ChecksumMismatchError` to abort the install before any move — an
    unverified archive is never installed. On POSIX the extracted binary is
    marked executable (`0o755`) before it is moved into place.

    Args:
        asset: Release asset filename to download.
        sha256: Pinned hex digest the download must match.

    `_verify_sha256` propagates `ChecksumMismatchError` on a mismatch, which
    aborts the install before the extracted binary is moved into place.

    Returns:
        Absolute path to the installed `rg` entrypoint.
    """
    import tempfile

    BIN_DIR.mkdir(parents=True, exist_ok=True)
    url = f"{_RELEASE_URL_PREFIX}/{asset}"
    with tempfile.TemporaryDirectory(prefix=".bog-agents-rg-", dir=BIN_DIR) as tmp_str:
        tmp = Path(tmp_str)
        archive = tmp / asset
        _download_to(url, archive)
        _verify_sha256(archive, sha256)
        extracted = _extract_rg(archive, tmp / "unpacked")
        if sys.platform != "win32":
            extracted.chmod(0o755)
        dest = managed_rg_path()
        extracted.replace(dest)
        return dest


def ensure_ripgrep(*, config_path: Path | None = None) -> Path | None:
    """Ensure a usable `rg` binary is available, installing if necessary.

    Resolution order:

    1. If a managed `rg` already exists, return it.
    2. Else if a `rg` is on `PATH`, return its resolved path (no install).
    3. Else, if the install gate is open (`managed_install_allowed`) and the
        platform/arch has a pinned asset, download → SHA-256 verify →
        extract → install → prepend `BIN_DIR` to `PATH` → return the path.

    Network, extract, and permission errors are swallowed (logged) so callers
    fall back to the existing missing-tool warning. A checksum mismatch is
    logged loudly and returns `None` — the install never proceeds on a
    mismatch. An unsupported platform/arch also returns `None`.

    Args:
        config_path: Optional override for the config file location, used by
            the install gate. Defaults to `~/.bog-agents/config.toml`.

    Returns:
        Path to a usable `rg` binary, or `None` when one could not be located
            or installed.
    """
    import shutil
    import tarfile
    import urllib.error
    import zipfile

    managed = managed_rg_path()
    if managed.exists():
        return managed

    system_rg = shutil.which("rg")
    if system_rg is not None:
        return Path(system_rg)

    if not managed_install_allowed(config_path=config_path):
        return None

    arch = _normalized_arch()
    if arch is None:
        import platform

        logger.debug(
            "Skipping ripgrep install: unsupported arch %r", platform.machine()
        )
        return None

    asset_entry = RIPGREP_ASSETS.get((sys.platform, arch))
    if asset_entry is None:
        logger.debug(
            "Skipping ripgrep install: no asset for (%s, %s)", sys.platform, arch
        )
        return None
    asset, sha256 = asset_entry

    try:
        installed = _install_ripgrep_sync(asset, sha256)
    except ChecksumMismatchError:
        logger.warning(
            "Refusing to install ripgrep: downloaded archive failed SHA-256 "
            "verification (possible tampered mirror or MITM)",
            exc_info=True,
        )
        return None
    except (urllib.error.URLError, TimeoutError):
        logger.warning(
            "Could not download ripgrep from %s", _RELEASE_URL_PREFIX, exc_info=True
        )
        return None
    except (tarfile.TarError, zipfile.BadZipFile, FileNotFoundError) as exc:
        logger.warning("ripgrep install failed: archive error (%s)", type(exc).__name__)
        return None
    except OSError as exc:
        logger.warning("ripgrep install failed: %s", type(exc).__name__, exc_info=True)
        return None
    else:
        prepend_managed_bin_to_path()
        return installed


def describe_ripgrep() -> tuple[str, str]:
    """Describe how `rg` currently resolves, for diagnostics.

    Returns:
        A `(status, detail)` tuple where `status` is one of `"managed"`,
            `"system"`, or `"absent"`, and `detail` is a human-readable
            one-line description suitable for the doctor report.
    """
    import shutil

    managed = managed_rg_path()
    if managed.exists():
        return "managed", f"Managed ripgrep {RIPGREP_VERSION} at {managed}"
    system_rg = shutil.which("rg")
    if system_rg is not None:
        return "system", f"System ripgrep at {system_rg}"
    return "absent", "ripgrep not found (grep tool will use a slower fallback)"
