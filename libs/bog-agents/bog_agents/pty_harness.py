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
    """Whether a live `PtySession` can run on this platform (POSIX only today)."""
    return os.name == "posix"


@dataclass
class PtySession:
    """Drive a program in a real pseudo-terminal (POSIX).

    Spawn a command, send vim-notation keystrokes, read the rendered screen, and
    wait on screen conditions. Construction raises on an unsupported platform;
    check `pty_supported()` first (Windows ConPTY support is a follow-up).
    """

    command: list[str]
    env: dict[str, str] | None = None
    cwd: str | None = None
    _pid: int = field(default=0, init=False)
    _fd: int = field(default=-1, init=False)
    _out: TerminalOutput = field(default_factory=TerminalOutput, init=False)
    _last_change: float = field(default=0.0, init=False)
    _last_len: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        """Validate the platform (fail closed on Windows)."""
        if not pty_supported():
            msg = "PtySession requires a POSIX PTY; Windows ConPTY support is not implemented yet"
            raise RuntimeError(msg)

    def start(self) -> None:
        """Fork the child into a new PTY and force a truecolor xterm."""
        import pty as _pty  # POSIX-only stdlib module; imported lazily

        env = dict(os.environ if self.env is None else self.env)
        env.setdefault("TERM", "xterm-256color")
        env.setdefault("COLORTERM", "truecolor")
        pid, fd = _pty.fork()
        if pid == 0:  # child
            if self.cwd:
                os.chdir(self.cwd)
            os.execvpe(self.command[0], self.command, env)  # noqa: S606 - intentional PTY child exec
        self._pid = pid
        self._fd = fd
        self._last_change = time.monotonic()

    def _drain(self) -> None:
        """Read whatever is currently available from the PTY (non-blocking)."""
        import select

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
            self._out.feed(data)
        length = len(self._out.text)
        if length != self._last_len:
            self._last_len = length
            self._last_change = time.monotonic()

    def send(self, notation: str) -> None:
        """Encode `notation` and write it to the PTY."""
        os.write(self._fd, encode_keys(notation))

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
        if self._fd >= 0:
            with contextlib.suppress(OSError):
                os.close(self._fd)
            self._fd = -1
        if self._pid > 0:
            with contextlib.suppress(OSError):
                os.waitpid(self._pid, os.WNOHANG)


__all__ = [
    "KeyEncodeError",
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
