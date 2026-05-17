"""Tests for the compliance auditor (Wave R).

Six layers of coverage:

1. **Pack loader** — strict YAML parsing, schema versioning, per-kind
   validation, duplicate-id detection.
2. **Evidence collectors** — event_count bounds, no_event_with_actor,
   at_least_one_session, bad-parameter handling.
3. **Runner** — dispatches to invariant prover, evidence collectors,
   rule_presence / rule_absence; aggregates verdicts correctly.
4. **Report renderer + seal** — markdown structure, JSON sidecar,
   HMAC sign/verify round-trip, tamper detection.
5. **Controller dispatch** — /audit run/list/show/packs/help; saves
   files under .bog-agents/audits/.
6. **Cron entrypoint** — daemon contract: resolves bundled pack,
   raises CronAuditFailed on non-PASS.
"""

from __future__ import annotations

import secrets
import time
from pathlib import Path

import pytest
import yaml
from bog_agents.middleware.expert_engine import (
    Action,
    ActionKind,
    Pattern,
    Predicate,
    PredicateOp,
    Rule,
)

from bog_agents_cli.causal.ledger import EventKind, open_session
from bog_agents_cli.compliance import (
    AuditPack,
    Check,
    CheckKind,
    PackParseError,
    Verdict,
    load_pack_from_dict,
    load_pack_from_yaml,
    render_markdown,
    report as report_mod,
    run_audit,
    seal_report,
    verify_seal,
)
from bog_agents_cli.compliance.audit_pack import (
    AuditWindow,
    EvidenceKind,
    EvidenceSpec,
)
from bog_agents_cli.compliance.controller import dispatch as audit_dispatch
from bog_agents_cli.compliance.cron import (
    CronAuditFailed,
    run as cron_run,
)
from bog_agents_cli.compliance.evidence import (
    COLLECTORS,
    TraceSlice,
    collect_at_least_one_session,
    collect_event_count,
    collect_no_event_with_actor,
    load_trace_slice,
    window_for_lookback,
)
from bog_agents_cli.compliance.report import (
    AuditReport,
    CheckResult,
    Evidence,
    report_to_json,
)
from bog_agents_cli.policy_prove.invariant import (
    Invariant,
    PatternSpec,
    PredicateSpec,
)

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def deterministic_key(monkeypatch: pytest.MonkeyPatch) -> bytes:
    """Pin the HMAC key so seals are byte-stable across tests."""
    key = bytes(range(32))
    monkeypatch.setattr(report_mod, "_load_or_create_key", lambda: key)
    return key


def _make_invariant_pack_dict(name: str = "test-pack") -> dict:
    return {
        "version": 1,
        "name": name,
        "description": "tests",
        "window": {"lookback_hours": 1.0},
        "checks": [
            {
                "id": "INV-1",
                "title": "leak rule guards leaks",
                "kind": "invariant",
                "control": "TEST.X.1",
                "invariant": {
                    "name": "no_leak",
                    "precondition": {
                        "fact_type": "tool_call",
                        "predicates": [
                            {"field": "name", "op": "eq", "value": "shell_execute"}
                        ],
                    },
                    "forbidden": {
                        "fact_type": "tool_call",
                        "predicates": [
                            {"field": "name", "op": "eq", "value": "leak"}
                        ],
                    },
                },
            }
        ],
    }


def _guard_rule(name: str = "block_leak") -> Rule:
    return Rule(
        name=name,
        when=(
            Pattern(
                fact_type="tool_call",
                predicates=(
                    Predicate(field="name", op=PredicateOp.EQ, value="leak"),
                ),
            ),
        ),
        then=(Action(kind=ActionKind.DENY, params={"reason": "block"}),),
    )


# ---------------------------------------------------------------------------
# 1. Pack loader
# ---------------------------------------------------------------------------


class TestPackLoader:
    def test_load_minimal_pack(self):
        pack = load_pack_from_dict(_make_invariant_pack_dict())
        assert pack.name == "test-pack"
        assert pack.version == 1
        assert pack.window.lookback_hours == 1.0
        assert len(pack.checks) == 1
        assert pack.checks[0].kind == CheckKind.INVARIANT
        assert pack.checks[0].invariant is not None

    def test_missing_version_raises(self):
        d = _make_invariant_pack_dict()
        del d["version"]
        with pytest.raises(PackParseError, match="version"):
            load_pack_from_dict(d)

    def test_non_integer_version_raises(self):
        d = _make_invariant_pack_dict()
        d["version"] = "one"
        with pytest.raises(PackParseError, match="version"):
            load_pack_from_dict(d)

    def test_unsupported_version_raises(self):
        d = _make_invariant_pack_dict()
        d["version"] = 99
        with pytest.raises(PackParseError, match="not supported"):
            load_pack_from_dict(d)

    def test_missing_name_raises(self):
        d = _make_invariant_pack_dict()
        del d["name"]
        with pytest.raises(PackParseError, match="'name'"):
            load_pack_from_dict(d)

    def test_no_checks_raises(self):
        d = _make_invariant_pack_dict()
        d["checks"] = []
        with pytest.raises(PackParseError, match="no checks"):
            load_pack_from_dict(d)

    def test_negative_lookback_raises(self):
        d = _make_invariant_pack_dict()
        d["window"] = {"lookback_hours": -1}
        with pytest.raises(PackParseError, match="positive"):
            load_pack_from_dict(d)

    def test_duplicate_check_ids_raise(self):
        d = _make_invariant_pack_dict()
        d["checks"].append(dict(d["checks"][0]))
        with pytest.raises(PackParseError, match="duplicate check id"):
            load_pack_from_dict(d)

    def test_invariant_without_block_raises(self):
        d = _make_invariant_pack_dict()
        del d["checks"][0]["invariant"]
        with pytest.raises(PackParseError, match="requires an 'invariant' block"):
            load_pack_from_dict(d)

    def test_trace_assertion_without_evidence_raises(self):
        d = _make_invariant_pack_dict()
        d["checks"][0] = {
            "id": "TA",
            "title": "ta",
            "kind": "trace_assertion",
        }
        with pytest.raises(PackParseError, match="requires an 'evidence' block"):
            load_pack_from_dict(d)

    def test_rule_presence_without_name_raises(self):
        d = _make_invariant_pack_dict()
        d["checks"][0] = {
            "id": "RP",
            "title": "rp",
            "kind": "rule_presence",
        }
        with pytest.raises(PackParseError, match="rule_name"):
            load_pack_from_dict(d)

    def test_unknown_kind_raises(self):
        d = _make_invariant_pack_dict()
        d["checks"][0]["kind"] = "wibble"
        with pytest.raises(PackParseError, match="Unknown check kind"):
            load_pack_from_dict(d)

    def test_unknown_evidence_kind_raises(self):
        d = _make_invariant_pack_dict()
        d["checks"][0] = {
            "id": "TA",
            "title": "ta",
            "kind": "trace_assertion",
            "evidence": {"kind": "wibble"},
        }
        with pytest.raises(PackParseError, match="unknown evidence kind"):
            load_pack_from_dict(d)

    def test_load_from_yaml_path(self, tmp_path: Path):
        d = _make_invariant_pack_dict()
        path = tmp_path / "pack.yaml"
        path.write_text(yaml.safe_dump(d), encoding="utf-8")
        pack = load_pack_from_yaml(path)
        assert pack.source_path == path

    def test_load_from_yaml_string(self):
        d = _make_invariant_pack_dict()
        pack = load_pack_from_yaml(yaml.safe_dump(d))
        assert pack.name == "test-pack"

    def test_empty_yaml_raises(self):
        with pytest.raises(PackParseError, match="empty"):
            load_pack_from_yaml("")

    def test_bundled_soc2_pack_parses(self):
        """The shipped SOC2 example must always be parseable."""
        bundled = (
            Path(__file__).parent.parent.parent
            / "bog_agents_cli"
            / "compliance"
            / "examples"
            / "soc2-baseline.yaml"
        )
        assert bundled.is_file()
        pack = load_pack_from_yaml(bundled)
        assert pack.name == "soc2-baseline"
        assert len(pack.checks) >= 4
        # Each example check has a control field set.
        assert all(c.control for c in pack.checks)


# ---------------------------------------------------------------------------
# 2. Evidence collectors
# ---------------------------------------------------------------------------


class TestEvidenceCollectors:
    @pytest.fixture
    def slice_with_events(self, tmp_path: Path) -> TraceSlice:
        ledger = open_session(tmp_path)
        ledger.record(EventKind.USER_MESSAGE, actor="user", summary="hi")
        ledger.record(EventKind.USER_MESSAGE, actor="user", summary="hi2")
        ledger.record(
            EventKind.RULE_FIRE,
            actor="block_x",
            summary="deny x",
            payload={"action": "deny"},
        )
        ledger.close()
        window = window_for_lookback(24.0)
        return load_trace_slice(tmp_path, window)

    def test_event_count_pass(self, slice_with_events: TraceSlice):
        finding = collect_event_count(
            slice_with_events,
            {"fact_kind": "user_message", "min": 1},
        )
        assert finding.passes is True
        assert "2 event(s)" in finding.observed

    def test_event_count_max_violated(self, slice_with_events: TraceSlice):
        finding = collect_event_count(
            slice_with_events,
            {"fact_kind": "user_message", "max": 1},
        )
        assert finding.passes is False

    def test_event_count_missing_kind(self, slice_with_events: TraceSlice):
        finding = collect_event_count(slice_with_events, {})
        assert finding.inconclusive is True
        assert "fact_kind" in finding.reason

    def test_event_count_bad_bounds(self, slice_with_events: TraceSlice):
        finding = collect_event_count(
            slice_with_events,
            {"fact_kind": "user_message", "min": 5, "max": 1},
        )
        assert finding.inconclusive is True

    def test_event_count_unknown_kind(self, slice_with_events: TraceSlice):
        finding = collect_event_count(
            slice_with_events,
            {"fact_kind": "no_such_kind"},
        )
        assert finding.inconclusive is True

    def test_no_event_with_actor_pass(self, slice_with_events: TraceSlice):
        finding = collect_no_event_with_actor(
            slice_with_events,
            {"fact_kind": "rule_fire", "actor": "block_x_missing"},
        )
        assert finding.passes is True

    def test_no_event_with_actor_fail(self, slice_with_events: TraceSlice):
        finding = collect_no_event_with_actor(
            slice_with_events,
            {"fact_kind": "rule_fire", "actor": "block_x"},
        )
        assert finding.passes is False
        assert len(finding.samples) >= 1

    def test_no_event_actor_missing(self, slice_with_events: TraceSlice):
        finding = collect_no_event_with_actor(
            slice_with_events,
            {"fact_kind": "rule_fire"},
        )
        assert finding.inconclusive is True

    def test_at_least_one_session_pass(self, slice_with_events: TraceSlice):
        assert collect_at_least_one_session(slice_with_events, {}).passes is True

    def test_at_least_one_session_fail_on_empty(self, tmp_path: Path):
        window = window_for_lookback(24.0)
        empty = load_trace_slice(tmp_path, window)
        assert collect_at_least_one_session(empty, {}).passes is False

    def test_collectors_dispatch_table(self):
        assert set(COLLECTORS) >= {
            "event_count",
            "no_event_with_actor",
            "at_least_one_session",
        }

    def test_load_trace_slice_filters_old_events(self, tmp_path: Path):
        # Create an event then move the window forward.
        ledger = open_session(tmp_path)
        ledger.record(EventKind.USER_MESSAGE, actor="u", summary="old")
        ledger.close()
        # Hour-from-now window — old event filtered out.
        future_window = window_for_lookback(
            0.0001, now=time.time() + 3600,
        )
        slice_ = load_trace_slice(tmp_path, future_window)
        assert len(slice_.events) == 0


# ---------------------------------------------------------------------------
# 3. Runner
# ---------------------------------------------------------------------------


class TestRunner:
    def test_invariant_pass(self, tmp_path: Path):
        pack = load_pack_from_dict(_make_invariant_pack_dict())
        report = run_audit(pack, working_dir=tmp_path, rules=[_guard_rule()])
        assert report.results[0].verdict == Verdict.PASS

    def test_invariant_fail_when_no_guard(self, tmp_path: Path):
        pack = load_pack_from_dict(_make_invariant_pack_dict())
        report = run_audit(pack, working_dir=tmp_path, rules=[])
        assert report.results[0].verdict == Verdict.FAIL

    def test_trace_assertion_pass(self, tmp_path: Path):
        ledger = open_session(tmp_path)
        ledger.record(EventKind.USER_MESSAGE, actor="u", summary="hi")
        ledger.close()
        d = {
            "version": 1,
            "name": "ta-pack",
            "checks": [
                {
                    "id": "TA-1",
                    "title": "user_message present",
                    "kind": "trace_assertion",
                    "evidence": {
                        "kind": "event_count",
                        "fact_kind": "user_message",
                        "min": 1,
                    },
                }
            ],
        }
        pack = load_pack_from_dict(d)
        report = run_audit(pack, working_dir=tmp_path, rules=[])
        assert report.results[0].verdict == Verdict.PASS

    def test_rule_presence_pass_and_fail(self, tmp_path: Path):
        d = {
            "version": 1,
            "name": "rp",
            "checks": [
                {
                    "id": "RP-1",
                    "title": "block_leak loaded",
                    "kind": "rule_presence",
                    "rule_name": "block_leak",
                },
                {
                    "id": "RP-2",
                    "title": "ghost not loaded",
                    "kind": "rule_presence",
                    "rule_name": "ghost_rule",
                },
            ],
        }
        pack = load_pack_from_dict(d)
        report = run_audit(
            pack, working_dir=tmp_path, rules=[_guard_rule()],
        )
        assert report.results[0].verdict == Verdict.PASS
        assert report.results[1].verdict == Verdict.FAIL

    def test_rule_absence(self, tmp_path: Path):
        d = {
            "version": 1,
            "name": "ra",
            "checks": [
                {
                    "id": "RA-1",
                    "title": "no experimental",
                    "kind": "rule_absence",
                    "rule_name": "__experimental_bypass",
                },
                {
                    "id": "RA-2",
                    "title": "no block_leak",
                    "kind": "rule_absence",
                    "rule_name": "block_leak",
                },
            ],
        }
        pack = load_pack_from_dict(d)
        report = run_audit(
            pack, working_dir=tmp_path, rules=[_guard_rule()],
        )
        assert report.results[0].verdict == Verdict.PASS
        assert report.results[1].verdict == Verdict.FAIL

    def test_overall_verdict_aggregation(self, tmp_path: Path):
        # Pack with one PASS + one FAIL → overall FAIL.
        d = {
            "version": 1,
            "name": "agg",
            "checks": [
                {
                    "id": "A",
                    "title": "ok",
                    "kind": "rule_presence",
                    "rule_name": "block_leak",
                },
                {
                    "id": "B",
                    "title": "bad",
                    "kind": "rule_presence",
                    "rule_name": "ghost",
                },
            ],
        }
        pack = load_pack_from_dict(d)
        report = run_audit(
            pack, working_dir=tmp_path, rules=[_guard_rule()]
        )
        assert report.overall == Verdict.FAIL

    def test_overall_inconclusive_when_no_fail_but_some_inconclusive(
        self, tmp_path: Path
    ):
        # event_count without fact_kind → inconclusive.
        d = {
            "version": 1,
            "name": "agg-inc",
            "checks": [
                {
                    "id": "A",
                    "title": "ok",
                    "kind": "rule_presence",
                    "rule_name": "block_leak",
                },
                {
                    "id": "B",
                    "title": "bad bounds",
                    "kind": "trace_assertion",
                    "evidence": {"kind": "event_count"},
                },
            ],
        }
        pack = load_pack_from_dict(d)
        report = run_audit(
            pack, working_dir=tmp_path, rules=[_guard_rule()]
        )
        assert report.overall == Verdict.INCONCLUSIVE

    def test_records_sessions_audited(self, tmp_path: Path):
        ledger = open_session(tmp_path)
        ledger.record(EventKind.USER_MESSAGE, actor="u", summary="hi")
        ledger.close()
        pack = load_pack_from_dict(_make_invariant_pack_dict())
        report = run_audit(pack, working_dir=tmp_path, rules=[])
        assert ledger.session_id in report.sessions_audited


# ---------------------------------------------------------------------------
# 4. Renderer + seal
# ---------------------------------------------------------------------------


class TestRenderer:
    def test_render_markdown_includes_all_sections(self, tmp_path: Path):
        pack = load_pack_from_dict(_make_invariant_pack_dict())
        report = run_audit(pack, working_dir=tmp_path, rules=[_guard_rule()])
        md = render_markdown(report, pack=pack)
        for header in (
            "# Audit report:",
            "**Overall verdict:**",
            "## Summary",
            "## Audit metadata",
            "## Control coverage",
            "## Per-check detail",
        ):
            assert header in md

    def test_seal_round_trip(
        self, tmp_path: Path, deterministic_key: bytes
    ):
        pack = load_pack_from_dict(_make_invariant_pack_dict())
        report = run_audit(pack, working_dir=tmp_path, rules=[_guard_rule()])
        body = render_markdown(report, pack=pack)
        sealed = seal_report(body, key=deterministic_key)
        ok, msg = verify_seal(sealed, key=deterministic_key)
        assert ok is True, msg

    def test_seal_detects_tampering(
        self, tmp_path: Path, deterministic_key: bytes
    ):
        pack = load_pack_from_dict(_make_invariant_pack_dict())
        report = run_audit(pack, working_dir=tmp_path, rules=[_guard_rule()])
        body = render_markdown(report, pack=pack)
        sealed = seal_report(body, key=deterministic_key)
        # Tamper with the body — flip a Pass to a Fail in the table.
        tampered = sealed.replace("✓ pass", "✗ fail", 1)
        ok, msg = verify_seal(tampered, key=deterministic_key)
        assert ok is False
        assert "mismatch" in msg or "edited" in msg

    def test_seal_detects_missing_footer(self, deterministic_key: bytes):
        ok, msg = verify_seal("just text, no seal", key=deterministic_key)
        assert ok is False
        assert "no seal footer" in msg

    def test_seal_detects_malformed_footer(self, deterministic_key: bytes):
        broken = "body\n\n---\n\n## Seal\n\n- algorithm: HMAC-SHA-256\n"
        ok, _msg = verify_seal(broken, key=deterministic_key)
        assert ok is False

    def test_seal_is_deterministic(
        self, tmp_path: Path, deterministic_key: bytes
    ):
        body = "stable body\n"
        s1 = seal_report(body, key=deterministic_key)
        s2 = seal_report(body, key=deterministic_key)
        # Sealed_at differs by epoch second — strip and compare bodies.
        digest1 = [
            line for line in s1.splitlines() if line.startswith("- digest:")
        ]
        digest2 = [
            line for line in s2.splitlines() if line.startswith("- digest:")
        ]
        assert digest1 == digest2

    def test_json_sidecar_round_trip(
        self, tmp_path: Path
    ):
        pack = load_pack_from_dict(_make_invariant_pack_dict())
        report = run_audit(pack, working_dir=tmp_path, rules=[])
        payload = report_to_json(report)
        # Must be valid JSON with the structural fields a CI gate
        # would key on.
        import json

        parsed = json.loads(payload)
        assert parsed["pack_name"] == "test-pack"
        assert "overall" in parsed
        assert "counts" in parsed
        assert parsed["overall"] in {v.value for v in Verdict}


# ---------------------------------------------------------------------------
# 5. Controller dispatch
# ---------------------------------------------------------------------------


class TestController:
    @pytest.fixture
    def project_with_pack(
        self, tmp_path: Path, deterministic_key: bytes
    ) -> tuple[Path, Path]:
        """tmp_path acts as working dir; drop a pack under audit_packs/."""
        pack_dir = tmp_path / "audit_packs"
        pack_dir.mkdir()
        pack_path = pack_dir / "test.yaml"
        pack_path.write_text(
            yaml.safe_dump(_make_invariant_pack_dict()), encoding="utf-8"
        )
        # Add an event so the audit has a session to point at.
        ledger = open_session(tmp_path)
        ledger.record(EventKind.USER_MESSAGE, actor="u", summary="hi")
        ledger.close()
        return tmp_path, pack_path

    def test_help_empty(self, tmp_path: Path):
        out = audit_dispatch("/audit", tmp_path)
        assert "/compliance run" in out

    def test_help_word(self, tmp_path: Path):
        out = audit_dispatch("/audit help", tmp_path)
        assert "/compliance run" in out

    def test_packs_lists_bundled(self, tmp_path: Path):
        out = audit_dispatch("/audit packs", tmp_path)
        assert "soc2-baseline.yaml" in out

    def test_packs_lists_local(self, project_with_pack):
        wdir, _ = project_with_pack
        out = audit_dispatch("/audit packs", wdir)
        assert "test.yaml" in out

    def test_list_empty(self, tmp_path: Path):
        out = audit_dispatch("/audit list", tmp_path)
        assert "No audits saved" in out

    def test_run_and_list(self, project_with_pack):
        wdir, _ = project_with_pack
        out = audit_dispatch("/audit run audit_packs/test.yaml", wdir)
        assert "Audit complete" in out
        assert "Saved markdown" in out

        ls_out = audit_dispatch("/audit list", wdir)
        assert ".md" in ls_out

    def test_run_unknown_pack(self, tmp_path: Path):
        out = audit_dispatch("/audit run not-a-real-pack.yaml", tmp_path)
        assert "Pack file not found" in out

    def test_run_bundled_by_name(self, tmp_path: Path):
        out = audit_dispatch(
            "/audit run soc2-baseline.yaml", tmp_path
        )
        assert "Audit complete" in out

    def test_show_unknown(self, tmp_path: Path):
        out = audit_dispatch("/audit show no-such.md", tmp_path)
        assert "not found" in out

    def test_show_verifies_seal(self, project_with_pack):
        wdir, _ = project_with_pack
        audit_dispatch("/audit run audit_packs/test.yaml", wdir)
        # Find the saved file
        files = sorted((wdir / ".bog-agents" / "audits").glob("*.md"))
        assert files
        out = audit_dispatch(f"/audit show {files[0].name}", wdir)
        assert "[seal: OK" in out

    def test_show_detects_tampering(self, project_with_pack):
        wdir, _ = project_with_pack
        audit_dispatch("/audit run audit_packs/test.yaml", wdir)
        files = sorted((wdir / ".bog-agents" / "audits").glob("*.md"))
        # Tamper with the saved report.
        text = files[0].read_text(encoding="utf-8")
        files[0].write_text(text.replace("Per-check detail", "PWNED"), encoding="utf-8")
        out = audit_dispatch(f"/audit show {files[0].name}", wdir)
        assert "[seal: INVALID" in out

    def test_unknown_subcommand(self, tmp_path: Path):
        out = audit_dispatch("/audit wibble", tmp_path)
        assert "Unknown" in out


# ---------------------------------------------------------------------------
# 6. Cron entrypoint
# ---------------------------------------------------------------------------


class TestCron:
    @pytest.fixture
    def passing_project(
        self, tmp_path: Path, deterministic_key: bytes
    ) -> Path:
        # Use the bundled pack but inject a passing setup: a session
        # with a user_message + a rule that satisfies the invariant.
        ledger = open_session(tmp_path)
        ledger.record(EventKind.USER_MESSAGE, actor="u", summary="hi")
        ledger.close()
        return tmp_path

    def test_cron_resolves_bundled_pack(
        self, passing_project: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # When no rules are loaded, the SOC2 invariant fails — so the
        # default fail_on_non_pass raises.
        with pytest.raises(CronAuditFailed):
            cron_run(
                working_dir=passing_project,
                pack="soc2-baseline.yaml",
            )

    def test_cron_fail_on_non_pass_disabled(
        self, passing_project: Path
    ):
        outcome = cron_run(
            working_dir=passing_project,
            pack="soc2-baseline.yaml",
            fail_on_non_pass=False,
        )
        assert outcome.pack_name == "soc2-baseline"
        assert outcome.saved_markdown.exists()
        assert outcome.saved_json.exists()

    def test_cron_unknown_pack(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError, match="resolve"):
            cron_run(working_dir=tmp_path, pack="nope.yaml")

    def test_cron_project_local_pack(self, tmp_path: Path):
        # All-pass minimal pack: just a rule_absence that's guaranteed true.
        d = {
            "version": 1,
            "name": "tiny",
            "window": {"lookback_hours": 1.0},
            "checks": [
                {
                    "id": "A",
                    "title": "ok",
                    "kind": "rule_absence",
                    "rule_name": "definitely-not-loaded",
                }
            ],
        }
        pack_dir = tmp_path / "audit_packs"
        pack_dir.mkdir()
        (pack_dir / "tiny.yaml").write_text(
            yaml.safe_dump(d), encoding="utf-8"
        )
        outcome = cron_run(
            working_dir=tmp_path,
            pack="tiny.yaml",
        )
        assert outcome.overall == Verdict.PASS
