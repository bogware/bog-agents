"""Numeric tokens-per-turn baseline (ROADMAP #54).

The system-prompt snapshots in this directory catch *wording* drift; this test
catches *cost* drift. It measures the fixed cost of one turn (assembled system
prompt + every tool schema + injected messages) for the default harness and
the `lean` profile with the deterministic offline counter, and fails when
either grows more than `TOLERANCE` over `snapshots/harness_overhead.json`.
Refresh the baseline deliberately with `make update-snapshots` after an
intentional prompt or tool change.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bog_agents.backends import LocalShellBackend
from bog_agents.feature_config import FeatureConfig
from bog_agents.token_audit import audit_create_agent

TOLERANCE = 0.05
"""Allowed growth over the baseline before the test fails."""
COLLAPSE = 0.5
"""A measurement below this fraction of the baseline means a middleware or tool silently vanished."""
KEYS = ("per_turn_overhead", "system_prompt_tokens", "tool_schema_tokens")


def _measure() -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for label, config in (("default", None), ("lean", FeatureConfig(harness_profile="lean"))):
        kwargs = {"backend": LocalShellBackend(root_dir=Path.cwd(), virtual_mode=True)}
        if config is not None:
            kwargs["config"] = config
        audit = audit_create_agent(method="approx", **kwargs)
        data = audit.to_dict()
        out[label] = {key: int(data[key]) for key in KEYS}
    return out


def test_harness_overhead_baseline(snapshots_dir: Path, *, update_snapshots: bool) -> None:
    baseline_path = snapshots_dir / "harness_overhead.json"
    measured = _measure()
    if update_snapshots or not baseline_path.exists():
        baseline_path.write_text(json.dumps({"tokenizer": "approx", **measured}, indent=2) + "\n", encoding="utf-8")
        if update_snapshots:
            return
        pytest.fail(f"Created baseline at {baseline_path}. Re-run tests.")
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    for profile, numbers in measured.items():
        for key, value in numbers.items():
            pinned = int(baseline[profile][key])
            assert value <= pinned * (1 + TOLERANCE), (
                f"{profile}.{key} grew from {pinned} to {value} (>{TOLERANCE:.0%}); "
                "trim the prompt/tool descriptions or refresh the baseline with `make update-snapshots`"
            )
            assert value >= pinned * COLLAPSE, f"{profile}.{key} collapsed from {pinned} to {value}; did a middleware or tool disappear?"
    assert measured["lean"]["per_turn_overhead"] * 2 < measured["default"]["per_turn_overhead"]
