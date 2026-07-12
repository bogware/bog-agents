"""Tests for TraceFile v1 (Wave S).

Six layers:

1. Signing primitives — keygen, save/load, sign/verify round-trip,
   key-collision detection.
2. Canonical JSON + frame hashing — byte-stable across runs;
   prev_hash linkage works.
3. build_tracefile + write_tracefile round-trip via read_tracefile.
4. verify_tracefile — happy path, broken chain, wrong key, tampered
   signature, mismatched session id.
5. Claude Code adapter — hook payload → frames, transcript walker,
   end-to-end session_to_tracefile.
6. /trace slash dispatch — export / import / verify / keygen /
   refusal of unverified imports.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from bog_agents_cli.causal.ledger import EventKind, open_session
from bog_agents_cli.tracefile import (
    KeyMaterial,
    TraceFileError,
    TraceFrame,
    TraceHeader,
    build_tracefile,
    canonical_frame_hash,
    canonical_json,
    generate_keypair,
    load_keypair_from_path,
    read_tracefile,
    save_keypair,
    verify_tracefile,
    write_tracefile,
)
from bog_agents_cli.tracefile.controller import dispatch as trace_dispatch
from bog_agents_cli.tracefile.exporters.claude_code import (
    ClaudeCodeExportError,
    claude_code_hook_to_frames,
    claude_code_session_to_tracefile,
    hook_payload_stream_to_frames,
    parse_claude_code_session_log,
)
from bog_agents_cli.tracefile.signing import (
    SignatureVerificationError,
    SigningError,
    material_from_public_b64,
    sign,
    verify,
)
from bog_agents_cli.tracefile.spec import (
    SPEC_VERSION,
    parse_tracefile,
)

# ---------------------------------------------------------------------------
# 1. Signing primitives
# ---------------------------------------------------------------------------


class TestSigning:
    def test_generate_keypair_yields_distinct_keys(self):
        a = generate_keypair()
        b = generate_keypair()
        assert a.public_key_b64 != b.public_key_b64
        assert a.fingerprint != b.fingerprint

    def test_save_and_load_roundtrip(self, tmp_path: Path):
        material = generate_keypair()
        path = tmp_path / "key"
        save_keypair(material, path)
        loaded = load_keypair_from_path(path)
        assert loaded.public_key_b64 == material.public_key_b64
        assert loaded.fingerprint == material.fingerprint

    def test_load_corrupt_file_raises(self, tmp_path: Path):
        path = tmp_path / "bad"
        path.write_text("not a key", encoding="utf-8")
        with pytest.raises(SigningError):
            load_keypair_from_path(path)

    def test_sign_and_verify_roundtrip(self):
        material = generate_keypair()
        message = b"hello world"
        sig = sign(material, message)
        assert verify(material, message, sig) is True

    def test_verify_tampered_message_raises(self):
        material = generate_keypair()
        sig = sign(material, b"hello")
        with pytest.raises(SignatureVerificationError):
            verify(material, b"goodbye", sig)

    def test_verify_corrupt_signature_raises(self):
        material = generate_keypair()
        with pytest.raises(SignatureVerificationError):
            verify(material, b"hello", "not-base64-***")

    def test_verify_only_material_can_verify_not_sign(self):
        original = generate_keypair()
        ver_only = material_from_public_b64(original.public_key_b64)
        sig = sign(original, b"msg")
        assert verify(ver_only, b"msg", sig) is True
        with pytest.raises(SigningError):
            sign(ver_only, b"msg")

    def test_save_keypair_refuses_verify_only(self, tmp_path: Path):
        original = generate_keypair()
        ver_only = material_from_public_b64(original.public_key_b64)
        with pytest.raises(SigningError):
            save_keypair(ver_only, tmp_path / "k")


# ---------------------------------------------------------------------------
# 2. Canonical JSON + frame hash
# ---------------------------------------------------------------------------


class TestCanonical:
    def test_canonical_json_sorts_keys(self):
        out = canonical_json({"b": 1, "a": 2})
        assert out == '{"a":2,"b":1}'

    def test_canonical_json_no_whitespace(self):
        out = canonical_json([{"k": "v"}, 1])
        assert " " not in out
        assert out == '[{"k":"v"},1]'

    def test_canonical_json_preserves_unicode(self):
        out = canonical_json({"x": "café→"})
        assert "café" in out
        assert "→" in out

    def test_frame_hash_is_deterministic(self):
        frame = TraceFrame(
            id=1,
            event_kind="user_message",
            actor="user",
            summary="hi",
            timestamp=1.0,
        )
        h1 = canonical_frame_hash("0" * 64, frame)
        h2 = canonical_frame_hash("0" * 64, frame)
        assert h1 == h2
        assert len(h1) == 64  # 32 bytes hex

    def test_frame_hash_depends_on_prev(self):
        frame = TraceFrame(
            id=1,
            event_kind="x",
            actor="a",
            summary="s",
            timestamp=1.0,
        )
        h1 = canonical_frame_hash("0" * 64, frame)
        h2 = canonical_frame_hash("1" * 64, frame)
        assert h1 != h2


# ---------------------------------------------------------------------------
# 3. Build + write + read round-trip
# ---------------------------------------------------------------------------


def _sample_frames(n: int = 3) -> list[TraceFrame]:
    return [
        TraceFrame(
            id=i + 1,
            event_kind="user_message" if i == 0 else "tool_call",
            actor="user" if i == 0 else "shell",
            summary=f"event {i + 1}",
            timestamp=1700000000.0 + i,
            parents=(i,) if i > 0 else (),
            payload={"index": i},
        )
        for i in range(n)
    ]


class TestBuildAndRoundTrip:
    def test_build_assigns_hashes_in_order(self):
        key = generate_keypair()
        tf = build_tracefile(
            _sample_frames(),
            key=key,
            session_id="sess-A",
            produced_at=1700000000.0,
        )
        assert len(tf.frames) == 3
        # First frame links to the zero hash; subsequent frames
        # carry the previous frame's hash as prev_hash.
        assert tf.frames[0].prev_hash == "0" * 64
        assert tf.frames[1].prev_hash == tf.frames[0].frame_hash
        assert tf.frames[2].prev_hash == tf.frames[1].frame_hash
        assert tf.merkle_root == tf.frames[-1].frame_hash

    def test_build_refuses_verify_only_key(self):
        original = generate_keypair()
        verify_only = material_from_public_b64(original.public_key_b64)
        with pytest.raises(TraceFileError):
            build_tracefile(
                _sample_frames(),
                key=verify_only,
                session_id="s",
            )

    def test_build_refuses_empty_frames(self):
        key = generate_keypair()
        with pytest.raises(TraceFileError):
            build_tracefile([], key=key, session_id="s")

    def test_write_then_read_roundtrip(self, tmp_path: Path):
        key = generate_keypair()
        tf = build_tracefile(
            _sample_frames(),
            key=key,
            session_id="sess-B",
            produced_at=1700000000.0,
        )
        path = tmp_path / "out.trace"
        write_tracefile(tf, path)
        loaded = read_tracefile(path)
        assert loaded.header.session_id == tf.header.session_id
        assert len(loaded.frames) == len(tf.frames)
        assert loaded.merkle_root == tf.merkle_root
        assert loaded.public_key_b64 == tf.public_key_b64
        assert loaded.signature_b64 == tf.signature_b64
        # Frame-by-frame deep equality.
        for a, b in zip(tf.frames, loaded.frames, strict=True):
            assert a == b

    def test_dict_input_works(self):
        key = generate_keypair()
        as_dicts = [
            {
                "id": 1,
                "event_kind": "user_message",
                "actor": "user",
                "summary": "hi",
                "timestamp": 1.0,
            },
            {
                "id": 2,
                "event_kind": "tool_call",
                "actor": "shell",
                "summary": "ls",
                "timestamp": 2.0,
                "parents": [1],
                "payload": {"cmd": "ls"},
            },
        ]
        tf = build_tracefile(as_dicts, key=key, session_id="s")
        assert tf.frames[1].payload["cmd"] == "ls"

    def test_malformed_dict_raises(self):
        key = generate_keypair()
        with pytest.raises(TraceFileError):
            build_tracefile([{"event_kind": "x"}], key=key, session_id="s")


# ---------------------------------------------------------------------------
# 4. Verification
# ---------------------------------------------------------------------------


class TestVerify:
    def test_happy_path(self, tmp_path: Path):
        key = generate_keypair()
        tf = build_tracefile(_sample_frames(), key=key, session_id="sess-V")
        path = tmp_path / "x.trace"
        write_tracefile(tf, path)
        loaded = read_tracefile(path)
        result = verify_tracefile(loaded)
        assert result.ok is True
        assert result.chain_ok is True
        assert result.signature_ok is True
        assert result.frames_checked == 3
        assert result.fingerprint == key.fingerprint

    def test_broken_chain_caught(self, tmp_path: Path):
        key = generate_keypair()
        tf = build_tracefile(_sample_frames(), key=key, session_id="s")
        path = tmp_path / "x.trace"
        write_tracefile(tf, path)
        # Corrupt one frame's summary in the file.
        text = path.read_text(encoding="utf-8")
        corrupt = text.replace('"summary":"event 2"', '"summary":"HACKED"')
        path.write_text(corrupt, encoding="utf-8")
        loaded = read_tracefile(path)
        result = verify_tracefile(loaded)
        assert result.ok is False
        assert result.chain_ok is False
        assert "chain" in result.message.lower()

    def test_wrong_key_rejected(self, tmp_path: Path):
        key = generate_keypair()
        tf = build_tracefile(_sample_frames(), key=key, session_id="s")
        other = generate_keypair()
        # Use verify-only material derived from the OTHER key.
        ver_only = material_from_public_b64(other.public_key_b64)
        result = verify_tracefile(tf, key=ver_only)
        assert result.ok is False
        assert "does not match" in result.message

    def test_tampered_signature_caught(self, tmp_path: Path):
        key = generate_keypair()
        tf = build_tracefile(_sample_frames(), key=key, session_id="s")
        # Mutate the signature directly in the in-memory TraceFile by
        # producing a new TraceFile dataclass instance.
        from bog_agents_cli.tracefile.spec import TraceFile

        bad_sig = "A" * len(tf.signature_b64)
        tampered = TraceFile(
            header=tf.header,
            frames=tf.frames,
            signature_b64=bad_sig,
            public_key_b64=tf.public_key_b64,
            merkle_root=tf.merkle_root,
        )
        result = verify_tracefile(tampered)
        assert result.ok is False
        assert result.signature_ok is False

    def test_parse_refuses_unknown_version(self, tmp_path: Path):
        key = generate_keypair()
        tf = build_tracefile(_sample_frames(), key=key, session_id="s")
        text = trace_to_text(tf)
        bumped = text.replace(f'"version":{SPEC_VERSION}', '"version":99')
        with pytest.raises(TraceFileError):
            parse_tracefile(bumped)

    def test_parse_refuses_unknown_alg(self):
        key = generate_keypair()
        tf = build_tracefile(_sample_frames(), key=key, session_id="s")
        text = trace_to_text(tf)
        bad = text.replace('"sign_alg":"ed25519"', '"sign_alg":"rsa-pss"')
        with pytest.raises(TraceFileError):
            parse_tracefile(bad)

    def test_parse_refuses_too_few_lines(self):
        with pytest.raises(TraceFileError):
            parse_tracefile("only one line\n")


def trace_to_text(tf) -> str:
    """Convenience: render a TraceFile to its on-disk text form."""
    import tempfile

    with tempfile.NamedTemporaryFile(
        "w", suffix=".trace", encoding="utf-8", delete=False
    ) as fh:
        tmp_path = Path(fh.name)
    try:
        write_tracefile(tf, tmp_path)
        return tmp_path.read_text(encoding="utf-8")
    finally:
        try:
            Path(tmp_path).unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# 5. Claude Code adapter
# ---------------------------------------------------------------------------


_CLAUDE_HOOK_PAYLOAD_POST = {
    "session_id": "abc",
    "hook_event_name": "PostToolUse",
    "tool_name": "Bash",
    "tool_input": {"command": "ls"},
    "tool_response": {"output": "file.txt\n", "is_error": False},
}

_CLAUDE_HOOK_PAYLOAD_USER = {
    "session_id": "abc",
    "hook_event_name": "UserPromptSubmit",
    "prompt": "what is 2+2?",
}


class TestClaudeCodeAdapter:
    def test_hook_post_yields_call_and_result(self):
        frames = claude_code_hook_to_frames(_CLAUDE_HOOK_PAYLOAD_POST, next_id=1)
        kinds = [f.event_kind for f in frames]
        assert "tool_call" in kinds
        assert "tool_result" in kinds
        # Result references the call as its parent.
        result_frame = next(f for f in frames if f.event_kind == "tool_result")
        call_frame = next(f for f in frames if f.event_kind == "tool_call")
        assert call_frame.id in result_frame.parents

    def test_hook_user_prompt(self):
        frames = claude_code_hook_to_frames(_CLAUDE_HOOK_PAYLOAD_USER, next_id=1)
        assert len(frames) == 1
        assert frames[0].event_kind == "user_message"
        assert "2+2" in frames[0].summary

    def test_hook_missing_event_name_raises(self):
        with pytest.raises(ClaudeCodeExportError):
            claude_code_hook_to_frames({}, next_id=1)

    def test_hook_non_dict_payload_raises(self):
        with pytest.raises(ClaudeCodeExportError):
            claude_code_hook_to_frames("not a dict", next_id=1)  # type: ignore[arg-type]

    def test_payload_stream_chains_ids(self):
        frames = hook_payload_stream_to_frames(
            [_CLAUDE_HOOK_PAYLOAD_USER, _CLAUDE_HOOK_PAYLOAD_POST]
        )
        ids = [f.id for f in frames]
        assert ids == sorted(ids)
        assert len(set(ids)) == len(ids)

    def test_transcript_walker_round_trip(self, tmp_path: Path):
        # Mimic a Claude Code transcript JSONL.
        transcript = tmp_path / "session.jsonl"
        transcript.write_text(
            "\n".join(
                [
                    json.dumps({"type": "user", "text": "do x", "timestamp": 1.0}),
                    json.dumps({"type": "assistant", "text": "ok"}),
                    json.dumps(
                        {
                            "type": "tool_use",
                            "tool_name": "Bash",
                            "input": {"command": "ls"},
                        }
                    ),
                    json.dumps(
                        {
                            "type": "tool_result",
                            "content": "file.txt",
                        }
                    ),
                    "broken json not valid",  # skipped silently
                ]
            ),
            encoding="utf-8",
        )
        frames = parse_claude_code_session_log(transcript)
        kinds = [f.event_kind for f in frames]
        assert kinds == [
            "user_message",
            "model_call",
            "tool_call",
            "tool_result",
        ]
        # ids are 1..4, parents thread the chain.
        assert frames[0].parents == ()
        assert frames[1].parents == (frames[0].id,)
        assert frames[-1].parents == (frames[-2].id,)

    def test_session_to_tracefile_signs_real(self, tmp_path: Path):
        transcript = tmp_path / "s.jsonl"
        transcript.write_text(
            "\n".join(
                [
                    json.dumps({"type": "user", "text": "do x"}),
                    json.dumps({"type": "assistant", "text": "ok"}),
                ]
            ),
            encoding="utf-8",
        )
        key = generate_keypair()
        tf = claude_code_session_to_tracefile(transcript, key=key)
        result = verify_tracefile(tf)
        assert result.ok is True
        assert tf.header.producer.startswith("claude-code-hook")

    def test_session_to_tracefile_empty_raises(self, tmp_path: Path):
        empty = tmp_path / "empty.jsonl"
        empty.write_text("", encoding="utf-8")
        key = generate_keypair()
        with pytest.raises(ClaudeCodeExportError):
            claude_code_session_to_tracefile(empty, key=key)


# ---------------------------------------------------------------------------
# 6. Slash dispatch
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Pin the default key location to a tmp file.

    So /tracefile export doesn't touch the developer's
    ~/.bog-agents directory while tests run.
    """
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: fake_home))
    return fake_home


class TestSlashDispatch:
    def test_help(self, tmp_path: Path):
        out = trace_dispatch("/tracefile", tmp_path)
        assert "/tracefile export" in out
        assert "/tracefile import" in out

    def test_unknown_subcommand(self, tmp_path: Path):
        out = trace_dispatch("/tracefile wibble", tmp_path)
        assert "Unknown /tracefile subcommand" in out

    def test_keygen_creates_key(self, tmp_path: Path, isolated_key: Path):
        out = trace_dispatch(f"/tracefile keygen --out {tmp_path / 'k.key'}", tmp_path)
        assert "Generated Ed25519 keypair" in out
        assert (tmp_path / "k.key").is_file()

    def test_keygen_refuses_overwrite(self, tmp_path: Path):
        target = tmp_path / "k.key"
        target.write_text("existing", encoding="utf-8")
        out = trace_dispatch(f"/tracefile keygen --out {target}", tmp_path)
        assert "Refusing to overwrite" in out

    def test_export_no_sessions(self, tmp_path: Path, isolated_key: Path):
        out = trace_dispatch("/tracefile export latest", tmp_path)
        assert "No causal sessions" in out

    def test_export_round_trip(self, tmp_path: Path, isolated_key: Path):
        ledger = open_session(tmp_path)
        ledger.record(EventKind.USER_MESSAGE, actor="user", summary="hi")
        ledger.record(
            EventKind.MODEL_CALL,
            actor="m",
            summary="thinking",
            parent_ids=(1,),
        )
        ledger.close()
        out = trace_dispatch(f"/tracefile export {ledger.session_id}", tmp_path)
        assert "TraceFile exported" in out
        # Find the file we just wrote.
        files = list((tmp_path / ".bog-agents" / "tracefiles").glob("*.trace"))
        assert files
        path = files[0]
        # Verify round-trip through the dispatcher.
        verify_out = trace_dispatch(f"/tracefile verify {path}", tmp_path)
        assert "✓ TraceFile" in verify_out
        # And import-render works.
        import_out = trace_dispatch(f"/tracefile import {path}", tmp_path)
        assert "TraceFile (verified)" in import_out
        assert "hi" in import_out

    def test_import_refuses_tampered(self, tmp_path: Path, isolated_key: Path):
        ledger = open_session(tmp_path)
        ledger.record(EventKind.USER_MESSAGE, actor="user", summary="hi")
        ledger.close()
        trace_dispatch(f"/tracefile export {ledger.session_id}", tmp_path)
        files = list((tmp_path / ".bog-agents" / "tracefiles").glob("*.trace"))
        assert files
        text = files[0].read_text(encoding="utf-8")
        files[0].write_text(text.replace("hi", "HACKED"), encoding="utf-8")
        out = trace_dispatch(f"/tracefile import {files[0]}", tmp_path)
        assert "refused unverified TraceFile" in out

    def test_unparseable_args(self, tmp_path: Path):
        out = trace_dispatch('/tracefile export "unbalanced', tmp_path)
        assert "Could not parse" in out

    def test_export_missing_argument(self, tmp_path: Path):
        out = trace_dispatch("/tracefile export", tmp_path)
        assert "Usage: /tracefile export" in out

    def test_verify_unknown_file(self, tmp_path: Path):
        out = trace_dispatch(
            f"/tracefile verify {tmp_path / 'missing.trace'}", tmp_path
        )
        assert "not found" in out


# ---------------------------------------------------------------------------
# Header round-trip sanity (lightweight integration)
# ---------------------------------------------------------------------------


class TestHeader:
    def test_header_is_first_line_of_serialized(self, tmp_path: Path):
        key = generate_keypair()
        tf = build_tracefile(
            _sample_frames(),
            key=key,
            session_id="sess-h",
            actor="claude-haiku-4-5-20251001",
            produced_at=1700000000.0,
        )
        path = tmp_path / "h.trace"
        write_tracefile(tf, path)
        first = path.read_text(encoding="utf-8").splitlines()[0]
        obj = json.loads(first)
        assert obj["kind"] == "header"
        assert obj["actor"] == "claude-haiku-4-5-20251001"
        assert obj["version"] == SPEC_VERSION

    def test_default_header_includes_zero_notes_when_none(self, tmp_path: Path):
        key = generate_keypair()
        tf = build_tracefile(_sample_frames(), key=key, session_id="x")
        assert tf.header.notes == ()


# ---------------------------------------------------------------------------
# 7. Header binding (P24) — producer/actor/notes are signed (sign_format v2)
# ---------------------------------------------------------------------------


def _retext(tf, *, replace: tuple[str, str] | None = None) -> str:
    """Render a TraceFile to text, optionally applying a string swap.

    The swap mimics an attacker editing the on-disk header in place.
    """
    text = trace_to_text(tf)
    if replace is not None:
        old, new = replace
        assert old in text, f"expected {old!r} in serialized TraceFile"
        text = text.replace(old, new)
    return text


class TestHeaderBinding:
    """P24: the free-text header fields must be cryptographically bound.

    Forging producer/actor/notes on a v2 file must break verification;
    legacy v1 files (no header binding) must keep verifying so old
    artefacts don't suddenly fail.
    """

    def test_new_files_default_to_sign_format_v2(self):
        from bog_agents_cli.tracefile.spec import SIGN_FORMAT_V2

        key = generate_keypair()
        tf = build_tracefile(
            _sample_frames(),
            key=key,
            session_id="sess-v2",
            producer="prod/1.0",
            actor="claude-haiku",
            notes=("origin=ci",),
        )
        assert tf.header.sign_format == SIGN_FORMAT_V2
        assert verify_tracefile(tf).ok is True

    def test_tampered_producer_fails_v2(self):
        key = generate_keypair()
        tf = build_tracefile(
            _sample_frames(),
            key=key,
            session_id="s",
            producer="trusted/1.0",
            actor="claude",
            notes=("origin=ci",),
        )
        forged = _retext(
            tf, replace=('"producer":"trusted/1.0"', '"producer":"evilcorp/9.9"')
        )
        loaded = parse_tracefile(forged)
        assert loaded.header.producer == "evilcorp/9.9"
        result = verify_tracefile(loaded)
        assert result.ok is False
        assert result.signature_ok is False
        # Chain is untouched — only the signature should fail.
        assert result.chain_ok is True

    def test_tampered_actor_fails_v2(self):
        key = generate_keypair()
        tf = build_tracefile(
            _sample_frames(),
            key=key,
            session_id="s",
            actor="claude-haiku-4-5",
        )
        forged = _retext(
            tf, replace=('"actor":"claude-haiku-4-5"', '"actor":"gpt-impersonator"')
        )
        loaded = parse_tracefile(forged)
        assert loaded.header.actor == "gpt-impersonator"
        result = verify_tracefile(loaded)
        assert result.ok is False
        assert result.signature_ok is False

    def test_tampered_notes_fails_v2(self):
        key = generate_keypair()
        tf = build_tracefile(
            _sample_frames(),
            key=key,
            session_id="s",
            notes=("origin=internal-audit",),
        )
        forged = _retext(
            tf, replace=("origin=internal-audit", "origin=blessed-by-security")
        )
        loaded = parse_tracefile(forged)
        assert "origin=blessed-by-security" in loaded.header.notes
        result = verify_tracefile(loaded)
        assert result.ok is False
        assert result.signature_ok is False

    def test_legacy_v1_file_still_verifies(self):
        """A genuine v1 artefact (no sign_format key) must still verify."""
        from bog_agents_cli.tracefile.spec import SIGN_FORMAT_V1

        key = generate_keypair()
        tf = build_tracefile(
            _sample_frames(),
            key=key,
            session_id="legacy",
            producer="legacy-producer/0.1",
            actor="legacy-actor",
            notes=("legacy-note",),
            sign_format=SIGN_FORMAT_V1,
        )
        assert tf.header.sign_format == SIGN_FORMAT_V1
        # Emulate an on-disk v1 file: strip the sign_format key entirely,
        # exactly as files emitted before this change look.
        text = trace_to_text(tf)
        stripped = text.replace(',"sign_format":1', "")
        assert "sign_format" not in stripped.splitlines()[0]
        loaded = parse_tracefile(stripped)
        assert loaded.header.sign_format == SIGN_FORMAT_V1
        result = verify_tracefile(loaded)
        assert result.ok is True
        assert result.signature_ok is True

    def test_legacy_v1_does_not_bind_header(self):
        """v1 keeps its old semantics — header tamper does NOT break v1.

        This documents the backward-compat trade-off: v1 files never
        protected the header, and we keep that path byte-identical. The
        protection is opt-in via v2 (the new default for fresh files).
        """
        from bog_agents_cli.tracefile.spec import SIGN_FORMAT_V1

        key = generate_keypair()
        tf = build_tracefile(
            _sample_frames(),
            key=key,
            session_id="legacy",
            producer="old/1.0",
            sign_format=SIGN_FORMAT_V1,
        )
        text = trace_to_text(tf).replace(',"sign_format":1', "")
        forged = text.replace('"producer":"old/1.0"', '"producer":"forged/2.0"')
        loaded = parse_tracefile(forged)
        # v1 path: signature still verifies because the header was never
        # part of the signed message in v1.
        assert verify_tracefile(loaded).ok is True

    def test_downgrade_attack_fails(self):
        """Stripping sign_format to forge a v2 file must fail closed.

        An attacker who edits producer/actor/notes on a v2 file and
        downgrades sign_format to 1 (to dodge header binding) still
        fails: the signature was computed over the v2 message, but
        re-verifying as v1 produces a different message.
        """
        key = generate_keypair()
        tf = build_tracefile(
            _sample_frames(),
            key=key,
            session_id="s",
            producer="trusted/1.0",
        )
        text = trace_to_text(tf)
        # Downgrade the format tag AND forge the producer.
        downgraded = text.replace('"sign_format":2', '"sign_format":1')
        downgraded = downgraded.replace(
            '"producer":"trusted/1.0"', '"producer":"forged/9.9"'
        )
        loaded = parse_tracefile(downgraded)
        from bog_agents_cli.tracefile.spec import SIGN_FORMAT_V1

        assert loaded.header.sign_format == SIGN_FORMAT_V1
        result = verify_tracefile(loaded)
        assert result.ok is False
        assert result.signature_ok is False

    def test_parse_refuses_unknown_sign_format(self):
        key = generate_keypair()
        tf = build_tracefile(_sample_frames(), key=key, session_id="s")
        text = trace_to_text(tf).replace('"sign_format":2', '"sign_format":99')
        with pytest.raises(TraceFileError):
            parse_tracefile(text)

    def test_build_refuses_unknown_sign_format(self):
        key = generate_keypair()
        with pytest.raises(TraceFileError):
            build_tracefile(_sample_frames(), key=key, session_id="s", sign_format=7)

    def test_v2_roundtrip_through_disk(self, tmp_path: Path):
        key = generate_keypair()
        tf = build_tracefile(
            _sample_frames(),
            key=key,
            session_id="disk",
            producer="p",
            actor="a",
            notes=("n1", "n2"),
        )
        path = tmp_path / "v2.trace"
        write_tracefile(tf, path)
        loaded = read_tracefile(path)
        assert loaded.header.sign_format == 2
        assert loaded.header.producer == "p"
        assert loaded.header.notes == ("n1", "n2")
        assert verify_tracefile(loaded).ok is True


# Silence "imported but unused" without removing — these are part of
# the public surface tests assert against and the import keeps the
# module discoverable in IDEs.
_unused = (KeyMaterial, TraceHeader)
