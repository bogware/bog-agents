"""Tests for the dreamscape feature set.

The most important assertion in this file is the *opt-in default*:
without a ``~/.bog-agents/dreamscape.toml`` (or with ``enabled=false``),
**no dreamscape middleware attaches and no behavior changes**. Every
other test confirms that when you DO opt in, the feature works.

Tests:
* Config loader handles missing file / malformed file / env-var
  overrides / emergency disable.
* Lifecycle state machine transitions are deterministic and correct.
* Laws rule parser handles the comment / bullet / heading shapes.
* Laws audit catches "never X" phrases without false-rejecting safe text.
* Shared memory SQLite round-trip + redaction.
* Dream-engine seed picker is deterministic with seeded rng.
* Imagination middleware respects the threshold + auto-disable.
* Slash-command registry: new commands present, handlers wired.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Config + master switch — the single most-important opt-in invariant
# ---------------------------------------------------------------------------


class TestConfigDefaults:
    """With no file + no env vars, everything is OFF and behavior is unchanged."""

    @pytest.fixture(autouse=True)
    def _clear_cache(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Every test gets a fresh config cache and a clean env."""
        for var in (
            "BOG_AGENTS_DREAMSCAPE",
            "BOG_AGENTS_DREAMSCAPE_DISABLE",
            "BOG_AGENTS_DREAMSCAPE_LIFECYCLE",
            "BOG_AGENTS_DREAMSCAPE_LAWS",
            "BOG_AGENTS_DREAMSCAPE_SHARED_MEMORY",
            "BOG_AGENTS_DREAMSCAPE_DREAMS_AUTO",
            "BOG_AGENTS_DREAMSCAPE_IMAGINATION",
        ):
            monkeypatch.delenv(var, raising=False)
        from bog_agents_cli.dreamscape import config as ds_config

        ds_config.clear_cache()

    def test_default_master_off(self, tmp_path: Path) -> None:
        from bog_agents_cli.dreamscape import load_dreamscape_config

        cfg = load_dreamscape_config(path=tmp_path / "absent.toml")
        assert cfg.master_enabled is False
        assert cfg.lifecycle.enabled is False
        assert cfg.laws.enabled is False
        assert cfg.shared_memory.enabled is False
        assert cfg.dreams.auto_on_dormancy is False
        assert cfg.imagination.enabled is False
        # Dashboard defaults ON because it's read-only — that's intentional.
        assert cfg.dashboard.enabled is True
        # any_active is the gate the wiring layer reads — it MUST be False.
        assert cfg.any_active is False

    def test_master_off_means_any_active_false_even_with_subfeatures_on(
        self, tmp_path: Path
    ) -> None:
        """The master switch overrides per-feature toggles."""
        from bog_agents_cli.dreamscape import (
            load_dreamscape_config,
            save_dreamscape_config,
        )

        cfg = load_dreamscape_config(path=tmp_path / "ds.toml")
        cfg.master_enabled = False
        cfg.lifecycle.enabled = True
        cfg.laws.enabled = True
        cfg.imagination.enabled = True
        save_dreamscape_config(cfg, path=tmp_path / "ds.toml")
        # Cache is cleared by save; reload from same path.
        reloaded = load_dreamscape_config(path=tmp_path / "ds.toml")
        assert reloaded.any_active is False

    def test_emergency_disable_wins(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``BOG_AGENTS_DREAMSCAPE_DISABLE=1`` forces everything off."""
        from bog_agents_cli.dreamscape import (
            load_dreamscape_config,
            save_dreamscape_config,
        )

        cfg = load_dreamscape_config(path=tmp_path / "ds.toml")
        cfg.master_enabled = True
        cfg.lifecycle.enabled = True
        cfg.laws.enabled = True
        save_dreamscape_config(cfg, path=tmp_path / "ds.toml")
        monkeypatch.setenv("BOG_AGENTS_DREAMSCAPE_DISABLE", "1")
        from bog_agents_cli.dreamscape import config as ds_config

        ds_config.clear_cache()
        reloaded = load_dreamscape_config(path=tmp_path / "ds.toml")
        assert reloaded.master_enabled is False
        assert reloaded.lifecycle.enabled is False
        assert reloaded.laws.enabled is False

    def test_malformed_file_falls_back_to_defaults(self, tmp_path: Path) -> None:
        from bog_agents_cli.dreamscape import load_dreamscape_config

        bad = tmp_path / "ds.toml"
        bad.write_text("this is = not toml = at all]]]", encoding="utf-8")
        cfg = load_dreamscape_config(path=bad)
        assert cfg.master_enabled is False

    def test_env_var_overrides_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from bog_agents_cli.dreamscape import (
            load_dreamscape_config,
            save_dreamscape_config,
        )

        cfg = load_dreamscape_config(path=tmp_path / "ds.toml")
        cfg.master_enabled = False
        save_dreamscape_config(cfg, path=tmp_path / "ds.toml")
        monkeypatch.setenv("BOG_AGENTS_DREAMSCAPE", "1")
        from bog_agents_cli.dreamscape import config as ds_config

        ds_config.clear_cache()
        reloaded = load_dreamscape_config(path=tmp_path / "ds.toml")
        assert reloaded.master_enabled is True


# ---------------------------------------------------------------------------
# Lifecycle state machine
# ---------------------------------------------------------------------------


class TestLifecycleTransitions:
    def test_awake_when_recent_activity(self) -> None:
        from bog_agents_cli.dreamscape.config import LifecycleConfig
        from bog_agents_cli.dreamscape.lifecycle import (
            LifecycleSnapshot,
            LifecycleState,
            compute_state,
        )

        cfg = LifecycleConfig(enabled=True, dormancy_after_seconds=1800)
        snap = LifecycleSnapshot(agent_id="a", last_activity_at=1000.0)
        # 60s after activity = still well within the awake window
        assert compute_state(snap, cfg, now=1060.0) == LifecycleState.AWAKE

    def test_idle_after_some_silence(self) -> None:
        from bog_agents_cli.dreamscape.config import LifecycleConfig
        from bog_agents_cli.dreamscape.lifecycle import (
            LifecycleSnapshot,
            LifecycleState,
            compute_state,
        )

        cfg = LifecycleConfig(enabled=True, dormancy_after_seconds=1800)
        snap = LifecycleSnapshot(agent_id="a", last_activity_at=1000.0)
        # ~10 min after activity, less than dormancy threshold
        assert compute_state(snap, cfg, now=1000.0 + 600) == LifecycleState.IDLE

    def test_dormant_past_threshold(self) -> None:
        from bog_agents_cli.dreamscape.config import LifecycleConfig
        from bog_agents_cli.dreamscape.lifecycle import (
            LifecycleSnapshot,
            LifecycleState,
            compute_state,
        )

        cfg = LifecycleConfig(
            enabled=True,
            dormancy_after_seconds=1800,
            dreaming_after_dormant_seconds=600,
        )
        # Use a non-zero starting timestamp — ``last_activity_at=0.0``
        # is the "never observed" sentinel that compute_state treats as
        # "fresh, stay AWAKE".
        snap = LifecycleSnapshot(agent_id="a", last_activity_at=1.0)
        # 2 hours after activity → past dormancy_after_seconds.
        assert compute_state(snap, cfg, now=1.0 + 7200) == LifecycleState.DORMANT

    def test_transient_states_preserved(self) -> None:
        """DREAMING / IMAGINING are owned by other subsystems — don't time-travel out."""
        from bog_agents_cli.dreamscape.config import LifecycleConfig
        from bog_agents_cli.dreamscape.lifecycle import (
            LifecycleSnapshot,
            LifecycleState,
            compute_state,
        )

        cfg = LifecycleConfig(enabled=True, dormancy_after_seconds=1800)
        snap = LifecycleSnapshot(
            agent_id="a",
            last_activity_at=1.0,
            state=LifecycleState.DREAMING.value,
        )
        assert compute_state(snap, cfg, now=100_000.0) == LifecycleState.DREAMING

    def test_bump_imagination_caps_at_100(self) -> None:
        from bog_agents_cli.dreamscape.lifecycle import (
            LifecycleSnapshot,
            bump_imagination,
        )

        snap = LifecycleSnapshot(agent_id="a", imagination=99.9)
        bump_imagination(snap, increment=1.0)
        assert snap.imagination == 100.0
        bump_imagination(snap, increment=5.0)
        assert snap.imagination == 100.0  # still capped


class TestLifecycleSnapshotRoundtrip:
    def test_save_load(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from bog_agents_cli.dreamscape import lifecycle as lc_mod

        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path, raising=False)
        snap = lc_mod.LifecycleSnapshot(
            agent_id="alpha",
            imagination=12.5,
            total_dreams=4,
            last_activity_at=42.0,
        )
        lc_mod.save_snapshot(snap)
        loaded = lc_mod.load_snapshot("alpha")
        assert loaded.imagination == 12.5
        assert loaded.total_dreams == 4

    def test_missing_returns_fresh(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from bog_agents_cli.dreamscape import lifecycle as lc_mod

        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path, raising=False)
        fresh = lc_mod.load_snapshot("brand-new")
        assert fresh.agent_id == "brand-new"
        assert fresh.imagination == 0.0
        assert fresh.total_dreams == 0


# ---------------------------------------------------------------------------
# Laws + Constitution
# ---------------------------------------------------------------------------


class TestLawsParser:
    def test_skips_comments_and_blanks(self, tmp_path: Path) -> None:
        from bog_agents_cli.dreamscape.laws import _read_rule_file

        path = tmp_path / "laws.md"
        path.write_text(
            "# This is a heading, skip me\n"
            "\n"
            "- Never run rm -rf /\n"
            "// also skip me (comment)\n"
            "* Must include tests for new behavior\n",
            encoding="utf-8",
        )
        rules = _read_rule_file(path)
        assert len(rules) == 2
        assert "rm -rf" in rules[0].text
        assert "tests" in rules[1].text

    def test_extract_phrases_from_never(self) -> None:
        from bog_agents_cli.dreamscape.laws import _extract_key_phrases

        phrases = _extract_key_phrases("Never run rm -rf / or recursive deletes")
        assert any("rm -rf" in p for p in phrases)

    def test_audit_catches_phrase_in_sample(self, tmp_path: Path) -> None:
        from bog_agents_cli.dreamscape.config import LawsConfig
        from bog_agents_cli.dreamscape.laws import audit_text, write_default_templates

        cfg = LawsConfig(
            laws_path=str(tmp_path / "laws.md"),
            constitution_path=str(tmp_path / "constitution.md"),
        )
        write_default_templates(cfg, project_root=tmp_path, overwrite=True)
        result = audit_text(
            "I'm going to rm -rf / the workspace.", cfg, project_root=tmp_path
        )
        assert result.laws_found > 0
        assert any("rm -rf" in v for v in result.violations)

    def test_audit_clean_sample_no_violations(self, tmp_path: Path) -> None:
        from bog_agents_cli.dreamscape.config import LawsConfig
        from bog_agents_cli.dreamscape.laws import audit_text, write_default_templates

        cfg = LawsConfig(
            laws_path=str(tmp_path / "laws.md"),
            constitution_path=str(tmp_path / "constitution.md"),
        )
        write_default_templates(cfg, project_root=tmp_path, overwrite=True)
        result = audit_text(
            "Here is a nice safe response with no violations.",
            cfg,
            project_root=tmp_path,
        )
        assert result.violations == []


# ---------------------------------------------------------------------------
# Shared memory
# ---------------------------------------------------------------------------


class TestSharedMemorySQLite:
    def test_round_trip(self, tmp_path: Path) -> None:
        from bog_agents_cli.dreamscape.shared_memory import SQLiteSharedMemory

        backend = SQLiteSharedMemory(tmp_path / "shared.db")
        entry = backend.write(
            agent_id="alpha", content="hello world", tags=["greeting"]
        )
        assert entry is not None
        assert entry.content == "hello world"
        found = backend.search("hello")
        assert any(e.content == "hello world" for e in found)
        recent = backend.recent(limit=10)
        assert len(recent) == 1
        assert recent[0].tags == ["greeting"]

    def test_redaction(self) -> None:
        from bog_agents_cli.dreamscape.shared_memory import redact_secrets

        redacted = redact_secrets(
            "my key is sk-abcdef1234567890abcdef and that's secret",
            patterns=[r"sk-[A-Za-z0-9]{16,}"],
        )
        assert "[redacted]" in redacted
        assert "sk-abcdef" not in redacted


# ---------------------------------------------------------------------------
# Dream engine — seed library
# ---------------------------------------------------------------------------


class TestDreamSeeds:
    def test_deterministic_with_seed(self) -> None:
        import random

        from bog_agents_cli.dreamscape.seeds import pick_seeds

        rng1 = random.Random(42)
        rng2 = random.Random(42)
        picks1 = pick_seeds(["nature", "space"], count=2, rng=rng1)
        picks2 = pick_seeds(["nature", "space"], count=2, rng=rng2)
        assert picks1 == picks2

    def test_handles_empty_categories(self) -> None:
        from bog_agents_cli.dreamscape.seeds import pick_seeds

        # Empty categories means "draw from all"
        picks = pick_seeds([], count=3)
        assert len(picks) == 3

    def test_unknown_category_yields_no_seed(self) -> None:
        from bog_agents_cli.dreamscape.seeds import pick_seeds

        picks = pick_seeds(["totally-fake-category"], count=3)
        assert picks == []


class TestDreamExcerptSampling:
    def test_sample_returns_excerpts_from_per_agent_log(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from bog_agents_cli.dreamscape import dream_engine

        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path, raising=False)
        agent_dir = tmp_path / ".bog-agents" / "agents" / "alpha" / "dreams"
        agent_dir.mkdir(parents=True, exist_ok=True)
        (agent_dir / "20260512-100000.md").write_text(
            "---\nkind: dream-auto\n---\n\n"
            "### Tonight I dreamed of glaciers\n\n"
            "Slow rivers of ice that remember every winter, "
            "carving valleys without hands.\n",
            encoding="utf-8",
        )
        excerpts = dream_engine.sample_dream_excerpts("alpha", count=1, rng_seed=1)
        assert len(excerpts) == 1
        assert "glaciers" in excerpts[0].lower()

    def test_sample_empty_when_no_dreams(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from bog_agents_cli.dreamscape.dream_engine import sample_dream_excerpts

        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path, raising=False)
        assert sample_dream_excerpts("nobody-here", count=3) == []


# ---------------------------------------------------------------------------
# Imagination middleware — gating logic
# ---------------------------------------------------------------------------


class TestImaginationGating:
    def test_disabled_when_trait_below_threshold(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from bog_agents_cli.dreamscape import lifecycle as lc_mod
        from bog_agents_cli.dreamscape.config import ImaginationConfig
        from bog_agents_cli.dreamscape.imagination import ImaginationMiddleware

        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path, raising=False)
        snap = lc_mod.LifecycleSnapshot(
            agent_id="alpha", imagination=0.0, consecutive_tool_failures=10
        )
        lc_mod.save_snapshot(snap)

        mw = ImaginationMiddleware(
            agent_id="alpha",
            cfg=ImaginationConfig(
                enabled=True,
                trigger_after_failures=3,
                min_imagination_trait=1.0,
            ),
        )
        assert mw._should_inject() is False

    def test_active_with_failures_and_trait(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from bog_agents_cli.dreamscape import lifecycle as lc_mod
        from bog_agents_cli.dreamscape.config import ImaginationConfig
        from bog_agents_cli.dreamscape.imagination import ImaginationMiddleware

        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path, raising=False)
        snap = lc_mod.LifecycleSnapshot(
            agent_id="beta", imagination=5.0, consecutive_tool_failures=5
        )
        lc_mod.save_snapshot(snap)
        mw = ImaginationMiddleware(
            agent_id="beta",
            cfg=ImaginationConfig(
                enabled=True,
                trigger_after_failures=3,
                min_imagination_trait=1.0,
            ),
        )
        assert mw._should_inject() is True

    def test_auto_disable_below_success_rate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from bog_agents_cli.dreamscape import lifecycle as lc_mod
        from bog_agents_cli.dreamscape.config import ImaginationConfig
        from bog_agents_cli.dreamscape.imagination import ImaginationMiddleware

        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path, raising=False)
        snap = lc_mod.LifecycleSnapshot(
            agent_id="gamma",
            imagination=50.0,
            consecutive_tool_failures=10,
            imagination_injections=20,
            imagination_injections_helped=3,  # 15% success — below the floor
        )
        lc_mod.save_snapshot(snap)
        mw = ImaginationMiddleware(
            agent_id="gamma",
            cfg=ImaginationConfig(
                enabled=True,
                trigger_after_failures=3,
                min_imagination_trait=1.0,
                auto_disable_below_success_rate=0.4,
            ),
        )
        assert mw._should_inject() is False


# ---------------------------------------------------------------------------
# Slash-command wiring smoke test
# ---------------------------------------------------------------------------


class TestSlashCommandsRegistered:
    def test_all_dreamscape_commands_present(self) -> None:
        from bog_agents_cli.commands import COMMANDS

        names = {c.spec.name for c in COMMANDS}
        for expected in (
            "/agent-state",
            "/repo",
            "/dreamscape",
            "/laws",
            "/help-dream",
        ):
            assert expected in names, f"missing: {expected}"

    def test_all_handlers_resolve_on_app(self) -> None:
        from bog_agents_cli.app import BogAgentsApp
        from bog_agents_cli.commands import COMMANDS

        for c in COMMANDS:
            if c.handler_method:
                assert hasattr(BogAgentsApp, c.handler_method), (
                    f"{c.spec.name} → BogAgentsApp.{c.handler_method} missing"
                )


# ---------------------------------------------------------------------------
# The headline opt-in invariant: agent.py does NOT attach any dreamscape
# middleware when master_enabled=False.
# ---------------------------------------------------------------------------


class TestAgentWiringRespectsOptIn:
    def test_any_active_false_means_no_middleware(self, tmp_path: Path) -> None:
        """Spot-check the gate ``agent.py`` uses to decide attachment."""
        from bog_agents_cli.dreamscape import load_dreamscape_config

        cfg = load_dreamscape_config(path=tmp_path / "absent.toml")
        # If this is False, `agent.py` short-circuits the attach.
        assert cfg.any_active is False

    def test_attach_helper_runs_clean(self, tmp_path: Path) -> None:
        """The attach helper itself is a no-throw best-effort call."""
        from bog_agents_cli.agent import _attach_dreamscape_middleware
        from bog_agents_cli.dreamscape import load_dreamscape_config

        cfg = load_dreamscape_config(path=tmp_path / "absent.toml")
        cfg.master_enabled = True
        cfg.lifecycle.enabled = True
        middleware: list = []
        _attach_dreamscape_middleware(middleware, cfg=cfg, agent_id="alpha")
        # At least the lifecycle middleware should have been appended.
        assert len(middleware) == 1
        # And the master switch off path appends nothing:
        cfg2 = load_dreamscape_config(path=tmp_path / "absent2.toml")
        middleware2: list = []
        # When master is off, callers should not invoke this — but if
        # someone does, it should still process per-feature flags.
        # We pass with lifecycle disabled to confirm nothing attaches.
        _attach_dreamscape_middleware(middleware2, cfg=cfg2, agent_id="beta")
        assert middleware2 == []


# Re-export to keep `os` usage live for the env-var manipulation tests.
_ = os
