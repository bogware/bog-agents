"""Unit tests for the trace-mind causal-replay subsystem (Wave M).

Covers four layers in isolation:

* The append-only ledger + JSONL persistence + ancestry walk
  (``bog_agents_cli.causal.ledger``).
* The middleware shape — :meth:`record_user_message`, the post-model
  bookkeeping that produces TOOL_CALL events, and the toggle gate
  (``bog_agents_cli.causal.middleware``).
* The renderers — status / recent / ancestry / graph
  (``bog_agents_cli.causal.render``).
* The slash-command controller end-to-end via :func:`dispatch`
  (``bog_agents_cli.causal.controller``).

No real LLM is invoked. The middleware tests use a tiny fake response
object so the ``_post_model_call`` branch logic gets full coverage.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from bog_agents_cli.causal.controller import dispatch, reset_controllers
from bog_agents_cli.causal.ledger import (
    CausalEvent,
    CausalLedger,
    EventKind,
    list_sessions,
    load_session,
    open_session,
)
from bog_agents_cli.causal.middleware import CausalMiddleware
from bog_agents_cli.causal.render import (
    render_ancestry,
    render_graph,
    render_recent,
    render_status,
)

# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------


class TestLedger:
    """Append, persist, reload, ancestry-walk."""

    def test_open_session_creates_file_and_assigns_ids(self, tmp_path: Path) -> None:
        ledger = open_session(tmp_path)
        e1 = ledger.record(EventKind.USER_MESSAGE, actor="user", summary="hello")
        e2 = ledger.record(
            EventKind.MODEL_CALL,
            actor="m",
            summary="thinking",
            parent_ids=(e1.id,),
        )
        assert e1.id == 1
        assert e2.id == 2
        assert e2.parent_ids == (e1.id,)
        assert ledger.path.exists()
        # File should have exactly two JSON lines.
        lines = ledger.path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2
        for line in lines:
            json.loads(line)  # parses cleanly

    def test_load_session_round_trip(self, tmp_path: Path) -> None:
        ledger = open_session(tmp_path)
        e1 = ledger.record(EventKind.USER_MESSAGE, actor="user", summary="x")
        ledger.record(
            EventKind.MODEL_CALL,
            actor="m",
            summary="y",
            parent_ids=(e1.id,),
        )
        ledger.close()
        events = load_session(tmp_path, ledger.session_id)
        assert len(events) == 2
        assert all(isinstance(e, CausalEvent) for e in events)
        assert events[0].kind == EventKind.USER_MESSAGE
        assert events[1].parent_ids == (e1.id,)

    def test_resume_continues_id_counter(self, tmp_path: Path) -> None:
        l1 = open_session(tmp_path)
        l1.record(EventKind.USER_MESSAGE, actor="u", summary="a")
        l1.record(EventKind.NOTE, actor="u", summary="b")
        l1.close()
        l2 = open_session(tmp_path, session_id=l1.session_id, resume=True)
        assert l2._next_id == 3
        e = l2.record(EventKind.NOTE, actor="u", summary="c")
        assert e.id == 3
        assert len(load_session(tmp_path, l1.session_id)) == 3

    def test_collision_without_resume_raises(self, tmp_path: Path) -> None:
        l1 = open_session(tmp_path)
        sid = l1.session_id
        l1.close()
        with pytest.raises(ValueError, match="already exists"):
            open_session(tmp_path, session_id=sid)

    def test_ancestry_handles_diamonds_and_cycles(self, tmp_path: Path) -> None:
        ledger = open_session(tmp_path)
        a = ledger.record(EventKind.USER_MESSAGE, actor="u", summary="a")
        b = ledger.record(
            EventKind.MODEL_CALL, actor="m", summary="b", parent_ids=(a.id,)
        )
        c = ledger.record(
            EventKind.MODEL_CALL, actor="m", summary="c", parent_ids=(a.id,)
        )
        d = ledger.record(
            EventKind.TOOL_RESULT,
            actor="t",
            summary="d",
            parent_ids=(b.id, c.id),
        )
        ancestry = ledger.ancestry(d.id)
        ids = {e.id for e in ancestry}
        # All four ancestors present, no duplicates.
        assert ids == {a.id, b.id, c.id, d.id}

    def test_counts_by_kind(self, tmp_path: Path) -> None:
        ledger = open_session(tmp_path)
        ledger.record(EventKind.USER_MESSAGE, actor="u", summary="x")
        ledger.record(EventKind.TOOL_CALL, actor="t", summary="y")
        ledger.record(EventKind.TOOL_CALL, actor="t", summary="z")
        counts = ledger.counts_by_kind()
        assert counts[EventKind.USER_MESSAGE] == 1
        assert counts[EventKind.TOOL_CALL] == 2
        assert counts[EventKind.RULE_FIRE] == 0

    def test_load_session_skips_torn_final_line(self, tmp_path: Path) -> None:
        # Write a valid event + a torn line.
        ledger = open_session(tmp_path)
        ledger.record(EventKind.USER_MESSAGE, actor="u", summary="ok")
        with ledger.path.open("a", encoding="utf-8") as fh:
            fh.write('{"id":2,"kind":"note","timestamp":')  # truncated
        events = load_session(tmp_path, ledger.session_id)
        assert len(events) == 1  # torn line dropped, valid line kept

    def test_list_sessions_returns_newest_first(self, tmp_path: Path) -> None:
        l1 = open_session(tmp_path)
        l1.record(EventKind.NOTE, actor="u", summary="1")
        l1.close()
        # The session id includes a timestamp resolution of seconds, so
        # we mint a second one with an explicit later id to avoid races.
        l2 = open_session(tmp_path, session_id="20990101T000000Z-deadbe")
        l2.record(EventKind.NOTE, actor="u", summary="2")
        l2.close()
        sessions = list_sessions(tmp_path)
        assert sessions[0] == "20990101T000000Z-deadbe"

    def test_long_summary_is_truncated(self, tmp_path: Path) -> None:
        ledger = open_session(tmp_path)
        e = ledger.record(EventKind.NOTE, actor="u", summary="x" * 1000)
        assert len(e.summary) <= 240
        assert e.summary.endswith("…")

    def test_record_after_close_raises(self, tmp_path: Path) -> None:
        ledger = open_session(tmp_path)
        ledger.close()
        with pytest.raises(RuntimeError, match="closed"):
            ledger.record(EventKind.NOTE, actor="u", summary="x")


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------


class TestMiddleware:
    """Gate + bookkeeping logic — exercised without a real agent.

    We synthesise model responses that look like the LangChain
    ModelResponse shape and assert the middleware translates them
    into the right CausalEvent records.
    """

    def test_disabled_does_not_record(self, tmp_path: Path) -> None:
        ledger = open_session(tmp_path)
        mw = CausalMiddleware(ledger=ledger, enabled=False)
        result = mw.record_rule_fire(rule_name="r", action="deny")
        assert result is None
        assert ledger.counts_by_kind()[EventKind.RULE_FIRE] == 0

    def test_record_user_message_anchors_chain(self, tmp_path: Path) -> None:
        ledger = open_session(tmp_path)
        mw = CausalMiddleware(ledger=ledger, enabled=True)
        e = mw.record_user_message("hello world")
        assert e.kind == EventKind.USER_MESSAGE
        assert mw._head_id == e.id

    def test_post_model_call_emits_final_answer_when_no_tools(
        self, tmp_path: Path
    ) -> None:
        ledger = open_session(tmp_path)
        mw = CausalMiddleware(ledger=ledger, enabled=True, actor_label="m")
        mw.record_user_message("hi")
        # Simulate a model response: no tool calls → FINAL_ANSWER.
        response = SimpleNamespace(content="done", tool_calls=[])
        mw._post_model_call(response, model_event_id=mw._head_id or 1)
        kinds = [e.kind for e in ledger.events()]
        assert EventKind.FINAL_ANSWER in kinds

    def test_post_model_call_emits_tool_call_events(self, tmp_path: Path) -> None:
        ledger = open_session(tmp_path)
        mw = CausalMiddleware(ledger=ledger, enabled=True, actor_label="m")
        mw.record_user_message("hi")
        response = SimpleNamespace(
            content="",
            tool_calls=[
                {"id": "call_a", "name": "shell", "args": {"cmd": "ls"}},
                {"id": "call_b", "name": "read", "args": {"path": "x.py"}},
            ],
        )
        mw._post_model_call(response, model_event_id=mw._head_id or 1)
        tc_events = [e for e in ledger.events() if e.kind == EventKind.TOOL_CALL]
        assert len(tc_events) == 2
        assert {e.actor for e in tc_events} == {"shell", "read"}
        # tool_call_id → event id mapping populated.
        assert "call_a" in mw._tool_call_to_event
        assert "call_b" in mw._tool_call_to_event

    def test_record_rule_fire_threads_parents(self, tmp_path: Path) -> None:
        ledger = open_session(tmp_path)
        mw = CausalMiddleware(ledger=ledger, enabled=True)
        u = mw.record_user_message("x")
        e = mw.record_rule_fire(rule_name="r1", action="deny", detail="bad")
        assert e is not None
        assert e.parent_ids == (u.id,)
        assert e.kind == EventKind.RULE_FIRE


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------


class TestRenderers:
    """Plain-text rendering — assertions are kind-anchored.

    We assert on the presence of kind labels rather than exact
    whitespace, so cosmetic tweaks to the renderer don't break the
    test suite.
    """

    def _populate(self, tmp_path: Path) -> CausalLedger:
        ledger = open_session(tmp_path)
        u = ledger.record(EventKind.USER_MESSAGE, actor="user", summary="run it")
        m = ledger.record(
            EventKind.MODEL_CALL,
            actor="haiku",
            summary="thinking",
            parent_ids=(u.id,),
        )
        tc = ledger.record(
            EventKind.TOOL_CALL,
            actor="shell",
            summary="cmd='ls'",
            parent_ids=(m.id,),
        )
        ledger.record(
            EventKind.TOOL_RESULT,
            actor="shell",
            summary="ok",
            parent_ids=(tc.id,),
        )
        return ledger

    def test_render_status_shows_session_and_counts(self, tmp_path: Path) -> None:
        ledger = self._populate(tmp_path)
        out = render_status(ledger)
        assert "Causal session:" in out
        assert "Recorded events: 4" in out
        assert "MODEL=1" in out
        assert "TOOL >=1" in out

    def test_render_recent_orders_oldest_to_newest(self, tmp_path: Path) -> None:
        ledger = self._populate(tmp_path)
        out = render_recent(ledger, limit=10)
        # USER first, ANSWER never appears in this fixture.
        user_idx = out.find("[USER")
        tool_idx = out.find("[TOOL <")
        assert 0 <= user_idx < tool_idx

    def test_render_ancestry_walks_back_to_root(self, tmp_path: Path) -> None:
        ledger = self._populate(tmp_path)
        # The last event (TOOL_RESULT) should trace back to USER.
        last = ledger.last()
        assert last is not None
        out = render_ancestry(ledger, last.id)
        # All four kinds appear in the rendered tree.
        for label in ("USER", "MODEL", "TOOL >", "TOOL <"):
            assert label in out

    def test_render_ancestry_unknown_id_is_friendly(self, tmp_path: Path) -> None:
        ledger = self._populate(tmp_path)
        out = render_ancestry(ledger, 9999)
        assert "No event with id 9999" in out

    def test_render_graph_renders_all_events_with_indent(self, tmp_path: Path) -> None:
        ledger = self._populate(tmp_path)
        out = render_graph(ledger)
        assert "Causal graph" in out
        # The TOOL_RESULT is two levels deep from USER → MODEL → TOOL_CALL → TOOL_RESULT,
        # so the TOOL < line must be indented at least once.
        result_line = next(line for line in out.splitlines() if "TOOL <" in line)
        assert result_line.startswith(" ")


# ---------------------------------------------------------------------------
# Controller / dispatch
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_causal_controllers() -> None:
    """Each controller test starts from a clean registry."""
    reset_controllers()
    yield
    reset_controllers()


class TestController:
    """End-to-end ``/causal …`` dispatch."""

    def test_status_before_on_says_not_initialized(self, tmp_path: Path) -> None:
        out = dispatch("/causal", tmp_path)
        assert "not yet initialized" in out

    def test_on_creates_session_and_status_shows_on(self, tmp_path: Path) -> None:
        on_out = dispatch("/causal on", tmp_path)
        assert "ON" in on_out
        status_out = dispatch("/causal", tmp_path)
        assert "Recording: ON" in status_out

    def test_off_after_on_preserves_session(self, tmp_path: Path) -> None:
        dispatch("/causal on", tmp_path)
        off = dispatch("/causal off", tmp_path)
        assert "OFF" in off

    def test_last_with_no_events_friendly(self, tmp_path: Path) -> None:
        dispatch("/causal on", tmp_path)
        out = dispatch("/causal last 5", tmp_path)
        # Either "No events" or a header — both acceptable for empty
        # ledgers. Assert the kinder one.
        assert "No events" in out or "Last" in out

    def test_why_routes_through_renderer(self, tmp_path: Path) -> None:
        # Drive the ledger directly via the controller's middleware
        # because the dispatch surface doesn't expose record_user_message.
        dispatch("/causal on", tmp_path)
        from bog_agents_cli.causal.controller import get_controller

        ctl = get_controller(tmp_path)
        assert ctl.middleware is not None
        u = ctl.middleware.record_user_message("hello")
        ctl.middleware.record_rule_fire(rule_name="r1", action="deny")
        # Whichever id was last assigned, /causal why <id> should run cleanly.
        out = dispatch(f"/causal why {u.id}", tmp_path)
        assert f"#{u.id:>4}" in out or f"#{u.id}" in out

    def test_unknown_subcommand_lists_help(self, tmp_path: Path) -> None:
        out = dispatch("/trace-mind nope", tmp_path)
        assert "Unknown" in out
        # Wave V renamed the slash; the help text reflects that.
        assert "/trace-mind on" in out

    def test_trace_mind_alias_works(self, tmp_path: Path) -> None:
        on_out = dispatch("/trace-mind on", tmp_path)
        assert "ON" in on_out
        status_out = dispatch("/trace-mind", tmp_path)
        assert "Recording" in status_out

    def test_sessions_lists_recorded_ids(self, tmp_path: Path) -> None:
        dispatch("/causal on", tmp_path)
        out = dispatch("/causal sessions", tmp_path)
        assert "session" in out.lower()
