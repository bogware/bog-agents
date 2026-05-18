"""Secure variable store for bog-agents-cli.

Stores named secrets and configuration values (API keys, URLs, tokens, etc.)
that can be referenced in prompts, pipelines, and skills using the syntax::

    {{vars.MY_VAR}}

Storage backends (tried in order):

1. **OS keyring** (preferred) — uses the platform's native secret store:
   macOS Keychain, Windows Credential Manager, Linux SecretService/libsecret.
   Values are encrypted by the OS.  Requires the ``keyring`` package.

2. **TOML fallback** — ``~/.bog-agents/vars.toml``. Values are stored in
   plaintext inside the file. On POSIX the file is ``chmod 0600``; on
   Windows we invoke ``icacls`` to remove inherited ACLs and grant only
   the current user. When ``icacls`` isn't available the file falls back
   to whatever Windows defaults to and the user gets a loud warning.
   A one-time warning is shown when this path is taken regardless.

Variable names are case-sensitive, alphanumeric + underscore only.

Usage::

    from bog_agents_cli.vars_store import get_var, set_var, delete_var, list_var_names, resolve_vars

    set_var("JIRA_API_KEY", "mysecret")
    key = get_var("JIRA_API_KEY")
    text = resolve_vars("See {{vars.JIRA_URL}}/browse/{{vars.PROJECT}}")
"""

from __future__ import annotations

import logging
import os
import re
import subprocess  # noqa: S404 — icacls invocation on Windows; no user-controlled args
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_SERVICE_NAME = "bog-agents"
_VARS_FILENAME = "vars.toml"
_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_VAR_REF_RE = re.compile(r"\{\{vars\.([A-Za-z_][A-Za-z0-9_]*)\}\}")

_DEFAULT_CONFIG_DIR = Path.home() / ".bog-agents"
_VARS_PATH = _DEFAULT_CONFIG_DIR / _VARS_FILENAME

_warned_fallback = False


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _validate_name(name: str) -> None:
    """Raise ValueError if *name* is not a valid variable name.

    Args:
        name: Variable name to validate.

    Raises:
        ValueError: When the name contains invalid characters.
    """
    if not _NAME_RE.match(name):
        msg = (
            f"Invalid variable name {name!r}. "
            "Names must start with a letter or underscore and contain only "
            "letters, digits, and underscores."
        )
        raise ValueError(msg)


# ---------------------------------------------------------------------------
# Keyring backend (preferred)
# ---------------------------------------------------------------------------


def _keyring_available() -> bool:
    """Return True when the ``keyring`` package and a usable backend exist."""
    try:
        import keyring

        backend = keyring.get_keyring()
        # keyring.backends.fail.Keyring signals no usable backend
        return "fail" not in type(backend).__module__.lower()
    except (ImportError, Exception):
        return False


def _keyring_set(name: str, value: str) -> bool:
    """Store *value* in the OS keyring under *name*.

    Returns:
        True on success, False when keyring is unavailable.
    """
    try:
        import keyring

        keyring.set_password(_SERVICE_NAME, name, value)
        return True
    except Exception as exc:
        logger.debug("keyring set failed for %r: %s", name, exc)
        return False


def _keyring_get(name: str) -> str | None:
    """Retrieve the value for *name* from the OS keyring.

    Returns:
        The stored value, or None if not found or keyring unavailable.
    """
    try:
        import keyring

        return keyring.get_password(_SERVICE_NAME, name)
    except Exception as exc:
        logger.debug("keyring get failed for %r: %s", name, exc)
        return None


def _keyring_delete(name: str) -> bool:
    """Remove *name* from the OS keyring.

    Returns:
        True if deleted, False if not found or unavailable.
    """
    try:
        import keyring

        keyring.delete_password(_SERVICE_NAME, name)
        return True
    except Exception as exc:
        logger.debug("keyring delete failed for %r: %s", name, exc)
        return False


def _keyring_list() -> list[str]:
    """Return all variable names stored in the keyring.

    Note: The generic keyring API has no ``list`` method.  We fall back to
    reading the TOML index (which tracks names without values) for enumeration.

    Returns:
        List of variable names known to the keyring-backed store.
    """
    data = _load_toml()
    return list(data.get("vars", {}).keys())


# ---------------------------------------------------------------------------
# TOML fallback backend
# ---------------------------------------------------------------------------


def _ensure_config_dir() -> None:
    """Create the config directory with restrictive permissions."""
    _DEFAULT_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    _secure_owner_only(_DEFAULT_CONFIG_DIR, is_dir=True)


# ---------------------------------------------------------------------------
# Cross-platform "owner-only" file/directory permissions
# ---------------------------------------------------------------------------
#
# ``Path.chmod(0o600)`` is a silent no-op on Windows — the file ends up
# readable by every member of the local ``Users`` group. The vars-store
# docstring used to promise mode 0600 unconditionally; on Windows that was
# a lie. We now invoke ``icacls`` to grant only the current user when on
# Windows (which is a real ACL change). The function reports its success /
# failure so :func:`is_using_toml_fallback` can warn loudly when neither
# protection is available. Fixes P0-E in REVIEW.md.


def _secure_owner_only(path: Path, *, is_dir: bool = False) -> bool:
    """Restrict *path* to owner-only access.

    On POSIX this is ``chmod 0600`` (file) / ``0700`` (dir). On Windows we
    invoke ``icacls`` to grant only the current SID and remove inherited
    ACLs — the only realistic way to mirror POSIX 0600 without a native
    extension. If ``icacls`` is missing or fails we return False so the
    caller can decide whether to warn or refuse.

    Args:
        path: File or directory to lock down.
        is_dir: Pass True for directories so the POSIX mode becomes 0700.

    Returns:
        True when an OS-level restriction was applied, False if we
        couldn't (and the file is therefore at the default umask /
        Windows ACL).
    """
    if not path.exists():
        return False
    if os.name == "nt":
        return _secure_owner_only_windows(path)
    try:
        path.chmod(0o700 if is_dir else 0o600)
    except OSError as exc:
        logger.debug("chmod on %s failed: %s", path, exc)
        return False
    return True


def _secure_owner_only_windows(path: Path) -> bool:
    """Lock down *path* on Windows via icacls.

    Uses the cheapest reliable option that ships with every modern Windows:
    ``icacls <path> /inheritance:r /grant:r %USERNAME%:F``. Removes
    inherited ACLs (everyone-readable inheritance from %APPDATA% is the
    usual leak) and grants Full Control only to the running user.

    Returns True on success. Does not raise — caller decides what to do
    when False.
    """
    import shutil

    icacls = shutil.which("icacls")
    if icacls is None:
        logger.debug("icacls not found on PATH; skipping Windows ACL hardening")
        return False
    user = os.environ.get("USERNAME") or os.environ.get("USER") or ""
    if not user:
        logger.debug("could not determine current user for icacls grant")
        return False
    try:
        # /inheritance:r removes inherited ACEs; /grant:r adds explicit ACE.
        # /Q quiet, /C continue on errors (still returns non-zero on real
        # failure though).
        completed = subprocess.run(  # noqa: S603 — icacls path is known absolute
            [icacls, str(path), "/inheritance:r", "/grant:r", f"{user}:F", "/Q", "/C"],
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.debug("icacls hardening failed on %s: %s", path, exc)
        return False
    if completed.returncode != 0:
        logger.debug(
            "icacls returned %d for %s (stderr=%r)",
            completed.returncode,
            path,
            completed.stderr.decode(errors="replace")[:200],
        )
        return False
    return True


def can_secure_owner_only() -> bool:
    """Return True iff this host can actually enforce owner-only perms.

    On POSIX always True (``chmod`` always works on regular files we own).
    On Windows we need ``icacls`` on PATH. Used by ``is_using_toml_fallback``
    to escalate the warning when neither path works.
    """
    if os.name == "nt":
        import shutil

        return shutil.which("icacls") is not None
    return True


def _load_toml() -> dict[str, Any]:
    """Load the vars TOML file.  Returns empty dict on any error."""
    if not _VARS_PATH.exists():
        return {}
    try:
        import tomllib

        return tomllib.loads(_VARS_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Could not read vars file %s: %s", _VARS_PATH, exc)
        return {}


def _save_toml(data: dict[str, Any]) -> None:
    """Write *data* to the vars TOML file with owner-only perms.

    POSIX: chmod 0600. Windows: ``icacls`` to grant only the current user
    and remove inherited ACLs. See P0-E in REVIEW.md.
    """
    import tomli_w

    from bog_agents_cli.io_utils import atomic_write_text

    _ensure_config_dir()
    atomic_write_text(_VARS_PATH, tomli_w.dumps(data))
    _secure_owner_only(_VARS_PATH, is_dir=False)


def _warn_fallback_once() -> None:
    """Emit a one-time warning that vars fall back to plaintext TOML.

    The wording is platform-aware (P0-E): on Windows we tell the user
    whether ``icacls`` is on PATH so the warning matches reality. Before
    the fix, the message claimed "mode 0600" unconditionally — a lie on
    Windows where ``Path.chmod`` is a no-op.
    """
    global _warned_fallback  # noqa: PLW0603
    if _warned_fallback:
        return
    _warned_fallback = True
    if os.name == "nt":
        if can_secure_owner_only():
            perms_note = "Windows ACL restricted to current user via icacls"
        else:
            perms_note = (
                "WARNING: icacls not on PATH; file is readable by the local Users "
                "group. Install/repair Windows components or use the keyring backend"
            )
    else:
        perms_note = "POSIX mode 0600"
    logger.warning(
        "OS keyring unavailable; storing vars in %s (plaintext; %s). "
        "Install 'keyring' and a backend (e.g. 'keyrings.alt' or 'secretstorage') "
        "for encrypted storage.",
        _VARS_PATH,
        perms_note,
    )


def is_using_toml_fallback() -> bool:
    """Return True when the OS keyring is unavailable and secrets use plaintext TOML.

    Used by the TUI to surface a one-time security advisory at startup.

    Returns:
        True when the TOML fallback is active, False when keyring is available.
    """
    return not _keyring_available()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def set_var(name: str, value: str) -> str:
    """Store a named variable securely.

    Uses the OS keyring when available, otherwise falls back to
    ``~/.bog-agents/vars.toml`` (mode 0600).  Raises ``ValueError`` for
    invalid names (delegated to ``_validate_name``).

    Args:
        name: Variable name (alphanumeric + underscore, starts with letter/underscore).
        value: Value to store.

    Returns:
        The backend used: ``'keyring'`` or ``'toml'``.
    """
    _validate_name(name)

    if _keyring_available() and _keyring_set(name, value):
        # Also track the name in TOML for enumeration (keyring has no list API)
        data = _load_toml()
        data.setdefault("vars", {})[name] = "__keyring__"
        _save_toml(data)
        return "keyring"

    _warn_fallback_once()
    data = _load_toml()
    data.setdefault("vars", {})[name] = value
    _save_toml(data)
    return "toml"


def get_var(name: str) -> str | None:
    """Retrieve a stored variable by name.

    Args:
        name: Variable name.

    Returns:
        The value string, or ``None`` if not found.
    """
    _validate_name(name)

    # Try keyring first
    if _keyring_available():
        val = _keyring_get(name)
        if val is not None:
            return val

    # Fall back to TOML
    data = _load_toml()
    raw = data.get("vars", {}).get(name)
    if raw == "__keyring__":
        # Name tracked but value is in keyring — keyring must have failed
        logger.warning("Variable %r is keyring-backed but keyring is unavailable", name)
        return None
    return raw


def delete_var(name: str) -> bool:
    """Delete a stored variable.

    Args:
        name: Variable name.

    Returns:
        True if the variable existed and was deleted, False if not found.
    """
    _validate_name(name)
    found = False

    if _keyring_available():
        found = _keyring_delete(name) or found

    data = _load_toml()
    vars_section = data.get("vars", {})
    if name in vars_section:
        del vars_section[name]
        found = True
        _save_toml(data)

    return found


def list_var_names() -> list[str]:
    """Return all stored variable names (never their values).

    Returns:
        Sorted list of variable names.
    """
    data = _load_toml()
    return sorted(data.get("vars", {}).keys())


def var_backend(name: str) -> str:
    """Return where *name* is stored: ``'keyring'``, ``'toml'``, or ``'not found'``.

    Args:
        name: Variable name.

    Returns:
        Storage backend label.
    """
    _validate_name(name)
    data = _load_toml()
    raw = data.get("vars", {}).get(name)
    if raw == "__keyring__":
        return "keyring"
    if raw is not None:
        return "toml"
    if _keyring_available() and _keyring_get(name) is not None:
        return "keyring"
    return "not found"


def resolve_vars(text: str, *, strict: bool = False) -> str:
    """Expand ``{{vars.NAME}}`` placeholders in *text* with stored values.

    When *strict* is True, raises ``KeyError`` for any unresolved variable
    (raised inside the inner replacement function).

    Args:
        text: Input string containing zero or more ``{{vars.NAME}}`` references.
        strict: When True, raise ``KeyError`` for any unresolved variable.

    Returns:
        The input string with all resolvable placeholders substituted.
    """

    def _replace(m: re.Match[str]) -> str:
        var_name = m.group(1)
        value = get_var(var_name)
        if value is None:
            if strict:
                msg = f"Variable '{{vars.{var_name}}}' not found in store"
                raise KeyError(msg)
            logger.debug(
                "vars.resolve: variable %r not found; leaving placeholder", var_name
            )
            return m.group(0)
        return value

    return _VAR_REF_RE.sub(_replace, text)
