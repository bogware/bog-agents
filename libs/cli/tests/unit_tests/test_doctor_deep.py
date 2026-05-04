"""Unit tests for bog_agents_cli.doctor_deep."""

from __future__ import annotations

from pathlib import Path

import pytest

from bog_agents_cli.doctor_deep import (
    Probe,
    _format_report,
    _probe_python,
    _safe_run,
    run_deep_doctor,
)


class TestProbeRunner:
    def test_safe_run_catches_unhandled_exception(self):
        def crashing() -> Probe:
            msg = "boom"
            raise RuntimeError(msg)

        probe = _safe_run(crashing)
        assert probe.status == "fail"
        assert "RuntimeError" in probe.detail
        assert probe.duration_ms >= 0

    def test_safe_run_records_duration_when_unset(self):
        def fast() -> Probe:
            return Probe(name="x", status="ok", detail="hi")

        probe = _safe_run(fast)
        assert probe.status == "ok"
        # Duration should be filled in by _safe_run if probe didn't set it.
        assert probe.duration_ms >= 0


class TestPythonProbe:
    def test_python_probe_always_ok(self):
        probe = _probe_python()
        assert probe.status == "ok"
        assert "." in probe.detail  # version dot


class TestFormatReport:
    def test_renders_summary(self):
        probes = [
            Probe(name="a", status="ok", detail="x"),
            Probe(name="b", status="warn", detail="y"),
            Probe(name="c", status="fail", detail="z"),
        ]
        out = _format_report(probes, total_ms=42)
        assert "doctor --deep" in out
        assert "ok=1" in out
        assert "warn=1" in out
        assert "fail=1" in out
        assert "42ms" in out

    def test_warns_about_failures(self):
        probes = [Probe(name="a", status="fail", detail="x")]
        out = _format_report(probes, total_ms=10)
        assert "Some critical checks failed" in out

    def test_no_warning_when_all_ok(self):
        probes = [Probe(name="a", status="ok", detail="x")]
        out = _format_report(probes, total_ms=10)
        assert "Some critical checks failed" not in out


class TestRunDeepDoctor:
    def test_runs_end_to_end(self):
        out = run_deep_doctor()
        assert "doctor --deep" in out
        # All probe names should appear.
        for name in ("python", "user-agents-dir", "git", "provider-envs"):
            assert name in out

    def test_output_starts_with_header(self):
        out = run_deep_doctor()
        assert out.splitlines()[0].startswith("bog-agents doctor --deep")
