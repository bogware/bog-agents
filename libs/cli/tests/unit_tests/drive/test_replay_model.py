"""Tests for the drive-only chat-model shims."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from langchain_core.messages import HumanMessage

from bog_agents_cli.drive.replay_model import (
    DriveModelSpec,
    FakeChatModel,
    ReplayChatModel,
    is_drive_model_spec,
    parse_drive_model_spec,
    resolve_drive_model,
    write_replay_records,
)


class TestSpecParser:
    def test_recognises_fake(self):
        assert is_drive_model_spec("fake:hi")
        parsed = parse_drive_model_spec("fake:hi")
        assert parsed is not None
        assert parsed.kind == "fake"
        assert parsed.payload == "hi"

    def test_recognises_replay(self):
        parsed = parse_drive_model_spec("replay:fixtures/run1.jsonl")
        assert parsed is not None
        assert parsed.kind == "replay"
        assert parsed.payload == "fixtures/run1.jsonl"

    def test_real_provider_returns_none(self):
        assert not is_drive_model_spec("anthropic:claude-opus-4-7")
        assert parse_drive_model_spec("anthropic:claude-opus-4-7") is None


class TestFakeChatModel:
    def test_returns_fixed_response(self):
        model = FakeChatModel(response_text="hello drive")
        result = model._generate([HumanMessage(content="hi")])
        assert result.generations[0].message.content == "hello drive"


class TestReplayChatModel:
    def test_walks_turns_in_order(self, tmp_path: Path):
        fixture = tmp_path / "run.jsonl"
        write_replay_records(
            fixture,
            [
                {"response": "first"},
                {"response": "second"},
                {"response": "third"},
            ],
        )
        model = ReplayChatModel(fixture_path=str(fixture))
        outputs = [
            model._generate([HumanMessage(content="x")]).generations[0].message.content
            for _ in range(3)
        ]
        assert outputs == ["first", "second", "third"]

    def test_loops_on_exhaustion(self, tmp_path: Path, caplog):
        fixture = tmp_path / "run.jsonl"
        write_replay_records(fixture, [{"response": "only"}])
        model = ReplayChatModel(fixture_path=str(fixture))
        for _ in range(3):
            model._generate([HumanMessage(content="x")])
        assert "exhausted" in caplog.text.lower()

    def test_missing_fixture_raises(self, tmp_path: Path):
        model = ReplayChatModel(fixture_path=str(tmp_path / "nope.jsonl"))
        with pytest.raises(FileNotFoundError):
            model._generate([HumanMessage(content="x")])

    def test_invalid_json_raises(self, tmp_path: Path):
        fixture = tmp_path / "bad.jsonl"
        fixture.write_text("not json\n", encoding="utf-8")
        model = ReplayChatModel(fixture_path=str(fixture))
        with pytest.raises(ValueError, match="invalid JSON"):
            model._generate([HumanMessage(content="x")])


class TestResolveDriveModel:
    def test_resolves_fake(self):
        model = resolve_drive_model("fake:hi")
        assert isinstance(model, FakeChatModel)

    def test_resolves_replay(self, tmp_path: Path):
        fixture = tmp_path / "run.jsonl"
        write_replay_records(fixture, [{"response": "yo"}])
        model = resolve_drive_model(f"replay:{fixture}")
        assert isinstance(model, ReplayChatModel)

    def test_replay_without_path_errors(self):
        with pytest.raises(ValueError, match="fixture path"):
            resolve_drive_model("replay:")
