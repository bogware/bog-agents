"""ROADMAP #53: cost-objective routing, the decisions log, its bias, the /cost counterfactual and failover wiring."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from bog_agents_cli import operator_decisions as od, operator_mode as om

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture(autouse=True)
def _isolated_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    log = tmp_path / "operator-decisions.jsonl"
    monkeypatch.setattr(od, "decisions_path", lambda: log)
    monkeypatch.setattr(om, "operator_config_path", lambda: tmp_path / "operator.toml")
    monkeypatch.delenv("BOG_AGENTS_OPERATOR", raising=False)
    monkeypatch.delenv("BOG_AGENTS_OPERATOR_DISABLE", raising=False)
    return log


class _DummyApp:
    def __init__(self) -> None:
        self.mounted: list[str] = []
        self._model_override: str | None = None
        self._profile_override = None
        self._session_stats = SimpleNamespace(input_tokens=0, output_tokens=0)

    async def _mount_message(self, message: object) -> None:
        text = (
            getattr(message, "_content", None)
            or getattr(message, "content", None)
            or str(message)
        )
        self.mounted.append(str(text))


class TestObjective:
    def test_apply_objective_steps_and_respects_bias(self) -> None:
        assert od.apply_objective("hard", "cost") == "medium"
        assert od.apply_objective("easy", "cost") == "easy"
        assert od.apply_objective("medium", "intelligence") == "hard"
        assert od.apply_objective("max", "intelligence") == "max"
        assert od.apply_objective("hard", "balance") == "hard"
        assert od.apply_objective("hard", "cost", blocked=frozenset({"hard"})) == "hard"
        assert od.apply_objective("weird", "cost") == "weird"

    def test_config_parses_objective_and_pool(self, tmp_path: Path) -> None:
        (tmp_path / "operator.toml").write_text(
            'objective = "cost"\n[pool.judge]\nmodel = "ollama:llama3.2"\neffort = "low"\n[pool.subagent]\nmodel = "ollama:qwen3"\n',
            encoding="utf-8",
        )
        cfg = om.load_operator_config(tmp_path / "operator.toml")
        assert cfg.objective == "cost"
        assert (
            cfg.pool["judge"].model == "ollama:llama3.2"
            and cfg.pool["judge"].effort == "low"
        )
        assert cfg.pool["subagent"].model == "ollama:qwen3"
        bad = tmp_path / "bad.toml"
        bad.write_text('objective = "yolo"\n', encoding="utf-8")
        assert om.load_operator_config(bad).objective == "balance"
        session = om.OperatorSession(
            config=cfg, tiers=om.resolve_tiers(cfg), active=True
        )
        assert om._judge_model_spec(session, _DummyApp()) == "ollama:llama3.2"

    def test_default_config_mentions_objective_and_parses(self, tmp_path: Path) -> None:
        import tomllib

        path = om.write_default_config(tmp_path / "operator.toml")
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        assert data["objective"] == "balance"

    async def test_judge_applies_objective(self) -> None:
        tiers = om.resolve_tiers(om.OperatorConfig())

        async def invoke(_system: str, _user: str) -> str:
            return '{"tier": "hard", "route": "direct", "reason": "meaty"}'

        cost = await om.judge_prompt(
            "do the thing", tiers, invoke=invoke, objective="cost"
        )
        assert (
            cost is not None
            and cost.tier == "medium"
            and cost.judged_tier == "hard"
            and cost.model == tiers["medium"].model
        )
        smart = await om.judge_prompt(
            "do the thing", tiers, invoke=invoke, objective="intelligence"
        )
        assert smart is not None and smart.tier == "max"
        held = await om.judge_prompt(
            "do the thing",
            tiers,
            invoke=invoke,
            objective="cost",
            blocked=frozenset({"hard"}),
        )
        assert held is not None and held.tier == "hard"


class TestDecisionsLog:
    def test_record_update_and_bias(self, _isolated_log: Path) -> None:
        assert od.load_decisions() == []
        for verdict in ("bad", "bad", "good"):
            rec = od.record_decision(
                od.DecisionRecord(
                    judged_tier="hard",
                    tier="medium",
                    objective="cost",
                    model="m",
                    judged_model="j",
                )
            )
            assert od.update_decision(rec.decision_id, verdict=verdict)
        assert od.bias() == frozenset({"hard"})
        assert od.bias(min_samples=4) == frozenset()
        assert od.bias(bad_ratio=0.7) == frozenset()
        assert not od.update_decision("nope", verdict="bad")
        records = od.load_decisions()
        assert (
            len(records) == 3 and records[0].downgraded and records[0].verdict == "bad"
        )
        _isolated_log.write_text(
            _isolated_log.read_text(encoding="utf-8") + "{not json\n", encoding="utf-8"
        )
        assert len(od.load_decisions()) == 3

    def test_counterfactual_prices_the_judged_model(self) -> None:
        assert od.counterfactual_line() is None
        priced = od.estimate_cost_usd("anthropic:claude-opus-4-6", 1_000_000, 0)
        assert priced is not None and priced > 0
        assert od.estimate_cost_usd("ollama:llama3.2", 5, 5) == 0.0
        rec = od.record_decision(
            od.DecisionRecord(
                judged_tier="hard",
                tier="easy",
                objective="cost",
                model="ollama:llama3.2",
                judged_model="anthropic:claude-opus-4-6",
            )
        )
        od.update_decision(
            rec.decision_id, input_tokens=1_000_000, output_tokens=0, cost_usd=0.0
        )
        same = od.record_decision(
            od.DecisionRecord(
                judged_tier="hard",
                tier="hard",
                model="anthropic:claude-opus-4-6",
                judged_model="anthropic:claude-opus-4-6",
            )
        )
        od.update_decision(
            same.decision_id, input_tokens=10, output_tokens=10, cost_usd=0.001
        )
        saved, routed, local = od.counterfactual()
        assert saved == pytest.approx(priced) and routed == 1 and local == 1
        line = od.counterfactual_line()
        assert line is not None and "1 turn(s)" in line and "1 to local" in line


class TestSessionHooks:
    async def test_routing_persists_and_turn_outcome_fills_tokens(self) -> None:
        app = _DummyApp()
        cfg = om.OperatorConfig()
        session = om.OperatorSession(
            config=cfg, tiers=om.resolve_tiers(cfg), active=True, objective="cost"
        )
        app._operator_session = session  # type: ignore[attr-defined]
        session.forced_tier = "hard"
        decision = await om.apply_operator_routing(app, "refactor everything")
        assert decision is not None and decision.decision_id
        records = od.load_decisions()
        assert len(records) == 1 and records[0].decision_id == decision.decision_id

        app._session_stats = SimpleNamespace(input_tokens=1200, output_tokens=300)
        om.operator_turn_finished(app)
        after = od.load_decisions()[0]
        assert (after.input_tokens, after.output_tokens) == (1200, 300)
        om.operator_turn_finished(app)  # no snapshot left: no-op
        assert od.load_decisions()[0].input_tokens == 1200

    async def test_objective_and_verdict_subcommands(self) -> None:
        app = _DummyApp()
        cfg = om.OperatorConfig()
        session = om.OperatorSession(
            config=cfg, tiers=om.resolve_tiers(cfg), active=True
        )
        app._operator_session = session  # type: ignore[attr-defined]
        await om.handle_operator_subcommand(app, "objective bogus")
        assert "Usage: /operator objective" in app.mounted[-1]
        await om.handle_operator_subcommand(app, "objective cost")
        assert session.objective == "cost" and "step down" in app.mounted[-1]
        await om.handle_operator_subcommand(app, "verdict bad")
        assert "Usage: /operator verdict" in app.mounted[-1]  # nothing routed yet
        session.forced_tier = "hard"
        await om.apply_operator_routing(app, "hello")
        await om.handle_operator_subcommand(app, "verdict bad too slow")
        assert "Recorded [bold]bad[/bold]" in app.mounted[-1]
        last = od.load_decisions()[-1]
        assert last.verdict == "bad" and last.note == "too slow"
        status = om.render_status(session)
        assert "objective: cost" in status

    def test_failover_middleware_attaches_when_fallbacks_configured(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from bog_agents.token_audit import audit_agent, capture_assembly

        from bog_agents_cli import agent as agent_mod

        monkeypatch.setattr(
            agent_mod, "_configured_fallbacks", lambda: ["ollama:llama3.2"]
        )
        names: list[str] = []

        def _build(model: object) -> object:
            with capture_assembly(
                lambda a: names.extend(type(m).__name__ for m in a.middleware)
            ):
                return agent_mod.create_cli_agent(
                    model=model, assistant_id="agent", cwd=tmp_path
                )  # type: ignore[arg-type]

        audit_agent(_build, method="approx")
        assert "ProviderFailoverMiddleware" in names
        assert "BedrockResilienceMiddleware" not in names
