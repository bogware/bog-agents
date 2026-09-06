"""ROADMAP #74: the CLI side of the action log — approval chain, Expert sink, /actionlog verbs, signed export."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from bog_agents.action_log import verify_chain, verify_export

from bog_agents_cli import action_log_controller as alc

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    directory = tmp_path / "action-log"
    monkeypatch.setattr(alc, "action_log_dir", lambda: directory)
    monkeypatch.setattr(alc, "enabled", lambda: True)
    monkeypatch.setattr(alc, "_APPROVALS", None)
    yield directory
    monkeypatch.setattr(alc, "_APPROVALS", None)


def _decision(**overrides: object) -> SimpleNamespace:
    base = {
        "tool": "execute",
        "call": "rm -rf build",
        "decision": "reject",
        "rule_source": "never_allow",
        "risk": "high",
        "reason": "destructive",
        "judge": "",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_approvals_and_verdicts_share_one_chain(_isolated: Path) -> None:
    assert (
        alc.record_approval_events(
            [_decision(), _decision(decision="allow", tool="read_file")]
        )
        == 2
    )
    sink = alc.expert_audit_sink()
    assert sink is not None
    sink("deny", {"rule": "no-prod", "tool": "execute"})
    logs = alc.list_logs()
    assert len(logs) == 1 and logs[0].name.startswith("approvals-")
    result = verify_chain(logs[0])
    assert result.ok and result.checked == 3
    kinds = [
        json.loads(line)["kind"]
        for line in logs[0].read_text(encoding="utf-8").splitlines()
    ]
    assert kinds == ["approval", "approval", "expert_verdict"]


def test_disabled_log_records_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(alc, "enabled", lambda: False)
    assert alc.approvals_log() is None
    assert alc.record_approval_events([_decision()]) == 0
    assert alc.expert_audit_sink() is None
    assert "off" in alc.run_actionlog_command("/actionlog")


def test_verbs_status_verify_export_prune(
    _isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert "no chains yet" in alc.run_actionlog_command("/actionlog status")
    assert alc.run_actionlog_command("/actionlog verify") == "No action log to verify."
    alc.record_approval_events([_decision()])
    status = alc.run_actionlog_command("/actionlog")
    assert "chain intact: 1 event(s)" in status and "Action log: on" in status
    assert "chain intact" in alc.run_actionlog_command("/actionlog verify")
    assert "No action log matches" in alc.run_actionlog_command(
        "/actionlog verify nope"
    )

    monkeypatch.setattr(
        alc, "signer", lambda: ((lambda payload: "sig:" + str(len(payload))), "fp-1")
    )
    reply = alc.run_actionlog_command("/actionlog export")
    assert "signed with TraceFile key fp-1" in reply
    export_path = next(_isolated.glob("export-*.json"))
    bundle = json.loads(export_path.read_text(encoding="utf-8"))
    assert bundle["signer"] == "fp-1" and bundle["count"] == 1
    assert verify_export(
        bundle, verify=lambda payload, sig: sig == "sig:" + str(len(payload))
    ).ok

    assert "unsigned" in alc.run_actionlog_command("/actionlog export --unsigned")
    assert alc.run_actionlog_command("/actionlog bogus") == alc.USAGE
    assert "Pruned 0 chain(s)" in alc.run_actionlog_command(
        "/actionlog prune --days 30"
    )
    assert alc.run_actionlog_command("/actionlog prune --days") == alc.USAGE


def test_real_tracefile_signer_round_trip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bog_agents_cli.tracefile.signing import generate_keypair, save_keypair, verify

    key_path = tmp_path / "keys" / "tracefile.key"
    save_keypair(generate_keypair(), key_path)
    monkeypatch.setenv("BOG_AGENTS_TRACEFILE_KEY", str(key_path))
    signing = alc.signer()
    assert signing is not None
    _sign, fingerprint = signing
    alc.record_approval_events([_decision()])
    alc.run_actionlog_command("/actionlog export")
    bundle = json.loads(
        next(alc.action_log_dir().glob("export-*.json")).read_text(encoding="utf-8")
    )
    assert bundle["signer"] == fingerprint
    from bog_agents_cli.tracefile.signing import load_keypair_from_path

    material = load_keypair_from_path(key_path)
    assert verify_export(
        bundle, verify=lambda payload, sig: verify(material, payload, sig)
    ).ok
