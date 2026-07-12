"""Tests for the `/skills trust` command controller.

Pure-logic: no TUI. Verifies routing (trust/revoke/clear/list), the "not a
trust subcommand -> None" fall-through, and the round-trip through the store.
"""

from __future__ import annotations

from pathlib import Path

from bog_agents_cli import skill_trust
from bog_agents_cli.skill_trust_controller import handle_skills_command


def _store(tmp_path: Path) -> Path:
    return tmp_path / ".state" / "skill_trust.json"


def _make_skill_dir(root: Path, name: str) -> Path:
    d = root / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: demo\n---\n", encoding="utf-8")
    return d


def test_non_trust_command_falls_through() -> None:
    assert handle_skills_command("/skills") is None
    assert (
        handle_skills_command("/skills list") is None
    )  # `list` alone is the summary path


def test_trust_list_empty(tmp_path: Path) -> None:
    msg = handle_skills_command("/skills trust list", store_path=_store(tmp_path))
    assert msg is not None
    assert "No skill directories are trusted" in msg


def test_trust_bare_is_list(tmp_path: Path) -> None:
    msg = handle_skills_command("/skills trust", store_path=_store(tmp_path))
    assert msg is not None
    assert "trusted" in msg.lower()


def test_trust_a_dir_then_list(tmp_path: Path) -> None:
    store = _store(tmp_path)
    d = _make_skill_dir(tmp_path, "sk")

    msg = handle_skills_command(f"/skills trust {d}", store_path=store)
    assert msg is not None
    assert "Trusted skill directory" in msg
    assert str(d.resolve()) in skill_trust.list_trusted_skill_dirs(store_path=store)

    listed = handle_skills_command("/skills trust list", store_path=store)
    assert str(d.resolve()) in listed


def test_trust_nonexistent_dir_errors(tmp_path: Path) -> None:
    msg = handle_skills_command(
        f"/skills trust {tmp_path / 'nope'}", store_path=_store(tmp_path)
    )
    assert msg is not None
    assert "Not a directory" in msg


def test_trust_dir_without_skill_md_errors(tmp_path: Path) -> None:
    d = tmp_path / "empty"
    d.mkdir()
    msg = handle_skills_command(f"/skills trust {d}", store_path=_store(tmp_path))
    assert "No SKILL.md" in msg


def test_revoke_round_trip(tmp_path: Path) -> None:
    store = _store(tmp_path)
    d = _make_skill_dir(tmp_path, "sk")
    handle_skills_command(f"/skills trust {d}", store_path=store)

    msg = handle_skills_command(f"/skills trust revoke {d}", store_path=store)
    assert "Revoked trust" in msg
    assert skill_trust.list_trusted_skill_dirs(store_path=store) == []


def test_revoke_missing_reports_not_found(tmp_path: Path) -> None:
    d = _make_skill_dir(tmp_path, "sk")
    msg = handle_skills_command(
        f"/skills trust revoke {d}", store_path=_store(tmp_path)
    )
    assert "No trust entry matched" in msg


def test_revoke_without_path_shows_usage(tmp_path: Path) -> None:
    msg = handle_skills_command("/skills trust revoke", store_path=_store(tmp_path))
    assert "Usage:" in msg


def test_clear_round_trip(tmp_path: Path) -> None:
    store = _store(tmp_path)
    d = _make_skill_dir(tmp_path, "sk")
    handle_skills_command(f"/skills trust {d}", store_path=store)

    msg = handle_skills_command("/skills trust clear", store_path=store)
    assert "Cleared all trusted skill directories" in msg
    assert skill_trust.list_trusted_skill_dirs(store_path=store) == []


def test_list_reports_unreadable_store(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.parent.mkdir(parents=True)
    store.write_text("}}not json", encoding="utf-8")
    msg = handle_skills_command("/skills trust list", store_path=store)
    assert "Could not read the skill trust store" in msg
