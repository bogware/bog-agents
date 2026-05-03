"""Session-only secret vault.

The vault holds secrets needed during a single CLI session — API tokens, test
user passwords, fixture credentials — and lives entirely in process memory.
Nothing is written to disk, no environment variable is exported, and on
process exit the dict is dropped.

Two read paths are supported:

1. **Direct injection.** Other code (e.g. the Vars resolver) calls
   :meth:`SessionVault.put` after prompting the user.
2. **Optional OS keychain bridge.** When ``allow_keyring=True`` the vault
   will *read* (never write) from the host's OS keychain via the ``keyring``
   library. This is the dev-friendly path: stash a value once with
   ``keyring set bog-agents <alias>`` and reference it as a secret variable
   later. Failures (no backend, missing key, locked keychain) fall back to
   prompting.

A :class:`SecretStr` wrapper is used so accidentally formatting a vault
value in a log line shows ``***`` rather than the cleartext.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


class SecretStr:
    """Wrapper around a sensitive string that redacts itself in repr/str.

    The underlying value is accessible via :meth:`get_secret_value`. ``str``
    and ``repr`` always render ``***`` so a stray ``f"{token}"`` in a log
    line cannot leak the secret.
    """

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        if not isinstance(value, str):
            msg = f"SecretStr requires str, got {type(value).__name__}"
            raise TypeError(msg)
        self._value = value

    def get_secret_value(self) -> str:
        """Return the cleartext value. Use sparingly."""
        return self._value

    def __repr__(self) -> str:
        return "SecretStr('***')"

    def __str__(self) -> str:
        return "***"

    def __eq__(self, other: object) -> bool:
        # Constant-time compare to avoid timing oracles when comparing tokens.
        if isinstance(other, SecretStr):
            return _consteq(self._value, other._value)
        if isinstance(other, str):
            return _consteq(self._value, other)
        return NotImplemented

    # Disable hashing entirely. Using a SecretStr as a dict key or set
    # member would expose the value via hash-bucket / collision oracles
    # in the holding container; in practice the codebase never needs this,
    # so we forbid it. Callers who need to *test* membership against a
    # vault use ``SessionVault.has(name)`` against the var name, never the
    # value.
    __hash__ = None  # type: ignore[assignment]

    def __bool__(self) -> bool:
        return bool(self._value)

    def __len__(self) -> int:
        return len(self._value)


def _consteq(a: str, b: str) -> bool:
    """Constant-time string comparison."""
    if len(a) != len(b):
        return False
    diff = 0
    for x, y in zip(a, b, strict=True):
        diff |= ord(x) ^ ord(y)
    return diff == 0


@dataclass
class SessionVault:
    """In-memory secret store, live for the duration of the process only.

    Attributes:
        allow_keyring: When True, :meth:`get` will also try the OS keychain
            via the ``keyring`` library (read-only) for keys not yet cached
            in memory. Default: False — keep behaviour explicit.
        keyring_service: Service name used when reading from the OS
            keychain. Defaults to ``"bog-agents"``.
    """

    allow_keyring: bool = False
    keyring_service: str = "bog-agents"
    _store: dict[str, SecretStr] = field(default_factory=dict, init=False, repr=False)

    def put(self, key: str, value: str | SecretStr) -> None:
        """Store a secret. Overwrites any existing value with the same key.

        Args:
            key: Non-empty identifier (the var name).
            value: Cleartext string or pre-wrapped ``SecretStr``.

        Raises:
            ValueError: If ``key`` is empty.
        """
        if not key:
            msg = "vault key must be a non-empty string"
            raise ValueError(msg)
        if isinstance(value, SecretStr):
            self._store[key] = value
        else:
            self._store[key] = SecretStr(value)

    def get(self, key: str) -> SecretStr | None:
        """Return the secret for ``key``, or None if not present.

        Tries in order:
        1. In-memory store.
        2. OS keychain (if ``allow_keyring=True``).

        Args:
            key: Variable name.

        Returns:
            ``SecretStr`` or ``None``.
        """
        cached = self._store.get(key)
        if cached is not None:
            return cached
        if self.allow_keyring:
            cleartext = self._read_keyring(key)
            if cleartext is not None:
                wrapped = SecretStr(cleartext)
                # Cache so we don't hit the keychain repeatedly during a run.
                self._store[key] = wrapped
                return wrapped
        return None

    def has(self, key: str) -> bool:
        """Return True if ``key`` is currently in the in-memory store.

        Note this does NOT consult the OS keychain — call :meth:`get` for
        that. ``has`` is for code that wants to know whether a value was
        already prompted-and-stored this session.
        """
        return key in self._store

    def keys(self) -> list[str]:
        """Return a snapshot of currently-stored keys (no values)."""
        return list(self._store.keys())

    def clear(self) -> None:
        """Drop all in-memory secrets. Idempotent."""
        self._store.clear()

    def _read_keyring(self, key: str) -> str | None:
        """Read ``key`` from the OS keychain, returning None on any failure.

        Failures (no backend installed, key absent, keychain locked) are
        deliberately swallowed — the caller will fall back to prompting.
        """
        try:
            import keyring  # type: ignore[import-untyped]
        except ImportError:
            logger.debug("vault: keyring not installed, skipping OS keychain lookup")
            return None
        try:
            value = keyring.get_password(self.keyring_service, key)
        except Exception as exc:
            logger.debug("vault: keyring lookup for %r failed: %s", key, exc)
            return None
        return value

    def render(self, value: Any) -> Any:
        """Recursively replace any ``SecretStr`` inside ``value`` with cleartext.

        This is the **only** place in the codebase that should turn a
        ``SecretStr`` back into a plain string. Use it when handing values
        to a tool/process boundary (e.g. an HTTP header or subprocess env)
        and never when logging.
        """
        if isinstance(value, SecretStr):
            return value.get_secret_value()
        if isinstance(value, dict):
            return {k: self.render(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.render(v) for v in value]
        if isinstance(value, tuple):
            return tuple(self.render(v) for v in value)
        return value


# A module-level singleton is useful so middleware/widgets that don't want to
# thread a vault reference everywhere can grab one. Tests construct fresh
# instances; production code uses get_default_vault().
_default_vault: SessionVault | None = None


def get_default_vault() -> SessionVault:
    """Return the process-wide default ``SessionVault`` (lazy-init)."""
    global _default_vault
    if _default_vault is None:
        _default_vault = SessionVault()
    return _default_vault


def reset_default_vault() -> None:
    """Drop and replace the process-wide default vault. Test helper."""
    global _default_vault
    if _default_vault is not None:
        _default_vault.clear()
    _default_vault = None
