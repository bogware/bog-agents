"""Tests for the dreamscape → expert rule proposer (REVIEW.md Wave E)."""

from __future__ import annotations

import textwrap
from typing import TYPE_CHECKING

import pytest
from langchain_core.messages import AIMessage

from bog_agents_cli.dreamscape.rule_proposer import (
    approve_proposal,
    build_intent,
    discard_proposal,
    list_pending_proposals,
    propose_rules,
    render_proposals_list,
)

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Stub model
# ---------------------------------------------------------------------------


class _StubModel:
    """Mini chat model that returns a pre-scripted YAML response."""

    def __init__(self, scripted: str) -> None:
        self._scripted = scripted
        self.invocations: list = []

    def invoke(self, messages: list) -> object:
        self.invocations.append(list(messages))
        return AIMessage(content=self._scripted)


# ---------------------------------------------------------------------------
# Intent assembly
# ---------------------------------------------------------------------------


class TestBuildIntent:
    def test_includes_agent_id_and_existing_rules(self) -> None:
        text = build_intent(
            agent_id="alpha",
            dreams=[],
            tool_history=[],
            existing_rules=["block_rm", "budget_brake"],
        )
        assert "alpha" in text
        assert "block_rm" in text
        assert "budget_brake" in text

    def test_includes_tool_history_tail(self, tmp_path: Path) -> None:
        history = [
            {"name": "shell", "command": f"echo {i}", "args": {}} for i in range(60)
        ]
        text = build_intent(
            agent_id="x",
            dreams=[],
            tool_history=history,
            existing_rules=[],
        )
        # The tail should appear; earliest entries should be truncated.
        assert "echo 59" in text
        # And not the very earliest (since we slice [-40:])
        assert "echo 0" not in text

    def test_dreams_reversed_oldest_first(self, tmp_path: Path) -> None:
        d1 = tmp_path / "older.md"
        d2 = tmp_path / "newer.md"
        d1.write_text("first content", encoding="utf-8")
        d2.write_text("second content", encoding="utf-8")
        # Most-recent-first input → output puts oldest first
        text = build_intent(
            agent_id="x",
            dreams=[
                (d2, d2.read_text(encoding="utf-8")),
                (d1, d1.read_text(encoding="utf-8")),
            ],
            tool_history=[],
            existing_rules=[],
        )
        assert text.index("first content") < text.index("second content")


# ---------------------------------------------------------------------------
# propose_rules
# ---------------------------------------------------------------------------


class TestProposeRules:
    def test_saves_proposal_when_history_present(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Pretend dreamscape has no dreams — we'll feed history only.
        monkeypatch.setattr(
            "bog_agents_cli.dreamscape.dream_engine.list_agent_dreams",
            lambda _agent_id, *, limit=20: [],
        )
        yaml = textwrap.dedent(
            """
            - name: block_force_push
              when:
                - tool_call:
                    command:
                      matches: 'git push --force'
              then:
                - deny: "no force-push to main"
            """
        )
        run = propose_rules(
            agent_id="test",
            model=_StubModel(yaml),
            tool_history=[
                {"name": "shell", "command": "git push --force main", "args": {}}
            ],
            proposals_dir=tmp_path,
        )
        assert run.error == ""
        assert not run.skipped
        assert run.saved_path is not None
        assert run.saved_path.exists()
        assert "block_force_push" in run.saved_path.read_text(encoding="utf-8")
        assert run.saved_path.parent == tmp_path

    def test_no_proposals_signal_results_in_skipped(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            "bog_agents_cli.dreamscape.dream_engine.list_agent_dreams",
            lambda _agent_id, *, limit=20: [],
        )
        run = propose_rules(
            agent_id="test",
            model=_StubModel("# no-proposals\n"),
            tool_history=[{"name": "shell", "command": "ls", "args": {}}],
            proposals_dir=tmp_path,
        )
        assert run.skipped
        assert run.saved_path is None

    def test_empty_inputs_skip(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            "bog_agents_cli.dreamscape.dream_engine.list_agent_dreams",
            lambda _agent_id, *, limit=20: [],
        )
        run = propose_rules(
            agent_id="test",
            model=_StubModel("ignored"),
            tool_history=[],
            proposals_dir=tmp_path,
        )
        assert run.skipped
        assert "no dreams or tool history" in run.error

    def test_dry_run_doesnt_write(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            "bog_agents_cli.dreamscape.dream_engine.list_agent_dreams",
            lambda _agent_id, *, limit=20: [],
        )
        yaml = "- name: r\n  when:\n    - tool_call: {}\n  then:\n    - audit_log\n"
        run = propose_rules(
            agent_id="x",
            model=_StubModel(yaml),
            tool_history=[{"name": "x", "args": {}}],
            proposals_dir=tmp_path,
            save=False,
        )
        assert run.proposal is not None
        assert run.saved_path is None
        assert list(tmp_path.iterdir()) == []

    def test_uses_real_dreams_directory(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        dream_file = tmp_path / "20260516-dream.md"
        dream_file.write_text("session noted X happening 3 times", encoding="utf-8")
        monkeypatch.setattr(
            "bog_agents_cli.dreamscape.dream_engine.list_agent_dreams",
            lambda _agent_id, *, limit=20: [dream_file],
        )
        yaml = (
            "- name: r\n"
            "  description: from dream 20260516\n"
            "  when:\n"
            "    - tool_call: {}\n"
            "  then:\n"
            "    - audit_log\n"
        )
        model = _StubModel(yaml)
        run = propose_rules(
            agent_id="x",
            model=model,
            tool_history=[],
            proposals_dir=tmp_path / "proposals",
        )
        assert run.dream_sources == [dream_file]
        assert run.saved_path is not None
        # Verify the dream content was forwarded to the model.
        seen_text = " ".join(
            str(getattr(m, "content", "")) for msgs in model.invocations for m in msgs
        )
        assert "X happening 3 times" in seen_text


# ---------------------------------------------------------------------------
# approve / discard / list / render
# ---------------------------------------------------------------------------


class TestProposalManagement:
    def test_list_empty(self, tmp_path: Path) -> None:
        assert list_pending_proposals(tmp_path) == []

    def test_list_returns_yamls_sorted(self, tmp_path: Path) -> None:
        (tmp_path / "b.yaml").write_text(
            "- name: b\n  when:\n    - x: {}\n  then:\n    - audit_log\n"
        )
        (tmp_path / "a.yaml").write_text(
            "- name: a\n  when:\n    - x: {}\n  then:\n    - audit_log\n"
        )
        names = [p.name for p in list_pending_proposals(tmp_path)]
        assert names == ["a.yaml", "b.yaml"]

    def test_approve_moves_file(self, tmp_path: Path) -> None:
        prop = tmp_path / "proposals"
        rules = tmp_path / "active"
        prop.mkdir()
        (prop / "rule.yaml").write_text(
            "- name: r\n  when:\n    - x: {}\n  then:\n    - audit_log\n",
            encoding="utf-8",
        )
        approved = approve_proposal(
            proposals_dir=prop, rules_dir=rules, name="rule.yaml"
        )
        assert approved.exists()
        assert approved.parent == rules
        assert not (prop / "rule.yaml").exists()

    def test_approve_rejects_unsafe_name(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="path separators"):
            approve_proposal(
                proposals_dir=tmp_path / "proposals",
                rules_dir=tmp_path / "rules",
                name="../escape.yaml",
            )

    def test_approve_refuses_overwrite_by_default(self, tmp_path: Path) -> None:
        prop = tmp_path / "proposals"
        rules = tmp_path / "active"
        prop.mkdir()
        rules.mkdir()
        (prop / "r.yaml").write_text(
            "- name: r\n  when:\n    - x: {}\n  then:\n    - audit_log\n"
        )
        (rules / "r.yaml").write_text("# existing\n")
        with pytest.raises(ValueError, match="already exists"):
            approve_proposal(proposals_dir=prop, rules_dir=rules, name="r.yaml")

    def test_discard_deletes(self, tmp_path: Path) -> None:
        (tmp_path / "junk.yaml").write_text(
            "- name: r\n  when:\n    - x: {}\n  then:\n    - audit_log\n",
            encoding="utf-8",
        )
        deleted = discard_proposal(proposals_dir=tmp_path, name="junk.yaml")
        assert not deleted.exists()

    def test_render_proposals_list_empty(self, tmp_path: Path) -> None:
        text = render_proposals_list(tmp_path / "nope")
        assert "No pending" in text

    def test_render_proposals_list_with_entries(self, tmp_path: Path) -> None:
        (tmp_path / "a.yaml").write_text(
            "- name: a\n  when:\n    - x: {}\n  then:\n    - audit_log\n"
        )
        text = render_proposals_list(tmp_path)
        assert "a.yaml" in text
        assert "approve" in text.lower()


# ---------------------------------------------------------------------------
# Controller wiring
# ---------------------------------------------------------------------------


class TestControllerProposalFlow:
    """End-to-end: /expert propose → list → approve / discard."""

    def test_propose_dispatch_without_model_factory(self, tmp_path: Path) -> None:
        from bog_agents_cli.expert_controller import (
            get_controller,
            reset_controllers,
        )

        reset_controllers()
        out = get_controller(tmp_path).propose_from_dreamscape("alpha")
        assert "no model factory" in out.lower()

    def test_propose_and_approve_round_trip(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from bog_agents_cli.expert_controller import (
            dispatch,
            get_controller,
            reset_controllers,
        )

        monkeypatch.setattr(
            "bog_agents_cli.dreamscape.dream_engine.list_agent_dreams",
            lambda _agent_id, *, limit=20: [],
        )
        reset_controllers()
        yaml = (
            "- name: block_force\n"
            "  when:\n"
            "    - tool_call:\n"
            "        command:\n"
            "          matches: 'git push --force'\n"
            "  then:\n"
            "    - deny: 'no'\n"
        )
        c = get_controller(tmp_path, model_factory=lambda: _StubModel(yaml))
        # Seed tool_call_history so propose has something to look at.
        c.middleware._tool_call_history.append(
            {"name": "shell", "command": "git push --force main", "args": {}}
        )

        out = c.propose_from_dreamscape("default")
        assert "Saved proposal" in out

        # List shows it.
        listed = dispatch("/expert proposals", tmp_path)
        assert "block_force" in listed

        # Approve it.
        # Find the proposed file's name from the saved file.
        proposals_dir = tmp_path / ".bog-agents" / "expert_rules" / "proposals"
        proposed = next(proposals_dir.glob("*.yaml"))
        out = dispatch(f"/expert proposals approve {proposed.name}", tmp_path)
        assert "Approved" in out
        # File moved to rules dir.
        rules_dir = tmp_path / ".bog-agents" / "expert_rules"
        assert (rules_dir / proposed.name).exists()
        assert not proposed.exists()
        # Engine reloaded; the rule is live.
        assert any(r.name == "block_force" for r in c.middleware.engine.rules)

    def test_proposals_discard_dispatch(
        self,
        tmp_path: Path,
    ) -> None:
        from bog_agents_cli.expert_controller import (
            dispatch,
            reset_controllers,
        )

        reset_controllers()
        proposals_dir = tmp_path / ".bog-agents" / "expert_rules" / "proposals"
        proposals_dir.mkdir(parents=True)
        target = proposals_dir / "discard-me.yaml"
        target.write_text(
            "- name: x\n  when:\n    - tool_call: {}\n  then:\n    - audit_log\n",
            encoding="utf-8",
        )
        out = dispatch("/expert proposals discard discard-me.yaml", tmp_path)
        assert "Discarded" in out
        assert not target.exists()


# ---------------------------------------------------------------------------
# Wave F1: auto_activate
# ---------------------------------------------------------------------------


class TestAutoActivate:
    def test_auto_activate_writes_to_rules_dir(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            "bog_agents_cli.dreamscape.dream_engine.list_agent_dreams",
            lambda _agent_id, *, limit=20: [],
        )
        yaml = (
            "- name: gate_x\n"
            "  when:\n"
            "    - tool_call:\n"
            "        name: x\n"
            "  then:\n"
            "    - deny: 'no'\n"
        )
        rules = tmp_path / "rules"
        proposals = tmp_path / "proposals"
        run = propose_rules(
            agent_id="x",
            model=_StubModel(yaml),
            tool_history=[{"name": "x", "args": {}}],
            proposals_dir=proposals,
            rules_dir=rules,
            auto_activate=True,
        )
        assert run.active
        assert run.saved_path is not None
        assert run.saved_path.parent == rules
        # Filename should NOT have a timestamp prefix when auto-activated.
        assert run.saved_path.name == "gate_x.yaml"
        # Staging dir untouched.
        assert not proposals.exists() or not list(proposals.iterdir())

    def test_auto_activate_refuses_silent_clobber(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            "bog_agents_cli.dreamscape.dream_engine.list_agent_dreams",
            lambda _agent_id, *, limit=20: [],
        )
        yaml_first = (
            "- name: gate\n  when:\n    - tool_call: {}\n  then:\n    - deny: 'first'\n"
        )
        yaml_second = (
            "- name: gate\n"
            "  when:\n"
            "    - tool_call: {}\n"
            "  then:\n"
            "    - deny: 'second different'\n"
        )
        rules = tmp_path / "rules"
        run1 = propose_rules(
            agent_id="x",
            model=_StubModel(yaml_first),
            tool_history=[{"name": "x", "args": {}}],
            rules_dir=rules,
            auto_activate=True,
        )
        assert run1.active
        run2 = propose_rules(
            agent_id="x",
            model=_StubModel(yaml_second),
            tool_history=[{"name": "x", "args": {}}],
            rules_dir=rules,
            auto_activate=True,
        )
        assert not run2.active
        assert "refusing to overwrite" in run2.error

    def test_apply_flag_routes_to_auto_activate(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from bog_agents_cli.expert_controller import (
            dispatch,
            get_controller,
            reset_controllers,
        )

        monkeypatch.setattr(
            "bog_agents_cli.dreamscape.dream_engine.list_agent_dreams",
            lambda _agent_id, *, limit=20: [],
        )
        reset_controllers()
        yaml = (
            "- name: applied_rule\n"
            "  when:\n"
            "    - tool_call: {}\n"
            "  then:\n"
            "    - audit_log\n"
        )
        c = get_controller(tmp_path, model_factory=lambda: _StubModel(yaml))
        c.middleware._tool_call_history.append({"name": "x", "args": {}})
        out = dispatch("/expert propose --apply", tmp_path)
        assert "Auto-activated" in out or "applied_rule" in out
        # The rule should be in the active rules dir, not proposals.
        active = (tmp_path / ".bog-agents" / "expert_rules").glob("*.yaml")
        names = [p.name for p in active]
        assert "applied_rule.yaml" in names
        # And reloaded into the engine.
        assert any(r.name == "applied_rule" for r in c.middleware.engine.rules)
