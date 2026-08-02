"""Vim-style modal editing for the chat input.

A pure state machine (no Textual imports) that turns normal-mode key presses
into cursor moves and text edits over a plain string buffer. `ChatTextArea`
applies the emitted commands to the real document so the widget's built-in
undo/redo stacks keep working unchanged.

The engine tracks the subset of vim that is useful in a chat input box:

- Normal / insert modes with `esc` to leave insert mode.
- Motions: `h` `l` `j` `k`, `w` `b` `e` (and `W` `B` `E`), `0` `^` `$`,
  `gg` `G`, `f` `F` `t` `T` with `;`/`,`, `{` `}`, `%`.
- Operators: `d` `c` `y` over motions, line operators `dd` `cc` `yy`,
  text objects (`iw` `aw` `i"` `a"` `i(` `a(` ...), `D` `C` `Y`.
- Character edits: `x` `X`, `r`, `~`, `s` `S`.
- Insert entry: `i` `a` `I` `A` `o` `O`, `u`/`ctrl+r` undo/redo, `.` repeat,
  and `p`/`P` paste from the yank register.

The engine never mutates the buffer itself; it returns a list of
`VimCommand` dataclasses describing the edits to apply.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

VimModeName = Literal["normal", "insert"]

_PAIR_OPEN_TO_CLOSE = {"(": ")", "[": "]", "{": "}"}
_PAIR_CLOSE_TO_OPEN = {value: key for key, value in _PAIR_OPEN_TO_CLOSE.items()}


def _is_word_char(ch: str) -> bool:
    """Return whether `ch` is a vim 'word' character (alnum or underscore)."""
    return ch.isalnum() or ch == "_"


@dataclass(frozen=True)
class VimMove:
    """Move the cursor to a character offset."""

    offset: int


@dataclass(frozen=True)
class VimReplace:
    """Replace the text range with `text` and move the cursor to `cursor`."""

    start: int
    end: int
    text: str
    cursor: int | None = None


@dataclass(frozen=True)
class VimYank:
    """Yank a text range into the yank register."""

    start: int
    end: int
    linewise: bool = False


@dataclass(frozen=True)
class VimUndo:
    """Undo the last edit."""


@dataclass(frozen=True)
class VimRedo:
    """Redo the last undone edit."""


@dataclass(frozen=True)
class VimSetMode:
    """Switch the editor between normal and insert mode."""

    mode: VimModeName


VimCommand = VimMove | VimReplace | VimYank | VimUndo | VimRedo | VimSetMode


@dataclass
class _Repeat:
    """Record of the last change, used to implement the `.` repeat."""

    kind: str
    op: str | None = None
    motion: str | None = None
    count: int = 1
    text: str = ""
    find_char: str | None = None
    find_backwards: bool = False
    find_before: bool = False


def _row_of(text: str, offset: int) -> int:
    """Return the zero-based row containing `offset`."""
    return text.count("\n", 0, offset)


def _start_of_row(text: str, row: int) -> int:
    """Return the offset of the first character of `row`."""
    index = 0
    for _ in range(row):
        index = text.find("\n", index) + 1
    return index


def _offset_at(text: str, row: int, col: int) -> int:
    """Return the buffer offset for a row/col position, clamped."""
    start = _start_of_row(text, row)
    return start + col


def _line_bounds(text: str, offset: int) -> tuple[int, int]:
    """Return (start, end) offsets of the line containing `offset`.

    `end` excludes the trailing newline.
    """
    start = text.rfind("\n", 0, offset) + 1
    end = text.find("\n", offset)
    if end == -1:
        end = len(text)
    return start, end


def line_start(text: str, offset: int) -> int:
    """Return the offset of the start of the current line."""
    return _line_bounds(text, offset)[0]


def line_end(text: str, offset: int) -> int:
    """Return the offset just past the last character of the current line."""
    return _line_bounds(text, offset)[1]


def line_first_nonblank(text: str, offset: int) -> int:
    """Return the offset of the first non-whitespace char on the line."""
    start, end = _line_bounds(text, offset)
    index = start
    while index < end and text[index] in " \t":
        index += 1
    return index


def line_range(
    text: str, offset: int, count: int, *, direction: int
) -> tuple[int, int]:
    """Return the inclusive line range for `d`/`c`/`y` line operators.

    The returned range includes the trailing newline so consecutive lines
    collapse correctly. `direction` is `1` for lines below the cursor and
    `-1` for lines above.

    Args:
        text: Buffer contents.
        offset: Cursor offset.
        count: Number of lines to cover (at least 1).
        direction: `1` to include lines below the cursor, `-1` above.

    Returns:
        `(start, end)` offsets, where `end` excludes a trailing newline only
        when the range reaches the end of the buffer.
    """
    row = _row_of(text, offset)
    lines = text.split("\n")
    last_row = len(lines) - 1
    if direction >= 0:
        start_row = row
        end_row = min(row + count - 1, last_row)
    else:
        start_row = max(0, row - count + 1)
        end_row = row
    start = _start_of_row(text, start_row)
    end = line_end(text, start)
    if end_row > start_row:
        end = _start_of_row(text, end_row) + len(lines[end_row])
    if end < len(text):
        end += 1  # include the trailing newline
    return start, end


def move_lines(text: str, offset: int, delta: int) -> int:
    """Move `offset` up/down by `delta` rows, preserving column."""
    row = _row_of(text, offset)
    col = offset - _start_of_row(text, row)
    lines = text.split("\n")
    target_row = max(0, min(row + delta, len(lines) - 1))
    return _offset_at(text, target_row, min(col, len(lines[target_row])))


def word_forward(text: str, offset: int, count: int = 1, *, big: bool = False) -> int:
    """Return the offset of the start of the `count`-th next word."""
    match = (lambda ch: not ch.isspace()) if big else _is_word_char
    length = len(text)
    off = offset
    for _ in range(count):
        if off >= length:
            break
        while off < length and match(text[off]):
            off += 1
        while off < length and text[off].isspace():
            off += 1
    return off


def word_back(text: str, offset: int, count: int = 1, *, big: bool = False) -> int:
    """Return the offset of the start of the `count`-th previous word."""
    match = (lambda ch: not ch.isspace()) if big else _is_word_char
    off = offset
    for _ in range(count):
        if off <= 0:
            return 0
        while off > 0 and text[off - 1].isspace():
            off -= 1
        while off > 0 and match(text[off - 1]):
            off -= 1
    return off


def _word_end_at_or_after(text: str, pos: int, match: Callable[[str], bool]) -> int:
    """Return the end of the word containing `pos`, or the next word after."""
    length = len(text)
    if pos >= length:
        return length - 1 if length else 0
    while pos < length and text[pos].isspace():
        pos += 1
    if pos >= length:
        return length - 1 if length else 0
    start = pos
    while pos < length and match(text[pos]):
        pos += 1
    if pos == start:
        pos += 1  # a single punctuation char counts as a word
    return pos - 1


def word_end(text: str, offset: int, count: int = 1, *, big: bool = False) -> int:
    """Return the offset of the last char of the `count`-th next word end.

    When the cursor is inside a word, the first `e` lands on that word's end;
    when it is already on a word end, it moves to the next word's end.
    """
    match = (lambda ch: not ch.isspace()) if big else _is_word_char
    if count <= 0:
        return offset
    if offset < len(text) and not text[offset].isspace():
        end = offset
        while end < len(text) and match(text[end]):
            end += 1
        if end == offset:
            end = offset + 1  # single punctuation char
        if end - 1 > offset:
            position = end - 1
        else:
            position = _word_end_at_or_after(text, end, match)
    else:
        position = _word_end_at_or_after(text, offset, match)
    for _ in range(count - 1):
        position = _word_end_at_or_after(text, position + 1, match)
    return position


def find_char(
    text: str,
    offset: int,
    char: str,
    count: int = 1,
    *,
    backwards: bool = False,
    before: bool = False,
) -> int | None:
    """Return the offset of the `count`-th occurrence of `char`.

    Args:
        text: Buffer contents.
        offset: Cursor offset (the char itself is not matched).
        char: Character to search for.
        count: Occurrence index (>= 1).
        backwards: Search toward the start of the buffer.
        before: Stop just before the match instead of on it (`t`/`T`).

    Returns:
        Match offset, or `None` when not found.
    """
    if backwards:
        index = offset - 1
        found: int | None = None
        while index >= 0 and count > 0:
            if text[index] == char:
                found = index
                count -= 1
            index -= 1
    else:
        index = offset + 1
        found = None
        while index < len(text) and count > 0:
            if text[index] == char:
                found = index
                count -= 1
            index += 1
    if found is None:
        return None
    if before:
        # `t` stops just before the match; `T` stops just after it.
        return found - 1 if not backwards else found + 1
    return found


def matching_pair(text: str, offset: int) -> int | None:
    """Return the offset of the bracket matching the one at `offset`."""
    if offset >= len(text):
        return None
    char = text[offset]
    if char in _PAIR_CLOSE_TO_OPEN:
        open_char = _PAIR_CLOSE_TO_OPEN[char]
        depth = 0
        index = offset - 1
        while index >= 0:
            current = text[index]
            if current == open_char:
                if depth == 0:
                    return index
                depth -= 1
            elif current == char:
                depth += 1
            index -= 1
        return None
    if char in _PAIR_OPEN_TO_CLOSE:
        close_char = _PAIR_OPEN_TO_CLOSE[char]
        depth = 0
        index = offset + 1
        while index < len(text):
            current = text[index]
            if current == close_char:
                if depth == 0:
                    return index
                depth -= 1
            elif current == char:
                depth += 1
            index += 1
        return None
    return None


def paragraph_jump(text: str, offset: int, count: int, *, forward: bool) -> int:
    """Jump to the next/previous blank line boundary.

    Args:
        text: Buffer contents.
        offset: Cursor offset.
        count: Number of blank-line boundaries to skip.
        forward: Jump downward (`}`) or upward (`{`).

    Returns:
        Offset of the target line start, or the original offset when no
        boundary exists in that direction.
    """
    row = _row_of(text, offset)
    lines = text.split("\n")
    last_row = len(lines) - 1
    step = 1 if forward else -1
    target = row
    for _ in range(count):
        # A blank run is a paragraph boundary: skip it first so a cursor
        # sitting on a blank line moves to the far side of the run.
        if not lines[target].strip():
            probe = target + step
            while 0 <= probe <= last_row and not lines[probe].strip():
                probe += step
            if probe < 0 or probe > last_row:
                break
            target = probe
        boundary: int | None = None
        probe = target + step
        while 0 <= probe <= last_row:
            if not lines[probe].strip():
                boundary = probe
                break
            probe += step
        if boundary is None:
            break
        target = boundary
    return _offset_at(text, target, 0)


def _word_object_range(
    text: str, offset: int, *, inner: bool, big: bool
) -> tuple[int, int]:
    """Return the word text-object range around `offset`."""
    match = (lambda ch: not ch.isspace()) if big else _is_word_char
    if offset >= len(text):
        if not text:
            return 0, 0
        offset = len(text) - 1
    if text[offset].isspace():
        start = offset
        while start > 0 and text[start - 1].isspace():
            start -= 1
        end = offset
        while end < len(text) and text[end].isspace():
            end += 1
        if inner:
            return start, end
        word_end = end
        while word_end < len(text) and match(text[word_end]):
            word_end += 1
        if word_end > end:
            return start, word_end
        while start > 0 and match(text[start - 1]):
            start -= 1
        return start, end
    start = offset
    while start > 0 and match(text[start - 1]):
        start -= 1
    end = offset
    while end < len(text) and match(text[end]):
        end += 1
    if start == end:
        start = offset
        end = offset + 1
    if inner:
        return start, end
    if end < len(text) and text[end].isspace():
        while end < len(text) and text[end].isspace():
            end += 1
    elif start > 0 and text[start - 1].isspace():
        while start > 0 and text[start - 1].isspace():
            start -= 1
    return start, end


def _pair_object_range(
    text: str, offset: int, *, inner: bool, target: str
) -> tuple[int, int] | None:
    """Return the bracket text-object range enclosing `offset`."""
    if target in _PAIR_CLOSE_TO_OPEN:
        open_char = _PAIR_CLOSE_TO_OPEN[target]
        close_char = target
    else:
        open_char = target
        close_char = _PAIR_OPEN_TO_CLOSE[target]

    depth = 0
    open_index: int | None = None
    if offset < len(text) and text[offset] == open_char:
        open_index = offset
    else:
        for index in range(offset - 1, -1, -1):
            current = text[index]
            if current == close_char:
                depth += 1
            elif current == open_char:
                if depth == 0:
                    open_index = index
                    break
                depth -= 1
    if open_index is None:
        return None

    depth = 0
    close_index: int | None = None
    for index in range(open_index, len(text)):
        current = text[index]
        if current == open_char:
            depth += 1
        elif current == close_char:
            depth -= 1
            if depth == 0:
                close_index = index
                break
    if close_index is None:
        return None

    if inner:
        return open_index + 1, close_index
    return open_index, close_index + 1


def _quote_object_range(
    text: str, offset: int, *, inner: bool, target: str
) -> tuple[int, int] | None:
    """Return the quote text-object range enclosing `offset`."""
    open_index: int | None = None
    if (
        offset < len(text)
        and text[offset] == target
        and (offset == 0 or text[offset - 1] != "\\")
    ):
        open_index = offset
    else:
        for index in range(offset, -1, -1):
            if text[index] == target and (index == 0 or text[index - 1] != "\\"):
                open_index = index
                break
    if open_index is None:
        return None
    close_index: int | None = None
    for index in range(open_index + 1, len(text)):
        if text[index] == target and text[index - 1] != "\\":
            close_index = index
            break
    if close_index is None:
        return None
    if inner:
        return open_index + 1, close_index
    return open_index, close_index + 1


def text_object_range(
    text: str, offset: int, *, inner: bool, target: str
) -> tuple[int, int] | None:
    """Return the text-object range for `target` around `offset`.

    Args:
        text: Buffer contents.
        offset: Cursor offset.
        inner: Use the `i` (inner) variant; otherwise the `a` variant.
        target: Object character (`w`/`W`, `"`/`'`/`` ` ``, or a bracket).

    Returns:
        `(start, end)` offsets, or `None` when the object is not found.
    """
    if target in {"w", "W"}:
        return _word_object_range(text, offset, inner=inner, big=(target == "W"))
    if target in {"(", ")", "[", "]", "{", "}"}:
        return _pair_object_range(text, offset, inner=inner, target=target)
    if target in {'"', "'", "`"}:
        return _quote_object_range(text, offset, inner=inner, target=target)
    return None


class VimEngine:
    """Modal editing state machine over a plain-text buffer.

    Feed single-character key names via `process_key` along with the current
    buffer contents and cursor offset; collect the returned commands and apply
    them to the real document.
    """

    def __init__(self) -> None:
        """Initialize the engine in normal mode with an empty register."""
        self.mode: VimModeName = "normal"
        self.yank_register = ""
        self.yank_linewise = False
        self._count_str = ""
        self._operator: str | None = None
        self._waiting: str | None = None
        self._last_find: tuple[str, bool, bool] | None = None
        self._last_change: _Repeat | None = None

    def reset(self) -> None:
        """Return to a clean normal-mode state."""
        self.mode = "normal"
        self._count_str = ""
        self._operator = None
        self._waiting = None

    def _clear_pending(self) -> None:
        """Drop buffered count digits, operator, and waiting state."""
        self._count_str = ""
        self._operator = None
        self._waiting = None

    def _count_int(self) -> int | None:
        """Return the buffered count as an int, or `None` when unset."""
        return int(self._count_str) if self._count_str else None

    def _record_repeat(
        self,
        kind: str,
        *,
        op: str | None = None,
        motion: str | None = None,
        count: int = 1,
        text: str = "",
        find_char: str | None = None,
        find_backwards: bool = False,
        find_before: bool = False,
    ) -> None:
        """Remember the last change so `.` can repeat it."""
        self._last_change = _Repeat(
            kind=kind,
            op=op,
            motion=motion,
            count=count,
            text=text,
            find_char=find_char,
            find_backwards=find_backwards,
            find_before=find_before,
        )

    def yank(self, text: str, linewise: bool = False) -> None:
        """Store yanked text in the register for later pasting.

        Linewise yanks always keep a trailing newline so they paste cleanly
        onto their own line, even when the source was the last line.
        """
        if linewise and text and not text.endswith("\n"):
            text += "\n"
        self.yank_register = text
        self.yank_linewise = linewise

    def record_insert(self, text: str) -> None:
        """Record text inserted during insert mode for `.` repetition."""
        if text:
            self._record_repeat("insert", text=text)

    def exit_insert_mode(self, offset: int) -> list[VimCommand]:
        """Leave insert mode, moving the cursor one character left."""
        self.mode = "normal"
        self._clear_pending()
        if offset > 0:
            return [VimMove(offset - 1)]
        return []

    def process_key(self, text: str, offset: int, key: str) -> list[VimCommand]:
        """Process one key while in normal mode.

        Args:
            text: Current buffer contents.
            offset: Cursor offset.
            key: Single character key name (e.g. `'w'`, `' '`, `'ctrl+r'`).

        Returns:
            Commands to apply; empty when the key is a no-op.
        """
        if self.mode != "normal":
            return []
        if key == "escape":
            self._clear_pending()
            return []
        if self._waiting is not None:
            return self._consume_waiting(text, offset, key)
        if self._operator is not None:
            return self._consume_operator(text, offset, key)

        if key.isdigit():
            if key == "0" and not self._count_str:
                return self._do_motion(text, offset, "0", 1)
            self._count_str += key
            return []

        if key in "dcy":
            self._operator = key
            return []
        if key == "D":
            return self._do_to_eol(text, offset, "d")
        if key == "C":
            return self._do_to_eol(text, offset, "c")
        if key == "Y":
            return self._do_yank_line(text, offset)
        if key == "x":
            return self._do_delete_chars(text, offset, forward=True)
        if key == "X":
            return self._do_delete_chars(text, offset, forward=False)
        if key in "fFtT":
            self._waiting = key
            return []
        if key == "r":
            self._waiting = "r"
            return []
        if key == "g":
            self._waiting = "g"
            return []
        if key in "pP":
            return self._do_paste(text, offset, after=(key == "p"))
        if key == "u":
            return [VimUndo()]
        if key == "ctrl+r":
            return [VimRedo()]
        if key == ".":
            return self._do_repeat(text, offset)
        if key == "~":
            return self._do_toggle_case(text, offset)
        if key in "iIaAoOsS":
            return self._enter_insert(text, offset, key)
        if key in ";,":
            return self._do_repeat_find(text, offset, key)
        return self._do_motion(text, offset, key, self._count_int() or 1)

    def _consume_waiting(self, text: str, offset: int, key: str) -> list[VimCommand]:
        """Resolve a two-key sequence (`f`+char, `r`+char, `g`+key, text object)."""
        waiting = self._waiting
        count = self._count_int() or 1
        op = self._operator
        self._clear_pending()

        if waiting == "r":
            if offset >= len(text) or key == "escape":
                return []
            self._record_repeat("replace_char", count=1, text=key)
            return [VimReplace(offset, offset + 1, key, cursor=offset + 1)]
        if waiting == "g":
            if key == "g":
                return [VimMove(0)]
            if key == "G":
                return [VimMove(len(text))]
            return []
        if waiting in "fFtT":
            backwards = waiting in "FT"
            before = waiting in "tT"
            return self._do_find(
                text, offset, key, count, backwards=backwards, before=before, op=op
            )
        if waiting.startswith("obj:"):
            inner = waiting == "obj:i"
            rng = text_object_range(text, offset, inner=inner, target=key)
            if rng is None:
                return []
            start, end = rng
            # Bake the inner/around variant into the motion key so `.`
            # replay picks the same range shape.
            motion = f"obj:{'i' if inner else 'a'}:{key}"
            return self._do_operator_range(offset, op or "d", count, motion, start, end)
        return []

    def _consume_operator(self, text: str, offset: int, key: str) -> list[VimCommand]:
        """Resolve the motion half of a pending `d`/`c`/`y` operator."""
        op = self._operator
        count = self._count_int() or 1
        if key == op:
            self._clear_pending()
            return self._do_lines(text, offset, op, count)
        if key == "D":
            self._clear_pending()
            return self._do_to_eol(text, offset, op)
        if key in "ia":
            self._waiting = f"obj:{key}"
            return []
        if key in "fFtT":
            self._waiting = key
            return []
        self._clear_pending()
        if key == "g":
            return []
        return self._do_operator_motion(
            text, offset, op, count, key, char=None, backwards=False, before=False
        )

    def _do_motion(
        self, text: str, offset: int, key: str, count: int
    ) -> list[VimCommand]:
        """Resolve a plain cursor motion, clearing the buffered count."""
        self._clear_pending()
        target = _motion_target(text, offset, key, count)
        if target is None:
            return []
        return [VimMove(target)]

    def _do_find(
        self,
        text: str,
        offset: int,
        char: str,
        count: int,
        *,
        backwards: bool,
        before: bool,
        op: str | None,
    ) -> list[VimCommand]:
        """Run an `f`/`F`/`t`/`T` motion, optionally with an operator."""
        target = find_char(
            text, offset, char, count, backwards=backwards, before=before
        )
        self._last_find = (char, backwards, before)
        if target is None:
            return []
        motion = (
            ("F" if backwards else "f") if not before else ("T" if backwards else "t")
        )
        if op is not None:
            if backwards:
                start, end = target, offset
            else:
                start, end = offset, target + (0 if before else 1)
            return self._do_operator_range(
                offset,
                op,
                count,
                motion,
                start,
                end,
                char=char,
                backwards=backwards,
                before=before,
            )
        return [VimMove(target)]

    def _do_repeat_find(self, text: str, offset: int, key: str) -> list[VimCommand]:
        """Repeat the last `f`/`t` motion (`;`/`,`)."""
        if self._last_find is None:
            return []
        char, backwards, before = self._last_find
        if key == ",":
            backwards = not backwards
        count = self._count_int() or 1
        return self._do_find(
            text, offset, char, count, backwards=backwards, before=before, op=None
        )

    def _do_operator_motion(
        self,
        text: str,
        offset: int,
        op: str,
        count: int,
        motion_key: str,
        *,
        char: str | None,
        backwards: bool,
        before: bool,
    ) -> list[VimCommand]:
        """Apply `op` over the range implied by `motion_key`."""
        if motion_key.startswith("obj:"):
            inner = motion_key.startswith("obj:i")
            target = motion_key.rsplit(":", 1)[-1]
            rng = text_object_range(text, offset, inner=inner, target=target)
            if rng is None:
                return []
            start, end = rng
            return self._do_operator_range(offset, op, count, motion_key, start, end)
        if motion_key in "fFtT":
            target = find_char(
                text, offset, char or "", count, backwards=backwards, before=before
            )
            if target is None:
                return []
            self._last_find = (char or "", backwards, before)
            if backwards:
                start, end = target, offset
            else:
                start, end = offset, target + (0 if before else 1)
        if motion_key in "wW":
            if op == "c":
                # `cw` deletes to the end of the word, keeping trailing
                # whitespace so the replacement reads as a swap-in word.
                target = word_end(text, offset, count, big=(motion_key == "W"))
                if target < offset:
                    return None
                start, end = offset, min(target + 1, len(text))
                return self._do_operator_range(
                    offset, op, count, motion_key, start, end
                )
            rng = _motion_range(text, offset, motion_key, count)
            if rng is None:
                return None
            start, end = rng
        else:
            rng = _motion_range(text, offset, motion_key, count)
            if rng is None:
                return []
            start, end = rng
        return self._do_operator_range(
            offset,
            op,
            count,
            motion_key,
            start,
            end,
            char=char,
            backwards=backwards,
            before=before,
        )

    def _do_operator_range(
        self,
        offset: int,
        op: str,
        count: int,
        motion_key: str,
        start: int,
        end: int,
        *,
        char: str | None = None,
        backwards: bool = False,
        before: bool = False,
    ) -> list[VimCommand]:
        """Apply `op` to the range `[start, end)`."""
        if end <= start:
            return []
        if op == "y":
            return [VimYank(start, end)]
        cursor = _operator_cursor_after(motion_key, offset, start)
        self._record_repeat(
            "motion",
            op=op,
            motion=motion_key,
            count=count,
            find_char=char,
            find_backwards=backwards,
            find_before=before,
        )
        replace = VimReplace(start, end, "", cursor=cursor)
        if op == "c":
            self.mode = "insert"
            return [replace, VimSetMode("insert")]
        return [replace]

    def _do_lines(
        self, text: str, offset: int, op: str, count: int
    ) -> list[VimCommand]:
        """Handle `dd`/`cc`/`yy` line operators."""
        start, end = line_range(text, offset, count, direction=1)
        if end <= start:
            return []
        if op == "y":
            return [VimYank(start, end, linewise=True)]
        self._record_repeat("lines", op=op, count=count)
        replace = VimReplace(start, end, "", cursor=start)
        if op == "c":
            self.mode = "insert"
            return [replace, VimSetMode("insert")]
        return [replace]

    def _do_to_eol(self, text: str, offset: int, op: str) -> list[VimCommand]:
        """Handle `D`/`C` (delete/change to end of line)."""
        count = self._count_int() or 1
        self._clear_pending()
        end = line_end(text, offset)
        if end <= offset:
            return []
        if op == "y":
            return [VimYank(offset, end)]
        self._record_repeat("to_eol", op=op, count=count)
        replace = VimReplace(offset, end, "", cursor=offset)
        if op == "c":
            self.mode = "insert"
            return [replace, VimSetMode("insert")]
        return [replace]

    def _do_yank_line(self, text: str, offset: int) -> list[VimCommand]:
        """Handle `Y` (yank whole line)."""
        self._clear_pending()
        start, end = line_range(text, offset, 1, direction=1)
        return [VimYank(start, end, linewise=True)]

    def _do_delete_chars(
        self, text: str, offset: int, *, forward: bool
    ) -> list[VimCommand]:
        """Handle `x` (delete under cursor) and `X` (delete before cursor)."""
        count = self._count_int() or 1
        self._clear_pending()
        if forward:
            end = min(offset + count, len(text))
            if end == offset:
                return []
            self._record_repeat("char", count=count)
            return [VimReplace(offset, end, "", cursor=offset)]
        start = max(0, offset - count)
        if start == offset:
            return []
        self._record_repeat("char_back", count=count)
        return [VimReplace(start, offset, "", cursor=start)]

    def _do_toggle_case(self, text: str, offset: int) -> list[VimCommand]:
        """Handle `~` (toggle case under cursor, honoring count)."""
        count = self._count_int() or 1
        self._clear_pending()
        end = min(offset + count, len(text))
        if end == offset:
            return []
        self._record_repeat("toggle_case", count=count)
        return [VimReplace(offset, end, text[offset:end].swapcase(), cursor=end)]

    def _do_paste(self, text: str, offset: int, *, after: bool) -> list[VimCommand]:
        """Handle `p`/`P` paste from the yank register."""
        self._clear_pending()
        if not self.yank_register:
            return []
        if self.yank_linewise:
            start = line_end(text, offset) + 1 if after else line_start(text, offset)
            return [VimReplace(start, start, self.yank_register, cursor=start)]
        if after:
            start = offset + 1
            return [
                VimReplace(
                    start,
                    start,
                    self.yank_register,
                    cursor=start + len(self.yank_register),
                )
            ]
        return [
            VimReplace(
                offset,
                offset,
                self.yank_register,
                cursor=offset + len(self.yank_register),
            )
        ]

    def _do_repeat(self, text: str, offset: int) -> list[VimCommand]:
        """Repeat the last change (`.`)."""
        rep = self._last_change
        if rep is None:
            return []
        self._clear_pending()
        if rep.kind == "insert":
            return [VimReplace(offset, offset, rep.text, cursor=offset + len(rep.text))]
        if rep.kind == "char":
            end = min(offset + rep.count, len(text))
            if end == offset:
                return []
            return [VimReplace(offset, end, "", cursor=offset)]
        if rep.kind == "char_back":
            start = max(0, offset - rep.count)
            return [VimReplace(start, offset, "", cursor=start)]
        if rep.kind == "replace_char":
            if offset >= len(text):
                return []
            return [VimReplace(offset, offset + 1, rep.text, cursor=offset + 1)]
        if rep.kind == "toggle_case":
            end = min(offset + rep.count, len(text))
            return [VimReplace(offset, end, text[offset:end].swapcase(), cursor=end)]
        if rep.kind == "to_eol":
            end = line_end(text, offset)
            if end <= offset:
                return []
            return [VimReplace(offset, end, "", cursor=offset)]
        if rep.kind == "lines":
            start, end = line_range(text, offset, rep.count, direction=1)
            return [VimReplace(start, end, "", cursor=start)]
        if rep.kind == "motion":
            return self._do_operator_motion(
                text,
                offset,
                rep.op or "d",
                rep.count,
                rep.motion or "",
                char=rep.find_char,
                backwards=rep.find_backwards,
                before=rep.find_before,
            )
        return []

    def _enter_insert(self, text: str, offset: int, key: str) -> list[VimCommand]:
        """Enter insert mode via `i`/`a`/`I`/`A`/`o`/`O`/`s`/`S`."""
        count = self._count_int() or 1
        self._clear_pending()
        self.mode = "insert"
        if key == "i":
            return [VimSetMode("insert")]
        if key == "a":
            if offset >= len(text):
                return [VimSetMode("insert")]
            return [VimMove(offset + 1), VimSetMode("insert")]
        if key == "I":
            return [VimMove(line_start(text, offset)), VimSetMode("insert")]
        if key == "A":
            return [VimMove(line_end(text, offset)), VimSetMode("insert")]
        if key == "o":
            end = line_end(text, offset)
            return [VimReplace(end, end, "\n", cursor=end + 1), VimSetMode("insert")]
        if key == "O":
            start = line_start(text, offset)
            return [VimReplace(start, start, "\n", cursor=start), VimSetMode("insert")]
        if key == "s":
            end = min(offset + count, len(text))
            return [VimReplace(offset, end, "", cursor=offset), VimSetMode("insert")]
        if key == "S":
            start, end = line_range(text, offset, count, direction=1)
            return [VimReplace(start, end, "", cursor=start), VimSetMode("insert")]
        return [VimSetMode("insert")]


def _motion_target(text: str, offset: int, key: str, count: int) -> int | None:
    """Return the destination offset for a plain cursor motion."""
    length = len(text)
    if key == "h":
        return max(0, offset - count)
    if key == "l":
        return min(length, offset + count)
    if key == "j":
        return move_lines(text, offset, count)
    if key == "k":
        return move_lines(text, offset, -count)
    if key == "w":
        return word_forward(text, offset, count)
    if key == "W":
        return word_forward(text, offset, count, big=True)
    if key == "b":
        return word_back(text, offset, count)
    if key == "B":
        return word_back(text, offset, count, big=True)
    if key == "e":
        return word_end(text, offset, count)
    if key == "E":
        return word_end(text, offset, count, big=True)
    if key == "0":
        return line_start(text, offset)
    if key == "^":
        return line_first_nonblank(text, offset)
    if key == "$":
        return line_end(text, offset)
    if key == "G":
        return length
    if key == "{":
        return paragraph_jump(text, offset, count, forward=False)
    if key == "}":
        return paragraph_jump(text, offset, count, forward=True)
    if key == "%":
        return matching_pair(text, offset)
    return None


def _motion_range(
    text: str, offset: int, key: str, count: int
) -> tuple[int, int] | None:
    """Return the `[start, end)` range implied by a motion for operators."""
    length = len(text)
    if key == "h":
        target = max(0, offset - count)
        return (target, offset) if target < offset else None
    if key == "l":
        target = min(length, offset + count)
        return (offset, target) if target > offset else None
    if key in "wW":
        target = word_forward(text, offset, count, big=(key == "W"))
        return (offset, target) if target > offset else None
    if key in "bB":
        target = word_back(text, offset, count, big=(key == "B"))
        return (target, offset) if target < offset else None
    if key in "eE":
        target = word_end(text, offset, count, big=(key == "E"))
        if target >= offset:
            return offset, min(target + 1, length)
        return None
    if key == "0":
        target = line_start(text, offset)
        return (target, offset) if target < offset else None
    if key == "^":
        target = line_first_nonblank(text, offset)
        return (target, offset) if target < offset else None
    if key == "$":
        target = line_end(text, offset)
        return (offset, target) if target > offset else None
    if key == "j":
        return line_range(text, offset, count, direction=1)
    if key == "k":
        return line_range(text, offset, count, direction=-1)
    if key == "G":
        return (offset, length) if offset < length else None
    if key == "%":
        target = matching_pair(text, offset)
        if target is None:
            return None
        return (offset, target + 1) if target >= offset else (target, offset)
    return None


def _operator_cursor_after(motion_key: str, offset: int, start: int) -> int:
    """Return where the cursor lands after an operator+motion edit."""
    if motion_key in {"$", "l", "e", "E"}:
        return offset
    return start


__all__ = [
    "VimCommand",
    "VimEngine",
    "VimMove",
    "VimRedo",
    "VimReplace",
    "VimSetMode",
    "VimUndo",
    "VimYank",
    "find_char",
    "line_end",
    "line_first_nonblank",
    "line_start",
    "matching_pair",
    "move_lines",
    "text_object_range",
    "word_back",
    "word_end",
    "word_forward",
]
