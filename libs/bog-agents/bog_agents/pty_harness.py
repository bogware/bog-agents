"""PTY harness — drive interactive terminal programs (Tier-2 #6).

Most coding agents can't touch full-screen TUIs (`vim`, `top`, a REPL). Grok
Build solves this with `ptyctl`: spawn a program in a real pseudo-terminal,
render its screen, send vim-notation keystrokes, and wait on screen conditions —
turning an interactive program into a request/response API. This module brings
that to bog.

Structure (each layer testable in isolation):
  * `encode_keys` — vim-notation (`"<Esc>:wq<CR>"`, `"<C-c>"`, `"<Up><Up>"`) →
    the exact terminal byte sequence. Pure, cross-platform.
  * `TerminalOutput` — accumulates PTY bytes and renders plain text (ANSI
    stripped, bounded scrollback). Pure, cross-platform.
  * wait conditions (`WaitText` / `WaitRegex` / `WaitGone` / `WaitStable`) — pure
    predicates over the rendered screen.
  * `PtySession` — the live layer: spawn + send + read + wait over a real PTY.
    Implemented on POSIX via the stdlib `pty`; Windows (ConPTY/pywinpty) is a
    documented follow-up, so `PtySession.supported()` reports availability and
    construction fails closed on an unsupported platform.

The pure layers are what most logic + tests live in; the session is a thin
driver over them.
"""

from __future__ import annotations

import contextlib
import os
import re
import time
from dataclasses import dataclass, field

# --- keystroke encoding (pure) ----------------------------------------------

# Named keys → their terminal byte sequences (xterm-ish).
_NAMED_KEYS: dict[str, str] = {
    "cr": "\r",
    "enter": "\r",
    "return": "\r",
    "lf": "\n",
    "nl": "\n",
    "esc": "\x1b",
    "escape": "\x1b",
    "tab": "\t",
    "space": " ",
    "bs": "\x7f",
    "backspace": "\x7f",
    "del": "\x1b[3~",
    "delete": "\x1b[3~",
    "up": "\x1b[A",
    "down": "\x1b[B",
    "right": "\x1b[C",
    "left": "\x1b[D",
    "home": "\x1b[H",
    "end": "\x1b[F",
    "pageup": "\x1b[5~",
    "pagedown": "\x1b[6~",
    "insert": "\x1b[2~",
    "f1": "\x1bOP",
    "f2": "\x1bOQ",
    "f3": "\x1bOR",
    "f4": "\x1bOS",
    "f5": "\x1b[15~",
    "f6": "\x1b[17~",
    "f7": "\x1b[18~",
    "f8": "\x1b[19~",
    "f9": "\x1b[20~",
    "f10": "\x1b[21~",
    "f11": "\x1b[23~",
    "f12": "\x1b[24~",
}

_TOKEN_RE = re.compile(r"<([^<>]+)>")


class KeyEncodeError(ValueError):
    """Raised when a key notation token cannot be parsed."""


def _resolve_token(token: str) -> str:
    """Resolve one `<...>` token (its inner text) to a byte string."""
    parts = token.split("-")
    mods = [p.upper() for p in parts[:-1]]
    key = parts[-1]

    # Base key: a named key, or a single literal character.
    named = _NAMED_KEYS.get(key.lower())
    if named is not None:
        base = named
    elif len(key) == 1:
        base = key
    else:
        msg = f"unknown key in token <{token}>"
        raise KeyEncodeError(msg)

    for mod in reversed(mods):
        if mod == "C":  # Ctrl: control-ify a single printable char
            if len(base) == 1:
                base = chr(ord(base.upper()) & 0x1F)
        elif mod in ("M", "A"):  # Meta/Alt: prefix ESC
            base = "\x1b" + base
        elif mod == "S":  # Shift: uppercase a single letter
            if len(base) == 1 and base.isalpha():
                base = base.upper()
        else:
            msg = f"unknown modifier {mod!r} in token <{token}>"
            raise KeyEncodeError(msg)
    return base


def encode_keys(notation: str) -> bytes:
    r"""Encode vim-style key notation into terminal bytes.

    Supports `<CR> <Esc> <Tab> <Up> <F5>` … named keys, modifiers
    `<C-c> <M-x> <S-Tab>`, and literal characters (typed verbatim). Example:
    ``encode_keys("<Esc>:wq<CR>")`` → ``b"\\x1b:wq\\r"``.

    Args:
        notation: The key notation string.

    Returns:
        The UTF-8 byte sequence to write to the PTY.

    Raises:
        KeyEncodeError: On an unparseable `<...>` token.
    """
    out: list[str] = []
    pos = 0
    for match in _TOKEN_RE.finditer(notation):
        out.append(notation[pos : match.start()])  # literal text before the token
        out.append(_resolve_token(match.group(1)))
        pos = match.end()
    out.append(notation[pos:])
    return "".join(out).encode("utf-8")


# --- terminal output model (pure) -------------------------------------------

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[]P^_].*?(?:\x1b\\|\x07)|\x1b[()][0-9A-Za-z]|\x1b[=>]|[\x00\x07\x08]")


def strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences + a few control bytes for plain-text views."""
    return _ANSI_RE.sub("", text)


class TerminalOutput:
    """Accumulates PTY bytes and renders a bounded plain-text view.

    This is a line buffer (ANSI stripped), not a full cursor-addressable grid —
    enough for text/regex/quiescence waits. A `pyte`-backed full grid is a
    documented follow-up.
    """

    def __init__(self, *, max_chars: int = 200_000) -> None:
        """Initialize with a rolling capacity of `max_chars` decoded characters."""
        self._raw = bytearray()
        self._max = max_chars

    def feed(self, data: bytes) -> None:
        """Append raw PTY bytes."""
        self._raw.extend(data)
        if len(self._raw) > self._max * 4:  # bytes can be >1/char; keep a generous tail
            del self._raw[: len(self._raw) - self._max * 4]

    @property
    def text(self) -> str:
        """The accumulated output with ANSI stripped (bounded to the tail)."""
        decoded = self._raw.decode("utf-8", errors="replace")
        stripped = strip_ansi(decoded)
        return stripped[-self._max :]

    @property
    def lines(self) -> list[str]:
        """The plain-text view split into lines."""
        return self.text.splitlines()

    def snapshot(self, *, tail_lines: int | None = None) -> str:
        """Return the plain-text view, optionally only the last `tail_lines`."""
        if tail_lines is None:
            return self.text
        return "\n".join(self.lines[-tail_lines:])


# --- wait conditions (pure) -------------------------------------------------


@dataclass
class WaitText:
    """Satisfied when `needle` appears in the screen."""

    needle: str

    def satisfied(self, ctx: WaitContext) -> bool:
        """True when the needle is present."""
        return self.needle in ctx.screen


@dataclass
class WaitRegex:
    """Satisfied when `pattern` matches the screen."""

    pattern: str

    def satisfied(self, ctx: WaitContext) -> bool:
        """True when the pattern matches somewhere in the screen."""
        return re.search(self.pattern, ctx.screen) is not None


@dataclass
class WaitGone:
    """Satisfied when `needle` is absent from the screen."""

    needle: str

    def satisfied(self, ctx: WaitContext) -> bool:
        """True when the needle is not present."""
        return self.needle not in ctx.screen


@dataclass
class WaitStable:
    """Satisfied when the screen hasn't changed for `quiet_ms` milliseconds."""

    quiet_ms: float

    def satisfied(self, ctx: WaitContext) -> bool:
        """True when the screen has been unchanged long enough."""
        return ctx.ms_since_change >= self.quiet_ms


@dataclass
class WaitContext:
    """State passed to a wait condition at each check.

    Attributes:
        screen: The current rendered screen text.
        ms_since_change: Milliseconds since the screen last changed.
    """

    screen: str
    ms_since_change: float


# A wait condition is anything with a `satisfied(ctx) -> bool` method.
WaitCondition = WaitText | WaitRegex | WaitGone | WaitStable


@dataclass
class WaitResult:
    """Outcome of a `PtySession.wait`.

    Attributes:
        ok: Whether the condition was met before the timeout.
        screen: The screen text at the moment of resolution/timeout.
        elapsed_s: How long the wait took.
    """

    ok: bool
    screen: str
    elapsed_s: float


# --- the live session (POSIX) -----------------------------------------------


def pty_supported() -> bool:
    """Whether a live `PtySession` can run on this platform.

    True on POSIX (stdlib `pty`), and on Windows when `pywinpty` (ConPTY) is
    installed — `pip install bog-agents[pty]` or `pip install pywinpty`.
    """
    return os.name == "posix" or _winpty_available()


def _winpty_available() -> bool:
    """True on Windows with `pywinpty` importable."""
    if os.name != "nt":
        return False
    try:
        import winpty  # noqa: F401
    except ImportError:
        return False
    return True


class _PosixPtyBackend:
    """A PTY backed by the stdlib `pty` (fork + fd)."""

    def __init__(self) -> None:
        self._pid = 0
        self._fd = -1

    def spawn(self, command: list[str], env: dict[str, str] | None, cwd: str | None) -> None:
        import pty as _pty  # POSIX-only stdlib module

        environ = dict(os.environ if env is None else env)
        environ.setdefault("TERM", "xterm-256color")
        environ.setdefault("COLORTERM", "truecolor")
        pid, fd = _pty.fork()
        if pid == 0:  # child
            if cwd:
                os.chdir(cwd)
            os.execvpe(command[0], command, environ)  # noqa: S606 - intentional PTY child exec
        self._pid = pid
        self._fd = fd

    def write(self, data: bytes) -> None:
        os.write(self._fd, data)

    def read_available(self) -> bytes:
        import select

        chunks = bytearray()
        while True:
            ready, _, _ = select.select([self._fd], [], [], 0)
            if not ready:
                break
            try:
                data = os.read(self._fd, 65536)
            except OSError:
                break
            if not data:
                break
            chunks.extend(data)
        return bytes(chunks)

    def close(self) -> None:
        if self._fd >= 0:
            with contextlib.suppress(OSError):
                os.close(self._fd)
            self._fd = -1
        if self._pid > 0:
            with contextlib.suppress(OSError):
                os.waitpid(self._pid, os.WNOHANG)


class _WindowsPtyBackend:
    """A PTY backed by `pywinpty` (ConPTY)."""

    def __init__(self, *, cols: int = 120, rows: int = 40) -> None:
        self._cols = cols
        self._rows = rows
        self._pty: object | None = None

    def spawn(self, command: list[str], env: dict[str, str] | None, cwd: str | None) -> None:
        import shutil
        import subprocess

        from winpty import PTY

        # CreateProcess does not PATH-search the application name, so resolve it.
        # pywinpty prepends `appname` as argv[0], so `cmdline` is the args only.
        appname = shutil.which(command[0]) or command[0]
        cmdline = subprocess.list2cmdline(command[1:]) if len(command) > 1 else ""
        env_block: str | None = None
        if env is not None:
            env_block = "".join(f"{k}={v}\0" for k, v in env.items()) + "\0"
        self._pty = PTY(self._cols, self._rows)
        self._pty.spawn(appname, cmdline=cmdline, cwd=cwd, env=env_block)  # type: ignore[attr-defined]

    def write(self, data: bytes) -> None:
        if self._pty is not None:
            self._pty.write(data.decode("utf-8", errors="replace"))  # type: ignore[attr-defined]

    def read_available(self) -> bytes:
        if self._pty is None:
            return b""
        try:
            text = self._pty.read(blocking=False)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - pywinpty raises varied errors at EOF
            return b""
        return text.encode("utf-8", errors="replace") if text else b""

    def close(self) -> None:
        # pywinpty terminates the child when the PTY object is dropped.
        self._pty = None


def _make_backend() -> _PosixPtyBackend | _WindowsPtyBackend:
    """Pick the PTY backend for this platform (raises if unsupported)."""
    if os.name == "posix":
        return _PosixPtyBackend()
    if _winpty_available():
        return _WindowsPtyBackend()
    msg = "PtySession is unsupported here: POSIX needs stdlib pty; Windows needs pywinpty (pip install pywinpty)"
    raise RuntimeError(msg)


@dataclass
class PtySession:
    """Drive a program in a real pseudo-terminal (POSIX or Windows/ConPTY).

    Spawn a command, send vim-notation keystrokes, read the rendered screen, and
    wait on screen conditions. Construction raises on an unsupported platform;
    check `pty_supported()` first.
    """

    command: list[str]
    env: dict[str, str] | None = None
    cwd: str | None = None
    _backend: _PosixPtyBackend | _WindowsPtyBackend = field(init=False)
    _out: TerminalOutput = field(default_factory=TerminalOutput, init=False)
    _last_change: float = field(default=0.0, init=False)
    _last_len: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        """Select a PTY backend (fail closed on an unsupported platform)."""
        self._backend = _make_backend()

    def start(self) -> None:
        """Spawn the command in a new PTY (truecolor xterm)."""
        self._backend.spawn(self.command, self.env, self.cwd)
        self._last_change = time.monotonic()

    def _drain(self) -> None:
        """Read whatever is currently available from the PTY (non-blocking)."""
        data = self._backend.read_available()
        if data:
            self._out.feed(data)
        length = len(self._out.text)
        if length != self._last_len:
            self._last_len = length
            self._last_change = time.monotonic()

    def send(self, notation: str) -> None:
        """Encode `notation` and write it to the PTY."""
        self._backend.write(encode_keys(notation))

    def screen(self, *, tail_lines: int | None = None) -> str:
        """Drain pending output and return the current rendered screen."""
        self._drain()
        return self._out.snapshot(tail_lines=tail_lines)

    def wait(self, condition: WaitCondition, *, timeout_s: float = 10.0, poll_s: float = 0.05) -> WaitResult:
        """Poll until `condition` is satisfied or `timeout_s` elapses."""
        start = time.monotonic()
        while True:
            self._drain()
            ctx = WaitContext(screen=self._out.text, ms_since_change=(time.monotonic() - self._last_change) * 1000.0)
            if condition.satisfied(ctx):
                return WaitResult(ok=True, screen=self._out.text, elapsed_s=time.monotonic() - start)
            if time.monotonic() - start >= timeout_s:
                return WaitResult(ok=False, screen=self._out.text, elapsed_s=time.monotonic() - start)
            time.sleep(poll_s)

    def close(self) -> None:
        """Close the PTY and reap the child."""
        self._backend.close()


class PtyController:
    """A registry of named `PtySession`s for agent tools (Tier-2 #6).

    Holds interactive terminal sessions across tool calls so an agent can
    `start` a program, `send` keystrokes, read the `screen`, and `wait` on
    conditions — driving `vim`/`top`/REPLs. All methods return human-readable
    strings suitable for a tool result and never raise on bad input.
    """

    def __init__(self) -> None:
        """Initialize an empty session registry."""
        self._sessions: dict[str, PtySession] = {}

    def start(self, name: str, command: str, *, cwd: str | None = None) -> str:
        """Start `command` in a new PTY under `name` (replacing any existing one)."""
        import shlex

        if not pty_supported():
            return "PTY sessions are unavailable here (POSIX needs stdlib pty; Windows needs `pip install pywinpty`)."
        if name in self._sessions:
            self.close(name)
        try:
            argv = shlex.split(command, posix=os.name == "posix")
            if not argv:
                return "Empty command."
            session = PtySession(command=argv, cwd=cwd)
            session.start()
        except Exception as exc:  # noqa: BLE001 - a tool entry point must never crash the turn
            return f"Could not start '{command}': {exc}"
        self._sessions[name] = session
        return f"Started PTY session '{name}' running: {command}"

    def send(self, name: str, keys: str) -> str:
        """Send vim-notation keystrokes to session `name`."""
        session = self._sessions.get(name)
        if session is None:
            return f"No PTY session named '{name}'."
        try:
            session.send(keys)
        except (OSError, KeyEncodeError) as exc:
            return f"Could not send keys: {exc}"
        return f"Sent to '{name}'."

    def screen(self, name: str, *, tail_lines: int = 40) -> str:
        """Return the current rendered screen of session `name`."""
        session = self._sessions.get(name)
        if session is None:
            return f"No PTY session named '{name}'."
        return session.screen(tail_lines=tail_lines) or "<no output yet>"

    def wait(self, name: str, until: str, target: str = "", *, timeout_s: float = 10.0) -> str:
        """Wait on a screen condition, then return the screen.

        `until` is one of ``text`` / ``regex`` / ``gone`` / ``stable`` (for
        ``stable``, `target` is the quiet-milliseconds, default 500).
        """
        session = self._sessions.get(name)
        if session is None:
            return f"No PTY session named '{name}'."
        condition: WaitCondition
        kind = until.strip().lower()
        if kind == "text":
            condition = WaitText(target)
        elif kind == "regex":
            condition = WaitRegex(target)
        elif kind == "gone":
            condition = WaitGone(target)
        elif kind == "stable":
            condition = WaitStable(quiet_ms=float(target) if target else 500.0)
        else:
            return f"Unknown wait kind '{until}' (use text/regex/gone/stable)."
        result = session.wait(condition, timeout_s=timeout_s)
        status = "matched" if result.ok else f"timed out after {result.elapsed_s:.1f}s"
        return f"[{status}]\n{session.screen(tail_lines=40)}"

    def close(self, name: str) -> str:
        """Close and remove session `name`."""
        session = self._sessions.pop(name, None)
        if session is None:
            return f"No PTY session named '{name}'."
        session.close()
        return f"Closed PTY session '{name}'."

    def list_sessions(self) -> str:
        """List active PTY session names."""
        if not self._sessions:
            return "No active PTY sessions."
        return "Active PTY sessions: " + ", ".join(sorted(self._sessions))

    def shutdown(self) -> None:
        """Close every session (call on agent teardown)."""
        for session in list(self._sessions.values()):
            session.close()
        self._sessions.clear()


__all__ = [
    "KeyEncodeError",
    "PtyController",
    "PtySession",
    "TerminalOutput",
    "WaitContext",
    "WaitGone",
    "WaitRegex",
    "WaitResult",
    "WaitStable",
    "WaitText",
    "encode_keys",
    "pty_supported",
    "strip_ansi",
]
