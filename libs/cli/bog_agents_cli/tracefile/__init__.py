"""TraceFile v1 — open, signed, replay-able trace format (Wave S).

TraceFile is bog-agents' contribution to a shared agent-observability
substrate: every vendor records traces, none of those formats are
interchangeable, none capture rule fires + dream completions, and
none can be deterministically replayed across tools. TraceFile v1
addresses all three.

Each ``.trace`` file is a single JSONL document:

* **Header line** describing the format version, producer, session
  id, signing algorithm, and Merkle-chain configuration.
* **Frame lines** — one per causal event — each carrying its
  ``prev_hash`` (linking back to the previous frame) and a
  ``frame_hash`` over the canonical-JSON serialisation of the
  frame minus the hash itself. This Merkle-chain framing means
  *any* edit to *any* frame breaks every downstream hash.
* **Signature line** — an Ed25519 signature over the final
  ``frame_hash`` (the Merkle root). The signer's public key is
  embedded so a reader can verify without out-of-band material.

Why JSONL and not protobuf:

* JSONL is trivially editable, greppable, and survives 30 years.
* It streams — a producer never needs to buffer the whole trace.
* Other vendors (Claude Code, Codex, Cursor) emit JSON-shaped logs
  natively; mapping in is one ``map`` away.

Public surface
--------------

* :class:`TraceHeader` / :class:`TraceFrame` / :class:`TraceFile` —
  data model.
* :func:`build_tracefile`, :func:`write_tracefile` — produce a
  signed TraceFile from an iterable of frames.
* :func:`read_tracefile`, :func:`verify_tracefile` — read + verify.
* :func:`generate_keypair` / :func:`load_keypair_from_path` /
  :func:`save_keypair` — local Ed25519 key management.
"""

from __future__ import annotations

from bog_agents_cli.tracefile.signing import (
    KeyMaterial,
    SignatureVerificationError,
    SigningError,
    generate_keypair,
    load_keypair_from_path,
    save_keypair,
)
from bog_agents_cli.tracefile.spec import (
    TraceFile,
    TraceFileError,
    TraceFrame,
    TraceHeader,
    TraceVerification,
    build_tracefile,
    canonical_frame_hash,
    canonical_json,
    read_tracefile,
    verify_tracefile,
    write_tracefile,
)

__all__ = [
    "KeyMaterial",
    "SignatureVerificationError",
    "SigningError",
    "TraceFile",
    "TraceFileError",
    "TraceFrame",
    "TraceHeader",
    "TraceVerification",
    "build_tracefile",
    "canonical_frame_hash",
    "canonical_json",
    "generate_keypair",
    "load_keypair_from_path",
    "read_tracefile",
    "save_keypair",
    "verify_tracefile",
    "write_tracefile",
]
