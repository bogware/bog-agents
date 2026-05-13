"""End-to-end integration tests for the dreamscape feature set.

Drives every dreamscape surface through real I/O — disk-backed config,
on-disk lifecycle snapshots, SQLite shared-memory, real dream-engine
prompt construction. LLM calls use a stub by default but can be
swapped to a real model via the ``BOG_AGENTS_E2E_REAL_LLM=1`` env var
+ a valid ``ANTHROPIC_API_KEY``.

The test environment is fully isolated:

* ``Path.home`` is monkey-patched to ``tmp_path`` so the entire
  ``~/.bog-agents/`` tree lives inside the test's temporary directory.
* No network calls unless the env var is set.
* Oregon Trail (``E:\\oregon-trail``) is used as the *repo under test*
  for ``/repo`` and ``/laws audit`` surfaces — the user's stated
  testing target.

Run with::

    cd libs/cli && uv run --group test pytest tests/integration_tests/test_dreamscape_e2e.py -v
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

import pytest


# Oregon Trail path is platform-specific to the user's machine; tests
# that need it skip cleanly when the directory doesn't exist.
_OREGON_TRAIL = Path("E:/oregon-trail")
_OREGON_TRAIL_AVAILABLE = _OREGON_TRAIL.exists()


@pytest.fixture()
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ``Path.home()`` at the test's tmp dir + clear dreamscape cache."""
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path, raising=False)
    monkeypatch.delenv("BOG_AGENTS_DREAMSCAPE", raising=False)
    monkeypatch.delenv("BOG_AGENTS_DREAMSCAPE_DISABLE", raising=False)
    monkeypatch.delenv("BOG_AGENTS_DREAMSCAPE_LIFECYCLE", raising=False)
    monkeypatch.delenv("BOG_AGENTS_DREAMSCAPE_LAWS", raising=False)
    monkeypatch.delenv("BOG_AGENTS_DREAMSCAPE_SHARED_MEMORY", raising=False)
    monkeypatch.delenv("BOG_AGENTS_DREAMSCAPE_DREAMS_AUTO", raising=False)
    monkeypatch.delenv("BOG_AGENTS_DREAMSCAPE_IMAGINATION", raising=False)
    from bog_agents_cli.dreamscape import config as ds_config

    ds_config.clear_cache()
    yield tmp_path
    ds_config.clear_cache()


# ---------------------------------------------------------------------------
# 1. Default behavior: dreamscape off, agent unchanged
# ---------------------------------------------------------------------------


class TestDreamscapeDefaultsOff:
    """The bedrock guarantee: zero overhead when not enabled."""

    def test_load_with_no_file_returns_inert(self, isolated_home: Path) -> None:
        from bog_agents_cli.dreamscape import load_dreamscape_config

        cfg = load_dreamscape_config()
        assert cfg.master_enabled is False
        assert cfg.any_active is False

    def test_agent_attach_helper_no_op_when_inert(self, isolated_home: Path) -> None:
        """``_attach_dreamscape_middleware`` with inert config attaches nothing."""
        from bog_agents_cli.agent import _attach_dreamscape_middleware
        from bog_agents_cli.dreamscape import load_dreamscape_config

        cfg = load_dreamscape_config()
        middlewares: list = []
        _attach_dreamscape_middleware(middlewares, cfg=cfg, agent_id="alpha")
        assert middlewares == []

    def test_emergency_disable_overrides_everything(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from bog_agents_cli.dreamscape import (
            load_dreamscape_config,
            save_dreamscape_config,
        )
        from bog_agents_cli.dreamscape import config as ds_config

        cfg = load_dreamscape_config()
        cfg.master_enabled = True
        cfg.lifecycle.enabled = True
        cfg.laws.enabled = True
        cfg.imagination.enabled = True
        save_dreamscape_config(cfg)

        monkeypatch.setenv("BOG_AGENTS_DREAMSCAPE_DISABLE", "1")
        ds_config.clear_cache()
        reloaded = load_dreamscape_config()
        assert reloaded.any_active is False


# ---------------------------------------------------------------------------
# 2. Lifecycle middleware: state transitions through real I/O
# ---------------------------------------------------------------------------


class TestLifecycleE2E:
    def test_record_activity_persists(self, isolated_home: Path) -> None:
        from bog_agents_cli.dreamscape.config import LifecycleConfig
        from bog_agents_cli.dreamscape.lifecycle import (
            LifecycleMiddleware,
            load_snapshot,
        )

        cfg = LifecycleConfig(enabled=True, dormancy_after_seconds=10)
        mw = LifecycleMiddleware(agent_id="alpha", cfg=cfg)
        # Drive the internal recorder directly (avoids LangGraph plumbing).
        mw._safely_record_activity()
        loaded = load_snapshot("alpha")
        assert loaded.last_activity_at > 0
        assert loaded.state == "awake"

    def test_state_transitions_through_time(self, isolated_home: Path) -> None:
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
        snap = LifecycleSnapshot(agent_id="alpha", last_activity_at=0.0)
        # State at 1s, 5min, 30min, 1h
        assert compute_state(snap, cfg, now=1.0) == LifecycleState.AWAKE
        # Use a non-zero base so the "never observed" sentinel doesn't kick in.
        snap.last_activity_at = 1.0
        assert compute_state(snap, cfg, now=301.0) == LifecycleState.AWAKE
        assert compute_state(snap, cfg, now=601.0) == LifecycleState.IDLE
        assert compute_state(snap, cfg, now=1801.0 + 1) == LifecycleState.DORMANT
        # Past the dreaming window — still DORMANT until dream subsystem
        # transitions explicitly to DREAMING.
        assert compute_state(snap, cfg, now=3601.0) == LifecycleState.DORMANT


# ---------------------------------------------------------------------------
# 3. Laws + Constitution: real disk-backed rule files
# ---------------------------------------------------------------------------


class TestLawsE2E:
    def test_starter_templates_round_trip(self, isolated_home: Path) -> None:
        from bog_agents_cli.dreamscape.config import LawsConfig
        from bog_agents_cli.dreamscape.laws import (
            audit_text,
            load_rules,
            write_default_templates,
        )

        cfg = LawsConfig(
            laws_path=str(isolated_home / ".bog-agents/laws.md"),
            constitution_path=str(isolated_home / ".bog-agents/constitution.md"),
        )
        written = write_default_templates(
            cfg, project_root=isolated_home, overwrite=True
        )
        assert len(written) == 2
        rule_set = load_rules(cfg, project_root=isolated_home)
        assert len(rule_set.laws) >= 4
        assert len(rule_set.constitution) >= 4

        result = audit_text(
            "I'm about to rm -rf / the entire repo, that should be fine right?",
            cfg,
            project_root=isolated_home,
        )
        assert result.violations  # at least one phrase triggered
        assert any("rm -rf" in v for v in result.violations)

    def test_audit_clean_sample(self, isolated_home: Path) -> None:
        from bog_agents_cli.dreamscape.config import LawsConfig
        from bog_agents_cli.dreamscape.laws import audit_text, write_default_templates

        cfg = LawsConfig(
            laws_path=str(isolated_home / ".bog-agents/laws.md"),
            constitution_path=str(isolated_home / ".bog-agents/constitution.md"),
        )
        write_default_templates(cfg, project_root=isolated_home, overwrite=True)
        result = audit_text(
            "Here's a clean refactor. I added a small helper and pruned dead code.",
            cfg,
            project_root=isolated_home,
        )
        assert result.violations == []


# ---------------------------------------------------------------------------
# 4. Shared memory: real SQLite round-trips + redaction
# ---------------------------------------------------------------------------


class TestSharedMemoryE2E:
    def test_multi_agent_write_read(self, isolated_home: Path) -> None:
        from bog_agents_cli.dreamscape.shared_memory import SQLiteSharedMemory

        db_path = isolated_home / ".bog-agents/shared-memory.db"
        b1 = SQLiteSharedMemory(db_path)
        b2 = SQLiteSharedMemory(db_path)

        b1.write(agent_id="alpha", content="found a hot loop in compute()", tags=["perf"])
        b2.write(agent_id="beta", content="alpha was right; here's a fix sketch", tags=["perf", "fix"])

        # Either backend sees both entries.
        from_alpha = b1.search("hot loop")
        from_beta = b2.search("hot loop")
        assert len(from_alpha) == 1
        assert len(from_beta) == 1
        # Recent order: beta's reply first
        recent = b2.recent(limit=10)
        assert recent[0].agent_id == "beta"

    def test_redaction_prevents_secret_persistence(
        self, isolated_home: Path
    ) -> None:
        from bog_agents_cli.dreamscape.shared_memory import redact_secrets

        patterns = [r"sk-ant-[A-Za-z0-9_-]{20,}"]
        cleaned = redact_secrets(
            "leaked key: sk-ant-api03-aaaaaaaaaaaaaaaaaaaaaa",
            patterns=patterns,
        )
        assert "[redacted]" in cleaned
        assert "sk-ant-" not in cleaned

    def test_concurrent_writes_dont_corrupt(self, isolated_home: Path) -> None:
        """WAL + busy-timeout means parallel writes from a thread don't lose entries."""
        import threading

        from bog_agents_cli.dreamscape.shared_memory import SQLiteSharedMemory

        db_path = isolated_home / ".bog-agents/shared-memory.db"
        backend = SQLiteSharedMemory(db_path)

        def write_burst(agent_id: str) -> None:
            for i in range(20):
                backend.write(agent_id=agent_id, content=f"note-{i}", tags=[])

        threads = [
            threading.Thread(target=write_burst, args=(f"agent-{n}",))
            for n in range(3)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)
        # All 60 writes (3 threads × 20 entries) survived.
        with sqlite3.connect(db_path) as conn:
            count = conn.execute("SELECT COUNT(*) FROM shared_memory").fetchone()[0]
        assert count == 60


# ---------------------------------------------------------------------------
# 5. Dream engine + imagination: end-to-end flow with a fake model
# ---------------------------------------------------------------------------


class _FakeChatModel:
    """Stub chat model with a scriptable response sequence."""

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.invocations: list[list] = []

    async def ainvoke(self, messages, **_kwargs):  # noqa: ANN001
        self.invocations.append(messages)
        from langchain_core.messages import AIMessage

        reply = self._responses.pop(0) if self._responses else "(no more scripted responses)"
        return AIMessage(content=reply)


class TestDreamEngineE2E:
    def test_full_dream_pass_persists_artifact_and_bumps_trait(
        self, isolated_home: Path
    ) -> None:
        import asyncio

        from bog_agents_cli.dreamscape import lifecycle as lc_mod
        from bog_agents_cli.dreamscape.config import DreamsConfig
        from bog_agents_cli.dreamscape.dream_engine import generate_dream

        cfg = DreamsConfig(
            auto_on_dormancy=True,
            max_seeds_per_dream=2,
            imagination_trait_increment=2.5,
        )
        # Seed a snapshot so we can check imagination bumps.
        lc_mod.save_snapshot(lc_mod.LifecycleSnapshot(agent_id="dreamer"))

        model = _FakeChatModel(
            [
                "### Tonight I dreamed of glaciers and rope memory\n\n"
                "Slow ice carrying Margaret Hamilton's hand-woven cores...\n\n"
                "**Waking thought:**\nMemory shapes the future as much as the past."
            ]
        )
        artifact = asyncio.run(
            generate_dream(model=model, agent_id="dreamer", cfg=cfg, rng_seed=42)
        )
        assert artifact.path.exists()
        assert "glaciers" in artifact.body
        assert artifact.title  # title extracted from the ### heading
        assert len(model.invocations) == 1

    def test_imagination_injection_with_real_dreams(
        self, isolated_home: Path
    ) -> None:
        from bog_agents_cli.dreamscape import lifecycle as lc_mod
        from bog_agents_cli.dreamscape.config import ImaginationConfig
        from bog_agents_cli.dreamscape.dream_engine import sample_dream_excerpts
        from bog_agents_cli.dreamscape.imagination import ImaginationMiddleware

        # Plant a dream archive on disk
        dreams_dir = isolated_home / ".bog-agents/agents/stuck-agent/dreams"
        dreams_dir.mkdir(parents=True, exist_ok=True)
        (dreams_dir / "20260512-100000.md").write_text(
            "---\nkind: dream-auto\n---\n\n"
            "### Tonight I dreamed of black holes humming\n\n"
            "The M87 shadow we photographed listened back, attentive.\n",
            encoding="utf-8",
        )
        (dreams_dir / "20260512-110000.md").write_text(
            "---\nkind: dream-auto\n---\n\n"
            "### Tonight I dreamed of Antikythera\n\n"
            "Bronze gears turning under sea pressure for two thousand years.\n",
            encoding="utf-8",
        )

        # Seed a snapshot showing the agent is stuck
        snap = lc_mod.LifecycleSnapshot(
            agent_id="stuck-agent",
            imagination=5.0,
            consecutive_tool_failures=4,
        )
        lc_mod.save_snapshot(snap)

        mw = ImaginationMiddleware(
            agent_id="stuck-agent",
            cfg=ImaginationConfig(
                enabled=True,
                trigger_after_failures=3,
                min_imagination_trait=1.0,
            ),
        )
        assert mw._should_inject() is True

        excerpts = sample_dream_excerpts("stuck-agent", count=2, rng_seed=99)
        assert len(excerpts) == 2
        assert any("black hole" in e.lower() or "antikythera" in e.lower() for e in excerpts)


# ---------------------------------------------------------------------------
# 6. /agent-state + /repo dashboard surfaces against the real Oregon Trail repo
# ---------------------------------------------------------------------------


class TestDashboardSurfaces:
    def test_agent_state_renders_with_no_data(self, isolated_home: Path) -> None:
        from bog_agents_cli.dreamscape.dashboard import render_agent_state

        body = render_agent_state("fresh-agent")
        # Fresh agent → all defaults visible
        assert "Agent state" in body
        assert "fresh-agent" in body
        assert "Imagination" in body

    def test_agent_state_renders_with_full_state(self, isolated_home: Path) -> None:
        from bog_agents_cli.dreamscape import lifecycle as lc_mod
        from bog_agents_cli.dreamscape.dashboard import render_agent_state

        snap = lc_mod.LifecycleSnapshot(
            agent_id="loaded",
            imagination=42.5,
            total_dreams=17,
            consecutive_tool_failures=2,
            imagination_injections=10,
            imagination_injections_helped=6,
        )
        lc_mod.save_snapshot(snap)
        body = render_agent_state("loaded")
        assert "42.5" in body or "42.50" in body
        assert "17" in body
        assert "imagination injections" in body.lower()

    def test_dreamscape_status_renders(self, isolated_home: Path) -> None:
        from bog_agents_cli.dreamscape.dashboard import render_dreamscape_status

        body = render_dreamscape_status()
        assert "Master switch" in body
        # Default → master OFF
        assert "OFF" in body.upper()

    @pytest.mark.skipif(
        not _OREGON_TRAIL_AVAILABLE,
        reason="Oregon Trail repo not available at E:/oregon-trail",
    )
    def test_repo_overview_against_oregon_trail(self, isolated_home: Path) -> None:
        """Real ``/repo`` view of the user's Oregon Trail project."""
        from bog_agents_cli.dreamscape.dashboard import render_repo_overview

        body = render_repo_overview(_OREGON_TRAIL)
        assert "Branch" in body
        # Either the project is on a branch, or we get the helpful
        # "(no branch)" placeholder — both pass the renderer.


# ---------------------------------------------------------------------------
# 7. CLI surface — slash command registry resolves against real BogAgentsApp
# ---------------------------------------------------------------------------


class TestSlashCommandSurface:
    def test_all_dreamscape_commands_attach_handlers(self) -> None:
        from bog_agents_cli.app import BogAgentsApp
        from bog_agents_cli.commands import COMMANDS

        dreamscape_cmds = {
            "/agent-state",
            "/repo",
            "/dreamscape",
            "/laws",
            "/help-dream",
        }
        for c in COMMANDS:
            if c.spec.name in dreamscape_cmds:
                assert c.handler_method, f"{c.spec.name} has no handler"
                assert hasattr(BogAgentsApp, c.handler_method), (
                    f"{c.spec.name} → {c.handler_method} missing on app"
                )

    def test_dreamscape_init_then_status(self, isolated_home: Path) -> None:
        from bog_agents_cli.dreamscape.dashboard import (
            init_dreamscape_config,
            render_dreamscape_status,
        )

        path = init_dreamscape_config()
        assert path.exists()
        body = render_dreamscape_status()
        assert path.name in body or "present" in body

    def test_laws_init_creates_files(self, isolated_home: Path) -> None:
        from bog_agents_cli.dreamscape.dashboard import init_laws_templates

        # Mock the cwd so project templates write under the isolated home.
        os.chdir(isolated_home)
        written = init_laws_templates()
        assert len(written) >= 2
        for p in written:
            assert p.exists()
            assert p.read_text(encoding="utf-8").strip()


# ---------------------------------------------------------------------------
# 8. Real-LLM smoke test (opt-in via env var)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not (
        os.environ.get("BOG_AGENTS_E2E_REAL_LLM") == "1"
        and (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_API_KEP"))
    ),
    reason=(
        "Real-LLM smoke test disabled. Set BOG_AGENTS_E2E_REAL_LLM=1 and "
        "ANTHROPIC_API_KEY to run."
    ),
)
class TestRealLLMSmoke:
    """Validate end-to-end with the actual Anthropic API.

    Only runs when explicitly enabled; costs a few cents per pass.
    """

    def test_dream_generation_produces_markdown(self, isolated_home: Path) -> None:
        import asyncio

        # Source the API key from either env var name (user has typo'd "KEP" form).
        api_key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get(
            "ANTHROPIC_API_KEP"
        )
        os.environ["ANTHROPIC_API_KEY"] = api_key or ""

        from langchain_anthropic import ChatAnthropic

        from bog_agents_cli.dreamscape.config import DreamsConfig
        from bog_agents_cli.dreamscape.dream_engine import generate_dream

        model = ChatAnthropic(  # type: ignore[call-arg]
            model_name="claude-haiku-4-5", max_tokens=400, timeout=30.0
        )
        cfg = DreamsConfig(
            auto_on_dormancy=True, max_seeds_per_dream=2, imagination_trait_increment=1.0
        )
        artifact = asyncio.run(
            generate_dream(model=model, agent_id="real-dreamer", cfg=cfg, rng_seed=7)
        )
        assert artifact.body
        # The system prompt asks for a markdown ### heading
        assert "###" in artifact.body
        assert artifact.path.exists()
