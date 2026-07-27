"""Tests for wiring the OS sandbox into `LocalShellBackend` (#22).

Covers three things, all platform-independently (the launcher is stubbed via
`get_platform_sandbox_support`, so these run on Windows CI too):

  * `local_sandbox` network gating: `--share-net` / seatbelt `(allow network*)`
    follow `network_enabled` (unrestricted OR allowlisted), and a hard cut is
    kept otherwise.
  * `LocalShellBackend._prepare_execution`: no-sandbox is a plain shell string;
    an available launcher yields a wrapped argv with `shell=False`; an
    unavailable launcher either fails closed (`require_sandbox`) or falls back.
  * egress env injection: an allowlist starts an internal proxy; a
    runner-provided proxy URL is honored without starting one; unrestricted
    network adds no proxy vars.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bog_agents.backends.local_shell import LocalShellBackend
from bog_agents.sandbox import local_sandbox
from bog_agents.sandbox.egress_proxy import SANDBOX_EGRESS_PROXY_ENV
from bog_agents.sandbox.local_sandbox import (
    LocalSandbox,
    SandboxLevel,
    SandboxSupport,
)


def _force_bwrap(monkeypatch: pytest.MonkeyPatch) -> None:
    support = SandboxSupport(platform="linux", bubblewrap_available=True, best_method="bubblewrap")
    monkeypatch.setattr(local_sandbox, "get_platform_sandbox_support", lambda: support)


def _force_no_launcher(monkeypatch: pytest.MonkeyPatch) -> None:
    support = SandboxSupport(platform="windows")
    monkeypatch.setattr(local_sandbox, "get_platform_sandbox_support", lambda: support)


class TestNetworkGating:
    def test_bwrap_shares_net_when_unrestricted(self, tmp_path: Path) -> None:
        sb = LocalSandbox(working_dir=tmp_path, allow_network=True)
        args = local_sandbox._build_bubblewrap_args(sb)
        assert "--share-net" in args
        assert "--unshare-net" not in args

    def test_bwrap_cuts_net_by_default(self, tmp_path: Path) -> None:
        sb = LocalSandbox(working_dir=tmp_path)
        args = local_sandbox._build_bubblewrap_args(sb)
        # net is cut by --unshare-all; we must NOT re-share it.
        assert "--unshare-all" in args
        assert "--share-net" not in args

    def test_allowlist_keeps_network_namespace(self, tmp_path: Path) -> None:
        sb = LocalSandbox(working_dir=tmp_path, network_allowlist=["pypi.org"])
        assert sb.network_enabled is True
        assert "--share-net" in local_sandbox._build_bubblewrap_args(sb)

    def test_seatbelt_allows_network_for_allowlist(self, tmp_path: Path) -> None:
        sb = LocalSandbox(working_dir=tmp_path, network_allowlist=["pypi.org"])
        assert "(allow network*)" in local_sandbox._build_seatbelt_profile(sb)

    def test_launcher_available_reflects_support(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _force_bwrap(monkeypatch)
        assert local_sandbox.sandbox_launcher_available() is True
        _force_no_launcher(monkeypatch)
        assert local_sandbox.sandbox_launcher_available() is False


class TestPrepareExecution:
    def test_no_sandbox_is_plain_shell(self, tmp_path: Path) -> None:
        backend = LocalShellBackend(root_dir=tmp_path)
        cmd, use_shell, _env = backend._prepare_execution("echo hi")
        assert cmd == "echo hi"
        assert use_shell is True

    def test_wraps_when_launcher_available(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _force_bwrap(monkeypatch)
        sb = LocalSandbox(level=SandboxLevel.WORKSPACE_WRITE, working_dir=tmp_path)
        backend = LocalShellBackend(root_dir=tmp_path, sandbox=sb)
        cmd, use_shell, _env = backend._prepare_execution("echo hi")
        assert use_shell is False
        assert isinstance(cmd, list)
        assert cmd[0] == "bwrap"
        assert cmd[-3:] == ["sh", "-c", "echo hi"]

    def test_require_sandbox_fails_closed(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _force_no_launcher(monkeypatch)
        sb = LocalSandbox(working_dir=tmp_path)
        backend = LocalShellBackend(root_dir=tmp_path, sandbox=sb, require_sandbox=True)
        with pytest.raises(PermissionError, match="no OS launcher"):
            backend._prepare_execution("echo hi")

    def test_unavailable_falls_back_unsandboxed(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _force_no_launcher(monkeypatch)
        sb = LocalSandbox(working_dir=tmp_path)
        backend = LocalShellBackend(root_dir=tmp_path, sandbox=sb)  # require_sandbox=False
        cmd, use_shell, _env = backend._prepare_execution("echo hi")
        assert cmd == "echo hi"
        assert use_shell is True

    def test_disabled_level_is_plain_shell(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _force_bwrap(monkeypatch)
        sb = LocalSandbox(level=SandboxLevel.DISABLED, working_dir=tmp_path)
        backend = LocalShellBackend(root_dir=tmp_path, sandbox=sb)
        cmd, use_shell, _env = backend._prepare_execution("echo hi")
        assert cmd == "echo hi"
        assert use_shell is True


class TestEgressEnvInjection:
    def test_allowlist_starts_internal_proxy(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _force_bwrap(monkeypatch)
        monkeypatch.delenv(SANDBOX_EGRESS_PROXY_ENV, raising=False)
        try:
            import pytest_socket

            pytest_socket.enable_socket()
        except ImportError:
            pass
        sb = LocalSandbox(working_dir=tmp_path, network_allowlist=["github.com"])
        backend = LocalShellBackend(root_dir=tmp_path, sandbox=sb)
        try:
            _cmd, _use_shell, env = backend._prepare_execution("echo hi")
            assert env["HTTPS_PROXY"].startswith("http://127.0.0.1:")
            assert "127.0.0.1" in env["NO_PROXY"]
            assert backend._egress_proxy is not None
        finally:
            backend.close()
        assert backend._egress_proxy is None

    def test_runner_proxy_url_is_honored_without_starting_one(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _force_bwrap(monkeypatch)
        sb = LocalSandbox(working_dir=tmp_path, allow_network=True)
        backend = LocalShellBackend(
            root_dir=tmp_path,
            sandbox=sb,
            env={SANDBOX_EGRESS_PROXY_ENV: "http://127.0.0.1:9999"},
        )
        _cmd, _use_shell, env = backend._prepare_execution("echo hi")
        assert env["HTTP_PROXY"] == "http://127.0.0.1:9999"
        assert backend._egress_proxy is None  # no internal proxy started

    def test_unrestricted_network_adds_no_proxy(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _force_bwrap(monkeypatch)
        monkeypatch.delenv(SANDBOX_EGRESS_PROXY_ENV, raising=False)
        sb = LocalSandbox(working_dir=tmp_path, allow_network=True)
        backend = LocalShellBackend(root_dir=tmp_path, sandbox=sb)
        _cmd, _use_shell, env = backend._prepare_execution("echo hi")
        assert "HTTPS_PROXY" not in env
        assert backend._egress_proxy is None
