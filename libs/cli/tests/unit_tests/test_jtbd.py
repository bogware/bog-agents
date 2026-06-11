"""Tests for the Jobs To Be Done workflow."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pytest

from bog_agents_cli.jtbd import (
    JobSpec,
    JTBDPending,
    consume_interview_answers,
    handle_jtbd_subcommand,
    parse_questions_response,
    parse_spec_response,
    render_execution_brief,
    render_spec_markdown,
    write_spec,
)


class _DummyApp:
    def __init__(self, cwd: Path | None = None) -> None:
        self.mounted: list[str] = []
        self._jtbd_pending: object | None = None
        self._jtbd_active_spec: object | None = None
        self._model_override: str | None = None
        self._profile_override = None
        if cwd is not None:
            self._cwd = str(cwd)

    async def _mount_message(self, message: object) -> None:
        text = (
            getattr(message, "_content", None)
            or getattr(message, "content", None)
            or str(message)
        )
        self.mounted.append(str(text))


def _spec(**overrides: object) -> JobSpec:
    base: dict = {
        "job_statement": "When my repo grows, I want CI to stay fast, so I can ship daily.",
        "functional_job": "keep CI under 10 minutes",
        "emotional_job": "stop dreading pushes",
        "social_job": "look like a team that ships",
        "desired_outcomes": [
            "CI completes in under 10 minutes",
            "no flaky reruns needed",
        ],
        "hiring_criteria": ["works without babysitting"],
        "constraints": ["no paid runners"],
        "non_goals": ["rewriting the test suite"],
        "original_prompt": "make ci faster",
    }
    base.update(overrides)
    return JobSpec(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


class TestParsing:
    def test_questions_clean(self) -> None:
        reply = json.dumps({"questions": ["Why now?", "What breaks?"]})
        assert parse_questions_response(reply) == ["Why now?", "What breaks?"]

    def test_questions_capped_at_four(self) -> None:
        reply = json.dumps({"questions": [f"q{i}" for i in range(9)]})
        questions = parse_questions_response(reply)
        assert questions is not None
        assert len(questions) == 4

    def test_questions_garbage(self) -> None:
        assert parse_questions_response("no") is None
        assert parse_questions_response(json.dumps({"questions": []})) is None

    def test_spec_clean(self) -> None:
        reply = json.dumps(
            {
                "job_statement": "When X, I want Y, so I can Z.",
                "functional_job": "f",
                "emotional_job": "e",
                "social_job": "",
                "desired_outcomes": ["o1", "o2"],
                "hiring_criteria": ["h"],
                "constraints": [],
                "non_goals": ["n"],
            }
        )
        spec = parse_spec_response(reply)
        assert spec is not None
        assert spec.job_statement.startswith("When X")
        assert spec.desired_outcomes == ["o1", "o2"]
        assert spec.social_job == ""
        assert spec.non_goals == ["n"]

    def test_spec_requires_statement_and_outcomes(self) -> None:
        assert (
            parse_spec_response(
                json.dumps({"job_statement": "x", "desired_outcomes": []})
            )
            is None
        )
        assert parse_spec_response(json.dumps({"desired_outcomes": ["o"]})) is None
        assert parse_spec_response("nope") is None


# ---------------------------------------------------------------------------
# Rendering + artifact
# ---------------------------------------------------------------------------


class TestRendering:
    def test_spec_markdown_sections(self) -> None:
        body = render_spec_markdown(_spec())
        assert body.startswith("# Job Spec")
        assert "When my repo grows" in body
        assert "## Dimensions of the job" in body
        assert "**Functional:**" in body
        assert "## Desired outcomes (the score sheet)" in body
        assert "## Non-goals" in body
        assert "make ci faster" in body

    def test_brief_is_outcome_driven(self) -> None:
        brief = render_execution_brief(_spec())
        assert "CI completes in under 10 minutes" in brief
        assert "Outcome Verification" in brief
        assert "Non-goals" in brief
        assert "make ci faster" in brief
        # The contract framing must be explicit.
        assert "they are the contract" in brief

    def test_write_spec_artifact(self, tmp_path: Path) -> None:
        spec = _spec()
        path = write_spec(spec, tmp_path)
        assert path.name == "job-spec.md"
        assert path.parent.parent == tmp_path / ".bog-agents" / "jtbd"
        assert "When my repo grows" in path.read_text(encoding="utf-8")
        assert spec.spec_path == path


# ---------------------------------------------------------------------------
# Interview answer consumption (model-free paths)
# ---------------------------------------------------------------------------


class TestConsumeAnswers:
    async def test_no_pending_returns_none(self) -> None:
        app = _DummyApp()
        assert await consume_interview_answers(app, "whatever") is None

    async def test_cancel_words_abort(self) -> None:
        app = _DummyApp()
        app._jtbd_pending = JTBDPending(prompt="make ci faster", questions=["why?"])
        result = await consume_interview_answers(app, "cancel")
        assert result is None
        assert app._jtbd_pending is None
        assert any("cancelled" in m.lower() for m in app.mounted)

    async def test_pending_always_cleared(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import bog_agents_cli.jtbd as jtbd_mod

        # Force the no-model path: synthesis unavailable → original prompt.
        monkeypatch.setattr(jtbd_mod, "_build_invoke", lambda _app, _t: None)
        app = _DummyApp()
        app._jtbd_pending = JTBDPending(prompt="make ci faster", questions=["why?"])
        result = await consume_interview_answers(app, "because deploys hurt")
        assert result == "make ci faster"
        assert app._jtbd_pending is None

    async def test_successful_synthesis_returns_brief(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import bog_agents_cli.jtbd as jtbd_mod

        reply = json.dumps(
            {
                "job_statement": "When deploys hurt, I want fast CI, so I can ship.",
                "functional_job": "speed up CI",
                "emotional_job": "",
                "social_job": "",
                "desired_outcomes": ["CI under 10 minutes"],
                "hiring_criteria": [],
                "constraints": [],
                "non_goals": [],
            }
        )

        def fake_build_invoke(_app: object, _timeout: float):
            async def _invoke(_system: str, user: str) -> str:
                assert "because deploys hurt" in user
                assert "make ci faster" in user
                return reply

            return _invoke

        monkeypatch.setattr(jtbd_mod, "_build_invoke", fake_build_invoke)
        app = _DummyApp(cwd=tmp_path)
        app._jtbd_pending = JTBDPending(prompt="make ci faster", questions=["why?"])
        brief = await consume_interview_answers(app, "because deploys hurt")
        assert brief is not None
        assert "CI under 10 minutes" in brief
        assert "Outcome Verification" in brief
        spec = app._jtbd_active_spec
        assert isinstance(spec, JobSpec)
        assert spec.original_prompt == "make ci faster"
        assert spec.spec_path is not None
        assert spec.spec_path.exists()
        assert any("Job Spec" in m for m in app.mounted)

    async def test_skip_keyword_marks_inference(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import bog_agents_cli.jtbd as jtbd_mod

        seen: dict[str, str] = {}

        def fake_build_invoke(_app: object, _timeout: float):
            async def _invoke(_system: str, user: str) -> str:
                seen["user"] = user
                return json.dumps(
                    {
                        "job_statement": "When …, I want …, so I can ….",
                        "desired_outcomes": ["done"],
                    }
                )

            return _invoke

        monkeypatch.setattr(jtbd_mod, "_build_invoke", fake_build_invoke)
        app = _DummyApp(cwd=tmp_path)
        app._jtbd_pending = JTBDPending(prompt="make ci faster", questions=["why?"])
        brief = await consume_interview_answers(app, "skip")
        assert brief is not None
        assert "skipped the interview" in seen["user"]

    async def test_failed_synthesis_falls_back_to_prompt(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import bog_agents_cli.jtbd as jtbd_mod

        def fake_build_invoke(_app: object, _timeout: float):
            async def _invoke(_system: str, _user: str) -> str:
                return "the model rambled instead of JSON"

            return _invoke

        monkeypatch.setattr(jtbd_mod, "_build_invoke", fake_build_invoke)
        app = _DummyApp(cwd=tmp_path)
        app._jtbd_pending = JTBDPending(prompt="make ci faster", questions=["why?"])
        result = await consume_interview_answers(app, "some answers")
        assert result == "make ci faster"
        assert any("failed" in m.lower() for m in app.mounted)


# ---------------------------------------------------------------------------
# Subcommands (model-free paths)
# ---------------------------------------------------------------------------


class TestSubcommands:
    async def test_status_idle(self) -> None:
        app = _DummyApp()
        await handle_jtbd_subcommand(app, "status")
        assert any("No JTBD activity" in m for m in app.mounted)

    async def test_status_pending(self) -> None:
        app = _DummyApp()
        app._jtbd_pending = JTBDPending(
            prompt="make ci faster", questions=["why?", "what?"]
        )
        await handle_jtbd_subcommand(app, "status")
        assert any("Interview in progress" in m for m in app.mounted)

    async def test_status_active_spec(self) -> None:
        app = _DummyApp()
        app._jtbd_active_spec = _spec()
        await handle_jtbd_subcommand(app, "status")
        assert any("Active Job Spec" in m for m in app.mounted)

    async def test_cancel(self) -> None:
        app = _DummyApp()
        app._jtbd_pending = JTBDPending(prompt="x", questions=["q"])
        await handle_jtbd_subcommand(app, "cancel")
        assert app._jtbd_pending is None
        assert any("cancelled" in m.lower() for m in app.mounted)

    async def test_verify_without_spec_errors(self) -> None:
        app = _DummyApp()
        await handle_jtbd_subcommand(app, "verify")
        assert any("No active Job Spec" in m for m in app.mounted)

    async def test_empty_arg_shows_usage(self, monkeypatch: pytest.MonkeyPatch) -> None:
        app = _DummyApp()
        await handle_jtbd_subcommand(app, "")
        assert any("Usage" in m for m in app.mounted)
