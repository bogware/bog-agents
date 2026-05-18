"""Unit tests for the YAML record/replay subsystem."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from bog_agents_cli.replay import (
    ReplaySession,
    ReplayStep,
    SessionRecorder,
    find_replay_session,
    list_replay_sessions,
    load_replay_session,
    save_drive_script_for_session,
    save_replay_session,
    session_from_dict,
    session_to_dict,
    session_to_drive_yaml,
)


class TestRecorder:
    def test_records_user_message(self):
        r = SessionRecorder(name="t")
        r.start()
        r.record_user_message("hi")
        s = r.finalize()
        assert s.steps[0].kind == "user_message"
        assert s.steps[0].content == "hi"

    def test_does_not_record_when_stopped(self):
        r = SessionRecorder()
        r.record_user_message("ignored")  # not started
        r.start()
        r.record_user_message("kept")
        r.stop()
        r.record_user_message("ignored2")
        s = r.finalize()
        assert [step.content for step in s.steps] == ["kept"]

    def test_records_tool_call_with_args(self):
        r = SessionRecorder()
        r.start()
        r.record_tool_call("fetch", {"path": "/a/b"}, result="ok")
        s = r.finalize()
        assert s.steps[0].kind == "tool_call"
        assert s.steps[0].tool == "fetch"
        assert s.steps[0].args == {"path": "/a/b"}
        assert s.steps[0].result_pattern == "ok"

    def test_truncates_long_ai_messages(self):
        r = SessionRecorder()
        r.start()
        r.record_ai_message("x" * 5000)
        s = r.finalize()
        assert len(s.steps[0].content) == 2000

    def test_finalize_extracts_jira_ticket(self):
        r = SessionRecorder()
        r.start()
        r.record_user_message("Fetch JIRA-134 details please")
        s = r.finalize()
        assert "${jira_ticket}" in s.steps[0].content
        assert "jira_ticket" in s.vars_spec
        assert s.vars_spec["jira_ticket"]["default"] == "JIRA-134"

    def test_finalize_shares_var_across_steps(self):
        r = SessionRecorder()
        r.start()
        r.record_user_message("Look at JIRA-134")
        r.record_tool_call("jira__get", {"id": "JIRA-134"})
        s = r.finalize()
        assert s.steps[0].content == "Look at ${jira_ticket}"
        assert s.steps[1].args["id"] == "${jira_ticket}"
        # Only one var spec for both occurrences.
        ticket_keys = [k for k in s.vars_spec if k.startswith("jira_ticket")]
        assert ticket_keys == ["jira_ticket"]

    def test_finalize_distinct_tickets_get_distinct_names(self):
        r = SessionRecorder()
        r.start()
        r.record_user_message("Compare JIRA-100 with JIRA-200")
        s = r.finalize()
        assert "JIRA-100" not in s.steps[0].content
        assert "JIRA-200" not in s.steps[0].content
        ticket_keys = [k for k in s.vars_spec if k.startswith("jira_ticket")]
        assert len(ticket_keys) == 2

    def test_session_id_stable_after_init(self):
        r = SessionRecorder()
        sid = r.session_id
        r.start()
        r.record_user_message("x")
        r.stop()
        assert r.finalize().session_id == sid


class TestSerialization:
    def test_round_trip(self):
        s = ReplaySession(
            session_id="abc",
            name="test",
            recorded_at=123.0,
            vars_spec={"x": {"type": "string", "default": "y"}},
            steps=[
                ReplayStep(kind="user_message", content="hi ${x}"),
                ReplayStep(kind="tool_call", tool="t", args={"a": 1}),
            ],
        )
        d = session_to_dict(s)
        loaded = session_from_dict(d)
        assert loaded.session_id == "abc"
        assert loaded.steps[0].content == "hi ${x}"
        assert loaded.steps[1].tool == "t"

    def test_yaml_save_and_load(self, tmp_path: Path):
        s = ReplaySession(
            session_id="t1",
            name="hello",
            steps=[ReplayStep(kind="user_message", content="x")],
        )
        path = save_replay_session(tmp_path, s)
        assert path.exists()
        assert path.suffix == ".yaml"
        loaded = load_replay_session(path)
        assert loaded.session_id == "t1"
        assert loaded.steps[0].content == "x"

    def test_save_includes_helpful_header_comments(self, tmp_path: Path):
        s = ReplaySession(session_id="t1")
        path = save_replay_session(tmp_path, s)
        text = path.read_text(encoding="utf-8")
        assert text.startswith("#")
        assert "edit freely" in text.lower()

    def test_legacy_json_loads(self, tmp_path: Path):
        # Mimics the v1 schema from the previous JSON-based replay.py.
        legacy = {
            "session_id": "old1",
            "name": "legacy",
            "recorded_at": 0,
            "original_context": {"cwd": "/tmp"},
            "variables": {"CWD": "/tmp"},
            "actions": [
                {"step": 1, "action_type": "user_message", "content": "hello"},
                {
                    "step": 2,
                    "action_type": "tool_call",
                    "tool_name": "fetch",
                    "tool_args": {"path": "${CWD}/x"},
                    "content": "",
                    "result_pattern": "",
                },
            ],
        }
        path = tmp_path / "old1.json"
        path.write_text(json.dumps(legacy))
        loaded = load_replay_session(path)
        assert loaded.session_id == "old1"
        assert loaded.steps[1].tool == "fetch"
        # Legacy 'variables' flat dict promoted to spec form.
        assert loaded.vars_spec["CWD"]["default"] == "/tmp"

    def test_load_rejects_non_dict(self, tmp_path: Path):
        path = tmp_path / "bad.yaml"
        path.write_text("- just\n- a\n- list\n")
        with pytest.raises(ValueError, match="did not parse to a dict"):
            load_replay_session(path)

    def test_yaml_save_omits_default_string_keys(self, tmp_path: Path):
        # Empty content/tool/args fields shouldn't bloat the YAML output.
        s = ReplaySession(
            session_id="t1",
            steps=[ReplayStep(kind="user_message", content="hello")],
        )
        path = save_replay_session(tmp_path, s)
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert loaded["steps"][0] == {"kind": "user_message", "content": "hello"}


class TestListAndFind:
    def test_list_returns_empty_when_dir_missing(self, tmp_path: Path):
        assert list_replay_sessions(tmp_path) == []

    def test_list_sorted_by_recorded_at_desc(self, tmp_path: Path):
        replays = tmp_path / "replays"
        replays.mkdir()
        for sid, ts in [("a", 1.0), ("b", 3.0), ("c", 2.0)]:
            save_replay_session(tmp_path, ReplaySession(session_id=sid, recorded_at=ts))
        ids = [s.session_id for s in list_replay_sessions(tmp_path)]
        assert ids == ["b", "c", "a"]

    def test_list_skips_corrupt(self, tmp_path: Path):
        replays = tmp_path / "replays"
        replays.mkdir()
        save_replay_session(tmp_path, ReplaySession(session_id="ok"))
        (replays / "broken.yaml").write_text(": : :\n  invalid")
        ids = [s.session_id for s in list_replay_sessions(tmp_path)]
        assert ids == ["ok"]

    def test_find_exact_id(self, tmp_path: Path):
        save_replay_session(tmp_path, ReplaySession(session_id="abc123"))
        path = find_replay_session(tmp_path, "abc123")
        assert path is not None
        assert path.stem == "abc123"

    def test_find_substring_match(self, tmp_path: Path):
        save_replay_session(tmp_path, ReplaySession(session_id="abc123"))
        path = find_replay_session(tmp_path, "abc")
        assert path is not None

    def test_find_missing_returns_none(self, tmp_path: Path):
        assert find_replay_session(tmp_path, "nope") is None


class TestSessionToDriveYaml:
    """The /record -> drive script converter replaces build_replay_prompt.

    The drive script is what /replay run executes via the drive runner;
    the older prose-prompt approach asked the LLM to "follow these
    steps as a reference" and never round-tripped tool calls.
    """

    def test_user_messages_become_submit_steps(self):
        s = ReplaySession(
            session_id="x",
            steps=[
                ReplayStep(kind="user_message", content="Look at JIRA-200"),
                ReplayStep(kind="ai_message", content="Done"),
                ReplayStep(kind="user_message", content="Also fix the typo"),
            ],
        )
        out = session_to_drive_yaml(s)
        loaded = yaml.safe_load(out)
        submits = [step for step in loaded["steps"] if "submit" in step]
        assert len(submits) == 2
        assert submits[0]["submit"]["value"] == "Look at JIRA-200"
        assert submits[1]["submit"]["value"] == "Also fix the typo"

    def test_tool_calls_become_yaml_comments(self):
        # Comments survive yaml.safe_dump? No — we hand-write them in
        # session_to_drive_yaml so the output text contains them even
        # though they vanish on a round-trip through safe_load.
        s = ReplaySession(
            session_id="x",
            steps=[
                ReplayStep(kind="tool_call", tool="open_pr", args={"repo": "o/r"}),
                ReplayStep(kind="user_message", content="hi"),
            ],
        )
        out = session_to_drive_yaml(s)
        assert "open_pr" in out
        assert "# " in out  # comment marker present

    def test_includes_vars_block(self):
        s = ReplaySession(
            session_id="x",
            vars_spec={"ticket": {"type": "string", "default": "JIRA-200"}},
            steps=[ReplayStep(kind="user_message", content="Look at ${ticket}")],
        )
        loaded = yaml.safe_load(session_to_drive_yaml(s))
        assert loaded["vars"] == {"ticket": {"type": "string", "default": "JIRA-200"}}

    def test_save_drive_script_writes_drive_yaml_file(self, tmp_path: Path):
        s = ReplaySession(
            session_id="abc",
            name="t",
            steps=[ReplayStep(kind="user_message", content="hello")],
        )
        path = save_drive_script_for_session(tmp_path, s)
        assert path.exists()
        assert path.suffix == ".yaml"
        assert ".drive" in path.stem
        text = path.read_text(encoding="utf-8")
        assert "# bog-agents drive script" in text
        assert "submit" in text


class TestLiveCapture:
    """Recorder is fed live by app._mount_message via app._feed_recorder.

    These tests exercise the recorder directly with the same shape of
    inputs the app feeds it, since spinning up a Textual app for a
    plumbing test is heavyweight overkill.
    """

    def test_user_then_tool_then_ai_captures_in_order(self):
        from bog_agents_cli.replay import SessionRecorder

        r = SessionRecorder(name="t")
        r.start()
        r.record_user_message("Look at JIRA-200")
        r.record_tool_call("jira__get", {"id": "JIRA-200"})
        r.record_ai_message("Got it: blocker on checkout.")
        r.stop()
        s = r.finalize()
        kinds = [step.kind for step in s.steps]
        assert kinds == ["user_message", "tool_call", "ai_message"]
        # The Jira ticket should have been variabilized once and reused.
        assert "${jira_ticket}" in s.steps[0].content
        assert s.steps[1].args["id"] == "${jira_ticket}"
        assert s.vars_spec["jira_ticket"]["default"] == "JIRA-200"

    def test_recorder_skips_slash_commands_via_app_filter(self):
        # The app filters lines starting with `/` before calling the
        # recorder — the recorder itself doesn't filter. This test just
        # documents the contract by exercising the recorder with a
        # filtered-input flow.
        from bog_agents_cli.replay import SessionRecorder

        r = SessionRecorder()
        r.start()
        # Slash command not passed in (caller filtered it).
        r.record_user_message("real prompt")
        s = r.finalize()
        assert len(s.steps) == 1
        assert s.steps[0].content == "real prompt"


class TestL1CredentialRedaction:
    """L1: ``record_tool_call`` must scrub credential-bearing fields.

    Recordings land on disk under ``~/.bog-agents/replays/`` as plain
    YAML, so any value we capture verbatim is a credential leak waiting
    to happen. The recorder owns the denylist; callers should not have
    to remember to redact.
    """

    def test_record_tool_call_redacts_obvious_secrets(self):
        from bog_agents_cli.replay import SessionRecorder

        r = SessionRecorder(name="redact")
        r.start()
        r.record_tool_call(
            "http_get",
            {
                "url": "https://api.example.com/x",
                "api_key": "sk-not-this-one",
                "headers": {
                    "Authorization": "Bearer 12345",
                    "X-Trace": "ok-to-keep",
                },
            },
        )
        # Inspect the raw session before finalize() — the variabilizer
        # rewrites benign fields (urls/paths) into ${var} placeholders,
        # which would conflate "redacted" with "variabilized" here.
        step = next(st for st in r._session.steps if st.kind == "tool_call")
        assert step.args["url"] == "https://api.example.com/x"
        assert step.args["api_key"] == "***REDACTED***"
        assert step.args["headers"]["Authorization"] == "***REDACTED***"
        assert step.args["headers"]["X-Trace"] == "ok-to-keep"
        r.finalize()

    def test_redact_secrets_handles_camelcase_keys(self):
        from bog_agents_cli.replay import _redact_secrets

        out = _redact_secrets(
            {
                "apiKey": "leak",
                "passwordHash": "leak",
                "client_secret": "leak",
                "TOKEN": "leak",
                "username": "ok",
                "auth_header": "leak",
            }
        )
        assert out["apiKey"] == "***REDACTED***"
        assert out["passwordHash"] == "***REDACTED***"
        assert out["client_secret"] == "***REDACTED***"
        assert out["TOKEN"] == "***REDACTED***"
        assert out["auth_header"] == "***REDACTED***"
        assert out["username"] == "ok"

    def test_redact_secrets_walks_lists(self):
        from bog_agents_cli.replay import _redact_secrets

        out = _redact_secrets(
            {
                "items": [
                    {"name": "a", "api_key": "leak1"},
                    {"name": "b", "api_key": "leak2"},
                ],
            }
        )
        assert out["items"][0]["api_key"] == "***REDACTED***"
        assert out["items"][1]["api_key"] == "***REDACTED***"
        assert out["items"][0]["name"] == "a"
