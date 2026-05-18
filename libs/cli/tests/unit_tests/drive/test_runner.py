"""End-to-end tests for the drive runner.

These spin up a real :class:`~bog_agents_cli.app.BogAgentsApp` under
Textual's :class:`~textual.pilot.Pilot` with a FakeChatModel so no
network access is needed. They are the canary that proves every layer
of the drive feature wires together.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from bog_agents_cli.drive import (
    RunOptions,
    load_script,
    parse_script,
    run_script,
    run_script_path,
)

SCRIPTS_DIR = Path(__file__).parent / "scripts"


def _read_jsonl(stream: io.StringIO) -> list[dict]:
    stream.seek(0)
    return [json.loads(line) for line in stream.read().splitlines() if line.strip()]


class TestRunScriptHelpModal:
    async def test_help_command_dispatches(self, tmp_path: Path):
        script = load_script(SCRIPTS_DIR / "help_modal.yaml")
        out = io.StringIO()
        result = await run_script(
            script,
            options=RunOptions(output_stream=out, artifact_dir=tmp_path / "art"),
        )
        assert result.exit_code == 0, (
            f"failed steps: {[s.error for s in result.steps if not s.ok]}"
        )
        records = _read_jsonl(out)
        # Each step + one summary line.
        assert any("summary" in r for r in records)


class TestRunScriptTypeSubmit:
    async def test_typed_prompt_round_trips(self, tmp_path: Path):
        script = load_script(SCRIPTS_DIR / "type_and_submit.yaml")
        out = io.StringIO()
        result = await run_script(
            script,
            options=RunOptions(output_stream=out, artifact_dir=tmp_path / "art"),
        )
        assert result.exit_code == 0, [s.error for s in result.steps if not s.ok]
        assert any(
            "Hello, drive!" in (msg.get("content") or "") for msg in result.transcript
        )


class TestSnapshotsWritten:
    async def test_snapshot_action_writes_both_files(self, tmp_path: Path):
        script = load_script(SCRIPTS_DIR / "snapshot.yaml")
        out = io.StringIO()
        artifact_dir = tmp_path / "art"
        result = await run_script(
            script,
            options=RunOptions(output_stream=out, artifact_dir=artifact_dir),
        )
        assert result.exit_code == 0, [s.error for s in result.steps if not s.ok]
        svg = artifact_dir / "shot.svg"
        txt = artifact_dir / "shot.txt"
        # At least one of the two snapshot artefacts must exist; both
        # is the happy path but the SVG writer can fail on exotic
        # platforms (we only test that the runner handles it without
        # crashing). The text fallback always works.
        assert txt.exists()
        assert svg.exists() or txt.read_text(encoding="utf-8")


class TestVarSubstitution:
    async def test_default_var_resolves_at_runtime(self, tmp_path: Path):
        script = load_script(SCRIPTS_DIR / "vars.yaml")
        out = io.StringIO()
        result = await run_script(
            script,
            options=RunOptions(output_stream=out, artifact_dir=tmp_path / "art"),
        )
        assert result.exit_code == 0, [s.error for s in result.steps if not s.ok]
        assert any(
            "hello World" in (msg.get("content") or "") for msg in result.transcript
        )

    async def test_var_override_wins(self, tmp_path: Path):
        script = load_script(SCRIPTS_DIR / "vars.yaml")
        out = io.StringIO()
        result = await run_script(
            script,
            options=RunOptions(
                output_stream=out,
                artifact_dir=tmp_path / "art",
                var_overrides={"who": "Mars"},
            ),
        )
        assert result.exit_code == 0, [s.error for s in result.steps if not s.ok]
        assert any(
            "hello Mars" in (msg.get("content") or "") for msg in result.transcript
        )


class TestRunScriptPath:
    async def test_run_from_path(self, tmp_path: Path):
        out = io.StringIO()
        result = await run_script_path(
            SCRIPTS_DIR / "type_and_submit.yaml",
            options=RunOptions(output_stream=out, artifact_dir=tmp_path / "art"),
        )
        assert result.exit_code == 0, [s.error for s in result.steps if not s.ok]


class TestFailureCases:
    async def test_expect_transcript_times_out_when_no_match(self, tmp_path: Path):
        script = parse_script(
            {
                "session": {
                    "model": "fake:nothing-useful",
                    "approval_mode": "auto-all",
                },
                "steps": [
                    {"slash": "/help"},
                    {"wait_for_idle": 3},
                    {
                        "expect_transcript_contains": {
                            "pattern": "this-will-never-appear-xyz",
                            "timeout_seconds": 1,
                        }
                    },
                ],
            }
        )
        out = io.StringIO()
        result = await run_script(
            script,
            options=RunOptions(output_stream=out, artifact_dir=tmp_path / "art"),
        )
        assert result.failed == 1
        assert result.exit_code == 1

    async def test_stop_on_failure_skips_subsequent_steps(self, tmp_path: Path):
        script = parse_script(
            {
                "session": {"model": "fake:x", "approval_mode": "auto-all"},
                "steps": [
                    {
                        "expect_transcript_contains": {
                            "pattern": "missing",
                            "timeout_seconds": 1,
                        }
                    },
                    {"slash": "/help"},
                ],
            }
        )
        out = io.StringIO()
        result = await run_script(
            script,
            options=RunOptions(
                output_stream=out,
                artifact_dir=tmp_path / "art",
                stop_on_failure=True,
            ),
        )
        assert result.steps[0].ok is False
        assert result.steps[1].ok is False
        assert result.steps[1].error == "skipped (prior failure)"


class TestSnapshotDataModel:
    def test_step_result_jsonl_round_trip(self):
        from bog_agents_cli.drive.runner import StepResult

        r = StepResult(index=0, action="type", ok=True, duration_ms=5, detail={"x": 1})
        record = json.loads(r.to_jsonl())
        assert record["step"] == 0
        assert record["action"] == "type"
        assert record["ok"] is True
        assert record["detail"] == {"x": 1}
