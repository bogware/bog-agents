"""`.bog-agents/sandbox.lock` (ROADMAP #76): remember a provider's environment snapshot and when it is still valid.

A remote sandbox (daytona, Docker) that already has the project's dependencies
installed can be snapshotted; the lock records the snapshot id per provider
together with the lockfile hashes it was built from. `snapshot_for()` returns
the id only while those hashes still match the checkout, so a bumped lockfile
invalidates the template automatically instead of silently reusing a stale
image. Pure JSON read / write; providers consult it when they accept an image
or snapshot id.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

from bog_agents_cli.envcache import GENERIC_LOCKFILES, LOCKFILES

LOCK_RELATIVE = Path(".bog-agents") / "sandbox.lock"
VERSION = 1


def lock_path(project_root: str | Path) -> Path:
    """`<root>/.bog-agents/sandbox.lock`."""
    return Path(project_root) / LOCK_RELATIVE


def current_hashes(project_root: str | Path) -> dict[str, str]:
    """`{lockfile: hash}` for every lockfile present at the root."""
    root = Path(project_root)
    out: dict[str, str] = {}
    names = {name for group in LOCKFILES.values() for name in group} | set(
        GENERIC_LOCKFILES
    )
    for name in sorted(names):
        path = root / name
        if path.is_file():
            out[name] = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    return out


def read_lock(project_root: str | Path) -> dict[str, Any]:
    """The lock document (`{"version": 1, "snapshots": {provider: {...}}}`), empty when absent or unreadable."""
    path = lock_path(project_root)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"version": VERSION, "snapshots": {}}
    if not isinstance(data, dict) or not isinstance(data.get("snapshots"), dict):
        return {"version": VERSION, "snapshots": {}}
    return data


def record_snapshot(
    project_root: str | Path,
    provider: str,
    snapshot_id: str,
    *,
    hashes: dict[str, str] | None = None,
    note: str = "",
) -> Path:
    """Record `snapshot_id` for `provider`, keyed by the current lockfile hashes; returns the lock path."""
    data = read_lock(project_root)
    data["version"] = VERSION
    data["snapshots"][provider] = {
        "id": snapshot_id,
        "created_at": time.time(),
        "lock_hashes": hashes if hashes is not None else current_hashes(project_root),
        "note": note,
    }
    path = lock_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def snapshot_for(project_root: str | Path, provider: str) -> tuple[str | None, str]:
    """`(snapshot id or None, reason)`: the id only while the recorded lockfile hashes still match."""
    entry = read_lock(project_root)["snapshots"].get(provider)
    if not isinstance(entry, dict) or not entry.get("id"):
        return None, f"no {provider} snapshot recorded"
    recorded = entry.get("lock_hashes") or {}
    now = current_hashes(project_root)
    changed = sorted(
        name for name in set(recorded) | set(now) if recorded.get(name) != now.get(name)
    )
    if changed:
        return (
            None,
            f"{provider} snapshot {entry['id']} is stale: {', '.join(changed)} changed",
        )
    return str(entry["id"]), f"{provider} snapshot {entry['id']} matches the lockfiles"


def forget_snapshot(project_root: str | Path, provider: str) -> bool:
    """Drop a provider's entry; `True` when one existed."""
    data = read_lock(project_root)
    if provider not in data["snapshots"]:
        return False
    del data["snapshots"][provider]
    lock_path(project_root).write_text(
        json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return True


def describe(project_root: str | Path) -> str:
    """Rows for `/sandbox snapshots`."""
    data = read_lock(project_root)
    if not data["snapshots"]:
        return "No sandbox snapshots recorded (.bog-agents/sandbox.lock)."
    lines = []
    for provider in sorted(data["snapshots"]):
        snapshot, reason = snapshot_for(project_root, provider)
        lines.append(f"{provider}: {'valid' if snapshot else 'STALE'} — {reason}")
    return "\n".join(lines)


__all__ = [
    "current_hashes",
    "describe",
    "forget_snapshot",
    "lock_path",
    "read_lock",
    "record_snapshot",
    "snapshot_for",
]
