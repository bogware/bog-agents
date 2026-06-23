"""Hardening tests for daemon findings S40, S41, S42, S44.

Covers:
- S40: malformed skill frontmatter logs a warning; empty/missing git-push
  ref is rejected (no wildcard jobs fire) instead of matching every job.
- S41: file-change trigger prunes heavy dirs and caps files per tick.
- S42: token file is written atomically with owner-only perms and an
  empty/blank token file is treated as corrupt and regenerated.
- S44: daemon ruff config enables PLW1514 (unspecified-encoding).
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bog_agents_daemon.api import create_app
from bog_agents_daemon.models import AmbientJob, TriggerConfig, TriggerType
from bog_agents_daemon.runner import _parse_skill_frontmatter
from bog_agents_daemon.scheduler import (
    _FILE_TRIGGER_PRUNE_DIRS,
    DaemonScheduler,
    _check_file_trigger,
)
from bog_agents_daemon.store import load_jobs, upsert_job

_TEST_TOKEN = "test-token-abc123"


@pytest.fixture()
def scheduler():
    from unittest.mock import AsyncMock

    return DaemonScheduler(store_loader=load_jobs, runner=AsyncMock())


@pytest.fixture()
def client(tmp_daemon_dir: Path, scheduler: DaemonScheduler) -> TestClient:
    app = create_app(token=_TEST_TOKEN, scheduler=scheduler)
    return TestClient(app, raise_server_exceptions=True)


@pytest.fixture()
def auth() -> dict[str, str]:
    return {"X-Daemon-Token": _TEST_TOKEN}


# ---------------------------------------------------------------------------
# S40 — skill frontmatter diagnostics
# ---------------------------------------------------------------------------


class TestSkillFrontmatterDiagnostics:
    def test_malformed_yaml_logs_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        content = "---\nchain: [unterminated\n---\nbody text\n"
        with caplog.at_level(logging.WARNING, logger="bog_agents_daemon.runner"):
            frontmatter, body = _parse_skill_frontmatter(content, skill_path=Path("SKILL.md"))
        assert frontmatter == {}
        assert body == "body text\n"
        assert any("not valid YAML" in rec.message for rec in caplog.records)

    def test_non_mapping_frontmatter_logs_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        content = "---\n- just\n- a\n- list\n---\nbody\n"
        with caplog.at_level(logging.WARNING, logger="bog_agents_daemon.runner"):
            frontmatter, _body = _parse_skill_frontmatter(content)
        assert frontmatter == {}
        assert any("not a mapping" in rec.message for rec in caplog.records)

    def test_valid_frontmatter_no_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        content = "---\nchain: [a, b]\n---\nbody\n"
        with caplog.at_level(logging.WARNING, logger="bog_agents_daemon.runner"):
            frontmatter, _body = _parse_skill_frontmatter(content)
        assert frontmatter == {"chain": ["a", "b"]}
        assert not caplog.records


# ---------------------------------------------------------------------------
# S40 — git-push empty-ref rejection
# ---------------------------------------------------------------------------


class TestGitPushEmptyRef:
    def _wildcard_push_job(self) -> AmbientJob:
        return AmbientJob(
            name="push-job",
            prompt="run",
            enabled=True,
            triggers=[TriggerConfig(type=TriggerType.GIT_PUSH, git_branch_pattern="*")],
        )

    def test_missing_ref_triggers_nothing(self, client: TestClient, auth: dict, tmp_daemon_dir: Path) -> None:
        upsert_job(self._wildcard_push_job())
        resp = client.post("/webhooks/git-push", json={"new_sha": "abc"}, headers=auth)
        assert resp.status_code == 200
        assert resp.json() == {"triggered": [], "count": 0}

    def test_empty_ref_triggers_nothing(self, client: TestClient, auth: dict, tmp_daemon_dir: Path) -> None:
        upsert_job(self._wildcard_push_job())
        resp = client.post("/webhooks/git-push", json={"ref": ""}, headers=auth)
        assert resp.status_code == 200
        assert resp.json() == {"triggered": [], "count": 0}

    def test_valid_ref_still_triggers(self, client: TestClient, auth: dict, tmp_daemon_dir: Path) -> None:
        upsert_job(self._wildcard_push_job())
        resp = client.post("/webhooks/git-push", json={"ref": "refs/heads/main"}, headers=auth)
        assert resp.status_code == 200
        assert resp.json()["count"] == 1


# ---------------------------------------------------------------------------
# S41 — file-change trigger pruning + cap
# ---------------------------------------------------------------------------


class TestFileTriggerPruning:
    def test_prunes_heavy_dirs(self, tmp_path: Path) -> None:
        # A matching file buried inside a pruned dir must NOT trigger.
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (git_dir / "config.log").write_text("x", encoding="utf-8")
        t = TriggerConfig(type=TriggerType.FILE_CHANGE, watch_dir=str(tmp_path), watch_patterns=["*.log"])
        assert _check_file_trigger(t, 0) is None

    def test_top_level_match_still_detected(self, tmp_path: Path) -> None:
        (tmp_path / "app.log").write_text("x", encoding="utf-8")
        t = TriggerConfig(type=TriggerType.FILE_CHANGE, watch_dir=str(tmp_path), watch_patterns=["*.log"])
        result = _check_file_trigger(t, 0)
        assert result is not None
        assert result.name == "app.log"

    def test_prune_set_contains_expected(self) -> None:
        for expected in (".git", "node_modules", ".venv", "__pycache__", "dist", "build"):
            assert expected in _FILE_TRIGGER_PRUNE_DIRS

    def test_file_cap_bails_out(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
        # Drop the cap to a tiny number and prove the scan stops + logs.
        monkeypatch.setattr("bog_agents_daemon.scheduler._FILE_TRIGGER_MAX_FILES", 2)
        for i in range(5):
            (tmp_path / f"f{i}.txt").write_text("x", encoding="utf-8")
        # Pattern that nothing matches so we don't early-return on a match.
        t = TriggerConfig(type=TriggerType.FILE_CHANGE, watch_dir=str(tmp_path), watch_patterns=["*.nomatch"])
        with caplog.at_level(logging.WARNING, logger="bog_agents_daemon.scheduler"):
            assert _check_file_trigger(t, 0) is None
        assert any("exceeds" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# S42 — atomic, secure, corrupt-resistant token creation
# ---------------------------------------------------------------------------


class TestEnsureToken:
    @pytest.fixture()
    def token_path(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        import bog_agents_daemon.main as main_mod

        path = tmp_path / "daemon" / "token"
        monkeypatch.setattr(main_mod, "_TOKEN_FILE", path)
        return path

    def test_creates_token_when_missing(self, token_path: Path) -> None:
        from bog_agents_daemon.main import _ensure_token

        token = _ensure_token()
        assert token
        assert token_path.read_text(encoding="utf-8").strip() == token

    def test_reuses_existing_token(self, token_path: Path) -> None:
        from bog_agents_daemon.main import _ensure_token

        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text("preexisting-token", encoding="utf-8")
        assert _ensure_token() == "preexisting-token"

    def test_empty_token_file_regenerated(self, token_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        from bog_agents_daemon.main import _ensure_token

        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text("   \n", encoding="utf-8")
        with caplog.at_level(logging.WARNING, logger="bog_agents_daemon.main"):
            token = _ensure_token()
        assert token.strip()
        assert token_path.read_text(encoding="utf-8").strip() == token
        assert any("empty/blank" in rec.message for rec in caplog.records)

    def test_no_temp_files_left_behind(self, token_path: Path) -> None:
        from bog_agents_daemon.main import _ensure_token

        _ensure_token()
        leftovers = list(token_path.parent.glob("*.tmp"))
        assert leftovers == []


# ---------------------------------------------------------------------------
# S44 — ruff config enables PLW1514
# ---------------------------------------------------------------------------


def test_daemon_ruff_enables_plw1514() -> None:
    import tomllib

    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    select = data["tool"]["ruff"]["lint"]["select"]
    # Enabled either explicitly or via the ALL/PLW group.
    assert "PLW1514" in select or "PLW" in select or "ALL" in select
