"""Unit tests for the vim-mode editing engine.

The engine is a pure state machine: feed it key names plus the current buffer
text and cursor offset, and apply the returned commands to a simulated buffer.
Textual's `ChatTextArea` integration is tested separately in `test_chat_input.py`.
"""

from __future__ import annotations

import pytest

from bog_agents_cli.widgets.vim_mode import (
    VimCommand,
    VimEngine,
    VimMove,
    VimRedo,
    VimReplace,
    VimSetMode,
    VimUndo,
    VimYank,
    find_char,
    line_start,
    matching_pair,
    move_lines,
    text_object_range,
    word_back,
    word_end,
    word_forward,
)


class Buffer:
    """Simulated document that applies engine commands to a plain string."""

    def __init__(self, text: str = "", offset: int = 0) -> None:
        """Initialize the buffer with text and a cursor offset."""
        self.text = text
        self.offset = offset
        self.engine = VimEngine()
        self._insert_anchor = 0
        self._insert_before = ""

    def key(self, key: str) -> None:
        """Feed one key into the engine and apply the resulting commands."""
        for command in self.engine.process_key(self.text, self.offset, key):
            self._apply(command)

    def type_insert(self, text: str, *, escape: bool = True) -> None:
        """Simulate typing `text` in insert mode, then optionally escape."""
        self.text = self.text[: self.offset] + text + self.text[self.offset :]
        self.offset += len(text)
        if escape:
            self.escape_insert()

    def escape_insert(self) -> None:
        """Record the inserted text and leave insert mode (mirrors ChatTextArea)."""
        inserted = ""
        cursor_off = self.offset
        if (
            cursor_off >= self._insert_anchor
            and self.text[: self._insert_anchor]
            == self._insert_before[: self._insert_anchor]
            and self.text[cursor_off:] == self._insert_before[self._insert_anchor :]
        ):
            inserted = self.text[self._insert_anchor : cursor_off]
        elif self.text.startswith(self._insert_before):
            inserted = self.text[len(self._insert_before) :]
        self.engine.record_insert(inserted)
        for command in self.engine.exit_insert_mode(self.offset):
            self._apply(command)

    def _apply(self, command: VimCommand) -> None:
        """Apply a single command to the simulated buffer."""
        if isinstance(command, VimMove):
            self.offset = max(0, min(command.offset, len(self.text)))
        elif isinstance(command, VimReplace):
            self.text = (
                self.text[: command.start] + command.text + self.text[command.end :]
            )
            if command.cursor is not None:
                self.offset = max(0, min(command.cursor, len(self.text)))
        elif isinstance(command, VimYank):
            self.engine.yank(self.text[command.start : command.end], command.linewise)
        elif isinstance(command, VimSetMode):
            self.engine.mode = command.mode
            if command.mode == "insert":
                self._insert_anchor = self.offset
                self._insert_before = self.text
        # VimUndo / VimRedo are handled by the host document's undo stack.


class TestHelperFunctions:
    """Tests for the exported pure motion helpers."""

    def test_word_forward(self) -> None:
        """`w` should land on the start of the next word."""
        text = "foo bar baz"
        assert word_forward(text, 0) == 4
        assert word_forward(text, 4) == 8
        assert word_forward(text, 0, 2) == 8
        assert word_forward(text, 8) == 11  # beyond the last word clamps to end

    def test_word_forward_big(self) -> None:
        """`W` should treat punctuation as part of words."""
        assert word_forward("one.two three", 0, big=True) == 8
        # plain `w` treats the period as its own word
        assert word_forward("one.two three", 0) == 3

    def test_word_back(self) -> None:
        """`b` should land on the start of the previous word."""
        text = "foo bar baz"
        assert word_back(text, 4) == 0
        assert word_back(text, 8) == 4
        assert word_back(text, 0) == 0

    def test_word_end(self) -> None:
        """`e` should land on word ends, advancing from a word end."""
        text = "foo bar baz"
        assert word_end(text, 0) == 2
        assert word_end(text, 2) == 6
        assert word_end(text, 4) == 6
        assert word_end(text, 0, 2) == 6

    def test_line_helpers(self) -> None:
        """Line start/end helpers should respect newlines."""
        text = "abc\ndef"
        assert line_start(text, 4) == 4
        assert line_start(text, 0) == 0
        assert word_forward(text, 0, 1) != 0

    def test_move_lines(self) -> None:
        """Vertical movement preserves column within line bounds."""
        text = "abcdef\nxyz\nq"
        assert move_lines(text, 2, 1) == 9
        assert move_lines(text, 0, 1) == 7
        assert move_lines(text, 9, -1) == 2
        assert move_lines(text, 2, 2) == 12  # clamps to last row/col
        assert move_lines(text, 0, 100) == 11

    def test_find_char(self) -> None:
        """`f`/`F`/`t`/`T` target computation."""
        text = "hello world"
        assert find_char(text, 0, "o") == 4
        assert find_char(text, 0, "o", 2) == 7
        assert find_char(text, 5, "o", backwards=True) == 4
        assert find_char(text, 0, "o", before=True) == 3
        assert find_char(text, 0, "z") is None

    def test_matching_pair(self) -> None:
        """`%` should jump to the matching bracket, ignoring nesting."""
        text = "(a[b]c)"
        assert matching_pair(text, 0) == 6
        assert matching_pair(text, 6) == 0
        assert matching_pair(text, 2) == 4
        assert matching_pair(text, 4) == 2
        assert matching_pair(text, 1) is None

    def test_matching_pair_handles_cursor_on_bracket(self) -> None:
        """Cursor directly on a bracket should still match the other one."""
        assert matching_pair("()", 1) == 0
        assert matching_pair("()", 0) == 1

    def test_text_object_range_word(self) -> None:
        """Word text objects include/exclude surrounding whitespace."""
        text = "one two three"
        assert text_object_range(text, 4, inner=True, target="w") == (4, 7)
        assert text_object_range(text, 4, inner=False, target="w") == (4, 8)

    def test_text_object_range_on_whitespace(self) -> None:
        """A cursor on whitespace should pick the enclosing word."""
        text = "one   two"
        assert text_object_range(text, 4, inner=True, target="w") == (3, 6)
        assert text_object_range(text, 4, inner=False, target="w") == (3, 9)

    def test_text_object_range_brackets(self) -> None:
        """Bracket objects work for both inner and around variants."""
        text = "call(foo)"
        assert text_object_range(text, 6, inner=True, target="(") == (5, 8)
        assert text_object_range(text, 6, inner=False, target="(") == (4, 9)
        assert text_object_range(text, 6, inner=True, target=")") == (5, 8)

    def test_text_object_range_quotes(self) -> None:
        """Quote objects respect escaped quotes."""
        text = 'say "hi there"'
        assert text_object_range(text, 6, inner=True, target='"') == (5, 13)
        assert text_object_range(text, 6, inner=False, target='"') == (4, 14)
        escaped = r'a "b\"c" d'
        assert text_object_range(escaped, 5, inner=True, target='"') == (3, 7)


class TestMotions:
    """Tests for plain (non-operator) cursor motions."""

    @pytest.mark.parametrize(
        ("keys", "start", "expected"),
        [
            ("h", 3, 2),
            ("l", 3, 4),
            ("0", 5, 0),
            ("$", 0, 11),
            ("^", 5, 0),
            ("w", 0, 4),
            ("b", 4, 0),
            ("e", 0, 2),
            ("gg", 5, 0),
            ("G", 0, 11),
            ("2w", 0, 8),
            ("2e", 0, 6),
            ("0l", 5, 1),
        ],
    )
    def test_motion_targets(self, keys: str, start: int, expected: int) -> None:
        """Individual motions should land on the expected offsets."""
        buf = Buffer("foo bar baz", offset=start)
        for key in keys:
            buf.key(key)
        assert buf.offset == expected

    def test_jk_preserve_column(self) -> None:
        """`j`/`k` move between lines preserving the column."""
        buf = Buffer("abcdef\nxyz", offset=2)
        buf.key("j")
        assert buf.offset == 9
        buf.key("k")
        assert buf.offset == 2

    def test_find_motion(self) -> None:
        """`f`/`t`/`F`/`T` and `;`/`,` navigate by character."""
        buf = Buffer("x y x y x", offset=0)
        buf.key("f")
        buf.key("x")
        assert buf.offset == 4
        buf.key(";")
        assert buf.offset == 8
        buf.key(",")
        assert buf.offset == 4
        buf.offset = 0
        buf.key("t")
        buf.key("x")
        assert buf.offset == 3
        buf.offset = 8
        buf.key("F")
        buf.key("x")
        assert buf.offset == 4
        buf.key("T")
        buf.key("x")
        assert buf.offset == 1

    def test_percent_jump(self) -> None:
        """`%` jumps between matching brackets."""
        buf = Buffer("(one [two] three)", offset=0)
        buf.key("%")
        assert buf.offset == 16
        buf.key("%")
        assert buf.offset == 0

    def test_paragraph_motions(self) -> None:
        """`{`/`}` jump between blank-line paragraph boundaries."""
        buf = Buffer("alpha\n\nbeta\ngamma", offset=0)
        buf.key("}")
        assert buf.offset == 6
        buf.key("{")
        assert buf.offset == 0

    def test_h_motion_ignored_for_count_zero(self) -> None:
        """Motion count of zero is a no-op (matching vim)."""
        buf = Buffer("hello", offset=2)
        buf.key("0")
        buf.key("2")
        buf.key("h")
        assert buf.offset == 0


class TestOperatorMotions:
    """Tests for `d`/`c`/`y` combined with motions."""

    def test_dw(self) -> None:
        """`dw` deletes the word plus its trailing whitespace."""
        buf = Buffer("foo bar", offset=0)
        buf.key("d")
        buf.key("w")
        assert buf.text == "bar"
        assert buf.offset == 0

    def test_dd(self) -> None:
        """`dd` deletes the whole line including the newline."""
        buf = Buffer("one\ntwo\nthree", offset=4)
        buf.key("d")
        buf.key("d")
        assert buf.text == "one\nthree"
        assert buf.offset == 4

    def test_dd_last_line(self) -> None:
        """`dd` on the last line removes it and collapses the buffer."""
        buf = Buffer("one\ntwo\nthree", offset=9)
        buf.key("d")
        buf.key("d")
        assert buf.text == "one\ntwo\n"
        assert buf.offset == 8

    def test_dd_count(self) -> None:
        """`2dd` deletes two lines."""
        buf = Buffer("one\ntwo\nthree", offset=0)
        buf.key("2")
        buf.key("d")
        buf.key("d")
        assert buf.text == "three"

    def test_capital_d(self) -> None:
        """`D` deletes to end of line."""
        buf = Buffer("hello world", offset=0)
        buf.key("D")
        assert buf.text == ""

    def test_cw(self) -> None:
        """`cw` replaces the word but keeps trailing whitespace (vim `ce`)."""
        buf = Buffer("foo bar", offset=0)
        buf.key("c")
        buf.key("w")
        assert buf.text == " bar"
        assert buf.engine.mode == "insert"
        buf.type_insert("baz")
        assert buf.text == "baz bar"

    def test_cc(self) -> None:
        """`cc` replaces the whole line and enters insert mode."""
        buf = Buffer("one\ntwo\nthree", offset=4)
        buf.key("c")
        buf.key("c")
        assert buf.text == "one\nthree"
        assert buf.engine.mode == "insert"

    def test_yw_then_paste(self) -> None:
        """`yw` yanks a word (with trailing space) and `p` pastes it."""
        buf = Buffer("one two three", offset=4)
        buf.key("y")
        buf.key("w")
        assert buf.engine.yank_register == "two "
        buf.offset = 0
        buf.key("p")
        assert buf.text == "otwo ne two three"

    def test_yy_then_paste(self) -> None:
        """`yy` yanks linewise and `p`/`P` paste on their own lines."""
        buf = Buffer("one\ntwo\nthree", offset=0)
        buf.key("y")
        buf.key("y")
        assert buf.engine.yank_register == "one\n"
        assert buf.engine.yank_linewise is True
        buf.key("p")
        assert buf.text == "one\none\ntwo\nthree"
        buf.offset = 0
        buf.key("P")
        assert buf.text == "one\none\none\ntwo\nthree"

    def test_yy_last_line_register(self) -> None:
        """Yanking the last line still stores a trailing newline."""
        buf = Buffer("one\ntwo\nthree", offset=9)
        buf.key("y")
        buf.key("y")
        assert buf.engine.yank_register == "three\n"

    def test_yank_motion_does_not_edit(self) -> None:
        """`yw` should not modify the buffer."""
        buf = Buffer("foo bar", offset=0)
        buf.key("y")
        buf.key("w")
        assert buf.text == "foo bar"


class TestTextObjects:
    """Tests for `i`/`a` text-object operations."""

    def test_ciw(self) -> None:
        """`ciw` replaces the inner word and enters insert mode."""
        buf = Buffer("one two three", offset=4)
        buf.key("c")
        buf.key("i")
        buf.key("w")
        assert buf.text == "one  three"
        assert buf.engine.mode == "insert"
        buf.type_insert("X")
        assert buf.text == "one X three"

    def test_diw(self) -> None:
        """`diw` deletes the inner word only."""
        buf = Buffer("one two three", offset=4)
        buf.key("d")
        buf.key("i")
        buf.key("w")
        assert buf.text == "one  three"

    def test_caw(self) -> None:
        """`caw` replaces the word including adjacent whitespace."""
        buf = Buffer("one two three", offset=4)
        buf.key("c")
        buf.key("a")
        buf.key("w")
        assert buf.text == "one three"
        assert buf.engine.mode == "insert"

    def test_di_brackets(self) -> None:
        """`di(` deletes the parenthesized contents."""
        buf = Buffer("call(foo)", offset=6)
        buf.key("d")
        buf.key("i")
        buf.key("(")
        assert buf.text == "call()"

    def test_da_brackets(self) -> None:
        """`da(` deletes the brackets and their contents."""
        buf = Buffer("call(foo)", offset=6)
        buf.key("d")
        buf.key("a")
        buf.key("(")
        assert buf.text == "call"

    def test_ci_quote(self) -> None:
        """`ci"` replaces the quoted string contents."""
        buf = Buffer('say "hi there"', offset=6)
        buf.key("c")
        buf.key("i")
        buf.key('"')
        assert buf.text == 'say ""'
        assert buf.engine.mode == "insert"

    def test_ci_escaped_quote(self) -> None:
        """Escaped quotes do not terminate the object."""
        buf = Buffer(r'a "b\"c" d', offset=5)
        buf.key("c")
        buf.key("i")
        buf.key('"')
        assert buf.text == r'a "" d'


class TestInsertModeEntries:
    """Tests for the various ways to enter insert mode."""

    def test_i_and_escape(self) -> None:
        """`i` inserts before the cursor; esc returns to normal mode."""
        buf = Buffer("hello", offset=2)
        buf.key("i")
        assert buf.engine.mode == "insert"
        buf.type_insert("X")
        assert buf.text == "heXllo"
        assert buf.engine.mode == "normal"

    def test_a_appends_after_cursor(self) -> None:
        """`a` inserts after the cursor character."""
        buf = Buffer("hello", offset=2)
        buf.key("a")
        buf.type_insert("X")
        assert buf.text == "helXlo"

    def test_capital_i_and_a(self) -> None:
        """`I`/`A` insert at the start/end of the line."""
        buf = Buffer("hello world", offset=5)
        buf.key("A")
        buf.type_insert("!")
        assert buf.text == "hello world!"
        buf.key("I")
        buf.type_insert(">")
        assert buf.text == ">hello world!"

    def test_o_opens_line_below(self) -> None:
        """`o` opens a new line below the cursor."""
        buf = Buffer("one\ntwo", offset=0)
        buf.key("o")
        assert buf.text == "one\n\ntwo"
        assert buf.engine.mode == "insert"

    def test_capital_o_opens_line_above(self) -> None:
        """`O` opens a new line above the cursor."""
        buf = Buffer("one\ntwo", offset=4)
        buf.key("O")
        assert buf.text == "one\n\ntwo"
        assert buf.engine.mode == "insert"

    def test_s_substitutes_char(self) -> None:
        """`s` deletes one char and enters insert mode."""
        buf = Buffer("hello", offset=0)
        buf.key("s")
        assert buf.text == "ello"
        assert buf.engine.mode == "insert"

    def test_capital_s_substitutes_line(self) -> None:
        """`S` deletes the line and enters insert mode."""
        buf = Buffer("one\ntwo", offset=0)
        buf.key("S")
        assert buf.text == "two"
        assert buf.engine.mode == "insert"

    def test_escape_at_start_keeps_offset(self) -> None:
        """Escaping insert mode at offset 0 does not move the cursor left."""
        buf = Buffer("hi", offset=0)
        buf.key("i")
        buf.type_insert("X", escape=False)
        buf.escape_insert()
        assert buf.text == "Xhi"
        assert buf.offset == 0


class TestCharEdits:
    """Tests for `x`, `X`, `r`, `~` and counts."""

    def test_x_deletes_char(self) -> None:
        """`x` deletes the character under the cursor."""
        buf = Buffer("hello", offset=1)
        buf.key("x")
        assert buf.text == "hllo"
        assert buf.offset == 1

    def test_2x_deletes_two(self) -> None:
        """`2x` deletes two characters."""
        buf = Buffer("hello", offset=0)
        buf.key("2")
        buf.key("x")
        assert buf.text == "llo"

    def test_capital_x_deletes_before(self) -> None:
        """`X` deletes the character before the cursor."""
        buf = Buffer("hello", offset=2)
        buf.key("X")
        assert buf.text == "hllo"
        assert buf.offset == 1

    def test_r_replaces_char(self) -> None:
        """`rX` replaces the character under the cursor."""
        buf = Buffer("hello", offset=1)
        buf.key("r")
        buf.key("X")
        assert buf.text == "hXllo"
        assert buf.offset == 2

    def test_toggle_case(self) -> None:
        """`~` toggles case of the character under the cursor."""
        buf = Buffer("Hello", offset=0)
        buf.key("~")
        assert buf.text == "hello"
        assert buf.offset == 1


class TestRepeat:
    """Tests for the `.` repeat command."""

    def test_repeat_x(self) -> None:
        """`.` repeats the last `x` delete at the new cursor."""
        buf = Buffer("hello", offset=0)
        buf.key("x")
        buf.key(".")
        assert buf.text == "llo"

    def test_repeat_dw(self) -> None:
        """`.` repeats the last `dw` delete."""
        buf = Buffer("one two three", offset=0)
        buf.key("d")
        buf.key("w")
        assert buf.text == "two three"
        buf.offset = 1
        buf.key(".")
        assert buf.text == "tthree"

    def test_repeat_insert(self) -> None:
        """`.` repeats the last insert-mode text at the cursor."""
        buf = Buffer("one", offset=0)
        buf.key("i")
        buf.type_insert("ab")
        buf.offset = 2
        buf.key(".")
        assert buf.text == "ababone"

    def test_repeat_replace_char(self) -> None:
        """`.` repeats the last `r` substitution."""
        buf = Buffer("hello", offset=0)
        buf.key("r")
        buf.key("X")
        buf.offset = 1
        buf.key(".")
        assert buf.text == "XXllo"

    def test_repeat_text_object(self) -> None:
        """`.` repeats the last text-object change at a new cursor."""
        buf = Buffer("one two three", offset=4)
        buf.key("d")
        buf.key("i")
        buf.key("w")
        assert buf.text == "one  three"
        buf.offset = 6  # inside 'three'
        buf.key(".")
        assert buf.text == "one  "


class TestUndoRedoSignals:
    """Tests that `u`/`ctrl+r` emit undo/redo commands."""

    def test_undo_emits_vim_undo(self) -> None:
        """`u` should emit a VimUndo command."""
        buf = Buffer("hello")
        commands = buf.engine.process_key(buf.text, buf.offset, "u")
        assert commands == [VimUndo()]

    def test_redo_emits_vim_redo(self) -> None:
        """`ctrl+r` should emit a VimRedo command."""
        buf = Buffer("hello")
        commands = buf.engine.process_key(buf.text, buf.offset, "ctrl+r")
        assert commands == [VimRedo()]

    def test_escape_clears_pending(self) -> None:
        """`escape` cancels a pending operator/count."""
        buf = Buffer("hello world")
        buf.key("d")
        buf.key("escape")
        buf.key("w")
        assert buf.text == "hello world"
        assert buf.offset == 6  # plain `w` motion still works


class TestEdgeCases:
    """Tests for boundary conditions."""

    def test_empty_buffer(self) -> None:
        """Operations on an empty buffer are safe no-ops."""
        buf = Buffer()
        buf.key("x")
        buf.key("w")
        buf.key("d")
        buf.key("d")
        buf.key("0")
        assert buf.text == ""

    def test_cursor_at_end_of_line(self) -> None:
        """`l` and `x` at the end of a line do not error."""
        buf = Buffer("hello", offset=5)
        buf.key("l")
        assert buf.offset == 5
        buf.key("x")
        assert buf.text == "hello"

    def test_operator_without_target_clears(self) -> None:
        """A lone operator followed by escape does not corrupt state."""
        buf = Buffer("hello")
        buf.key("c")
        assert buf.engine.mode == "normal"
        buf.key("escape")
        buf.key("i")
        assert buf.engine.mode == "insert"

    def test_waiting_key_cleared_on_escape(self) -> None:
        """`f` then escape leaves the cursor and buffer untouched."""
        buf = Buffer("hello world", offset=0)
        buf.key("f")
        buf.key("escape")
        assert buf.offset == 0
        buf.key("l")
        assert buf.offset == 1
