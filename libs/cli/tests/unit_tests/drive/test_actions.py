"""Unit tests for the drive-script action parser."""

from __future__ import annotations

from pathlib import Path

import pytest

from bog_agents_cli.drive.actions import (
    AssertWidget,
    ExpectModal,
    ExpectTranscript,
    Press,
    ScriptLoadError,
    SelectOption,
    Shell,
    Slash,
    Snapshot,
    Submit,
    SwitchModel,
    Type,
    WaitForIdle,
    load_script,
    parse_script,
)


class TestStringShorthands:
    def test_slash_string_becomes_slash_action(self):
        script = parse_script({"steps": ["/help"]})
        assert script.steps == [Slash(command="/help")]

    def test_bang_string_becomes_shell_action(self):
        script = parse_script({"steps": ["!ls -la"]})
        assert script.steps == [Shell(command="ls -la")]

    def test_submit_keyword_becomes_bare_submit(self):
        script = parse_script({"steps": ["submit"]})
        assert script.steps == [Submit()]

    def test_unknown_bare_string_errors(self):
        with pytest.raises(ScriptLoadError):
            parse_script({"steps": ["just some text"]})


class TestActionBuilders:
    def test_type_with_scalar(self):
        script = parse_script({"steps": [{"type": "hello"}]})
        assert script.steps == [Type(text="hello")]

    def test_type_with_dict_sets_slow(self):
        script = parse_script({"steps": [{"type": {"text": "h", "slow": True}}]})
        assert script.steps == [Type(text="h", slow=True)]

    def test_press_with_list(self):
        script = parse_script({"steps": [{"press": ["ctrl+c", "escape"]}]})
        assert script.steps == [Press(keys=("ctrl+c", "escape"))]

    def test_press_with_scalar(self):
        script = parse_script({"steps": [{"press": "tab"}]})
        assert script.steps == [Press(keys=("tab",))]

    def test_wait_for_idle_default(self):
        script = parse_script({"steps": [{"wait_for_idle": None}]})
        assert script.steps == [WaitForIdle()]

    def test_wait_for_idle_with_seconds(self):
        script = parse_script({"steps": [{"wait_for_idle": 12.5}]})
        assert script.steps == [WaitForIdle(timeout_seconds=12.5)]

    def test_expect_transcript_string(self):
        script = parse_script({"steps": [{"expect_transcript_contains": "hi"}]})
        assert script.steps == [ExpectTranscript(pattern="hi")]

    def test_expect_modal_string(self):
        script = parse_script({"steps": [{"expect_modal": "ModelSelector"}]})
        assert script.steps == [ExpectModal(name="ModelSelector")]

    def test_select_option_by_label(self):
        script = parse_script({"steps": [{"select_option": "Anthropic"}]})
        assert script.steps == [SelectOption(label="Anthropic")]

    def test_select_option_by_index(self):
        script = parse_script({"steps": [{"select_option": 2}]})
        assert script.steps == [SelectOption(index=2)]

    def test_snapshot(self):
        script = parse_script({"steps": [{"snapshot": "shot-1"}]})
        assert script.steps == [Snapshot(path="shot-1")]

    def test_assert_widget(self):
        script = parse_script(
            {"steps": [{"assert_widget": {"selector": "#bar", "text_matches": "x"}}]}
        )
        assert script.steps == [AssertWidget(selector="#bar", text_matches="x")]

    def test_switch_model(self):
        script = parse_script({"steps": [{"switch_model": "anthropic:foo"}]})
        assert script.steps == [SwitchModel(model="anthropic:foo")]


class TestSessionBlock:
    def test_default_session(self):
        script = parse_script({"steps": []})
        assert script.session.model.startswith("fake:")
        assert script.session.approval_mode == "explicit"
        assert script.session.no_mcp is True

    def test_explicit_session(self):
        script = parse_script(
            {
                "session": {
                    "model": "anthropic:claude-opus-4-7",
                    "approval_mode": "auto-reads",
                    "vars": {"name": "world"},
                },
                "steps": [],
            }
        )
        assert script.session.model == "anthropic:claude-opus-4-7"
        assert script.session.approval_mode == "auto-reads"
        assert script.session.vars == {"name": "world"}

    def test_invalid_approval_mode_rejected(self):
        with pytest.raises(ScriptLoadError):
            parse_script({"session": {"approval_mode": "yolo"}, "steps": []})


class TestLoadScript:
    def test_load_smoke_script(self):
        script_dir = Path(__file__).parent / "scripts"
        script = load_script(script_dir / "help_modal.yaml")
        assert script.source_path is not None
        assert len(script.steps) >= 3
        assert isinstance(script.steps[0], Slash)

    def test_unknown_action_errors(self):
        with pytest.raises(ScriptLoadError, match="unknown action"):
            parse_script({"steps": [{"fly_to_mars": True}]})

    def test_top_level_must_be_mapping(self, tmp_path):
        p = tmp_path / "bad.yaml"
        p.write_text("- not a mapping\n", encoding="utf-8")
        with pytest.raises(ScriptLoadError):
            load_script(p)
