"""`/scratch` — ephemeral git worktrees with isolated venvs.

A *scratch* is a throwaway working directory:
  * created via ``git worktree add`` off a fresh branch,
  * optionally seeded with an isolated venv (``python -m venv``),
  * tracked in ``~/.bog-agents/scratches/index.json`` so you can list,
    enter, or drop them from anywhere.

The point is to make "let me try a wild idea without messing up my
main checkout" a single command. Drop the scratch when you're done
and the worktree, branch, venv, and index entry all vanish.

This module is git-shell-heavy by necessity but every subprocess call
is hardened: no ``shell=True``, controlled argv, bounded timeout,
errors surfaced as clean :class:`ScratchError` instances rather than
raw ``CalledProcessError``.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess  # noqa: S404 — controlled git/venv invocation only
import sys
import time
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from pathlib import Path

from bog_agents_cli.feature_helpers import feature_state_dir

logger = logging.getLogger(__name__)


class ScratchError(RuntimeError):
    """User-facing scratch subsystem failure."""


_INDEX_FILENAME = "index.json"


@dataclass
class ScratchEntry:
    """One scratch worktree record."""

    scratch_id: str
    """Short slug used in commands (e.g. ``abc12345``)."""

    label: str
    """User-supplied tag (e.g. ``"try pydantic v3"``)."""

    branch: str
    """Git branch backing the worktree (typically ``scratch/<id>``)."""

    path: str
    """Absolute path to the worktree directory."""

    venv_path: str = ""
    """Path to the ephemeral venv if one was created."""

    parent_repo: str = ""
    """Absolute path to the repository root the scratch was forked off."""

    created_at: float = 0.0

    def to_dict(self) -> dict[str, str | float]:
        """Serialise to a plain JSON-safe dict (used by the persisted index)."""
        return asdict(self)


@dataclass
class ScratchIndex:
    """In-memory index of all scratches on this machine."""

    entries: list[ScratchEntry] = field(default_factory=list)

    def find(self, scratch_id: str) -> ScratchEntry | None:
        """Look up a scratch by exact id or a unique prefix; None if no match."""
        scratch_id = scratch_id.strip()
        if not scratch_id:
            return None
        for e in self.entries:
            if e.scratch_id == scratch_id:
                return e
        # Tolerate a short prefix (one of the entry ids starts with arg).
        candidates = [e for e in self.entries if e.scratch_id.startswith(scratch_id)]
        if len(candidates) == 1:
            return candidates[0]
        return None


# --------------------------------------------------------------------------- #
# Persistence                                                                 #
# --------------------------------------------------------------------------- #


def scratch_dir() -> Path:
    """Root of all scratch worktrees + their index."""
    path = feature_state_dir() / "scratches"
    with suppress(OSError):
        path.mkdir(parents=True, exist_ok=True)
    return path


def index_path() -> Path:
    """Path to the on-disk scratch index file."""
    return scratch_dir() / _INDEX_FILENAME


def load_index() -> ScratchIndex:
    """Read the on-disk index. Returns an empty index on any failure."""
    target = index_path()
    if not target.exists():
        return ScratchIndex()
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        logger.warning("Failed to load scratch index; treating as empty", exc_info=True)
        return ScratchIndex()
    raw_entries = data.get("entries", []) if isinstance(data, dict) else []
    entries: list[ScratchEntry] = []
    for raw in raw_entries:
        if not isinstance(raw, dict):
            continue
        try:
            entries.append(
                ScratchEntry(
                    scratch_id=str(raw["scratch_id"]),
                    label=str(raw.get("label", "")),
                    branch=str(raw["branch"]),
                    path=str(raw["path"]),
                    venv_path=str(raw.get("venv_path", "")),
                    parent_repo=str(raw.get("parent_repo", "")),
                    created_at=float(raw.get("created_at", 0.0)),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return ScratchIndex(entries=entries)


def save_index(index: ScratchIndex) -> None:
    """Write the index atomically (tmp+rename)."""
    target = index_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    payload = {"entries": [e.to_dict() for e in index.entries]}
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(target)


# --------------------------------------------------------------------------- #
# Worktree + venv management                                                  #
# --------------------------------------------------------------------------- #


def _short_id() -> str:
    """Return a short, filesystem-safe id."""
    import secrets

    return secrets.token_hex(4)


def _repo_root(cwd: Path) -> Path:
    """Resolve the git repo root containing ``cwd``.

    Raises:
        ScratchError: When ``cwd`` is not inside a git checkout.
    """
    result = _git(["rev-parse", "--show-toplevel"], cwd=cwd)
    if not result:
        msg = f"{cwd} is not inside a git repository — /scratch requires a git checkout"
        raise ScratchError(msg)
    return Path(result)


def _git(args: list[str], *, cwd: Path) -> str:
    """Run a git subcommand, return stdout.

    Raises:
        ScratchError: When git is missing, times out, or exits non-zero.
    """
    try:
        result = subprocess.run(  # noqa: S603 — controlled argv
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env={
                **os.environ,
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_OPTIONAL_LOCKS": "0",
            },
        )
    except FileNotFoundError as exc:
        msg = "git executable not found on PATH"
        raise ScratchError(msg) from exc
    except subprocess.TimeoutExpired as exc:
        msg = f"git {args[0]} timed out after 30s"
        raise ScratchError(msg) from exc
    if result.returncode != 0:
        msg = (
            f"git {' '.join(args)} failed (exit {result.returncode}): "
            f"{(result.stderr or '').strip()[:400]}"
        )
        raise ScratchError(msg)
    return (result.stdout or "").strip()


def create_scratch(
    label: str,
    *,
    cwd: Path | None = None,
    base_branch: str = "",
    create_venv: bool = False,
) -> ScratchEntry:
    """Create a new scratch worktree.

    Args:
        label: Free-text tag for what you're trying (shown in
            ``/scratch list``). Empty strings are tolerated.
        cwd: Path inside the repo to fork from. Defaults to ``Path.cwd()``.
        base_branch: Branch to fork off. Defaults to the current HEAD.
        create_venv: When True, run ``python -m venv`` inside the new
            worktree so ``pip install`` doesn't pollute the parent.

    Returns:
        The new :class:`ScratchEntry`, already persisted to the index.

    Raises:
        ScratchError: If git operations or venv creation fail.
    """
    base = cwd or Path.cwd()
    repo_root = _repo_root(base)

    if not base_branch:
        head = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_root)
        base_branch = head if head and head != "HEAD" else "HEAD"

    scratch_id = _short_id()
    branch_name = f"scratch/{scratch_id}"
    worktree_path = scratch_dir() / scratch_id

    # ``git worktree add -b <branch> <path> <base>`` creates the branch
    # AND checks it out into the new worktree atomically.
    _git(
        ["worktree", "add", "-b", branch_name, str(worktree_path), base_branch],
        cwd=repo_root,
    )

    venv_path = ""
    if create_venv:
        venv_path = str(worktree_path / ".venv")
        try:
            subprocess.run(  # noqa: S603 — controlled argv
                [sys.executable, "-m", "venv", venv_path],
                check=True,
                capture_output=True,
                text=True,
                timeout=120,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            # Roll back the worktree on venv failure.
            with suppress(ScratchError):
                _git(
                    ["worktree", "remove", "--force", str(worktree_path)],
                    cwd=repo_root,
                )
            stderr = getattr(exc, "stderr", "") or ""
            msg = f"venv creation failed: {str(stderr)[:300] or exc}"
            raise ScratchError(msg) from exc

    entry = ScratchEntry(
        scratch_id=scratch_id,
        label=label,
        branch=branch_name,
        path=str(worktree_path),
        venv_path=venv_path,
        parent_repo=str(repo_root),
        created_at=time.time(),
    )
    index = load_index()
    index.entries.append(entry)
    save_index(index)
    return entry


def drop_scratch(scratch_id: str) -> ScratchEntry:
    """Remove a scratch's worktree, branch, venv, and index entry.

    Returns the entry that was dropped (so callers can show what they did).

    Raises:
        ScratchError: When the scratch can't be found or git refuses
            to remove the worktree (usually because it has uncommitted
            changes — in that case re-run after ``/scratch enter`` +
            stash).
    """
    index = load_index()
    entry = index.find(scratch_id)
    if entry is None:
        msg = f"no scratch found for id {scratch_id!r}"
        raise ScratchError(msg)

    if entry.venv_path and Path(entry.venv_path).exists():
        # Remove the venv first so it's gone even if the worktree
        # removal complains about uncommitted state.
        with suppress(OSError):
            shutil.rmtree(entry.venv_path, ignore_errors=True)

    repo_root = Path(entry.parent_repo)
    if repo_root.exists():
        _git(["worktree", "remove", "--force", entry.path], cwd=repo_root)
        with suppress(ScratchError):
            _git(["branch", "-D", entry.branch], cwd=repo_root)

    # Even if git failed (e.g. repo moved) we still drop the index
    # entry — leaving it would just confuse the user.
    index.entries = [e for e in index.entries if e.scratch_id != entry.scratch_id]
    save_index(index)

    # Worktree dir might still exist if `git worktree remove` partially
    # succeeded. Best-effort cleanup.
    if Path(entry.path).exists():
        with suppress(OSError):
            shutil.rmtree(entry.path, ignore_errors=True)
    return entry


def drop_all_scratches() -> list[ScratchEntry]:
    """Drop every scratch. Continues on individual failures."""
    index = load_index()
    dropped: list[ScratchEntry] = []
    for entry in list(index.entries):
        try:
            dropped.append(drop_scratch(entry.scratch_id))
        except ScratchError:
            logger.warning("Could not drop scratch %s", entry.scratch_id, exc_info=True)
    return dropped


# --------------------------------------------------------------------------- #
# App handler glue                                                            #
# --------------------------------------------------------------------------- #


def _format_entry(entry: ScratchEntry) -> str:
    age = ""
    if entry.created_at:
        seconds = max(0.0, time.time() - entry.created_at)
        if seconds < 60:
            age = f"{int(seconds)}s ago"
        elif seconds < 3600:
            age = f"{int(seconds / 60)}m ago"
        else:
            age = f"{int(seconds / 3600)}h ago"
    parts = [
        f"[bold]{entry.scratch_id}[/bold]",
        f"[cyan]{entry.label or '(no label)'}[/cyan]",
        f"[dim]{entry.branch}[/dim]",
        f"[dim]{entry.path}[/dim]",
    ]
    if entry.venv_path:
        parts.append("[green]venv[/green]")
    if age:
        parts.append(f"[dim]{age}[/dim]")
    return "  ".join(parts)


async def handle_scratch_subcommand(app: object, raw_arg: str) -> None:
    """Dispatch ``/scratch <sub>`` subcommands."""
    import asyncio

    from bog_agents_cli.widgets.chat_messages import AppMessage, ErrorMessage

    arg = raw_arg.strip()
    head, _, rest = arg.partition(" ")
    head = head.lower()
    rest = rest.strip()

    if not head or head == "list":
        index = load_index()
        if not index.entries:
            await app._mount_message(  # type: ignore[attr-defined]
                AppMessage(
                    "[dim]No scratches yet.[/dim]\n"
                    'Create one with [bold]/scratch new "try X"[/bold].'
                )
            )
            return
        lines = [f"[bold]{len(index.entries)} active scratches[/bold]\n"]
        for entry in index.entries:
            lines.append(_format_entry(entry))
        await app._mount_message(AppMessage("\n".join(lines)))  # type: ignore[attr-defined]
        return

    if head == "new":
        label = rest
        create_venv = False
        # Recognise an optional ``--venv`` flag.
        if "--venv" in label.split():
            create_venv = True
            label = " ".join(p for p in label.split() if p != "--venv").strip()

        await app._set_spinner("Creating scratch")  # type: ignore[attr-defined]
        try:
            cwd = Path(getattr(app, "_cwd", Path.cwd()))
            entry = await asyncio.to_thread(
                create_scratch, label, cwd=cwd, create_venv=create_venv
            )
        except ScratchError as exc:
            await app._set_spinner("")  # type: ignore[attr-defined]
            await app._mount_message(ErrorMessage(f"/scratch new: {exc}"))  # type: ignore[attr-defined]
            return
        await app._set_spinner("")  # type: ignore[attr-defined]

        msg = (
            f"[bold]Scratch created[/bold]\n"
            f"  id: [bold]{entry.scratch_id}[/bold]\n"
            f"  label: {entry.label or '(none)'}\n"
            f"  branch: [cyan]{entry.branch}[/cyan]\n"
            f"  path: [cyan]{entry.path}[/cyan]\n"
        )
        if entry.venv_path:
            msg += f"  venv: [green]{entry.venv_path}[/green]\n"
        msg += f"\nEnter it: [bold]/scratch enter {entry.scratch_id}[/bold]"
        await app._mount_message(AppMessage(msg))  # type: ignore[attr-defined]
        return

    if head == "enter":
        target = rest or ""
        if not target:
            await app._mount_message(  # type: ignore[attr-defined]
                ErrorMessage("Usage: /scratch enter <id>")
            )
            return
        index = load_index()
        entry = index.find(target)
        if entry is None:
            await app._mount_message(  # type: ignore[attr-defined]
                ErrorMessage(f"No scratch found for id {target!r}")
            )
            return
        new_cwd = Path(entry.path)
        # Path.exists is a sync stat() call — fine in a slash-command
        # handler. ruff's ASYNC240 would prefer trio.Path here but we
        # are not using anyio/trio anywhere else in the CLI.
        if not new_cwd.exists():  # noqa: ASYNC240
            await app._mount_message(  # type: ignore[attr-defined]
                ErrorMessage(
                    f"Scratch path {entry.path} no longer exists — "
                    f"try /scratch drop {entry.scratch_id} to clean up the index"
                )
            )
            return
        # Update the app's effective cwd. Other state (model, settings)
        # is unaffected; only the working directory changes.
        try:
            app._cwd = new_cwd  # type: ignore[attr-defined]
        except Exception:
            logger.warning("Could not set app._cwd")
        await app._mount_message(  # type: ignore[attr-defined]
            AppMessage(
                f"[bold]Entered scratch[/bold] [bold]{entry.scratch_id}[/bold] — "
                f"working dir is now [cyan]{entry.path}[/cyan]"
            )
        )
        return

    if head == "drop":
        rest = rest.strip()
        if rest in {"--all", "-a", "all"}:
            await app._set_spinner("Dropping all scratches")  # type: ignore[attr-defined]
            dropped = await asyncio.to_thread(drop_all_scratches)
            await app._set_spinner("")  # type: ignore[attr-defined]
            if not dropped:
                await app._mount_message(  # type: ignore[attr-defined]
                    AppMessage("[dim]No scratches to drop.[/dim]")
                )
                return
            await app._mount_message(  # type: ignore[attr-defined]
                AppMessage(f"[bold]Dropped {len(dropped)} scratches.[/bold]")
            )
            return
        if not rest:
            await app._mount_message(  # type: ignore[attr-defined]
                ErrorMessage("Usage: /scratch drop <id>  or  /scratch drop --all")
            )
            return
        await app._set_spinner("Dropping scratch")  # type: ignore[attr-defined]
        try:
            entry = await asyncio.to_thread(drop_scratch, rest)
        except ScratchError as exc:
            await app._set_spinner("")  # type: ignore[attr-defined]
            await app._mount_message(  # type: ignore[attr-defined]
                ErrorMessage(f"/scratch drop: {exc}")
            )
            return
        await app._set_spinner("")  # type: ignore[attr-defined]
        await app._mount_message(  # type: ignore[attr-defined]
            AppMessage(
                f"[bold]Dropped scratch[/bold] [bold]{entry.scratch_id}[/bold] "
                f"({entry.label or 'no label'})"
            )
        )
        return

    await app._mount_message(  # type: ignore[attr-defined]
        AppMessage(
            "Usage:\n"
            '  /scratch new "label"    Create a new scratch\n'
            "  /scratch new --venv     Create with an isolated venv\n"
            "  /scratch list           List active scratches\n"
            "  /scratch enter <id>     Set the active cwd to a scratch\n"
            "  /scratch drop <id>      Delete a scratch\n"
            "  /scratch drop --all     Delete all scratches"
        )
    )
