"""Opt-in `powershell` tool bundle (ROADMAP #61).

`execute` runs whatever the model writes through the platform shell — on
Windows that is `cmd.exe`, whose quoting rules and missing conveniences (`ls`,
`grep`, `cat` are all absent or different) cost a turn or two per session.
This bundle gives the agent a first-class PowerShell: the script goes to
`pwsh` (PowerShell 7) or `powershell.exe` (5.1) as one argv element, never
through `cmd.exe`, with `-NoProfile -NonInteractive -ExecutionPolicy Bypass`
so a user's profile or execution policy can neither hang nor block it.

Safety reuses the shell tool's gates rather than inventing new ones: the
same `_DANGEROUS_PATTERNS` accident-catcher (`dangerous_command_match`)
refuses the recursive deletes and disk wipes, and the CLI classifies the
`command` argument with the same auto-mode rules (`exec_risk`, git
classification, bash hygiene) it applies to `execute`. `find_powershell`
also side-steps the Microsoft Store "App execution alias": a zero-byte
`WindowsApps/pwsh.exe` that `shutil.which` happily returns and that fails
with `WinError 5` the moment it is spawned.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any

from langchain.tools import ToolRuntime  # noqa: TC002  # runtime type hint consumed by pydantic via StructuredTool
from langchain_core.tools import BaseTool, StructuredTool

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = logging.getLogger(__name__)

POWERSHELL_TOOL_NAME = "powershell"
DEFAULT_TIMEOUT_S = 120
DEFAULT_MAX_OUTPUT_BYTES = 100_000
_CANDIDATES: tuple[str, ...] = ("pwsh", "powershell")
_LAUNCH_FLAGS: tuple[str, ...] = ("-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command")


def is_windows_apps_alias(path: str | Path) -> bool:
    """Whether `path` is a Microsoft Store execution alias rather than a real executable.

    The alias lives under `.../Microsoft/WindowsApps/` and is a zero-byte
    reparse point; spawning it fails with `PermissionError: [WinError 5]`
    unless the Store app is installed.
    """
    p = Path(path)
    if "windowsapps" not in str(p).lower():
        return False
    try:
        return p.stat().st_size == 0
    except OSError:
        return True


def find_powershell(candidates: Sequence[str] = _CANDIDATES, *, which: Any = shutil.which) -> str | None:  # noqa: ANN401 - injectable for tests
    """Locate a usable PowerShell (`pwsh` first, then Windows PowerShell), skipping Store aliases."""
    for name in candidates:
        found = which(name)
        if not found:
            continue
        if is_windows_apps_alias(found):
            logger.debug("Skipping WindowsApps execution alias for %s: %s", name, found)
            continue
        return found
    return None


def powershell_argv(executable: str, command: str) -> list[str]:
    """The exact argv used to run `command` (no `cmd.exe`, no shell string parsing)."""
    return [executable, *_LAUNCH_FLAGS, command]


def _truncate(text: str, limit: int) -> tuple[str, bool]:
    if len(text.encode("utf-8", errors="replace")) <= limit:
        return text, False
    clipped = text.encode("utf-8", errors="replace")[:limit].decode("utf-8", errors="ignore")
    return clipped + f"\n... [output truncated to {limit} bytes]", True


def run_powershell(
    command: str,
    *,
    executable: str,
    cwd: str | Path | None = None,
    timeout: float = DEFAULT_TIMEOUT_S,
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    env: dict[str, str] | None = None,
    allow_dangerous: bool = False,
) -> str:
    """Run one PowerShell command and return its combined output as the tool would.

    Args:
        command: The PowerShell script text.
        executable: Path to `pwsh` or `powershell.exe` (see `find_powershell`).
        cwd: Working directory.
        timeout: Seconds before the process is killed.
        max_output_bytes: Output cap (UTF-8 bytes) before truncation.
        env: Environment for the child (defaults to the current process's).
        allow_dangerous: Skip the `_DANGEROUS_PATTERNS` accident-catcher.

    Returns:
        stdout + stderr, with `[exit code N]` appended when non-zero, or an
        `Error:` line for refused, timed-out or unlaunchable commands.
    """
    from bog_agents.backends.local_shell import dangerous_command_match

    if not command or not command.strip():
        return "Error: command must be a non-empty string."
    if not allow_dangerous:
        matched = dangerous_command_match(command)
        if matched is not None:
            return f"Error: dangerous command blocked: {matched}. Ask the user to run it themselves if it is intended."
    if timeout <= 0:
        return "Error: timeout must be positive."
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, PowerShell resolved by find_powershell
            powershell_argv(executable, command),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            cwd=str(cwd) if cwd is not None else None,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return f"Error: command timed out after {timeout:g}s and was killed."
    except OSError as exc:
        return f"Error: could not start PowerShell ({executable}): {exc}"
    output = result.stdout
    if result.stderr:
        output = f"{output}\n{result.stderr}" if output else result.stderr
    output, _truncated = _truncate(output, max_output_bytes)
    if result.returncode != 0:
        output = f"{output.rstrip()}\n[exit code {result.returncode}]".lstrip("\n")
    return output or "(no output)"


def powershell_tool_bundle(
    backend: Any = None,  # noqa: ANN401 - a LocalShellBackend, or None
    *,
    cwd: str | Path | None = None,
    executable: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_S,
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    allow_dangerous: bool = False,
) -> list[BaseTool]:
    """Return the `powershell` tool, or an empty list when no PowerShell is installed.

    Args:
        backend: Optional `LocalShellBackend`; its `cwd` is used when `cwd` is not given.
        cwd: Working directory for scripts (defaults to the backend's, then the process's).
        executable: Explicit PowerShell path; auto-detected with `find_powershell` when `None`.
        timeout: Default per-call timeout in seconds.
        max_output_bytes: Output cap before truncation.
        allow_dangerous: Skip the shared dangerous-command gate (not recommended).

    Returns:
        `[powershell]` or `[]`.
    """
    exe = executable or find_powershell()
    if exe is None:
        logger.debug("powershell tool not registered: no pwsh/powershell on PATH")
        return []
    resolved_cwd = cwd if cwd is not None else getattr(backend, "cwd", None)

    def powershell(runtime: ToolRuntime[None, Any], command: str, timeout_seconds: float | None = None) -> str:
        """Run a PowerShell command (pwsh/PowerShell 7 when available) and return its output.

        Use it on Windows instead of `execute` when you need PowerShell cmdlets,
        object pipelines, or `.ps1` scripts; the command never passes through
        cmd.exe. Runs with -NoProfile -NonInteractive, so interactive prompts
        fail instead of hanging. Set `timeout_seconds` for long commands.
        """
        del runtime
        return run_powershell(
            command,
            executable=exe,
            cwd=resolved_cwd or Path.cwd(),
            timeout=timeout_seconds or timeout,
            max_output_bytes=max_output_bytes,
            allow_dangerous=allow_dangerous,
        )

    return [StructuredTool.from_function(func=powershell, name=POWERSHELL_TOOL_NAME)]


__all__ = [
    "DEFAULT_TIMEOUT_S",
    "POWERSHELL_TOOL_NAME",
    "find_powershell",
    "is_windows_apps_alias",
    "powershell_argv",
    "powershell_tool_bundle",
    "run_powershell",
]
