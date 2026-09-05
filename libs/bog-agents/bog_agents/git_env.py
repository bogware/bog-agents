"""Hostile-repo hardening for every internal git call (ROADMAP #49).

A cloned repository controls its own `.git/config`, and git will happily run
whatever that file names: `core.fsmonitor` (the Goose GHSA-r5pp-p5r8-466r
RCE), `core.hooksPath`, `core.pager`, `core.sshCommand`, `diff.external`,
`credential.helper = !cmd`, shell aliases, custom filters. Every git command
bog runs on the user's behalf — evidence, worktrees, checkpoints, `/diff`,
PR creation, index building — therefore goes through `hardened_git_env()`,
which uses git's `GIT_CONFIG_COUNT` override mechanism (highest precedence,
git >= 2.31) to pin those keys. The pinned value is whatever the *trusted*
scopes (system + global config, which the repo cannot edit) say, or an inert
default when they say nothing — so a global `credential.helper = manager`
keeps working while a repo-level `credential.helper = !curl …` never runs.
Editors and the pager are always inert: an internal call must never open one.
External diff programs are the one thing a config override cannot switch
off, so patch-producing diffs pass `NO_EXTERNAL_DIFF` instead.
External diff programs are the one thing a config override cannot switch
off, so patch-producing diffs pass `NO_EXTERNAL_DIFF` instead.
External diff programs are the one thing a config override cannot switch
off, so patch-producing diffs pass `NO_EXTERNAL_DIFF` instead.
`scan_repo_config()` lets the CLI block the review surfaces until the user
has acknowledged what the repo's config would have executed.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

HARDENED_GIT_CONFIG: tuple[tuple[str, str], ...] = (
    ("core.fsmonitor", "false"),
    ("core.hooksPath", "__BOG_NO_HOOKS__"),  # replaced with an empty directory at call time
    ("core.pager", "cat"),
    ("core.sshCommand", "ssh"),
    ("core.editor", "true"),
    ("core.askPass", ""),
    ("core.alternateRefsCommand", ""),
    ("credential.helper", ""),
    ("gpg.program", "gpg"),
    ("sequence.editor", "true"),
)
"""Repo-config keys that can execute code, with the inert value used when no trusted scope sets them."""

ALWAYS_INERT: frozenset[str] = frozenset({"core.editor", "sequence.editor", "core.pager"})
"""Keys pinned to the inert value even when the user's own config sets them (internal calls never open an editor/pager)."""

NO_EXTERNAL_DIFF: tuple[str, ...] = ("--no-ext-diff", "--no-textconv")
"""Flags for every patch-producing `git diff`: `diff.external` and `diff.<driver>.command|textconv` cannot be
neutralised through a config override (git spawns even an empty value, verified on 2.44), so the diff calls
bog runs itself opt out explicitly; `scan_repo_config` still reports the keys so the user sees them."""

NO_EXTERNAL_DIFF: tuple[str, ...] = ("--no-ext-diff", "--no-textconv")
"""Flags for every patch-producing `git diff`: `diff.external` and `diff.<driver>.command|textconv` cannot be
neutralised through a config override (git spawns even an empty value, verified on 2.44), so the diff calls
bog runs itself opt out explicitly; `scan_repo_config` still reports the keys so the user sees them."""

NO_EXTERNAL_DIFF: tuple[str, ...] = ("--no-ext-diff", "--no-textconv")
"""Flags for every patch-producing `git diff`: `diff.external` and `diff.<driver>.command|textconv` cannot be
neutralised through a config override (git spawns even an empty value, verified on 2.44), so the diff calls
bog runs itself opt out explicitly; `scan_repo_config` still reports the keys so the user sees them."""

_MULTI_VALUED: frozenset[str] = frozenset({"credential.helper"})
_DISCOVERY_TIMEOUT = 10.0
_EMPTY_HOOKS_DIR: Path | None = None
_TRUSTED_CONFIG: dict[str, list[str]] | None = None


def _empty_hooks_dir() -> Path:
    """An always-empty directory to point `core.hooksPath` at (created once per process)."""
    global _EMPTY_HOOKS_DIR  # noqa: PLW0603 - process-wide cache
    if _EMPTY_HOOKS_DIR is None or not _EMPTY_HOOKS_DIR.is_dir():
        path = Path(tempfile.gettempdir()) / "bog-agents-no-hooks"
        path.mkdir(parents=True, exist_ok=True)
        _EMPTY_HOOKS_DIR = path
    return _EMPTY_HOOKS_DIR


def parse_null_config(blob: str) -> dict[str, list[str]]:
    """Parse `git config --list --null` output into `{lowercased key: [values]}` (config-file order)."""
    out: dict[str, list[str]] = {}
    for entry in blob.split("\0"):
        if not entry:
            continue
        key, _, value = entry.partition("\n")
        out.setdefault(key.lower(), []).append(value)
    return out


def _discover_trusted_config(base: Mapping[str, str]) -> dict[str, list[str]]:
    """Read the system + global git config (the scopes a cloned repo cannot write).

    Runs `git config --<scope> --list --null` outside any repository. Any
    failure (no git, a missing file, a timeout) yields an empty mapping, which
    makes every pinned key fall back to its inert value — fail closed.
    """
    env = {k: v for k, v in base.items() if k not in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR")}
    env["GIT_TERMINAL_PROMPT"] = "0"
    merged: dict[str, list[str]] = {}
    for scope in ("--system", "--global"):
        try:
            result = subprocess.run(  # noqa: S603 - fixed argv
                ["git", "config", scope, "--list", "--null"],  # noqa: S607 - git from PATH by design
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=_DISCOVERY_TIMEOUT,
                cwd=tempfile.gettempdir(),
                env=env,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode != 0:
            continue
        for key, values in parse_null_config(result.stdout).items():
            merged.setdefault(key, []).extend(values)
    return merged


def trusted_git_config(base: Mapping[str, str] | None = None) -> dict[str, list[str]]:
    """The system + global git config, discovered once per process (see `reset_trusted_config_cache`)."""
    global _TRUSTED_CONFIG  # noqa: PLW0603 - process-wide cache
    if _TRUSTED_CONFIG is None:
        _TRUSTED_CONFIG = _discover_trusted_config(os.environ if base is None else base)
    return _TRUSTED_CONFIG


def reset_trusted_config_cache(preset: dict[str, list[str]] | None = None) -> None:
    """Drop (or replace, for tests) the cached trusted config."""
    global _TRUSTED_CONFIG  # noqa: PLW0603 - process-wide cache
    _TRUSTED_CONFIG = preset


def pinned_git_config(base: Mapping[str, str] | None = None) -> list[tuple[str, str]]:
    """The `(key, value)` overrides `hardened_git_env` applies, in `GIT_CONFIG_*` order.

    Multi-valued keys (`credential.helper`) are reset with an empty entry
    first so repo-level helpers are dropped before the trusted ones are
    re-added; single-valued keys take the last trusted value or the inert one.
    """
    trusted = trusted_git_config(base)
    hooks_dir = str(_empty_hooks_dir())
    pinned: list[tuple[str, str]] = []
    for key, inert in HARDENED_GIT_CONFIG:
        inert_value = hooks_dir if inert == "__BOG_NO_HOOKS__" else inert
        values = [] if key in ALWAYS_INERT else trusted.get(key.lower(), [])
        if key in _MULTI_VALUED:
            pinned.append((key, ""))
            pinned.extend((key, value) for value in values if value)
        elif values:
            pinned.append((key, values[-1]))
        else:
            pinned.append((key, inert_value))
    return pinned


def hardened_git_env(base: Mapping[str, str] | None = None, *, extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return an environment for running git that ignores the repo's code-executing config.

    Args:
        base: Environment to start from (`os.environ` when `None`).
        extra: Variables to add on top (e.g. `GIT_INDEX_FILE`).

    Returns:
        A new environment dict with terminal prompts off, optional locks off
        (internal calls never rewrite the index as a side effect), and every
        key in `HARDENED_GIT_CONFIG` pinned through `GIT_CONFIG_COUNT`
        (appended after any overrides already present, so they win).
    """
    env = dict(os.environ if base is None else base)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.setdefault("GIT_OPTIONAL_LOCKS", "0")
    try:
        start = int(env.get("GIT_CONFIG_COUNT", "0") or 0)
    except ValueError:
        start = 0
    pinned = pinned_git_config(base)
    for offset, (key, value) in enumerate(pinned):
        index = start + offset
        env[f"GIT_CONFIG_KEY_{index}"] = key
        env[f"GIT_CONFIG_VALUE_{index}"] = value
    env["GIT_CONFIG_COUNT"] = str(start + len(pinned))
    if extra:
        env.update({str(k): str(v) for k, v in extra.items()})
    return env


# ------------------------------------------------------------------ repo config scan


@dataclass(frozen=True)
class RepoConfigFinding:
    """One `.git/config` entry that would execute code or redirect git."""

    key: str
    value: str
    reason: str


_MIN_QUOTED_LEN = 2
_SECTION_RE = re.compile(r'^\s*\[\s*([A-Za-z0-9.-]+)(?:\s+"((?:[^"\\]|\\.)*)")?\s*\]\s*$')
_KV_RE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9-]*)\s*=\s*(.*)$")
_ALWAYS_SUSPICIOUS: dict[str, str] = {
    "core.fsmonitor": "runs a program on every git status/diff (RCE via cloned config)",
    "core.hookspath": "points git hooks at repo-controlled scripts that run on commit/checkout",
    "core.pager": "runs a program with every paged output",
    "core.sshcommand": "replaces ssh for every fetch/push",
    "core.editor": "runs a program when git opens an editor",
    "core.askpass": "runs a program for credential prompts",
    "core.alternaterefscommand": "runs a program during fetch negotiation",
    "diff.external": "runs a program for every diff",
    "gpg.program": "runs a program to sign/verify commits",
    "sequence.editor": "runs a program during rebase",
    "uploadpack.packobjectshook": "runs a program on fetch",
}


def parse_git_config(text: str) -> list[tuple[str, str]]:
    """Parse git's INI dialect into `(section.subsection.key, value)` pairs (lowercased keys)."""
    pairs: list[tuple[str, str]] = []
    section = ""
    for raw in text.splitlines():
        line = raw.split(";", 1)[0].split("#", 1)[0].rstrip() if not raw.lstrip().startswith(("#", ";")) else ""
        if not line.strip():
            continue
        match = _SECTION_RE.match(line)
        if match:
            name = match.group(1).lower()
            sub = match.group(2)
            section = f"{name}.{sub}" if sub is not None else name
            continue
        kv = _KV_RE.match(line)
        if kv and section:
            value = kv.group(2).strip()
            if len(value) >= _MIN_QUOTED_LEN and value[0] == value[-1] == '"':
                value = value[1:-1]
            pairs.append((f"{section}.{kv.group(1).lower()}", value))
    return pairs


def _findings_for(pairs: list[tuple[str, str]]) -> list[RepoConfigFinding]:
    out: list[RepoConfigFinding] = []
    for key, value in pairs:
        lowered = key.lower()
        if lowered in _ALWAYS_SUSPICIOUS:
            if lowered == "core.fsmonitor" and value.lower() in {"false", "0", "no", "off", ""}:
                continue
            out.append(RepoConfigFinding(key, value, _ALWAYS_SUSPICIOUS[lowered]))
        elif lowered == "credential.helper" and (value.startswith("!") or "/" in value or "\\" in value):
            out.append(RepoConfigFinding(key, value, "runs a repo-chosen credential helper"))
        elif lowered.startswith("alias.") and value.startswith("!"):
            out.append(RepoConfigFinding(key, value, "shell alias runs a program when the alias is used"))
        elif lowered.startswith("filter.") and lowered.endswith((".clean", ".smudge", ".process", ".required")):
            if not value.lower().startswith(("git-lfs", "git lfs")) and lowered.endswith((".clean", ".smudge", ".process")):
                out.append(RepoConfigFinding(key, value, "content filter runs a program on checkout/commit"))
        elif lowered.startswith("diff.") and lowered.endswith((".command", ".textconv")):
            out.append(RepoConfigFinding(key, value, "diff driver runs a program"))
        elif lowered.startswith("merge.") and lowered.endswith(".driver"):
            out.append(RepoConfigFinding(key, value, "merge driver runs a program"))
        elif lowered.startswith("remote.") and lowered.endswith((".vcs", ".uploadpack", ".receivepack")):
            out.append(RepoConfigFinding(key, value, "remote helper / pack program override"))
        elif lowered.startswith("url.") and lowered.endswith(".insteadof"):
            out.append(RepoConfigFinding(key, value, "URL rewrite can redirect fetches/pushes"))
    return out


def repo_config_path(repo_dir: str | Path) -> Path | None:
    """Locate the repo's `config` file (handles worktree `.git` pointer files)."""
    dot_git = Path(repo_dir) / ".git"
    if dot_git.is_dir():
        candidate = dot_git / "config"
        return candidate if candidate.is_file() else None
    if dot_git.is_file():
        try:
            pointer = dot_git.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        if pointer.startswith("gitdir:"):
            git_dir = Path(pointer[len("gitdir:") :].strip())
            if not git_dir.is_absolute():
                git_dir = (Path(repo_dir) / git_dir).resolve()
            common = git_dir / "commondir"
            if common.is_file():
                with contextlib.suppress(OSError):
                    git_dir = (git_dir / common.read_text(encoding="utf-8").strip()).resolve()
            candidate = git_dir / "config"
            return candidate if candidate.is_file() else None
    return None


def scan_repo_config(repo_dir: str | Path) -> list[RepoConfigFinding]:
    """Report every code-executing or redirecting entry in the repo's own git config."""
    path = repo_config_path(repo_dir)
    if path is None:
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return _findings_for(parse_git_config(text))


def repo_config_fingerprint(repo_dir: str | Path) -> str:
    """`sha256:<hex>` of the repo config bytes (`""` when there is no config)."""
    path = repo_config_path(repo_dir)
    if path is None:
        return ""
    try:
        return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def format_findings(findings: list[RepoConfigFinding]) -> str:
    """One line per finding."""
    return "\n".join(f"  {f.key} = {f.value[:80]}  — {f.reason}" for f in findings)
