"""Hardening tests for dreamscape Laws + Imagination response inspection (P1).

These tests pin the fix for the inert governance/failure-trigger bug:

* The live langchain ``ModelResponse`` is a frozen ``@dataclass`` whose
  model output lives in ``.result`` (a list of ``AIMessage``), with no
  ``.content`` attribute. The previous helpers read ``.content`` and so
  *always* saw the empty string on the real model path. This made
  ``LawsMiddleware`` Hard-Law enforcement fail OPEN (no violation ever
  detected, even with ``reject_on_violation=True``) and made
  ``ImaginationMiddleware``'s failure trigger never arm.

The assertions below drive a *real* ``ModelResponse(result=[...])`` and
prove:

* ``_response_text`` reads the text out of ``.result`` (live path) and
  still falls back to a bare ``AIMessage`` (legacy/test path).
* A Hard-Law violation is detected and the offending response content is
  replaced with the refusal — and that the refusal is written into
  ``.result`` where langchain actually reads it.
* Clean output is passed through untouched.
* The shared helper is the *same* object in both modules.
* Imagination's failure detection now fires on a failure-shaped
  ``ModelResponse`` (records a tool failure), and a clean response does
  not.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# The live langchain ModelResponse dataclass.
from langchain.agents.middleware.types import ModelResponse
from langchain_core.messages import AIMessage, HumanMessage

from bog_agents_cli.dreamscape import imagination as imag_mod, laws as laws_mod
from bog_agents_cli.dreamscape.config import ImaginationConfig, LawsConfig
from bog_agents_cli.dreamscape.laws import (
    LawsMiddleware,
    _replace_response_content,
    _response_text,
)


def _model_response(text: str) -> ModelResponse:
    """Build a real ModelResponse the way the live model path produces it."""
    return ModelResponse(result=[AIMessage(content=text)])


# ---------------------------------------------------------------------------
# _response_text: read from .result, not the non-existent .content
# ---------------------------------------------------------------------------


class TestResponseTextReadsResult:
    def test_reads_text_from_model_response_result(self) -> None:
        """The live path: text lives in .result, not .content."""
        resp = _model_response("hello from the model")
        # Sanity: the live ModelResponse genuinely has no .content.
        assert not hasattr(resp, "content")
        assert _response_text(resp) == "hello from the model"

    def test_reads_list_content_blocks_in_result(self) -> None:
        """Anthropic-style list-of-blocks content is concatenated."""
        msg = AIMessage(
            content=[
                {"type": "text", "text": "alpha "},
                {"type": "text", "text": "beta"},
            ]
        )
        resp = ModelResponse(result=[msg])
        assert _response_text(resp) == "alpha beta"

    def test_concatenates_multiple_result_messages(self) -> None:
        resp = ModelResponse(
            result=[AIMessage(content="one "), AIMessage(content="two")]
        )
        assert _response_text(resp) == "one two"

    def test_falls_back_to_bare_aimessage(self) -> None:
        """Legacy/test path: a bare AIMessage passed as the response."""
        assert _response_text(AIMessage(content="bare message")) == "bare message"

    def test_empty_result_is_empty_string(self) -> None:
        assert _response_text(ModelResponse(result=[])) == ""

    def test_shared_helper_is_single_source_of_truth(self) -> None:
        """Imagination re-imports the exact same helper from laws."""
        assert imag_mod._response_text is laws_mod._response_text


# ---------------------------------------------------------------------------
# _replace_response_content: rebuild the frozen dataclass via replace()
# ---------------------------------------------------------------------------


class TestReplaceResponseContent:
    def test_writes_refusal_into_result(self) -> None:
        original = _model_response("the original offending text")
        replaced = _replace_response_content(original, "REFUSED")
        # The refusal must land in .result where langchain reads it.
        assert isinstance(replaced, ModelResponse)
        assert _response_text(replaced) == "REFUSED"
        assert isinstance(replaced.result[0], AIMessage)

    def test_preserves_structured_response(self) -> None:
        sentinel = {"k": "v"}
        original = ModelResponse(
            result=[AIMessage(content="x")], structured_response=sentinel
        )
        replaced = _replace_response_content(original, "REFUSED")
        assert replaced.structured_response is sentinel

    def test_does_not_mutate_original_frozen_response(self) -> None:
        original = _model_response("keep me")
        _replace_response_content(original, "REFUSED")
        # Frozen dataclass — the original is untouched.
        assert _response_text(original) == "keep me"


# ---------------------------------------------------------------------------
# LawsMiddleware end-to-end: Hard-Law enforcement no longer fails open
# ---------------------------------------------------------------------------


class TestLawsEnforcementNotInert:
    @pytest.fixture
    def _laws_root(self, tmp_path: Path) -> Path:
        """Write a project Laws file with a force-push prohibition."""
        laws_file = tmp_path / ".bog-agents" / "laws.md"
        laws_file.parent.mkdir(parents=True, exist_ok=True)
        laws_file.write_text(
            "# Hard Laws\n- Never force-push to a shared branch.\n",
            encoding="utf-8",
        )
        return tmp_path

    def _cfg(self, root: Path, *, reject: bool) -> LawsConfig:
        return LawsConfig(
            enabled=True,
            laws_path=str(root / ".bog-agents" / "laws.md"),
            constitution_path=str(root / ".bog-agents" / "constitution.md"),
            reject_on_violation=reject,
        )

    def test_violation_detected_and_response_replaced(self, _laws_root: Path) -> None:
        """ADVERSARIAL: model output that violates a Law is now refused.

        Before the fix this returned the offending text verbatim because
        ``_response_text`` read the always-empty ``.content``.
        """
        mw = LawsMiddleware(
            cfg=self._cfg(_laws_root, reject=True), project_root=_laws_root
        )

        offending = _model_response("Sure, I'll force push to the shared branch now.")

        def _call_next(_req: object) -> ModelResponse:
            return offending

        # Drive through the real wrap_model_call hook with a stub request.
        request = _StubModelRequest()
        result = mw.wrap_model_call(request, _call_next)  # type: ignore[arg-type]

        text = _response_text(result)
        assert "force push to the shared branch" not in text.lower()
        assert "violate one of the configured Laws" in text

    def test_no_reject_passes_offending_text_through(self, _laws_root: Path) -> None:
        """reject_on_violation=False logs but does NOT replace the response."""
        mw = LawsMiddleware(
            cfg=self._cfg(_laws_root, reject=False), project_root=_laws_root
        )
        offending = _model_response("I'll force push to the shared branch.")

        result = mw.wrap_model_call(
            _StubModelRequest(),  # type: ignore[arg-type]
            lambda _req: offending,
        )
        assert "force push" in _response_text(result).lower()

    def test_clean_output_passes_through(self, _laws_root: Path) -> None:
        mw = LawsMiddleware(
            cfg=self._cfg(_laws_root, reject=True), project_root=_laws_root
        )
        clean = _model_response("I opened a small pull request for review.")
        result = mw.wrap_model_call(
            _StubModelRequest(),  # type: ignore[arg-type]
            lambda _req: clean,
        )
        assert _response_text(result) == "I opened a small pull request for review."

    def test_disabled_is_passthrough(self, _laws_root: Path) -> None:
        cfg = self._cfg(_laws_root, reject=True)
        cfg.enabled = False
        mw = LawsMiddleware(cfg=cfg, project_root=_laws_root)
        offending = _model_response("force push to the shared branch")
        result = mw.wrap_model_call(
            _StubModelRequest(),  # type: ignore[arg-type]
            lambda _req: offending,
        )
        # No enforcement when disabled — offending text survives.
        assert "force push" in _response_text(result).lower()


# ---------------------------------------------------------------------------
# Imagination failure trigger: looks_like_failure now fires
# ---------------------------------------------------------------------------


class TestImaginationFailureTrigger:
    @pytest.fixture(autouse=True)
    def _home(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path, raising=False)

    def test_failure_shaped_result_records_failure(self) -> None:
        """A failure-shaped ModelResponse increments the failure counter.

        Before the fix the failure markers were searched against the
        always-empty ``.content`` so the counter never moved.
        """
        from bog_agents_cli.dreamscape import lifecycle as lc_mod

        mw = imag_mod.ImaginationMiddleware(
            agent_id="fail-trigger", cfg=ImaginationConfig(enabled=True)
        )
        failure = _model_response(
            "Traceback (most recent call last): RuntimeError: boom"
        )
        mw._record_outcome(failure)

        snap = lc_mod.load_snapshot("fail-trigger")
        assert snap.consecutive_tool_failures == 1

    def test_clean_result_does_not_record_failure(self) -> None:
        from bog_agents_cli.dreamscape import lifecycle as lc_mod

        mw = imag_mod.ImaginationMiddleware(
            agent_id="clean-trigger", cfg=ImaginationConfig(enabled=True)
        )
        clean = _model_response("Everything completed successfully.")
        mw._record_outcome(clean)

        snap = lc_mod.load_snapshot("clean-trigger")
        assert snap.consecutive_tool_failures == 0


class _StubModelRequest:
    """Minimal stand-in for ModelRequest used by the Laws hook tests.

    ``LawsMiddleware._inject_rules`` calls ``request.override(...)``; we
    return ``self`` so the (rules-injected) request flows on to
    ``call_next`` unchanged for the purposes of inspecting the response.
    """

    def __init__(self) -> None:
        self.system_message = None
        self.messages = [HumanMessage(content="please force push")]

    def override(self, **_kwargs: object) -> _StubModelRequest:
        return self
