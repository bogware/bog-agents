"""Regression tests for the REVIEW.md P1 cleanup batch.

Each class corresponds to one P1 item; cross-reference the REVIEW.md
section codes in the docstrings.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# P1-1: webhook payload redaction
# ---------------------------------------------------------------------------


class TestP11WebhookRedaction:
    """``tool_args`` and ``metadata`` are redacted before payloads leave the agent."""

    @pytest.mark.asyncio
    async def test_secret_shaped_values_redacted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from bog_agents.middleware.http_hooks import (
            HttpHooksMiddleware,
            WebhookEndpoint,
            WebhookEvent,
        )

        captured: list = []

        async def fake_fire(endpoint, payload):
            captured.append(payload)

        monkeypatch.setattr("bog_agents.middleware.http_hooks.fire_webhook", fake_fire)

        endpoint = WebhookEndpoint(
            url="http://example.com/hook",
            events=[WebhookEvent.PRE_TOOL_USE],
        )
        mw = HttpHooksMiddleware(endpoints=[endpoint])
        await mw._fire_event(
            WebhookEvent.PRE_TOOL_USE,
            tool_name="shell",
            tool_args={
                "command": "echo sk-abc123def456ghi789jkl0123456789",
                "api_key": "supersecret",
                "nested": {"token": "ghp_aaaabbbbccccddddeeeeffffgggghhhh00"},
            },
            metadata={"safe": "value"},
        )
        assert captured, "no payload emitted"
        payload = captured[0]
        # Top-level api_key by name → redacted.
        assert payload.tool_args["api_key"] == "[REDACTED]"
        # Token-shaped string inside command → redacted (in-place
        # ``sub`` not used; value-pattern hits go whole-string).
        assert "sk-abc123" not in payload.tool_args["command"]
        # Nested secret-named key → redacted.
        assert payload.tool_args["nested"]["token"] == "[REDACTED]"
        # Safe values untouched.
        assert payload.metadata["safe"] == "value"

    @pytest.mark.asyncio
    async def test_redaction_can_be_disabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from bog_agents.middleware.http_hooks import (
            HttpHooksMiddleware,
            WebhookEndpoint,
            WebhookEvent,
        )

        captured: list = []

        async def fake_fire(endpoint, payload):
            captured.append(payload)

        monkeypatch.setattr("bog_agents.middleware.http_hooks.fire_webhook", fake_fire)
        mw = HttpHooksMiddleware(
            endpoints=[
                WebhookEndpoint(
                    url="http://x/",
                    events=[WebhookEvent.ON_AGENT_START],
                )
            ],
            redact_secret_shaped_args=False,
        )
        await mw._fire_event(
            WebhookEvent.ON_AGENT_START,
            tool_args={"api_key": "plaintext"},
        )
        assert captured[0].tool_args["api_key"] == "plaintext"

    @pytest.mark.asyncio
    async def test_payload_filter_runs_after_redaction(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from bog_agents.middleware.http_hooks import (
            HttpHooksMiddleware,
            WebhookEndpoint,
            WebhookEvent,
        )

        captured: list = []

        async def fake_fire(endpoint, payload):
            captured.append(payload)

        monkeypatch.setattr("bog_agents.middleware.http_hooks.fire_webhook", fake_fire)

        def filter_fn(payload):
            payload.metadata["filter_ran"] = True
            return payload

        mw = HttpHooksMiddleware(
            endpoints=[
                WebhookEndpoint(
                    url="http://x/", events=[WebhookEvent.ON_AGENT_START]
                )
            ],
            payload_filter=filter_fn,
        )
        await mw._fire_event(WebhookEvent.ON_AGENT_START)
        assert captured[0].metadata["filter_ran"] is True


# ---------------------------------------------------------------------------
# P1-2: filesystem traversal substring vs parts check
# ---------------------------------------------------------------------------


class TestP12PathTraversal:
    """The virtual-mode traversal check splits on path separators so a
    legitimate filename containing ``..`` isn't a false positive.
    """

    def test_legitimate_double_dot_in_name_allowed(self, tmp_path: Path) -> None:
        from bog_agents.backends.filesystem import FilesystemBackend

        backend = FilesystemBackend(root_dir=str(tmp_path), virtual_mode=True)
        # A filename containing ``..`` (e.g. ``version..backup``) is
        # legitimate. The old substring check rejected it; the new parts
        # check allows it.
        target = tmp_path / "version..backup"
        target.write_text("ok", encoding="utf-8")
        out = backend.download_files(["/version..backup"])
        assert out[0].content == b"ok"

    def test_real_traversal_still_rejected(self, tmp_path: Path) -> None:
        from bog_agents.backends.filesystem import FilesystemBackend

        backend = FilesystemBackend(root_dir=str(tmp_path), virtual_mode=True)
        out = backend.download_files(["/../escape.txt"])
        # download_files normalises rejection reasons; the important
        # invariant is that the call did NOT return content.
        assert out[0].content is None
        assert out[0].error is not None


# ---------------------------------------------------------------------------
# P1-7: SSO stub fires NOTSECURE warning on init
# ---------------------------------------------------------------------------


class TestP17SsoWarning:
    def test_first_instantiation_warns(self) -> None:
        from bog_agents.middleware.sso_auth import SSOAuthMiddleware

        # Reset the once-per-process flag so the warning fires for this test.
        SSOAuthMiddleware._NOTSECURE_WARNING_FIRED = False
        with pytest.warns(UserWarning, match="STUB"):
            SSOAuthMiddleware()


# ---------------------------------------------------------------------------
# P1-8: skill loader rejects symlinked skill directory
# ---------------------------------------------------------------------------


@dataclass
class _StubBackend:
    """Minimal in-memory backend mimicking BackendProtocol surface."""

    items_response: list
    downloaded_paths: list = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.downloaded_paths = []

    def ls_info(self, path: str) -> list:
        return self.items_response

    def download_files(self, paths: list) -> list:
        self.downloaded_paths.extend(paths)
        return []


class TestP18SkillSymlinks:
    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="Windows symlink creation requires admin / dev-mode",
    )
    def test_symlinked_skill_dir_is_skipped(self, tmp_path: Path) -> None:
        # Create one real skill dir + one symlinked skill dir pointing
        # outside the project root.
        real = tmp_path / "real-skill"
        real.mkdir()
        (real / "SKILL.md").write_text("---\nname: real\n---\n", encoding="utf-8")
        outside = tmp_path.parent / "hostile"
        outside.mkdir(exist_ok=True)
        link = tmp_path / "hostile-skill"
        link.symlink_to(outside)

        from bog_agents.middleware.skills import _list_skills

        backend = _StubBackend(
            items_response=[
                {"path": str(real), "is_dir": True},
                {"path": str(link), "is_dir": True},
            ]
        )
        _list_skills(backend, str(tmp_path))
        # Only the real skill's SKILL.md should be requested for download.
        downloaded = backend.downloaded_paths
        assert any("real-skill" in p for p in downloaded)
        assert not any("hostile-skill" in p for p in downloaded), downloaded


# ---------------------------------------------------------------------------
# P1-9: git ref validator + -- separator
# ---------------------------------------------------------------------------


class TestP19RefValidator:
    @pytest.mark.parametrize(
        "good_ref",
        [
            "main",
            "feature/expert-mode",
            "release-2.0",
            "user_branch_42",
        ],
    )
    def test_safe_names_accepted(self, good_ref: str) -> None:
        from bog_agents.middleware.worktree import _validate_git_ref

        assert _validate_git_ref(good_ref) == good_ref

    @pytest.mark.parametrize(
        "bad_ref",
        [
            "-exec=evil",
            "--no-verify",
            "..",
            "feature/..",
            "branch.lock",
            "branch//foo",
            "branch\\windows",
            "branch@{0}",
            "",
            "branch with space",
            "branch;rm",
        ],
    )
    def test_hostile_names_rejected(self, bad_ref: str) -> None:
        from bog_agents.middleware.worktree import _validate_git_ref

        with pytest.raises(ValueError):
            _validate_git_ref(bad_ref)


# ---------------------------------------------------------------------------
# P1-5: cost_tracker snapshot cap
# ---------------------------------------------------------------------------


class TestP15CostTrackerCap:
    def test_snapshots_capped_at_max_snapshots(self) -> None:
        from bog_agents.middleware.cost_tracker import CostTracker

        store = CostTracker(model_name="test", max_snapshots=5)
        for _ in range(20):
            store.record_usage(input_tokens=1, output_tokens=1)
        # 20 inserts, cap at 5 — list shrinks to exactly 5.
        assert len(store.snapshots) == 5
        # The totals on the parent object stay correct (cap only
        # affects the per-call history list).
        assert store.input_tokens == 20
        assert store.output_tokens == 20


# Provide an asyncio fixture; tests above use @pytest.mark.asyncio.
@pytest.fixture
def anyio_backend() -> str:  # pragma: no cover — pytest-asyncio wiring
    return "asyncio"
