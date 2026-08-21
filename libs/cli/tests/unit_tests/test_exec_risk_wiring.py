"""Regression tests: the exec-risk veto is wired into the live CLI approval path.

v5 T1-2 / SAFE-2: the deterministic exec-risk analyzer (`command_has_exec_risk`)
was dead code — never consulted by `AutoModeRuleEngine._eval_shell`, so commands
that look read-only but can run attacker code (`git -c core.pager=…`,
`sort --compress-program=…`, `tar --to-command=…`) were auto-approved instead of
falling through to a human. This test pins the wiring so it can't silently
regress.
"""

from __future__ import annotations

import pytest
from bog_agents.exec_risk import command_has_exec_risk

from bog_agents_cli.auto_mode import AutoDecision, AutoModeRuleEngine, AutoModeSettings

_EXEC_RISK_VECTORS = [
    "git -c core.fsmonitor=/tmp/evil.sh status",
    "git -c core.pager=/tmp/evil log",
    "sort --compress-program=/tmp/evil big.txt",
    "tar --to-command=/tmp/evil -xf a.tar",
]


class TestExecRiskVetoWired:
    def _engine(self) -> AutoModeRuleEngine:
        return AutoModeRuleEngine(AutoModeSettings())

    @pytest.mark.parametrize("cmd", _EXEC_RISK_VECTORS)
    def test_exec_risk_command_asks(self, cmd: str) -> None:
        # Sanity: the SDK analyzer flags it...
        assert command_has_exec_risk(cmd)
        # ...and the CLI approval engine must now ASK, sourced from exec_risk,
        # not silently ALLOW via the default fall-through.
        verdict = self._engine().evaluate("execute", {"command": cmd})
        assert verdict.decision is AutoDecision.ASK
        assert verdict.rule_source == "exec_risk"

    @pytest.mark.parametrize(
        "cmd",
        ["git status", "ls -la", "pytest -q", "cat README.md"],
    )
    def test_benign_commands_unaffected(self, cmd: str) -> None:
        assert not command_has_exec_risk(cmd)
        verdict = self._engine().evaluate("execute", {"command": cmd})
        assert verdict.decision is AutoDecision.ALLOW
