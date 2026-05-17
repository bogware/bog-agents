"""Tests for /expert write — LLM-driven rule authoring (REVIEW.md T-11 v2 #4)."""

from __future__ import annotations

import textwrap
from typing import TYPE_CHECKING

import pytest

from bog_agents.middleware.expert_engine import (
    build_proposal,
    generate_yaml,
    render_proposal,
    save_proposal,
)

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Stub model
# ---------------------------------------------------------------------------


class _ScriptedModel:
    """Minimal model: returns a pre-scripted response per call."""

    def __init__(self, scripted_yaml: str) -> None:
        self._yaml = scripted_yaml
        self.invocations: list = []

    def invoke(self, messages: list) -> object:
        from langchain_core.messages import AIMessage

        self.invocations.append(list(messages))
        return AIMessage(content=self._yaml)


# ---------------------------------------------------------------------------
# generate_yaml
# ---------------------------------------------------------------------------


class TestGenerateYaml:
    def test_strips_fenced_blocks(self) -> None:
        model = _ScriptedModel(
            textwrap.dedent(
                """
                ```yaml
                - name: r
                  when:
                    - tool_call: {}
                  then:
                    - audit_log
                ```
                """
            )
        )
        out = generate_yaml("block X", model=model)
        assert out.lstrip().startswith("- name:")
        assert "```" not in out

    def test_empty_intent_returns_empty(self) -> None:
        out = generate_yaml("   ", model=_ScriptedModel("anything"))
        assert out == ""

    def test_passes_intent_to_model(self) -> None:
        model = _ScriptedModel("- name: r\n  when:\n    - tool_call: {}\n  then:\n    - audit_log\n")
        generate_yaml("block force push", model=model)
        assert model.invocations, "model should have been called"
        msgs = model.invocations[0]
        contents = [str(getattr(m, "content", "")) for m in msgs]
        assert any("block force push" in c for c in contents)


# ---------------------------------------------------------------------------
# build_proposal — happy path + parse failure + lint
# ---------------------------------------------------------------------------


class TestBuildProposal:
    def test_happy_path(self) -> None:
        yaml = textwrap.dedent(
            """
            - name: block_rm
              salience: 100
              when:
                - tool_call:
                    name: shell_execute
                    command:
                      matches: '^rm '
              then:
                - deny: "no rm"
            """
        )
        proposal = build_proposal(
            "block rm commands", model=_ScriptedModel(yaml)
        )
        assert proposal.parse_error == ""
        assert len(proposal.rules) == 1
        # underscores in the rule name are preserved; only invalid chars are replaced
        assert proposal.suggested_filename == "block_rm.yaml"
        assert proposal.ok_to_save
        assert proposal.lint is not None

    def test_parse_error_recorded(self) -> None:
        proposal = build_proposal(
            "bad", model=_ScriptedModel("not: real yaml: [\n")
        )
        assert proposal.parse_error
        assert not proposal.rules
        assert not proposal.ok_to_save

    def test_empty_yaml_recorded(self) -> None:
        proposal = build_proposal("intent", model=_ScriptedModel(""))
        assert "no YAML" in proposal.parse_error

    def test_replay_against_history(self) -> None:
        yaml = textwrap.dedent(
            """
            - name: block_rm
              when:
                - tool_call:
                    name: shell_execute
                    command:
                      matches: '^rm '
              then:
                - deny: "no rm"
            """
        )
        history = [
            {"name": "shell_execute", "command": "rm -rf /tmp/x", "args": {}},
            {"name": "shell_execute", "command": "ls", "args": {}},
            {"name": "edit", "command": "", "args": {}},
        ]
        proposal = build_proposal(
            "block rm", model=_ScriptedModel(yaml), history=history
        )
        assert len(proposal.replay) == 3
        assert proposal.replay_count_denied == 1
        # Find the rm snapshot in the outcomes.
        rm_outcome = next(r for r in proposal.replay if "rm -rf" in r.snapshot["command"])
        assert rm_outcome.denied
        assert "no rm" in rm_outcome.deny_reasons


# ---------------------------------------------------------------------------
# render_proposal
# ---------------------------------------------------------------------------


class TestRenderProposal:
    def test_render_includes_yaml_and_lint(self) -> None:
        yaml = (
            "- name: x\n"
            "  when:\n"
            "    - tool_call: {}\n"
            "  then:\n"
            "    - audit_log\n"
        )
        proposal = build_proposal("any", model=_ScriptedModel(yaml))
        text = render_proposal(proposal)
        assert "Expert rule proposal" in text
        assert "name: x" in text
        assert "Approve with: /expert write save" in text

    def test_render_parse_error(self) -> None:
        proposal = build_proposal("any", model=_ScriptedModel("garbage: [\n"))
        text = render_proposal(proposal)
        assert "Parse error" in text
        assert "Could not parse".lower() in text.lower() or "Could not parse" in text or "Parse error" in text


# ---------------------------------------------------------------------------
# save_proposal
# ---------------------------------------------------------------------------


class TestSaveProposal:
    def _good_proposal(self):
        yaml = (
            "- name: gate\n"
            "  when:\n"
            "    - tool_call: {}\n"
            "  then:\n"
            '    - deny: "no"\n'
        )
        return build_proposal("any", model=_ScriptedModel(yaml))

    def test_writes_yaml_file(self, tmp_path: Path) -> None:
        proposal = self._good_proposal()
        target = save_proposal(proposal, rules_dir=tmp_path)
        assert target.exists()
        assert target.read_text(encoding="utf-8").startswith("- name: gate")

    def test_refuses_to_overwrite_by_default(self, tmp_path: Path) -> None:
        proposal = self._good_proposal()
        save_proposal(proposal, rules_dir=tmp_path)
        with pytest.raises(ValueError, match="already exists"):
            save_proposal(proposal, rules_dir=tmp_path)

    def test_overwrite_replaces(self, tmp_path: Path) -> None:
        proposal = self._good_proposal()
        save_proposal(proposal, rules_dir=tmp_path)
        save_proposal(proposal, rules_dir=tmp_path, overwrite=True)
        # OK, no raise.

    def test_rejects_unsafe_filename(self, tmp_path: Path) -> None:
        proposal = self._good_proposal()
        with pytest.raises(ValueError, match="path separators"):
            save_proposal(proposal, rules_dir=tmp_path, filename="../escape.yaml")
        with pytest.raises(ValueError, match=r"\.yaml or \.yml"):
            save_proposal(proposal, rules_dir=tmp_path, filename="rule.txt")

    def test_refuses_unsaveable_proposal(self, tmp_path: Path) -> None:
        bad = build_proposal("x", model=_ScriptedModel("not: yaml: [\n"))
        with pytest.raises(ValueError, match="not ok_to_save"):
            save_proposal(bad, rules_dir=tmp_path)
