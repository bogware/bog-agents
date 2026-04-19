"""Tests for _debug.configure_debug_logging."""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from unittest.mock import patch

import pytest

import bog_agents_cli._debug as _debug_mod
from bog_agents_cli._debug import configure_debug_logging


def _reset_shared_handler() -> None:
    """Reset the module-level shared handler so tests start clean."""
    if _debug_mod._shared_handler is not None:
        _debug_mod._shared_handler.close()
        _debug_mod._shared_handler = None
    _debug_mod._shared_handler_unavailable = False
    _debug_mod._active_log_path = None


class TestConfigureDebugLogging:
    def test_always_attaches_handler(self, tmp_path) -> None:
        """A rotating file handler is always attached (even without BOG_AGENTS_DEBUG)."""
        _reset_shared_handler()
        logger = logging.getLogger("test.debug.always_on")
        log_file = tmp_path / "test.log"
        with patch.dict(
            os.environ, {"BOG_AGENTS_DEBUG_FILE": str(log_file)}, clear=True
        ):
            configure_debug_logging(logger)
        rotating = [h for h in logger.handlers if isinstance(h, RotatingFileHandler)]
        assert len(rotating) == 1
        assert rotating[0].level == logging.WARNING
        # Cleanup
        _reset_shared_handler()
        for h in rotating:
            logger.removeHandler(h)

    def test_debug_env_lowers_level(self, tmp_path) -> None:
        """When BOG_AGENTS_DEBUG is set, handler level drops to DEBUG."""
        _reset_shared_handler()
        logger = logging.getLogger("test.debug.debug_level")
        log_file = tmp_path / "debug.log"
        with patch.dict(
            os.environ,
            {"BOG_AGENTS_DEBUG": "1", "BOG_AGENTS_DEBUG_FILE": str(log_file)},
        ):
            configure_debug_logging(logger)
        rotating = [h for h in logger.handlers if isinstance(h, RotatingFileHandler)]
        assert len(rotating) == 1
        assert rotating[0].level == logging.DEBUG
        assert logger.level == logging.DEBUG
        # Cleanup
        _reset_shared_handler()
        for h in rotating:
            logger.removeHandler(h)

    def test_custom_path_used(self, tmp_path) -> None:
        _reset_shared_handler()
        logger = logging.getLogger("test.debug.custom_path")
        log_file = tmp_path / "custom.log"
        with patch.dict(
            os.environ,
            {"BOG_AGENTS_DEBUG": "1", "BOG_AGENTS_DEBUG_FILE": str(log_file)},
        ):
            configure_debug_logging(logger)
        rotating = [h for h in logger.handlers if isinstance(h, RotatingFileHandler)]
        assert len(rotating) >= 1
        assert str(log_file) in rotating[-1].baseFilename
        # Cleanup
        _reset_shared_handler()
        for h in rotating:
            logger.removeHandler(h)

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="/dev/null is not a blocking device on Windows; mkdir C:\\dev\\null\\impossible may succeed under admin",
    )
    def test_bad_path_prints_warning_no_crash(self, capsys) -> None:
        """Invalid log path should print warning to stderr, not crash."""
        _reset_shared_handler()
        logger = logging.getLogger("test.debug.bad_path")
        original_count = len(logger.handlers)
        # Use /dev/null as a directory (can't mkdir inside a file)
        with patch.dict(
            os.environ,
            {"BOG_AGENTS_DEBUG_FILE": "/dev/null/impossible/debug.log"},
            clear=True,
        ):
            configure_debug_logging(logger)
        assert len(logger.handlers) == original_count
        captured = capsys.readouterr()
        assert "Warning" in captured.err
        _reset_shared_handler()

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="/dev/null is not a blocking device on Windows; mkdir C:\\dev\\null may succeed under admin",
    )
    def test_default_path_falls_back_without_warning(self, tmp_path, capsys) -> None:
        """Default logging should quietly fall back when home path is unavailable."""
        _reset_shared_handler()
        logger = logging.getLogger("test.debug.default_fallback")
        fallback_log = tmp_path / "bog-agents" / "logs" / "bog_agents.log"

        with (
            patch.object(
                _debug_mod, "_DEFAULT_LOG_FILE", Path("/dev/null/impossible.log")
            ),
            patch(
                "bog_agents_cli._debug.tempfile.gettempdir", return_value=str(tmp_path)
            ),
            patch.dict(os.environ, {}, clear=True),
        ):
            configure_debug_logging(logger)

        rotating = [h for h in logger.handlers if isinstance(h, RotatingFileHandler)]
        assert len(rotating) == 1
        assert rotating[0].baseFilename == str(fallback_log)
        assert capsys.readouterr().err == ""

        _reset_shared_handler()
        for h in rotating:
            logger.removeHandler(h)

    def test_idempotent(self, tmp_path) -> None:
        """Calling configure_debug_logging twice doesn't add duplicate handlers."""
        _reset_shared_handler()
        logger = logging.getLogger("test.debug.idempotent")
        log_file = tmp_path / "idem.log"
        with patch.dict(
            os.environ, {"BOG_AGENTS_DEBUG_FILE": str(log_file)}, clear=True
        ):
            configure_debug_logging(logger)
            configure_debug_logging(logger)
        rotating = [h for h in logger.handlers if isinstance(h, RotatingFileHandler)]
        assert len(rotating) == 1
        # Cleanup
        _reset_shared_handler()
        for h in rotating:
            logger.removeHandler(h)
