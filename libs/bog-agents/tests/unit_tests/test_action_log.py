"""ROADMAP #74: the hash-chained action log."""

from __future__ import annotations

import contextlib
import json
from pathlib import Path

from langchain.agents.middleware.types import ModelResponse
from langchain.tools.tool_node import ToolCallRequest
from langchain_core.messages import AIMessage, ToolMessage

from bog_agents import action_log as al


def test_chain_links_and_verifies(tmp_path: Path) -> None:
    log = al.ActionLog(tmp_path / "run.jsonl", run_id="run-1", clock=lambda: 1_000.0)
    first = log.append("approval", tool="execute", decision="allow")
    second = log.append("model_call", model="m", input_tokens=10, output_tokens=2)
    assert first.prev == al.GENESIS and second.prev == first.hash
    assert first.hash.startswith("sha256:") and log.head == second.hash
    result = log.verify()
    assert result.ok and result.checked == 2 and result.head == second.hash
    assert "intact" in result.describe()

    # Reopening continues the chain.
    reopened = al.ActionLog(tmp_path / "run.jsonl")
    third = reopened.append("tool_call", tool="ls")
    assert third.seq == 3 and third.prev == second.hash
    assert al.verify_chain(tmp_path / "run.jsonl").checked == 3


def test_tampering_is_detected(tmp_path: Path) -> None:
    path = tmp_path / "run.jsonl"
    log = al.ActionLog(path)
    for i in range(4):
        log.append("tool_call", tool=f"t{i}")
    lines = path.read_text(encoding="utf-8").splitlines()

    edited = json.loads(lines[1])
    edited["data"]["tool"] = "rm -rf"
    path.write_text("\n".join([lines[0], json.dumps(edited), *lines[2:]]) + "\n", encoding="utf-8")
    result = al.verify_chain(path)
    assert not result.ok and result.broken_at == 2 and "does not recompute" in result.reason

    path.write_text("\n".join([lines[0], *lines[2:]]) + "\n", encoding="utf-8")  # a line removed
    result = al.verify_chain(path)
    assert not result.ok and result.broken_at == 3 and "prev hash" in result.reason

    path.write_text("\n".join([*lines[:2], "{garbage", *lines[2:]]) + "\n", encoding="utf-8")
    assert not al.verify_chain(path).ok


def test_export_signature_round_trip(tmp_path: Path) -> None:
    log = al.ActionLog(tmp_path / "run.jsonl", run_id="r")
    log.append("expert_verdict", action="deny", rule="no-prod")
    bundle = log.export(sign=lambda payload: "sig:" + str(len(payload)), signer_id="test-key")
    assert bundle["count"] == 1 and bundle["signer"] == "test-key" and bundle["signature"].startswith("sig:")

    def verify(payload: bytes, signature: str) -> bool:
        return signature == "sig:" + str(len(payload))

    assert al.verify_export(bundle, verify=verify).ok
    bundle["events"][0]["data"]["rule"] = "changed"
    assert not al.verify_export(bundle, verify=verify).ok
    fresh = log.export(sign=lambda payload: "sig:" + str(len(payload)))
    fresh["signature"] = "sig:0"
    assert "signature" in al.verify_export(fresh, verify=verify).reason


def test_retention_and_expert_sink(tmp_path: Path) -> None:
    old = tmp_path / "old.jsonl"
    old.write_text("{}\n", encoding="utf-8")
    import os

    os.utime(old, (1_000_000, 1_000_000))
    fresh = al.ActionLog(tmp_path / "fresh.jsonl")
    fresh.append("x")
    assert al.apply_retention(tmp_path, keep_days=30) == 1
    assert not old.exists() and (tmp_path / "fresh.jsonl").exists()

    sink = al.expert_sink(fresh)
    sink("deny", {"action": "deny", "rule": "r1", "tool": "execute"})
    events = list(fresh.events())
    assert events[-1].kind == "expert_verdict" and events[-1].data == {"action": "deny", "rule": "r1", "tool": "execute"}


def test_middleware_records_model_and_tool_calls(tmp_path: Path) -> None:
    log = al.ActionLog(tmp_path / "run.jsonl")
    mw = al.ActionLogMiddleware(log, price=lambda model, i, o: 0.001 * (i + o))

    class _Req:
        model = None

    reply = AIMessage(
        content="ok", usage_metadata={"input_tokens": 100, "output_tokens": 20, "total_tokens": 120}, response_metadata={"model_name": "claude-x"}
    )
    response = mw.wrap_model_call(_Req(), lambda _r: ModelResponse(result=[reply]))
    assert response.result[0] is reply

    request = ToolCallRequest(tool_call={"name": "execute", "args": {"command": "ls"}, "id": "c1"}, tool=None, state={}, runtime=None)  # type: ignore[arg-type]
    mw.wrap_tool_call(request, lambda _r: ToolMessage(content="a.py", tool_call_id="c1", name="execute"))

    def boom(_r: ToolCallRequest) -> ToolMessage:
        raise RuntimeError("nope")

    with contextlib.suppress(RuntimeError):
        mw.wrap_tool_call(request, boom)
    events = list(log.events())
    assert [e.kind for e in events] == ["model_call", "tool_call", "tool_call"]
    assert events[0].data == {"model": "claude-x", "input_tokens": 100, "output_tokens": 20, "cost_usd": 0.12}
    assert events[1].data["tool"] == "execute" and '"command": "ls"' in events[1].data["args"]
    assert events[2].data["status"] == "RuntimeError"
    assert log.verify().ok
