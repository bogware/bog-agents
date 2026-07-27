"""`LocalShellBackend`: Filesystem backend with local shell execution.

This backend extends FilesystemBackend to add shell command execution on the
local host system. By default it provides NO sandboxing or isolation — all
operations run directly on the host machine with full system access.

An OS-level sandbox is available opt-in via the `sandbox=` parameter (#22):
pass a `bog_agents.sandbox.LocalSandbox` to confine each command with
bubblewrap (Linux) or seatbelt (macOS) — filesystem confinement plus a hard
network cut or a proxy-enforced egress allowlist. Set `require_sandbox=True` to
fail closed where no native launcher is available (e.g. Windows today).
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import uuid
import warnings
from typing import TYPE_CHECKING

from bog_agents.backends.filesystem import FilesystemBackend
from bog_agents.backends.protocol import ExecuteResponse, SandboxBackendProtocol
from bog_agents.sandbox import egress_proxy, local_sandbox

if TYPE_CHECKING:
    from pathlib import Path

    from bog_agents.sandbox.egress_proxy import AllowlistEgressProxy
    from bog_agents.sandbox.local_sandbox import LocalSandbox

logger = logging.getLogger(__name__)

DEFAULT_EXECUTE_TIMEOUT = 7200
"""Default timeout in seconds for shell command execution.

Defaults to 2 hours so legitimate long-running commands (builds, test
suites, large data fetches, fan-out scripts) don't get cut short. Lower
it per-instance via ``LocalShellBackend(timeout=...)`` or per-call via
the ``timeout`` kwarg on ``execute()`` / ``aexecute()`` if you want a
tighter ceiling.
"""

# Patterns for commands that can cause catastrophic, irreversible damage.
# Each entry is a (pattern, description) tuple. Blocked by default; pass
# allow_dangerous=True to LocalShellBackend to downgrade to a warning.
#
# IMPORTANT — this gate is an ACCIDENT-CATCHER, not an adversary-catcher.
# A determined LLM can bypass any regex (e.g. ``python -c 'shutil.rmtree("/")'``
# from a long ago shell, base64-encoded payloads, novel rm-equivalent
# binaries). The real safeguard for adversarial inputs is HITL +
# SafeToolsMiddleware. The patterns here exist so the model doesn't
# accidentally clobber a developer's home directory while interpreting a
# README literally. P0-K in REVIEW.md expanded the pattern list to cover
# the most common bypasses; do NOT treat the gate as a security boundary.
_DANGEROUS_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # --- Linux-y file/disk destruction ------------------------------------
    (re.compile(r"rm\s+(-[a-zA-Z]*f[a-zA-Z]*\s+)?/", re.IGNORECASE), "rm targeting root path"),
    (re.compile(r"rm\s+-[a-zA-Z]*r[a-zA-Z]*f|rm\s+-[a-zA-Z]*f[a-zA-Z]*r", re.IGNORECASE), "recursive force remove (rm -rf)"),
    (re.compile(r"rm\s+--no-preserve-root", re.IGNORECASE), "rm with --no-preserve-root"),
    (re.compile(r"rm\s+(--recursive\b|--force\b).*(--recursive\b|--force\b)", re.IGNORECASE), "rm with long-form --recursive --force"),
    (re.compile(r"rm\s+-[a-zA-Z]*r", re.IGNORECASE), "recursive remove (rm -r)"),
    # ``find ... -delete`` was a documented P0-K bypass of the rm regex.
    (re.compile(r"\bfind\b.*\s-delete\b", re.IGNORECASE), "find … -delete (rm bypass)"),
    # ``git clean -fdx`` wipes untracked + ignored, which often includes the
    # venv / build dir. Matches single-group flags (-fdx) and split flags
    # (-f -d -x in any order).
    (
        re.compile(
            r"\bgit\s+clean\b[^|]*-[a-zA-Z]*f[a-zA-Z]*[dx][a-zA-Z]*|\bgit\s+clean\b[^|]*-[a-zA-Z]*[dx][a-zA-Z]*f[a-zA-Z]*|\bgit\s+clean\b[^|]*-[a-zA-Z]*f.*-[a-zA-Z]*[dx]|\bgit\s+clean\b[^|]*-[a-zA-Z]*[dx].*-[a-zA-Z]*f",
            re.IGNORECASE,
        ),
        "git clean -fdx (untracked wipe)",
    ),
    # ``python -c 'shutil.rmtree(...)'`` is a documented bypass.
    (re.compile(r"\bshutil\.rmtree\s*\(", re.IGNORECASE), "shutil.rmtree() inside python -c"),
    (re.compile(r"\bos\.unlink\s*\(\s*['\"][^'\"]*\.(ssh|aws|kube)\b", re.IGNORECASE), "os.unlink against credentials dir"),
    (re.compile(r":\(\)\s*\{\s*:|:\s*&\s*\};\s*:|:\(\)\{:\|:&\};:", re.IGNORECASE), "fork bomb"),
    (re.compile(r"mkfs\b", re.IGNORECASE), "filesystem format (mkfs)"),
    (re.compile(r"dd\s+.*\bof=/dev/", re.IGNORECASE), "raw device write (dd)"),
    (re.compile(r">\s*/dev/(s?d[a-z]|nvme|xvd)", re.IGNORECASE), "redirect to block device"),
    (re.compile(r"shred\s+", re.IGNORECASE), "shred (irreversible file destruction)"),
    (re.compile(r"wipefs\s+", re.IGNORECASE), "wipefs (wipe filesystem signatures)"),
    # --- Pipe-to-shell / pipe-to-interpreter ------------------------------
    (re.compile(r"curl\s+.*\|\s*(ba)?sh", re.IGNORECASE), "pipe URL to shell (curl|sh)"),
    (re.compile(r"wget\s+.*\|\s*(ba)?sh", re.IGNORECASE), "pipe URL to shell (wget|sh)"),
    (re.compile(r"curl\s+.*\|\s*python", re.IGNORECASE), "pipe URL to python"),
    (re.compile(r"chmod\s+(-[a-zA-Z]+\s+)?777\s+/", re.IGNORECASE), "world-writable root path"),
    (re.compile(r"chown\s+.*\s+/", re.IGNORECASE), "chown on root path"),
    (re.compile(r"nc\s+.*-e\s", re.IGNORECASE), "netcat exec shell"),
    (re.compile(r"\beval\s+.*base64", re.IGNORECASE), "eval base64 payload"),
    (re.compile(r"\bshutdown\b|\breboot\b|\bhalt\b|\bpoweroff\b", re.IGNORECASE), "system shutdown/reboot"),
    # --- Windows equivalents (cmd.exe + PowerShell) -----------------------
    # ``del /f /s /q`` is the cmd.exe rm-rf-like that bypassed the gate entirely.
    (re.compile(r"\bdel\s+(/[fFsSqQaA]\s*)+", re.IGNORECASE), "del /f /s /q (Windows recursive delete)"),
    (re.compile(r"\brmdir\s+/s\b", re.IGNORECASE), "rmdir /s (Windows recursive directory remove)"),
    (re.compile(r"\bformat\s+[a-zA-Z]:\s*/", re.IGNORECASE), "format <drive>: (Windows disk format)"),
    (re.compile(r"\bcipher\s+/w:", re.IGNORECASE), "cipher /w (Windows secure-wipe)"),
    (re.compile(r"\bRemove-Item\b.*-Recurse.*-Force|\bRemove-Item\b.*-Force.*-Recurse", re.IGNORECASE), "PowerShell Remove-Item -Recurse -Force"),
    (re.compile(r"\bClear-Disk\b", re.IGNORECASE), "PowerShell Clear-Disk"),
]


class LocalShellBackend(FilesystemBackend, SandboxBackendProtocol):
    """Filesystem backend with unrestricted local shell command execution.

    This backend extends `FilesystemBackend` to add shell command execution
    capabilities. Commands are executed directly on the host system without any
    sandboxing, process isolation, or security restrictions.

    !!! warning "Security Warning"

        This backend grants agents BOTH direct filesystem access AND unrestricted
        shell execution on your local machine. Use with extreme caution and only in
        appropriate environments.

        **Appropriate use cases:**

        - Local development CLIs (coding assistants, development tools)
        - Personal development environments where you trust the agent's code
        - CI/CD pipelines with proper secret management (see security considerations)

        **Inappropriate use cases:**

        - Production environments (e.g., web servers, APIs, multi-tenant systems)
        - Processing untrusted user input or executing untrusted code

        Use `StateBackend`, `StoreBackend`, or extend `BaseSandbox` for production.

        **Security risks:**

        - Agents can execute **arbitrary shell commands** with your user's permissions
        - Agents can read **any accessible file**, including secrets (API keys,
            credentials, `.env` files, SSH keys, etc.)
        - Combined with network tools, secrets may be exfiltrated via SSRF attacks
        - File modifications and command execution are **permanent and irreversible**
        - Agents can install packages, modify system files, spawn processes, etc.
        - **No process isolation** - commands run directly on your host system
        - **No resource limits** - commands can consume unlimited CPU, memory, disk

        **Recommended safeguards:**

        Since shell access is unrestricted and can bypass filesystem restrictions:

        1. **Enable Human-in-the-Loop (HITL) middleware** to review and approve ALL
            operations before execution. This is STRONGLY RECOMMENDED as your primary
            safeguard when using this backend.
        2. Run in dedicated development environments only - never on shared or
            production systems
        3. Never expose to untrusted users or allow execution of untrusted code
        4. For production environments requiring code execution, extend `BaseSandbox`
            to create a properly isolated backend (Docker containers, VMs, or other
            sandboxed execution environments)

        !!! note

            `virtual_mode=True` and path-based restrictions provide NO security
            with shell access enabled, since commands can access any path on the system

    Examples:
        ```python
        from bog_agents.backends import LocalShellBackend

        # Create backend with explicit environment
        backend = LocalShellBackend(root_dir="/home/user/project", env={"PATH": "/usr/bin:/bin"})

        # Execute shell commands (runs directly on host)
        result = backend.execute("ls -la")
        print(result.output)
        print(result.exit_code)

        # Use filesystem operations (inherited from FilesystemBackend)
        content = backend.read("/README.md")
        backend.write("/output.txt", "Hello world")

        # Inherit all environment variables
        backend = LocalShellBackend(root_dir="/home/user/project", inherit_env=True)
        ```
    """

    def __init__(
        self,
        root_dir: str | Path | None = None,
        *,
        virtual_mode: bool | None = None,
        timeout: int = DEFAULT_EXECUTE_TIMEOUT,
        max_output_bytes: int = 100_000,
        env: dict[str, str] | None = None,
        inherit_env: bool = False,
        allow_dangerous: bool = False,
        sandbox: LocalSandbox | None = None,
        require_sandbox: bool = False,
    ) -> None:
        """Initialize local shell backend with filesystem access.

        Args:
            root_dir: Working directory for both filesystem operations and shell commands.

                - If not provided, defaults to the current working directory.
                - Shell commands execute with this as their working directory.
                - When `virtual_mode=True` (default): Acts as a virtual root for filesystem
                    operations. Useful with `CompositeBackend` to support routing file
                    operations across different backend implementations. **Note:** This does
                    NOT restrict shell commands.
                - When `virtual_mode=False` (deprecated): Paths are used as-is. Agents can
                    access any file using absolute paths or `..` sequences.

            virtual_mode: Enable virtual path mode for filesystem operations.

                When `True` (default), treats `root_dir` as a virtual root filesystem. All
                paths are interpreted relative to `root_dir` (e.g., `/file.txt` maps to
                `{root_dir}/file.txt`). Path traversal (`..`, `~`) is blocked.

                **Primary use case:** Working with `CompositeBackend`, which routes
                different path prefixes to different backends. Virtual mode allows the
                CompositeBackend to strip route prefixes and pass normalized paths to
                each backend, enabling file operations to work correctly across multiple
                backend implementations.

                **Important:** This only affects filesystem operations. Shell commands
                executed via `execute()` are NOT restricted and can access any path.

            timeout: Default maximum time in seconds to wait for shell command execution.

                Defaults to 7200 seconds (2 hours) so legitimate long-running
                commands (builds, test suites, large data fetches) don't get
                cut short. Pass a smaller value here if you want a tighter
                ceiling, or override per-command via the `timeout` parameter
                on `execute()`.

                Commands exceeding this timeout will be terminated.

            max_output_bytes: Maximum number of bytes to capture from command output.
                Output exceeding this limit will be truncated. Defaults to 100,000 bytes.

            env: Environment variables for shell commands. If None, starts with an empty
                environment (unless `inherit_env=True`).

            inherit_env: Whether to inherit the parent process's environment variables.
                When False (default), only variables in `env` dict are available.
                When True, inherits all `os.environ` variables and applies `env` overrides.

            allow_dangerous: When `False` (default), commands matching known destructive
                patterns (e.g., `rm -rf /`, fork bombs, raw block device writes) raise a
                `PermissionError` before execution. When `True`, those commands are
                allowed but a WARNING is logged. Never set this to `True` in production
                environments.

            sandbox: Optional OS-level sandbox (bubblewrap on Linux, seatbelt on
                macOS). When set, `execute()` wraps each command in the native
                launcher — confining filesystem access to the sandbox's
                `working_dir` and cutting or allowlisting network egress. Off by
                default (this backend is unrestricted unless a sandbox is given).
                Windows has no launcher yet (ROADMAP #22), so on Windows a
                sandbox is honored only via `require_sandbox`.

            require_sandbox: Fail closed. When `True` and a `sandbox` is
                configured but no native launcher is available on this platform,
                `execute()` raises `PermissionError` rather than silently running
                the command unsandboxed. When `False` (default), an unavailable
                launcher logs a warning and the command runs unsandboxed.

        Raises:
            ValueError: If timeout is not positive.
        """
        if timeout <= 0:
            msg = f"timeout must be positive, got {timeout}"
            raise ValueError(msg)

        self._allow_dangerous = allow_dangerous

        if virtual_mode is None:
            virtual_mode = True
        elif virtual_mode is False:
            warnings.warn(
                "LocalShellBackend virtual_mode=False is deprecated. The default flipped to "
                "True (secure-by-default) in 0.8.0. Passing False disables path-based "
                "guardrails: absolute paths and '..' can bypass root_dir for filesystem "
                "operations. (Shell execution remains unrestricted regardless — "
                "LocalShellBackend provides no process isolation.) "
                "See https://github.com/bogware/bog-agents for usage guidelines.",
                DeprecationWarning,
                stacklevel=2,
            )

        # Initialize parent FilesystemBackend
        super().__init__(
            root_dir=root_dir,
            virtual_mode=virtual_mode,
            max_file_size_mb=10,
        )

        # Store execution parameters
        self._default_timeout = timeout
        self._max_output_bytes = max_output_bytes

        # Build environment based on inherit_env setting
        if inherit_env:
            self._env = os.environ.copy()
            if env is not None:
                self._env.update(env)
        else:
            self._env = env if env is not None else {}

        # OS-level sandbox wiring (opt-in; #22).
        self._sandbox = sandbox
        self._require_sandbox = require_sandbox
        self._egress_proxy: AllowlistEgressProxy | None = None

        # Generate unique sandbox ID
        self._sandbox_id = f"local-{uuid.uuid4().hex[:8]}"

    @property
    def id(self) -> str:
        """Unique identifier for this backend instance.

        Returns:
            String identifier in format "local-{random_hex}".
        """
        return self._sandbox_id

    def execute(
        self,
        command: str,
        *,
        timeout: int | None = None,
        allow_dangerous: bool = False,
    ) -> ExecuteResponse:
        r"""Execute a shell command directly on the host system.

        !!! danger "Unrestricted Execution"

            Commands are executed directly on your host system using `subprocess.run()`
            with `shell=True`. There is **no sandboxing, isolation, or security
            restrictions**. The command runs with your user's full permissions and can:

            - Access any file on the filesystem (regardless of `virtual_mode`)
            - Execute any program or script
            - Make network connections
            - Modify system configuration
            - Spawn additional processes
            - Install packages or modify dependencies

            **Always use Human-in-the-Loop (HITL) middleware when using this method.**

        The command is executed using the system shell (`/bin/sh` or equivalent) with
        the working directory set to the backend's `root_dir`. Stdout and stderr are
        combined into a single output stream.

        Args:
            command: Shell command string to execute.
                Examples: "python script.py", "ls -la", "grep pattern file.txt"

                **Security:** This string is passed directly to the shell. Agents can
                execute arbitrary commands including pipes, redirects, command
                substitution, etc.
            timeout: Maximum time in seconds to wait for this command.

                Overrides the default timeout set at init.

                If None, uses the default.
            allow_dangerous: Bypass the built-in dangerous-command gate. Only set
                this when the caller has already obtained explicit human approval
                (e.g., HITL middleware confirmed the command). Defaults to False.

        Returns:
            ExecuteResponse containing:
                - output: Combined stdout and stderr (stderr lines prefixed with [stderr])
                - exit_code: Process exit code (0 for success, non-zero for failure)
                - truncated: True if output was truncated due to size limits

        Raises:
            ValueError: If per-command timeout is not positive.

        Examples:
            ```python
            # Run a simple command
            result = backend.execute("echo hello")
            assert result.output == "hello\\n"
            assert result.exit_code == 0

            # Handle errors
            result = backend.execute("cat nonexistent.txt")
            assert result.exit_code != 0
            assert "[stderr]" in result.output

            # Check for truncation
            result = backend.execute("cat huge_file.txt")
            if result.truncated:
                print("Output was truncated")

            # Override timeout for long-running commands
            result = backend.execute("make build", timeout=300)

            # Commands run in root_dir, but can access any path
            result = backend.execute("cat /etc/passwd")  # Can read system files!
            ```
        """
        if not command or not isinstance(command, str):
            return ExecuteResponse(
                output="Error: Command must be a non-empty string.",
                exit_code=1,
                truncated=False,
            )

        # Check for dangerous patterns before executing.
        _allow = allow_dangerous or self._allow_dangerous
        for pattern, description in _DANGEROUS_PATTERNS:
            if pattern.search(command):
                if _allow:
                    logger.warning(
                        "Dangerous command allowed (allow_dangerous=True): %s — matched: %s",
                        command,
                        description,
                    )
                else:
                    msg = f"Dangerous command blocked: {description}. Pass allow_dangerous=True to LocalShellBackend or execute() to bypass."
                    raise PermissionError(msg)
                break

        effective_timeout = timeout if timeout is not None else self._default_timeout
        if effective_timeout <= 0:
            msg = f"timeout must be positive, got {effective_timeout}"
            raise ValueError(msg)

        # Resolve the actual process invocation (possibly sandbox-wrapped) and
        # the environment (possibly carrying egress-proxy vars). May raise
        # PermissionError when a sandbox is required but unavailable.
        popen_command, use_shell, run_env = self._prepare_execution(command)

        try:
            result = subprocess.run(
                popen_command,
                check=False,
                shell=use_shell,  # False when sandbox-wrapped (argv list), else LLM shell string
                capture_output=True,
                text=True,
                # Force UTF-8 decoding for stdout/stderr regardless of the
                # platform default. Windows' cp1252 default would crash the
                # subprocess reader thread on byte sequences emitted by
                # common tools — npx/vitest checkmarks (✓), tsc colored
                # output, ripgrep box-drawing, ripgrep ANSI sequences with
                # \xa0, etc. errors='replace' guarantees the reader never
                # dies and the agent always sees the actual command output.
                encoding="utf-8",
                errors="replace",
                # Redirect stdin from /dev/null so commands that expect
                # interactive input (e.g. Windows ``date`` / ``time``
                # which prompt for a new value, ``apt install`` / ``npm
                # init`` / ``git rebase -i`` / ``read``) fail fast with
                # an EOF rather than hanging the agent forever waiting
                # on a TTY that no one is sitting at. The agent can
                # always retry with a non-interactive flag.
                stdin=subprocess.DEVNULL,
                timeout=effective_timeout,
                env=run_env,
                cwd=str(self.cwd),  # Use the root_dir from FilesystemBackend
            )

            # Combine stdout and stderr
            # Prefix each stderr line with [stderr] for clear attribution.
            # Example: "hello\n[stderr] error: file not found"  # noqa: ERA001
            output_parts = []
            if result.stdout:
                output_parts.append(result.stdout)
            if result.stderr:
                stderr_lines = result.stderr.strip().split("\n")
                output_parts.extend(f"[stderr] {line}" for line in stderr_lines)

            output = "\n".join(output_parts) if output_parts else "<no output>"

            # Check for truncation
            truncated = False
            if len(output) > self._max_output_bytes:
                output = output[: self._max_output_bytes]
                output += f"\n\n... Output truncated at {self._max_output_bytes} bytes."
                truncated = True

            # Add exit code info if non-zero
            if result.returncode != 0:
                output = f"{output.rstrip()}\n\nExit code: {result.returncode}"

            return ExecuteResponse(
                output=output,
                exit_code=result.returncode,
                truncated=truncated,
            )

        except subprocess.TimeoutExpired:
            if timeout is not None:
                msg = f"Error: Command timed out after {effective_timeout} seconds (custom timeout). The command may be stuck or require more time."
            else:
                msg = f"Error: Command timed out after {effective_timeout} seconds. For long-running commands, re-run using the timeout parameter."
            return ExecuteResponse(
                output=msg,
                exit_code=124,  # Standard timeout exit code
                truncated=False,
            )
        except Exception as e:  # noqa: BLE001
            # Broad exception catch is intentional: we want to catch all execution errors
            # and return a consistent ExecuteResponse rather than propagating exceptions
            return ExecuteResponse(
                output=f"Error executing command ({type(e).__name__}): {e}",
                exit_code=1,
                truncated=False,
            )

    def _prepare_execution(self, command: str) -> tuple[str | list[str], bool, dict[str, str]]:
        """Resolve the process invocation, shell flag, and environment for a command.

        Without a sandbox this is a no-op: the command runs as a shell string in
        `self._env`. With a sandbox it wraps the command in the native launcher
        (argv list, `shell=False`) and merges egress-proxy env when the sandbox
        permits network. When a sandbox is configured but no launcher exists,
        either raises (`require_sandbox=True`) or falls back to unsandboxed
        execution with a warning.

        Args:
            command: The raw shell command the agent asked to run.

        Returns:
            ``(popen_command, use_shell, env)`` for `subprocess.run`.

        Raises:
            PermissionError: If a sandbox is required but unavailable here.
        """
        sandbox = self._sandbox
        if sandbox is None or sandbox.level == local_sandbox.SandboxLevel.DISABLED:
            return command, True, self._env

        if not local_sandbox.sandbox_launcher_available():
            if self._require_sandbox:
                support = local_sandbox.get_platform_sandbox_support()
                msg = (
                    f"Sandbox required but no OS launcher available on "
                    f"'{support.platform}' (need bubblewrap on Linux or "
                    f"sandbox-exec on macOS). Refusing to run unsandboxed."
                )
                raise PermissionError(msg)
            logger.warning(
                "Sandbox requested but no OS launcher available; running command unsandboxed. "
                "Pass require_sandbox=True to fail closed instead."
            )
            return command, True, self._env

        wrapped = local_sandbox.wrap_command_with_sandbox(command, sandbox)
        return wrapped, False, self._sandbox_env()

    def _sandbox_env(self) -> dict[str, str]:
        """Build the child environment for a sandboxed run, incl. egress proxy.

        When the sandbox permits network and an allowlist proxy is available
        (either a runner-provided `BOG_AGENTS_SANDBOX_EGRESS_PROXY`, or an
        internal proxy started on demand from the sandbox's `network_allowlist`),
        the proxy env vars are merged so well-behaved tools route egress through
        it. Unrestricted network (`allow_network=True`, no allowlist) adds no
        proxy vars.

        Returns:
            The environment dict to pass to the sandboxed subprocess.
        """
        env = dict(self._env)
        sandbox = self._sandbox
        if sandbox is None or not sandbox.network_enabled:
            return env

        proxy_url = env.get(egress_proxy.SANDBOX_EGRESS_PROXY_ENV) or os.environ.get(
            egress_proxy.SANDBOX_EGRESS_PROXY_ENV, ""
        )
        if not proxy_url and sandbox.network_allowlist:
            proxy_url = self._ensure_egress_proxy(sandbox.network_allowlist)
        if proxy_url:
            env.update(egress_proxy.egress_env_for(proxy_url))
        return env

    def _ensure_egress_proxy(self, allowlist: list[str]) -> str:
        """Start (once) an internal allowlist egress proxy and return its URL.

        The proxy lives for the backend's lifetime (daemon threads, so it dies
        with the process); `close()` stops it eagerly.

        Args:
            allowlist: Hostnames egress is permitted to.

        Returns:
            The proxy URL, or "" if the proxy could not be started.
        """
        if self._egress_proxy is not None:
            return self._egress_proxy.url
        try:
            proxy = egress_proxy.AllowlistEgressProxy(allowlist)
            proxy.start()
        except OSError as exc:
            logger.warning("Could not start egress allowlist proxy: %s", exc)
            return ""
        self._egress_proxy = proxy
        return proxy.url

    def close(self) -> None:
        """Release backend resources (stops the internal egress proxy if any)."""
        if self._egress_proxy is not None:
            self._egress_proxy.stop()
            self._egress_proxy = None


__all__ = ["DEFAULT_EXECUTE_TIMEOUT", "LocalShellBackend"]
