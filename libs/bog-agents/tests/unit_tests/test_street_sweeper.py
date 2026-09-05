"""Unit tests for `StreetSweeperMiddleware` — continuous context pruning.

These exercise the pure planning logic (no model, no network): Tier 0 lossless
cleanup, Tier 1 stale-read/dedup stubbing, Tier 2 truncation, the
message-count/order invariant, offload + recall round-trip, and sweep-log
accounting.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, ToolMessage

if TYPE_CHECKING:
    from pathlib import Path

from bog_agents.backends.filesystem import FilesystemBackend
from bog_agents.middleware.street_sweeper import (
    StreetSweeperMiddleware,
    SweepLog,
    _input_usd_per_token,
    _normalize_text,
    _truncate_head_tail,
)


def _read_call(call_id: str, path: str, *, name: str = "read_file") -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": {"file_path": path}, "id": call_id}])


def _tool_result(call_id: str, content: str, *, name: str = "read_file") -> ToolMessage:
    return ToolMessage(content=content, tool_call_id=call_id, name=name)


# --------------------------------------------------------------------------- Tier 0


def test_normalize_text_strips_ansi_and_trailing_whitespace() -> None:
    raw = "hello \x1b[31mred\x1b[0m   \nworld\t\n"
    out = _normalize_text(raw)
    assert "\x1b" not in out
    assert "red" in out
    assert out == "hello red\nworld\n"  # per-line trailing ws stripped; structure preserved


def test_normalize_text_collapses_blank_runs_and_identical_lines() -> None:
    raw = "a\n\n\n\n\nb\n" + ("frame\n" * 6)
    out = _normalize_text(raw)
    assert "\n\n\n" not in out  # blank runs squeezed
    assert "x6 identical lines" in out


def test_normalize_text_is_idempotent() -> None:
    raw = "x \x1b[1mY\x1b[0m\n\n\n\nz   \n" + ("dup\n" * 8)
    once = _normalize_text(raw)
    assert _normalize_text(once) == once  # stable -> cache-friendly


def test_truncate_head_tail_keeps_ends_and_elides_middle() -> None:
    text = "\n".join(str(i) for i in range(100))
    out = _truncate_head_tail(text, head_lines=3, tail_lines=2)
    assert out.startswith("0\n1\n2\n")
    assert out.endswith("98\n99")
    assert "lines swept" in out


# --------------------------------------------------------------------------- invariants


def test_plan_preserves_message_count_and_order_and_leaves_humans() -> None:
    mw = StreetSweeperMiddleware(keep_recent=0)
    messages: list[AnyMessage] = [
        HumanMessage(content="do the thing   \n\n\n\nplease"),
        _read_call("r1", "a.py"),
        _tool_result("r1", "line\x1b[0m\n\n\n\nmore"),
    ]
    plan = mw._plan(messages)
    assert len(plan.messages) == len(messages)
    assert [type(m) for m in plan.messages] == [type(m) for m in messages]
    # Human content is sacred even though it has blank runs.
    assert plan.messages[0].content == messages[0].content


# --------------------------------------------------------------------------- Tier 1


def test_stale_read_superseded_by_edit_is_stubbed() -> None:
    mw = StreetSweeperMiddleware(keep_recent=0)
    big = "\n".join(f"old content {i}" for i in range(50))
    messages: list[AnyMessage] = [
        _read_call("r1", "a.py"),
        _tool_result("r1", big),
        _read_call("e1", "a.py", name="edit_file"),
        _tool_result("e1", "edited ok", name="edit_file"),
    ]
    plan = mw._plan(messages)
    swept_read = plan.messages[1].content
    assert "stale" in swept_read.lower()
    assert "recall_swept" in swept_read
    assert len(swept_read) < len(big)
    assert any(a["technique"] == "stale_read" for a in plan.actions)
    # The original is queued for offload under the read's tool_call_id.
    assert any(marker == "r1" for marker, _, _ in plan.offloads)


def test_latest_read_is_kept_when_superseded_one_is_stubbed() -> None:
    mw = StreetSweeperMiddleware(keep_recent=0)
    messages: list[AnyMessage] = [
        _read_call("r1", "a.py"),
        _tool_result("r1", "VERSION ONE content here"),
        _read_call("r2", "a.py"),
        _tool_result("r2", "VERSION TWO content here"),
    ]
    plan = mw._plan(messages)
    assert "stale" in plan.messages[1].content.lower()  # first read stubbed
    assert plan.messages[3].content == "VERSION TWO content here"  # latest kept


def test_duplicate_tool_output_earlier_copy_is_stubbed() -> None:
    mw = StreetSweeperMiddleware(keep_recent=0)
    listing = "a.py\nb.py\nc.py"
    messages: list[AnyMessage] = [
        _read_call("l1", ".", name="ls"),
        _tool_result("l1", listing, name="ls"),
        _read_call("l2", ".", name="ls"),
        _tool_result("l2", listing, name="ls"),
    ]
    plan = mw._plan(messages)
    assert "duplicate" in plan.messages[1].content.lower()  # earlier copy stubbed
    assert plan.messages[3].content == listing  # latest kept
    assert any(a["technique"] == "dedup" for a in plan.actions)


# --------------------------------------------------------------------------- Tier 2


def test_aggressive_truncates_large_old_output() -> None:
    mw = StreetSweeperMiddleware(keep_recent=0, aggressive=True, truncate_min_lines=40, head_lines=4, tail_lines=4)
    big = "\n".join(f"row {i}" for i in range(80))
    messages: list[AnyMessage] = [_read_call("c1", "log.txt", name="execute"), _tool_result("c1", big, name="execute")]
    plan = mw._plan(messages)
    assert "lines swept" in plan.messages[1].content
    assert any(a["technique"] == "truncate" for a in plan.actions)


def test_conservative_mode_does_not_truncate() -> None:
    mw = StreetSweeperMiddleware(keep_recent=0, aggressive=False, truncate_min_lines=40)
    # No ANSI / trailing ws / repeats -> Tier 0 leaves it byte-identical.
    big = "\n".join(f"row {i}" for i in range(80))
    messages: list[AnyMessage] = [_read_call("c1", "log.txt", name="execute"), _tool_result("c1", big, name="execute")]
    plan = mw._plan(messages)
    assert plan.messages[1].content == big
    assert plan.actions == []


def test_recent_window_is_protected() -> None:
    mw = StreetSweeperMiddleware(keep_recent=2)
    messages: list[AnyMessage] = [
        _read_call("r1", "a.py"),
        _tool_result("r1", "garbage\x1b[0m   \n\n\n\nlots"),  # eligible (old)
        _read_call("r2", "b.py"),
        _tool_result("r2", "fresh\x1b[0m   \n\n\n\noutput"),  # within keep_recent=2 -> protected
    ]
    plan = mw._plan(messages)
    assert plan.messages[3].content == messages[3].content  # untouched


# --------------------------------------------------------------------------- offload + recall


def test_offload_and_recall_roundtrip(tmp_path: Path, monkeypatch: Any) -> None:
    backend = FilesystemBackend(root_dir=tmp_path)
    mw = StreetSweeperMiddleware(backend=backend, keep_recent=0)
    monkeypatch.setattr(mw, "_get_thread_id", lambda: "t1")

    offloads = [("r1", mw._offload_header("r1", "read_file", "stale_read"), "ORIGINAL READ BODY")]
    sections = mw._pending_offloads(offloads)
    assert [m for m, _ in sections] == ["r1"]
    assert "ORIGINAL READ BODY" in sections[0][1]
    mw._offload(backend, sections)
    assert "r1" in mw._offloaded

    # v6 SDK-4: one write-once file per marker under <prefix>/<thread>/.
    assert mw._marker_path("r1") == "/swept_context/t1/r1.md"
    stored = backend.download_files([mw._marker_path("r1")])[0].content.decode("utf-8")
    assert mw._extract_section(stored, "r1").strip() == "ORIGINAL READ BODY"
    # No marker -> listing of available markers from the thread directory.
    assert "r1" in mw._render_marker_index(backend.ls(mw._history_dir()))
    # Unknown marker -> not-found (a file without a section header).
    assert "No swept content" in mw._extract_section("", "nope")


def test_offload_writes_each_marker_once_and_never_rewrites(tmp_path: Path, monkeypatch: Any) -> None:
    """v6 SDK-4: a second turn writes only the new marker; earlier files are untouched."""
    backend = FilesystemBackend(root_dir=tmp_path)
    mw = StreetSweeperMiddleware(backend=backend, keep_recent=0)
    monkeypatch.setattr(mw, "_get_thread_id", lambda: "t1")
    writes: list[str] = []
    real_write = backend.write

    def _spy(path: str, content: str, *args: Any, **kwargs: Any) -> Any:
        writes.append(path)
        return real_write(path, content, *args, **kwargs)

    monkeypatch.setattr(backend, "write", _spy)

    mw._offload(backend, mw._pending_offloads([("r1", mw._offload_header("r1", "read_file", "stale_read"), "ONE")]))
    both = [
        ("r1", mw._offload_header("r1", "read_file", "stale_read"), "ONE"),
        ("r2", mw._offload_header("r2", "read_file", "duplicate"), "TWO"),
    ]
    mw._offload(backend, mw._pending_offloads(both))
    assert writes == ["/swept_context/t1/r1.md", "/swept_context/t1/r2.md"]
    listing = mw._render_marker_index(backend.ls(mw._history_dir()))
    assert "r1" in listing
    assert "r2" in listing
    # Nothing lands at the project root itself: only the routed prefix directory exists.
    assert sorted(p.name for p in tmp_path.iterdir()) == ["swept_context"]


def test_fallback_thread_id_is_stable_per_instance() -> None:
    """v6 SDK-2: outside a configured thread, the session id is minted once, not per call."""
    mw = StreetSweeperMiddleware(keep_recent=0)
    first = mw._get_thread_id()
    assert first.startswith("session_")
    assert mw._get_thread_id() == first
    assert mw._history_dir() == f"/swept_context/{first}"
    # A different instance gets its own id (no process-global leakage).
    assert StreetSweeperMiddleware(keep_recent=0)._get_thread_id() != first


def test_marker_and_thread_ids_are_path_safe(monkeypatch: Any) -> None:
    mw = StreetSweeperMiddleware(keep_recent=0)
    monkeypatch.setattr(mw, "_get_thread_id", lambda: "../evil")
    assert "/../" not in mw._history_dir()
    assert mw._history_dir().startswith("/swept_context/h_")
    assert mw._marker_path("call/with slash").startswith(mw._history_dir() + "/h_")
    assert mw._marker_path("toolu_01AbC-d.e") == mw._history_dir() + "/toolu_01AbC-d.e.md"


def test_pending_offloads_dedupes_by_marker() -> None:
    mw = StreetSweeperMiddleware(keep_recent=0)
    offloads = [("r1", "### swept r1 | read_file | stale_read | ts", "body")]
    first = mw._pending_offloads(offloads)
    assert [m for m, _ in first] == ["r1"]
    assert "body" in first[0][1]
    # v5 CTX-4: _pending_offloads no longer marks markers written — that only
    # happens after a successful _offload — so it stays pending until then.
    still_pending = mw._pending_offloads(offloads)
    assert [m for m, _ in still_pending] == ["r1"]
    # Once a write succeeds, the marker is recorded and no longer re-emitted.
    mw._offloaded.add("r1")
    assert mw._pending_offloads(offloads) == []


def test_failed_offload_leaves_marker_pending() -> None:
    # v5 CTX-4: a backend write failure must NOT mark the marker offloaded, so
    # the next model call retries instead of stranding an unrecoverable stub.
    class _FailingBackend:
        def write(self, _path: str, _content: str) -> None:
            raise OSError("disk full")

    class _ErrorResultBackend:
        def write(self, _path: str, _content: str) -> Any:
            @dataclass
            class _R:
                error: str | None = "Error writing file"

            return _R()

    mw = StreetSweeperMiddleware(keep_recent=0)
    sections = mw._pending_offloads([("r1", mw._offload_header("r1", "read_file", "stale_read"), "BODY")])
    mw._offload(_FailingBackend(), sections)  # type: ignore[arg-type]
    assert "r1" not in mw._offloaded  # still pending for retry
    # A backend that reports failure via `WriteResult.error` (the deepagents
    # contract) instead of raising must not be treated as a success either.
    mw._offload(_ErrorResultBackend(), sections)  # type: ignore[arg-type]
    assert "r1" not in mw._offloaded


# --------------------------------------------------------------------------- accounting + hooks


def test_sweep_log_accumulates_savings() -> None:
    mw = StreetSweeperMiddleware(keep_recent=0)
    big = "\n".join("noisy   \x1b[0m" for _ in range(20))
    messages: list[AnyMessage] = [_read_call("r1", "a.py", name="execute"), _tool_result("r1", big, name="execute")]
    plan = mw._plan(messages)
    mw._commit_actions(plan.actions)
    assert mw.sweep_log.actions_total >= 1
    assert mw.sweep_log.tokens_saved > 0
    assert mw.sweep_log.by_technique
    assert mw.sweep_log.calls_swept == 1  # one swept model call
    assert mw.sweep_log.counts_by_technique  # per-technique action counts populated


def test_pricing_resolves_from_cost_table() -> None:
    # Known model -> its input rate; provider prefix is stripped.
    assert _input_usd_per_token("anthropic:claude-sonnet-4-6") == 3.0 / 1_000_000
    # Unknown model -> conservative default; empty -> default.
    assert _input_usd_per_token("totally-unknown-model") == 5.0 / 1_000_000
    assert _input_usd_per_token(None) == 5.0 / 1_000_000


def test_dollars_and_reduction_metrics() -> None:
    log = SweepLog(usd_per_input_token=3.0 / 1_000_000)
    log.record({"technique": "truncate", "tool_name": "execute", "tokens_before": 1000, "tokens_after": 200})
    log.record_call()
    assert log.tokens_saved == 800
    assert log.reduction_pct == 80.0
    assert log.dollars_saved == 800 * (3.0 / 1_000_000)
    snapshot = log.to_dict()
    assert snapshot["tokens_saved"] == 800
    assert snapshot["dollars_saved"] == 800 * (3.0 / 1_000_000)
    assert snapshot["calls_swept"] == 1
    # Empty log is safe (no divide-by-zero).
    assert SweepLog().reduction_pct == 0.0


def test_set_pricing_updates_log_rate() -> None:
    mw = StreetSweeperMiddleware(keep_recent=0)
    mw.set_pricing("anthropic:claude-opus-4-6")
    assert mw.sweep_log.usd_per_input_token == 15.0 / 1_000_000


def test_on_commit_hook_fires_with_delta() -> None:
    deltas: list[dict[str, Any]] = []
    mw = StreetSweeperMiddleware(keep_recent=0, model_name="claude-sonnet-4-6", on_commit=deltas.append)
    big = "\n".join(f"old {i}" for i in range(50))
    messages: list[AnyMessage] = [
        _read_call("r1", "a.py"),
        _tool_result("r1", big),
        _read_call("e1", "a.py", name="edit_file"),
        _tool_result("e1", "edited", name="edit_file"),
    ]
    mw._commit_actions(mw._plan(messages).actions)
    assert len(deltas) == 1
    assert deltas[0]["tokens_saved"] > 0
    assert deltas[0]["dollars_saved"] > 0


@dataclass
class _FakeRequest:
    """Minimal stand-in for `ModelRequest` for hook tests."""

    messages: list[AnyMessage]
    state: dict[str, Any] = field(default_factory=dict)
    runtime: Any = None

    def override(self, *, messages: list[AnyMessage]) -> _FakeRequest:
        return _FakeRequest(messages=messages, state=self.state, runtime=self.runtime)


def test_wrap_model_call_sends_swept_view_to_handler() -> None:
    mw = StreetSweeperMiddleware(keep_recent=0, backend=None)
    big = "\n".join(f"old {i}" for i in range(50))
    messages: list[AnyMessage] = [
        _read_call("r1", "a.py"),
        _tool_result("r1", big),
        _read_call("e1", "a.py", name="edit_file"),
        _tool_result("e1", "edited", name="edit_file"),
    ]
    seen: dict[str, list[AnyMessage]] = {}

    def handler(req: _FakeRequest) -> str:
        seen["messages"] = req.messages
        return "RESPONSE"

    result = mw.wrap_model_call(_FakeRequest(messages=messages), handler)  # type: ignore[arg-type]
    assert result == "RESPONSE"
    assert len(seen["messages"]) == len(messages)  # count preserved
    assert "stale" in seen["messages"][1].content.lower()  # model sees the stub


def test_disabled_middleware_is_passthrough() -> None:
    mw = StreetSweeperMiddleware(enabled=False)
    messages: list[AnyMessage] = [_read_call("r1", "a.py"), _tool_result("r1", "x\x1b[0m   \n\n\n\ny")]
    seen: dict[str, list[AnyMessage]] = {}

    def handler(req: _FakeRequest) -> str:
        seen["messages"] = req.messages
        return "OK"

    mw.wrap_model_call(_FakeRequest(messages=messages), handler)  # type: ignore[arg-type]
    assert seen["messages"] is messages  # original list passed through untouched
    assert mw.sweep_log.actions_total == 0
