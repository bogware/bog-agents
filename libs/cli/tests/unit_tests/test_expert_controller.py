"""Tests for the ``/expert``, ``/why``, ``/prove`` slash-command controller."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated() -> None:
    """Reset the controller registry between tests so each gets a fresh engine."""
    from bog_agents_cli.expert_controller import reset_controllers

    reset_controllers()


@pytest.fixture
def project_with_rules(tmp_path: Path) -> Path:
    rules_dir = tmp_path / ".bog-agents" / "expert_rules"
    rules_dir.mkdir(parents=True)
    (rules_dir / "block.yaml").write_text(
        textwrap.dedent(
            """
            - name: block_force_push
              description: Block force-push to main.
              salience: 100
              when:
                - tool_call:
                    name: shell
                    command:
                      matches: 'git push.*--force.*main'
              then:
                - deny: "no force-push to main"

            - name: budget_brake
              description: Brake on cost > $5.
              salience: 90
              when:
                - session:
                    cost_usd:
                      gt: 5.0
              then:
                - require_approval:
                    gate: "Over $5 — continue?"
            """
        ),
        encoding="utf-8",
    )
    return tmp_path


class TestStatusAndToggle:
    def test_initial_status_off(self, tmp_path: Path) -> None:
        from bog_agents_cli.expert_controller import get_controller

        out = get_controller(tmp_path).status()
        assert "Expert mode: OFF" in out
        assert "Rules loaded: 0" in out

    def test_status_with_rules(self, project_with_rules: Path) -> None:
        from bog_agents_cli.expert_controller import get_controller

        out = get_controller(project_with_rules).status()
        assert "Rules loaded: 2" in out
        assert "block_force_push" in out
        assert "budget_brake" in out

    def test_toggle(self, tmp_path: Path) -> None:
        from bog_agents_cli.expert_controller import get_controller

        c = get_controller(tmp_path)
        assert "ON" in c.set_enabled(True)
        assert c.middleware.enabled is True
        assert "OFF" in c.set_enabled(False)
        assert c.middleware.enabled is False


class TestListAndShow:
    def test_list_rules(self, project_with_rules: Path) -> None:
        from bog_agents_cli.expert_controller import get_controller

        out = get_controller(project_with_rules).list_rules()
        assert "block_force_push" in out
        assert "budget_brake" in out
        assert "salience=100" in out

    def test_list_empty(self, tmp_path: Path) -> None:
        from bog_agents_cli.expert_controller import get_controller

        out = get_controller(tmp_path).list_rules()
        assert "No rules loaded" in out

    def test_show_existing(self, project_with_rules: Path) -> None:
        from bog_agents_cli.expert_controller import get_controller

        out = get_controller(project_with_rules).show_rule("block_force_push")
        assert "Rule: block_force_push" in out
        assert "tool_call" in out
        assert "deny" in out

    def test_show_missing(self, project_with_rules: Path) -> None:
        from bog_agents_cli.expert_controller import get_controller

        out = get_controller(project_with_rules).show_rule("nope")
        assert "not found" in out

    def test_show_no_name_returns_usage(self, project_with_rules: Path) -> None:
        from bog_agents_cli.expert_controller import get_controller

        out = get_controller(project_with_rules).show_rule("")
        assert "Usage:" in out


class TestExplainAndProve:
    def test_explain_with_direct_fact(self, project_with_rules: Path) -> None:
        from bog_agents_cli.expert_controller import get_controller

        c = get_controller(project_with_rules)
        c.assert_fact("session", cost_usd=10.0)
        out = c.explain("session")
        assert "✓" in out  # proven
        assert "fact: session" in out

    def test_prove_succeeds_when_producer_active(
        self,
        project_with_rules: Path,
    ) -> None:
        # block_force_push doesn't assert_fact (it denies), so we need a producer
        # rule. Use a rule that asserts via /expert assert + then run.
        from bog_agents_cli.expert_controller import get_controller

        c = get_controller(project_with_rules)
        c.assert_fact("session", cost_usd=10.0)
        out = c.prove("session", cost_usd=10.0)
        assert "✓" in out  # direct fact satisfies

    def test_why_with_no_producer_and_no_fact(self, tmp_path: Path) -> None:
        from bog_agents_cli.expert_controller import get_controller

        out = get_controller(tmp_path).explain("anything")
        assert "✗" in out

    def test_why_requires_fact_type(self, tmp_path: Path) -> None:
        from bog_agents_cli.expert_controller import get_controller

        out = get_controller(tmp_path).explain("")
        assert "Usage:" in out


class TestDispatcher:
    def test_dispatch_expert_status(self, tmp_path: Path) -> None:
        from bog_agents_cli.expert_controller import dispatch

        out = dispatch("/expert", tmp_path)
        assert "Expert mode" in out

    def test_dispatch_expert_on(self, tmp_path: Path) -> None:
        from bog_agents_cli.expert_controller import dispatch, get_controller

        out = dispatch("/expert on", tmp_path)
        assert "ON" in out
        assert get_controller(tmp_path).middleware.enabled

    def test_dispatch_expert_off(self, tmp_path: Path) -> None:
        from bog_agents_cli.expert_controller import dispatch

        dispatch("/expert on", tmp_path)
        assert "OFF" in dispatch("/expert off", tmp_path)

    def test_dispatch_expert_list(self, project_with_rules: Path) -> None:
        from bog_agents_cli.expert_controller import dispatch

        out = dispatch("/expert list", project_with_rules)
        assert "block_force_push" in out

    def test_dispatch_expert_assert_and_run(self, tmp_path: Path) -> None:
        """Inject a fact then run engine (no rules → no-op but still answer)."""
        from bog_agents_cli.expert_controller import dispatch

        out = dispatch("/expert assert session cost_usd=10.0", tmp_path)
        assert "Asserted session" in out
        run_out = dispatch("/expert run", tmp_path)
        assert "Engine ran" in run_out

    def test_dispatch_expert_memory(self, tmp_path: Path) -> None:
        from bog_agents_cli.expert_controller import dispatch

        dispatch("/expert assert thing kind=test", tmp_path)
        out = dispatch("/expert memory", tmp_path)
        assert "thing: 1" in out
        dispatch("/expert clear", tmp_path)
        assert "empty" in dispatch("/expert memory", tmp_path)

    def test_dispatch_expert_example(self, tmp_path: Path) -> None:
        from bog_agents_cli.expert_controller import dispatch

        out = dispatch("/expert example", tmp_path)
        assert "block_force_push_to_main" in out
        assert "salience" in out

    def test_dispatch_unknown_expert_subcommand(self, tmp_path: Path) -> None:
        from bog_agents_cli.expert_controller import dispatch

        out = dispatch("/expert wibble", tmp_path)
        assert "Unknown /expert subcommand" in out

    def test_dispatch_why(self, project_with_rules: Path) -> None:
        from bog_agents_cli.expert_controller import dispatch, get_controller

        get_controller(project_with_rules).assert_fact("session", cost_usd=10.0)
        out = dispatch("/why session", project_with_rules)
        assert "session" in out

    def test_dispatch_prove(self, tmp_path: Path) -> None:
        from bog_agents_cli.expert_controller import dispatch, get_controller

        get_controller(tmp_path).assert_fact("session", cost_usd=10.0)
        out = dispatch("/prove session cost_usd=10.0", tmp_path)
        assert "✓" in out


class TestSlashCommandRegistry:
    """Verify the new commands are wired into the registry / autocomplete."""

    def test_expert_command_registered(self) -> None:
        from bog_agents_cli.commands import general

        names = {cmd.name for cmd in general.COMMANDS}
        assert "/expert" in names
        assert "/why" in names
        assert "/prove" in names

    def test_handler_methods_named_consistently(self) -> None:
        from bog_agents_cli.commands import general

        handlers = {cmd.name: cmd.handler_method for cmd in general.COMMANDS}
        assert handlers["/expert"] == "_handle_expert_command"
        assert handlers["/why"] == "_handle_why_command"
        assert handlers["/prove"] == "_handle_prove_command"


class TestReloadAndExtraRules:
    def test_reload_picks_up_new_file(self, tmp_path: Path) -> None:
        from bog_agents_cli.expert_controller import get_controller

        c = get_controller(tmp_path)
        assert "0" in c.list_rules() or "No rules" in c.list_rules()
        rules_dir = tmp_path / ".bog-agents" / "expert_rules"
        rules_dir.mkdir(parents=True)
        (rules_dir / "r.yaml").write_text(
            "- name: r\n  when:\n    - x: {}\n  then:\n    - audit_log\n",
            encoding="utf-8",
        )
        c.reload()
        assert "r" in c.list_rules()

    def test_reload_with_bad_file_keeps_old(self, project_with_rules: Path) -> None:
        from bog_agents_cli.expert_controller import get_controller

        c = get_controller(project_with_rules)
        before = c.list_rules()
        # Add a malformed file
        (project_with_rules / ".bog-agents" / "expert_rules" / "broken.yaml").write_text(
            "not yaml: [\n",
            encoding="utf-8",
        )
        out = c.reload()
        assert "error" in out.lower() or "errors" in out.lower()
        # Original rules still listable
        after = c.list_rules()
        assert "block_force_push" in after
        assert before  # smoke


# ---------------------------------------------------------------------------
# Wave 5: lint + dry-run + ApprovalStore wiring
# ---------------------------------------------------------------------------


class TestLintCommand:
    def test_lint_clean(self, project_with_rules: Path) -> None:
        from bog_agents_cli.expert_controller import get_controller

        out = get_controller(project_with_rules).lint()
        # Two well-formed rules — clean (or at most info-only).
        assert "error" not in out.lower() or "0 error" in out.lower()

    def test_lint_flags_dead_rule(self, tmp_path: Path) -> None:
        from bog_agents_cli.expert_controller import get_controller

        rules_dir = tmp_path / ".bog-agents" / "expert_rules"
        rules_dir.mkdir(parents=True)
        (rules_dir / "dead.yaml").write_text(
            "- name: orphan\n  when:\n    - totally_unknown_fact: {}\n  then:\n    - audit_log\n",
            encoding="utf-8",
        )
        c = get_controller(tmp_path)
        out = c.lint()
        assert "dead-rule" in out

    def test_dispatch_lint(self, project_with_rules: Path) -> None:
        from bog_agents_cli.expert_controller import dispatch

        out = dispatch("/expert lint", project_with_rules)
        assert "Lint" in out


class TestDryRunCommand:
    def test_dry_run_simulates_without_persisting(self, project_with_rules: Path) -> None:
        from bog_agents_cli.expert_controller import get_controller

        c = get_controller(project_with_rules)
        # Counters before
        before = dict(c.middleware.counters)
        memory_before = c.middleware.engine.memory.stats()

        out = c.dry_run("tool_call", name="shell", command="git push --force main")
        assert "Activations fired" in out
        assert "block_force_push" in out
        # Engine reports the deny in the dry-run output, but the counters
        # and memory must be untouched.
        assert "Denied: True" in out
        assert c.middleware.counters == before
        assert c.middleware.engine.memory.stats() == memory_before

    def test_dispatch_dry_run(self, project_with_rules: Path) -> None:
        from bog_agents_cli.expert_controller import dispatch

        out = dispatch(
            "/expert dry-run tool_call name=shell command='git push --force main'",
            project_with_rules,
        )
        assert "Dry-run" in out

    def test_dry_run_without_fact_type_shows_usage(self, tmp_path: Path) -> None:
        from bog_agents_cli.expert_controller import dispatch

        out = dispatch("/expert dry-run", tmp_path)
        assert "Usage" in out


class TestStarterRulesShip:
    """The shipped starter file must exist and parse."""

    def test_starter_yaml_exists_and_loads(self) -> None:
        from pathlib import Path

        from bog_agents.middleware.expert_engine import load_rule_file

        candidates = list(
            (Path(__file__).resolve().parent.parent.parent / "bog_agents_cli")
            .glob("built_in_skills/expert_starter_rules/*.yaml")
        )
        assert candidates, "starter.yaml is missing"
        rules = load_rule_file(candidates[0])
        assert len(rules) >= 3


# ---------------------------------------------------------------------------
# Wave D: /expert write (LLM-driven authoring) — REVIEW.md T-11 v2 #4
# ---------------------------------------------------------------------------


class _ScriptedModel:
    """Mini chat model returning a pre-scripted YAML response."""

    def __init__(self, scripted_yaml: str) -> None:
        self._yaml = scripted_yaml

    def invoke(self, _messages: list) -> object:
        from langchain_core.messages import AIMessage

        return AIMessage(content=self._yaml)


class TestExpertWrite:
    def test_write_without_model_factory_returns_actionable_error(
        self, tmp_path: Path
    ) -> None:
        from bog_agents_cli.expert_controller import get_controller

        out = get_controller(tmp_path).write("block X")
        assert "model factory" in out.lower()

    def test_write_with_empty_intent_shows_usage(self, tmp_path: Path) -> None:
        from bog_agents_cli.expert_controller import get_controller

        c = get_controller(
            tmp_path, model_factory=lambda: _ScriptedModel("ignored")
        )
        assert "Usage" in c.write("")

    def test_write_full_flow(self, tmp_path: Path) -> None:
        import textwrap

        from bog_agents_cli.expert_controller import dispatch, get_controller

        yaml = textwrap.dedent(
            """
            - name: block_rm
              when:
                - tool_call:
                    name: shell
                    command:
                      matches: '^rm '
              then:
                - deny: "no rm"
            """
        )
        controller = get_controller(
            tmp_path, model_factory=lambda: _ScriptedModel(yaml)
        )
        out = controller.write("block rm commands")
        assert "Expert rule proposal" in out
        assert "block_rm" in out
        # save_save now commits to disk
        rules_dir = tmp_path / ".bog-agents" / "expert_rules"
        save_out = dispatch("/expert write save", tmp_path)
        assert "Saved" in save_out
        saved_files = list(rules_dir.glob("*.yaml"))
        assert len(saved_files) == 1
        assert saved_files[0].read_text(encoding="utf-8").startswith("- name: block_rm")
        # And the controller's engine now has the rule live.
        assert any(r.name == "block_rm" for r in controller.middleware.engine.rules)

    def test_write_save_without_pending_proposal(self, tmp_path: Path) -> None:
        from bog_agents_cli.expert_controller import get_controller

        c = get_controller(
            tmp_path, model_factory=lambda: _ScriptedModel("ignored")
        )
        assert "No pending proposal" in c.write_save("rule.yaml")

    def test_write_cancel_clears_proposal(self, tmp_path: Path) -> None:
        from bog_agents_cli.expert_controller import dispatch

        yaml = (
            "- name: x\n"
            "  when:\n"
            "    - tool_call: {}\n"
            "  then:\n"
            "    - audit_log\n"
        )
        dispatch_factory = lambda: _ScriptedModel(yaml)  # noqa: E731

        # Place a proposal by going through the dispatcher.
        from bog_agents_cli.expert_controller import get_controller

        c = get_controller(tmp_path, model_factory=dispatch_factory)
        c.write("any intent")
        # Cancel via the slash dispatcher
        out = dispatch("/expert write cancel", tmp_path)
        assert "Discarded" in out
        # A follow-up save now fails because the proposal is gone.
        assert "No pending proposal" in c.write_save()

    def test_write_dispatch_routes_correctly(self, tmp_path: Path) -> None:
        from bog_agents_cli.expert_controller import dispatch, get_controller

        yaml = (
            "- name: x\n"
            "  when:\n"
            "    - tool_call: {}\n"
            "  then:\n"
            "    - audit_log\n"
        )
        get_controller(tmp_path, model_factory=lambda: _ScriptedModel(yaml))
        out = dispatch("/expert write block X", tmp_path)
        assert "Expert rule proposal" in out
