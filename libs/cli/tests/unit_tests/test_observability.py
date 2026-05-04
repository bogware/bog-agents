"""Unit tests for bog_agents_cli._observability."""

from __future__ import annotations

import logging
import time

import pytest

from bog_agents_cli import _observability
from bog_agents_cli._observability import (
    EVT_PEAT_JOB_FIRE,
    get_metrics_snapshot,
    log_event,
    reset_metrics,
    timer,
)


@pytest.fixture(autouse=True)
def _isolate_registry():
    reset_metrics()
    yield
    reset_metrics()


class TestLogEvent:
    def test_emits_log_at_info_level(self, caplog: pytest.LogCaptureFixture):
        with caplog.at_level(logging.INFO, logger="bog_agents_cli.events"):
            log_event("test.basic", foo=1, bar="x")
        assert any("test.basic" in r.getMessage() for r in caplog.records)

    def test_bumps_counter_for_label(self):
        log_event("test.bump", label="alpha")
        log_event("test.bump", label="alpha")
        log_event("test.bump", label="beta")
        snap = get_metrics_snapshot()
        assert snap["counters"]["test.bump"] == {"alpha": 2, "beta": 1}

    def test_no_label_uses_empty_string_key(self):
        log_event("test.no_label")
        snap = get_metrics_snapshot()
        assert snap["counters"]["test.no_label"] == {"": 1}

    def test_extra_fields_use_evt_prefix(self, caplog: pytest.LogCaptureFixture):
        with caplog.at_level(logging.INFO, logger="bog_agents_cli.events"):
            log_event("test.extra", model="claude", tokens=42)
        rec = next(r for r in caplog.records if "test.extra" in r.getMessage())
        # Built-in collision protection: keys are prefixed with evt_.
        assert rec.evt_event == "test.extra"
        assert rec.evt_model == "claude"
        assert rec.evt_tokens == 42

    def test_first_seen_and_last_seen_tracked(self):
        before = time.time()
        log_event("test.times")
        after = time.time()
        snap = get_metrics_snapshot()
        first = snap["first_seen"]["test.times"]
        last = snap["last_seen"]["test.times"]
        assert before <= first <= after
        assert first == last  # one event = first == last

    def test_label_cardinality_capped(self):
        # Push more than the cap; subsequent labels should bucket under
        # *overflow*.
        for i in range(_observability._Registry._MAX_LABELS_PER_EVENT + 5):
            log_event("test.cardinality", label=f"k{i}")
        snap = get_metrics_snapshot()
        labels = snap["counters"]["test.cardinality"]
        assert "*overflow*" in labels
        assert labels["*overflow*"] == 5


class TestTimer:
    def test_emits_duration_on_exit(self, caplog: pytest.LogCaptureFixture):
        with caplog.at_level(logging.INFO, logger="bog_agents_cli.events"), timer("test.op"):
            time.sleep(0.01)
        rec = next(r for r in caplog.records if "test.op.end" in r.getMessage())
        assert rec.evt_status == "ok"
        assert rec.evt_duration_ms >= 0

    def test_marks_status_error_on_exception(self, caplog: pytest.LogCaptureFixture):
        with caplog.at_level(logging.INFO, logger="bog_agents_cli.events"):
            with pytest.raises(RuntimeError), timer("test.crash"):
                msg = "boom"
                raise RuntimeError(msg)
        rec = next(r for r in caplog.records if "test.crash.end" in r.getMessage())
        assert rec.evt_status == "error"
        assert rec.evt_error_type == "RuntimeError"

    def test_caller_can_set_status_explicitly(self, caplog: pytest.LogCaptureFixture):
        with caplog.at_level(logging.INFO, logger="bog_agents_cli.events"), timer("test.partial") as t:
            t.fields["status"] = "partial"
            t.fields["count"] = 3
        rec = next(r for r in caplog.records if "test.partial.end" in r.getMessage())
        assert rec.evt_status == "partial"
        assert rec.evt_count == 3


class TestEventConstantsAreStable:
    """The EVT_* constants are part of the stable observability contract."""

    def test_constants_are_dotted(self):
        assert "." in EVT_PEAT_JOB_FIRE

    def test_constants_are_lowercase(self):
        assert EVT_PEAT_JOB_FIRE.islower()
