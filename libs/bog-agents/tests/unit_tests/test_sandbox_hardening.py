"""Tests for the deepened sandbox: secret-env stripping + read-deny paths (#11)."""

from __future__ import annotations

from pathlib import Path

import pytest

from bog_agents.backends.local_shell import LocalShellBackend
from bog_agents.sandbox import local_sandbox
from bog_agents.sandbox.local_sandbox import (
    LocalSandbox,
    SandboxLevel,
    SandboxSupport,
    _build_bubblewrap_args,
    _build_seatbelt_profile,
    strip_secret_env,
)


class TestStripSecretEnv:
    def test_strips_secret_named_vars(self) -> None:
        env = {
            "ANTHROPIC_API_KEY": "sk-x",
            "AWS_SECRET_ACCESS_KEY": "y",
            "GITHUB_TOKEN": "gh",
            "DB_PASSWORD": "p",
            "PATH": "/usr/bin",
            "HOME": "/home/u",
            "LANG": "en_US.UTF-8",
        }
        out = strip_secret_env(env)
        assert set(out) == {"PATH", "HOME", "LANG"}

    def test_case_insensitive(self) -> None:
        assert "my_secret_thing" not in strip_secret_env({"my_secret_thing": "x", "ok": "y"})

    def test_custom_patterns(self) -> None:
        out = strip_secret_env({"FOO_COOKIE": "x", "API_KEY": "y", "BAR": "z"}, patterns=["COOKIE"])
        # Only COOKIE stripped; API_KEY kept because it's not in the custom list.
        assert set(out) == {"API_KEY", "BAR"}

    def test_does_not_mutate_input(self) -> None:
        env = {"SECRET": "x", "OK": "y"}
        strip_secret_env(env)
        assert "SECRET" in env  # original untouched


class TestBubblewrapDenyPaths:
    def test_file_denied_via_devnull_bind(self, tmp_path: Path) -> None:
        secret = tmp_path / "creds.txt"
        secret.write_text("top secret", encoding="utf-8")
        sb = LocalSandbox(level=SandboxLevel.WORKSPACE_WRITE, working_dir=tmp_path, deny_read_paths=[str(secret)])
        args = _build_bubblewrap_args(sb)
        # A /dev/null ro-bind over the secret file.
        joined = " ".join(args)
        assert "--ro-bind /dev/null" in joined
        assert str(secret) in args

    def test_dir_denied_via_tmpfs(self, tmp_path: Path) -> None:
        secret_dir = tmp_path / ".ssh"
        secret_dir.mkdir()
        sb = LocalSandbox(working_dir=tmp_path, deny_read_paths=[str(secret_dir)])
        args = _build_bubblewrap_args(sb)
        # An empty tmpfs over the secret dir.
        assert "--tmpfs" in args
        assert str(secret_dir) in args

    def test_deny_after_workspace_bind(self, tmp_path: Path) -> None:
        secret = tmp_path / "creds.txt"
        secret.write_text("x", encoding="utf-8")
        sb = LocalSandbox(working_dir=tmp_path, deny_read_paths=[str(secret)])
        args = _build_bubblewrap_args(sb)
        # The deny bind must come AFTER the workspace bind so it wins.
        work_idx = args.index(str(tmp_path))
        secret_idx = args.index(str(secret))
        assert secret_idx > work_idx


class TestSeatbeltDenyPaths:
    def test_deny_rule_emitted_last(self, tmp_path: Path) -> None:
        secret = tmp_path / "creds.txt"
        sb = LocalSandbox(working_dir=tmp_path, deny_read_paths=[str(secret)])
        profile = _build_seatbelt_profile(sb)
        assert f'(deny file-read* (subpath "{secret}"))' in profile
        # The deny must appear after the workspace allow so it overrides.
        assert profile.index("(deny file-read*") > profile.index(f'(allow file-read* (subpath "{tmp_path}"))')


class TestBackendSecretStripping:
    def _force_bwrap(self, monkeypatch: pytest.MonkeyPatch) -> None:
        support = SandboxSupport(platform="linux", bubblewrap_available=True, best_method="bubblewrap")
        monkeypatch.setattr(local_sandbox, "get_platform_sandbox_support", lambda: support)

    def test_sandboxed_child_env_strips_secrets(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        self._force_bwrap(monkeypatch)
        sb = LocalSandbox(working_dir=tmp_path)  # strip_secret_env defaults True
        be = LocalShellBackend(
            root_dir=str(tmp_path),
            sandbox=sb,
            env={"ANTHROPIC_API_KEY": "sk", "PATH": "/usr/bin", "HOME": "/h"},
        )
        _cmd, _use_shell, env = be._prepare_execution("echo hi")
        assert "ANTHROPIC_API_KEY" not in env
        assert env.get("PATH") == "/usr/bin"

    def test_strip_disabled_keeps_secrets(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        self._force_bwrap(monkeypatch)
        sb = LocalSandbox(working_dir=tmp_path, strip_secret_env=False)
        be = LocalShellBackend(root_dir=str(tmp_path), sandbox=sb, env={"ANTHROPIC_API_KEY": "sk", "PATH": "/x"})
        _cmd, _use_shell, env = be._prepare_execution("echo hi")
        assert env.get("ANTHROPIC_API_KEY") == "sk"
