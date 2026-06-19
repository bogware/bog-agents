"""Resilient self-update for the bog-agents suite.

This module is the pure-logic core behind the interactive ``/update`` slash
command (the TUI handler is a thin layer over it). It is deliberately free of
any Textual import so it can be unit-tested without spinning up the app.

Design contract — **an update attempt must never crash or corrupt the CLI**:

* Every public function is defensive. Network, subprocess, and package-metadata
  failures degrade to a clear status plus a manual command — they never raise.
* The actual upgrade runs the *correct* command for how the CLI was installed
  (uv tool / pipx / pip). A source ("editable") checkout is detected and never
  auto-upgraded, because that would clobber the working tree.
* The upgrade is run as a captured, timed-out subprocess. On any failure the
  existing install is left exactly as it was; we only ever *add* a newer
  version on top via the package manager's own upgrade path.
* Nothing here restarts the process. A running Python interpreter holds the old
  modules (and on Windows the files are locked), so the caller tells the user to
  restart — it does not try to hot-swap itself.
"""

from __future__ import annotations

import enum
import json
import logging
import os
import shutil
import subprocess  # noqa: S404  # package-manager upgrades require subprocess
import sys
import time
from dataclasses import dataclass
from importlib import metadata
from typing import TYPE_CHECKING

from bog_agents_cli._version import __version__ as CLI_VERSION  # noqa: N812

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

CLI_PACKAGE = "bog-agents-cli"
SDK_PACKAGE = "bog-agents"
DAEMON_PACKAGE = "bog-agents-daemon"

_CACHE_TTL = 86_400  # 24h; only used as a soft cache, /update forces a fresh check.
_FETCH_TIMEOUT = 4  # seconds for the PyPI version probe.
_UPGRADE_TIMEOUT = 600  # seconds; pip/uv can be slow on a cold cache.
_USER_AGENT = f"bog-agents-cli/{CLI_VERSION} update-manager"


class InstallMethod(enum.Enum):
    """How the running CLI was installed — decides the upgrade command."""

    UV_TOOL = "uv tool"
    PIPX = "pipx"
    PIP = "pip"
    EDITABLE = "source checkout (editable)"
    UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# Install-method detection
# ---------------------------------------------------------------------------


def _distribution(name: str) -> metadata.Distribution | None:
    """Return the installed distribution for `name`, or None if absent."""
    try:
        return metadata.distribution(name)
    except Exception:
        return None


def _is_editable_install(name: str = CLI_PACKAGE) -> bool:
    """Detect a PEP 660 editable / source-checkout install via direct_url.json."""
    dist = _distribution(name)
    if dist is None:
        return False
    try:
        raw = dist.read_text("direct_url.json")
    except Exception:
        raw = None
    if not raw:
        return False
    try:
        info = json.loads(raw)
    except Exception:
        return False
    dir_info = info.get("dir_info") or {}
    return bool(dir_info.get("editable"))


def _normalised_executable() -> str:
    """The current interpreter path, lower-cased with forward slashes."""
    return (sys.executable or "").replace("\\", "/").lower()


def detect_install_method() -> InstallMethod:
    """Best-effort detection of how this CLI was installed.

    Falls back to `InstallMethod.UNKNOWN` on any error rather than guessing
    wrong — callers treat UNKNOWN as "show the command, don't auto-run".
    """
    try:
        if _is_editable_install():
            return InstallMethod.EDITABLE

        exe = _normalised_executable()

        # Honour explicit tool-dir overrides first.
        uv_tool_dir = os.environ.get("UV_TOOL_DIR")
        if uv_tool_dir and exe.startswith(uv_tool_dir.replace("\\", "/").lower()):
            return InstallMethod.UV_TOOL
        pipx_home = os.environ.get("PIPX_HOME")
        if pipx_home and exe.startswith(pipx_home.replace("\\", "/").lower()):
            return InstallMethod.PIPX

        # Default tool layouts: uv → .../uv/tools/<tool>/...  pipx → .../pipx/venvs/<tool>/...
        if "/uv/tools/" in exe:
            return InstallMethod.UV_TOOL
        if "/pipx/venvs/" in exe:
            return InstallMethod.PIPX

        # Anything else with a real interpreter: a venv or system pip install.
        return InstallMethod.PIP
    except Exception:
        logger.debug("install-method detection failed", exc_info=True)
        return InstallMethod.UNKNOWN


# ---------------------------------------------------------------------------
# Version discovery
# ---------------------------------------------------------------------------


def _config_dir() -> Path | None:
    try:
        from bog_agents_cli.model_config import DEFAULT_CONFIG_DIR

        return DEFAULT_CONFIG_DIR
    except Exception:
        return None


def _installed_version(pypi_name: str, fallback: str | None = None) -> str | None:
    """Installed version of `pypi_name`, or `fallback` if it isn't importable."""
    try:
        return metadata.version(pypi_name)
    except Exception:
        return fallback


def is_newer(latest: str, current: str) -> bool:
    """Return True when `latest` is strictly newer than `current`.

    Prefers `packaging.version` (handles pre/post/dev releases correctly) and
    falls back to a dotted-integer comparison. Returns False on any parse error
    so a malformed version string can never *trigger* an upgrade prompt.
    """
    try:
        from packaging.version import Version

        return Version(latest) > Version(current)
    except Exception:
        try:
            return _version_tuple(latest) > _version_tuple(current)
        except Exception:
            return False


def _version_tuple(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in value.strip().split("."))


def _fetch_latest_pypi(pypi_name: str, *, force: bool = False) -> str | None:
    """Fetch the latest version of `pypi_name` from PyPI, cached to disk.

    `force=True` skips the cache read (used by the interactive `/update` so the
    user always sees the truly-latest version). Returns None on any failure.
    """
    cache_dir = _config_dir()
    cache_file = cache_dir / f"latest_{pypi_name}.json" if cache_dir else None

    if cache_file is not None and not force:
        try:
            if cache_file.exists():
                data = json.loads(cache_file.read_text(encoding="utf-8"))
                if time.time() - data.get("checked_at", 0) < _CACHE_TTL:
                    return data.get("version")
        except Exception:
            logger.debug("update cache read failed for %s", pypi_name, exc_info=True)

    try:
        import requests

        resp = requests.get(
            f"https://pypi.org/pypi/{pypi_name}/json",
            headers={"User-Agent": _USER_AGENT},
            timeout=_FETCH_TIMEOUT,
        )
        resp.raise_for_status()
        latest: str = resp.json()["info"]["version"]
    except Exception:
        logger.debug("PyPI version fetch failed for %s", pypi_name, exc_info=True)
        return None

    if cache_file is not None:
        try:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(
                json.dumps({"version": latest, "checked_at": time.time()}),
                encoding="utf-8",
            )
        except Exception:
            logger.debug("update cache write failed for %s", pypi_name, exc_info=True)

    return latest


# ---------------------------------------------------------------------------
# Suite status
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PackageStatus:
    """Installed-vs-latest status for one suite package."""

    pypi_name: str
    label: str
    installed: bool
    current: str | None
    latest: str | None
    update_available: bool
    note: str = ""


@dataclass(frozen=True)
class SuiteStatus:
    """Aggregate update status for the suite + how the CLI was installed."""

    method: InstallMethod
    cli: PackageStatus
    sdk: PackageStatus
    daemon: PackageStatus | None

    @property
    def any_update(self) -> bool:
        """Whether the CLI or the (optional) daemon has an update available."""
        return self.cli.update_available or bool(
            self.daemon and self.daemon.update_available
        )


def get_suite_status(*, force: bool = True) -> SuiteStatus:
    """Collect installed-vs-latest status for the suite.

    Fully defensive — individual probe failures collapse to "unknown latest"
    (which simply means "no update offered"), never an exception.
    """
    method = detect_install_method()

    cli_current = _installed_version(CLI_PACKAGE, CLI_VERSION)
    cli_latest = _fetch_latest_pypi(CLI_PACKAGE, force=force)
    cli_update = bool(cli_latest and cli_current and is_newer(cli_latest, cli_current))
    cli = PackageStatus(
        CLI_PACKAGE, "bog-agents-cli", True, cli_current, cli_latest, cli_update
    )

    # The SDK is a hard dependency of the CLI; it moves with the CLI upgrade, so
    # we surface its version for information but never offer it separately.
    sdk_current = _installed_version(SDK_PACKAGE)
    sdk = PackageStatus(
        SDK_PACKAGE,
        "bog-agents (SDK)",
        sdk_current is not None,
        sdk_current,
        None,
        False,
        note="upgrades with the CLI",
    )

    # The daemon is a *separate* tool — only show it if the user actually has it.
    daemon: PackageStatus | None = None
    daemon_current = _installed_version(DAEMON_PACKAGE)
    if daemon_current is not None:
        daemon_latest = _fetch_latest_pypi(DAEMON_PACKAGE, force=force)
        daemon_update = bool(daemon_latest and is_newer(daemon_latest, daemon_current))
        daemon = PackageStatus(
            DAEMON_PACKAGE,
            "bog-agents-daemon",
            True,
            daemon_current,
            daemon_latest,
            daemon_update,
            note="separate tool",
        )

    return SuiteStatus(method=method, cli=cli, sdk=sdk, daemon=daemon)


def render_status(status: SuiteStatus) -> str:
    """Human-readable status block for the TUI / headless output."""
    lines = [f"Install method: {status.method.value}", ""]
    for pkg in (status.cli, status.sdk, status.daemon):
        if pkg is None:
            continue
        current = pkg.current or "unknown"
        if pkg.update_available and pkg.latest:
            state = f"{current}  ->  {pkg.latest}   (update available)"
        elif pkg.latest and pkg.current and pkg.latest == pkg.current:
            state = f"{current}   (up to date)"
        else:
            state = current
        suffix = f"   [{pkg.note}]" if pkg.note else ""
        lines.append(f"  {pkg.label:<20} {state}{suffix}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Upgrade planning + execution
# ---------------------------------------------------------------------------


def upgrade_command_display(method: InstallMethod, package: str = CLI_PACKAGE) -> str:
    """The canonical, copy-pasteable upgrade command for `method`."""
    if method == InstallMethod.UV_TOOL:
        return f"uv tool upgrade {package}"
    if method == InstallMethod.PIPX:
        return f"pipx upgrade {package}"
    if method == InstallMethod.EDITABLE:
        return "git pull && uv sync"
    # PIP and UNKNOWN both get the portable pip incantation.
    return f"pip install --upgrade {package}"


def build_upgrade_argv(
    method: InstallMethod, package: str = CLI_PACKAGE
) -> list[str] | None:
    """Build the argv to run for an automatic upgrade, or None if unsafe.

    Returns None for editable/unknown installs, or when the required executable
    (uv / pipx) is not on PATH — the caller then shows the manual command.
    """
    if method == InstallMethod.UV_TOOL:
        uv = shutil.which("uv")
        return [uv, "tool", "upgrade", package] if uv else None
    if method == InstallMethod.PIPX:
        pipx = shutil.which("pipx")
        return [pipx, "upgrade", package] if pipx else None
    if method == InstallMethod.PIP:
        # Use the *current* interpreter's pip so we always hit the right env.
        return [sys.executable, "-m", "pip", "install", "--upgrade", package]
    # EDITABLE / UNKNOWN → no safe automatic upgrade.
    return None


@dataclass(frozen=True)
class UpdatePlan:
    """Everything the TUI needs to confirm and run (or explain) an update."""

    needs_update: bool
    method: InstallMethod
    package: str
    current: str | None
    latest: str | None
    argv: list[str] | None
    display_command: str
    can_auto_update: bool
    guidance: str
    daemon_note: str


def build_plan(status: SuiteStatus) -> UpdatePlan:
    """Turn a `SuiteStatus` into a concrete, resilient update plan."""
    cli = status.cli
    method = status.method
    needs = cli.update_available
    display = upgrade_command_display(method)

    argv = build_upgrade_argv(method) if needs else None
    guidance = ""

    if needs and method == InstallMethod.EDITABLE:
        argv = None
        guidance = (
            "bog-agents-cli is installed from a source checkout (editable), so it "
            "can't be auto-upgraded without clobbering your working tree.\n"
            "Update it with:\n"
            "  git pull && uv sync\n"
            "Then restart bog-agents."
        )
    elif needs and argv is None:
        guidance = (
            "An automatic upgrade isn't available for this install method "
            f"({method.value}). Update manually:\n"
            f"  {display}\n"
            "Then restart bog-agents."
        )

    daemon_note = ""
    if status.daemon and status.daemon.update_available:
        daemon_note = (
            f"Heads up: the daemon (bog-agents-daemon {status.daemon.current} -> "
            f"{status.daemon.latest}) is a separate tool and is not updated here. "
            f"Update it with:\n  {upgrade_command_display(method, DAEMON_PACKAGE)}"
        )

    return UpdatePlan(
        needs_update=needs,
        method=method,
        package=CLI_PACKAGE,
        current=cli.current,
        latest=cli.latest,
        argv=argv,
        display_command=display,
        can_auto_update=needs and argv is not None,
        guidance=guidance,
        daemon_note=daemon_note,
    )


@dataclass(frozen=True)
class UpgradeOutcome:
    """Result of running an upgrade subprocess."""

    ok: bool
    returncode: int | None
    stdout: str
    stderr: str
    error: str | None  # None | "no_command" | "timeout" | "not_found" | "exception"


def _creationflags() -> int:
    """Suppress a flashing console window on Windows."""
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def run_upgrade(
    argv: list[str] | None, *, timeout: int = _UPGRADE_TIMEOUT
) -> UpgradeOutcome:
    """Run the upgrade `argv` and capture the result without ever raising.

    Safe to call from a worker thread. On timeout, missing executable, or any
    OS error the function returns a failure outcome with a category in
    `error`; the caller's existing install is untouched.
    """
    if not argv:
        return UpgradeOutcome(False, None, "", "", "no_command")

    try:
        proc = subprocess.run(  # noqa: S603  # argv is built from a fixed set of package managers
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            creationflags=_creationflags(),
        )
    except subprocess.TimeoutExpired:
        logger.debug("upgrade timed out: %s", argv, exc_info=True)
        return UpgradeOutcome(False, None, "", "", "timeout")
    except FileNotFoundError:
        logger.debug("upgrade executable not found: %s", argv, exc_info=True)
        return UpgradeOutcome(False, None, "", "", "not_found")
    except OSError as exc:
        logger.debug("upgrade OS error: %s", argv, exc_info=True)
        return UpgradeOutcome(False, None, "", str(exc), "exception")
    except Exception as exc:  # last-resort guard: a /update must never crash
        logger.debug("upgrade unexpected error: %s", argv, exc_info=True)
        return UpgradeOutcome(False, None, "", str(exc), "exception")

    return UpgradeOutcome(
        ok=proc.returncode == 0,
        returncode=proc.returncode,
        stdout=proc.stdout or "",
        stderr=proc.stderr or "",
        error=None,
    )


def describe_failure(outcome: UpgradeOutcome, plan: UpdatePlan) -> str:
    """Compose a friendly failure message that never leaves the user stuck."""
    reason = {
        "timeout": "the upgrade timed out",
        "not_found": "the package manager executable wasn't found",
        "no_command": "no upgrade command was available",
        "exception": "the upgrade command couldn't be launched",
    }.get(outcome.error or "", f"the upgrade exited with code {outcome.returncode}")

    tail = (outcome.stderr or outcome.stdout or "").strip()
    if tail:
        tail_lines = tail.splitlines()[-6:]
        detail = "\n".join(tail_lines)
        tail_block = f"\n\nDetails:\n{detail}"
    else:
        tail_block = ""

    return (
        f"Update failed: {reason}. Your current install (v{plan.current}) is "
        f"unchanged.\nYou can try again, or update manually:\n"
        f"  {plan.display_command}{tail_block}"
    )
