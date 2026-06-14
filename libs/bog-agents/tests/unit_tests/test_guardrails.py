"""Tests for bog_agents.guardrails (ROADMAP #18)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from bog_agents.guardrails import (
    BlocklistGuardrail,
    GuardrailMiddleware,
    GuardrailResult,
    GuardrailTripwireError,
    LLMGuardrail,
    MaxLengthGuardrail,
    NoSecretsGuardrail,
    first_tripped,
    run_guardrails,
)


class TestBuiltinGuardrails:
    def test_blocklist_trips(self) -> None:
        g = BlocklistGuardrail(patterns=[r"rm\s+-rf", "password"])
        assert g.check("please rm -rf /").tripped
        assert not g.check("a calm sentence").tripped

    def test_max_length(self) -> None:
        g = MaxLengthGuardrail(max_chars=10)
        assert g.check("x" * 11).tripped
        assert not g.check("short").tripped

    def test_no_secrets_aws_and_openai(self) -> None:
        g = NoSecretsGuardrail()
        assert g.check("key=AKIA" + "A" * 16).tripped
        assert g.check("token sk-" + "a" * 40).tripped
        assert not g.check("no secrets here").tripped


class TestRunGuardrails:
    async def test_stop_on_first(self) -> None:
        guards = [MaxLengthGuardrail(5), BlocklistGuardrail(["zzz"])]
        results = await run_guardrails("toolongtext", guards, stop_on_first=True)
        assert len(results) == 1  # stopped at the first trip
        assert results[0].tripped

    async def test_collect_all(self) -> None:
        guards = [MaxLengthGuardrail(5), BlocklistGuardrail(["nope"])]
        results = await run_guardrails("toolong", guards, stop_on_first=False)
        assert len(results) == 2

    def test_first_tripped(self) -> None:
        results = [
            GuardrailResult("a", tripped=False),
            GuardrailResult("b", tripped=True, reason="x"),
        ]
        assert first_tripped(results).guardrail == "b"
        assert first_tripped([GuardrailResult("a", tripped=False)]) is None


class TestLLMGuardrail:
    async def test_violation(self) -> None:
        class _M:
            async def ainvoke(self, _messages: object) -> object:
                return SimpleNamespace(content='{"violation": true, "reason": "bad"}')

        g = LLMGuardrail(model=_M(), policy="no profanity")
        assert (await g.check("text")).tripped

    async def test_judge_failure_fails_open(self) -> None:
        class _M:
            async def ainvoke(self, _messages: object) -> object:
                raise RuntimeError("model down")

        g = LLMGuardrail(model=_M(), policy="x")
        result = await g.check("text")
        assert not result.tripped  # fail-open for judge outages


def _request(human_text: str) -> object:
    return SimpleNamespace(
        messages=[SimpleNamespace(type="human", content=human_text)]
    )


def _response(ai_text: str) -> object:
    return SimpleNamespace(result=[SimpleNamespace(content=ai_text)])


class TestGuardrailMiddleware:
    async def test_input_tripwire_raises_before_model(self) -> None:
        called = {"model": False}

        async def handler(_req: object) -> object:
            called["model"] = True
            return _response("ok")

        mw = GuardrailMiddleware(input_guardrails=[BlocklistGuardrail(["secret-plan"])])
        with pytest.raises(GuardrailTripwireError) as exc:
            await mw.awrap_model_call(_request("the secret-plan is X"), handler)
        assert exc.value.stage == "input"
        assert called["model"] is False  # failed fast, never called the model

    async def test_output_tripwire_raises(self) -> None:
        async def handler(_req: object) -> object:
            return _response("here is a key sk-" + "a" * 40)

        mw = GuardrailMiddleware(output_guardrails=[NoSecretsGuardrail()])
        with pytest.raises(GuardrailTripwireError) as exc:
            await mw.awrap_model_call(_request("hi"), handler)
        assert exc.value.stage == "output"

    async def test_clean_passes_through(self) -> None:
        async def handler(_req: object) -> object:
            return _response("a perfectly clean response")

        mw = GuardrailMiddleware(
            input_guardrails=[MaxLengthGuardrail(10000)],
            output_guardrails=[NoSecretsGuardrail()],
        )
        resp = await mw.awrap_model_call(_request("hello"), handler)
        assert resp.result[0].content == "a perfectly clean response"

    def test_sync_path_skips_async_guardrail(self) -> None:
        # Sync wrap can't await an async-only guardrail; it must skip, not crash.
        class _AsyncOnly:
            name = "async_only"

            async def check(self, _text: str) -> GuardrailResult:
                return GuardrailResult(self.name, tripped=True)

        def handler(_req: object) -> object:
            return _response("ok")

        mw = GuardrailMiddleware(output_guardrails=[_AsyncOnly()])
        resp = mw.wrap_model_call(_request("hi"), handler)  # no raise
        assert resp.result[0].content == "ok"
