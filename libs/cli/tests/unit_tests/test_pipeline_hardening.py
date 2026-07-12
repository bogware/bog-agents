"""Hardening tests for bog_agents_cli.pipeline (encoding correctness)."""

from __future__ import annotations

from pathlib import Path

import pytest

from bog_agents_cli import pipeline as pipeline_mod
from bog_agents_cli.pipeline import (
    Pipeline,
    PipelineStep,
    load_pipeline,
    save_pipeline,
)


@pytest.fixture
def pipelines_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Redirect the module-level pipelines directory to a temp dir."""
    target = tmp_path / "pipelines"
    monkeypatch.setattr(pipeline_mod, "_PIPELINES_DIR", target)
    return target


def test_save_pipeline_writes_non_ascii_as_utf8(pipelines_dir: Path) -> None:
    """save_pipeline must persist non-ASCII content as UTF-8 (S9).

    Without an explicit ``encoding="utf-8"`` on the open call, this crashes on
    non-en-US Windows (cp1252/cp932/cp949) when the pipeline name or step text
    contains characters outside the locale codepage.
    """
    pipeline = Pipeline(
        name="naïve-café-日本語",
        description="résumé — 概要",
        steps=[
            PipelineStep(
                id="step-1",
                type="message",
                text="Greet the user: ¡Hola! — café ☕ 日本語テスト",
            )
        ],
    )

    dest = save_pipeline(pipeline)

    # File must be readable as UTF-8 and contain the raw non-ASCII text.
    raw = dest.read_text(encoding="utf-8")
    assert "café" in raw
    assert "日本語テスト" in raw

    # And it must round-trip back to the same content.
    reloaded = load_pipeline(dest)
    assert reloaded.name == "naïve-café-日本語"
    assert reloaded.description == "résumé — 概要"
    assert reloaded.steps[0].text == "Greet the user: ¡Hola! — café ☕ 日本語テスト"
