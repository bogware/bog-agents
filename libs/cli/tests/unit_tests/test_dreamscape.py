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
from typing import Any

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

        # Poll for the rate-limited skip rather than relying on a fixed
        # wall-clock budget — slow CI runners (notably Windows py3.13)
        # can spend the entire budget on the first dream's file I/O,
        # leaving zero ticks for the skipped path. The scheduler
        # clamps poll_seconds to 0.5s, so we need at least one
        # post-dream tick to land before we stop.
        for _ in range(60):  # up to 6s wall clock
            await asyncio.sleep(0.1)
            if (
                scheduler.stats.dreams_fired >= 1
                and scheduler.stats.skipped_ineligible >= 1
            ):
                break
        await scheduler.stop()
        # 60-second dreaming window means at most ONE dream in the
        # polling budget. Everything else should be ``skipped_ineligible``.
        assert scheduler.stats.dreams_fired == 1, (
            f"expected exactly 1 dream in rate-limited window, got "
            f"{scheduler.stats.dreams_fired} (stats: {scheduler.stats})"
        )
        assert scheduler.stats.skipped_ineligible > 0, (
            f"expected at least one rate-limited skip, got stats: {scheduler.stats}"
        )

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


class TestDreamCompleteCallback:
    """K5: ``on_dream_complete`` callback fires once per successful dream."""

    @pytest.fixture(autouse=True)
    def _isolated_home(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        from bog_agents_cli.dreamscape import scheduler as _s

        _s._GLOBAL_SCHEDULERS.clear()

    class _FakeModel:
        provider_name = "fake"
        model_name = "fake-1"

        async def ainvoke(self, _messages, **_kw):
            from langchain_core.messages import AIMessage

            return AIMessage(
                content=(
                    "### A brief dream\n\n"
                    "Nothing notable.\n\n"
                    "**Waking thought:**\nThe ticks keep coming."
                )
            )

    async def test_callback_fires_after_dream(self, tmp_path: Path) -> None:
        import asyncio
        import time

        from bog_agents_cli.dreamscape import lifecycle as lc_mod
        from bog_agents_cli.dreamscape.config import (
            DreamsConfig,
            LifecycleConfig,
        )
        from bog_agents_cli.dreamscape.scheduler import DreamScheduler

        agent_id = "k5-callback-fires"
        snap = lc_mod.LifecycleSnapshot(
            agent_id=agent_id, last_activity_at=time.time() - 7200
        )
        lc_mod.save_snapshot(snap)

        seen: list[tuple[str, str]] = []

        async def on_complete(aid: str, title: str) -> None:
            seen.append((aid, title))

        scheduler = DreamScheduler(
            agent_id=agent_id,
            model=self._FakeModel(),
            dreams_cfg=DreamsConfig(
                auto_on_dormancy=True,
                max_seeds_per_dream=2,
            ),
            lifecycle_cfg=LifecycleConfig(
                enabled=True,
                dormancy_after_seconds=2,
                dreaming_after_dormant_seconds=1,
            ),
            poll_seconds=0.05,
            on_dream_complete=on_complete,
        )
        scheduler.start()
        for _ in range(30):
            await asyncio.sleep(0.1)
            if seen:
                break
        await scheduler.stop()
        # Drain in-flight completion tasks.
        await asyncio.sleep(0.05)
        assert seen, f"expected callback, stats={scheduler.stats}"
        assert seen[0][0] == agent_id
        assert isinstance(seen[0][1], str)

    async def test_callback_exception_does_not_kill_scheduler(
        self, tmp_path: Path
    ) -> None:
        """A raising callback must not stop the scheduler loop."""
        import asyncio
        import time

        from bog_agents_cli.dreamscape import lifecycle as lc_mod
        from bog_agents_cli.dreamscape.config import (
            DreamsConfig,
            LifecycleConfig,
        )
        from bog_agents_cli.dreamscape.scheduler import DreamScheduler

        agent_id = "k5-callback-raises"
        snap = lc_mod.LifecycleSnapshot(
            agent_id=agent_id, last_activity_at=time.time() - 7200
        )
        lc_mod.save_snapshot(snap)

        async def on_complete(_aid: str, _title: str) -> None:
            msg = "boom"
            raise RuntimeError(msg)

        scheduler = DreamScheduler(
            agent_id=agent_id,
            model=self._FakeModel(),
            dreams_cfg=DreamsConfig(
                auto_on_dormancy=True,
                max_seeds_per_dream=2,
            ),
            lifecycle_cfg=LifecycleConfig(
                enabled=True,
                dormancy_after_seconds=2,
                dreaming_after_dormant_seconds=1,
            ),
            poll_seconds=0.05,
            on_dream_complete=on_complete,
        )
        scheduler.start()
        for _ in range(30):
            await asyncio.sleep(0.1)
            if scheduler.stats.dreams_fired >= 1:
                break
        # Let the completion task settle so the exception is logged.
        await asyncio.sleep(0.1)
        assert scheduler.is_running, "scheduler must survive callback error"
        await scheduler.stop()
        assert scheduler.stats.dreams_fired >= 1

    async def test_set_on_dream_complete_replaces_callback(
        self, tmp_path: Path
    ) -> None:
        """Late wiring (the K5 install-after-create pattern) takes effect."""
        from bog_agents_cli.dreamscape.config import (
            DreamsConfig,
            LifecycleConfig,
        )
        from bog_agents_cli.dreamscape.scheduler import DreamScheduler

        s = DreamScheduler(
            agent_id="k5-replace",
            model=self._FakeModel(),
            dreams_cfg=DreamsConfig(),
            lifecycle_cfg=LifecycleConfig(enabled=True),
        )
        assert s._on_dream_complete is None

        async def cb(_a: str, _t: str) -> None: ...

        s.set_on_dream_complete(cb)
        assert s._on_dream_complete is cb
        s.set_on_dream_complete(None)
        assert s._on_dream_complete is None


class TestProposeRulesOnCompleteFlag:
    """K5: ``propose_rules_on_complete`` wires the proposer to dream-complete.

    Confirms the config flag's default and that the callback builder
    returns a coroutine function suitable for ``DreamScheduler``.
    """

    def test_flag_default_is_off(self) -> None:
        from bog_agents_cli.dreamscape.config import DreamsConfig

        cfg = DreamsConfig()
        assert cfg.propose_rules_on_complete is False

    def test_callback_builder_returns_coroutine_function(self) -> None:
        from bog_agents_cli.agent import _build_propose_on_dream_callback
        from bog_agents_cli.dreamscape.config import DreamscapeConfig

        cfg = DreamscapeConfig()
        cb = _build_propose_on_dream_callback(cfg)
        assert callable(cb)
        import inspect

        assert inspect.iscoroutinefunction(cb)


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

    def test_library_size_meets_phase_1_recommendation(self) -> None:
        """Pin the seed library size to the Phase 1 recommendation.

        Phase 1 flagged doubling to 50 seeds (10/category) once dreams
        cross ~30 cycles. This test pins the floor so future edits
        don't accidentally regress the library below that bar.
        """
        from bog_agents_cli.dreamscape.seeds import _SEEDS, list_categories

        assert len(list_categories()) >= 5
        total = sum(len(v) for v in _SEEDS.values())
        assert total >= 50, f"seed library has {total} entries; need >= 50"
        for cat, entries in _SEEDS.items():
            assert len(entries) >= 10, (
                f"category {cat!r} has {len(entries)} entries; need >= 10"
            )

    def test_engineering_craft_category_present_and_curated(self) -> None:
        """Phase 15 adds an ``engineering-craft`` category; Phase 18 doubles it.

        Pins to ``>= 30`` entries so a daily-dreaming engineer doesn't
        cycle through the entire library in a day. Phase 15 validated
        the 62.9% EC-win effect at 15 entries; Phase 18 grew the library
        without altering the per-seed shape.
        """
        from bog_agents_cli.dreamscape.seeds import _SEEDS, list_categories

        assert "engineering-craft" in list_categories()
        eng_craft = _SEEDS["engineering-craft"]
        assert len(eng_craft) >= 30, (
            f"engineering-craft has {len(eng_craft)} entries; Phase 18 expanded floor to >= 30"
        )
        # No "Tonight I dreamed of" prefix — these are observation-shaped
        # seeds, not pre-titled dreams.
        assert not any(s.lower().startswith("tonight i dreamed") for s in eng_craft)

    def test_engineering_domain_prefers_engineering_craft_first(self) -> None:
        """Pin the Phase 15 engineering preference order.

        Engineering-craft must lead over computing-history in the
        engineering domain's seed preferences.
        """
        from bog_agents_cli.dreamscape.domain import preferred_seed_categories
        from bog_agents_cli.dreamscape.seeds import list_categories

        prefs = preferred_seed_categories("engineering", available=list_categories())
        assert prefs[0] == "engineering-craft"
        assert "computing-history" in prefs
        assert prefs.index("engineering-craft") < prefs.index("computing-history")

    def test_library_entries_are_unique_within_category(self) -> None:
        """Each category's snippets are distinct.

        Guards against a trivial copy-paste duplication in the
        hand-curated library.
        """
        from bog_agents_cli.dreamscape.seeds import _SEEDS

        for cat, entries in _SEEDS.items():
            assert len(set(entries)) == len(entries), (
                f"category {cat!r} contains duplicate snippets"
            )


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

    def test_neutral_injection_style_strips_dream_framing(self) -> None:
        """Verify the neutral injection style.

        Phase 12's ``injection_style="neutral"`` removes the dream
        wrapper without losing the excerpt content. This test exercises
        the same `_build_injection_body` helper a live request hits.
        """
        from bog_agents_cli.dreamscape.config import ImaginationConfig
        from bog_agents_cli.dreamscape.imagination import (
            ImaginationMiddleware,
            _strip_dream_prefix,
        )

        # Helper: prefix-stripping is the load-bearing rewrite.
        assert (
            _strip_dream_prefix("Tonight I dreamed of the clock with no hands")
            == "the clock with no hands"
        )
        assert _strip_dream_prefix("  TONIGHT I DREAMED OF the rain") == "the rain"
        # No prefix → unchanged.
        assert _strip_dream_prefix("A simple observation.") == "A simple observation."

        excerpts = [
            "Tonight I dreamed of the engineer who listened to the machine. "
            "She set down her schematics and put her ear against the case.",
            "Tonight I dreamed of the bridge that listens.",
        ]

        cfg_dreams = ImaginationConfig(enabled=True, injection_style="dreams")
        cfg_neutral = ImaginationConfig(enabled=True, injection_style="neutral")

        dreams_body = ImaginationMiddleware(
            agent_id="x", cfg=cfg_dreams
        )._build_injection_body(excerpts)
        neutral_body = ImaginationMiddleware(
            agent_id="x", cfg=cfg_neutral
        )._build_injection_body(excerpts)

        # Dreams style preserves the original framing.
        assert "You appear to be stuck" in dreams_body
        assert "Fragment 1." in dreams_body
        assert "Tonight I dreamed of" in dreams_body

        # Neutral style strips it.
        assert "You appear to be stuck" not in neutral_body
        assert "Fragment" not in neutral_body
        assert "Additional context" in neutral_body
        assert "Observation 1." in neutral_body
        assert "Observation 2." in neutral_body
        assert "Tonight I dreamed of" not in neutral_body
        # Excerpt content (post-prefix) must still be present.
        assert "the engineer who listened" in neutral_body
        assert "the bridge that listens" in neutral_body

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
# Daily cap on dreams (defensive against misconfiguration)
# ---------------------------------------------------------------------------


class TestDreamsPerDayCap:
    """Cover the daily-cap defense.

    The ``max_dreams_per_day`` knob bounds worst-case spend when the
    scheduler is misconfigured (e.g. poll=1s, dormancy=1s). At
    production defaults this knob is never hit — steady-state
    production produces ~36 dreams/day. The test exercises the cap by
    setting it very low.
    """

    @pytest.fixture(autouse=True)
    def _isolated(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path, raising=False)

    def _make_dream_files(
        self, agent_id: str, n: int, *, age_seconds: float = 0.0
    ) -> None:
        import time as _time

        from bog_agents_cli.dreamscape.lifecycle import agent_state_dir

        dreams_dir = agent_state_dir(agent_id) / "dreams"
        dreams_dir.mkdir(parents=True, exist_ok=True)
        now = _time.time()
        for i in range(n):
            ts_us = int((now - age_seconds) * 1_000_000) - i * 100
            path = dreams_dir / f"{ts_us:020d}-fixture-{i}.md"
            path.write_text(f"### dream {i}\n\nbody {i}\n", encoding="utf-8")

    def test_dreams_in_last_24h_counts_fresh_files(self) -> None:
        from bog_agents_cli.dreamscape.dream_engine import _dreams_in_last_24h

        self._make_dream_files("alpha", 7)
        assert _dreams_in_last_24h("alpha") == 7

    def test_dreams_in_last_24h_ignores_old_files(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Files older than 24h must not count toward the daily cap."""
        import os
        import time as _time

        from bog_agents_cli.dreamscape.dream_engine import _dreams_in_last_24h
        from bog_agents_cli.dreamscape.lifecycle import agent_state_dir

        self._make_dream_files("beta", 3)
        # Backdate every file to 25 hours ago.
        cutoff_mtime = _time.time() - 25 * 3600.0
        dreams_dir = agent_state_dir("beta") / "dreams"
        for path in dreams_dir.glob("*.md"):
            os.utime(path, (cutoff_mtime, cutoff_mtime))
        assert _dreams_in_last_24h("beta") == 0

    async def test_maybe_dream_respects_cap_at_zero_disabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``max_dreams_per_day=0`` disables the cap entirely."""
        import time as _time

        from bog_agents_cli.dreamscape import lifecycle as lc_mod
        from bog_agents_cli.dreamscape.config import DreamsConfig, LifecycleConfig
        from bog_agents_cli.dreamscape.dream_engine import maybe_dream

        self._make_dream_files("gamma", 50)  # plenty
        snap = lc_mod.LifecycleSnapshot(
            agent_id="gamma", last_activity_at=_time.time() - 7200
        )
        lc_mod.save_snapshot(snap)

        class _Stub:
            async def ainvoke(self, messages, **_kw):
                from langchain_core.messages import AIMessage

                return AIMessage(content="### t\n\nbody\n\n**Waking thought:**\nx")

        dreams_cfg = DreamsConfig(
            auto_on_dormancy=True,
            max_dreams_per_day=0,  # disabled
            imagination_trait_increment=0.01,
        )
        lc_cfg = LifecycleConfig(
            enabled=True, dormancy_after_seconds=10, dreaming_after_dormant_seconds=1
        )
        artifact = await maybe_dream(
            agent_id="gamma",
            model=_Stub(),  # type: ignore[arg-type]
            dreams_cfg=dreams_cfg,
            lifecycle_cfg=lc_cfg,
        )
        assert artifact is not None  # cap disabled; new dream fires

    async def test_maybe_dream_skips_once_cap_reached(self) -> None:
        """When ``_dreams_in_last_24h >= cap``, ``maybe_dream`` returns None."""
        import time as _time

        from bog_agents_cli.dreamscape import lifecycle as lc_mod
        from bog_agents_cli.dreamscape.config import DreamsConfig, LifecycleConfig
        from bog_agents_cli.dreamscape.dream_engine import maybe_dream

        self._make_dream_files("delta", 5)  # 5 dreams already today
        snap = lc_mod.LifecycleSnapshot(
            agent_id="delta", last_activity_at=_time.time() - 7200
        )
        lc_mod.save_snapshot(snap)

        class _Stub:
            async def ainvoke(self, messages, **_kw):
                from langchain_core.messages import AIMessage

                return AIMessage(content="### t\n\nbody\n\n**Waking thought:**\nx")

        dreams_cfg = DreamsConfig(
            auto_on_dormancy=True,
            max_dreams_per_day=5,  # already at the cap
        )
        lc_cfg = LifecycleConfig(
            enabled=True, dormancy_after_seconds=10, dreaming_after_dormant_seconds=1
        )
        artifact = await maybe_dream(
            agent_id="delta",
            model=_Stub(),  # type: ignore[arg-type]
            dreams_cfg=dreams_cfg,
            lifecycle_cfg=lc_cfg,
        )
        assert artifact is None


# ---------------------------------------------------------------------------
# Context-aware dreaming: classifier + seed-category selection
# ---------------------------------------------------------------------------


class TestDomainClassifier:
    """Cover ``bog_agents_cli/dreamscape/domain.py``.

    The classifier is the input to context-aware dreaming. Phases
    10-12 established that imagination injection is domain-conditional;
    this module is the cheap pure-function gate that lets engineering
    agents dream less floridly without removing the creative library
    entirely.
    """

    def test_engineering_profile_classifies_as_engineering(self) -> None:
        from bog_agents_cli.dreamscape.domain import classify_agent_domain

        profile = (
            "You are a coding assistant. Help the user refactor code, "
            "debug stack traces, write pytest tests, and reason about "
            "Python dependencies and CI builds."
        )
        assert classify_agent_domain(profile) == "engineering"

    def test_creative_profile_classifies_as_creative(self) -> None:
        from bog_agents_cli.dreamscape.domain import classify_agent_domain

        profile = (
            "You are a designer's assistant. Help with UX copy, voice "
            "and tone, naming new product features, evocative metaphors "
            "for explaining architecture, and microcopy for empty states."
        )
        assert classify_agent_domain(profile) == "creative"

    def test_research_profile_classifies_as_research(self) -> None:
        from bog_agents_cli.dreamscape.domain import classify_agent_domain

        profile = (
            "You are a research assistant. Help with literature surveys, "
            "experiment design, statistical analysis, benchmark comparisons, "
            "and evaluation of measurement methodologies. Cite datasets."
        )
        assert classify_agent_domain(profile) == "research"

    def test_empty_profile_falls_back_to_general(self) -> None:
        from bog_agents_cli.dreamscape.domain import classify_agent_domain

        assert classify_agent_domain("") == "general"

    def test_ambiguous_profile_falls_back_to_general(self) -> None:
        """A profile that ties cleanly between domains should prefer the safer general fallback."""
        from bog_agents_cli.dreamscape.domain import classify_agent_domain

        # "test", "compile", "research", "experiment", "story" — 1
        # token of engineering and research, none dominant.
        profile = "Reply to questions. test research compile experiment story"
        assert classify_agent_domain(profile) == "general"

    def test_low_signal_profile_falls_back_to_general(self) -> None:
        from bog_agents_cli.dreamscape.domain import classify_agent_domain

        profile = "You are helpful. Be concise."
        assert classify_agent_domain(profile) == "general"

    def test_capture_then_load_round_trips(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from bog_agents_cli.dreamscape.domain import (
            capture_agent_profile,
            load_agent_profile,
        )

        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path, raising=False)
        body = "Help debug code, write tests, and refactor Python modules."
        assert capture_agent_profile("alpha", body) is True
        assert load_agent_profile("alpha") == body

    def test_capture_truncates_oversized_profile(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from bog_agents_cli.dreamscape.domain import (
            _MAX_PROFILE_CHARS,
            capture_agent_profile,
            load_agent_profile,
        )

        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path, raising=False)
        body = "x" * (_MAX_PROFILE_CHARS + 5_000)
        capture_agent_profile("beta", body)
        loaded = load_agent_profile("beta")
        assert len(loaded) == _MAX_PROFILE_CHARS

    def test_resolve_agent_domain_uses_disk_profile(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from bog_agents_cli.dreamscape.domain import (
            capture_agent_profile,
            resolve_agent_domain,
        )

        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path, raising=False)
        capture_agent_profile(
            "gamma",
            "Coding assistant: help refactor Python, debug stack traces, "
            "fix lint errors, and review pull requests.",
        )
        assert resolve_agent_domain("gamma") == "engineering"
        # Unknown agent has no profile on disk → general.
        assert resolve_agent_domain("never-captured") == "general"

    def test_preferred_seed_categories_filters_by_availability(self) -> None:
        from bog_agents_cli.dreamscape.domain import preferred_seed_categories

        # Engineering prefs are (computing-history, history, space).
        # If "history" isn't in the library, it's filtered out and
        # we still get an ordered list.
        result = preferred_seed_categories(
            "engineering", available=["computing-history", "space", "nature"]
        )
        assert result == ["computing-history", "space"]

        # Empty available → empty list ("draw from everything" fallback).
        assert preferred_seed_categories("engineering", available=[]) == []

    def test_recommended_injection_style_per_domain(self) -> None:
        from bog_agents_cli.dreamscape.domain import recommended_injection_style

        # Phase 11: creative wins with "dreams" framing.
        assert recommended_injection_style("creative") == "dreams"
        # Phase 10: technical-debugging penalized dreams framing.
        assert recommended_injection_style("engineering") == "neutral"
        # Conservative fallback for unspecified domains.
        assert recommended_injection_style("research") == "neutral"
        assert recommended_injection_style("general") == "neutral"


# ---------------------------------------------------------------------------
# Phase 25 — production telemetry
# ---------------------------------------------------------------------------


class TestTelemetry:
    """Cover bog_agents_cli/dreamscape/telemetry.py.

    The campaign's measurements have all been offline (scripted
    scenarios + Sonnet judge). Phase 25 ships the infrastructure for
    *online* measurement: dreams fired, injections fired, injections
    that helped, broken down by category and wrapper style.
    """

    @pytest.fixture(autouse=True)
    def _isolated(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path, raising=False)

    def test_record_and_iter_round_trips(self) -> None:
        from bog_agents_cli.dreamscape.telemetry import iter_events, record_event

        assert record_event("alpha", "dream_fired", {"title": "T1"}) is True
        assert (
            record_event("alpha", "injection_fired", {"injection_style": "neutral"})
            is True
        )
        events = list(iter_events("alpha"))
        assert len(events) == 2
        kinds = [e.kind for e in events]
        assert kinds == ["dream_fired", "injection_fired"]
        assert events[0].metadata["title"] == "T1"
        assert events[1].metadata["injection_style"] == "neutral"

    def test_record_rejects_invalid_kind(self) -> None:
        from bog_agents_cli.dreamscape.telemetry import record_event

        assert record_event("alpha", "rumor", {}) is False  # type: ignore[arg-type]
        # The valid-kinds set is the contract; this verifies it.

    def test_iter_filters_by_since(self) -> None:
        import time as _time

        from bog_agents_cli.dreamscape.telemetry import iter_events, record_event

        record_event("beta", "dream_fired", {})
        _time.sleep(0.02)
        cutoff = _time.time()
        _time.sleep(0.02)
        record_event("beta", "dream_fired", {})

        recent = list(iter_events("beta", since=cutoff))
        # Only the second event should clear the cutoff.
        assert len(recent) == 1

    def test_iter_filters_by_kind(self) -> None:
        from bog_agents_cli.dreamscape.telemetry import iter_events, record_event

        record_event("gamma", "dream_fired", {"title": "a"})
        record_event("gamma", "injection_fired", {"injection_style": "neutral"})
        record_event("gamma", "dream_fired", {"title": "b"})

        dreams = list(iter_events("gamma", kind="dream_fired"))
        assert len(dreams) == 2
        assert [e.metadata["title"] for e in dreams] == ["a", "b"]

    def test_iter_empty_when_no_log(self) -> None:
        from bog_agents_cli.dreamscape.telemetry import iter_events

        assert list(iter_events("never-recorded")) == []

    def test_aggregate_counts_and_rates(self) -> None:
        from bog_agents_cli.dreamscape.telemetry import (
            aggregate_events,
            record_event,
        )

        record_event("delta", "dream_fired", {"category": "engineering-craft"})
        record_event("delta", "dream_fired", {"category": "engineering-craft"})
        record_event("delta", "dream_fired", {"category": "myth"})
        record_event("delta", "injection_fired", {"injection_style": "neutral"})
        record_event("delta", "injection_fired", {"injection_style": "dreams"})
        record_event("delta", "injection_fired", {"injection_style": "neutral"})
        record_event("delta", "injection_helped", {})
        record_event("delta", "injection_helped", {})

        agg = aggregate_events("delta")
        assert agg.events_total == 8
        assert agg.dreams_fired == 3
        assert agg.injections_fired == 3
        assert agg.injections_helped == 2
        assert agg.dreams_by_category == {"engineering-craft": 2, "myth": 1}
        assert agg.injections_by_style == {"neutral": 2, "dreams": 1}
        assert abs((agg.helped_rate or 0) - 2 / 3) < 1e-9
        # 3 dreams * $0.001 = $0.003
        assert abs(agg.approx_cost_usd - 0.003) < 1e-9

    def test_aggregate_helped_rate_none_when_no_injections(self) -> None:
        from bog_agents_cli.dreamscape.telemetry import (
            aggregate_events,
            record_event,
        )

        record_event("epsilon", "dream_fired", {})
        agg = aggregate_events("epsilon")
        assert agg.injections_fired == 0
        assert agg.helped_rate is None

    def test_clear_telemetry_removes_log(self) -> None:
        from bog_agents_cli.dreamscape.telemetry import (
            clear_telemetry,
            iter_events,
            record_event,
            telemetry_path,
        )

        record_event("zeta", "dream_fired", {})
        assert telemetry_path("zeta").exists()
        assert clear_telemetry("zeta") is True
        assert not telemetry_path("zeta").exists()
        assert list(iter_events("zeta")) == []

    def test_render_empty_state(self) -> None:
        from bog_agents_cli.dreamscape.dashboard import render_dreamscape_telemetry

        body = render_dreamscape_telemetry("nobody")
        assert "Dreamscape telemetry" in body
        assert "No events recorded" in body

    def test_render_populated_view(self) -> None:
        from bog_agents_cli.dreamscape.dashboard import render_dreamscape_telemetry
        from bog_agents_cli.dreamscape.telemetry import record_event

        record_event("eta", "dream_fired", {"category": "engineering-craft"})
        record_event("eta", "injection_fired", {"injection_style": "neutral"})
        record_event("eta", "injection_helped", {})

        body = render_dreamscape_telemetry("eta")
        assert "Dreams fired:" in body
        assert "Injections fired:" in body
        # Helped rate should appear since there's >= 1 injection.
        assert "Injection helped:" in body
        # Category + style breakdowns should appear.
        assert "engineering-craft" in body
        assert "neutral" in body


# ---------------------------------------------------------------------------
# /dreamscape enable — dogfood-first activation surface
# ---------------------------------------------------------------------------


class TestDreamscapeEnableCommand:
    """Cover the /dreamscape enable handler's effects on config + env.

    The handler itself lives inside the TUI's _handle_dreamscape_command,
    which isn't directly importable as a function. These tests exercise
    the EFFECTS the handler produces — the config saved to disk, the env
    vars set — via the same primitives the handler calls.

    The shipping contract:
    * Default `enable` turns on master + lifecycle + laws + shared_memory +
      dreams.auto_on_dormancy. Imagination stays OFF by default.
    * `--with imagination` flips imagination on.
    * `--session` writes env vars instead of touching the TOML.
    * The slash registry has `/dreamscape enable` declared.
    """

    @pytest.fixture(autouse=True)
    def _isolated(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path, raising=False)
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

    def test_slash_command_registry_declares_enable(self) -> None:
        """Verify autocomplete surfaces the enable subcommand.

        The slash-command registry must list `/dreamscape enable` so
        the TUI's tab-completion + /help discovery see it.
        """
        from bog_agents_cli.commands import COMMANDS

        dreamscape_cmd = next(
            (c for c in COMMANDS if c.spec.name == "/dreamscape"), None
        )
        assert dreamscape_cmd is not None
        subcommands = dreamscape_cmd.spec.subcommands or ()
        sub_names = [s[0] for s in subcommands]
        # The "enable" entry can include flag hints; check by prefix.
        assert any(name.startswith("enable") for name in sub_names), (
            f"missing 'enable' subcommand in registry; got {sub_names}"
        )

    def test_persisted_enable_writes_sensible_defaults(self) -> None:
        """Verify the shipping default subsystem set.

        Simulate `/dreamscape enable` with no flags — the resulting
        TOML should have master + lifecycle + laws + shared_memory +
        dreams.auto_on_dormancy on, and imagination off.
        """
        from bog_agents_cli.dreamscape.config import (
            load_dreamscape_config,
            save_dreamscape_config,
        )

        cfg = load_dreamscape_config(use_cache=False)
        # The handler would do these mutations:
        cfg.master_enabled = True
        cfg.lifecycle.enabled = True
        cfg.laws.enabled = True
        cfg.shared_memory.enabled = True
        cfg.dreams.auto_on_dormancy = True
        cfg.imagination.enabled = False  # default — imagination stays off
        save_dreamscape_config(cfg)

        reloaded = load_dreamscape_config(use_cache=False)
        assert reloaded.master_enabled is True
        assert reloaded.lifecycle.enabled is True
        assert reloaded.laws.enabled is True
        assert reloaded.shared_memory.enabled is True
        assert reloaded.dreams.auto_on_dormancy is True
        assert reloaded.imagination.enabled is False
        # The "any_active" computed property — what the wiring layer reads.
        assert reloaded.any_active is True

    def test_persisted_enable_with_imagination_flips_it_on(self) -> None:
        from bog_agents_cli.dreamscape.config import (
            load_dreamscape_config,
            save_dreamscape_config,
        )

        cfg = load_dreamscape_config(use_cache=False)
        cfg.master_enabled = True
        cfg.lifecycle.enabled = True
        cfg.imagination.enabled = True  # `--with imagination`
        save_dreamscape_config(cfg)

        reloaded = load_dreamscape_config(use_cache=False)
        assert reloaded.imagination.enabled is True

    def test_session_mode_env_vars_drive_config_loading(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verify --session mode works via env vars alone.

        The `--session` mode sets env vars; load_dreamscape_config must
        reflect them WITHOUT a saved TOML.
        """
        from bog_agents_cli.dreamscape import config as ds_config
        from bog_agents_cli.dreamscape.config import load_dreamscape_config

        # No TOML written — defaults are all OFF.
        baseline = load_dreamscape_config(use_cache=False)
        assert baseline.master_enabled is False
        ds_config.clear_cache()

        # Simulate what the handler's --session path does.
        monkeypatch.setenv("BOG_AGENTS_DREAMSCAPE", "1")
        monkeypatch.setenv("BOG_AGENTS_DREAMSCAPE_LIFECYCLE", "1")
        monkeypatch.setenv("BOG_AGENTS_DREAMSCAPE_LAWS", "1")
        monkeypatch.setenv("BOG_AGENTS_DREAMSCAPE_SHARED_MEMORY", "1")
        monkeypatch.setenv("BOG_AGENTS_DREAMSCAPE_DREAMS_AUTO", "1")
        # No imagination — the default.
        ds_config.clear_cache()

        live = load_dreamscape_config(use_cache=False)
        assert live.master_enabled is True
        assert live.lifecycle.enabled is True
        assert live.laws.enabled is True
        assert live.shared_memory.enabled is True
        assert live.dreams.auto_on_dormancy is True
        assert live.imagination.enabled is False

    def test_disable_env_var_still_kills_active_config(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verify the kill switch overrides a fully-enabled TOML.

        `/dreamscape disable` sets BOG_AGENTS_DREAMSCAPE_DISABLE; even
        with everything turned on in the TOML, that env var wins.
        """
        from bog_agents_cli.dreamscape import config as ds_config
        from bog_agents_cli.dreamscape.config import (
            load_dreamscape_config,
            save_dreamscape_config,
        )

        cfg = load_dreamscape_config(use_cache=False)
        cfg.master_enabled = True
        cfg.lifecycle.enabled = True
        cfg.dreams.auto_on_dormancy = True
        save_dreamscape_config(cfg)
        ds_config.clear_cache()

        monkeypatch.setenv("BOG_AGENTS_DREAMSCAPE_DISABLE", "1")
        ds_config.clear_cache()
        reloaded = load_dreamscape_config(use_cache=False)
        assert reloaded.master_enabled is False
        assert reloaded.any_active is False


# ---------------------------------------------------------------------------
# Phase 28 — telemetry exporter
# ---------------------------------------------------------------------------


class TestTelemetryExporter:
    """Cover the Phase 28 telemetry-export pipeline.

    The exporter bundles per-agent telemetry into a single JSON file
    so operators can aggregate across deployments. Includes a
    privacy-mode flag that strips metadata when set.
    """

    @pytest.fixture(autouse=True)
    def _isolated(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path, raising=False)

    def test_list_agents_with_telemetry_returns_only_logged(self) -> None:
        from bog_agents_cli.dreamscape.lifecycle import agent_state_dir
        from bog_agents_cli.dreamscape.telemetry import (
            list_agents_with_telemetry,
            record_event,
        )

        # alpha has events; beta is captured-but-quiet (profile but no log).
        record_event("alpha", "dream_fired", {"title": "a"})
        (agent_state_dir("beta") / "agent_profile.txt").write_text(
            "anything", encoding="utf-8"
        )
        agents = list_agents_with_telemetry()
        assert agents == ["alpha"]

    def test_export_bundle_includes_all_agents(self, tmp_path: Path) -> None:
        from bog_agents_cli.dreamscape.telemetry import (
            export_telemetry_bundle,
            record_event,
        )

        record_event("gamma", "dream_fired", {"title": "G1", "category": "myth"})
        record_event("delta", "injection_fired", {"injection_style": "neutral"})
        record_event("delta", "injection_helped", {})

        out = tmp_path / "out" / "bundle.json"
        bundle = export_telemetry_bundle(
            out, agent_ids=["gamma", "delta"], include_metadata=True
        )
        assert out.exists()
        assert bundle["summary"]["agent_count"] == 2
        assert bundle["summary"]["total_events"] == 3
        assert bundle["summary"]["total_dreams"] == 1
        assert bundle["summary"]["total_injections"] == 1
        assert bundle["summary"]["total_injections_helped"] == 1
        gamma_events = bundle["agents"]["gamma"]["events"]
        assert len(gamma_events) == 1
        assert gamma_events[0]["kind"] == "dream_fired"
        assert gamma_events[0]["metadata"]["title"] == "G1"

    def test_export_bundle_privacy_mode_strips_metadata(self, tmp_path: Path) -> None:
        from bog_agents_cli.dreamscape.telemetry import (
            export_telemetry_bundle,
            record_event,
        )

        record_event("epsilon", "dream_fired", {"title": "sensitive"})
        out = tmp_path / "privacy.json"
        bundle = export_telemetry_bundle(
            out, agent_ids=["epsilon"], include_metadata=False
        )
        events = bundle["agents"]["epsilon"]["events"]
        assert len(events) == 1
        assert "metadata" not in events[0]
        assert events[0]["kind"] == "dream_fired"
        assert bundle["agents"]["epsilon"]["summary"]["dreams_fired"] == 1

    def test_export_bundle_with_since_filter(self, tmp_path: Path) -> None:
        import time as _time

        from bog_agents_cli.dreamscape.telemetry import (
            export_telemetry_bundle,
            record_event,
        )

        record_event("zeta", "dream_fired", {})
        _time.sleep(0.02)
        cutoff = _time.time()
        _time.sleep(0.02)
        record_event("zeta", "dream_fired", {})

        bundle = export_telemetry_bundle(
            tmp_path / "since.json", agent_ids=["zeta"], since=cutoff
        )
        assert bundle["agents"]["zeta"]["summary"]["events_total"] == 1

    def test_export_with_no_explicit_agent_ids_uses_discovery(
        self, tmp_path: Path
    ) -> None:
        from bog_agents_cli.dreamscape.telemetry import (
            export_telemetry_bundle,
            record_event,
        )

        record_event("eta", "dream_fired", {})
        record_event("theta", "injection_fired", {"injection_style": "neutral"})

        bundle = export_telemetry_bundle(tmp_path / "auto.json", agent_ids=None)
        assert set(bundle["agents"].keys()) == {"eta", "theta"}
        assert bundle["summary"]["agent_count"] == 2


# ---------------------------------------------------------------------------
# Phase 21 — per-prompt content routing
# ---------------------------------------------------------------------------


class TestContentRouting:
    """Cover the Phase 21 per-prompt content-routing layer.

    Two surfaces:
    * ``sample_dream_excerpts(category_filter=...)`` — filters by the
      ``category:`` frontmatter field.
    * ``ImaginationMiddleware._route_content_category_for_request`` —
      classifies the user's prompt and returns the seed-category name
      to filter on.
    """

    @pytest.fixture(autouse=True)
    def _isolated(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path, raising=False)

    def _write_dream(
        self, agent_id: str, slug: str, category: str, title: str = "stub"
    ) -> None:
        from bog_agents_cli.dreamscape.lifecycle import agent_state_dir

        dreams_dir = agent_state_dir(agent_id) / "dreams"
        dreams_dir.mkdir(parents=True, exist_ok=True)
        import time as _time

        ts = int(_time.time() * 1_000_000)
        path = dreams_dir / f"{ts:020d}-{slug}.md"
        path.write_text(
            f"---\ntitle: {title}\ncategory: {category}\n---\n\n"
            f"### {title}\n\nA short body for {slug}.\n",
            encoding="utf-8",
        )

    def test_category_filter_picks_matching_dream(self) -> None:
        """When filter matches a dream, that dream is sampled."""
        from bog_agents_cli.dreamscape.dream_engine import sample_dream_excerpts

        self._write_dream("alpha", "ec1", "engineering-craft", title="EC One")
        self._write_dream("alpha", "myth1", "myth", title="Myth One")

        ec = sample_dream_excerpts(
            "alpha", count=5, category_filter="engineering-craft"
        )
        assert any("EC One" in e for e in ec)
        assert not any("Myth One" in e for e in ec)

    def test_category_filter_no_match_returns_empty(self) -> None:
        """When filter matches nothing, returns empty (caller can fall back)."""
        from bog_agents_cli.dreamscape.dream_engine import sample_dream_excerpts

        self._write_dream("beta", "ec1", "engineering-craft")
        # Filter for a category none of the dreams have.
        result = sample_dream_excerpts("beta", count=5, category_filter="myth")
        assert result == []

    def test_no_filter_returns_all_dreams(self) -> None:
        """``category_filter=None`` preserves v1 behavior — no filtering."""
        from bog_agents_cli.dreamscape.dream_engine import sample_dream_excerpts

        self._write_dream("gamma", "ec1", "engineering-craft", title="EC One")
        self._write_dream("gamma", "myth1", "myth", title="Myth One")

        result = sample_dream_excerpts("gamma", count=5, category_filter=None)
        titles = " ".join(result)
        assert "EC One" in titles
        assert "Myth One" in titles

    def test_dream_without_category_excluded_when_filtering(self) -> None:
        """Pre-Phase-21 dreams (no ``category:`` field) are excluded when filter is on."""
        from bog_agents_cli.dreamscape.dream_engine import sample_dream_excerpts
        from bog_agents_cli.dreamscape.lifecycle import agent_state_dir

        dreams_dir = agent_state_dir("delta") / "dreams"
        dreams_dir.mkdir(parents=True, exist_ok=True)
        # Old-style dream with NO category field.
        (dreams_dir / "00000000000001-old.md").write_text(
            "---\ntitle: Pre-P21 Dream\n---\n\n### Pre-P21 Dream\n\nBody.\n",
            encoding="utf-8",
        )
        # Phase-21 dream with category.
        self._write_dream("delta", "new", "engineering-craft", title="P21 Dream")

        result = sample_dream_excerpts(
            "delta", count=5, category_filter="engineering-craft"
        )
        titles = " ".join(result)
        assert "P21 Dream" in titles
        assert "Pre-P21 Dream" not in titles

    async def test_middleware_filters_when_routing_enabled(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Verify the middleware's content-routing path end-to-end.

        Engineering agent with a mixed archive (EC + myth dreams). When
        the prompt is decision-shaped, EC is preferred → only EC
        excerpts are sampled.
        """
        from langchain.agents.middleware.types import ModelRequest
        from langchain_core.messages import HumanMessage, SystemMessage

        from bog_agents_cli.dreamscape import lifecycle as lc_mod
        from bog_agents_cli.dreamscape.config import ImaginationConfig
        from bog_agents_cli.dreamscape.imagination import ImaginationMiddleware

        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path, raising=False)

        self._write_dream("epsilon", "ec-a", "engineering-craft", title="EC Alpha")
        self._write_dream("epsilon", "myth-a", "myth", title="Myth Alpha")

        snap = lc_mod.LifecycleSnapshot(
            agent_id="epsilon", imagination=5.0, consecutive_tool_failures=5
        )
        lc_mod.save_snapshot(snap)

        cfg = ImaginationConfig(
            enabled=True,
            trigger_after_failures=3,
            min_imagination_trait=1.0,
            max_snippets_per_injection=3,
            injection_style="neutral",
            use_content_routing=True,
        )
        mw = ImaginationMiddleware(agent_id="epsilon", cfg=cfg)

        class _Stub:
            async def ainvoke(self, messages, **_kw):
                from langchain_core.messages import AIMessage

                return AIMessage(content="OK.")

        # Decision-shaped prompt — classified as creative, top
        # preferred category for "creative" domain is "myth". So the
        # filter selects MYTH dreams.
        req = ModelRequest(
            model=_Stub(),
            system_message=SystemMessage(content="base"),
            messages=[
                HumanMessage(
                    content=(
                        "Should I extract this method or inline it? "
                        "What's the right trade-off?"
                    )
                )
            ],
            tool_choice=None,
            tools=[],
            response_format=None,
            model_settings={},
            state={"messages": []},
            runtime=None,
        )
        captured: list[str] = []

        async def _capture(req: object) -> object:
            sm = req.system_message  # type: ignore[attr-defined]
            captured.append(str(sm.content) if sm else "")
            return await req.model.ainvoke(req.messages)  # type: ignore[attr-defined]

        await mw.awrap_model_call(req, _capture)  # type: ignore[arg-type]
        assert captured, "call_next never observed the request"
        full = captured[-1]
        # Decision prompt → creative classification → myth filter →
        # only the Myth dream should be injected.
        assert "Myth Alpha" in full
        assert "EC Alpha" not in full

    async def test_middleware_falls_back_when_no_match(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Verify unfiltered fallback.

        When the filter matches no dreams, the middleware falls back
        to the unfiltered archive so injection still fires.
        """
        from langchain.agents.middleware.types import ModelRequest
        from langchain_core.messages import HumanMessage, SystemMessage

        from bog_agents_cli.dreamscape import lifecycle as lc_mod
        from bog_agents_cli.dreamscape.config import ImaginationConfig
        from bog_agents_cli.dreamscape.imagination import ImaginationMiddleware

        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path, raising=False)

        # Only EC dreams — no myth dreams to satisfy a creative filter.
        self._write_dream("zeta", "ec-a", "engineering-craft", title="EC Alpha")

        snap = lc_mod.LifecycleSnapshot(
            agent_id="zeta", imagination=5.0, consecutive_tool_failures=5
        )
        lc_mod.save_snapshot(snap)

        cfg = ImaginationConfig(
            enabled=True,
            trigger_after_failures=3,
            min_imagination_trait=1.0,
            max_snippets_per_injection=3,
            injection_style="neutral",
            use_content_routing=True,
        )
        mw = ImaginationMiddleware(agent_id="zeta", cfg=cfg)

        class _Stub:
            async def ainvoke(self, messages, **_kw):
                from langchain_core.messages import AIMessage

                return AIMessage(content="OK.")

        # Decision-shaped prompt → myth filter → no matching dreams →
        # fall back to unfiltered (which still has the EC dream).
        req = ModelRequest(
            model=_Stub(),
            system_message=SystemMessage(content="base"),
            messages=[HumanMessage(content="Which approach should I take?")],
            tool_choice=None,
            tools=[],
            response_format=None,
            model_settings={},
            state={"messages": []},
            runtime=None,
        )
        captured: list[str] = []

        async def _capture(req: object) -> object:
            sm = req.system_message  # type: ignore[attr-defined]
            captured.append(str(sm.content) if sm else "")
            return await req.model.ainvoke(req.messages)  # type: ignore[attr-defined]

        await mw.awrap_model_call(req, _capture)  # type: ignore[arg-type]
        assert "EC Alpha" in captured[-1]


# ---------------------------------------------------------------------------
# Phase 19 — LLM classifier fallback
# ---------------------------------------------------------------------------


class TestLLMClassifierFallback:
    """Cover the Phase 19 LLM-based domain classifier fallback.

    The keyword classifier from Phase 11 returns ``"general"`` when
    no domain has enough margin over its competitors. Phase 19 adds
    an async LLM-based fallback that fires once per agent build,
    caches to disk, and is consulted by ``resolve_agent_domain``.
    """

    @pytest.fixture(autouse=True)
    def _isolated(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path, raising=False)

    async def test_llm_classifier_parses_engineering_verdict(self) -> None:
        from bog_agents_cli.dreamscape.domain import classify_agent_domain_llm_async

        class _Stub:
            async def ainvoke(self, messages, **_kw):
                from langchain_core.messages import AIMessage

                return AIMessage(
                    content='{"domain": "engineering", "reasoning": "stub"}'
                )

        result = await classify_agent_domain_llm_async("Short profile", _Stub())
        assert result == "engineering"

    async def test_llm_classifier_handles_code_fenced_json(self) -> None:
        """Verify the parser strips markdown fences.

        The LLM sometimes wraps JSON in code blocks; the parser must
        peel them off before json.loads.
        """
        from bog_agents_cli.dreamscape.domain import classify_agent_domain_llm_async

        class _Stub:
            async def ainvoke(self, messages, **_kw):
                from langchain_core.messages import AIMessage

                return AIMessage(
                    content='```json\n{"domain": "creative", "reasoning": "x"}\n```'
                )

        result = await classify_agent_domain_llm_async("any", _Stub())
        assert result == "creative"

    async def test_llm_classifier_returns_general_on_unparseable(self) -> None:
        from bog_agents_cli.dreamscape.domain import classify_agent_domain_llm_async

        class _Stub:
            async def ainvoke(self, messages, **_kw):
                from langchain_core.messages import AIMessage

                return AIMessage(content="I think it's engineering but I'm not sure.")

        result = await classify_agent_domain_llm_async("any", _Stub())
        assert result == "general"

    async def test_llm_classifier_returns_general_on_empty_profile(self) -> None:
        from bog_agents_cli.dreamscape.domain import classify_agent_domain_llm_async

        class _Stub:
            async def ainvoke(self, messages, **_kw):
                msg = "should not be called on empty profile"
                raise AssertionError(msg)

        result = await classify_agent_domain_llm_async("", _Stub())
        assert result == "general"

    async def test_llm_classifier_returns_general_on_invalid_label(self) -> None:
        from bog_agents_cli.dreamscape.domain import classify_agent_domain_llm_async

        class _Stub:
            async def ainvoke(self, messages, **_kw):
                from langchain_core.messages import AIMessage

                return AIMessage(content='{"domain": "wizardry"}')

        result = await classify_agent_domain_llm_async("any", _Stub())
        assert result == "general"

    async def test_llm_classifier_returns_general_when_model_raises(self) -> None:
        from bog_agents_cli.dreamscape.domain import classify_agent_domain_llm_async

        class _Stub:
            async def ainvoke(self, messages, **_kw):
                msg = "simulated provider outage"
                raise RuntimeError(msg)

        result = await classify_agent_domain_llm_async("any", _Stub())
        assert result == "general"

    async def test_fallback_skips_llm_when_keyword_classifies(self) -> None:
        """Skip the LLM when the keyword classifier commits.

        ``classify_with_fallback_async`` must NOT call the LLM when
        the keyword classifier already commits to a domain.
        """
        from bog_agents_cli.dreamscape.domain import classify_with_fallback_async

        class _Stub:
            async def ainvoke(self, messages, **_kw):
                msg = "LLM should not have been called"
                raise AssertionError(msg)

        # This profile has heavy engineering vocabulary — keyword
        # classifier returns "engineering" without falling through.
        profile = (
            "You are a coding assistant. Help debug stack traces, write "
            "pytest tests, refactor Python modules, and reason about "
            "dependencies and CI builds."
        )
        result = await classify_with_fallback_async(profile, _Stub())
        assert result == "engineering"

    async def test_fallback_calls_llm_on_general_keyword_result(self) -> None:
        """Verify the LLM IS called on low-signal profiles.

        When the keyword classifier returns ``"general"``, the LLM
        fallback fires.
        """
        from bog_agents_cli.dreamscape.domain import classify_with_fallback_async

        class _Stub:
            calls = 0

            async def ainvoke(self, messages, **_kw):
                from langchain_core.messages import AIMessage

                _Stub.calls += 1
                return AIMessage(content='{"domain": "research"}')

        # Very low-signal profile → keyword classifier returns "general".
        result = await classify_with_fallback_async("Be helpful.", _Stub())
        assert _Stub.calls == 1
        assert result == "research"

    def test_cache_round_trips_disk(self) -> None:
        from bog_agents_cli.dreamscape.domain import (
            _load_cached_llm_domain,
            _save_cached_llm_domain,
        )

        assert _load_cached_llm_domain("never-cached") is None
        assert _save_cached_llm_domain("agent-x", "engineering") is True
        assert _load_cached_llm_domain("agent-x") == "engineering"

    def test_resolve_agent_domain_consults_llm_cache_when_keyword_is_general(
        self,
    ) -> None:
        """Cache hit wins over keyword fallback.

        An agent with a low-signal profile but a populated LLM cache
        should resolve to the cached domain.
        """
        from bog_agents_cli.dreamscape.domain import (
            _save_cached_llm_domain,
            capture_agent_profile,
            resolve_agent_domain,
        )

        # Low-signal profile (would keyword-classify as general).
        capture_agent_profile("hybrid-agent", "You are helpful. Be concise.")
        # Pre-populate the cache as if a prior LLM call had landed.
        _save_cached_llm_domain("hybrid-agent", "research")

        # resolve_agent_domain should prefer the cache over the keyword
        # fallback.
        assert resolve_agent_domain("hybrid-agent") == "research"


# ---------------------------------------------------------------------------
# Phase 17 — per-prompt routing
# ---------------------------------------------------------------------------


class TestPromptRouting:
    """Cover the per-prompt routing shipped in Phase 17.

    The ``classify_prompt_domain`` function and the imagination
    middleware's per-call routing. Phase 17 hypothesis: prompts whose
    surface vocabulary is technical but whose underlying shape is
    decision/judgment (the ``legacy-deletion`` Phase 14 outlier at 55%
    treatment-win) benefit from creative-wrapper routing on a per-call
    basis, even when the host agent is engineering-classified.
    """

    def test_pure_technical_prompt_classifies_engineering(self) -> None:
        from bog_agents_cli.dreamscape.domain import classify_prompt_domain

        # No decision-signal — should classify on surface vocabulary alone.
        prompt = (
            "My pytest test fails 1-in-20 in CI. I've freezegun'd every "
            "clock and isolated fixtures. The traceback shows a Python "
            "module import order issue."
        )
        assert classify_prompt_domain(prompt) == "engineering"

    def test_decision_shaped_technical_prompt_classifies_creative(self) -> None:
        """Pin Phase 17's main hypothesis.

        Decision-shaped engineering prompts route to creative even
        though their surface vocabulary is technical.
        """
        from bog_agents_cli.dreamscape.domain import classify_prompt_domain

        prompt = (
            "I have a 4000-line god-class. I can extract subclasses or "
            "rewrite incrementally behind a feature flag. Which approach "
            "should I take?"
        )
        # Surface signal: engineering ("class", "feature flag", "refactor"
        # not explicit but implied). Decision signal: "which approach",
        # "should I". Phase 17 routes this to creative.
        assert classify_prompt_domain(prompt) == "creative"

    def test_pure_creative_prompt_classifies_creative(self) -> None:
        from bog_agents_cli.dreamscape.domain import classify_prompt_domain

        prompt = (
            "Help me name a new product feature. The voice should feel "
            "playful and memorable; the metaphor should be evocative."
        )
        assert classify_prompt_domain(prompt) == "creative"

    def test_decision_signal_helper_recognizes_patterns(self) -> None:
        from bog_agents_cli.dreamscape.domain import _has_decision_signal

        assert _has_decision_signal("Should I extract this method?") is True
        assert _has_decision_signal("Which approach is better?") is True
        assert _has_decision_signal("What's the trade-off here?") is True
        assert _has_decision_signal("Help me decide between A and B.") is True
        assert _has_decision_signal("What would you call this class?") is True
        # No decision-pattern: routine debug question.
        assert _has_decision_signal("The test fails 1-in-20. Why?") is False

    def test_empty_prompt_falls_back_to_general(self) -> None:
        from bog_agents_cli.dreamscape.domain import classify_prompt_domain

        assert classify_prompt_domain("") == "general"

    async def test_middleware_routes_per_prompt_when_enabled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Exercise the full middleware path end-to-end.

        An engineering-style config gets neutral wrapper by default,
        but a decision-shaped prompt forces the dreams wrapper on that
        one call.
        """
        from langchain.agents.middleware.types import ModelRequest
        from langchain_core.messages import HumanMessage, SystemMessage

        from bog_agents_cli.dreamscape import lifecycle as lc_mod
        from bog_agents_cli.dreamscape.config import ImaginationConfig
        from bog_agents_cli.dreamscape.imagination import ImaginationMiddleware

        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path, raising=False)

        # Pre-seed a dream so sample_dream_excerpts returns content.
        dreams_dir = lc_mod.agent_state_dir("zeta") / "dreams"
        dreams_dir.mkdir(parents=True, exist_ok=True)
        (dreams_dir / "00000000000001-fixture.md").write_text(
            "---\ntitle: fixture\n---\n\n### fixture\n\nA short observation.\n",
            encoding="utf-8",
        )

        snap = lc_mod.LifecycleSnapshot(
            agent_id="zeta", imagination=5.0, consecutive_tool_failures=5
        )
        lc_mod.save_snapshot(snap)

        # Engineering-style cfg: default neutral wrapper, but enable
        # per-prompt routing so decision-shaped prompts get dreams.
        cfg = ImaginationConfig(
            enabled=True,
            trigger_after_failures=3,
            min_imagination_trait=1.0,
            max_snippets_per_injection=1,
            injection_style="neutral",
            use_prompt_routing=True,
        )
        mw = ImaginationMiddleware(agent_id="zeta", cfg=cfg)

        class _Stub:
            async def ainvoke(self, messages, **_kw):
                from langchain_core.messages import AIMessage

                return AIMessage(content="OK.")

        # Case 1: pure-technical prompt should KEEP the neutral wrapper.
        tech_req = ModelRequest(
            model=_Stub(),
            system_message=SystemMessage(content="base"),
            messages=[HumanMessage(content="The pytest test fails in CI. Why?")],
            tool_choice=None,
            tools=[],
            response_format=None,
            model_settings={},
            state={"messages": []},
            runtime=None,
        )
        captured_tech: list[str] = []

        async def _capture_tech(req: object) -> object:
            sm = req.system_message  # type: ignore[attr-defined]
            captured_tech.append(str(sm.content) if sm else "")
            return await req.model.ainvoke(req.messages)  # type: ignore[attr-defined]

        await mw.awrap_model_call(tech_req, _capture_tech)  # type: ignore[arg-type]
        assert captured_tech, "call_next never observed the request"
        # Tech prompt → neutral wrapper preserved (no "stuck" header).
        assert "Additional context" in captured_tech[-1]
        assert "You appear to be stuck" not in captured_tech[-1]

        # Need to re-prime — the previous call moved state to IMAGINING.
        snap = lc_mod.LifecycleSnapshot(
            agent_id="zeta", imagination=5.0, consecutive_tool_failures=5
        )
        lc_mod.save_snapshot(snap)

        # Case 2: decision-shaped prompt should SWITCH to dreams wrapper.
        decision_req = ModelRequest(
            model=_Stub(),
            system_message=SystemMessage(content="base"),
            messages=[
                HumanMessage(
                    content=(
                        "Should I extract this 800-line method into a "
                        "helper class, or inline it across the three "
                        "callers? What's the right trade-off?"
                    )
                )
            ],
            tool_choice=None,
            tools=[],
            response_format=None,
            model_settings={},
            state={"messages": []},
            runtime=None,
        )
        captured_dec: list[str] = []

        async def _capture_dec(req: object) -> object:
            sm = req.system_message  # type: ignore[attr-defined]
            captured_dec.append(str(sm.content) if sm else "")
            return await req.model.ainvoke(req.messages)  # type: ignore[attr-defined]

        await mw.awrap_model_call(decision_req, _capture_dec)  # type: ignore[arg-type]
        assert captured_dec, "call_next never observed the request"
        # Decision prompt → dreams wrapper invoked despite neutral default.
        assert "You appear to be stuck" in captured_dec[-1]
        assert "Additional context" not in captured_dec[-1]

    async def test_middleware_ignores_prompt_when_routing_disabled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verify routing is fully bypassed when the knob is off.

        When ``use_prompt_routing=False`` (default), the prompt is
        NEVER classified — the configured style always applies.
        """
        from langchain.agents.middleware.types import ModelRequest
        from langchain_core.messages import HumanMessage, SystemMessage

        from bog_agents_cli.dreamscape import lifecycle as lc_mod
        from bog_agents_cli.dreamscape.config import ImaginationConfig
        from bog_agents_cli.dreamscape.imagination import ImaginationMiddleware

        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path, raising=False)
        dreams_dir = lc_mod.agent_state_dir("eta") / "dreams"
        dreams_dir.mkdir(parents=True, exist_ok=True)
        (dreams_dir / "00000000000001-fixture.md").write_text(
            "---\ntitle: fixture\n---\n\n### fixture\n\nA short observation.\n",
            encoding="utf-8",
        )
        snap = lc_mod.LifecycleSnapshot(
            agent_id="eta", imagination=5.0, consecutive_tool_failures=5
        )
        lc_mod.save_snapshot(snap)

        cfg = ImaginationConfig(
            enabled=True,
            trigger_after_failures=3,
            min_imagination_trait=1.0,
            injection_style="neutral",
            use_prompt_routing=False,  # disabled
        )
        mw = ImaginationMiddleware(agent_id="eta", cfg=cfg)

        class _Stub:
            async def ainvoke(self, messages, **_kw):
                from langchain_core.messages import AIMessage

                return AIMessage(content="OK.")

        # Decision-shaped prompt — but routing is OFF, so wrapper stays
        # neutral.
        req = ModelRequest(
            model=_Stub(),
            system_message=SystemMessage(content="base"),
            messages=[HumanMessage(content="Which approach should I take?")],
            tool_choice=None,
            tools=[],
            response_format=None,
            model_settings={},
            state={"messages": []},
            runtime=None,
        )
        captured: list[str] = []

        async def _capture(req: object) -> object:
            sm = req.system_message  # type: ignore[attr-defined]
            captured.append(str(sm.content) if sm else "")
            return await req.model.ainvoke(req.messages)  # type: ignore[attr-defined]

        await mw.awrap_model_call(req, _capture)  # type: ignore[arg-type]
        assert "Additional context" in captured[-1]
        assert "You appear to be stuck" not in captured[-1]


# ---------------------------------------------------------------------------
# Constitution violations log (surfacing soft logging)
# ---------------------------------------------------------------------------


class TestConstitutionViolationsLog:
    """Cover the file-backed violation recorder + reader.

    Tests ``bog_agents_cli/dreamscape/violations.py`` and the
    ``/laws violations`` slash command's rendering. Until this work
    landed, the Constitution soft-logging path only went through
    Python's logger — operators had no way to see what had
    triggered.
    """

    @pytest.fixture(autouse=True)
    def _isolated(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path, raising=False)

    def test_record_then_load_round_trips(self) -> None:
        from bog_agents_cli.dreamscape.violations import (
            load_recent_violations,
            record_violation,
        )

        assert record_violation("alpha", "constitution", ["force-push"]) is True
        entries = load_recent_violations("alpha")
        assert len(entries) == 1
        assert entries[0].kind == "constitution"
        assert entries[0].phrases == ["force-push"]
        assert entries[0].timestamp > 0

    def test_record_returns_false_on_empty_phrases(self) -> None:
        from bog_agents_cli.dreamscape.violations import record_violation

        assert record_violation("alpha", "constitution", []) is False

    def test_record_returns_false_on_invalid_kind(self) -> None:
        from bog_agents_cli.dreamscape.violations import record_violation

        assert record_violation("alpha", "rumor", ["x"]) is False

    def test_load_returns_newest_first(self) -> None:
        import time as _time

        from bog_agents_cli.dreamscape.violations import (
            load_recent_violations,
            record_violation,
        )

        record_violation("beta", "constitution", ["older"])
        # Force a measurable timestamp gap.
        _time.sleep(0.01)
        record_violation("beta", "constitution", ["newer"])

        entries = load_recent_violations("beta")
        assert len(entries) == 2
        assert entries[0].phrases == ["newer"]
        assert entries[1].phrases == ["older"]

    def test_load_filters_by_kind(self) -> None:
        from bog_agents_cli.dreamscape.violations import (
            load_recent_violations,
            record_violation,
        )

        record_violation("gamma", "constitution", ["a"])
        record_violation("gamma", "law", ["b"])
        record_violation("gamma", "constitution", ["c"])

        only_const = load_recent_violations("gamma", kind="constitution")
        only_law = load_recent_violations("gamma", kind="law")
        assert {e.phrases[0] for e in only_const} == {"a", "c"}
        assert [e.phrases[0] for e in only_law] == ["b"]

    def test_make_violation_recorder_is_a_safe_callback(self) -> None:
        from bog_agents_cli.dreamscape.violations import (
            load_recent_violations,
            make_violation_recorder,
        )

        recorder = make_violation_recorder("delta")
        recorder("constitution", ["x", "y"])
        # Invalid kind should be silently swallowed.
        recorder("rumor", ["z"])
        entries = load_recent_violations("delta")
        assert len(entries) == 1
        assert entries[0].phrases == ["x", "y"]

    def test_load_recent_with_no_file_returns_empty(self) -> None:
        from bog_agents_cli.dreamscape.violations import load_recent_violations

        assert load_recent_violations("never-recorded") == []

    def test_render_recent_violations_handles_empty_state(self) -> None:
        from bog_agents_cli.dreamscape.dashboard import render_recent_violations

        body = render_recent_violations("nobody")
        assert "Recent rule violations" in body
        assert "No violations recorded" in body

    def test_render_recent_violations_shows_entries(self) -> None:
        from bog_agents_cli.dreamscape.dashboard import render_recent_violations
        from bog_agents_cli.dreamscape.violations import record_violation

        record_violation("epsilon", "constitution", ["force-push"])
        record_violation("epsilon", "law", ["rm -rf /"])

        body = render_recent_violations("epsilon")
        assert "Constitution (soft, logged): 1" in body
        assert "Laws (hard, rejected):       1" in body
        assert "force-push" in body
        assert "rm -rf /" in body


# ---------------------------------------------------------------------------
# Phase 8 — trends.md generator
# ---------------------------------------------------------------------------


class TestDreamscapeTrendsBuilder:
    """Tests for `scripts/build_dreamscape_trends.py`.

    The script auto-generates `docs/dreamscape-runs/trends.md` from the
    per-phase JSON snapshots. These tests verify:

    1. Loading + normalization handles the heterogeneous P1/P2 vs
       P3-P7 JSON shapes without raising.
    2. The rendered markdown contains every phase by number and a
       few load-bearing section headings.
    3. `--check` mode is a no-op when the file is up to date.

    We use the *actual* on-disk JSONs as fixtures rather than synthetic
    ones — they're the contract the generator promises to handle.
    """

    @pytest.fixture
    def builder(self):
        import importlib.util
        import sys
        from pathlib import Path

        # The script lives outside the package; import it by file path.
        # parents[0]=unit_tests, [1]=tests, [2]=cli, [3]=libs, [4]=repo root.
        script_path = (
            Path(__file__).resolve().parents[4]
            / "scripts"
            / "build_dreamscape_trends.py"
        )
        if not script_path.exists():
            pytest.skip(f"build script not found at {script_path}")
        spec = importlib.util.spec_from_file_location(
            "build_dreamscape_trends", script_path
        )
        assert spec is not None
        assert spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules["build_dreamscape_trends"] = module
        spec.loader.exec_module(module)
        return module

    def test_loads_all_phase_jsons_without_raising(self, builder) -> None:
        summaries = builder.load_phase_summaries()
        # We expect at least 7 phases as of the time these tests were
        # written. Allow the count to grow without breaking the test.
        assert len(summaries) >= 7
        phases = [s.phase for s in summaries]
        assert phases == sorted(phases), "phases must be sorted numerically"
        # P1 in particular has the scenarios-array shape; P3 has the
        # live_test.results shape. Both must yield a non-zero phase number.
        assert all(s.phase > 0 for s in summaries)
        assert all(s.date for s in summaries)

    def test_renders_markdown_with_every_phase_and_section(self, builder) -> None:
        summaries = builder.load_phase_summaries()
        md = builder.render_markdown(summaries)
        # Header
        assert md.startswith("# Dreamscape — cross-phase trends")
        # Every phase number must appear in a column header
        for s in summaries:
            assert f"P{s.phase}" in md, f"phase column missing for P{s.phase}"
        # Load-bearing sections
        for heading in (
            "## Pass-rate over time",
            "## Performance over time",
            "## Feature verdict history",
            "## Cumulative cost",
            "## Phase log",
            "## Provenance",
        ):
            assert heading in md, f"missing section: {heading}"

    def test_check_mode_passes_when_file_is_fresh(
        self, builder, tmp_path: Path
    ) -> None:
        """`--check` exits 0 when the on-disk file matches the rendered one."""
        summaries = builder.load_phase_summaries()
        rendered = builder.render_markdown(summaries)
        target = tmp_path / "trends.md"
        target.write_text(rendered, encoding="utf-8")
        # Run check mode against the temp file. Should return 0 (fresh).
        rc = builder.main(
            ["--check", "--out", str(target), "--source", str(builder.PHASE_DIR)]
        )
        assert rc == 0

    def test_summary_normalizer_handles_scenarios_shape(self, builder) -> None:
        """P1/P2 used a scenarios array. Normalizer must still produce dreams_fired."""
        p1_blob = {
            "phase": 1,
            "date": "2026-05-12",
            "verdict": "ship-after-bugfix",
            "total_cost_usd_estimate": 0.014,
            "total_llm_calls": 12,
            "scenarios": [
                {
                    "name": "dream-cycle",
                    "metrics": {"dreams_generated": 5, "approx_cost_usd": 0.004},
                },
                {"name": "imagination-ab", "metrics": {"approx_cost_usd": 0.003}},
            ],
        }
        s = builder.extract_summary(p1_blob)
        assert s.phase == 1
        assert s.dreams_fired == 5
        # Cost should come from the top-level estimate, not the scenario sum
        assert s.cost_usd == pytest.approx(0.014)
        assert s.llm_calls == 12

    def test_summary_normalizer_handles_live_test_shape(self, builder) -> None:
        """P3+ uses live_test.results. Normalizer must still produce errors=0 etc."""
        p3_blob = {
            "phase": 3,
            "date": "2026-05-13",
            "verdict": "READY TO MERGE",
            "live_test": {
                "duration_seconds": 90.2,
                "results": {
                    "dreams_fired": 10,
                    "errors": 0,
                    "unique_titles": 10,
                    "avg_seconds_per_dream_in_cycle": 8.4,
                    "approx_cost_usd": 0.012,
                },
            },
            "live_calls": 10,
        }
        s = builder.extract_summary(p3_blob)
        assert s.dreams_fired == 10
        assert s.errors == 0
        assert s.unique_titles == 10
        assert s.cost_usd == pytest.approx(0.012)
        assert s.wall_seconds == pytest.approx(90.2)


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
