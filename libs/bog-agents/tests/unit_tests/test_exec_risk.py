"""Tests for the exec-risk analyzer + SafeTools auto-approval veto (Tier-1 #2)."""

from __future__ import annotations

import pytest

from bog_agents.exec_risk import analyze_exec_risk, command_has_exec_risk
from bog_agents.middleware.safe_tools import SafeToolRule, SafeToolsConfig, is_tool_safe


def _vectors(command: str) -> set[str]:
    return {r.vector for r in analyze_exec_risk(command)}


class TestGitConfigExec:
    @pytest.mark.parametrize(
        "cmd",
        [
            "git -c core.fsmonitor=/tmp/evil.sh status",
            "git -c core.sshCommand='sh -c evil' fetch",
            "git -c alias.x='!id' x",
            "git --config-env=core.pager=EVIL log",
            "git --upload-pack=/tmp/x clone ssh://h/r",
            "git --git-dir=/tmp/evil/.git log",
            "git -c diff.foo.command=/tmp/x diff",
            # Wrapper prefixes must be peeled or the vector hides behind them.
            "sudo git -c core.pager=/tmp/evil log",
            "command git -c core.fsmonitor=/tmp/evil.sh status",
        ],
    )
    def test_flagged(self, cmd: str) -> None:
        assert "git-config-exec" in _vectors(cmd)

    @pytest.mark.parametrize("cmd", ["git log", "git -c color.ui=false status", "git diff HEAD~1"])
    def test_benign_git_not_flagged(self, cmd: str) -> None:
        assert _vectors(cmd) == set()


class TestOtherVectors:
    def test_sort_compress_program(self) -> None:
        assert "sort-compress-program" in _vectors("sort --compress-program=/tmp/evil big.txt")

    def test_sort_compress_abbreviated(self) -> None:
        # `--co` is an unambiguous abbreviation of --compress-program for sort.
        assert "sort-compress-program" in _vectors("sort --co=/tmp/evil big.txt")

    def test_plain_sort_not_flagged(self) -> None:
        assert _vectors("sort -u names.txt") == set()

    def test_tar_to_command(self) -> None:
        assert "tar-command-exec" in _vectors("tar --to-command=/tmp/evil -xf a.tar")

    def test_rsync_rsh(self) -> None:
        assert "rsync-remote-shell" in _vectors("rsync -e '/tmp/evil' a b")

    def test_ssh_proxycommand(self) -> None:
        assert "ssh-proxy-command" in _vectors("ssh -o ProxyCommand='sh -c evil' host")


class TestWrapperPeelingAndSegments:
    def test_env_prefix_peeled(self) -> None:
        assert command_has_exec_risk("env FOO=bar git -c core.fsmonitor=x status")

    def test_timeout_wrapper_peeled(self) -> None:
        assert command_has_exec_risk("timeout 5 git -c core.pager=EVIL log")

    def test_nice_wrapper_with_args(self) -> None:
        assert command_has_exec_risk("nice -n 10 sort --compress-program=/tmp/x f")

    def test_risky_segment_in_a_chain(self) -> None:
        # A benign first segment must not hide a risky later one.
        assert command_has_exec_risk("ls && git -c core.fsmonitor=/tmp/x status")

    def test_fully_benign_chain(self) -> None:
        assert not command_has_exec_risk("ls -la && git status && grep foo *.py")


class TestSafeToolsVeto:
    def _cfg(self) -> SafeToolsConfig:
        return SafeToolsConfig(
            default_safe_tools=False,
            rules=[
                SafeToolRule(tool_pattern=r"git_.*", description="all git"),
                SafeToolRule(tool_name="execute", arg_constraints={"command": r"^git\s"}, description="git shell"),
            ],
        )

    def test_benign_git_command_auto_approved(self) -> None:
        assert is_tool_safe("execute", {"command": "git log --oneline"}, self._cfg()) is True

    def test_exec_risk_command_vetoed(self) -> None:
        # Matches the `^git ` auto-approve rule, but exec-risk forces HITL.
        assert is_tool_safe("execute", {"command": "git -c core.fsmonitor=/tmp/x log"}, self._cfg()) is False

    def test_non_command_tool_unaffected(self) -> None:
        assert is_tool_safe("git_status", {}, self._cfg()) is True
