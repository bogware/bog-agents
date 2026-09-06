"""ROADMAP #76: worktree environment reuse via the lockfile-keyed cache."""

from __future__ import annotations

from pathlib import Path

from bog_agents_cli import envcache as ec


def _repo(tmp_path: Path) -> tuple[Path, Path, Path]:
    repo = tmp_path / "repo"
    worktree = tmp_path / "wt"
    cache = tmp_path / "cache"
    repo.mkdir()
    worktree.mkdir()
    (repo / "package-lock.json").write_text('{"v": 1}', encoding="utf-8")
    (worktree / "package-lock.json").write_text('{"v": 1}', encoding="utf-8")
    (repo / "node_modules" / "left-pad").mkdir(parents=True)
    (repo / "node_modules" / "left-pad" / "index.js").write_text("module.exports = 1", encoding="utf-8")
    return repo, worktree, cache


def test_lock_hash_and_plans(tmp_path: Path) -> None:
    repo, worktree, cache = _repo(tmp_path)
    assert ec.lock_hash(repo, "node_modules") == ("package-lock.json", ec.lock_hash(worktree, "node_modules")[1])  # type: ignore[index]
    assert ec.lock_hash(repo, ".venv") is None
    plans = ec.plan_reuse(repo, worktree, ["node_modules", ".venv", "../evil", ""], cache_root=cache)
    assert [p.action for p in plans] == ["seed-then-link", "skip", "skip", "skip"]
    assert plans[0].cache_dir == cache / f"node_modules-{plans[0].key}" and "no lockfile" in plans[1].reason
    assert "not a plain" in plans[2].reason and "node_modules: seed-then-link via package-lock.json@" in plans[0].describe()

    (worktree / "package-lock.json").write_text('{"v": 2}', encoding="utf-8")
    plans = ec.plan_reuse(repo, worktree, ["node_modules"], cache_root=cache)
    assert plans[0].action == "skip" and "differs" in plans[0].reason


def test_apply_seed_link_and_reuse(tmp_path: Path) -> None:
    repo, worktree, cache = _repo(tmp_path)
    linked: list[tuple[Path, Path]] = []

    def _link(target: Path, cache_dir: Path) -> str:
        linked.append((target, cache_dir))
        target.mkdir(parents=True)
        (target / ".linked").write_text(str(cache_dir), encoding="utf-8")
        return "fake-link"

    notes = ec.apply_reuse(ec.plan_reuse(repo, worktree, ["node_modules"], cache_root=cache), link=_link)
    assert len(linked) == 1 and "fake-link" in notes[0] and "seeded" in notes[0]
    cache_dir = linked[0][1]
    assert (cache_dir / "left-pad" / "index.js").read_text(encoding="utf-8") == "module.exports = 1"
    assert not cache_dir.with_name(cache_dir.name + ".partial").exists()

    # A second worktree with the same lockfile links straight from the cache.
    second = tmp_path / "wt2"
    second.mkdir()
    (second / "package-lock.json").write_text('{"v": 1}', encoding="utf-8")
    plans = ec.plan_reuse(repo, second, ["node_modules"], cache_root=cache)
    assert plans[0].action == "link"
    notes = ec.apply_reuse(plans, link=_link)
    assert len(linked) == 2 and "seeded" not in notes[0]
    assert ec.apply_reuse(ec.plan_reuse(repo, second, ["node_modules"], cache_root=cache), link=_link)[0].startswith("node_modules: skipped (already present")

    def _boom(_t: Path, _c: Path) -> str:
        msg = "no junctions here"
        raise OSError(msg)

    third = tmp_path / "wt3"
    third.mkdir()
    (third / "package-lock.json").write_text('{"v": 1}', encoding="utf-8")
    assert "install normally" in ec.apply_reuse(ec.plan_reuse(repo, third, ["node_modules"], cache_root=cache), link=_boom)[0]
    assert ec.reuse_into_worktree(repo, third, [], cache_root=cache) == []


def test_real_link_dir(tmp_path: Path) -> None:
    cache_dir = tmp_path / "cache" / "x"
    cache_dir.mkdir(parents=True)
    (cache_dir / "f").write_text("1", encoding="utf-8")
    target = tmp_path / "wt" / "x"
    how = ec.link_dir(target, cache_dir)
    assert how in {"junction", "symlink"} and (target / "f").read_text(encoding="utf-8") == "1"
