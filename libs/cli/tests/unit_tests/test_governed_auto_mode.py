"""Governed Auto Mode (ROADMAP #47): batched review, ledger, breaker, wizard default."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

from bog_agents_cli import auto_mode
from bog_agents_cli.auto_mode import (
    ApprovalDecision,
    ApprovalLedger,
    AutoModeBreaker,
    AutoModeSettings,
    RiskAssessment,
    _batch_risk_prompt,
    _parse_batch_assessments,
    batch_risk_eval,
    load_auto_mode_settings,
)
from bog_agents_cli.textual_adapter import _evaluate_auto_mode_batch


def _reset() -> None:
    auto_mode.get_approval_ledger().clear()
    auto_mode.get_auto_mode_breaker(3).reset()
    auto_mode._JUDGE_CACHE.clear()


class TestBatchedReview:
    def test_prompt_lists_every_call_with_the_goal(self) -> None:
        prompt = _batch_risk_prompt(
            [
                (0, "execute", {"command": "pytest -q"}),
                (1, "write_file", {"path": "a.py"}),
            ],
            "fix the parser",
        )
        assert "User's stated goal: fix the parser" in prompt
        assert "[0] shell: pytest -q" in prompt and "[1]" in prompt and "a.py" in prompt

    def test_parse_grades_missing_and_unknown_as_critical(self) -> None:
        text = 'Here: {"assessments": [{"index": 0, "risk": "low", "reason": "tests"}, {"index": 1, "risk": "weird", "reason": "?"}]}'
        graded = _parse_batch_assessments(text, [0, 1, 2])
        assert [a.risk for a in graded] == ["low", "critical", "critical"]
        assert graded[0].risky is False and graded[2].risky is True

    async def test_one_judge_call_for_the_whole_batch(self) -> None:
        calls: list[str] = []

        async def judge(prompt: str) -> str:
            calls.append(prompt)
            return json.dumps(
                {
                    "assessments": [
                        {"index": 0, "risk": "low", "reason": "ok"},
                        {"index": 1, "risk": "high", "reason": "rm -rf"},
                    ]
                }
            )

        graded = await batch_risk_eval(
            [
                (0, "execute", {"command": "ls"}),
                (1, "execute", {"command": "rm -rf /"}),
            ],
            goal="clean",
            invoke=judge,
        )
        assert len(calls) == 1
        assert [a.risky for a in graded] == [False, True]

    async def test_judge_failure_fails_closed(self) -> None:
        async def judge(_prompt: str) -> str:
            raise RuntimeError("down")

        graded = await batch_risk_eval(
            [(0, "execute", {"command": "ls"})], goal="", invoke=judge
        )
        assert graded[0].risky and "unavailable" in graded[0].reason


class TestBreakerAndLedger:
    def test_breaker_trips_after_threshold_and_resets(self) -> None:
        b = AutoModeBreaker(threshold=3)
        assert [b.record(True), b.record(True)] == [False, False]
        assert b.record(True) is True and b.tripped
        assert b.record(True) is False  # already tripped
        b.reset()
        assert not b.tripped and b.record(False) is False and b.consecutive_risky == 0
        assert "armed" in b.status()

    def test_a_safe_verdict_resets_the_streak(self) -> None:
        b = AutoModeBreaker(threshold=2)
        b.record(True)
        b.record(False)
        assert b.record(True) is False

    def test_ledger_renders_newest_last(self) -> None:
        ledger = ApprovalLedger(maxlen=3)
        for i in range(5):
            ledger.record(
                ApprovalDecision(
                    "execute", f"shell: cmd{i}", "auto-approved", "allow_list", "ok"
                )
            )
        assert len(ledger) == 3
        text = ledger.render(2)
        assert "cmd3" in text and "cmd4" in text and "cmd2" not in text

    def test_settings_threshold_round_trip(self) -> None:
        s = AutoModeSettings().merge_dict({"breaker_threshold": 5})
        assert s.breaker_threshold == 5
        assert (
            AutoModeSettings().merge_dict({"breaker_threshold": 0}).breaker_threshold
            == 1
        )


class TestEvaluator:
    async def test_batch_asks_when_any_call_is_graded_risky_and_records_decisions(
        self, tmp_path: Path
    ) -> None:
        _reset()
        replies = json.dumps(
            {
                "assessments": [
                    {"index": 0, "risk": "low", "reason": "tests"},
                    {"index": 1, "risk": "high", "reason": "deletes"},
                ]
            }
        )
        judge = AsyncMock(return_value=replies)
        with patch(
            "bog_agents_cli.auto_mode.resolve_risk_judge",
            return_value=(judge, "ollama:llama3"),
        ):
            allowed = await _evaluate_auto_mode_batch(
                [
                    {"name": "execute", "args": {"command": "python -m pytest -q"}},
                    {
                        "name": "execute",
                        "args": {"command": "python cleanup.py --purge"},
                    },
                ],
                goal="run the tests",
                working_dir=tmp_path,
            )
        assert allowed is False
        assert judge.await_count == 1
        recent = auto_mode.get_approval_ledger().recent(5)
        assert [d.decision for d in recent] == ["auto-approved", "ask"]
        assert recent[1].risk == "high" and recent[1].judge == "ollama:llama3"

    async def test_rule_engine_decisions_skip_the_judge(self, tmp_path: Path) -> None:
        _reset()
        judge = AsyncMock()
        with patch(
            "bog_agents_cli.auto_mode.resolve_risk_judge", return_value=(judge, "x")
        ):
            allowed = await _evaluate_auto_mode_batch(
                [{"name": "execute", "args": {"command": "git status"}}],
                goal="",
                working_dir=tmp_path,
            )
        assert allowed is True
        judge.assert_not_awaited()
        assert auto_mode.get_approval_ledger().recent(1)[0].rule_source in {
            "allow_list",
            "git_ops",
        }

    async def test_breaker_pauses_auto_mode_and_notifies_once(
        self, tmp_path: Path
    ) -> None:
        _reset()
        risky = json.dumps(
            {"assessments": [{"index": 0, "risk": "critical", "reason": "bad"}]}
        )
        judge = AsyncMock(return_value=risky)
        notices: list[object] = []

        async def notify(widget: object) -> None:
            notices.append(widget)

        with patch(
            "bog_agents_cli.auto_mode.resolve_risk_judge", return_value=(judge, "x")
        ):
            for _ in range(3):
                assert (
                    await _evaluate_auto_mode_batch(
                        [{"name": "execute", "args": {"command": "python x.py"}}],
                        goal="",
                        notify=notify,
                        working_dir=tmp_path,
                    )
                    is False
                )
            assert auto_mode.get_auto_mode_breaker().tripped
            assert len(notices) == 1
            # tripped: the judge is not consulted any more and calls are marked paused
            assert (
                await _evaluate_auto_mode_batch(
                    [{"name": "execute", "args": {"command": "python y.py"}}],
                    goal="",
                    notify=notify,
                    working_dir=tmp_path,
                )
                is False
            )
        assert judge.await_count == 3
        assert auto_mode.get_approval_ledger().recent(1)[0].decision == "paused"
        assert len(notices) == 1

    async def test_no_judge_available_asks(self, tmp_path: Path) -> None:
        _reset()
        with patch(
            "bog_agents_cli.auto_mode.resolve_risk_judge",
            return_value=(None, "unavailable (no key)"),
        ):
            allowed = await _evaluate_auto_mode_batch(
                [{"name": "execute", "args": {"command": "python x.py"}}],
                goal="",
                working_dir=tmp_path,
            )
        assert allowed is False
        assert "no review model" in auto_mode.get_approval_ledger().recent(1)[0].reason


class TestWizardDefault:
    def test_save_user_section_round_trips_through_the_cascade(
        self, tmp_path: Path
    ) -> None:
        from bog_agents_cli._settings_cascade import save_user_section

        (tmp_path / ".bog-agents").mkdir()
        (tmp_path / ".bog-agents" / "settings.json").write_text(
            json.dumps({"auto_mode": {"breaker_threshold": 4}, "peat": {"x": 1}}),
            encoding="utf-8",
        )
        path = save_user_section("auto_mode", {"enabled": True}, user_home=tmp_path)
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["auto_mode"] == {"breaker_threshold": 4, "enabled": True}
        assert data["peat"] == {"x": 1}
        settings = load_auto_mode_settings(user_home=tmp_path)
        assert settings.enabled is True and settings.breaker_threshold == 4

    def test_normalize_permission_mode_honours_saved_default(self, monkeypatch) -> None:
        import argparse

        from bog_agents_cli import main as cli_main

        monkeypatch.setattr(cli_main, "_settings_default_auto_mode", lambda: True)
        args = argparse.Namespace(
            permission_mode=None, auto_mode=False, auto_approve=False, always_ask=False
        )
        cli_main._normalize_permission_mode(args)
        assert args.auto_mode is True

        explicit = argparse.Namespace(
            permission_mode=None, auto_mode=False, auto_approve=False, always_ask=True
        )
        cli_main._normalize_permission_mode(explicit)
        assert explicit.auto_mode is False


class TestStatusRendering:
    def test_status_names_judge_breaker_and_ledger(self, tmp_path: Path) -> None:
        _reset()
        with patch(
            "bog_agents_cli.auto_mode.resolve_risk_judge",
            return_value=(None, "ollama:llama3"),
        ):
            text = auto_mode.render_auto_mode_status(True, tmp_path)
        assert "auto mode is currently ON" in text
        assert "review model: ollama:llama3" in text
        assert "circuit breaker: armed" in text
        assert "decisions this session: 0" in text
