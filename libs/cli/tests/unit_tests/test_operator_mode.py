"""Tests for operator mode (judge-model prompt routing)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bog_agents_cli.operator_mode import (
    BUILTIN_PRESETS,
    DEFAULT_PRESET,
    TIER_NAMES,
    OperatorConfig,
    OperatorSession,
    TierSpec,
    apply_operator_routing,
    ensure_session,
    handle_operator_subcommand,
    judge_prompt,
    load_operator_config,
    parse_judge_response,
    render_status,
    resolve_tiers,
    write_default_config,
)


class _DummyApp:
    """Minimal stand-in for BogAgentsApp: collects mounted messages."""

    def __init__(self) -> None:
        self.mounted: list[str] = []
        self._model_override: str | None = None
        self._profile_override = None

    async def _mount_message(self, message: object) -> None:
        # Chat message widgets stash their body on the first ctor arg;
        # str() of the widget object isn't stable, so grab the raw text.
        text = (
            getattr(message, "_content", None)
            or getattr(message, "content", None)
            or str(message)
        )
        self.mounted.append(str(text))


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BOG_AGENTS_OPERATOR", raising=False)
    monkeypatch.delenv("BOG_AGENTS_OPERATOR_DISABLE", raising=False)


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


class TestConfig:
    def test_defaults(self, tmp_path: Path) -> None:
        cfg = load_operator_config(tmp_path / "missing.toml")
        assert cfg.enabled is False
        assert cfg.preset == DEFAULT_PRESET
        assert cfg.routes is True
        assert cfg.judge_model == ""

    def test_full_toml(self, tmp_path: Path) -> None:
        path = tmp_path / "operator.toml"
        path.write_text(
            """
enabled = true
judge_model = "ollama:llama3.2"
preset = "mine"
routes = false

[presets.mine.easy]
model = "ollama:llama3.2"
effort = "low"

[presets.mine.max]
model = "anthropic:claude-opus-4-6"
effort = "max"

[tiers.hard]
model = "anthropic:claude-sonnet-4-6"
effort = "high"
""",
            encoding="utf-8",
        )
        cfg = load_operator_config(path)
        assert cfg.enabled is True
        assert cfg.judge_model == "ollama:llama3.2"
        assert cfg.preset == "mine"
        assert cfg.routes is False
        assert cfg.custom_presets["mine"]["easy"] == TierSpec("ollama:llama3.2", "low")
        assert cfg.custom_presets["mine"]["max"] == TierSpec(
            "anthropic:claude-opus-4-6", "max"
        )
        assert cfg.tier_overrides["hard"] == TierSpec(
            "anthropic:claude-sonnet-4-6", "high"
        )

    def test_malformed_toml_falls_back(self, tmp_path: Path) -> None:
        path = tmp_path / "operator.toml"
        path.write_text("enabled = [not toml", encoding="utf-8")
        cfg = load_operator_config(path)
        assert cfg.enabled is False
        assert cfg.preset == DEFAULT_PRESET

    def test_bad_effort_normalised(self, tmp_path: Path) -> None:
        path = tmp_path / "operator.toml"
        path.write_text(
            '[tiers.easy]\nmodel = "x:y"\neffort = "ludicrous"\n', encoding="utf-8"
        )
        cfg = load_operator_config(path)
        assert cfg.tier_overrides["easy"].effort == "medium"

    def test_env_master_overrides_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / "operator.toml"
        path.write_text("enabled = false\n", encoding="utf-8")
        monkeypatch.setenv("BOG_AGENTS_OPERATOR", "1")
        assert load_operator_config(path).enabled is True
        monkeypatch.setenv("BOG_AGENTS_OPERATOR", "0")
        path.write_text("enabled = true\n", encoding="utf-8")
        assert load_operator_config(path).enabled is False

    def test_write_default_config_idempotent(self, tmp_path: Path) -> None:
        path = tmp_path / "operator.toml"
        first = write_default_config(path)
        assert first.exists()
        body = first.read_text(encoding="utf-8")
        assert "enabled = false" in body
        # Second call must not clobber user edits.
        first.write_text("enabled = true\n", encoding="utf-8")
        write_default_config(path)
        assert first.read_text(encoding="utf-8") == "enabled = true\n"


# ---------------------------------------------------------------------------
# Tier resolution
# ---------------------------------------------------------------------------


class TestResolveTiers:
    def test_builtin_presets_cover_all_tiers(self) -> None:
        for name, tiers in BUILTIN_PRESETS.items():
            assert set(tiers) == set(TIER_NAMES), f"preset {name} incomplete"

    def test_unknown_preset_falls_back(self) -> None:
        tiers = resolve_tiers(OperatorConfig(preset="nope"))
        assert tiers == BUILTIN_PRESETS[DEFAULT_PRESET]

    def test_partial_custom_preset_inherits_defaults(self) -> None:
        cfg = OperatorConfig(
            preset="mine",
            custom_presets={"mine": {"max": TierSpec("x:y", "max")}},
        )
        tiers = resolve_tiers(cfg)
        assert tiers["max"] == TierSpec("x:y", "max")
        assert tiers["easy"] == BUILTIN_PRESETS[DEFAULT_PRESET]["easy"]

    def test_tier_overrides_beat_preset(self) -> None:
        cfg = OperatorConfig(
            preset="local", tier_overrides={"hard": TierSpec("z:q", "high")}
        )
        assert resolve_tiers(cfg)["hard"] == TierSpec("z:q", "high")
        assert resolve_tiers(cfg)["easy"] == BUILTIN_PRESETS["local"]["easy"]


# ---------------------------------------------------------------------------
# Judge parsing + judging
# ---------------------------------------------------------------------------


class TestParseJudgeResponse:
    def test_strict_json(self) -> None:
        reply = json.dumps({"tier": "hard", "route": "direct", "reason": "multi-file"})
        assert parse_judge_response(reply) == ("hard", "direct", "multi-file")

    def test_json_in_prose(self) -> None:
        reply = 'Sure! Here you go:\n{"tier": "easy", "route": "direct", "reason": ""}\nHope that helps!'
        assert parse_judge_response(reply) == ("easy", "direct", "")

    def test_bad_route_normalised_to_direct(self) -> None:
        reply = json.dumps({"tier": "max", "route": "teleport", "reason": "x"})
        assert parse_judge_response(reply) == ("max", "direct", "x")

    def test_bare_tier_word_fallback(self) -> None:
        assert parse_judge_response("I would call this one medium.") == (
            "medium",
            "direct",
            "",
        )

    def test_garbage_returns_none(self) -> None:
        assert parse_judge_response("beats me") is None

    def test_invalid_tier_in_json_falls_through_to_word_scan(self) -> None:
        reply = json.dumps({"tier": "impossible", "route": "direct", "reason": ""})
        assert parse_judge_response(reply) is None


class TestJudgePrompt:
    @pytest.fixture
    def tiers(self) -> dict[str, TierSpec]:
        return dict(BUILTIN_PRESETS[DEFAULT_PRESET])

    async def test_happy_path(self, tiers: dict[str, TierSpec]) -> None:
        async def invoke(_s: str, _u: str) -> str:
            return json.dumps({"tier": "hard", "route": "butcher", "reason": "big"})

        decision = await judge_prompt("refactor everything", tiers, invoke=invoke)
        assert decision is not None
        assert decision.tier == "hard"
        assert decision.route == "butcher"
        assert decision.model == tiers["hard"].model
        assert decision.effort == tiers["hard"].effort

    async def test_routes_disabled_forces_direct(
        self, tiers: dict[str, TierSpec]
    ) -> None:
        async def invoke(system: str, _u: str) -> str:
            assert '"route" to "direct"' in system
            return json.dumps({"tier": "easy", "route": "butcher", "reason": ""})

        decision = await judge_prompt("hi", tiers, invoke=invoke, routes_enabled=False)
        assert decision is not None
        assert decision.route == "direct"

    async def test_invoke_failure_returns_none(
        self, tiers: dict[str, TierSpec]
    ) -> None:
        async def invoke(_s: str, _u: str) -> str:
            msg = "model exploded"
            raise RuntimeError(msg)

        assert await judge_prompt("hi", tiers, invoke=invoke) is None

    async def test_unparseable_returns_none(self, tiers: dict[str, TierSpec]) -> None:
        async def invoke(_s: str, _u: str) -> str:
            return "no idea"

        assert await judge_prompt("hi", tiers, invoke=invoke) is None

    async def test_forced_tier_skips_judge(self, tiers: dict[str, TierSpec]) -> None:
        async def invoke(
            _s: str, _u: str
        ) -> str:  # pragma: no cover - must not be called
            msg = "judge must not run when forced"
            raise AssertionError(msg)

        decision = await judge_prompt("hi", tiers, invoke=invoke, forced_tier="max")
        assert decision is not None
        assert decision.forced is True
        assert decision.tier == "max"
        assert decision.judge_ms == 0

    async def test_forced_unknown_tier_returns_none(
        self, tiers: dict[str, TierSpec]
    ) -> None:
        async def invoke(_s: str, _u: str) -> str:
            return ""

        assert (
            await judge_prompt("hi", tiers, invoke=invoke, forced_tier="extreme")
            is None
        )


# ---------------------------------------------------------------------------
# Session + seam behaviour
# ---------------------------------------------------------------------------


class TestSessionAndSeam:
    def test_ensure_session_caches(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import bog_agents_cli.operator_mode as om

        monkeypatch.setattr(
            om, "operator_config_path", lambda: tmp_path / "operator.toml"
        )
        app = _DummyApp()
        first = ensure_session(app)
        assert ensure_session(app) is first
        assert first.active is False  # default config = off

    async def test_inactive_session_routes_nothing(self) -> None:
        app = _DummyApp()
        cfg = OperatorConfig()
        app._operator_session = OperatorSession(
            config=cfg, tiers=resolve_tiers(cfg), active=False
        )
        assert await apply_operator_routing(app, "anything") is None

    async def test_emergency_disable_wins(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BOG_AGENTS_OPERATOR_DISABLE", "1")
        app = _DummyApp()
        cfg = OperatorConfig(enabled=True)
        app._operator_session = OperatorSession(
            config=cfg, tiers=resolve_tiers(cfg), active=True
        )
        assert await apply_operator_routing(app, "anything") is None

    async def test_forced_tier_stages_turn_override(self) -> None:
        app = _DummyApp()
        cfg = OperatorConfig(enabled=True)
        session = OperatorSession(
            config=cfg, tiers=resolve_tiers(cfg), active=True, forced_tier="max"
        )
        app._operator_session = session
        decision = await apply_operator_routing(app, "do the thing")
        assert decision is not None
        assert decision.forced is True
        assert app._operator_turn_model == session.tiers["max"].model
        assert app._operator_turn_effort == session.tiers["max"].effort
        # Forcing is one-shot and the decision is logged.
        assert session.forced_tier is None
        assert list(session.decisions)[-1] is decision


# ---------------------------------------------------------------------------
# /operator subcommands (no model required for these paths)
# ---------------------------------------------------------------------------


class TestSubcommands:
    @pytest.fixture
    def app(self) -> _DummyApp:
        app = _DummyApp()
        cfg = OperatorConfig()
        app._operator_session = OperatorSession(
            config=cfg, tiers=resolve_tiers(cfg), active=False
        )
        return app

    async def test_status_renders(self, app: _DummyApp) -> None:
        await handle_operator_subcommand(app, "status")
        assert any("Operator mode" in m for m in app.mounted)
        assert any("Tier map" in m for m in app.mounted)

    async def test_off(self, app: _DummyApp) -> None:
        app._operator_session.active = True
        await handle_operator_subcommand(app, "off")
        assert app._operator_session.active is False

    async def test_on_reloads_config(
        self, app: _DummyApp, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import bog_agents_cli.operator_mode as om

        path = tmp_path / "operator.toml"
        path.write_text('preset = "local"\n', encoding="utf-8")
        monkeypatch.setattr(om, "operator_config_path", lambda: path)
        await handle_operator_subcommand(app, "on")
        assert app._operator_session.active is True
        assert app._operator_session.config.preset == "local"

    async def test_preset_switch_and_unknown(self, app: _DummyApp) -> None:
        await handle_operator_subcommand(app, "preset local")
        assert app._operator_session.config.preset == "local"
        assert app._operator_session.tiers["easy"] == BUILTIN_PRESETS["local"]["easy"]
        await handle_operator_subcommand(app, "preset doesnotexist")
        assert app._operator_session.config.preset == "local"  # unchanged

    async def test_force(self, app: _DummyApp) -> None:
        await handle_operator_subcommand(app, "force hard")
        assert app._operator_session.forced_tier == "hard"
        await handle_operator_subcommand(app, "force ridiculous")
        assert app._operator_session.forced_tier == "hard"  # unchanged

    async def test_unknown_subcommand_shows_usage(self, app: _DummyApp) -> None:
        await handle_operator_subcommand(app, "frobnicate")
        assert any("Usage" in m for m in app.mounted)

    def test_render_status_includes_decisions(self, app: _DummyApp) -> None:
        session = app._operator_session
        text = render_status(session)
        assert "No routing decisions yet" in text
