"""OS-level sandboxing using bubblewrap (Linux) and seatbelt (macOS).

Feature #2: Provides process-level isolation for local shell execution,
restricting filesystem access, network, and system calls.
"""

from __future__ import annotations

import logging
import platform
import shutil
import tempfile
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

logger = logging.getLogger(__name__)


class SandboxLevel(StrEnum):
    """Sandbox restriction levels."""

    READ_ONLY = "read-only"
    """Read-only access to workspace, no network."""

    WORKSPACE_WRITE = "workspace-write"
    """Read/write to workspace directory only, no network."""

    FULL_ACCESS = "full-access"
    """Full filesystem access, no network restrictions."""

    DISABLED = "disabled"
    """No sandboxing (default for backwards compatibility)."""


@dataclass
class SandboxSupport:
    """Platform sandbox capabilities."""

    platform: str
    """Operating system: 'linux', 'darwin', 'windows'."""

    bubblewrap_available: bool = False
    """Whether bubblewrap (bwrap) is available (Linux)."""

    landlock_available: bool = False
    """Whether Landlock is available (Linux 5.13+)."""

    seatbelt_available: bool = False
    """Whether sandbox-exec is available (macOS)."""

    best_method: str = "none"
    """Best available sandboxing method."""


def get_platform_sandbox_support() -> SandboxSupport:
    """Detect available sandboxing methods on the current platform.

    Returns:
        SandboxSupport describing available methods.
    """
    system = platform.system().lower()
    support = SandboxSupport(platform=system)

    if system == "linux":
        # Check for bubblewrap
        if shutil.which("bwrap"):
            support.bubblewrap_available = True
            support.best_method = "bubblewrap"

        # Check for Landlock support (Linux 5.13+)
        try:
            release = platform.release()
            major, minor = (int(x) for x in release.split(".")[:2])
            if major > 5 or (major == 5 and minor >= 13):  # noqa: PLR2004
                support.landlock_available = True
                if not support.bubblewrap_available:
                    support.best_method = "landlock"
        except (ValueError, AttributeError):
            pass

    elif system == "darwin":
        # macOS always has sandbox-exec
        if shutil.which("sandbox-exec"):
            support.seatbelt_available = True
            support.best_method = "seatbelt"

    return support


@dataclass
class LocalSandbox:
    """Configuration for OS-level sandboxing.

    Args:
        level: Sandbox restriction level.
        working_dir: Directory the agent can access.
        allow_network: Whether to allow network access.
        extra_read_paths: Additional paths to allow read access to.
        extra_write_paths: Additional paths to allow write access to.
    """

    level: SandboxLevel = SandboxLevel.WORKSPACE_WRITE
    working_dir: Path = field(default_factory=Path.cwd)
    allow_network: bool = False
    extra_read_paths: list[str] = field(default_factory=list)
    extra_write_paths: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        """Detect platform support."""
        self._support = get_platform_sandbox_support()


def _build_bubblewrap_args(sandbox: LocalSandbox) -> list[str]:
    """Build bubblewrap (bwrap) command arguments.

    Args:
        sandbox: Sandbox configuration.

    Returns:
        List of bwrap arguments.
    """
    args = [
        "bwrap",
        "--die-with-parent",
        "--unshare-all",
    ]

    # Always bind /usr, /lib, /bin, /etc for basic functionality
    for system_dir in ["/usr", "/lib", "/lib64", "/bin", "/sbin", "/etc"]:
        if Path(system_dir).exists():
            args.extend(["--ro-bind", system_dir, system_dir])

    # Bind /dev/null, /dev/urandom
    args.extend(["--dev", "/dev"])

    # Bind /tmp
    args.extend(["--tmpfs", "/tmp"])

    # Bind /proc
    args.extend(["--proc", "/proc"])

    # Working directory access
    work_dir = str(sandbox.working_dir)
    if sandbox.level == SandboxLevel.READ_ONLY:
        args.extend(["--ro-bind", work_dir, work_dir])
    elif sandbox.level == SandboxLevel.WORKSPACE_WRITE:
        args.extend(["--bind", work_dir, work_dir])
    elif sandbox.level == SandboxLevel.FULL_ACCESS:
        args.extend(["--bind", "/", "/"])

    # Extra read paths
    for path in sandbox.extra_read_paths:
        if Path(path).exists():
            args.extend(["--ro-bind", path, path])

    # Extra write paths
    for path in sandbox.extra_write_paths:
        if Path(path).exists():
            args.extend(["--bind", path, path])

    # Network access
    if not sandbox.allow_network:
        args.append("--unshare-net")

    # Set working directory
    args.extend(["--chdir", work_dir])

    return args


def _build_seatbelt_profile(sandbox: LocalSandbox) -> str:
    """Build a macOS seatbelt sandbox profile.

    Args:
        sandbox: Sandbox configuration.

    Returns:
        Seatbelt profile string.
    """
    work_dir = str(sandbox.working_dir)

    profile_parts = [
        "(version 1)",
        "(deny default)",
        "",
        ";; Allow basic process operations",
        "(allow process-exec)",
        "(allow process-fork)",
        "(allow signal)",
        "(allow sysctl-read)",
        "",
        ";; Allow reading system files",
        '(allow file-read* (subpath "/usr"))',
        '(allow file-read* (subpath "/bin"))',
        '(allow file-read* (subpath "/sbin"))',
        '(allow file-read* (subpath "/Library"))',
        '(allow file-read* (subpath "/System"))',
        '(allow file-read* (subpath "/etc"))',
        '(allow file-read* (subpath "/var"))',
        '(allow file-read* (subpath "/dev"))',
        '(allow file-read* (subpath "/tmp"))',
        '(allow file-write* (subpath "/tmp"))',
        '(allow file-write* (subpath "/dev"))',
        "",
        ";; Allow reading home directory essentials",
        '(allow file-read* (subpath "/Users"))',
    ]

    # Working directory access
    if sandbox.level == SandboxLevel.READ_ONLY:
        profile_parts.append(f'(allow file-read* (subpath "{work_dir}"))')
    elif sandbox.level == SandboxLevel.WORKSPACE_WRITE:
        profile_parts.append(f'(allow file-read* (subpath "{work_dir}"))')
        profile_parts.append(f'(allow file-write* (subpath "{work_dir}"))')
    elif sandbox.level == SandboxLevel.FULL_ACCESS:
        profile_parts.append("(allow file-read*)")
        profile_parts.append("(allow file-write*)")

    # Extra paths
    profile_parts.extend(f'(allow file-read* (subpath "{path}"))' for path in sandbox.extra_read_paths)
    for path in sandbox.extra_write_paths:
        profile_parts.append(f'(allow file-read* (subpath "{path}"))')
        profile_parts.append(f'(allow file-write* (subpath "{path}"))')

    # Network access
    if sandbox.allow_network:
        profile_parts.append("(allow network*)")
    else:
        profile_parts.append(";; Network denied by default")

    return "\n".join(profile_parts)


def wrap_command_with_sandbox(
    command: str,
    sandbox: LocalSandbox,
) -> list[str]:
    """Wrap a shell command with appropriate sandbox isolation.

    Args:
        command: The shell command to execute.
        sandbox: Sandbox configuration.

    Returns:
        List of command arguments with sandbox wrapper.
    """
    if sandbox.level == SandboxLevel.DISABLED:
        return ["sh", "-c", command]

    support = get_platform_sandbox_support()

    if support.bubblewrap_available:
        bwrap_args = _build_bubblewrap_args(sandbox)
        return [*bwrap_args, "sh", "-c", command]

    if support.seatbelt_available:
        profile = _build_seatbelt_profile(sandbox)
        # Write profile to temp file
        profile_file = tempfile.NamedTemporaryFile(mode="w", suffix=".sb", delete=False, prefix="bog_agents_sandbox_")
        profile_file.write(profile)
        profile_file.close()
        return ["sandbox-exec", "-f", profile_file.name, "sh", "-c", command]

    # No sandbox available — fall back to unsandboxed
    logger.warning(
        "No OS-level sandbox available on %s. Running command unsandboxed.",
        support.platform,
    )
    return ["sh", "-c", command]


def create_local_sandbox(
    *,
    level: SandboxLevel = SandboxLevel.WORKSPACE_WRITE,
    working_dir: Path | None = None,
    allow_network: bool = False,
    extra_read_paths: list[str] | None = None,
    extra_write_paths: list[str] | None = None,
) -> LocalSandbox:
    """Create a local sandbox configuration.

    Args:
        level: Sandbox restriction level.
        working_dir: Directory the agent can access.
        allow_network: Whether to allow network access.
        extra_read_paths: Additional paths to allow read access to.
        extra_write_paths: Additional paths to allow write access to.

    Returns:
        Configured LocalSandbox instance.
    """
    return LocalSandbox(
        level=level,
        working_dir=working_dir or Path.cwd(),
        allow_network=allow_network,
        extra_read_paths=extra_read_paths or [],
        extra_write_paths=extra_write_paths or [],
    )
