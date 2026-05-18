"""Ed25519 key management for TraceFile v1.

We use the ``cryptography`` package's primitives (already a hard
dependency for the CLI's TLS surface) rather than reimplementing the
algorithm. Keys live on disk as base64-encoded blobs with explicit
version headers so we can rotate the format later without dropping
existing files on the floor.
"""

from __future__ import annotations

import base64
import logging
import secrets
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from bog_agents_cli.io_utils import atomic_write_text

logger = logging.getLogger(__name__)


class SigningError(RuntimeError):
    """Raised when a signing operation fails."""


class SignatureVerificationError(RuntimeError):
    """Raised when a signature is present but does not verify.

    Distinct from :class:`SigningError`: the former is a producer
    problem, the latter is a tampered or untrusted artefact.
    """


class UnsupportedAlgorithmError(SigningError):
    """Raised when a TraceFile names a signing algorithm we don't support."""


@dataclass(frozen=True, slots=True)
class KeyMaterial:
    """One Ed25519 keypair.

    Attributes:
        private_key: The private key. ``None`` for verify-only
            instances (e.g. a reader that only has the public key).
        public_key: The public key. Always present.
        public_key_b64: Cached base64-encoded public key — embedded
            in TraceFile signature lines so readers can verify
            without a separate keystore.
        fingerprint: Short, stable identifier derived from the public
            key. Used in log messages + reports to disambiguate
            multiple signers in a multi-tenant deployment.
    """

    private_key: Ed25519PrivateKey | None
    public_key: Ed25519PublicKey
    public_key_b64: str
    fingerprint: str

    @property
    def can_sign(self) -> bool:
        return self.private_key is not None


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------


def generate_keypair() -> KeyMaterial:
    """Mint a fresh Ed25519 keypair."""
    sk = Ed25519PrivateKey.generate()
    pk = sk.public_key()
    pk_b64 = _public_b64(pk)
    return KeyMaterial(
        private_key=sk,
        public_key=pk,
        public_key_b64=pk_b64,
        fingerprint=_fingerprint(pk_b64),
    )


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


_KEY_FILE_HEADER = "# bog-agents TraceFile signing key v1"
_PRIVATE_PREFIX = "private_b64:"
_PUBLIC_PREFIX = "public_b64:"


def save_keypair(material: KeyMaterial, path: Path) -> Path:
    """Persist a keypair to *path* (creates parent dirs as needed).

    The file mode is restricted to owner-only on POSIX + Windows via
    :func:`bog_agents_cli.vars_store._secure_owner_only`.
    """
    if material.private_key is None:
        msg = "save_keypair requires a private key (got verify-only material)."
        raise SigningError(msg)
    body_lines = [_KEY_FILE_HEADER]
    private_b64 = base64.urlsafe_b64encode(
        material.private_key.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )
    ).decode("ascii")
    body_lines.append(f"{_PRIVATE_PREFIX}{private_b64}")
    body_lines.append(f"{_PUBLIC_PREFIX}{material.public_key_b64}")
    body = "\n".join(body_lines) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, body, encoding="utf-8")
    try:
        from bog_agents_cli.vars_store import _secure_owner_only

        _secure_owner_only(path)
    except Exception:
        logger.debug("tracefile: _secure_owner_only failed", exc_info=True)
    return path


def load_keypair_from_path(path: Path) -> KeyMaterial:
    """Load an Ed25519 keypair previously saved via :func:`save_keypair`."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        msg = f"Could not read key file {path}: {exc}"
        raise SigningError(msg) from exc
    private_b64: str | None = None
    public_b64: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(_PRIVATE_PREFIX):
            private_b64 = stripped[len(_PRIVATE_PREFIX) :]
        elif stripped.startswith(_PUBLIC_PREFIX):
            public_b64 = stripped[len(_PUBLIC_PREFIX) :]
    if not private_b64 or not public_b64:
        msg = f"Key file {path} is malformed (missing private or public line)."
        raise SigningError(msg)
    try:
        sk = Ed25519PrivateKey.from_private_bytes(base64.urlsafe_b64decode(private_b64))
        pk = Ed25519PublicKey.from_public_bytes(base64.urlsafe_b64decode(public_b64))
    except (ValueError, TypeError) as exc:
        msg = f"Key file {path} contains malformed key material: {exc}"
        raise SigningError(msg) from exc
    # Sanity: derived public key from private must match the recorded one.
    derived = _public_b64(sk.public_key())
    if derived != public_b64:
        msg = f"Key file {path} is inconsistent (public != derived from private)."
        raise SigningError(msg)
    # U4: byte-equality on the public key catches obvious copy-paste
    # corruption, but a key that *parsed* but is otherwise damaged
    # could still fail at sign-time. Round-trip a random nonce so the
    # caller gets a clean error here rather than at the first export.
    nonce = secrets.token_bytes(32)
    try:
        signature = sk.sign(nonce)
        pk.verify(signature, nonce)
    except (InvalidSignature, ValueError) as exc:
        msg = (
            f"Key file {path} is structurally valid but failed a "
            f"sign+verify self-check: {exc}"
        )
        raise SigningError(msg) from exc
    return KeyMaterial(
        private_key=sk,
        public_key=pk,
        public_key_b64=public_b64,
        fingerprint=_fingerprint(public_b64),
    )


def material_from_public_b64(public_b64: str) -> KeyMaterial:
    """Build verify-only :class:`KeyMaterial` from a base64 public key."""
    try:
        pk = Ed25519PublicKey.from_public_bytes(base64.urlsafe_b64decode(public_b64))
    except (ValueError, TypeError) as exc:
        msg = f"Invalid public key blob: {exc}"
        raise SigningError(msg) from exc
    return KeyMaterial(
        private_key=None,
        public_key=pk,
        public_key_b64=public_b64,
        fingerprint=_fingerprint(public_b64),
    )


# ---------------------------------------------------------------------------
# Sign / verify
# ---------------------------------------------------------------------------


def sign(material: KeyMaterial, message: bytes) -> str:
    """Return a base64-encoded Ed25519 signature over *message*."""
    if material.private_key is None:
        msg = "Verify-only material can't sign."
        raise SigningError(msg)
    raw = material.private_key.sign(message)
    return base64.urlsafe_b64encode(raw).decode("ascii")


def verify(material: KeyMaterial, message: bytes, signature_b64: str) -> bool:
    """Verify *signature_b64* over *message*.

    Returns:
        True on valid signature.

    Raises:
        SignatureVerificationError: When the signature is invalid or
            the blob is malformed. Callers should treat the raise as
            a hard failure — not a soft warning.
    """
    try:
        raw = base64.urlsafe_b64decode(signature_b64)
    except (ValueError, TypeError) as exc:
        msg = f"Signature is not valid base64: {exc}"
        raise SignatureVerificationError(msg) from exc
    try:
        material.public_key.verify(raw, message)
    except InvalidSignature as exc:
        msg = "Signature did not verify against the recorded public key."
        raise SignatureVerificationError(msg) from exc
    return True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _public_b64(pk: Ed25519PublicKey) -> str:
    return base64.urlsafe_b64encode(
        pk.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
    ).decode("ascii")


def _fingerprint(public_b64: str) -> str:
    """Short fingerprint — first 8 chars of the base64 public key."""
    return public_b64[:8]


__all__ = [
    "KeyMaterial",
    "SignatureVerificationError",
    "SigningError",
    "UnsupportedAlgorithmError",
    "generate_keypair",
    "load_keypair_from_path",
    "material_from_public_b64",
    "save_keypair",
    "sign",
    "verify",
]
