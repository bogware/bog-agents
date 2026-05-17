"""TraceFile v1 — spec, serializer, reader, verifier (Wave S, S1).

A TraceFile is a JSONL document with three line kinds, in this order:

1. **header** — exactly one. Carries format version, producer,
   session id, timestamps, signing/hashing algorithms.
2. **frame** — one per causal event. Each frame's ``frame_hash`` is
   computed as ``blake2b(prev_hash || canonical_json(frame_no_hash))``,
   forming a content-addressed Merkle chain.
3. **signature** — exactly one. Carries the base64 public key, the
   final Merkle root (== last frame's hash), and an Ed25519
   signature over ``f"tracefile-v1\\n{root}\\n{session_id}\\n"``.

Why a fixed signed-message format
---------------------------------

A naive signature over "the whole file" leaves the message open to
length-extension and concatenation tricks. We sign a *short, framed*
header-string that includes the version tag, the root, and the
session id. The frames themselves are tamper-protected by the
Merkle chain — a single byte change anywhere in any frame
invalidates the next frame's hash, propagating to the root, which
breaks the signature.

Why blake2b for frame hashes
----------------------------

Faster than SHA-256, no length-extension concerns, in the stdlib
(``hashlib.blake2b``), 256-bit output. This is purely a fingerprint
— we're not using it for password storage or KDF, just
content-addressing.

Canonical JSON
--------------

The frame body is serialised with ``json.dumps(separators=(",", ":"),
sort_keys=True, ensure_ascii=False)``. That gives us a byte-stable
representation across runtimes that differ on whitespace + key order
defaults.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from bog_agents_cli.io_utils import atomic_write_text
from bog_agents_cli.tracefile.signing import (
    KeyMaterial,
    SignatureVerificationError,
    material_from_public_b64,
    sign,
    verify,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SPEC_VERSION = 1
"""Schema version embedded in every TraceFile. Bumped only on
incompatible changes; readers refuse files with a version they don't
recognise."""

_DEFAULT_PRODUCER = "bog-agents-cli/tracefile-v1"
_HASH_ALG = "blake2b-256"
_SIGN_ALG = "ed25519"
_ZERO_HASH = "0" * 64
"""Prepended to the first frame's hash so the chain has a uniform
shape — every frame_hash depends on exactly one prev_hash, never a
sentinel-empty value."""

_SIGNED_MESSAGE_PREFIX = "tracefile-v1\n"


class TraceFileError(RuntimeError):
    """Raised on shape/parse failures specific to TraceFile."""


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


class LineKind(StrEnum):
    HEADER = "header"
    FRAME = "frame"
    SIGNATURE = "signature"


@dataclass(frozen=True, slots=True)
class TraceHeader:
    """Header line in a TraceFile.

    Attributes:
        version: Spec version (always 1 today).
        producer: User-agent-style string identifying who wrote the
            file (``"bog-agents-cli/0.8.x"``,
            ``"claude-code-hook/1.2"``, etc.).
        produced_at: Wall-clock seconds when the file was written.
        session_id: Free-form identifier — usually the source
            causal session id. Embedded into the signed message so a
            signature can't be lifted onto a different session.
        actor: Optional label for the agent that produced the trace
            ("claude-haiku-4-5-20251001"). Carried through to the
            renderer.
        merkle_alg: Hash used for frame chaining. ``blake2b-256``
            today; readers refuse unknown values.
        sign_alg: Signature algorithm. ``ed25519`` today.
        notes: Free-form list of provenance breadcrumbs.
    """

    version: int = SPEC_VERSION
    producer: str = _DEFAULT_PRODUCER
    produced_at: float = 0.0
    session_id: str = ""
    actor: str = ""
    merkle_alg: str = _HASH_ALG
    sign_alg: str = _SIGN_ALG
    notes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TraceFrame:
    """One frame inside a TraceFile.

    The hash fields are populated by :func:`build_tracefile` — callers
    constructing frames programmatically usually leave them at the
    default sentinel values.

    Attributes:
        id: Monotonic integer id (matches the source CausalEvent id
            when produced from trace-mind).
        event_kind: Free-form event-kind string. Lowercase. Matches
            the :class:`bog_agents_cli.causal.EventKind` vocabulary
            when produced from trace-mind, but readers MUST tolerate
            arbitrary kinds — that's how third-party exporters
            integrate.
        actor: Short label naming who produced the event.
        summary: One-line human description.
        timestamp: Epoch seconds.
        parents: Direct causal-graph antecedents (other frame ids).
        payload: Free-form structured side data.
        prev_hash: Hash of the previous frame (or ``_ZERO_HASH`` for
            the first frame).
        frame_hash: Hash of *this* frame's canonical body, with
            ``prev_hash`` prepended.
    """

    id: int
    event_kind: str
    actor: str
    summary: str
    timestamp: float
    parents: tuple[int, ...] = ()
    payload: dict[str, Any] = field(default_factory=dict)
    prev_hash: str = _ZERO_HASH
    frame_hash: str = ""


@dataclass(frozen=True, slots=True)
class TraceFile:
    """Assembled TraceFile (in-memory representation)."""

    header: TraceHeader
    frames: tuple[TraceFrame, ...]
    signature_b64: str
    public_key_b64: str
    merkle_root: str

    @property
    def signed_message(self) -> bytes:
        """The exact bytes the signature was computed over."""
        return _signed_message(self.merkle_root, self.header.session_id)


@dataclass(frozen=True, slots=True)
class TraceVerification:
    """Outcome of :func:`verify_tracefile`."""

    ok: bool
    """True iff every check passed (chain unbroken AND signature OK)."""
    frames_checked: int
    chain_ok: bool
    signature_ok: bool
    fingerprint: str
    message: str
    """One-line summary suitable for log lines."""


# ---------------------------------------------------------------------------
# Canonicalisation + hashing
# ---------------------------------------------------------------------------


def canonical_json(value: Any) -> str:
    """Deterministic JSON serialisation used for hashing.

    Sorted keys, comma/colon separators with no spaces, unicode kept
    as unicode. The output is byte-stable across CPython versions.
    """
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def canonical_frame_hash(prev_hash: str, frame: TraceFrame) -> str:
    """Compute the frame's content-addressed hash.

    Hashes ``prev_hash`` followed by the canonical JSON of every
    frame field *except* the hash fields. Same algorithm on read +
    write, so a reader can independently recompute the chain.
    """
    body = {
        "id": frame.id,
        "event_kind": frame.event_kind,
        "actor": frame.actor,
        "summary": frame.summary,
        "timestamp": frame.timestamp,
        "parents": list(frame.parents),
        "payload": frame.payload,
    }
    text = prev_hash + canonical_json(body)
    return hashlib.blake2b(text.encode("utf-8"), digest_size=32).hexdigest()


def _signed_message(merkle_root: str, session_id: str) -> bytes:
    """The exact bytes covered by the Ed25519 signature.

    Including the spec version + session id prevents anyone from
    lifting a valid signature off one TraceFile onto another.
    """
    return f"{_SIGNED_MESSAGE_PREFIX}{merkle_root}\n{session_id}\n".encode()


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


def build_tracefile(
    frames: Iterable[TraceFrame | dict[str, Any]],
    *,
    key: KeyMaterial,
    session_id: str,
    producer: str = _DEFAULT_PRODUCER,
    actor: str = "",
    notes: Iterable[str] = (),
    produced_at: float | None = None,
) -> TraceFile:
    """Compute the Merkle chain + sign, returning an in-memory TraceFile.

    Args:
        frames: Iterable of frames (or dicts that look like frames).
            Frames may carry ``prev_hash`` / ``frame_hash`` already;
            those are overwritten. Frame ordering matters — the
            chain is computed in iteration order.
        key: Signing material with a private key.
        session_id: Source session id (also embedded in the signed
            message).
        producer: User-agent string.
        actor: Optional agent label.
        notes: Optional provenance breadcrumbs.
        produced_at: Override the wall-clock timestamp (tests use
            this to make headers byte-stable).
    """
    if not key.can_sign:
        msg = "build_tracefile requires a key with a private half."
        raise TraceFileError(msg)
    materialised: list[TraceFrame] = []
    prev = _ZERO_HASH
    for raw in frames:
        frame = _coerce_frame(raw)
        frame = TraceFrame(
            id=frame.id,
            event_kind=frame.event_kind,
            actor=frame.actor,
            summary=frame.summary,
            timestamp=frame.timestamp,
            parents=tuple(frame.parents),
            payload=dict(frame.payload),
            prev_hash=prev,
            frame_hash="",
        )
        digest = canonical_frame_hash(prev, frame)
        materialised.append(
            TraceFrame(
                id=frame.id,
                event_kind=frame.event_kind,
                actor=frame.actor,
                summary=frame.summary,
                timestamp=frame.timestamp,
                parents=frame.parents,
                payload=frame.payload,
                prev_hash=prev,
                frame_hash=digest,
            )
        )
        prev = digest

    if not materialised:
        msg = "build_tracefile requires at least one frame."
        raise TraceFileError(msg)
    merkle_root = materialised[-1].frame_hash
    signed = sign(key, _signed_message(merkle_root, session_id))
    header = TraceHeader(
        version=SPEC_VERSION,
        producer=producer,
        produced_at=produced_at if produced_at is not None else time.time(),
        session_id=session_id,
        actor=actor,
        merkle_alg=_HASH_ALG,
        sign_alg=_SIGN_ALG,
        notes=tuple(notes),
    )
    return TraceFile(
        header=header,
        frames=tuple(materialised),
        signature_b64=signed,
        public_key_b64=key.public_key_b64,
        merkle_root=merkle_root,
    )


def _coerce_frame(raw: TraceFrame | dict[str, Any]) -> TraceFrame:
    if isinstance(raw, TraceFrame):
        return raw
    if not isinstance(raw, dict):
        msg = f"Frame must be TraceFrame or dict, got {type(raw).__name__}."
        raise TraceFileError(msg)
    try:
        return TraceFrame(
            id=int(raw["id"]),
            event_kind=str(raw["event_kind"]),
            actor=str(raw.get("actor", "")),
            summary=str(raw.get("summary", "")),
            timestamp=float(raw.get("timestamp", 0.0)),
            parents=tuple(int(p) for p in raw.get("parents", ())),
            payload=dict(raw.get("payload") or {}),
        )
    except (KeyError, ValueError, TypeError) as exc:
        msg = f"Could not coerce dict to TraceFrame: {exc}"
        raise TraceFileError(msg) from exc


# ---------------------------------------------------------------------------
# Serializer
# ---------------------------------------------------------------------------


def write_tracefile(file: TraceFile, path: Path) -> Path:
    """Persist a TraceFile to *path* as JSONL. Returns the path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    body = _render(file)
    atomic_write_text(path, body, encoding="utf-8")
    return path


def _render(file: TraceFile) -> str:
    """Serialize a TraceFile to its on-disk text representation."""
    lines: list[str] = []
    header = {
        "kind": LineKind.HEADER.value,
        "version": file.header.version,
        "producer": file.header.producer,
        "produced_at": file.header.produced_at,
        "session_id": file.header.session_id,
        "actor": file.header.actor,
        "merkle_alg": file.header.merkle_alg,
        "sign_alg": file.header.sign_alg,
        "notes": list(file.header.notes),
    }
    lines.append(canonical_json(header))
    for frame in file.frames:
        lines.append(canonical_json(_frame_to_dict(frame)))
    sig = {
        "kind": LineKind.SIGNATURE.value,
        "alg": _SIGN_ALG,
        "public_key": file.public_key_b64,
        "merkle_root": file.merkle_root,
        "signature": file.signature_b64,
    }
    lines.append(canonical_json(sig))
    return "\n".join(lines) + "\n"


def _frame_to_dict(frame: TraceFrame) -> dict[str, Any]:
    return {
        "kind": LineKind.FRAME.value,
        "id": frame.id,
        "event_kind": frame.event_kind,
        "actor": frame.actor,
        "summary": frame.summary,
        "timestamp": frame.timestamp,
        "parents": list(frame.parents),
        "payload": frame.payload,
        "prev_hash": frame.prev_hash,
        "frame_hash": frame.frame_hash,
    }


# ---------------------------------------------------------------------------
# Reader
# ---------------------------------------------------------------------------


def read_tracefile(path: Path | str) -> TraceFile:
    """Parse a TraceFile from disk. Raises :class:`TraceFileError`
    on any structural issue; does *not* verify the chain or
    signature — call :func:`verify_tracefile` after.
    """
    target = Path(path)
    try:
        text = target.read_text(encoding="utf-8")
    except OSError as exc:
        msg = f"Could not read TraceFile {target}: {exc}"
        raise TraceFileError(msg) from exc
    return parse_tracefile(text)


def parse_tracefile(text: str) -> TraceFile:
    """Parse a TraceFile from an in-memory string."""
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) < 3:
        msg = "TraceFile must have at least a header, one frame, and a signature."
        raise TraceFileError(msg)
    header_line, *rest = lines
    if not rest:
        msg = "TraceFile is missing frame + signature lines."
        raise TraceFileError(msg)
    sig_line = rest[-1]
    frame_lines = rest[:-1]

    header_dict = _parse_object(header_line, "header")
    if header_dict.get("kind") != LineKind.HEADER.value:
        msg = f"First line must have kind=header, got {header_dict.get('kind')!r}."
        raise TraceFileError(msg)
    if int(header_dict.get("version", 0)) != SPEC_VERSION:
        msg = (
            f"TraceFile version {header_dict.get('version')} not supported "
            f"(this build understands v{SPEC_VERSION})."
        )
        raise TraceFileError(msg)
    if header_dict.get("merkle_alg") != _HASH_ALG:
        msg = (
            f"Unsupported Merkle algorithm {header_dict.get('merkle_alg')!r}; "
            f"expected {_HASH_ALG!r}."
        )
        raise TraceFileError(msg)
    if header_dict.get("sign_alg") != _SIGN_ALG:
        msg = (
            f"Unsupported signature algorithm {header_dict.get('sign_alg')!r}; "
            f"expected {_SIGN_ALG!r}."
        )
        raise TraceFileError(msg)

    header = TraceHeader(
        version=int(header_dict["version"]),
        producer=str(header_dict.get("producer", "")),
        produced_at=float(header_dict.get("produced_at", 0.0)),
        session_id=str(header_dict.get("session_id", "")),
        actor=str(header_dict.get("actor", "")),
        merkle_alg=str(header_dict["merkle_alg"]),
        sign_alg=str(header_dict["sign_alg"]),
        notes=tuple(header_dict.get("notes") or ()),
    )

    if not frame_lines:
        msg = "TraceFile has no frames."
        raise TraceFileError(msg)
    frames: list[TraceFrame] = []
    for idx, line in enumerate(frame_lines):
        data = _parse_object(line, f"frame[{idx}]")
        if data.get("kind") != LineKind.FRAME.value:
            msg = (
                f"Line {idx + 2} must have kind=frame, got {data.get('kind')!r}."
            )
            raise TraceFileError(msg)
        frames.append(_dict_to_frame(data))

    sig_dict = _parse_object(sig_line, "signature")
    if sig_dict.get("kind") != LineKind.SIGNATURE.value:
        msg = f"Last line must have kind=signature, got {sig_dict.get('kind')!r}."
        raise TraceFileError(msg)
    pub = str(sig_dict.get("public_key", ""))
    root = str(sig_dict.get("merkle_root", ""))
    sig_b64 = str(sig_dict.get("signature", ""))
    if not (pub and root and sig_b64):
        msg = "Signature line is missing public_key / merkle_root / signature."
        raise TraceFileError(msg)

    return TraceFile(
        header=header,
        frames=tuple(frames),
        signature_b64=sig_b64,
        public_key_b64=pub,
        merkle_root=root,
    )


def _parse_object(line: str, context: str) -> dict[str, Any]:
    try:
        value = json.loads(line)
    except json.JSONDecodeError as exc:
        msg = f"Could not parse {context}: {exc}"
        raise TraceFileError(msg) from exc
    if not isinstance(value, dict):
        msg = f"{context} must be a JSON object, got {type(value).__name__}."
        raise TraceFileError(msg)
    return value


def _dict_to_frame(data: dict[str, Any]) -> TraceFrame:
    try:
        return TraceFrame(
            id=int(data["id"]),
            event_kind=str(data["event_kind"]),
            actor=str(data.get("actor", "")),
            summary=str(data.get("summary", "")),
            timestamp=float(data.get("timestamp", 0.0)),
            parents=tuple(int(p) for p in data.get("parents", ())),
            payload=dict(data.get("payload") or {}),
            prev_hash=str(data.get("prev_hash", _ZERO_HASH)),
            frame_hash=str(data.get("frame_hash", "")),
        )
    except (KeyError, ValueError, TypeError) as exc:
        msg = f"Malformed frame: {exc}"
        raise TraceFileError(msg) from exc


# ---------------------------------------------------------------------------
# Verifier
# ---------------------------------------------------------------------------


def verify_tracefile(
    file: TraceFile, *, key: KeyMaterial | None = None
) -> TraceVerification:
    """Verify a TraceFile's Merkle chain and Ed25519 signature.

    Args:
        file: The in-memory TraceFile.
        key: Optional verify-only material whose public key MUST
            match the file's recorded public key. When ``None``,
            the file's public key is used directly (trust on first
            use). Callers managing a keystore should pass the
            expected key explicitly so a swap doesn't go
            unnoticed.

    Returns:
        :class:`TraceVerification` summarising the result.
    """
    if not file.frames:
        return TraceVerification(
            ok=False,
            frames_checked=0,
            chain_ok=False,
            signature_ok=False,
            fingerprint="",
            message="No frames in TraceFile.",
        )

    # 1. Re-derive every frame_hash, walking prev_hash forward.
    chain_ok = True
    expected_prev = _ZERO_HASH
    for idx, frame in enumerate(file.frames):
        if frame.prev_hash != expected_prev:
            chain_ok = False
            break
        recomputed = canonical_frame_hash(expected_prev, frame)
        if recomputed != frame.frame_hash:
            chain_ok = False
            break
        expected_prev = frame.frame_hash
    # 2. Recorded merkle_root must match the last frame's hash.
    if chain_ok and file.merkle_root != file.frames[-1].frame_hash:
        chain_ok = False

    # 3. Resolve the verify key and check the signature.
    verifier_material = key or material_from_public_b64(file.public_key_b64)
    if key is not None and key.public_key_b64 != file.public_key_b64:
        return TraceVerification(
            ok=False,
            frames_checked=len(file.frames),
            chain_ok=chain_ok,
            signature_ok=False,
            fingerprint=verifier_material.fingerprint,
            message="Provided key does not match the TraceFile's public key.",
        )
    signature_ok = False
    sig_message = "signature did not verify"
    if chain_ok:
        try:
            verify(
                verifier_material,
                _signed_message(file.merkle_root, file.header.session_id),
                file.signature_b64,
            )
        except SignatureVerificationError as exc:
            sig_message = str(exc)
        else:
            signature_ok = True
            sig_message = "signature valid"
    overall = chain_ok and signature_ok
    if not chain_ok:
        message = "Merkle chain broken (a frame was edited or reordered)."
    else:
        message = sig_message
    return TraceVerification(
        ok=overall,
        frames_checked=len(file.frames),
        chain_ok=chain_ok,
        signature_ok=signature_ok,
        fingerprint=verifier_material.fingerprint,
        message=message,
    )


__all__ = [
    "SPEC_VERSION",
    "LineKind",
    "TraceFile",
    "TraceFileError",
    "TraceFrame",
    "TraceHeader",
    "TraceVerification",
    "build_tracefile",
    "canonical_frame_hash",
    "canonical_json",
    "parse_tracefile",
    "read_tracefile",
    "verify_tracefile",
    "write_tracefile",
]
