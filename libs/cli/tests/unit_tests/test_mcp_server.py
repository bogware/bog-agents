"""Tests for the `bog-agents mcp-server` killer feature (#4).

Covers the pure logic: final-text extraction across content shapes and the
autonomous-mode guard. The full protocol round-trip is validated by a live MCP
client test (a real model call), not here.
"""

from __future__ import annotations

import pytest

from bog_agents_cli.mcp_server import _extract_final_text, run_mcp_server


class _Msg:
    def __init__(self, content: object) -> None:
        self.content = content


class TestExtractFinalText:
    def test_plain_string_content(self) -> None:
        result = {"messages": [_Msg("hello"), _Msg("final answer")]}
        assert _extract_final_text(result) == "final answer"

    def test_anthropic_block_list_content(self) -> None:
        result = {
            "messages": [
                _Msg(
                    [
                        {"type": "text", "text": "part one"},
                        {"type": "text", "text": "part two"},
                    ]
                )
            ]
        }
        assert _extract_final_text(result) == "part one\npart two"

    def test_list_with_plain_strings(self) -> None:
        result = {"messages": [_Msg(["a", "b"])]}
        assert _extract_final_text(result) == "a\nb"

    def test_no_messages(self) -> None:
        assert "no messages" in _extract_final_text({"messages": []})
        assert "no messages" in _extract_final_text({})


class TestAutonomousModeGuard:
    @pytest.mark.parametrize("mode", ["default", "plan", "paranoid", "nonsense"])
    async def test_non_autonomous_mode_rejected(self, mode: str) -> None:
        """MCP has no human approver, so non-autonomous modes exit 2 before
        any agent is built (no model/API key required).
        """
        rc = await run_mcp_server(permission_mode=mode)
        assert rc == 2

    @pytest.mark.parametrize("mode", ["bypass", "acceptEdits", "accept-edits"])
    def test_autonomous_modes_recognized(self, mode: str) -> None:
        """The autonomous-mode allowlist accepts the documented modes."""
        from bog_agents_cli.mcp_server import _AUTONOMOUS_MODES

        assert mode in _AUTONOMOUS_MODES
