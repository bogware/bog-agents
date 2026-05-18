"""Tests for the /expert wizard guided setup flow (Wave F2)."""

from __future__ import annotations

from typing import Any

from langchain_core.messages import AIMessage

from bog_agents.middleware.expert_engine import (
    default_catalog,
    find_category,
    menu_text,
    run_wizard,
)


class _StubModel:
    def __init__(self, scripted: str) -> None:
        self._scripted = scripted
        self.invocations: list = []

    def invoke(self, messages: list) -> Any:
        self.invocations.append(list(messages))
        return AIMessage(content=self._scripted)


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------


class TestCatalog:
    def test_default_catalog_nonempty(self) -> None:
        cat = default_catalog()
        keys = {c.key for c in cat}
        for required in ("safety", "budget", "prod", "testing", "custom"):
            assert required in keys

    def test_find_category_case_insensitive(self) -> None:
        assert find_category("Safety") is not None
        assert find_category("SAFETY") is not None
        assert find_category("doesnt-exist") is None


# ---------------------------------------------------------------------------
# Menu text
# ---------------------------------------------------------------------------


class TestMenu:
    def test_menu_lists_all_categories(self) -> None:
        text = menu_text()
        for c in default_catalog():
            assert c.key in text
            assert c.title in text
        assert "Usage: /expert wizard" in text


# ---------------------------------------------------------------------------
# Wizard runs
# ---------------------------------------------------------------------------


class TestWizardRun:
    def test_unknown_category_errors(self) -> None:
        run = run_wizard(
            category_key="not-a-real-category",
            intent="anything",
            model=_StubModel("ignored"),
        )
        assert run.error
        assert "Unknown wizard category" in run.error
        assert run.proposal is None

    def test_empty_intent_returns_help(self) -> None:
        run = run_wizard(
            category_key="safety",
            intent="",
            model=_StubModel("ignored"),
        )
        assert run.proposal is None
        assert run.error
        assert "Safety" in run.error
        # Should list the category's checklist questions.
        cat = find_category("safety")
        assert cat is not None
        for q in cat.questions:
            assert q in run.error

    def test_safety_intent_routes_through_authoring(self) -> None:
        yaml = (
            "- name: block_rm_home\n  when:\n    - tool_call:\n        command:\n          matches: 'rm -rf .*~'\n  then:\n    - deny: 'no rm home'\n"
        )
        model = _StubModel(yaml)
        run = run_wizard(
            category_key="safety",
            intent="block rm -rf targeting the home dir",
            model=model,
        )
        assert run.error == ""
        assert run.proposal is not None
        assert run.proposal.ok_to_save
        # Framing should be in the intent the model saw.
        first_call = model.invocations[0]
        human_text = str(first_call[-1].content)
        assert "Category: Safety" in human_text
        # User's actual ask is also present.
        assert "rm -rf" in human_text

    def test_budget_intent_routes_through_authoring(self) -> None:
        yaml = (
            "- name: budget_warn\n"
            "  once: true\n"
            "  when:\n"
            "    - session:\n"
            "        cost_usd:\n"
            "          gt: 2.0\n"
            "  then:\n"
            "    - notify: {channel: tui, text: 'over $2'}\n"
        )
        model = _StubModel(yaml)
        run = run_wizard(
            category_key="budget",
            intent="warn when session spend crosses $2",
            model=model,
        )
        assert run.proposal is not None
        assert run.proposal.ok_to_save


# CLI-controller wiring tests live in libs/cli/tests/unit_tests/test_expert_wizard_cli.py
# so this file can stay in the SDK package boundary.
