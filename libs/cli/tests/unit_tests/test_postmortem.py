"""Tests for the causal-replay postmortem subsystem (Q2).

No real LLM is invoked. A stub ``model_invoke`` returns canned text
covering the three-section markdown the parser expects.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bog_agents_cli.causal.ledger import EventKind, open_session
from bog_agents_cli.postmortem import (
    FailurePoint,
    PostmortemRun,
    Proposal,
    build_postmortem_prompt,
    dispatch as postmortem_dispatch,
    find_failure_point,
    parse_proposal,
    render_markdown,
    render_run,
    run_postmortem,
)

# ---------------------------------------------------------------------------
# Failure-point detection
# ---------------------------------------------------------------------------


class TestFindFailurePoint:
    def test_empty_log(self):
        fp = find_failure_point([])
        assert fp.reason == "no_failure_detected"
        assert fp.event is None

    def test_tool_error_takes_priority(self, tmp_path: Path):
        ledger = open_session(tmp_path)
        u = ledger.record(EventKind.USER_MESSAGE, actor="user", summary="run")
        tc = ledger.record(
            EventKind.TOOL_CALL,
            actor="shell",
            summary="cmd",
            parent_ids=(u.id,),
        )
        ledger.record(
            EventKind.TOOL_RESULT,
            actor="shell",
            summary="ok",
            parent_ids=(tc.id,),
            payload={"is_error": False},
        )
        err = ledger.record(
            EventKind.TOOL_RESULT,
            actor="shell",
            summary="boom",
            parent_ids=(tc.id,),
            payload={"is_error": True},
        )
        ledger.close()
        fp = find_failure_point(ledger.events())
        assert fp.reason == "tool_error"
        assert fp.event is not None
        assert fp.event.id == err.id

    def test_rule_deny_used_when_no_tool_error(self, tmp_path: Path):
        ledger = open_session(tmp_path)
        u = ledger.record(EventKind.USER_MESSAGE, actor="user", summary="x")
        rf = ledger.record(
            EventKind.RULE_FIRE,
            actor="rule_block_push",
            summary="deny: force push to main",
            parent_ids=(u.id,),
            payload={"action": "deny"},
        )
        ledger.close()
        fp = find_failure_point(ledger.events())
        assert fp.reason == "rule_denied"
        assert fp.event is not None
        assert fp.event.id == rf.id

    def test_final_answer_when_no_other_signal(self, tmp_path: Path):
        ledger = open_session(tmp_path)
        u = ledger.record(EventKind.USER_MESSAGE, actor="user", summary="x")
        fa = ledger.record(
            EventKind.FINAL_ANSWER,
            actor="model",
            summary="here is the answer",
            parent_ids=(u.id,),
        )
        ledger.close()
        fp = find_failure_point(ledger.events())
        assert fp.reason == "final_answer_unsatisfactory"
        assert fp.event is not None
        assert fp.event.id == fa.id

    def test_ancestry_threaded(self, tmp_path: Path):
        ledger = open_session(tmp_path)
        u = ledger.record(EventKind.USER_MESSAGE, actor="user", summary="x")
        m = ledger.record(
            EventKind.MODEL_CALL,
            actor="model",
            summary="think",
            parent_ids=(u.id,),
        )
        tc = ledger.record(
            EventKind.TOOL_CALL,
            actor="shell",
            summary="cmd",
            parent_ids=(m.id,),
        )
        ledger.record(
            EventKind.TOOL_RESULT,
            actor="shell",
            summary="boom",
            parent_ids=(tc.id,),
            payload={"is_error": True},
        )
        ledger.close()
        fp = find_failure_point(ledger.events())
        ancestry_ids = {e.id for e in fp.ancestry}
        assert {u.id, m.id, tc.id}.issubset(ancestry_ids)


# ---------------------------------------------------------------------------
# Prompt synthesis
# ---------------------------------------------------------------------------


class TestPromptSynthesis:
    def test_includes_section_headers(self, tmp_path: Path):
        ledger = open_session(tmp_path)
        ledger.record(EventKind.USER_MESSAGE, actor="user", summary="hi")
        fp = FailurePoint(event=None, reason="no_failure_detected")
        out = build_postmortem_prompt("sess-1", ledger.events(), fp)
        for header in (
            "## Reason",
            "## Trigger event",
            "## Full event log",
        ):
            assert header in out

    def test_user_note_appears(self, tmp_path: Path):
        ledger = open_session(tmp_path)
        ledger.record(EventKind.USER_MESSAGE, actor="user", summary="hi")
        fp = FailurePoint(event=None, reason="no_failure_detected")
        out = build_postmortem_prompt(
            "sess", ledger.events(), fp, user_note="i wanted X but got Y"
        )
        assert "i wanted X but got Y" in out


# ---------------------------------------------------------------------------
# Proposal parser
# ---------------------------------------------------------------------------


_FAKE_RESPONSE = """\
## Rule
```yaml
- name: block_force_push
  when:
    - tool_call:
        command: { matches: 'git push.*--force.*main' }
  then:
    - deny: "Force push to main is prohibited"
```

## Skill
Always confirm with the user before running destructive git operations.

## Config
Set BOG_AGENTS_EXPERT_RULES_AUTOLOAD=1 to load .bog-agents/expert_rules/ on boot.
"""


class TestParseProposal:
    def test_extracts_three_sections(self):
        p = parse_proposal(_FAKE_RESPONSE)
        assert "block_force_push" in p.rule_yaml
        assert "destructive git" in p.skill_markdown
        assert "BOG_AGENTS_EXPERT_RULES_AUTOLOAD" in p.config_change
        assert p.raw == _FAKE_RESPONSE

    def test_missing_sections_are_empty(self):
        # Only Rule, no Skill / Config.
        text = "## Rule\nfoo\n"
        p = parse_proposal(text)
        assert p.rule_yaml == "foo"
        assert p.skill_markdown == ""
        assert p.config_change == ""

    def test_case_insensitive_headers(self):
        text = "## rule\na\n## skill\nb\n## config\nc"
        p = parse_proposal(text)
        assert p.rule_yaml == "a"
        assert p.skill_markdown == "b"
        assert p.config_change == "c"


# ---------------------------------------------------------------------------
# End-to-end via stub model
# ---------------------------------------------------------------------------


class TestRunPostmortem:
    @pytest.fixture
    def session(self, tmp_path: Path) -> tuple[str, Path]:
        """Build a session with one tool error; return (id, working_dir)."""
        ledger = open_session(tmp_path)
        u = ledger.record(EventKind.USER_MESSAGE, actor="user", summary="run")
        tc = ledger.record(
            EventKind.TOOL_CALL,
            actor="shell",
            summary="cmd",
            parent_ids=(u.id,),
        )
        ledger.record(
            EventKind.TOOL_RESULT,
            actor="shell",
            summary="boom",
            parent_ids=(tc.id,),
            payload={"is_error": True},
        )
        ledger.close()
        return ledger.session_id, tmp_path

    def test_run_with_stub_model(self, session):
        sid, working_dir = session

        def stub(system_prompt: str, user_prompt: str) -> str:
            _ = system_prompt
            assert "Postmortem for session" in user_prompt
            return _FAKE_RESPONSE

        run = run_postmortem(
            session_id=sid,
            working_dir=working_dir,
            model_invoke=stub,
            save=True,
        )
        assert run.error == ""
        assert run.proposal is not None
        assert "block_force_push" in run.proposal.rule_yaml
        assert run.failure.reason == "tool_error"
        assert run.saved_path is not None
        assert run.saved_path.exists()
        body = run.saved_path.read_text(encoding="utf-8")
        assert "## Rule" in body
        assert "block_force_push" in body

    def test_latest_resolves_to_newest_session(self, tmp_path: Path):
        # Two sessions, both empty (no events).
        open_session(tmp_path, session_id="20990101T000000Z-old")
        open_session(tmp_path, session_id="20990102T000000Z-new")

        def stub(_s: str, _u: str) -> str:
            return _FAKE_RESPONSE

        run = run_postmortem(
            session_id="latest",
            working_dir=tmp_path,
            model_invoke=stub,
            save=False,
        )
        # Both sessions are empty; the runner returns an error without
        # trying to invoke the model.
        assert run.error != ""

    def test_latest_with_no_sessions(self, tmp_path: Path):
        def stub(_s: str, _u: str) -> str:
            return _FAKE_RESPONSE

        run = run_postmortem(
            session_id="latest",
            working_dir=tmp_path,
            model_invoke=stub,
        )
        assert "No causal sessions found" in run.error

    def test_unknown_session_id(self, tmp_path: Path):
        def stub(_s: str, _u: str) -> str:
            return _FAKE_RESPONSE

        run = run_postmortem(
            session_id="totally-not-a-real-session",
            working_dir=tmp_path,
            model_invoke=stub,
        )
        assert "no recorded events" in run.error

    def test_model_failure_is_surfaced(self, session):
        sid, working_dir = session

        def stub(_s: str, _u: str) -> str:
            msg = "synthetic provider outage"
            raise RuntimeError(msg)

        run = run_postmortem(
            session_id=sid,
            working_dir=working_dir,
            model_invoke=stub,
        )
        assert "Model call failed" in run.error
        assert "synthetic provider outage" in run.error


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


class TestRenderers:
    def test_render_run_happy(self, tmp_path: Path):
        run = PostmortemRun(
            session_id="sess-1",
            failure=FailurePoint(event=None, reason="tool_error"),
            proposal=Proposal(
                rule_yaml="a",
                skill_markdown="b",
                config_change="c",
                raw="raw",
            ),
        )
        out = render_run(run)
        assert "Postmortem for sess-1" in out
        assert "tool_error" in out
        assert "== Rule ==" in out
        assert "a" in out
        assert "b" in out
        assert "c" in out

    def test_render_run_with_error(self):
        run = PostmortemRun(
            session_id="sess-2",
            failure=FailurePoint(event=None, reason="no_failure_detected"),
            proposal=None,
            error="boom",
        )
        out = render_run(run)
        assert "boom" in out

    def test_render_markdown_includes_raw(self, tmp_path: Path):
        proposal = Proposal(
            rule_yaml="a",
            skill_markdown="b",
            config_change="c",
            raw="raw-model-output",
        )
        fp = FailurePoint(event=None, reason="tool_error")
        out = render_markdown("sess-3", fp, proposal)
        assert "Postmortem — session sess-3" in out
        assert "## Rule" in out
        assert "raw-model-output" in out


# ---------------------------------------------------------------------------
# Slash dispatch
# ---------------------------------------------------------------------------


class TestDispatch:
    def test_help_when_empty(self, tmp_path: Path):
        out = postmortem_dispatch("/postmortem", working_dir=tmp_path)
        assert "Usage" in out

    def test_list_empty(self, tmp_path: Path):
        out = postmortem_dispatch("/postmortem list", working_dir=tmp_path)
        assert "No postmortems saved" in out

    def test_list_with_files(self, tmp_path: Path):
        d = tmp_path / ".bog-agents" / "postmortems"
        d.mkdir(parents=True)
        (d / "20990101T000000Z-x.md").write_text("body", encoding="utf-8")
        out = postmortem_dispatch("/postmortem list", working_dir=tmp_path)
        assert "1 postmortem file" in out
        assert "20990101T000000Z-x.md" in out

    def test_dispatch_no_model_returns_hint(self, tmp_path: Path):
        out = postmortem_dispatch(
            "/postmortem latest", working_dir=tmp_path, model_invoke=None
        )
        assert "requires a model" in out

    def test_dispatch_with_stub_model(self, tmp_path: Path):
        ledger = open_session(tmp_path)
        ledger.record(EventKind.USER_MESSAGE, actor="user", summary="run")
        ledger.close()

        def stub(_s: str, _u: str) -> str:
            return _FAKE_RESPONSE

        out = postmortem_dispatch(
            f"/postmortem {ledger.session_id}",
            working_dir=tmp_path,
            model_invoke=stub,
        )
        # The session has no failure event; the runner still calls
        # the model. The render should include the parsed sections.
        assert "block_force_push" in out or "no rule needed" in out

    def test_user_note_after_session_id(self, tmp_path: Path):
        ledger = open_session(tmp_path)
        ledger.record(EventKind.USER_MESSAGE, actor="user", summary="run")
        ledger.close()

        captured: list[str] = []

        def stub(_s: str, user_prompt: str) -> str:
            captured.append(user_prompt)
            return _FAKE_RESPONSE

        postmortem_dispatch(
            f"/postmortem {ledger.session_id} I expected X but got Y",
            working_dir=tmp_path,
            model_invoke=stub,
        )
        assert any("I expected X but got Y" in p for p in captured)
