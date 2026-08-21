"""Regression tests for the v5 CLI config/trust hardening cluster.

Covers findings landed under the 1.0-hardening pass:

* CT-1 — /butcher resolves the session's active model when operator mode is
  off, instead of always resolving the hardcoded Anthropic preset tiers.
* CT-3 — BOG_AGENTS_HOME is honored as the base directory (was a dead override).
* CT-4 — one MCP server with an unresolvable ${VAR} header no longer disables
  every other server.
* CT-5 — the butcher per-slice allowlist is enforced for run_command write
  targets, not just write_file/edit_file.
* CT-6 — secret-bearing atomic writes create the temp file owner-only from the
  start (no umask-default world-readable window on POSIX).
"""

from __future__ import annotations

import stat
import sys
from pathlib import Path

import pytest


class TestBogAgentsHome:
    """CT-3: BOG_AGENTS_HOME must actually redirect the base directory."""

    def test_default_is_dot_bog_agents(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from bog_agents_cli._env_vars import bog_agents_home

        monkeypatch.delenv("BOG_AGENTS_HOME", raising=False)
        assert bog_agents_home() == Path.home() / ".bog-agents"

    def test_override_is_honored(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from bog_agents_cli._env_vars import bog_agents_home

        monkeypatch.setenv("BOG_AGENTS_HOME", str(tmp_path / "custom-home"))
        assert bog_agents_home() == tmp_path / "custom-home"

    def test_blank_override_falls_back_to_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from bog_agents_cli._env_vars import bog_agents_home

        monkeypatch.setenv("BOG_AGENTS_HOME", "   ")
        assert bog_agents_home() == Path.home() / ".bog-agents"

    def test_read_on_every_call(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The override is re-read per call, so a change within the process is
        # honored by call-time consumers.
        from bog_agents_cli._env_vars import bog_agents_home

        monkeypatch.setenv("BOG_AGENTS_HOME", str(tmp_path / "a"))
        assert bog_agents_home() == tmp_path / "a"
        monkeypatch.setenv("BOG_AGENTS_HOME", str(tmp_path / "b"))
        assert bog_agents_home() == tmp_path / "b"


class _FakeSession:
    def __init__(self, active: bool, tiers: dict[str, object]) -> None:
        self.active = active
        self.tiers = tiers


class _Tier:
    def __init__(self, model: str) -> None:
        self.model = model


class TestButcherModelResolution:
    """CT-1: operator preset tiers are used only when operator mode is active."""

    def _resolve(self, monkeypatch: pytest.MonkeyPatch, *, active: bool):
        from bog_agents_cli import butcher

        tiers = {
            "max": _Tier("anthropic:claude-opus-4"),
            "easy": _Tier("anthropic:claude-haiku-4"),
            "medium": _Tier("anthropic:claude-sonnet-4"),
            "hard": _Tier("anthropic:claude-opus-4"),
        }
        monkeypatch.setattr(
            butcher,
            "ensure_session",
            lambda _app: _FakeSession(active, tiers),
            raising=False,
        )
        monkeypatch.setattr(
            "bog_agents_cli.operator_mode.ensure_session",
            lambda _app: _FakeSession(active, tiers),
            raising=False,
        )
        monkeypatch.setattr(
            "bog_agents_cli.feature_helpers.resolve_active_model_spec",
            lambda _app: "bedrock:my-model",
        )
        return butcher._resolve_models(object(), butcher.ButcherConfig())

    def test_operator_off_uses_active_model(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        butcher_model, worker_model, _ladder = self._resolve(monkeypatch, active=False)
        # With operator off and an empty config, both roles must be the
        # session's active model — never the hardcoded Anthropic tiers.
        assert butcher_model == "bedrock:my-model"
        assert worker_model == "bedrock:my-model"

    def test_operator_on_uses_preset_tiers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        butcher_model, worker_model, _ladder = self._resolve(monkeypatch, active=True)
        assert butcher_model == "anthropic:claude-opus-4"  # max tier
        assert worker_model == "anthropic:claude-haiku-4"  # easy tier


class TestButcherRunCommandAllowlist:
    """CT-5: run_command write targets are screened against the slice allowlist."""

    def test_redirect_outside_allowlist_refused(self, tmp_path: Path) -> None:
        from bog_agents_cli.butcher import screen_shell_write_targets

        root = tmp_path.resolve()
        reason = screen_shell_write_targets(
            "echo pwned > /etc/evil.conf", root=root, allow=["src/*.py"]
        )
        assert reason is not None

    def test_redirect_inside_allowlist_permitted(self, tmp_path: Path) -> None:
        from bog_agents_cli.butcher import screen_shell_write_targets

        root = tmp_path.resolve()
        (root / "src").mkdir()
        reason = screen_shell_write_targets(
            "echo ok > src/out.py", root=root, allow=["src/*.py"]
        )
        assert reason is None

    def test_unverifiable_target_refused(self, tmp_path: Path) -> None:
        from bog_agents_cli.butcher import screen_shell_write_targets

        root = tmp_path.resolve()
        reason = screen_shell_write_targets(
            "echo x > $HOME/out.py", root=root, allow=["**/*.py"]
        )
        assert reason is not None

    def test_read_only_command_permitted(self, tmp_path: Path) -> None:
        from bog_agents_cli.butcher import screen_shell_write_targets

        root = tmp_path.resolve()
        assert (
            screen_shell_write_targets("pytest -q", root=root, allow=["src/*.py"])
            is None
        )


class TestSecureAtomicWrite:
    """CT-6: a secret-bearing write is owner-only from creation."""

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits")
    def test_secure_mode_no_world_readable_window(self, tmp_path: Path) -> None:
        from bog_agents_cli.io_utils import atomic_write_text

        target = tmp_path / "vault" / "token.json"
        atomic_write_text(target, '{"token": "secret"}', mode=0o600)
        assert target.read_text(encoding="utf-8") == '{"token": "secret"}'
        file_mode = stat.S_IMODE(target.stat().st_mode)
        assert file_mode == 0o600
        # The freshly created parent dir is owner-only too.
        dir_mode = stat.S_IMODE(target.parent.stat().st_mode)
        assert dir_mode == 0o700

    def test_plain_write_still_works(self, tmp_path: Path) -> None:
        from bog_agents_cli.io_utils import atomic_write_text

        target = tmp_path / "notes.txt"
        atomic_write_text(target, "hello")
        assert target.read_text(encoding="utf-8") == "hello"


class TestMcpHeaderInterpolation:
    """CT-4: an unresolvable ${VAR} raises so the loader can isolate one server.

    The full per-server isolation lives in the async `_load_tools_from_config`
    loop (a server whose headers raise is recorded and skipped while the others
    still load); here we pin the trigger it catches — an undefined variable
    must raise RuntimeError naming the server, not silently yield an empty
    header that would authenticate as nothing.
    """

    def test_undefined_var_raises_named_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from bog_agents_cli.mcp_tools import _interpolate_headers

        monkeypatch.delenv("DEFINITELY_UNSET_TOKEN_XYZ", raising=False)
        monkeypatch.setattr(
            "bog_agents_cli.vars_store.get_var", lambda _name: None, raising=False
        )
        with pytest.raises(RuntimeError, match="my-server"):
            _interpolate_headers(
                {"Authorization": "Bearer ${DEFINITELY_UNSET_TOKEN_XYZ}"},
                "my-server",
            )

    def test_default_value_is_used(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from bog_agents_cli.mcp_tools import _interpolate_headers

        monkeypatch.delenv("MAYBE_UNSET_XYZ", raising=False)
        monkeypatch.setattr(
            "bog_agents_cli.vars_store.get_var", lambda _name: None, raising=False
        )
        out = _interpolate_headers({"X-Env": "${MAYBE_UNSET_XYZ:-fallback}"}, "s")
        assert out["X-Env"] == "fallback"
