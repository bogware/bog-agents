"""ROADMAP #51 (CLI): caps from the manifest, spend ledger + daily gate, budget pause prompt, pre-flight."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, patch

import pytest
from bog_agents.cost_ledger import CostLedger, RunawayCaps
from bog_agents.spend_ledger import SCOPE_USER, SpendLedger

from bog_agents_cli import cost_controller as cc
from bog_agents_cli.textual_adapter import SessionStats

MODEL = "anthropic:claude-sonnet-4-6"


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("bog_agents_cli.config_manifest.load_config_toml", dict)
    for var in (
        "BOG_AGENTS_BUDGET_USD",
        "BOG_AGENTS_BUDGET_WARN_AT_PERCENT",
        "BOG_AGENTS_DAILY_CEILING_USD",
        "BOG_AGENTS_MAX_SUBAGENTS",
        "BOG_AGENTS_MAX_WEB_SEARCHES",
        "BOG_AGENTS_PREFLIGHT_THRESHOLD_USD",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(cc, "spend_db_path", lambda: tmp_path / "spend.db")
    cc.reset_spend_ledger()
    yield
    cc.reset_spend_ledger()


class TestCaps:
    def test_defaults(self) -> None:
        caps = cc.load_cost_caps()
        assert caps == cc.CostCaps()
        ledger = cc.build_cost_ledger(caps)
        assert ledger.caps == RunawayCaps(
            max_subagents=8, max_web_searches=50, max_cost_usd=None
        )

    def test_env_overrides_and_sentinels(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BOG_AGENTS_BUDGET_USD", "2.5")
        monkeypatch.setenv("BOG_AGENTS_MAX_SUBAGENTS", "none")
        monkeypatch.setenv("BOG_AGENTS_MAX_WEB_SEARCHES", "3")
        monkeypatch.setenv("BOG_AGENTS_PREFLIGHT_THRESHOLD_USD", "off")
        monkeypatch.setenv("BOG_AGENTS_DAILY_CEILING_USD", "20")
        caps = cc.load_cost_caps()
        assert caps.budget_usd == 2.5
        assert caps.max_subagents is None
        assert caps.max_web_searches == 3
        assert caps.preflight_threshold_usd is None
        assert caps.daily_ceiling_usd == 20.0
        assert cc.build_cost_ledger(caps).caps == RunawayCaps(
            max_subagents=None, max_web_searches=3, max_cost_usd=2.5
        )

    def test_manifest_lists_the_cost_group(self) -> None:
        from bog_agents_cli.config_manifest import get_option, resolve_option

        assert get_option("cost.budget_usd") is not None
        assert resolve_option("cost.warn_at_percent") == 80
        with pytest.raises(KeyError):
            resolve_option("cost.nope")


def _stats(
    model: str = MODEL, tokens_in: int = 1_000_000, tokens_out: int = 0
) -> SessionStats:
    stats = SessionStats()
    stats.record_request(model, tokens_in, tokens_out)
    return stats


class TestSpendLedger:
    def test_turn_spend_is_priced_and_recorded_under_both_scopes(
        self, tmp_path: Path
    ) -> None:
        ledger = SpendLedger(tmp_path / "x.db")
        usd = cc.record_turn_spend(
            _stats(), cwd=tmp_path, ledger=ledger, now=1_800_000_000.0
        )
        assert usd == pytest.approx(3.0)  # $3 / 1M input tokens
        assert ledger.total_usd(SCOPE_USER, now=1_800_000_000.0) == pytest.approx(3.0)
        assert ledger.total_usd(
            f"project:{cc.project_key(tmp_path)}", now=1_800_000_000.0
        ) == pytest.approx(3.0)

    def test_unpriced_model_records_nothing(self, tmp_path: Path) -> None:
        ledger = SpendLedger()
        assert (
            cc.record_turn_spend(_stats("ollama:llama3"), cwd=tmp_path, ledger=ledger)
            == 0.0
        )
        assert ledger.total_usd(SCOPE_USER) == 0.0

    def test_daily_gate_blocks_and_warns_once(self, tmp_path: Path) -> None:
        ledger = SpendLedger()
        caps = cc.CostCaps(daily_ceiling_usd=10.0, warn_at_percent=80)
        assert cc.daily_gate(cwd=tmp_path, caps=caps, ledger=ledger) == (False, None)
        ledger.record(SCOPE_USER, 8.5)
        blocked, note = cc.daily_gate(cwd=tmp_path, caps=caps, ledger=ledger)
        assert blocked is False
        assert note is not None and "85% used" in note
        # Same state again → no second warning.
        assert cc.daily_gate(cwd=tmp_path, caps=caps, ledger=ledger) == (False, None)
        ledger.record(SCOPE_USER, 2.0)
        blocked, note = cc.daily_gate(cwd=tmp_path, caps=caps, ledger=ledger)
        assert blocked is True
        assert note is not None and "daily ceiling reached" in note

    async def test_gate_turn_mounts_note_and_blocks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BOG_AGENTS_DAILY_CEILING_USD", "1")
        cc.get_spend_ledger().record(SCOPE_USER, 5.0)
        mounted: list[Any] = []

        async def _mount(widget: object) -> None:
            mounted.append(widget)

        app = SimpleNamespace(_cwd=str(tmp_path), _mount_message=_mount)
        assert await cc.gate_turn(app) is True
        assert len(mounted) == 1

    def test_no_ceiling_means_open(self, tmp_path: Path) -> None:
        assert cc.daily_gate(
            cwd=tmp_path, caps=cc.CostCaps(), ledger=SpendLedger()
        ) == (False, None)


class TestBudgetPause:
    def test_parse_budget_answer(self) -> None:
        assert cc.parse_budget_answer({"type": "answered", "answers": ["12"]}) == 12.0
        assert cc.parse_budget_answer({"type": "answered", "answers": ["$3.50"]}) == 3.5
        assert cc.parse_budget_answer({"type": "answered", "answers": [""]}) is None
        assert cc.parse_budget_answer({"type": "cancelled"}) is None
        assert cc.parse_budget_answer(None) is None

    async def test_ask_budget_raise_uses_the_ask_user_widget(self) -> None:
        seen: list[list[dict[str, Any]]] = []

        async def request_ask_user(
            questions: list[dict[str, Any]],
        ) -> asyncio.Future[Any]:
            seen.append(questions)
            fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
            fut.set_result({"type": "answered", "answers": ["25"]})
            return fut

        payload = {"type": "budget_reached", "spent_usd": 1.2345, "budget_usd": 1.0}
        assert await cc.ask_budget_raise(request_ask_user, payload) == 25.0
        assert seen[0][0]["type"] == "text"
        assert "$1.2345" in seen[0][0]["question"]
        assert "$1.00" in seen[0][0]["question"]

    async def test_no_ui_or_failure_stops_the_turn(self) -> None:
        assert await cc.ask_budget_raise(None, {"spent_usd": 1}) is None

        async def broken(_q: list[dict[str, Any]]) -> asyncio.Future[Any]:
            raise RuntimeError("no widget")

        assert await cc.ask_budget_raise(broken, {"spent_usd": 1}) is None
        assert "/cost budget" in cc.budget_stop_message({"spent_usd": 2.0})


class TestCostCommand:
    def test_budget_subcommand_sets_the_override(self) -> None:
        app = SimpleNamespace(_budget_override=None, _model_override=None)
        assert "unlimited" in cc.handle_cost_subcommand(app, "/cost budget")
        assert "$5.00" in cc.handle_cost_subcommand(app, "/cost budget 5")
        assert app._budget_override == 5.0
        assert "lifted" in cc.handle_cost_subcommand(app, "/cost budget off")
        assert app._budget_override == 0.0
        assert "Invalid" in cc.handle_cost_subcommand(app, "/cost budget abc")
        assert "positive" in cc.handle_cost_subcommand(app, "/cost budget -2")
        assert cc.handle_cost_subcommand(app, "/cost") is None
        assert cc.handle_cost_subcommand(app, "/tokens") is None

    def test_caps_and_today(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BOG_AGENTS_MAX_WEB_SEARCHES", "7")
        app = SimpleNamespace(_budget_override=None, _model_override="m")
        text = cc.handle_cost_subcommand(app, "/cost caps")
        assert "max_web_searches:        7" in text
        assert (
            cc.handle_cost_subcommand(app, "/cost today") == "No spend recorded today."
        )
        cc.get_spend_ledger().record(SCOPE_USER, 0.5)
        assert "user: $0.5000" in cc.handle_cost_subcommand(app, "/cost today")

    def test_render_report_without_and_with_usage(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings = Mock(
            model_name="anthropic:claude-sonnet-4-6", model_context_limit=200_000
        )
        monkeypatch.setattr("bog_agents_cli.config.settings", settings)
        app = SimpleNamespace(
            _token_tracker=None, _session_stats=_stats(), _budget_override=None
        )
        text = cc.render_tokens_report(app, None)
        assert text.startswith("No token usage yet")
        assert "Session spend: $3.0000 | budget: unlimited" in text
        app = SimpleNamespace(
            _token_tracker=SimpleNamespace(current_context=50_000),
            _session_stats=SessionStats(),
            _budget_override=4.0,
        )
        text = cc.render_tokens_report(app, 30_000)
        assert "(25%)" in text
        assert "Conversation: ~30" in text
        assert "budget: $4.00 (/cost budget)" in text


class TestPreflight:
    def test_below_threshold_starts_immediately(self) -> None:
        started: list[str] = []
        app = SimpleNamespace(
            _model_override="ollama:unpriced",
            _start_tracked_session=lambda coro, *, name: (
                started.append(name) or coro.close()
            ),
            push_screen=lambda *_a, **_k: pytest.fail("no modal expected"),
        )

        async def _session() -> None:
            return None

        cc.preflight_start(app, agents=8, name="/best-of-n", start=_session)
        assert started == ["/best-of-n"]

    def test_above_threshold_confirms_first(self) -> None:
        pushed: list[Any] = []
        started: list[str] = []
        app = SimpleNamespace(
            _model_override=MODEL,
            _start_tracked_session=lambda coro, *, name: (
                started.append(name) or coro.close()
            ),
            push_screen=lambda screen, cb: pushed.append((screen, cb)),
        )

        async def _session() -> None:
            return None

        cc.preflight_start(
            app,
            agents=4,
            name="/team run",
            start=_session,
            caps=cc.CostCaps(preflight_threshold_usd=1.0),
        )
        assert started == []
        screen, callback = pushed[0]
        assert screen._name == "/team run"
        assert any("projected $" in line for line in screen._lines)
        callback(True)
        assert started == ["/team run"]

    def test_message_lines(self) -> None:
        assert (
            cc.preflight_message(3, MODEL, cc.CostCaps(preflight_threshold_usd=None))
            is None
        )
        assert (
            cc.preflight_message(3, MODEL, cc.CostCaps(preflight_threshold_usd=100.0))
            is None
        )
        lines = cc.preflight_message(
            3, MODEL, cc.CostCaps(preflight_threshold_usd=1.0, budget_usd=5.0)
        )
        assert lines is not None
        assert lines[0].startswith("3 agent run(s)")
        assert any("Session budget: $5.00" in line for line in lines)


class TestWebSearchCap:
    def test_refused_once_the_cap_is_hit(self) -> None:
        from bog_agents_cli import tools

        tools.set_web_search_ledger(CostLedger(caps=RunawayCaps(max_web_searches=0)))
        try:
            out = tools.web_search("anything")
        finally:
            tools.set_web_search_ledger(None)
        assert isinstance(out, dict)
        assert "Web search refused" in out["error"]
        assert out["query"] == "anything"


class TestAgentWiring:
    def test_cli_agent_gets_ledger_budget_and_interrupt_mode(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from bog_agents.middleware.cost_tracker import CostTrackerMiddleware
        from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
        from langchain_core.messages import AIMessage

        from bog_agents_cli.agent import create_cli_agent
        from bog_agents_cli.config import Settings

        monkeypatch.setenv("BOG_AGENTS_BUDGET_USD", "1.5")
        monkeypatch.setenv("BOG_AGENTS_MAX_SUBAGENTS", "2")
        (tmp_path / "agent").mkdir(exist_ok=True)
        (tmp_path / "skills").mkdir(exist_ok=True)
        mock_settings = Mock()
        mock_settings.ensure_agent_dir.return_value = tmp_path / "agent"
        mock_settings.ensure_user_skills_dir.return_value = tmp_path / "skills"
        mock_settings.get_project_skills_dir.return_value = None
        mock_settings.get_built_in_skills_dir.return_value = (
            Settings.get_built_in_skills_dir()
        )
        mock_settings.get_user_agent_md_path.return_value = (
            tmp_path / "agent" / "AGENTS.md"
        )
        mock_settings.get_project_agent_md_path.return_value = []
        mock_settings.get_user_agents_dir.return_value = tmp_path / "agents"
        mock_settings.get_project_agents_dir.return_value = None
        mock_settings.model_name = None
        mock_settings.model_provider = None
        mock_settings.model_context_limit = None
        mock_agent = Mock()
        mock_agent.with_config.return_value = mock_agent
        fake_model = GenericFakeChatModel(messages=iter([AIMessage(content="ok")]))
        with (
            patch.dict(
                "os.environ", {"BOG_AGENTS_HOME": str(tmp_path / "home")}, clear=False
            ),
            patch("bog_agents_cli.agent.settings", mock_settings),
            patch("bog_agents_cli.agent.SkillsMiddleware"),
            patch("bog_agents_cli.agent.MemoryMiddleware"),
            patch("bog_agents_cli.agent.LocalShellBackend"),
            patch("bog_agents_cli.agent.FilesystemBackend"),
            patch(
                "bog_agents_cli.agent.create_agent", return_value=mock_agent
            ) as mock_create,
            patch(
                "bog_agents_cli.config.create_model",
                return_value=Mock(model=fake_model),
            ),
            patch("bog_agents_cli.agent.get_system_prompt", return_value=""),
        ):
            create_cli_agent(
                model="fake-model",
                assistant_id="test",
                enable_memory=False,
                enable_skills=False,
                enable_shell=False,
                interactive=False,
                cwd=str(tmp_path),
            )
        _, kwargs = mock_create.call_args
        ledger = kwargs["cost_ledger"]
        assert isinstance(ledger, CostLedger)
        assert ledger.caps == RunawayCaps(
            max_subagents=2, max_web_searches=50, max_cost_usd=1.5
        )
        trackers = [
            m for m in kwargs["middleware"] if isinstance(m, CostTrackerMiddleware)
        ]
        assert len(trackers) == 1
        assert trackers[0].tracker.budget_usd == 1.5
        assert trackers[0]._on_budget == "interrupt"
