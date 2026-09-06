"""ROADMAP #75: the `ask_advisor` tool — bounded, counted, capped, priced through the hook."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    import pytest

from bog_agents_cli import advisor_tools as at


def test_ask_advisor_caps_counts_and_bounds() -> None:
    seen: list[tuple[str, str]] = []
    usage: list[tuple[int, int]] = []

    def _ask(system: str, prompt: str) -> tuple[str, int, int]:
        seen.append((system, prompt))
        return "Recommendation: do X.", 120, 30

    tools, meter = at.advisor_tools_bundle(
        ask=_ask,
        model_label="opus",
        max_questions=2,
        on_usage=lambda i, o: usage.append((i, o)),
    )
    tool = tools[0]
    assert tool.name == "ask_advisor" and "sparingly" in (tool.description or "")
    answer = tool.invoke({"question": "Should I use a queue?", "context": "x" * 20_000})
    assert (
        "Advisor (opus)" in answer
        and "1 question(s) left" in answer
        and "do X." in answer
    )
    assert (
        seen[0][0] == at.ADVISOR_SYSTEM
        and len(seen[0][1])
        <= len("Should I use a queue?\n\nContext:\n") + at.MAX_CONTEXT_CHARS
    )
    assert (meter.asked, meter.input_tokens, meter.output_tokens, meter.remaining) == (
        1,
        120,
        30,
        1,
    ) and usage == [(120, 30)]
    assert tool.invoke({"question": "  "}) == "Error: the question is empty."
    assert "0 question(s) left" in tool.invoke({"question": "second"})
    capped = tool.invoke({"question": "third"})
    assert (
        capped.startswith("Advisor cap reached")
        and meter.asked == 2
        and len(meter.history) == 2
    )


def test_ask_advisor_reports_provider_failure() -> None:
    def _boom(_s: str, _p: str) -> tuple[str, int, int]:
        msg = "offline"
        raise RuntimeError(msg)

    tools, meter = at.advisor_tools_bundle(ask=_boom)
    assert (
        "Advisor unavailable (offline)" in tools[0].invoke({"question": "q"})
        and meter.asked == 1
    )


def test_hard_tier_ask_and_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _Msg:
        content: ClassVar[list[dict[str, str]]] = [
            {"type": "text", "text": "Use "},
            {"type": "text", "text": "a queue."},
        ]
        usage_metadata: ClassVar[dict[str, int]] = {
            "input_tokens": 7,
            "output_tokens": 3,
        }

    class _Model:
        def __init__(self) -> None:
            self.calls: list[object] = []

        def invoke(self, messages: object) -> _Msg:
            self.calls.append(messages)
            return _Msg()

    model = _Model()
    ask, spec = at.hard_tier_ask(
        resolve_model=lambda _s: model, tier_model="anthropic:claude-opus-4-6"
    )
    assert spec == "anthropic:claude-opus-4-6" and ask("sys", "q") == (
        "Use a queue.",
        7,
        3,
    )
    assert model.calls[0] == [("system", "sys"), ("human", "q")]

    from bog_agents_cli import operator_mode

    monkeypatch.setattr(
        operator_mode, "operator_config_path", lambda: tmp_path / "missing.toml"
    )
    hard = at.hard_tier_model(active_model="anthropic:claude-haiku-4-5")
    assert hard is not None and hard != "anthropic:claude-haiku-4-5"
    assert at.hard_tier_model(active_model=hard) is None
