"""``/trace`` slash-command controller (Wave S, S2 + S3).

Three subcommands plus an alias:

* ``/tracefile export <session-id|latest> [--out <path>]`` — read one
  causal session, build a signed TraceFile, write it to disk.
* ``/tracefile import <path>`` — read a TraceFile, verify chain +
  signature, render via the trace-mind viewer.
* ``/tracefile verify <path>`` — verify-only; no rendering.
* ``/tracefile keygen [--out <path>]`` — mint + persist an Ed25519
  signing keypair. Default location is
  ``~/.bog-agents/.tracefile-key``.
* ``/tracefile help`` — usage.

Key management
--------------

By default, signing uses a per-user keypair under
``~/.bog-agents/.tracefile-key``. The file is created on first
export. Callers wanting a stable cross-machine key set
``BOG_AGENTS_TRACEFILE_KEY=/path/to/key`` in the environment, or
pass ``--key <path>`` to the slash command.

The verifier reads the public key embedded in the TraceFile itself,
so import works without any local key material.
"""

from __future__ import annotations

import logging
import os
import shlex
import time
from pathlib import Path

from bog_agents_cli.causal.ledger import (
    CausalEvent,
    EventKind,
    list_sessions,
    load_session,
)
from bog_agents_cli.causal.render import render_graph
from bog_agents_cli.tracefile.signing import (
    KeyMaterial,
    SigningError,
    generate_keypair,
    load_keypair_from_path,
    save_keypair,
)
from bog_agents_cli.tracefile.spec import (
    TraceFile,
    TraceFileError,
    TraceFrame,
    build_tracefile,
    read_tracefile,
    verify_tracefile,
)

logger = logging.getLogger(__name__)


_DEFAULT_KEY_FILENAME = ".tracefile-key"
_DEFAULT_OUTPUT_SUBDIR = ".bog-agents/tracefiles"


def dispatch(command_text: str, working_dir: Path | str) -> str:
    """Top-level ``/tracefile …`` (also accepts ``/trace`` legacy)."""
    text = command_text.strip()
    for prefix in ("/tracefile", "/trace"):
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
            break
    if not text or text.lower() in ("help", "?"):
        return _help_text()
    try:
        # ``posix=False`` keeps backslashes verbatim, matching the
        # PowerShell convention on Windows where paths use them as
        # separators. We do still respect quoted strings.
        tokens = shlex.split(text, posix=False)
        # Strip surrounding quotes that ``posix=False`` preserves.
        tokens = [
            tok[1:-1] if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in ("'", '"') else tok
            for tok in tokens
        ]
    except ValueError as exc:
        return f"Could not parse /trace arguments: {exc}"
    head = tokens[0].lower()
    rest = tokens[1:]
    wdir = Path(working_dir)
    if head == "export":
        return _export(rest, wdir)
    if head == "import":
        return _import(rest, wdir)
    if head == "verify":
        return _verify(rest)
    if head == "keygen":
        return _keygen(rest)
    return f"Unknown /tracefile subcommand: {head!r}. Try /tracefile help."


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def _export(args: list[str], working_dir: Path) -> str:
    if not args:
        return "Usage: /tracefile export <session-id|latest> [--out PATH] [--key PATH]"
    session_id = args[0]
    out_path: Path | None = None
    key_path: Path | None = None
    i = 1
    while i < len(args):
        flag = args[i]
        if flag == "--out":
            if i + 1 >= len(args):
                return "Missing value after --out."
            out_path = Path(args[i + 1])
            i += 2
            continue
        if flag == "--key":
            if i + 1 >= len(args):
                return "Missing value after --key."
            key_path = Path(args[i + 1])
            i += 2
            continue
        return f"Unrecognised /tracefile export flag: {flag!r}"

    resolved_id = session_id
    if session_id == "latest":
        sessions = list_sessions(working_dir)
        if not sessions:
            return (
                "No causal sessions to export. Run /causal on and a turn first."
            )
        resolved_id = sessions[0]
    events = load_session(working_dir, resolved_id)
    if not events:
        return f"Session {resolved_id} has no recorded events."

    try:
        key = _resolve_key(key_path)
    except SigningError as exc:
        return f"Key error: {exc}"

    frames = [_event_to_frame(e) for e in events]
    try:
        file = build_tracefile(
            frames,
            key=key,
            session_id=resolved_id,
            actor=_summarise_actor(events),
            notes=("source=bog-agents-cli/causal-ledger",),
        )
    except TraceFileError as exc:
        return f"Could not build TraceFile: {exc}"

    target = _resolve_output_path(out_path, working_dir, resolved_id)
    try:
        from bog_agents_cli.tracefile.spec import write_tracefile

        write_tracefile(file, target)
    except OSError as exc:
        return f"Could not write {target}: {exc}"
    return (
        f"== TraceFile exported ==\n"
        f"  Session:     {resolved_id}\n"
        f"  Frames:      {len(file.frames)}\n"
        f"  Merkle root: {file.merkle_root}\n"
        f"  Signer:      {key.fingerprint}\n"
        f"  Path:        {target}\n"
    )


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------


def _import(args: list[str], working_dir: Path) -> str:
    if not args:
        return "Usage: /tracefile import <path>"
    path = Path(args[0])
    if not path.is_absolute():
        path = (working_dir / path).resolve()
    if not path.is_file():
        return f"TraceFile not found: {path}"
    try:
        tf = read_tracefile(path)
    except TraceFileError as exc:
        return f"Could not parse TraceFile: {exc}"
    verification = verify_tracefile(tf)
    if not verification.ok:
        return (
            f"/tracefile import: refused unverified TraceFile.\n"
            f"  reason: {verification.message}\n"
            f"  frames_checked: {verification.frames_checked}\n"
            f"  signer: {verification.fingerprint}"
        )
    # Render via a lightweight view — we don't drop frames into the
    # active causal ledger so a malicious TraceFile can't poison it.
    return _render_imported(tf, verification)


def _render_imported(tf: TraceFile, verification) -> str:  # noqa: ANN001
    lines = [
        "== TraceFile (verified) ==",
        f"  Producer:    {tf.header.producer}",
        f"  Session:     {tf.header.session_id}",
        f"  Actor:       {tf.header.actor or '<unknown>'}",
        f"  Frames:      {verification.frames_checked}",
        f"  Merkle root: {tf.merkle_root}",
        f"  Signer:      {verification.fingerprint}",
        "",
    ]
    if tf.header.notes:
        lines.append("Notes:")
        for note in tf.header.notes:
            lines.append(f"  · {note}")
        lines.append("")
    # Re-render through the existing causal-graph renderer so the
    # output is visually consistent with /causal graph.
    import tempfile

    from bog_agents_cli.causal.ledger import CausalLedger

    with tempfile.TemporaryDirectory() as tmp:
        synthetic = CausalLedger(
            working_dir=Path(tmp),
            session_id=f"imported-{tf.header.session_id}",
            existing_events=[_frame_to_event(f) for f in tf.frames],
        )
        synthetic.close()
        lines.append(render_graph(synthetic, limit=80))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Verify (no render)
# ---------------------------------------------------------------------------


def _verify(args: list[str]) -> str:
    if not args:
        return "Usage: /tracefile verify <path>"
    path = Path(args[0])
    if not path.is_file():
        return f"TraceFile not found: {path}"
    try:
        tf = read_tracefile(path)
    except TraceFileError as exc:
        return f"Could not parse TraceFile: {exc}"
    verification = verify_tracefile(tf)
    icon = "✓" if verification.ok else "✗"
    return (
        f"{icon} TraceFile {path.name}\n"
        f"  chain_ok:     {verification.chain_ok}\n"
        f"  signature_ok: {verification.signature_ok}\n"
        f"  frames:       {verification.frames_checked}\n"
        f"  signer:       {verification.fingerprint}\n"
        f"  message:      {verification.message}\n"
    )


# ---------------------------------------------------------------------------
# Keygen
# ---------------------------------------------------------------------------


def _keygen(args: list[str]) -> str:
    out_path: Path | None = None
    i = 0
    while i < len(args):
        if args[i] == "--out":
            if i + 1 >= len(args):
                return "Missing value after --out."
            out_path = Path(args[i + 1])
            i += 2
            continue
        return f"Unrecognised /tracefile keygen flag: {args[i]!r}"
    target = out_path or _default_key_path()
    if target.exists():
        return (
            f"Refusing to overwrite existing key file: {target}\n"
            "Delete it first if you really want to rotate."
        )
    material = generate_keypair()
    try:
        save_keypair(material, target)
    except SigningError as exc:
        return f"Could not save keypair: {exc}"
    return (
        f"Generated Ed25519 keypair.\n"
        f"  Path:        {target}\n"
        f"  Fingerprint: {material.fingerprint}\n"
        f"  Public key:  {material.public_key_b64}\n"
        "Add to BOG_AGENTS_TRACEFILE_KEY env var to override the default path."
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_key(explicit: Path | None) -> KeyMaterial:
    """Find a private key — explicit > env > default — minting if needed."""
    if explicit is not None:
        return load_keypair_from_path(explicit)
    env_path = (os.environ.get("BOG_AGENTS_TRACEFILE_KEY") or "").strip()
    if env_path:
        return load_keypair_from_path(Path(env_path))
    default = _default_key_path()
    if default.exists():
        return load_keypair_from_path(default)
    # First-export-on-this-machine path: mint + persist a fresh key
    # and tell the user where it landed via the export's stdout
    # (the message itself comes from the caller).
    material = generate_keypair()
    save_keypair(material, default)
    return material


def _default_key_path() -> Path:
    return Path.home() / ".bog-agents" / _DEFAULT_KEY_FILENAME


def _resolve_output_path(
    out_path: Path | None, working_dir: Path, session_id: str
) -> Path:
    if out_path is not None:
        return out_path
    target_dir = working_dir / _DEFAULT_OUTPUT_SUBDIR
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    return target_dir / f"{stamp}-{session_id}.trace"


def _event_to_frame(event: CausalEvent) -> TraceFrame:
    return TraceFrame(
        id=event.id,
        event_kind=event.kind.value,
        actor=event.actor,
        summary=event.summary,
        timestamp=event.timestamp,
        parents=event.parent_ids,
        payload=dict(event.payload),
    )


def _frame_to_event(frame: TraceFrame) -> CausalEvent:
    """Convert a TraceFrame back to a CausalEvent for the renderer."""
    try:
        kind = EventKind(frame.event_kind)
    except ValueError:
        kind = EventKind.NOTE
    return CausalEvent(
        id=frame.id,
        kind=kind,
        timestamp=frame.timestamp,
        actor=frame.actor,
        summary=frame.summary,
        parent_ids=frame.parents,
        payload=frame.payload,
    )


def _summarise_actor(events: list[CausalEvent]) -> str:
    """Pick a representative actor for the header (most-frequent model_call)."""
    counts: dict[str, int] = {}
    for e in events:
        if e.kind == EventKind.MODEL_CALL:
            counts[e.actor] = counts.get(e.actor, 0) + 1
    if not counts:
        return ""
    return max(counts.items(), key=lambda kv: kv[1])[0]


def _help_text() -> str:
    return (
        "/tracefile — TraceFile v1 export / import / verify.\n\n"
        "Usage:\n"
        "  /tracefile export <session-id|latest> [--out PATH] [--key PATH]\n"
        "                                  — sign and write a TraceFile\n"
        "  /tracefile import <path>            — verify + render a TraceFile\n"
        "  /tracefile verify <path>            — verify only; no render\n"
        "  /tracefile keygen [--out PATH]      — mint a new Ed25519 keypair\n"
        "  /tracefile help                     — this message\n\n"
        "Signing key (default ~/.bog-agents/.tracefile-key) is created\n"
        "on first export. Override via the BOG_AGENTS_TRACEFILE_KEY env\n"
        "var or per-call --key flag.\n"
    )


__all__ = ["dispatch"]
