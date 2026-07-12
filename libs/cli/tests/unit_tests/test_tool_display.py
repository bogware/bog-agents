"""Tests for control-character sanitization in tool_display rendering."""

from __future__ import annotations

from bog_agents_cli.tool_display import (
    format_tool_display,
    format_tool_message_content,
)

# A red-foreground CSI colour code followed by an OSC 52 clipboard-write
# terminated by BEL — the classic "spoof output / exfiltrate data" payload.
_ANSI = "\x1b[31m"
_OSC52 = "\x1b]52;c;ZXZpbA==\x07"


def _has_control_sequences(text: str) -> bool:
    """Return whether text still contains ESC or non-whitespace control bytes."""
    return any(
        ch == "\x1b" or (ord(ch) < 0x20 and ch not in "\t\n\r") or ord(ch) == 0x7F
        for ch in text
    )


class TestFormatToolDisplaySanitizes:
    """format_tool_display neutralizes escape sequences in argument values."""

    def test_execute_command_neutralized(self) -> None:
        payload = f"{_ANSI}rm -rf /{_OSC52}"
        result = format_tool_display("execute", {"command": payload})
        assert "\x1b" not in result
        assert not _has_control_sequences(result)
        # The visible command text survives.
        assert "rm -rf /" in result

    def test_generic_fallback_neutralized(self) -> None:
        result = format_tool_display("unknown_tool", {"k": f"{_ANSI}v{_OSC52}"})
        assert not _has_control_sequences(result)
        assert "v" in result

    def test_file_path_neutralized(self) -> None:
        result = format_tool_display(
            "read_file", {"file_path": f"{_ANSI}secret.py{_OSC52}"}
        )
        assert not _has_control_sequences(result)
        assert "secret.py" in result

    def test_ls_path_neutralized(self) -> None:
        result = format_tool_display("ls", {"path": f"{_ANSI}mydir{_OSC52}"})
        assert not _has_control_sequences(result)
        assert "mydir" in result

    def test_ordinary_value_unchanged(self) -> None:
        result = format_tool_display("web_search", {"query": "how to code"})
        assert 'web_search("how to code")' in result


class TestFormatToolMessageContentSanitizes:
    """format_tool_message_content neutralizes escape sequences in results."""

    def test_string_result_neutralized(self) -> None:
        result = format_tool_message_content(f"{_ANSI}output{_OSC52}")
        assert not _has_control_sequences(result)
        assert "output" in result

    def test_list_result_neutralized(self) -> None:
        result = format_tool_message_content([f"{_ANSI}line one{_OSC52}", "line two"])
        assert not _has_control_sequences(result)
        assert "line one" in result
        assert "line two" in result
        # Layout preserved: parts still joined by a newline.
        assert "\n" in result

    def test_none_result(self) -> None:
        assert format_tool_message_content(None) == ""

    def test_legitimate_whitespace_preserved(self) -> None:
        result = format_tool_message_content("a\tb\nc\r")
        assert result == "a\tb\nc\r"
