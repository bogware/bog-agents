"""Background shell command execution for `LocalShellBackend` (Tier-1 #1).

Grok Build's bash tool never *kills* a slow foreground command — once it blocks
past a budget it is moved to the background and keeps running, retrievable by a
task id. This module provides that capability for bog: a cross-platform registry
of long-running shell commands with poll / wait / kill, each capturing merged
stdout+stderr into a bounded buffer via a reader thread.

The registry is the reusable core; `LocalShellBackend` wires it in as an opt-in
`background=` path and as the target for auto-background-on-timeout. It is
deliberately dependency-free (stdlib `subprocess`/`threading` only) and safe on
Windows (new process group + `taskkill /T` teardown) as well as POSIX
(`start_new_session` + `killpg`).
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass

_DEFAULT_MAX_OUTPUT_BYTES = 1_000_000
_KILL_GRACE_SECS = 2.0


@dataclass
class BackgroundResult:
    """A snapshot of a background command's state.

    Attributes:
        task_id: The command's registry id.
        status: ``"running"`` or ``"exited"``.
        output: Merged stdout+stderr captured so far (bounded, utf-8).
        exit_code: Process exit code, or None while still running.
        truncated: True if output hit the buffer cap and older bytes were dropped.
    """

    task_id: str
    status: str
    output: str
    exit_code: int | None
    truncated: bool = False

    @property
    def running(self) -> bool:
        """Whether the command is still running."""
        return self.status == "running"


class _BackgroundCommand:
    """One backgrounded process plus a reader thread draining its output."""

    def __init__(self, task_id: str, command: str, proc: subprocess.Popen[bytes], max_output_bytes: int) -> None:
        self.task_id = task_id
        self.command = command
        self.started_at = time.time()
        self._proc = proc
        self._max = max_output_bytes
        self._buf = bytearray()
        self._truncated = False
        self._lock = threading.Lock()
        self._reader = threading.Thread(target=self._drain, name=f"bg-shell-{task_id}", daemon=True)
        self._reader.start()

    def _drain(self) -> None:
        stream = self._proc.stdout
        if stream is None:
            return
        try:
            for chunk in iter(lambda: stream.read(4096), b""):
                with self._lock:
                    self._buf.extend(chunk)
                    if len(self._buf) > self._max:
                        # Keep the tail (most recent output) — a long-running
                        # command's latest lines are what the agent wants.
                        overflow = len(self._buf) - self._max
                        del self._buf[:overflow]
                        self._truncated = True
        except (OSError, ValueError):
            pass  # stream closed under us during teardown

    def snapshot(self) -> BackgroundResult:
        """Return the current state without blocking."""
        code = self._proc.poll()
        with self._lock:
            text = self._buf.decode("utf-8", errors="replace")
            truncated = self._truncated
        return BackgroundResult(
            task_id=self.task_id,
            status="running" if code is None else "exited",
            output=text,
            exit_code=code,
            truncated=truncated,
        )

    def wait(self, timeout: float | None) -> BackgroundResult:
        """Block up to ``timeout`` seconds for exit, then snapshot."""
        try:
            self._proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            pass
        return self.snapshot()

    def kill(self) -> None:
        """Terminate the process (and its whole tree, best effort)."""
        if self._proc.poll() is not None:
            return
        pid = self._proc.pid
        try:
            if os.name == "nt":
                # /T kills the whole tree; /F forces it.
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(pid)],
                    check=False,
                    capture_output=True,
                    timeout=10,
                )
            else:
                # We spawned with start_new_session=True, so the pid is a
                # process-group leader; signal the whole group.
                try:
                    os.killpg(os.getpgid(pid), signal.SIGTERM)
                except ProcessLookupError:
                    return
                try:
                    self._proc.wait(timeout=_KILL_GRACE_SECS)
                except subprocess.TimeoutExpired:
                    os.killpg(os.getpgid(pid), signal.SIGKILL)
        except (OSError, subprocess.SubprocessError):
            with _suppress():
                self._proc.kill()


class BackgroundShellManager:
    """A registry of background shell commands with poll / wait / kill.

    Thread-safe. Owned by a `LocalShellBackend`; call `close()` to tear down
    every still-running command when the backend is closed.
    """

    def __init__(self, *, max_output_bytes: int = _DEFAULT_MAX_OUTPUT_BYTES) -> None:
        """Initialize an empty registry.

        Args:
            max_output_bytes: Per-command output buffer cap (tail-kept on overflow).
        """
        self._max = max_output_bytes
        self._commands: dict[str, _BackgroundCommand] = {}
        self._lock = threading.Lock()

    def start(
        self,
        command: str | list[str],
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        shell: bool = True,
        display: str | None = None,
    ) -> str:
        """Spawn ``command`` in the background and return its task id.

        The process is started in its own session/group so the whole tree can be
        killed later. stdout and stderr are merged; stdin is closed.

        Args:
            command: Shell command string, or an argv list (e.g. a sandbox-wrapped
                invocation) when ``shell=False``.
            cwd: Working directory for the command.
            env: Environment for the command.
            shell: Run via the system shell (True, for a command string) or exec
                the argv directly (False, for a wrapped invocation).
            display: Human-readable command text for `list`/`poll` (defaults to
                ``command`` stringified).

        Returns:
            A short task id to poll / wait / kill on.
        """
        task_id = uuid.uuid4().hex[:12]
        popen_kwargs: dict[str, object] = {
            "shell": shell,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.STDOUT,
            "stdin": subprocess.DEVNULL,
            "cwd": cwd,
            "env": env,
            "bufsize": 0,
        }
        if os.name == "nt":
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
        else:
            popen_kwargs["start_new_session"] = True
        proc = subprocess.Popen(command, **popen_kwargs)  # type: ignore[call-overload]
        shown = display or (command if isinstance(command, str) else " ".join(command))
        cmd = _BackgroundCommand(task_id, shown, proc, self._max)
        with self._lock:
            self._commands[task_id] = cmd
        return task_id

    def discard(self, task_id: str) -> None:
        """Drop a (finished) command from the registry without killing it."""
        with self._lock:
            self._commands.pop(task_id, None)

    def adopt(self, command: str, proc: subprocess.Popen[bytes]) -> str:
        """Register an already-running `Popen` (e.g. a timed-out foreground run).

        The process must have been started with a piped, merged stdout so the
        registry's reader thread can continue draining it.

        Args:
            command: The original command string (for `list`).
            proc: A live `Popen` whose `stdout` is readable (stderr merged in).

        Returns:
            The task id the adopted process is now tracked under.
        """
        task_id = uuid.uuid4().hex[:12]
        cmd = _BackgroundCommand(task_id, command, proc, self._max)
        with self._lock:
            self._commands[task_id] = cmd
        return task_id

    def poll(self, task_id: str) -> BackgroundResult | None:
        """Return the current snapshot of ``task_id`` (None if unknown)."""
        cmd = self._get(task_id)
        return cmd.snapshot() if cmd else None

    def wait(self, task_ids: list[str], *, mode: str = "any", timeout: float | None = None) -> list[BackgroundResult]:
        """Wait for one or all of ``task_ids`` to exit, then snapshot each.

        Args:
            task_ids: Task ids to wait on.
            mode: ``"any"`` returns as soon as one exits; ``"all"`` waits for all.
            timeout: Overall wall-clock budget in seconds (None = no limit).

        Returns:
            A snapshot per known task id (unknown ids are skipped).
        """
        cmds = [c for c in (self._get(t) for t in task_ids) if c is not None]
        deadline = None if timeout is None else time.time() + timeout
        while True:
            snaps = [c.snapshot() for c in cmds]
            exited = [s for s in snaps if not s.running]
            if not cmds:
                return snaps
            if (mode == "any" and exited) or (mode == "all" and len(exited) == len(cmds)):
                return snaps
            if deadline is not None and time.time() >= deadline:
                return snaps
            time.sleep(0.05)

    def kill(self, task_id: str) -> bool:
        """Kill ``task_id`` and its process tree. Returns False if unknown."""
        cmd = self._get(task_id)
        if cmd is None:
            return False
        cmd.kill()
        return True

    def list(self) -> list[BackgroundResult]:
        """Snapshot every registered command (running and exited)."""
        with self._lock:
            cmds = list(self._commands.values())
        return [c.snapshot() for c in cmds]

    def close(self) -> None:
        """Kill every still-running command and clear the registry."""
        with self._lock:
            cmds = list(self._commands.values())
            self._commands.clear()
        for cmd in cmds:
            cmd.kill()

    def _get(self, task_id: str) -> _BackgroundCommand | None:
        with self._lock:
            return self._commands.get(task_id)


class _suppress:  # noqa: N801 - context-manager helper, lowercase by convention
    """Swallow OSError/SubprocessError during best-effort teardown."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: type[BaseException] | None, *_rest: object) -> bool:
        return exc_type is not None and issubclass(exc_type, (OSError, subprocess.SubprocessError))


__all__ = ["BackgroundResult", "BackgroundShellManager"]
