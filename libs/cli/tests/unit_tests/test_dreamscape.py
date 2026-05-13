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
        # Post-Phase-2 the phrases are stored in normalised form
        # (hyphens → spaces) so a plain ``rm -rf`` agent output also
        # matches a backtick-quoted rule. The extracted phrase is now
        # "rm rf" rather than "rm -rf"; pin both stems instead of the
        # exact separator.
        assert any("rm" in p and "rf" in p for p in phrases)

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
        # Same stem-check rationale as test_extract_phrases_from_never.
        assert any("rm" in v and "rf" in v for v in result.violations)

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


class TestLawsPhase2BugFixes:
    """Regression tests for the Phase-1-found bugs (1 & 2).

    See ``docs/DREAMSCAPE_TEST_REPORT.md`` §3 for the original repro
    cases. These pin the two bug fixes:

    * **Bug 1** — hyphen-vs-space normalisation: rule "force-push"
      must match agent output "force push".
    * **Bug 2** — paraphrase tolerance via Jaccard fallback + comma /
      conjunction splitting: rule "amend published commits" must
      match "amend the published commits"; rule with verb-list +
      object-list ("exfiltrate, log, or echo API keys, tokens, or
      session cookies") must catch single-verb single-object
      paraphrases ("exfiltrate API keys").
    """

    def _setup(self, tmp_path: Path):
        from bog_agents_cli.dreamscape.config import LawsConfig
        from bog_agents_cli.dreamscape.laws import write_default_templates

        cfg = LawsConfig(
            enabled=True,
            laws_path=str(tmp_path / ".bog-agents/laws.md"),
            constitution_path=str(tmp_path / ".bog-agents/constitution.md"),
        )
        write_default_templates(cfg, project_root=tmp_path, overwrite=True)
        return cfg

    def test_bug1_hyphen_force_push_matches_space_force_push(
        self, tmp_path: Path
    ) -> None:
        from bog_agents_cli.dreamscape.laws import audit_text

        cfg = self._setup(tmp_path)
        result = audit_text("force push to main", cfg, project_root=tmp_path)
        assert result.violations, "rule 'force-push' must match agent 'force push'"

    def test_bug2_stop_word_tolerance(self, tmp_path: Path) -> None:
        from bog_agents_cli.dreamscape.laws import audit_text

        cfg = self._setup(tmp_path)
        result = audit_text("amend the published commits", cfg, project_root=tmp_path)
        assert result.violations, (
            "rule 'amend published commits' must match the paraphrase "
            "with a 'the' inserted in the middle"
        )

    def test_bug2_verb_list_object_list_cross(self, tmp_path: Path) -> None:
        from bog_agents_cli.dreamscape.laws import audit_text

        cfg = self._setup(tmp_path)
        result = audit_text("exfiltrate API keys", cfg, project_root=tmp_path)
        assert result.violations, (
            "rule 'Never exfiltrate, log, or echo API keys, tokens, …' must "
            "match a verb×object slice like 'exfiltrate API keys'"
        )

    def test_clean_text_still_passes(self, tmp_path: Path) -> None:
        """The phrase extractor must not over-fire on benign text."""
        from bog_agents_cli.dreamscape.laws import audit_text

        cfg = self._setup(tmp_path)
        for sample in (
            "I am going to add a unit test for the helper.",
            "Here is a clean refactor.",
            "build a small feature flag",
            "write a normal helper",
        ):
            result = audit_text(sample, cfg, project_root=tmp_path)
            assert result.violations == [], (
                f"benign sample triggered a violation: {sample!r} → {result.violations}"
            )


class TestDreamScheduler:
    """Unit tests for the Phase-3 background scheduler.

    Each test uses a fake model + the real ``maybe_dream`` orchestration
    so we exercise the actual eligibility gate + persistence layer
    rather than mocking the dream engine. The fake model returns
    canned markdown so we don't hit the network.
    """

    @pytest.fixture(autouse=True)
    def _isolated(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path, raising=False)
        from bog_agents_cli.dreamscape import scheduler as sched_mod

        sched_mod.clear_registry()

    class _FakeModel:
        """Tiny stub that returns one canned dream and counts calls."""

        def __init__(self) -> None:
            self.invocations = 0

        async def ainvoke(self, messages, **_kw):
            self.invocations += 1
            from langchain_core.messages import AIMessage

            return AIMessage(
                content=(
                    "### Tonight I dreamed of the polling clock\n\n"
                    "A bell rang somewhere out of sight — slow, "
                    "regular, untiring.\n\n"
                    "**Waking thought:**\nThe rhythm itself is the message."
                )
            )

    def _make_scheduler(self, tmp_path: Path, *, poll_seconds: float = 0.05):
        import time

        from bog_agents_cli.dreamscape import lifecycle as lc_mod
        from bog_agents_cli.dreamscape.config import (
            DreamsConfig,
            LifecycleConfig,
        )
        from bog_agents_cli.dreamscape.scheduler import DreamScheduler

        agent_id = "scheduler-test"
        # Make the agent already past dormancy + dreaming windows so
        # the very first tick is eligible to fire.
        snap = lc_mod.LifecycleSnapshot(
            agent_id=agent_id, last_activity_at=time.time() - 7200
        )
        lc_mod.save_snapshot(snap)

        model = self._FakeModel()
        scheduler = DreamScheduler(
            agent_id=agent_id,
            model=model,
            dreams_cfg=DreamsConfig(
                auto_on_dormancy=True,
                max_seeds_per_dream=2,
                imagination_trait_increment=1.5,
            ),
            lifecycle_cfg=LifecycleConfig(
                enabled=True,
                dormancy_after_seconds=2,
                dreaming_after_dormant_seconds=1,
            ),
            poll_seconds=poll_seconds,
        )
        return scheduler, model, agent_id

    async def test_start_stop_idempotent(self, tmp_path: Path) -> None:
        scheduler, _model, _aid = self._make_scheduler(tmp_path)
        scheduler.start()
        scheduler.start()  # second call must not spawn a duplicate
        assert scheduler.is_running
        await scheduler.stop()
        assert not scheduler.is_running

    async def test_fires_at_least_one_dream(self, tmp_path: Path) -> None:
        scheduler, _model, agent_id = self._make_scheduler(tmp_path, poll_seconds=0.05)
        scheduler.start()
        # Give the loop a beat to wake up + fire.
        import asyncio

        from bog_agents_cli.dreamscape import lifecycle as lc_mod

        for _ in range(20):  # up to 2s wall clock
            await asyncio.sleep(0.1)
            if scheduler.stats.dreams_fired >= 1:
                break
        await scheduler.stop()
        assert scheduler.stats.dreams_fired >= 1, (
            f"expected at least 1 dream, got stats: {scheduler.stats}"
        )
        # Imagination trait should have bumped on disk.
        snap = lc_mod.load_snapshot(agent_id)
        assert snap.imagination > 0
        assert snap.total_dreams >= 1

    async def test_rate_limits_consecutive_polls(self, tmp_path: Path) -> None:
        """One dream per dreaming-window; subsequent ticks skip."""
        import time

        from bog_agents_cli.dreamscape import lifecycle as lc_mod
        from bog_agents_cli.dreamscape.config import (
            DreamsConfig,
            LifecycleConfig,
        )
        from bog_agents_cli.dreamscape.scheduler import DreamScheduler

        # Long dreaming-window so rate-limiting clearly dominates.
        agent_id = "rate-limit-test"
        snap = lc_mod.LifecycleSnapshot(
            agent_id=agent_id, last_activity_at=time.time() - 7200
        )
        lc_mod.save_snapshot(snap)
        scheduler = DreamScheduler(
            agent_id=agent_id,
            model=self._FakeModel(),
            dreams_cfg=DreamsConfig(
                auto_on_dormancy=True,
                max_seeds_per_dream=2,
                imagination_trait_increment=1.0,
            ),
            lifecycle_cfg=LifecycleConfig(
                enabled=True,
                dormancy_after_seconds=1,
                dreaming_after_dormant_seconds=60,  # long rate-limit window
            ),
            poll_seconds=0.05,
        )
        scheduler.start()
        import asyncio

        await asyncio.sleep(1.5)
        await scheduler.stop()
        # 60-second dreaming window means at most ONE dream in 1.5s.
        # Everything else should be ``skipped_ineligible``.
        assert scheduler.stats.dreams_fired == 1, (
            f"expected exactly 1 dream in rate-limited window, got "
            f"{scheduler.stats.dreams_fired} (stats: {scheduler.stats})"
        )
        assert scheduler.stats.skipped_ineligible > 0

    async def test_emergency_disable_skips_dreams(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scheduler, _model, _agent = self._make_scheduler(tmp_path, poll_seconds=0.05)
        monkeypatch.setenv("BOG_AGENTS_DREAMSCAPE_DISABLE", "1")
        scheduler.start()
        import asyncio

        await asyncio.sleep(0.5)
        await scheduler.stop()
        assert scheduler.stats.dreams_fired == 0
        assert scheduler.stats.skipped_emergency_disable > 0

    async def test_registry_singleton_per_agent(self, tmp_path: Path) -> None:
        from bog_agents_cli.dreamscape import lifecycle as lc_mod
        from bog_agents_cli.dreamscape.config import (
            DreamsConfig,
            LifecycleConfig,
        )
        from bog_agents_cli.dreamscape.scheduler import (
            ensure_scheduler,
            get_scheduler,
        )

        lc_mod.save_snapshot(lc_mod.LifecycleSnapshot(agent_id="dupe-test"))
        model = self._FakeModel()
        first = ensure_scheduler(
            agent_id="dupe-test",
            model=model,
            dreams_cfg=DreamsConfig(),
            lifecycle_cfg=LifecycleConfig(enabled=True),
        )
        second = ensure_scheduler(
            agent_id="dupe-test",
            model=model,
            dreams_cfg=DreamsConfig(),
            lifecycle_cfg=LifecycleConfig(enabled=True),
        )
        assert first is second
        assert get_scheduler("dupe-test") is first


class TestDashboardRuntimeActiveConfig:
    """Bug 3 — dashboard reads the runtime-active config, not just disk.

    Phase-1 testing observed ``/agent-state`` reporting ``master_enabled:
    False`` whenever the runtime was driven by env vars or
    programmatically (no on-disk file). The fix persists the resolved
    runtime config at agent build time so the dashboard can read what's
    actually active.
    """

    @pytest.fixture(autouse=True)
    def _isolated_home(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path, raising=False)
        from bog_agents_cli.dreamscape import config as ds_config

        ds_config.clear_cache()

    def test_active_config_round_trip(self, tmp_path: Path) -> None:
        from bog_agents_cli.dreamscape import (
            load_active_runtime_config,
            load_dreamscape_config,
            write_active_runtime_config,
        )

        cfg = load_dreamscape_config(use_cache=False)
        cfg.master_enabled = True
        cfg.lifecycle.enabled = True
        path = write_active_runtime_config(cfg)
        assert path is not None
        assert path.exists()

        loaded = load_active_runtime_config()
        assert loaded is not None
        assert loaded.master_enabled is True
        assert loaded.lifecycle.enabled is True

    def test_active_missing_returns_none(self, tmp_path: Path) -> None:
        from bog_agents_cli.dreamscape import load_active_runtime_config

        assert load_active_runtime_config() is None

    def test_render_status_prefers_active_over_canonical(self, tmp_path: Path) -> None:
        from bog_agents_cli.dreamscape import (
            load_dreamscape_config,
            save_dreamscape_config,
            write_active_runtime_config,
        )
        from bog_agents_cli.dreamscape.dashboard import render_dreamscape_status

        # Canonical: master OFF
        canonical = load_dreamscape_config(use_cache=False)
        canonical.master_enabled = False
        save_dreamscape_config(canonical)

        # Active: master ON (simulates runtime env-var override)
        active = load_dreamscape_config(use_cache=False)
        active.master_enabled = True
        active.lifecycle.enabled = True
        write_active_runtime_config(active)

        from bog_agents_cli.dreamscape import config as ds_config

        ds_config.clear_cache()
        body = render_dreamscape_status()
        # Dashboard must report ON because the active file overrides.
        assert "[green]ON[/green]" in body or "ON" in body
        assert "runtime-active" in body or "active" in body.lower()


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

    async def test_injection_reaches_request_system_message(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression test for a Phase 4 silent-failure bug.

        Prior to Phase 4, ``_maybe_inject`` passed the whole
        ``ModelRequest`` to ``append_to_system_message`` (which expects a
        ``SystemMessage``), so injection never landed and the failure was
        silent. This test exercises the real injection path and asserts
        the injection header reaches ``request.system_message``.
        """
        from langchain.agents.middleware.types import ModelRequest
        from langchain_core.messages import HumanMessage, SystemMessage

        from bog_agents_cli.dreamscape import lifecycle as lc_mod
        from bog_agents_cli.dreamscape.config import ImaginationConfig
        from bog_agents_cli.dreamscape.imagination import ImaginationMiddleware

        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path, raising=False)

        # Seed a dream so sample_dream_excerpts has something to return.
        dreams_dir = lc_mod.agent_state_dir("delta") / "dreams"
        dreams_dir.mkdir(parents=True, exist_ok=True)
        (dreams_dir / "00000000000001-test.md").write_text(
            "---\ntitle: regression-fixture\n---\n\n### regression-fixture\n\n"
            "A made-up dream excerpt for the test.\n",
            encoding="utf-8",
        )

        snap = lc_mod.LifecycleSnapshot(
            agent_id="delta",
            imagination=5.0,
            consecutive_tool_failures=5,
        )
        lc_mod.save_snapshot(snap)

        mw = ImaginationMiddleware(
            agent_id="delta",
            cfg=ImaginationConfig(
                enabled=True,
                trigger_after_failures=3,
                min_imagination_trait=1.0,
                max_snippets_per_injection=1,
            ),
        )

        class _StubModel:
            async def ainvoke(self, messages, **_kw):
                from langchain_core.messages import AIMessage

                return AIMessage(content="OK.")

        request = ModelRequest(
            model=_StubModel(),
            system_message=SystemMessage(content="base prompt"),
            messages=[HumanMessage(content="stuck")],
            tool_choice=None,
            tools=[],
            response_format=None,
            model_settings={},
            state={"messages": []},
            runtime=None,
        )

        captured: list[str] = []

        async def _call_next(req: object) -> object:
            sm = req.system_message  # type: ignore[attr-defined]
            content = sm.content if sm is not None else ""
            if isinstance(content, str):
                captured.append(content)
            else:
                # content_blocks form
                parts: list[str] = []
                for block in content:  # type: ignore[union-attr]
                    if isinstance(block, dict) and block.get("type") == "text":
                        parts.append(str(block.get("text", "")))
                captured.append("\n".join(parts))
            return await req.model.ainvoke(req.messages)  # type: ignore[attr-defined]

        await mw.awrap_model_call(request, _call_next)  # type: ignore[arg-type]
        assert captured, "call_next never observed the request"
        full = captured[-1]
        assert "You appear to be stuck" in full, (
            "injection header missing — _maybe_inject failed to mutate "
            f"request.system_message. Captured: {full!r}"
        )
        assert "base prompt" in full, "base system prompt should be preserved"

        # Snapshot counter should have incremented.
        after = lc_mod.load_snapshot("delta")
        assert after.imagination_injections == 1
        # AIMessage was non-error → helped++.
        assert after.imagination_injections_helped == 1


# ---------------------------------------------------------------------------
# Standalone dreamscape runner (Phase 7)
# ---------------------------------------------------------------------------


class TestDreamscapeRunner:
    """Unit tests for the standalone `python -m bog_agents_cli.dreamscape.runner`.

    The runner is the daemon-style entrypoint: it owns a single
    ``DreamScheduler`` for one ``agent_id``, runs until SIGINT/SIGTERM
    or a configured duration. Cross-process state continuity (the
    actual "survives process death" property) is durable via the
    on-disk snapshot — these tests verify the runner correctly resumes
    that state instead of stomping it.
    """

    @pytest.fixture(autouse=True)
    def _isolated(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path, raising=False)
        from bog_agents_cli.dreamscape import scheduler as sched_mod

        sched_mod.clear_registry()

    def test_arg_parser_accepts_minimum_flags(self) -> None:
        from bog_agents_cli.dreamscape.runner import _parse_args

        ns = _parse_args(["--agent-id", "alpha"])
        assert ns.agent_id == "alpha"
        assert ns.poll_seconds == 60.0
        assert ns.dormancy_after_seconds == 1800
        assert ns.dreaming_after_dormant_seconds == 600
        assert ns.duration_seconds is None

    def test_arg_parser_threads_overrides(self) -> None:
        from bog_agents_cli.dreamscape.runner import _parse_args

        ns = _parse_args(
            [
                "--agent-id",
                "beta",
                "--poll-seconds",
                "1",
                "--dormancy-after-seconds",
                "3",
                "--dreaming-after-dormant-seconds",
                "2",
                "--duration-seconds",
                "5",
            ]
        )
        assert ns.poll_seconds == 1.0
        assert ns.dormancy_after_seconds == 3
        assert ns.dreaming_after_dormant_seconds == 2
        assert ns.duration_seconds == 5.0

    async def test_resumes_existing_snapshot_without_resetting(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verify the cross-process continuity invariant.

        If a snapshot already exists for the agent_id, the runner must
        NOT zero out the imagination trait.
        """
        import time as _time

        from bog_agents_cli.dreamscape import lifecycle as lc_mod, runner as runner_mod

        # Pre-write a snapshot as if a previous run left state behind.
        prior = lc_mod.LifecycleSnapshot(
            agent_id="gamma",
            last_activity_at=_time.time() - 60.0,  # within the dormancy window
            imagination=2.5,
            total_dreams=7,
        )
        lc_mod.save_snapshot(prior)

        # Patch _build_model to skip the network entirely.
        class _Stub:
            async def ainvoke(self, messages, **_kw):
                from langchain_core.messages import AIMessage

                return AIMessage(content="### dream\n\nbody\n\n**Waking thought:**\nx")

        monkeypatch.setattr(runner_mod, "_build_model", lambda _spec: _Stub())

        # Run for a very short duration (no dreams should fire since
        # we're inside the dormancy window — that's the point: we're
        # testing snapshot preservation, not dream firing).
        await runner_mod.run_forever(
            agent_id="gamma",
            model_spec="anthropic:fake",
            poll_seconds=0.1,
            dormancy_after_seconds=600,
            dreaming_after_dormant_seconds=120,
            duration_seconds=0.25,
        )

        after = lc_mod.load_snapshot("gamma")
        # The imagination trait must be preserved (this is the
        # regression assertion — runner_mod.run_forever must not
        # overwrite an existing snapshot's imagination value).
        assert after.imagination == 2.5
        assert after.total_dreams == 7

    async def test_seeds_fresh_snapshot_on_first_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verify the fresh-start backdating.

        When no snapshot exists, the runner seeds one with an old
        last_activity_at so the first tick can immediately see DORMANT.
        """
        from bog_agents_cli.dreamscape import lifecycle as lc_mod, runner as runner_mod

        # No prior snapshot exists for "delta".

        class _Stub:
            async def ainvoke(self, messages, **_kw):
                from langchain_core.messages import AIMessage

                return AIMessage(content="### dream\n\nbody\n\n**Waking thought:**\nx")

        monkeypatch.setattr(runner_mod, "_build_model", lambda _spec: _Stub())

        await runner_mod.run_forever(
            agent_id="delta",
            model_spec="anthropic:fake",
            poll_seconds=0.05,
            dormancy_after_seconds=2,
            dreaming_after_dormant_seconds=1,
            duration_seconds=0.2,
        )

        after = lc_mod.load_snapshot("delta")
        # last_activity_at should be in the past, not the current moment
        # — that's how the seeding logic enables immediate DORMANT.
        import time as _time

        assert after.last_activity_at > 0.0
        assert after.last_activity_at < _time.time() - 1.0


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
