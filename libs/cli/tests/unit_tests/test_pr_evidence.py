"""`--pr --pr-evidence` appends a proof-of-work bundle to the PR body (v6 SDK-11)."""

from __future__ import annotations

from bog_agents_cli.pr_output import PRConfig, PRResult, build_pr_evidence_markdown


def test_evidence_markdown_lists_files_and_test_outcome() -> None:
    md = build_pr_evidence_markdown(
        "Fix issue #42: crash on start",
        files_changed=["src/app.py", "tests/test_app.py"],
        tests_passed=True,
        test_output="2 passed in 0.10s",
    )
    assert "Evidence bundle" in md
    assert "Fix issue #42" in md
    assert "src/app.py" in md and "tests/test_app.py" in md
    assert "Checks (1/1 passed)" in md  # the renderer keeps output only for failures


def test_evidence_markdown_without_tests_has_no_command_section() -> None:
    md = build_pr_evidence_markdown(
        "Rename module", files_changed=["a.py"], tests_passed=None
    )
    assert "a.py" in md
    assert "## Checks" not in md


def test_failed_tests_are_reported_as_failed() -> None:
    md = build_pr_evidence_markdown(
        "x", files_changed=["a.py"], tests_passed=False, test_output="1 failed"
    )
    assert "1 failed" in md


def test_pr_config_and_result_defaults() -> None:
    assert PRConfig().evidence is False
    assert PRResult(success=False).tests_passed is None
