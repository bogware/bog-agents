"""Regression tests for incremental streamed tool-call arg accumulation.

v5 PERF-2: streamed tool-call args were re-`json.loads`'d over the whole
accumulated prefix on every chunk — O(n^2) for a large `write_file`, freezing
the UI. `_append_streamed_args` now scans each fragment once (O(len(fragment)))
to detect structural completeness, so a big streamed payload is linear.
"""

from __future__ import annotations

import json

from bog_agents_cli.textual_adapter import _append_streamed_args


def _feed(chunks: list[str]) -> dict:
    buffer: dict = {}
    for chunk in chunks:
        _append_streamed_args(buffer, chunk)
    return buffer


class TestStreamedArgsScan:
    def test_structure_completes_and_parses(self) -> None:
        buffer = _feed(['{"path": "a.py", ', '"content": "hello"}'])
        assert buffer.get("args_complete") is True
        assert json.loads(buffer["args"]) == {"path": "a.py", "content": "hello"}

    def test_not_complete_until_closing_brace(self) -> None:
        buffer = _feed(['{"path": "a.py"'])
        assert not buffer.get("args_complete")

    def test_braces_inside_strings_do_not_close_early(self) -> None:
        # A `}` inside a JSON string value must not be read as the closer.
        buffer = _feed(['{"content": "func() { return {}; }', '"}'])
        assert buffer.get("args_complete") is True
        assert json.loads(buffer["args"])["content"] == "func() { return {}; }"

    def test_escaped_quote_inside_string(self) -> None:
        buffer = _feed([r'{"content": "say \"hi\"', r'"}'])
        assert buffer.get("args_complete") is True
        assert json.loads(buffer["args"])["content"] == 'say "hi"'

    def test_large_payload_scans_incrementally(self) -> None:
        # Split a large string value across many fragments; the scanner must
        # only flag complete on the true closing brace, and the joined result
        # round-trips.
        # Distinct fragments (avoids the consecutive-duplicate dedup) summing to
        # a large JSON-valid string streamed across many chunks — no raw control
        # characters, since the model streams already-escaped content.
        body = " ".join(f"word{i:05d}" for i in range(4000))
        chunks = ['{"content": "']
        for i in range(0, len(body), 100):
            chunks.append(body[i : i + 100])
        chunks.append('"}')
        buffer = _feed(chunks)
        assert buffer.get("args_complete") is True
        assert json.loads(buffer["args"])["content"] == body

    def test_scalar_mode_exposes_each_fragment(self) -> None:
        # A non-structural (scalar) args stream is exposed as-is per fragment.
        buffer = _feed(['"just', ' a string"'])
        assert buffer["args"] == '"just a string"'

    def test_duplicate_consecutive_fragment_dropped(self) -> None:
        buffer = _feed(['{"a": 1}', '{"a": 1}'])
        # The resent duplicate is ignored; the value stays a single object.
        assert buffer["args"] == '{"a": 1}'
