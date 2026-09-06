"""ROADMAP #59 / #70: `/findings`, `/remediate`, the headless twin and the security-scan recipe."""

from __future__ import annotations

import json
from pathlib import Path

from bog_agents_cli import findings_controller as fc

REPORT = """Architecture: two services.

## Findings
- src/auth.py:42 [high] SQLI: user input reaches the query
- src/util.py:7 [low] DEAD: unused helper
- src/net.py:12 [critical] SSRF: fetch follows a user-supplied URL
"""


def _seed(root: Path) -> None:
    (root / "report.md").write_text(REPORT, encoding="utf-8")
    assert "3 new" in fc.run_findings_command(
        "/findings record report.md --source security-scan --run r1", root
    )


def test_list_show_triage_gate_and_sarif(tmp_path: Path) -> None:
    assert "No open findings" in fc.run_findings_command("/findings", tmp_path)
    _seed(tmp_path)
    table = fc.run_findings_command("/findings", tmp_path)
    assert (
        table.index("SSRF") < table.index("SQLI") < table.index("DEAD")
        and "3 open" in table
    )
    assert "DEAD" not in fc.run_findings_command("/findings --min high", tmp_path)
    fp = fc.list_findings(tmp_path)[0].fingerprint
    assert "rule_id: SSRF" in fc.run_findings_command(
        f"/findings show {fp[:19]}", tmp_path
    )
    assert "→ false_positive (behind the proxy)" in fc.run_findings_command(
        f"/findings triage {fp[:19]} false_positive behind the proxy", tmp_path
    )
    assert "Unknown state" in fc.run_findings_command(
        f"/findings triage {fp} nonsense", tmp_path
    )
    assert "No finding matches" in fc.run_findings_command(
        "/findings triage sha256:zzz fixed", tmp_path
    )
    assert "SSRF" not in fc.run_findings_command("/findings", tmp_path)
    assert "SSRF" in fc.run_findings_command("/findings --all", tmp_path)

    gate = fc.run_findings_command("/findings gate", tmp_path)
    assert "FAILED" in gate and "SQLI" in gate
    assert "passed" in fc.run_findings_command(
        "/findings gate --max critical", tmp_path
    )

    out = fc.run_findings_command("/findings sarif out/f.sarif", tmp_path)
    sarif = json.loads((tmp_path / "out" / "f.sarif").read_text(encoding="utf-8"))
    assert "SARIF written" in out and {
        r["ruleId"] for r in sarif["runs"][0]["results"]
    } == {"SQLI", "DEAD"}

    # A second recorded run for the same source closes what it no longer reports.
    (tmp_path / "r2.md").write_text(
        "## Findings\n- src/auth.py:50 [high] SQLI: user input reaches the query\n",
        encoding="utf-8",
    )
    assert "1 fixed" in fc.run_findings_command(
        "/findings record r2.md --source security-scan --run r2", tmp_path
    )
    assert "Cannot read" in fc.run_findings_command(
        "/findings record missing.md", tmp_path
    )
    assert "Usage" in fc.run_findings_command("/findings help", tmp_path)
    assert "Unknown verb" in fc.run_findings_command("/findings dance", tmp_path)


def test_remediation_prompt(tmp_path: Path) -> None:
    prompt, note = fc.remediation_prompt("/remediate", tmp_path)
    assert prompt is None and "Usage" in note
    _seed(tmp_path)
    sqli = next(f for f in fc.list_findings(tmp_path) if f.rule_id == "SQLI")
    prompt, note = fc.remediation_prompt(
        f"/remediate {sqli.fingerprint[:19]}", tmp_path
    )
    assert (
        prompt is not None
        and "src/auth.py:42" in prompt
        and "user input reaches the query" in prompt
    )
    assert "/pr --evidence" in note
    store = fc.open_store(tmp_path)
    try:
        assert store.get(sqli.fingerprint).state == "triaged"  # type: ignore[union-attr]
        store.triage(sqli.fingerprint, "fixed")
    finally:
        store.close()
    prompt, note = fc.remediation_prompt(f"/remediate {sqli.fingerprint}", tmp_path)
    assert prompt is None and "is fixed" in note
    assert fc.remediation_prompt("/remediate sha256:nope", tmp_path)[0] is None


def test_headless_findings_twin(tmp_path: Path, monkeypatch) -> None:
    from bog_agents_cli import headless_commands as hc

    monkeypatch.chdir(tmp_path)
    assert "findings" in hc.HEADLESS_COMMANDS
    result = hc.HEADLESS_COMMANDS["findings"][1]("gate")
    assert result.ok and result.data and result.data["passed"] is True
    _seed(tmp_path)
    result = hc.HEADLESS_COMMANDS["findings"][1]("gate --max high")
    assert not result.ok and result.data["blocking"] == 2 and "FAILED" in result.text
    listing = hc.HEADLESS_COMMANDS["findings"][1]("list --min critical")
    assert (
        listing.ok and listing.data and listing.data["findings"][0]["rule_id"] == "SSRF"
    )


def test_security_scan_recipe_ends_in_the_ledger(tmp_path: Path) -> None:
    from bog_agents_cli.pipeline import load_pipeline
    from bog_agents_cli.recipes import get_recipe

    recipe = get_recipe("security-scan")
    assert recipe is not None and "security" in recipe.tags
    (tmp_path / "security-scan.yaml").write_text(recipe.yaml, encoding="utf-8")
    pipeline = load_pipeline(tmp_path / "security-scan.yaml")
    ids = [step.id for step in pipeline.steps]
    assert ids == [
        "map",
        "threat-model",
        "hunt",
        "jury",
        "reproduce",
        "report",
        "record",
    ]
    assert pipeline.steps[-1].type == "slash" and pipeline.steps[-1].command.startswith(
        "/findings record"
    )
    assert (
        "## Findings" in pipeline.steps[-2].text
        and "<path>:<line>" in pipeline.steps[-2].text
    )
