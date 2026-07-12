"""Tests for the symlinked-skill-directory trust store and its loader wiring.

Security-sensitive: the default posture refuses every symlinked skill
directory (P1-8 containment). The trust store relaxes that only for
explicitly-approved, per-resolved-path entries, and must re-refuse a
post-approval symlink swap. These tests pin that model.

Windows note: symlink creation needs privilege, so the swap/enforcement tests
guard `symlink_to` with try/except and skip when unavailable (per the CLI test
conventions). The store-level trust/revoke/clear/list tests do not need
symlinks and run everywhere.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bog_agents_cli import skill_trust
from bog_agents_cli.skill_trust import RevokeResult


def _store(tmp_path: Path) -> Path:
    """Return a temp trust-store path (not yet created)."""
    return tmp_path / ".state" / "skill_trust.json"


def _make_skill_dir(root: Path, name: str, body: str = "name: demo") -> Path:
    """Create a skill directory containing a SKILL.md and return it."""
    d = root / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\n{body}\n---\n", encoding="utf-8")
    return d


def _try_symlink(link: Path, target: Path) -> bool:
    """Create `link -> target`, returning False if the OS forbids symlinks."""
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):
        return False
    return link.is_symlink()


# ---------------------------------------------------------------------------
# Store round-trip: trust / is_trusted / revoke / clear / list
# ---------------------------------------------------------------------------


def test_trust_then_is_trusted(tmp_path: Path) -> None:
    store = _store(tmp_path)
    d = _make_skill_dir(tmp_path, "sk")
    resolved = d.resolve()

    assert skill_trust.is_skill_dir_trusted(resolved, store_path=store) is False
    assert skill_trust.trust_skill_dir(resolved, store_path=store) is True
    assert skill_trust.is_skill_dir_trusted(resolved, store_path=store) is True


def test_revoke_after_trust(tmp_path: Path) -> None:
    store = _store(tmp_path)
    d = _make_skill_dir(tmp_path, "sk").resolve()
    skill_trust.trust_skill_dir(d, store_path=store)

    assert (
        skill_trust.revoke_skill_dir_trust(d, store_path=store) is RevokeResult.REMOVED
    )
    assert skill_trust.is_skill_dir_trusted(d, store_path=store) is False


def test_revoke_not_found(tmp_path: Path) -> None:
    store = _store(tmp_path)
    d = _make_skill_dir(tmp_path, "sk").resolve()
    assert (
        skill_trust.revoke_skill_dir_trust(d, store_path=store)
        is RevokeResult.NOT_FOUND
    )


def test_clear_removes_all(tmp_path: Path) -> None:
    store = _store(tmp_path)
    a = _make_skill_dir(tmp_path, "a").resolve()
    b = _make_skill_dir(tmp_path, "b").resolve()
    skill_trust.trust_skill_dir(a, store_path=store)
    skill_trust.trust_skill_dir(b, store_path=store)
    assert len(skill_trust.list_trusted_skill_dirs(store_path=store)) == 2

    assert skill_trust.clear_trusted_skill_dirs(store_path=store) is True
    assert skill_trust.list_trusted_skill_dirs(store_path=store) == []


def test_clear_on_missing_store_is_ok(tmp_path: Path) -> None:
    assert skill_trust.clear_trusted_skill_dirs(store_path=_store(tmp_path)) is True


def test_list_entries_includes_timestamp(tmp_path: Path) -> None:
    store = _store(tmp_path)
    d = _make_skill_dir(tmp_path, "sk").resolve()
    skill_trust.trust_skill_dir(d, store_path=store)

    entries = skill_trust.list_trusted_skill_dir_entries(store_path=store)
    assert len(entries) == 1
    path, trusted_at = entries[0]
    assert path == str(d)
    assert trusted_at  # non-empty ISO timestamp


def test_store_written_owner_only_json(tmp_path: Path) -> None:
    store = _store(tmp_path)
    d = _make_skill_dir(tmp_path, "sk").resolve()
    skill_trust.trust_skill_dir(d, store_path=store)

    data = json.loads(store.read_text(encoding="utf-8"))
    assert data["version"] == skill_trust._STORAGE_VERSION
    assert str(d) in data["dirs"]


# ---------------------------------------------------------------------------
# Fail-closed: malformed / oversized / wrong-version store trusts nothing
# ---------------------------------------------------------------------------


def test_malformed_store_fails_closed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.parent.mkdir(parents=True)
    store.write_text("{ not valid json ", encoding="utf-8")

    assert skill_trust.list_trusted_skill_dirs(store_path=store) == []
    assert skill_trust.load_trusted_skill_dirs(store_path=store) == []
    assert skill_trust.is_skill_dir_trusted(tmp_path, store_path=store) is False


def test_wrong_version_store_fails_closed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.parent.mkdir(parents=True)
    store.write_text(
        json.dumps(
            {"version": skill_trust._STORAGE_VERSION + 99, "dirs": {str(tmp_path): {}}}
        ),
        encoding="utf-8",
    )
    assert skill_trust.load_trusted_skill_dirs(store_path=store) == []


def test_non_object_store_fails_closed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.parent.mkdir(parents=True)
    store.write_text(json.dumps(["not", "an", "object"]), encoding="utf-8")
    assert skill_trust.load_trusted_skill_dirs(store_path=store) == []


def test_oversized_store_fails_closed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.parent.mkdir(parents=True)
    store.write_text(
        "[" + "0," * (skill_trust._MAX_STORE_BYTES) + "0]", encoding="utf-8"
    )
    assert skill_trust.load_trusted_skill_dirs(store_path=store) == []


def test_strict_list_raises_on_corrupt(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.parent.mkdir(parents=True)
    store.write_text("}}bad", encoding="utf-8")
    with pytest.raises((ValueError, json.JSONDecodeError)):
        skill_trust.list_trusted_skill_dirs(store_path=store, strict=True)


def test_trust_refuses_to_clobber_corrupt_store(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.parent.mkdir(parents=True)
    store.write_text("not json", encoding="utf-8")
    d = _make_skill_dir(tmp_path, "sk").resolve()
    # A strict read failure must abort the write rather than rebuild from {}.
    assert skill_trust.trust_skill_dir(d, store_path=store) is False


# ---------------------------------------------------------------------------
# Default posture: no trust file -> symlinks refused
# ---------------------------------------------------------------------------


def test_default_no_store_refuses_symlink(tmp_path: Path) -> None:
    store = _store(tmp_path)
    real = _make_skill_dir(tmp_path, "real").resolve()
    link = tmp_path / "linked"
    if not _try_symlink(link, real):
        pytest.skip("symlinks not permitted on this platform")
    # Nothing trusted yet.
    assert skill_trust.is_symlinked_skill_dir_allowed(link, store_path=store) is False


# ---------------------------------------------------------------------------
# Enforcement: trusted symlink allowed; swap re-refused
# ---------------------------------------------------------------------------


def test_trusted_symlink_is_allowed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    real = _make_skill_dir(tmp_path, "real").resolve()
    link = tmp_path / "linked"
    if not _try_symlink(link, real):
        pytest.skip("symlinks not permitted on this platform")

    # Trust the resolved real target (what the user is shown).
    assert skill_trust.trust_skill_dir(real, store_path=store) is True
    assert skill_trust.is_symlinked_skill_dir_allowed(link, store_path=store) is True


def test_symlink_repoint_after_trust_is_refused(tmp_path: Path) -> None:
    store = _store(tmp_path)
    dir_a = _make_skill_dir(tmp_path, "dir_a").resolve()
    dir_b = _make_skill_dir(tmp_path, "dir_b").resolve()
    link = tmp_path / "linked"
    if not _try_symlink(link, dir_a):
        pytest.skip("symlinks not permitted on this platform")

    # Trust A (the current target of the symlink).
    skill_trust.trust_skill_dir(dir_a, store_path=store)
    assert skill_trust.is_symlinked_skill_dir_allowed(link, store_path=store) is True

    # Attacker repoints the discovery symlink at B.
    link.unlink()
    if not _try_symlink(link, dir_b):
        pytest.skip("symlinks not permitted on this platform")
    # B was never trusted -> re-refused.
    assert skill_trust.is_symlinked_skill_dir_allowed(link, store_path=store) is False


def test_swapped_stored_dir_is_refused(tmp_path: Path) -> None:
    """Replacing the trusted directory itself with a symlink drops the entry."""
    store = _store(tmp_path)
    real = _make_skill_dir(tmp_path, "real").resolve()
    elsewhere = _make_skill_dir(tmp_path, "elsewhere").resolve()
    skill_trust.trust_skill_dir(real, store_path=store)

    # Swap the stored dir for a symlink pointing at attacker content.
    import shutil

    shutil.rmtree(real)
    if not _try_symlink(real, elsewhere):
        pytest.skip("symlinks not permitted on this platform")

    # resolve()-to-self re-verification drops the now-symlinked stored entry.
    assert skill_trust.load_trusted_skill_dirs(store_path=store) == []
    assert skill_trust.is_symlinked_skill_dir_allowed(real, store_path=store) is False


def test_skill_md_fingerprint_change_is_refused(tmp_path: Path) -> None:
    """In-place SKILL.md replacement at the trusted path re-refuses."""
    store = _store(tmp_path)
    real = _make_skill_dir(tmp_path, "real", body="name: original").resolve()
    link = tmp_path / "linked"
    if not _try_symlink(link, real):
        pytest.skip("symlinks not permitted on this platform")
    skill_trust.trust_skill_dir(real, store_path=store)
    assert skill_trust.is_symlinked_skill_dir_allowed(link, store_path=store) is True

    # Rewrite SKILL.md with a distinct size and bumped mtime.
    skill_md = real / "SKILL.md"
    skill_md.write_text(
        "---\nname: tampered-with-much-longer-content\n---\nmalicious\n",
        encoding="utf-8",
    )
    import os

    st = skill_md.stat()
    os.utime(skill_md, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))

    assert skill_trust.is_symlinked_skill_dir_allowed(link, store_path=store) is False


def test_revoke_disallows_symlink(tmp_path: Path) -> None:
    store = _store(tmp_path)
    real = _make_skill_dir(tmp_path, "real").resolve()
    link = tmp_path / "linked"
    if not _try_symlink(link, real):
        pytest.skip("symlinks not permitted on this platform")
    skill_trust.trust_skill_dir(real, store_path=store)
    assert skill_trust.is_symlinked_skill_dir_allowed(link, store_path=store) is True

    skill_trust.revoke_skill_dir_trust(real, store_path=store)
    assert skill_trust.is_symlinked_skill_dir_allowed(link, store_path=store) is False


# ---------------------------------------------------------------------------
# Enforcement logic without symlinks (runs on Windows too): membership +
# fingerprint are checked against whatever `item_path` resolves to, so the real
# directory can stand in for a symlink whose target it is.
# ---------------------------------------------------------------------------


def test_enforcement_membership_without_symlink(tmp_path: Path) -> None:
    store = _store(tmp_path)
    trusted = _make_skill_dir(tmp_path, "trusted").resolve()
    other = _make_skill_dir(tmp_path, "other").resolve()
    skill_trust.trust_skill_dir(trusted, store_path=store)

    # The trusted resolved dir is allowed; a different, untrusted dir is not.
    assert skill_trust.is_symlinked_skill_dir_allowed(trusted, store_path=store) is True
    assert skill_trust.is_symlinked_skill_dir_allowed(other, store_path=store) is False


def test_enforcement_fingerprint_without_symlink(tmp_path: Path) -> None:
    import os

    store = _store(tmp_path)
    trusted = _make_skill_dir(tmp_path, "trusted", body="name: original").resolve()
    skill_trust.trust_skill_dir(trusted, store_path=store)
    assert skill_trust.is_symlinked_skill_dir_allowed(trusted, store_path=store) is True

    # Replace SKILL.md in place with different size + bumped mtime -> re-refused.
    skill_md = trusted / "SKILL.md"
    skill_md.write_text(
        "---\nname: tampered-with-a-much-longer-body\n---\nevil\n", encoding="utf-8"
    )
    st = skill_md.stat()
    os.utime(skill_md, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    assert (
        skill_trust.is_symlinked_skill_dir_allowed(trusted, store_path=store) is False
    )


def test_enforcement_missing_skill_md_after_trust(tmp_path: Path) -> None:
    store = _store(tmp_path)
    trusted = _make_skill_dir(tmp_path, "trusted").resolve()
    skill_trust.trust_skill_dir(trusted, store_path=store)
    (trusted / "SKILL.md").unlink()
    # Fingerprint was recorded but SKILL.md is now unreadable -> fail-closed.
    assert (
        skill_trust.is_symlinked_skill_dir_allowed(trusted, store_path=store) is False
    )


# ---------------------------------------------------------------------------
# Integration with the SDK chokepoint _filter_skill_dirs
# ---------------------------------------------------------------------------


def test_filter_skill_dirs_honors_trust(tmp_path: Path) -> None:
    """The SDK chokepoint refuses an untrusted symlink and allows a trusted one."""
    from bog_agents.middleware import skills as sdk_skills

    store = _store(tmp_path)
    real = _make_skill_dir(tmp_path, "real").resolve()
    link = tmp_path / "linked"
    if not _try_symlink(link, real):
        pytest.skip("symlinks not permitted on this platform")

    items = [{"path": str(link), "is_dir": True}]

    # Register a checker bound to the temp store, restoring afterwards.
    previous = sdk_skills._symlink_trust_checker
    try:
        sdk_skills.set_symlink_trust_checker(
            lambda p: skill_trust.is_symlinked_skill_dir_allowed(p, store_path=store)
        )
        # Untrusted -> filtered out.
        assert sdk_skills._filter_skill_dirs(items) == []
        # Trusted -> passes through.
        skill_trust.trust_skill_dir(real, store_path=store)
        assert sdk_skills._filter_skill_dirs(items) == [str(link)]
    finally:
        sdk_skills.set_symlink_trust_checker(previous)


def test_filter_skill_dirs_default_refuses_symlink(tmp_path: Path) -> None:
    """With no checker registered, the chokepoint refuses symlinks (default)."""
    from bog_agents.middleware import skills as sdk_skills

    real = _make_skill_dir(tmp_path, "real").resolve()
    link = tmp_path / "linked"
    if not _try_symlink(link, real):
        pytest.skip("symlinks not permitted on this platform")

    items = [{"path": str(link), "is_dir": True}]
    previous = sdk_skills._symlink_trust_checker
    try:
        sdk_skills.set_symlink_trust_checker(None)
        assert sdk_skills._filter_skill_dirs(items) == []
    finally:
        sdk_skills.set_symlink_trust_checker(previous)


def test_filter_skill_dirs_checker_exception_refuses(tmp_path: Path) -> None:
    """A checker that raises must not grant access."""
    from bog_agents.middleware import skills as sdk_skills

    real = _make_skill_dir(tmp_path, "real").resolve()
    link = tmp_path / "linked"
    if not _try_symlink(link, real):
        pytest.skip("symlinks not permitted on this platform")

    items = [{"path": str(link), "is_dir": True}]

    def _boom(_p: str) -> bool:
        raise RuntimeError("checker blew up")

    previous = sdk_skills._symlink_trust_checker
    try:
        sdk_skills.set_symlink_trust_checker(_boom)
        assert sdk_skills._filter_skill_dirs(items) == []
    finally:
        sdk_skills.set_symlink_trust_checker(previous)
