"""Tests for the postmortem → dreamscape proposer bridge (Wave T).

Four layers:

1. The bridge in isolation (``enroll_postmortem_proposal``) — every
   shape of input the parser surfaces.
2. The renderer (``render_enrollment``).
3. Integration with :func:`bog_agents_cli.postmortem.run_postmortem`
   via the ``enroll=True`` flag.
4. The ``/postmortem … --enroll`` slash flag end-to-end through
   ``dispatch``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bog_agents_cli.causal.ledger import EventKind, open_session
from bog_agents_cli.feedback_loop import (
    EnrolledProposal,
    enroll_postmortem_proposal,
    render_enrollment,
)
from bog_agents_cli.postmortem import (
    Proposal,
    dispatch as postmortem_dispatch,
    run_postmortem,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


_GOOD_RULE_YAML = """\
- name: block_force_push_to_main
  description: Block force-pushes to main/master.
  when:
    - tool_call:
        name: { eq: shell_execute }
        command: { matches: 'git push.*--force.*(main|master)' }
  then:
    - deny: "Force-push to main is prohibited."
"""

_GOOD_RULE_FENCED = f"""```yaml
{_GOOD_RULE_YAML}
```"""


def _proposal(
    *,
    rule_yaml: str = _GOOD_RULE_YAML,
    skill_markdown: str = "When asked to push code, confirm the branch.",
    config_change: str = "Set BOG_AGENTS_EXPERT_RULES_AUTOLOAD=1.",
    raw: str = "<raw>",
) -> Proposal:
    return Proposal(
        rule_yaml=rule_yaml,
        skill_markdown=skill_markdown,
        config_change=config_change,
        raw=raw,
    )


# ---------------------------------------------------------------------------
# 1. enroll_postmortem_proposal()
# ---------------------------------------------------------------------------


class TestEnrollment:
    def test_happy_path_writes_rule_and_skill(self, tmp_path: Path):
        result = enroll_postmortem_proposal(
            _proposal(),
            working_dir=tmp_path,
            source_session="abc123def456",
            now=1700000000.0,
        )
        assert result.rule_saved_path is not None
        assert result.rule_saved_path.is_file()
        assert "block_force_push_to_main" in result.rule_saved_path.read_text(
            encoding="utf-8"
        )
        # Default = staged, not active.
        assert result.active is False
        assert "proposals" in str(result.rule_saved_path)
        assert result.skill_saved_path is not None
        assert result.skill_saved_path.is_file()
        assert "When asked to push code" in result.skill_saved_path.read_text(
            encoding="utf-8"
        )
        assert result.rules_parsed == 1
        assert result.lint_errors == ()

    def test_fenced_yaml_is_unwrapped(self, tmp_path: Path):
        result = enroll_postmortem_proposal(
            _proposal(rule_yaml=_GOOD_RULE_FENCED),
            working_dir=tmp_path,
        )
        assert result.rule_saved_path is not None
        # The saved body must not include the ```yaml fence.
        body = result.rule_saved_path.read_text(encoding="utf-8")
        assert "```" not in body

    def test_no_rule_marker_skips_rule_save(self, tmp_path: Path):
        result = enroll_postmortem_proposal(
            _proposal(rule_yaml="(no rule needed) — model declined"),
            working_dir=tmp_path,
        )
        assert result.rule_saved_path is None
        # Skill still lands because skill_markdown is real.
        assert result.skill_saved_path is not None

    def test_no_skill_marker_skips_skill_save(self, tmp_path: Path):
        result = enroll_postmortem_proposal(
            _proposal(skill_markdown="(no skill needed)"),
            working_dir=tmp_path,
        )
        assert result.skill_saved_path is None
        assert result.rule_saved_path is not None

    def test_empty_proposal_returns_skipped(self, tmp_path: Path):
        result = enroll_postmortem_proposal(
            _proposal(rule_yaml="", skill_markdown="", config_change=""),
            working_dir=tmp_path,
        )
        assert result.rule_saved_path is None
        assert result.skill_saved_path is None
        assert "empty proposal" in result.skipped_reason

    def test_malformed_yaml_reports_parse_error(self, tmp_path: Path):
        bad = "- name: invalid\n  : bad : indent\n  not: a list"
        result = enroll_postmortem_proposal(
            _proposal(rule_yaml=bad, skill_markdown="(no skill needed)"),
            working_dir=tmp_path,
        )
        assert result.rule_saved_path is None
        assert "rule parse failed" in result.skipped_reason

    def test_yaml_parses_but_zero_rules(self, tmp_path: Path):
        result = enroll_postmortem_proposal(
            _proposal(rule_yaml="[]", skill_markdown="(no skill needed)"),
            working_dir=tmp_path,
        )
        # Empty list parses fine but produces no Rule objects.
        assert result.rule_saved_path is None
        assert "zero rules" in str(result.lint_errors) + result.skipped_reason

    def test_auto_activate_writes_to_active_dir(self, tmp_path: Path):
        result = enroll_postmortem_proposal(
            _proposal(),
            working_dir=tmp_path,
            auto_activate=True,
        )
        assert result.active is True
        assert result.rule_saved_path is not None
        # Active path is the non-proposals dir.
        assert "proposals" not in result.rule_saved_path.parent.name

    def test_disambiguates_existing_filename(self, tmp_path: Path):
        # Force a filename collision by running twice with a pinned clock.
        first = enroll_postmortem_proposal(
            _proposal(),
            working_dir=tmp_path,
            source_session="sess",
            now=1700000000.0,
        )
        second = enroll_postmortem_proposal(
            _proposal(),
            working_dir=tmp_path,
            source_session="sess",
            now=1700000000.0,
        )
        assert first.rule_saved_path is not None
        assert second.rule_saved_path is not None
        assert first.rule_saved_path != second.rule_saved_path
        # Both files exist on disk.
        assert first.rule_saved_path.is_file()
        assert second.rule_saved_path.is_file()

    def test_override_proposals_dir(self, tmp_path: Path):
        custom = tmp_path / "custom_proposals"
        result = enroll_postmortem_proposal(
            _proposal(skill_markdown="(no skill needed)"),
            working_dir=tmp_path,
            proposals_dir=custom,
        )
        assert result.rule_saved_path is not None
        assert custom in result.rule_saved_path.parents

    def test_override_skills_dir(self, tmp_path: Path):
        custom_skills = tmp_path / "skill_drafts"
        result = enroll_postmortem_proposal(
            _proposal(rule_yaml="(no rule needed)"),
            working_dir=tmp_path,
            skills_dir=custom_skills,
        )
        assert result.skill_saved_path is not None
        assert custom_skills in result.skill_saved_path.parents

    def test_filename_includes_session_tag(self, tmp_path: Path):
        result = enroll_postmortem_proposal(
            _proposal(skill_markdown="(no skill needed)"),
            working_dir=tmp_path,
            source_session="20260517T201500Z-abc123",
        )
        assert result.rule_saved_path is not None
        # First 12 chars of the session id are embedded in the filename.
        assert "20260517T201" in result.rule_saved_path.name
        # And the rule's name shows up as well.
        assert "block_force_push_to_main" in result.rule_saved_path.name

    def test_config_change_passed_through_unchanged(self, tmp_path: Path):
        result = enroll_postmortem_proposal(
            _proposal(config_change="Set FOO=bar"),
            working_dir=tmp_path,
        )
        assert result.config_change == "Set FOO=bar"


# ---------------------------------------------------------------------------
# 2. render_enrollment()
# ---------------------------------------------------------------------------


class TestRenderEnrollment:
    def test_render_full(self, tmp_path: Path):
        enrolled = EnrolledProposal(
            rule_saved_path=tmp_path / "rule.yaml",
            skill_saved_path=tmp_path / "skill.md",
            rules_parsed=1,
            config_change="Set FOO=bar",
            notes=("note one",),
        )
        out = render_enrollment(enrolled)
        assert "Postmortem enrolled" in out
        assert "rule.yaml" in out
        assert "skill.md" in out
        assert "Set FOO=bar" in out
        assert "STAGED" in out

    def test_render_active(self, tmp_path: Path):
        enrolled = EnrolledProposal(
            rule_saved_path=tmp_path / "x.yaml",
            rules_parsed=1,
            active=True,
        )
        out = render_enrollment(enrolled)
        assert "ACTIVE" in out

    def test_render_lint_errors(self, tmp_path: Path):
        enrolled = EnrolledProposal(
            lint_errors=("dupe-name: redeclared", "missing-then"),
            skipped_reason="rule lint errors prevented save",
        )
        out = render_enrollment(enrolled)
        assert "Lint errors" in out
        assert "dupe-name" in out
        assert "Skipped" in out

    def test_render_nothing(self):
        enrolled = EnrolledProposal()
        out = render_enrollment(enrolled)
        assert "nothing enrolled" in out

    def test_no_config_marker_suppressed(self, tmp_path: Path):
        enrolled = EnrolledProposal(
            rule_saved_path=tmp_path / "x.yaml",
            rules_parsed=1,
            config_change="(no config change)",
        )
        out = render_enrollment(enrolled)
        assert "Config" not in out


# ---------------------------------------------------------------------------
# 3. Integration via run_postmortem(enroll=True)
# ---------------------------------------------------------------------------


_FAKE_POSTMORTEM_RESPONSE = f"""\
## Rule
```yaml
{_GOOD_RULE_YAML}
```

## Skill
Always confirm before running destructive git commands.

## Config
Set BOG_AGENTS_EXPERT_RULES_AUTOLOAD=1 to auto-load .bog-agents/expert_rules/.
"""


class TestRunPostmortemEnrollment:
    @pytest.fixture
    def session_with_event(self, tmp_path: Path) -> str:
        ledger = open_session(tmp_path)
        ledger.record(EventKind.USER_MESSAGE, actor="user", summary="run")
        ledger.close()
        return ledger.session_id

    def test_enroll_false_yields_no_enrollment(
        self, tmp_path: Path, session_with_event: str
    ):
        def stub(_s, _u):
            return _FAKE_POSTMORTEM_RESPONSE

        run = run_postmortem(
            session_id=session_with_event,
            working_dir=tmp_path,
            model_invoke=stub,
            enroll=False,
            save=False,
        )
        assert run.enrollment is None

    def test_enroll_true_stages_rule(
        self, tmp_path: Path, session_with_event: str
    ):
        def stub(_s, _u):
            return _FAKE_POSTMORTEM_RESPONSE

        run = run_postmortem(
            session_id=session_with_event,
            working_dir=tmp_path,
            model_invoke=stub,
            enroll=True,
            save=False,
        )
        assert run.enrollment is not None
        assert run.enrollment.rule_saved_path is not None
        assert run.enrollment.rule_saved_path.is_file()
        # Default = staged (not active).
        assert run.enrollment.active is False
        # The staged file is under the standard proposals subdir.
        rel = run.enrollment.rule_saved_path.relative_to(tmp_path)
        assert ".bog-agents/expert_rules/proposals" in rel.as_posix()

    def test_enroll_apply_writes_active(
        self, tmp_path: Path, session_with_event: str
    ):
        def stub(_s, _u):
            return _FAKE_POSTMORTEM_RESPONSE

        run = run_postmortem(
            session_id=session_with_event,
            working_dir=tmp_path,
            model_invoke=stub,
            enroll=True,
            enroll_auto_activate=True,
            save=False,
        )
        assert run.enrollment is not None
        assert run.enrollment.active is True

    def test_enroll_failure_surfaces_in_skipped_reason(
        self, tmp_path: Path, session_with_event: str
    ):
        # Model returns a postmortem with a broken rule body.
        broken_response = (
            "## Rule\n```yaml\n: bad : yaml\n```\n"
            "## Skill\n(no skill needed)\n"
            "## Config\n(no config change)\n"
        )

        def stub(_s, _u):
            return broken_response

        run = run_postmortem(
            session_id=session_with_event,
            working_dir=tmp_path,
            model_invoke=stub,
            enroll=True,
            save=False,
        )
        assert run.enrollment is not None
        assert run.enrollment.rule_saved_path is None
        assert "rule parse failed" in run.enrollment.skipped_reason


# ---------------------------------------------------------------------------
# 4. Slash dispatch — /postmortem ... --enroll
# ---------------------------------------------------------------------------


class TestSlashDispatch:
    @pytest.fixture
    def session(self, tmp_path: Path) -> str:
        ledger = open_session(tmp_path)
        ledger.record(EventKind.USER_MESSAGE, actor="u", summary="hi")
        ledger.close()
        return ledger.session_id

    def test_help_documents_enroll(self, tmp_path: Path):
        out = postmortem_dispatch("/postmortem", working_dir=tmp_path)
        assert "--enroll" in out
        assert "--apply" in out

    def test_dispatch_with_enroll_flag(self, tmp_path: Path, session: str):
        def stub(_s, _u):
            return _FAKE_POSTMORTEM_RESPONSE

        out = postmortem_dispatch(
            f"/postmortem {session} --enroll",
            working_dir=tmp_path,
            model_invoke=stub,
        )
        assert "Postmortem enrolled" in out
        assert "STAGED" in out
        # Verify the rule actually landed.
        proposals_dir = (
            tmp_path / ".bog-agents" / "expert_rules" / "proposals"
        )
        assert any(proposals_dir.glob("postmortem-*"))

    def test_dispatch_with_apply_flag(self, tmp_path: Path, session: str):
        def stub(_s, _u):
            return _FAKE_POSTMORTEM_RESPONSE

        out = postmortem_dispatch(
            f"/postmortem {session} --apply",
            working_dir=tmp_path,
            model_invoke=stub,
        )
        assert "Postmortem enrolled" in out
        assert "ACTIVE" in out
        # The rule landed in the *active* dir, not staging.
        active_dir = tmp_path / ".bog-agents" / "expert_rules"
        files = [
            p for p in active_dir.glob("postmortem-*") if p.is_file()
        ]
        assert files

    def test_flags_can_be_mixed_with_note(self, tmp_path: Path, session: str):
        captured_prompts: list[str] = []

        def stub(_s, user_prompt: str) -> str:
            captured_prompts.append(user_prompt)
            return _FAKE_POSTMORTEM_RESPONSE

        out = postmortem_dispatch(
            f"/postmortem {session} --enroll my custom note",
            working_dir=tmp_path,
            model_invoke=stub,
        )
        # The note made it to the prompt (and the flag was stripped).
        assert any("my custom note" in p for p in captured_prompts)
        assert "Postmortem enrolled" in out

    def test_only_flags_with_no_session_id_returns_usage(
        self, tmp_path: Path
    ):
        def stub(_s, _u):
            return _FAKE_POSTMORTEM_RESPONSE

        out = postmortem_dispatch(
            "/postmortem --enroll",
            working_dir=tmp_path,
            model_invoke=stub,
        )
        assert "Usage:" in out
