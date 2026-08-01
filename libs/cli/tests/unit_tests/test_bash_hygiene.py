"""Tests for the bash-hygiene hang/block analyzer (Feature #9)."""

from __future__ import annotations

import pytest

from bog_agents_cli.bash_hygiene import (
    BashHygieneFinding,
    HygieneSeverity,
    analyze_bash_hygiene,
)


def _messages(command: str) -> list[str]:
    return [f.message for f in analyze_bash_hygiene(command)]


class TestCleanCommands:
    @pytest.mark.parametrize(
        "cmd",
        [
            "",
            "ls -la",
            "git status",
            "npm test",
            "python -m pytest",
            "cat README.md",
            "grep -rn TODO src/",
            "sleep 5",
            "sleep 10 && echo done",
        ],
    )
    def test_no_findings(self, cmd: str) -> None:
        assert analyze_bash_hygiene(cmd) == []


class TestSleep:
    def test_long_sleep_flagged(self) -> None:
        findings = analyze_bash_hygiene("sleep 3600")
        assert len(findings) == 1
        assert "sleep" in findings[0].message

    def test_threshold_exact_thirty(self) -> None:
        assert analyze_bash_hygiene("sleep 30") != []
        assert analyze_bash_hygiene("sleep 29") == []

    def test_timeout_wrapper_suppresses(self) -> None:
        assert analyze_bash_hygiene("timeout 30 sleep 3600") == []
        assert analyze_bash_hygiene("gtimeout 30 sleep 3600") == []


class TestInfiniteLoops:
    @pytest.mark.parametrize(
        "cmd",
        [
            "while true; do echo hi; done",
            "while :; do :; done",
            "while 1; do x; done",
            "until false; do x; done",
            "for ((;;)); do x; done",
        ],
    )
    def test_infinite_loops_flagged(self, cmd: str) -> None:
        assert _messages(cmd) != []

    def test_bounded_loop_not_flagged(self) -> None:
        assert analyze_bash_hygiene("for i in 1 2 3; do echo $i; done") == []
        assert analyze_bash_hygiene("timeout 5 while true; do :; done") == []


class TestYes:
    def test_unbounded_yes_flagged(self) -> None:
        assert _messages("yes") != []
        assert _messages("yes | grep foo") != []

    def test_piped_to_head_is_fine(self) -> None:
        assert analyze_bash_hygiene("yes | head -5") == []
        assert analyze_bash_hygiene("yes | head") == []


class TestTailFollow:
    def test_tail_f_flagged(self) -> None:
        assert _messages("tail -f app.log") != []
        assert _messages("tail -f app.log | grep ERROR") != []

    def test_plain_tail_fine(self) -> None:
        assert analyze_bash_hygiene("tail -20 app.log") == []
        assert analyze_bash_hygiene("tail -n 10 app.log") == []

    def test_timeout_suppresses(self) -> None:
        assert analyze_bash_hygiene("timeout 10 tail -f app.log") == []


class TestPing:
    def test_unbounded_ping_flagged(self) -> None:
        assert _messages("ping 8.8.8.8") != []
        assert _messages("ping -t 8.8.8.8") != []

    def test_bounded_ping_fine(self) -> None:
        assert analyze_bash_hygiene("ping -c 4 8.8.8.8") == []
        assert analyze_bash_hygiene("ping -n 3 8.8.8.8") == []


class TestMonitoring:
    @pytest.mark.parametrize("cmd", ["watch -n 1 df -h", "top", "htop"])
    def test_monitors_flagged(self, cmd: str) -> None:
        assert _messages(cmd) != []


class TestInteractive:
    @pytest.mark.parametrize("cmd", ["less big.log", "more data.txt", "vim file.py"])
    def test_interactive_high_severity(self, cmd: str) -> None:
        findings = analyze_bash_hygiene(cmd)
        assert findings
        assert all(f.severity is HygieneSeverity.HIGH for f in findings)

    def test_man_flagged(self) -> None:
        assert _messages("man ls") != []


class TestRead:
    def test_unbounded_read_flagged(self) -> None:
        assert _messages("read answer") != []
        assert _messages("read -p 'Continue? ' var") != []

    def test_timed_read_fine(self) -> None:
        assert analyze_bash_hygiene("read -t 5 answer") == []


class TestNetwork:
    def test_curl_without_timeout_flagged(self) -> None:
        assert _messages("curl https://api.example.com") != []

    def test_curl_with_timeout_fine(self) -> None:
        assert analyze_bash_hygiene("curl --max-time 10 https://x") == []
        assert analyze_bash_hygiene("curl -m 10 https://x") == []

    def test_wget_without_timeout_flagged(self) -> None:
        assert _messages("wget https://example.com/file") != []

    def test_wget_with_timeout_fine(self) -> None:
        assert analyze_bash_hygiene("wget --timeout=10 https://x") == []

    def test_ssh_without_connect_timeout_flagged(self) -> None:
        assert _messages("ssh deploy@prod") != []

    def test_ssh_with_connect_timeout_fine(self) -> None:
        assert analyze_bash_hygiene("ssh -o ConnectTimeout=5 deploy@prod") == []


class TestGitEditor:
    def test_editor_opening_git_flagged(self) -> None:
        assert _messages("git commit") != []
        assert _messages("git merge feature/x") != []
        assert _messages("git revert abc123") != []

    def test_message_flags_suppress(self) -> None:
        assert analyze_bash_hygiene("git commit -m 'done'") == []
        assert analyze_bash_hygiene("git commit --message 'done'") == []
        assert analyze_bash_hygiene("git merge --no-edit feature/x") == []
        assert analyze_bash_hygiene("git commit -am 'done'") == []
        assert analyze_bash_hygiene("git commit --amend -m 'x'") == []


def test_finding_is_frozen_dataclass() -> None:
    f = BashHygieneFinding(HygieneSeverity.WARN, "test")
    assert f.severity is HygieneSeverity.WARN
    assert f.message == "test"
    assert f.bounded_by_timeout is True
