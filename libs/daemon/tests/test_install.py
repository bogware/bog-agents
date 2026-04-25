"""Unit tests for daemon install helpers."""

from __future__ import annotations

import stat
import sys
from pathlib import Path

import pytest

from bog_agents_daemon.install import generate_git_hook, install_git_hook


class TestGenerateGitHook:
    def test_basic_hook_is_valid_bash(self):
        script = generate_git_hook("http://localhost:7391")
        assert "#!/usr/bin/env bash" in script
        assert "curl" in script
        assert "git-push" in script

    def test_token_safely_embedded(self):
        # Token with special chars — should not break the script
        script = generate_git_hook("http://localhost:7391", token="tok'en$with\"special")
        # shlex.quote wraps in single quotes and escapes internal quotes
        assert "tok" in script
        # The raw token should NOT appear unquoted in the script
        assert "tok'en$with\"special" not in script

    def test_no_token_produces_empty_header_array(self):
        script = generate_git_hook("http://localhost:7391", token="")
        assert "TOKEN_HEADER=()" in script
        assert "X-Daemon-Token" not in script

    def test_with_token_produces_header_array(self):
        script = generate_git_hook("http://localhost:7391", token="mytoken")
        assert "X-Daemon-Token" in script
        assert "mytoken" in script

    def test_daemon_url_safely_embedded(self):
        script = generate_git_hook("http://example.com:7391")
        assert "http://example.com:7391" in script

    def test_url_with_special_chars_embedded(self):
        # URL with path — should be safe via shlex.quote
        script = generate_git_hook("http://host/daemon", token="")
        assert "http://host/daemon" in script


class TestInstallGitHook:
    def test_creates_hook_file(self, tmp_path: Path):
        git_hooks = tmp_path / ".git" / "hooks"
        git_hooks.mkdir(parents=True)

        install_git_hook(str(tmp_path), "http://localhost:7391")

        hook_path = git_hooks / "post-receive"
        assert hook_path.exists()
        content = hook_path.read_text()
        assert "#!/usr/bin/env bash" in content

    @pytest.mark.skipif(sys.platform == "win32", reason="chmod executable bits not supported on Windows")
    def test_hook_is_executable(self, tmp_path: Path):
        git_hooks = tmp_path / ".git" / "hooks"
        git_hooks.mkdir(parents=True)

        install_git_hook(str(tmp_path))

        hook_path = git_hooks / "post-receive"
        mode = hook_path.stat().st_mode
        assert bool(mode & stat.S_IXUSR), "Hook must be user-executable"

    def test_missing_git_dir_raises(self, tmp_path: Path):
        import pytest
        with pytest.raises(FileNotFoundError):
            install_git_hook(str(tmp_path))  # no .git/hooks directory
