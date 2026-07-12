"""Persistent trust store for symlinked skill directories.

By default the skill loader refuses **every** symlinked skill directory
(the P1-8 containment posture, enforced in the SDK's `_filter_skill_dirs`
chokepoint on both the sync and async listing paths). A symlinked skill
directory could otherwise point its `SKILL.md` at an arbitrary file, so the
loader walks past it.

This module deliberately *relaxes* that posture for directories the user has
**explicitly** approved. Trust is:

* **Explicit** — nothing is trusted until the user runs `/skills trust <path>`.
* **Per-resolved-path** — an entry is keyed by the real (`resolve()`-d) target
  directory shown to the user at approval time, stored as-is and never
  re-resolved. That single property is what makes a post-approval symlink swap
  detectable (see below).
* **Fail-closed** — a missing, malformed, oversized, or wrong-version store
  trusts nothing, so the default "refuse all symlinks" posture is what a broken
  store degrades to. Trust is never granted by default.

Two distinct post-approval swaps are both re-refused:

* **Re-pointing the discovery symlink** (trust dir A, then repoint the symlink
  at attacker dir B) — `is_symlinked_skill_dir_allowed` resolves the *current*
  symlink target and checks membership, so a target that now resolves to B is
  not on the trusted set and the read is refused.
* **Replacing the stored directory (or a parent) with a symlink** —
  `load_trusted_skill_dirs` re-verifies each stored entry with a
  `resolve()`-to-self check and drops any entry that no longer resolves to
  itself, so the injected symlink is never followed to a directory the user
  never approved.

A content **fingerprint** (`SKILL.md` size + mtime) is stored alongside each
entry as a defence-in-depth tamper check: if the approved directory's
`SKILL.md` is later replaced in place, the fingerprint no longer matches and
the directory is re-refused until re-approved (mirroring `mcp_trust`'s
"content changed → re-approve" philosophy).

Trust entries are app-managed bookkeeping, not hand-editable configuration, so
they live under `~/.bog-agents/.state/skill_trust.json` rather than in
`config.toml`. The file is written atomically and locked to owner-only access.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypedDict

from bog_agents_cli.io_utils import atomic_write_text
from bog_agents_cli.vars_store import _secure_owner_only

if TYPE_CHECKING:
    from collections.abc import Mapping

logger = logging.getLogger(__name__)

_STORAGE_VERSION = 1
"""Schema version stamped into `skill_trust.json`; bump on incompatible changes."""

_MAX_STORE_BYTES = 1_000_000
"""Reject a store larger than this (fail-closed) so a hostile/corrupt file
cannot be slurped into memory or hang parsing. A real store holds a handful of
paths; anything approaching a megabyte is pathological."""


class _TrustEntry(TypedDict, total=False):
    """One trusted-directory record in the store's `dirs` map."""

    trusted_at: str
    """ISO-8601 UTC timestamp of when the directory was approved."""

    skill_md_size: int
    """Size in bytes of the approved directory's `SKILL.md` at trust time."""

    skill_md_mtime_ns: int
    """Modification time (ns) of the approved `SKILL.md` at trust time."""


class _TrustStore(TypedDict):
    """On-disk shape of `skill_trust.json`."""

    version: int
    dirs: dict[str, _TrustEntry]


class RevokeResult(Enum):
    """Outcome of a `revoke_skill_dir_trust` call.

    Distinguishing `NOT_FOUND` from `REMOVED` lets the CLI print an honest
    message instead of a false success when the target was never trusted.
    """

    REMOVED = "removed"
    """An entry existed and was removed from the store."""

    NOT_FOUND = "not_found"
    """No matching entry existed; the store was left unchanged."""

    ERROR = "error"
    """The store could not be read or the removal could not be persisted."""


def _default_store_path() -> Path:
    """Return `~/.bog-agents/.state/skill_trust.json`.

    Resolved at call time (not import time) so tests can point storage at a
    temp directory by passing `store_path=` explicitly.

    Returns:
        Path to the default trust store file.
    """
    return Path.home() / ".bog-agents" / ".state" / "skill_trust.json"


def _normalize(target_dir: Path | str) -> str:
    """Return the resolved absolute string form of a directory key.

    Args:
        target_dir: Directory to canonicalize.

    Returns:
        The `expanduser().resolve()`-d absolute path as a string.
    """
    return str(Path(target_dir).expanduser().resolve())


def _approved_key(target_dir: Path | str) -> str:
    """Return the already-approved directory key without resolving again.

    Args:
        target_dir: Directory whose stored key form is wanted.

    Returns:
        The `expanduser()`-only path as a string (not re-resolved).
    """
    return str(Path(target_dir).expanduser())


def _skill_md_fingerprint(target_dir: Path | str) -> tuple[int, int] | None:
    """Return `(size, mtime_ns)` of `target_dir/SKILL.md`, or None.

    Args:
        target_dir: Directory expected to contain a `SKILL.md`.

    Returns:
        A `(size, mtime_ns)` tuple, or None when the `SKILL.md` cannot be
            stat-ed (missing or unreadable).
    """
    skill_md = Path(target_dir).expanduser() / "SKILL.md"
    try:
        st = skill_md.stat()
    except OSError:
        return None
    return int(st.st_size), int(st.st_mtime_ns)


def _load_store(store_path: Path, *, strict: bool = False) -> dict[str, Any]:
    """Read and validate the JSON trust store file.

    Args:
        store_path: Path to the trust store file.
        strict: When True, an existing-but-unreadable/corrupt/wrong-version
            store re-raises instead of degrading to `{}`. Read/modify/write
            callers pass `strict=True` so a transient read error aborts the
            write rather than clobbering every prior approval by rebuilding from
            an empty dict. Enforcement callers leave it False to stay
            fail-closed (a broken store trusts nothing).

    Returns:
        Parsed JSON data, or an empty dict when the file is missing, or (only
            when `strict` is False) when it is unreadable, oversized, corrupt,
            not an object, or carries an unrecognized schema version.

    Raises:
        OSError: When `strict` and an existing store cannot be read.
        json.JSONDecodeError: When `strict` and an existing store is not valid
            JSON.
        ValueError: When `strict` and the store is oversized, is not a JSON
            object, or has an unrecognized schema `version`.
    """
    # A missing store is a normal first-run state, never an error.
    if not store_path.exists():
        return {}
    try:
        raw = store_path.read_text(encoding="utf-8")
    except OSError as exc:
        if strict:
            raise
        logger.warning(
            "Could not read skill trust store %s; treating as empty: %s",
            store_path,
            exc,
        )
        return {}
    # Fail-closed on an implausibly large file: never trust it, and don't parse it.
    if len(raw.encode("utf-8", errors="ignore")) > _MAX_STORE_BYTES:
        if strict:
            msg = f"Skill trust store {store_path} is too large ({len(raw)} chars); refusing to read it"
            raise ValueError(msg)
        logger.warning(
            "Skill trust store %s is too large; treating as empty", store_path
        )
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        if strict:
            raise
        # A corrupt store silently drops every prior approval and forces a
        # re-prompt, so log at WARNING to leave a breadcrumb.
        logger.warning(
            "Skill trust store %s is corrupt; treating as empty: %s", store_path, exc
        )
        return {}
    if not isinstance(data, dict):
        if strict:
            msg = f"Skill trust store {store_path} is not a JSON object"
            raise ValueError(msg)
        logger.warning(
            "Skill trust store %s is not a JSON object; ignoring", store_path
        )
        return {}
    # A store written by a newer build may carry an incompatible schema; refuse
    # it (fail-closed for enforcement, surfaced for the audit path). A
    # present-but-non-integer `version` is unrecognized the same way (only
    # tampering or a corrupt write produces it, since every writer stamps an
    # int). A missing `version` stays tolerated: an empty `{}` has no `dirs`.
    version = data.get("version")
    if version is not None and (
        not isinstance(version, int) or version > _STORAGE_VERSION
    ):
        if strict:
            msg = (
                f"Skill trust store {store_path} has an unrecognized schema version {version!r} "
                f"(this build understands <= {_STORAGE_VERSION}); refusing to read it"
            )
            raise ValueError(msg)
        logger.warning(
            "Skill trust store %s has an unrecognized schema version %r (understood <= %s); treating as empty",
            store_path,
            version,
            _STORAGE_VERSION,
        )
        return {}
    return data


def _save_store(data: Mapping[str, Any], store_path: Path) -> bool:
    """Atomically persist JSON trust data with owner-only permissions.

    Args:
        data: Full store dict to write.
        store_path: Destination path.

    Returns:
        True on success, False on I/O failure.
    """
    try:
        store_path.parent.mkdir(parents=True, exist_ok=True)
        # Lock the .state directory down too (it may hold other secret-bearing
        # state); best-effort, never fatal.
        _secure_owner_only(store_path.parent, is_dir=True)
        atomic_write_text(
            store_path, json.dumps(data, indent=2), encoding="utf-8", mode=0o600
        )
    except OSError:
        logger.exception("Failed to save skill trust store to %s", store_path)
        return False
    # atomic_write_text applies the POSIX mode; _secure_owner_only additionally
    # covers Windows (icacls) and is a no-op-safe re-assert on POSIX.
    _secure_owner_only(store_path)
    return True


def _read_dirs(store_path: Path, *, strict: bool = False) -> dict[str, Any]:
    """Return the `dirs` mapping from the store, or an empty dict.

    Args:
        store_path: Path to the trust store file.
        strict: Propagated to `_load_store`; see its docstring.

    Returns:
        The `dirs` mapping, or an empty dict when absent or malformed.
    """
    dirs = _load_store(store_path, strict=strict).get("dirs", {})
    return dirs if isinstance(dirs, dict) else {}


def is_skill_dir_trusted(
    target_dir: Path | str, *, store_path: Path | None = None
) -> bool:
    """Check whether a resolved skill directory is present in the store.

    Warning:
        This resolves `target_dir` and checks raw membership; it does NOT do the
        `resolve()`-to-self re-verification that `load_trusted_skill_dirs`
        performs. It is therefore an informational "is this exact resolved dir on
        record?" check, **not** the containment-enforcement primitive — use
        `is_symlinked_skill_dir_allowed` for enforcement.

        The check happens to fail *closed*, not open: because it resolves the
        query, a stored directory later swapped for a symlink is reported **not**
        trusted (the query resolves to the swap target, which is not the stored
        key), forcing a re-prompt.

    Args:
        target_dir: Directory to check; resolved before lookup.
        store_path: Trust store path. Defaults to the standard location.

    Returns:
        True if the resolved directory is present in the store.
    """
    if store_path is None:
        store_path = _default_store_path()
    return _normalize(target_dir) in _read_dirs(store_path)


def trust_skill_dir(target_dir: Path | str, *, store_path: Path | None = None) -> bool:
    """Persist trust for a resolved skill directory.

    Args:
        target_dir: Canonical directory to trust. Expected to be the
            already-resolved path shown to the user; it is stored `expanduser()`
            -only (not re-resolved) so a post-approval symlink swap cannot change
            what was persisted. The directory's `SKILL.md` is fingerprinted
            (size + mtime) for the tamper check.
        store_path: Trust store path. Defaults to the standard location.

    Returns:
        True if the entry was saved successfully.
    """
    if store_path is None:
        store_path = _default_store_path()

    # Read strictly: if an existing store can't be read, abort rather than
    # rebuild from `{}` and drop every prior approval.
    try:
        data = _load_store(store_path, strict=True)
    except (OSError, ValueError, json.JSONDecodeError):
        logger.exception(
            "Refusing to persist skill trust: could not read existing store %s",
            store_path,
        )
        return False

    key = _approved_key(target_dir)
    # The stored key must be canonical for the resolve()-to-self recheck to
    # accept it later. Warn (don't abort) if the caller passed a non-canonical
    # path — the read-time recheck is the real safety net.
    try:
        is_canonical = key == _normalize(target_dir)
    except OSError:
        is_canonical = True
    if not is_canonical:
        logger.warning(
            "trust_skill_dir called with a non-canonical path %r; the stored entry will be dropped at "
            "read time. Pass an already-resolved directory.",
            target_dir,
        )

    entry = _TrustEntry(trusted_at=datetime.now(UTC).isoformat())
    fingerprint = _skill_md_fingerprint(target_dir)
    if fingerprint is not None:
        entry["skill_md_size"], entry["skill_md_mtime_ns"] = fingerprint

    dirs = data.get("dirs")
    if not isinstance(dirs, dict):
        dirs = {}
    dirs[key] = entry
    return _save_store(_TrustStore(version=_STORAGE_VERSION, dirs=dirs), store_path)


def revoke_skill_dir_trust(
    target_dir: Path | str, *, store_path: Path | None = None
) -> RevokeResult:
    """Remove trust for a skill directory.

    Matches on both the approved (`expanduser()`-only) key form that
    `trust_skill_dir` stores and the fully-resolved form, so a caller can revoke
    either by the path shown in `/skills trust list` or by the original symlink
    path.

    Args:
        target_dir: Directory to revoke.
        store_path: Trust store path. Defaults to the standard location.

    Returns:
        `RevokeResult.REMOVED` if a matching entry was removed and persisted,
        `RevokeResult.NOT_FOUND` if no entry matched, or `RevokeResult.ERROR`
        if the store could not be read or the write failed.
    """
    if store_path is None:
        store_path = _default_store_path()

    try:
        data = _load_store(store_path, strict=True)
    except (OSError, ValueError, json.JSONDecodeError):
        logger.exception(
            "Refusing to revoke skill trust: could not read existing store %s",
            store_path,
        )
        return RevokeResult.ERROR
    dirs = data.get("dirs")
    if not isinstance(dirs, dict):
        return RevokeResult.NOT_FOUND
    keys = {_approved_key(target_dir)}
    try:
        keys.add(_normalize(target_dir))
    except OSError:
        pass
    removed = False
    for key in keys:
        if key in dirs:
            del dirs[key]
            removed = True
    if not removed:
        return RevokeResult.NOT_FOUND
    data["version"] = _STORAGE_VERSION
    data["dirs"] = dirs
    return RevokeResult.REMOVED if _save_store(data, store_path) else RevokeResult.ERROR


def clear_trusted_skill_dirs(*, store_path: Path | None = None) -> bool:
    """Remove all trusted skill directories.

    Args:
        store_path: Trust store path. Defaults to the standard location.

    Returns:
        True if the store was cleared (or was already empty).
    """
    if store_path is None:
        store_path = _default_store_path()
    if not store_path.exists():
        return True
    return _save_store(_TrustStore(version=_STORAGE_VERSION, dirs={}), store_path)


def list_trusted_skill_dirs(
    *, store_path: Path | None = None, strict: bool = False
) -> list[str]:
    """Return the sorted list of trusted skill directory paths.

    Args:
        store_path: Trust store path. Defaults to the standard location.
        strict: When True, an existing-but-unreadable store re-raises instead of
            degrading to an empty list (used by the audit command so it can
            report an error rather than falsely printing "nothing trusted").

    Returns:
        Sorted absolute directory paths previously trusted.
    """
    if store_path is None:
        store_path = _default_store_path()
    return sorted(_read_dirs(store_path, strict=strict))


def list_trusted_skill_dir_entries(
    *, store_path: Path | None = None, strict: bool = False
) -> list[tuple[str, str]]:
    """Return trusted directories paired with their approval timestamps.

    Args:
        store_path: Trust store path. Defaults to the standard location.
        strict: Propagated to `_load_store`; see `list_trusted_skill_dirs`.

    Returns:
        `(path, trusted_at)` tuples sorted by path. `trusted_at` is the stored
            ISO-8601 string, or `""` when a hand-edited entry omitted or
            malformed it (the path is still listed so it stays visible and
            revocable).
    """
    if store_path is None:
        store_path = _default_store_path()
    entries: list[tuple[str, str]] = []
    for path, entry in _read_dirs(store_path, strict=strict).items():
        trusted_at = entry.get("trusted_at", "") if isinstance(entry, dict) else ""
        entries.append((path, trusted_at if isinstance(trusted_at, str) else ""))
    return sorted(entries)


def load_trusted_skill_dirs(*, store_path: Path | None = None) -> list[Path]:
    """Return verified trusted skill directories as canonical `Path` objects.

    Each stored entry is re-verified rather than blindly re-resolved: if a
    stored path no longer resolves to itself — because it, or a parent
    component, was replaced with a symlink after approval — the current
    resolution would point somewhere the user never approved. Such entries are
    dropped (and logged) instead of silently allowlisting the swapped target, so
    a post-approval symlink swap re-prompts rather than granting access.

    Args:
        store_path: Trust store path. Defaults to the standard location.

    Returns:
        Canonical directory paths that still resolve to themselves; empty when
            nothing is trusted.
    """
    verified: list[Path] = []
    for entry in list_trusted_skill_dirs(store_path=store_path):
        stored = Path(entry)
        try:
            resolves_to_self = stored.resolve() == stored
        except (OSError, RuntimeError):
            # A single unresolvable entry (e.g. a symlink cycle) must not abort
            # discovery of every other trusted dir. Drop it like the swap case.
            logger.warning(
                "Trusted skill directory %s could not be resolved; ignoring the trust entry.",
                entry,
                exc_info=True,
            )
            continue
        if resolves_to_self:
            verified.append(stored)
        else:
            logger.warning(
                "Trusted skill directory %s no longer resolves to itself (a symlink may have been introduced "
                "since approval); ignoring the stale trust entry.",
                entry,
            )
    return verified


def install_symlink_trust_hook() -> None:
    """Register this module's enforcement checker with the SDK skill loader.

    Idempotent: safe to call from multiple entry points. After this runs, the
    SDK's `_filter_skill_dirs` chokepoint consults `is_symlinked_skill_dir_allowed`
    (against the default store) before refusing a symlinked skill directory, so
    both the CLI's `/skills` listing and the agent's `SkillsMiddleware` honor
    trust. Without it, the SDK's default "refuse every symlink" posture stands.
    """
    from bog_agents.middleware.skills import set_symlink_trust_checker

    set_symlink_trust_checker(is_symlinked_skill_dir_allowed)


def is_symlinked_skill_dir_allowed(
    item_path: Path | str, *, store_path: Path | None = None
) -> bool:
    """Enforcement primitive: may this symlinked skill dir be loaded?

    Called by the skill loader's containment chokepoint for a directory it has
    already determined is a symlink. Returns True only when **all** of the
    following hold, so the default "refuse every symlink" posture is preserved
    unless the user explicitly opted in:

    1. The symlink's current target resolves without error.
    2. The resolved target is on the verified trusted set — i.e. it was
       explicitly trusted **and** still resolves to itself (catches both the
       repointed-discovery-symlink swap and the swapped-stored-dir swap).
    3. The target's `SKILL.md` fingerprint (size + mtime) still matches what was
       recorded at trust time, when a fingerprint was recorded (defence-in-depth
       against in-place content replacement at the same real path).

    Any error resolving, reading the store, or stat-ing the `SKILL.md` returns
    False (fail-closed).

    Args:
        item_path: The symlinked directory path reported by the backend.
        store_path: Trust store path. Defaults to the standard location.

    Returns:
        True if the symlinked directory may be loaded; False to keep refusing.
    """
    if store_path is None:
        store_path = _default_store_path()
    try:
        resolved = Path(item_path).expanduser().resolve()
    except (OSError, RuntimeError):
        return False

    verified = load_trusted_skill_dirs(store_path=store_path)
    if resolved not in verified:
        return False

    # Fingerprint (tamper) check against the stored entry, keyed by the resolved
    # path (the canonical form stored at trust time).
    entry = _read_dirs(store_path).get(str(resolved))
    if (
        isinstance(entry, dict)
        and "skill_md_size" in entry
        and "skill_md_mtime_ns" in entry
    ):
        current = _skill_md_fingerprint(resolved)
        if current is None:
            return False
        if (current[0], current[1]) != (
            entry.get("skill_md_size"),
            entry.get("skill_md_mtime_ns"),
        ):
            logger.warning(
                "Trusted skill directory %s has a changed SKILL.md fingerprint since approval; refusing until re-approved.",
                resolved,
            )
            return False
    return True
