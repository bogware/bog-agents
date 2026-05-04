"""Tests for ``bog_agents._logging``."""

from __future__ import annotations

import io
import json
import logging

from bog_agents._logging import (
    _JsonFormatter,
    bind_run,
    bind_turn,
    configure,
    get_logger,
)


def _capture(level: int = logging.INFO) -> tuple[logging.Logger, io.StringIO]:
    buf = io.StringIO()
    logger = logging.getLogger("bog_agents.test_capture")
    logger.handlers.clear()
    logger.setLevel(level)
    handler = logging.StreamHandler(buf)
    handler.setFormatter(_JsonFormatter())
    from bog_agents._logging import _CorrelationFilter

    handler.addFilter(_CorrelationFilter())
    logger.addHandler(handler)
    logger.propagate = False
    return logger, buf


def test_json_formatter_includes_correlation_ids() -> None:
    logger, buf = _capture()
    with bind_run("run-abc"), bind_turn("turn-xyz"):
        logger.info("hello")
    payload = json.loads(buf.getvalue().strip())
    assert payload["run_id"] == "run-abc"
    assert payload["turn_id"] == "turn-xyz"
    assert payload["level"] == "INFO"
    assert payload["message"] == "hello"


def test_json_formatter_omits_empty_correlation_ids() -> None:
    logger, buf = _capture()
    logger.info("no-context")
    payload = json.loads(buf.getvalue().strip())
    assert "run_id" not in payload
    assert "turn_id" not in payload


def test_json_formatter_forwards_extra_dict() -> None:
    logger, buf = _capture()
    logger.info("with-extras", extra={"job_id": "j-1", "duration_ms": 42})
    payload = json.loads(buf.getvalue().strip())
    assert payload["job_id"] == "j-1"
    assert payload["duration_ms"] == 42


def test_get_logger_under_bog_agents_namespace() -> None:
    configure()
    assert get_logger("bog_agents.middleware.dlp").name == "bog_agents.middleware.dlp"
    # Non-bog-agents __name__ is reparented so the root handler still catches it.
    assert get_logger("third_party.foo").name == "bog_agents.ext.third_party.foo"


def test_bind_run_unsets_after_block() -> None:
    from bog_agents._logging import run_id_var

    assert run_id_var.get() == ""
    with bind_run("inside"):
        assert run_id_var.get() == "inside"
    assert run_id_var.get() == ""
