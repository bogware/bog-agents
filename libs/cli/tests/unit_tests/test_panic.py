"""Unit tests for bog_agents_cli._panic."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from bog_agents_cli._panic import (
    _redact,
    install_panic_handler,
    write_panic_dump,
)

if TYPE_CHECKING:
    import pytest


class TestRedact:
    def test_redacts_anthropic_style_token(self):
        assert "sk-abc" not in _redact("API_KEY=sk-abc1234567890def")

    def test_redacts_github_pat(self):
        text = "token: ghp_aaaaaaaaaaaaaaaaaaaaaaaaaa"
        assert "ghp_a" not in _redact(text)

    def test_redacts_aws_access_key(self):
        text = "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE"
        assert "AKIA" not in _redact(text)

    def test_redacts_jwt(self):
        text = "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
        assert "eyJh" not in _redact(text)

    def test_redacts_generic_secret_assignment(self):
        text = 'password = "hunter2hunter2"'
        out = _redact(text)
        assert "hunter2" not in out
        assert "***" in out

    def test_keeps_non_credential_text(self):
        text = "the build took 12.3 seconds and emitted 4 warnings"
        assert _redact(text) == text


class TestWritePanicDump:
    def test_creates_file_under_crash_dir(self, tmp_path: Path):
        try:
            msg = "boom"
            raise RuntimeError(msg)
        except RuntimeError as e:
            path = write_panic_dump(e, config_dir=tmp_path)
        assert path is not None
        assert path.exists()
        assert path.parent == tmp_path / "crash"
        assert path.suffix == ".log"

    def test_payload_contains_redacted_traceback(self, tmp_path: Path):
        try:
            secret = "sk-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
            msg = f"failure with token {secret}"
            raise RuntimeError(msg)
        except RuntimeError as e:
            path = write_panic_dump(e, config_dir=tmp_path)
        assert path is not None
        text = path.read_text(encoding="utf-8")
        assert "sk-aaaa" not in text
        assert "***" in text
        # Header line is informative.
        assert text.startswith("# bog-agents panic dump")

    def test_payload_includes_metrics_snapshot(self, tmp_path: Path):
        from bog_agents_cli import _observability

        _observability.reset_metrics()
        _observability.log_event("test.event")
        try:
            msg = "x"
            raise ValueError(msg)
        except ValueError as e:
            path = write_panic_dump(e, config_dir=tmp_path)
        assert path is not None
        text = path.read_text(encoding="utf-8")
        # JSON section should contain our event.
        assert "test.event" in text

    def test_extra_fields_included(self, tmp_path: Path):
        try:
            msg = "x"
            raise RuntimeError(msg)
        except RuntimeError as e:
            path = write_panic_dump(e, config_dir=tmp_path, extra={"thread": "abc-123"})
        assert path is not None
        text = path.read_text(encoding="utf-8")
        assert "abc-123" in text

    def test_returns_none_on_io_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # Force the dir creation to fail.
        bogus_dir = tmp_path / "nope"
        bogus_dir.write_text("i'm a file, not a dir")
        try:
            msg = "x"
            raise RuntimeError(msg)
        except RuntimeError as e:
            # Pointing config_dir at an existing FILE means crash_dir.mkdir
            # will fail and the function should return None gracefully.
            path = write_panic_dump(e, config_dir=bogus_dir)
        assert path is None

    def test_payload_structured_fields(self, tmp_path: Path):
        try:
            msg = "boom"
            raise RuntimeError(msg)
        except RuntimeError as e:
            path = write_panic_dump(e, config_dir=tmp_path)
        assert path is not None
        text = path.read_text(encoding="utf-8")
        # Extract JSON between the header and the traceback section.
        json_start = text.index("{")
        json_end = text.index("\n## Traceback")
        payload = json.loads(text[json_start:json_end].strip())
        assert payload["schema"] == "bog-agents-panic-dump-v1"
        assert payload["exception"]["type"] == "RuntimeError"
        assert payload["versions"]["bog-agents-cli"]
        assert payload["host"]["python"]


class TestInstallPanicHandler:
    def test_keyboard_interrupt_does_not_dump(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        import sys

        original_hook = sys.excepthook
        try:
            install_panic_handler(config_dir=tmp_path)
            # Simulate a KeyboardInterrupt; the hook should pass through
            # without writing a dump.
            try:
                raise KeyboardInterrupt
            except KeyboardInterrupt as e:
                sys.excepthook(type(e), e, e.__traceback__)
            crash_dir = tmp_path / "crash"
            assert not crash_dir.exists() or list(crash_dir.iterdir()) == []
        finally:
            sys.excepthook = original_hook

    def test_runtime_error_writes_dump(self, tmp_path: Path):
        import sys

        original_hook = sys.excepthook
        try:
            install_panic_handler(config_dir=tmp_path)
            try:
                msg = "boom"
                raise RuntimeError(msg)
            except RuntimeError as e:
                sys.excepthook(type(e), e, e.__traceback__)
            crash_dir = tmp_path / "crash"
            files = list(crash_dir.iterdir())
            assert len(files) == 1
        finally:
            sys.excepthook = original_hook
