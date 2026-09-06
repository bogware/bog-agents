"""Worktree environment reuse (ROADMAP #76): share `node_modules` / `.venv` across worktrees via a lockfile-keyed cache.

A fresh `git worktree` has no dependencies installed; installing them per
attempt is what makes parallel worktrees slow. With `[worktree] reuse =
["node_modules", ".venv"]` in `sandbox.toml`, each listed directory is keyed by
the hash of its lockfile (`package-lock.json`, `uv.lock`, …) and kept once
under `~/.bog-agents/envcache/<name>-<hash>/`; a new worktree gets a junction
(Windows) or symlink (POSIX) to it, seeded from the main checkout the first
time. When the lockfile differs the key differs, so a worktree never sees a
stale environment. Pure planning + small injectable filesystem steps.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

LOCKFILES: dict[str, tuple[str, ...]] = {
    "node_modules": (
        "package-lock.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "bun.lock",
        "bun.lockb",
        "package.json",
    ),
    ".venv": (
        "uv.lock",
        "poetry.lock",
        "Pipfile.lock",
        "requirements.txt",
        "pyproject.toml",
    ),
    "vendor": ("go.sum", "Gemfile.lock", "composer.lock"),
    "target": ("Cargo.lock",),
    ".gradle": ("gradle.lockfile", "build.gradle", "build.gradle.kts"),
}
GENERIC_LOCKFILES: tuple[str, ...] = (
    "package-lock.json",
    "uv.lock",
    "poetry.lock",
    "Cargo.lock",
    "go.sum",
    "Gemfile.lock",
)


def default_cache_root() -> Path:
    """`~/.bog-agents/envcache`."""
    try:
        from bog_agents_cli._env_vars import bog_agents_home

        return bog_agents_home() / "envcache"
    except Exception:
        return Path.home() / ".bog-agents" / "envcache"


def lock_hash(repo_dir: str | Path, name: str) -> tuple[str, str] | None:
    """`(lockfile name, short sha256)` for the first lockfile that keys `name`, or `None`."""
    repo = Path(repo_dir)
    for lockfile in LOCKFILES.get(name, ()):
        path = repo / lockfile
        if path.is_file():
            digest = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
            return lockfile, digest
    return None


@dataclass
class ReusePlan:
    """What to do for one reusable directory."""

    name: str
    action: str  # "link" | "seed-then-link" | "skip"
    reason: str = ""
    lockfile: str = ""
    key: str = ""
    cache_dir: Path | None = None
    source: Path | None = None
    target: Path | None = None
    notes: list[str] = field(default_factory=list)

    def describe(self) -> str:
        """One line."""
        if self.action == "skip":
            return f"{self.name}: skipped ({self.reason})"
        return f"{self.name}: {self.action} via {self.lockfile}@{self.key}"


def plan_reuse(
    repo_dir: str | Path,
    worktree_dir: str | Path,
    reuse: Sequence[str],
    *,
    cache_root: str | Path | None = None,
) -> list[ReusePlan]:
    """Decide, per directory, whether to link an existing cache entry, seed one from the main checkout, or skip."""
    repo, worktree = Path(repo_dir), Path(worktree_dir)
    root = Path(cache_root) if cache_root is not None else default_cache_root()
    plans: list[ReusePlan] = []
    for raw in reuse:
        name = raw.strip().strip("/").strip("\\")
        if not name or "/" in name or "\\" in name or name in {".", ".."}:
            plans.append(
                ReusePlan(name=raw, action="skip", reason="not a plain directory name")
            )
            continue
        target = worktree / name
        if target.exists() or target.is_symlink():
            plans.append(
                ReusePlan(
                    name=name,
                    action="skip",
                    reason="already present in the worktree",
                    target=target,
                )
            )
            continue
        keyed = lock_hash(worktree, name) or lock_hash(repo, name)
        if keyed is None:
            plans.append(
                ReusePlan(
                    name=name, action="skip", reason="no lockfile to key the cache on"
                )
            )
            continue
        lockfile, key = keyed
        cache_dir = root / f"{name}-{key}"
        source = repo / name
        if cache_dir.is_dir():
            plans.append(
                ReusePlan(
                    name=name,
                    action="link",
                    lockfile=lockfile,
                    key=key,
                    cache_dir=cache_dir,
                    target=target,
                )
            )
        elif source.is_dir() and not source.is_symlink():
            repo_keyed = lock_hash(repo, name)
            if repo_keyed is None or repo_keyed[1] != key:
                plans.append(
                    ReusePlan(
                        name=name,
                        action="skip",
                        reason="main checkout's lockfile differs from the worktree's",
                        lockfile=lockfile,
                        key=key,
                    )
                )
                continue
            plans.append(
                ReusePlan(
                    name=name,
                    action="seed-then-link",
                    lockfile=lockfile,
                    key=key,
                    cache_dir=cache_dir,
                    source=source,
                    target=target,
                )
            )
        else:
            plans.append(
                ReusePlan(
                    name=name,
                    action="skip",
                    reason="nothing cached and the main checkout has no such directory",
                    lockfile=lockfile,
                    key=key,
                )
            )
    return plans


def _link_or_copy(src: str, dst: str) -> None:
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def seed_cache(source: Path, cache_dir: Path) -> None:
    """Copy `source` into `cache_dir` (hardlinks where the filesystem allows), atomically via a temp dir."""
    tmp = cache_dir.with_name(cache_dir.name + ".partial")
    if tmp.exists():
        shutil.rmtree(tmp, ignore_errors=True)
    shutil.copytree(source, tmp, symlinks=True, copy_function=_link_or_copy)
    tmp.replace(cache_dir)


def link_dir(target: Path, cache_dir: Path) -> str:
    """Make `target` point at `cache_dir`: a junction on Windows, a symlink elsewhere; returns which."""
    target.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        import _winapi  # noqa: PLC2701 - the only junction API in the stdlib

        _winapi.CreateJunction(str(cache_dir), str(target))
        return "junction"
    target.symlink_to(cache_dir, target_is_directory=True)
    return "symlink"


def apply_reuse(
    plans: Sequence[ReusePlan],
    *,
    seed: Callable[[Path, Path], None] = seed_cache,
    link: Callable[[Path, Path], str] = link_dir,
) -> list[str]:
    """Execute the plans; a failure skips that directory with a note instead of failing the worktree."""
    notes: list[str] = []
    for plan in plans:
        if plan.action == "skip" or plan.cache_dir is None or plan.target is None:
            notes.append(plan.describe())
            continue
        try:
            if plan.action == "seed-then-link" and plan.source is not None:
                seed(plan.source, plan.cache_dir)
                plan.notes.append(f"seeded {plan.cache_dir} from {plan.source}")
            how = link(plan.target, plan.cache_dir)
            notes.append(
                f"{plan.name}: {how} → {plan.cache_dir}"
                + (f" ({'; '.join(plan.notes)})" if plan.notes else "")
            )
        except Exception as exc:
            logger.warning("envcache reuse failed for %s", plan.name, exc_info=True)
            notes.append(f"{plan.name}: reuse failed ({exc}); install normally")
    return notes


def reuse_into_worktree(
    repo_dir: str | Path,
    worktree_dir: str | Path,
    reuse: Sequence[str],
    *,
    cache_root: str | Path | None = None,
) -> list[str]:
    """Plan and apply in one call (what the worktree creation sites use)."""
    if not reuse:
        return []
    return apply_reuse(plan_reuse(repo_dir, worktree_dir, reuse, cache_root=cache_root))


def configured_reuse() -> tuple[str, ...]:
    """`[worktree] reuse` from `sandbox.toml` (empty when unset or unreadable)."""
    try:
        from bog_agents_cli.sandbox_config import load_sandbox_config

        return tuple(load_sandbox_config().worktree_reuse)
    except Exception:
        return ()


__all__ = [
    "LOCKFILES",
    "ReusePlan",
    "apply_reuse",
    "configured_reuse",
    "default_cache_root",
    "link_dir",
    "lock_hash",
    "plan_reuse",
    "reuse_into_worktree",
    "seed_cache",
]
