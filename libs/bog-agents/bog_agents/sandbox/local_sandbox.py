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


def sandbox_launcher_available() -> bool:
    """Whether a native OS sandbox launcher exists on this platform.

    True when bubblewrap (Linux) or sandbox-exec/seatbelt (macOS) is present.
    Callers use this to decide between wrapping a command and either
    failing closed or running it unsandboxed. Windows has no launcher yet
    (AppContainer wiring is tracked as ROADMAP #22).

    Returns:
        True if `wrap_command_with_sandbox` will actually confine the command.
    """
    support = get_platform_sandbox_support()
    return support.bubblewrap_available or support.seatbelt_available


@dataclass
class LocalSandbox:
    """Configuration for OS-level sandboxing.

    Args:
        level: Sandbox restriction level.
        working_dir: Directory the agent can access.
        allow_network: Whether to allow *unrestricted* network access.
        network_allowlist: Hostnames egress is restricted to. When non-empty,
            the network namespace is kept (so egress works) but traffic is
            expected to be routed through an allowlist proxy — see
            `bog_agents.sandbox.egress_proxy`. Empty + `allow_network=False`
            means a hard network cut (`--unshare-net` on Linux).
        extra_read_paths: Additional paths to allow read access to.
        extra_write_paths: Additional paths to allow write access to.
        deny_read_paths: Paths that must NOT be readable even inside the
            workspace (#11 hardening). On Linux each is bound over with
            `/dev/null` (files) or an empty tmpfs (dirs), so the secret reads as
            empty *and* can't be `mv`'d out and read elsewhere; on macOS an
            explicit seatbelt `(deny file-read* …)` rule is emitted.
        strip_secret_env: When True (default), environment variables whose names
            look secret (`*KEY*` / `*SECRET*` / `*TOKEN*` / `*PASSWORD*` /
            `*CREDENTIAL*`) are removed from the sandboxed child's environment,
            so an approved command can't read a secret sitting in the shell env.
        secret_env_patterns: Override the default secret-name substrings.
    """

    level: SandboxLevel = SandboxLevel.WORKSPACE_WRITE
    working_dir: Path = field(default_factory=Path.cwd)
    allow_network: bool = False
    network_allowlist: list[str] = field(default_factory=list)
    extra_read_paths: list[str] = field(default_factory=list)
    extra_write_paths: list[str] = field(default_factory=list)
    deny_read_paths: list[str] = field(default_factory=list)
    strip_secret_env: bool = True
    secret_env_patterns: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        """Detect platform support."""
        self._support = get_platform_sandbox_support()

    @property
    def network_enabled(self) -> bool:
        """Whether the sandbox keeps a network namespace (any egress possible).

        True for unrestricted access *or* allowlisted access (the allowlist is
        enforced by a proxy, not the namespace, so the namespace must stay).
        """
        return self.allow_network or bool(self.network_allowlist)


# Substrings (case-insensitive) that mark an env var name as secret-bearing.
_DEFAULT_SECRET_ENV_PATTERNS: tuple[str, ...] = (
    "KEY",
    "SECRET",
    "TOKEN",
    "PASSWORD",
    "PASSWD",
    "CREDENTIAL",
    "PRIVATE",
    "API",
)


def strip_secret_env(env: dict[str, str], patterns: list[str] | None = None) -> dict[str, str]:
    """Return `env` with secret-looking variables removed (#11 hardening).

    A variable is dropped when its NAME contains any of `patterns` (default
    `KEY`/`SECRET`/`TOKEN`/`PASSWORD`/…), case-insensitively — so an approved
    sandboxed command can't read a credential sitting in the shell environment.

    Args:
        env: The environment mapping.
        patterns: Override the default secret-name substrings.

    Returns:
        A new dict with secret-named variables removed.
    """
    pats = tuple(p.upper() for p in (patterns or _DEFAULT_SECRET_ENV_PATTERNS))
    return {k: v for k, v in env.items() if not any(p in k.upper() for p in pats)}


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

    # Deny-read paths (#11): bind /dev/null over a secret file (reads empty) or
    # an empty tmpfs over a secret dir. Placed AFTER the workspace bind so it
    # wins, and it also closes the `mv secret x && cat x` bypass since the bind
    # is on the path, not a copy. Applied last so nothing re-exposes them.
    for path in sandbox.deny_read_paths:
        p = Path(path)
        if p.is_dir():
            args.extend(["--tmpfs", path])
        elif p.exists():
            args.extend(["--ro-bind", "/dev/null", path])

    # Network access. `--unshare-all` already cut the net namespace; re-share
    # it when egress is wanted (unrestricted OR allowlisted — the allowlist is
    # enforced downstream by the egress proxy, so the namespace must stay).
    if sandbox.network_enabled:
        args.append("--share-net")

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

    # Deny-read paths (#11): emitted LAST so an explicit deny overrides any
    # allow above (Seatbelt applies the most recent matching rule). This is
    # airtight on macOS and covers files created after launch.
    profile_parts.extend(f'(deny file-read* (subpath "{path}"))' for path in sandbox.deny_read_paths)

    # Network access (allowlist egress is enforced by the proxy, so the profile
    # must still permit network when an allowlist is configured).
    if sandbox.network_enabled:
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
        # Write profile to a temp file. `delete=False` is required because
        # sandbox-exec reads the `-f` profile lazily at exec time, so the file
        # must outlive this function. NOTE: the call site that wires this API
        # into an executor is responsible for unlinking `profile_file.name`
        # after sandbox-exec exits (do NOT unlink here — see [S43]).
        profile_file = tempfile.NamedTemporaryFile(mode="w", suffix=".sb", delete=False, prefix="bog_agents_sandbox_", encoding="utf-8")
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
    network_allowlist: list[str] | None = None,
    extra_read_paths: list[str] | None = None,
    extra_write_paths: list[str] | None = None,
    deny_read_paths: list[str] | None = None,
    strip_secret_env: bool = True,
) -> LocalSandbox:
    """Create a local sandbox configuration.

    Args:
        level: Sandbox restriction level.
        working_dir: Directory the agent can access.
        allow_network: Whether to allow unrestricted network access.
        network_allowlist: Hostnames egress is restricted to (proxy-enforced);
            keeps the network namespace open while a hard cut is applied when
            both this is empty and `allow_network` is False.
        extra_read_paths: Additional paths to allow read access to.
        extra_write_paths: Additional paths to allow write access to.
        deny_read_paths: Paths that must not be readable (bound over / denied).
        strip_secret_env: Remove secret-looking env vars from the child (#11).

    Returns:
        Configured LocalSandbox instance.
    """
    return LocalSandbox(
        level=level,
        working_dir=working_dir or Path.cwd(),
        allow_network=allow_network,
        network_allowlist=network_allowlist or [],
        extra_read_paths=extra_read_paths or [],
        extra_write_paths=extra_write_paths or [],
        deny_read_paths=deny_read_paths or [],
        strip_secret_env=strip_secret_env,
    )
