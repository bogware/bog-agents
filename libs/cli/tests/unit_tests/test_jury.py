"""Tests for ``bog_agents_cli.jury``."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import tomli_w

from bog_agents_cli.jury import (
    JurorVerdict,
    _consensus,
    _parse_juror_response,
    load_jury_model_specs,
    run_jury,
)


def test_parse_clean_json_verdict() -> None:
    raw = '{"verdict": "approve", "summary": "looks good", "issues": [], "score": 9}'
    v = _parse_juror_response("j1", raw)
    assert v.verdict == "approve"
    assert v.score == 9
    assert v.summary == "looks good"
    assert v.is_valid


def test_parse_extracts_json_from_chatty_response() -> None:
    raw = (
        "Sure! Here is my JSON:\n"
        '{"verdict":"request_changes","summary":"missing tests","issues":["no tests"],"score":4}\n'
        "Hope this helps."
    )
    v = _parse_juror_response("j2", raw)
    assert v.verdict == "request_changes"
    assert v.score == 4
    assert v.issues == ("no tests",)


def test_parse_invalid_json_marks_invalid() -> None:
    v = _parse_juror_response("j3", "not json at all")
    assert v.verdict == "invalid"
    assert not v.is_valid


def test_parse_clamps_score_to_0_10() -> None:
    raw = '{"verdict":"approve","summary":"","issues":[],"score":99}'
    v = _parse_juror_response("j4", raw)
    assert v.score == 10
    raw_neg = '{"verdict":"approve","summary":"","issues":[],"score":-3}'
    v = _parse_juror_response("j5", raw_neg)
    assert v.score == 0


def test_consensus_reject_wins_when_any_juror_rejects() -> None:
    verdicts = (
        JurorVerdict("a", "approve", "ok", (), 9),
        JurorVerdict("b", "approve", "ok", (), 8),
        JurorVerdict("c", "reject", "no way", (), 1),
    )
    assert _consensus(verdicts) == "reject"


def test_consensus_approve_when_majority_approves() -> None:
    verdicts = (
        JurorVerdict("a", "approve", "", (), 8),
        JurorVerdict("b", "approve", "", (), 7),
        JurorVerdict("c", "request_changes", "", (), 5),
    )
    assert _consensus(verdicts) == "approve"


def test_consensus_request_changes_otherwise() -> None:
    verdicts = (
        JurorVerdict("a", "request_changes", "", (), 5),
        JurorVerdict("b", "request_changes", "", (), 5),
    )
    assert _consensus(verdicts) == "request_changes"


def test_consensus_inconclusive_when_all_invalid() -> None:
    verdicts = (
        JurorVerdict("a", "invalid", "", (), 0),
        JurorVerdict("b", "invalid", "", (), 0),
    )
    assert _consensus(verdicts) == "inconclusive"


def test_load_jury_model_specs_returns_empty_on_missing(tmp_path: Path) -> None:
    cfg = tmp_path / "missing.toml"
    assert load_jury_model_specs(cfg) == []


def test_load_jury_model_specs_reads_list(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    with cfg.open("wb") as f:
        tomli_w.dump(
            {"jury": {"models": ["openai:gpt-5", "anthropic:claude-sonnet-4-6"]}}, f
        )
    assert load_jury_model_specs(cfg) == [
        "openai:gpt-5",
        "anthropic:claude-sonnet-4-6",
    ]


def test_load_jury_model_specs_ignores_non_list(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    with cfg.open("wb") as f:
        tomli_w.dump({"jury": {"models": "not a list"}}, f)
    assert load_jury_model_specs(cfg) == []


async def test_run_jury_runs_each_juror_in_parallel() -> None:
    def make_model(text: str) -> object:
        m = MagicMock()
        response = MagicMock()
        response.content = text
        m.ainvoke = AsyncMock(return_value=response)
        return m

    jurors = [
        ("a", make_model('{"verdict":"approve","summary":"ok","issues":[],"score":9}')),
        (
            "b",
            make_model('{"verdict":"reject","summary":"bad","issues":["x"],"score":2}'),
        ),
    ]
    report = await run_jury("--- a\n+++ b\n@@ -1 +1 @@\n-old\n+new\n", jurors)
    assert len(report.verdicts) == 2
    assert report.consensus == "reject"


async def test_run_jury_rejects_empty_diff() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        await run_jury("", [])


async def test_run_jury_rejects_empty_jurors() -> None:
    with pytest.raises(ValueError, match="at least one juror"):
        await run_jury("diff", [])


def test_jury_command_registered() -> None:
    from bog_agents_cli.commands import COMMAND_HANDLER_MAP

    assert "/jury" in COMMAND_HANDLER_MAP
    assert COMMAND_HANDLER_MAP["/jury"] == "_handle_jury_command"
