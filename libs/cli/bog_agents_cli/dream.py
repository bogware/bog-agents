"""`/dream` — overnight ideation pass over the codebase's TODOs and open issues.

A *dream* is a low-cost background-model run that:
  1. scans the working tree for TODO/FIXME/XXX comments,
  2. picks N of them (configurable),
  3. asks a cheap model to sketch possible implementations for each,
  4. drops the markdown into ``~/.bog-agents/dreams/<date>.md``.

The slash command is the on-demand entry point. For genuinely ambient
behaviour the user can also ``/dream install`` a daemon job that runs
the same pass every night via ``bog-agents-daemon``.

Configuration lives at ``~/.bog-agents/dream.toml``:

.. code-block:: toml

    model = "anthropic:claude-haiku-4-5"
    n_targets = 3
    max_words_per_target = 200
    extensions = [".py", ".ts", ".tsx", ".js"]
"""

from __future__ import annotations

import logging
import re
import time
import tomllib
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path

import tomli_w

from bog_agents_cli.feature_helpers import (
    _git,
    feature_state_dir,
    invoke_model,
    resolve_active_model_spec,
    write_artifact,
)

logger = logging.getLogger(__name__)


_DREAM_CONFIG_NAME = "dream.toml"
_DREAM_OUTPUT_SUBDIR = "dreams"
_DEFAULT_EXTENSIONS = (".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs")
_TODO_MARKERS_RE = re.compile(
    r"#\s*(TODO|FIXME|XXX|HACK)\b[:\s]*(.+)$|//\s*(TODO|FIXME|XXX|HACK)\b[:\s]*(.+)$",
    re.IGNORECASE,
)

DREAM_SYSTEM_PROMPT = """\
You are an overnight engineering assistant exploring possible
implementations for a TODO comment found in the codebase. The user
will fix it themselves later; your job is to seed their morning with
a clear-headed sketch they can either accept, adapt, or reject.

For each target you receive, produce ONE markdown block in this shape:

### {target.path}:{target.line} — {target.label}

**Context excerpt:**
```
{code excerpt the user supplied}
```

**Sketch:** Two to four sentences proposing how you would tackle this.
Be concrete: name functions, modules, or interfaces. Cite any related
files only if the excerpt makes them obvious — do NOT invent file paths.

**Open question:** ONE specific thing the user must answer before
they can implement (skip if there's no real ambiguity).

Hard rules:
- Stay under ~150 words per target.
- Never invent code that doesn't follow from the excerpt.
- Be plain-spoken; this is morning reading, not a design doc.
"""


@dataclass
class DreamConfig:
    """Tuning knobs persisted to ~/.bog-agents/dream.toml."""

    model: str = ""
    """Optional model spec override (e.g. ``anthropic:claude-haiku-4-5``).

    Empty means inherit from the running app's active model.
    """

    n_targets: int = 3
    extensions: list[str] = field(default_factory=lambda: list(_DEFAULT_EXTENSIONS))
    max_files_scanned: int = 400
    """Hard upper bound on how many files the scanner reads."""

    excluded_paths: list[str] = field(
        default_factory=lambda: [".venv", "node_modules", "dist", "build", ".git"]
    )


@dataclass
class TodoHit:
    """One discovered TODO/FIXME comment."""

    path: Path
    line: int
    label: str
    excerpt: str
    """A few lines surrounding the comment so the model has context."""


@dataclass
class DreamRun:
    """Outcome of one /dream run."""

    config: DreamConfig
    targets: list[TodoHit]
    body: str
    path: Path
    elapsed_seconds: float


# --------------------------------------------------------------------------- #
# Config persistence                                                          #
# --------------------------------------------------------------------------- #


def dream_config_path() -> Path:
    return feature_state_dir() / _DREAM_CONFIG_NAME


def load_dream_config(path: Path | None = None) -> DreamConfig:
    """Load dream config, falling back to defaults on missing/malformed."""
    target = path or dream_config_path()
    cfg = DreamConfig()
    if not target.exists():
        return cfg
    try:
        data = tomllib.loads(target.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError):
        logger.warning("Failed to parse dream.toml; using defaults", exc_info=True)
        return cfg
    if not isinstance(data, dict):
        return cfg
    if isinstance(data.get("model"), str):
        cfg.model = data["model"].strip()
    if isinstance(data.get("n_targets"), int):
        cfg.n_targets = max(1, min(20, int(data["n_targets"])))
    if isinstance(data.get("max_files_scanned"), int):
        cfg.max_files_scanned = max(50, min(5_000, int(data["max_files_scanned"])))
    if isinstance(data.get("extensions"), list):
        cfg.extensions = [str(e) for e in data["extensions"] if isinstance(e, str)]
    if isinstance(data.get("excluded_paths"), list):
        cfg.excluded_paths = [
            str(e) for e in data["excluded_paths"] if isinstance(e, str)
        ]
    return cfg


def save_dream_config(config: DreamConfig, *, path: Path | None = None) -> None:
    """Persist a :class:`DreamConfig` to disk (atomic tmp+rename)."""
    target = path or dream_config_path()
    payload = {
        "model": config.model,
        "n_targets": config.n_targets,
        "max_files_scanned": config.max_files_scanned,
        "extensions": config.extensions,
        "excluded_paths": config.excluded_paths,
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    with tmp.open("wb") as fh:
        tomli_w.dump(payload, fh)
    tmp.replace(target)


# --------------------------------------------------------------------------- #
# Scanner for code-comment markers                                            #
# --------------------------------------------------------------------------- #


def scan_for_todos(
    root: Path, config: DreamConfig, *, max_hits: int = 50
) -> list[TodoHit]:
    """Walk ``root`` looking for ``# TODO`` / ``// FIXME`` etc.

    Returns at most ``max_hits`` results, sampling deterministically by
    file order so repeated runs surface different parts of the codebase.

    Args:
        root: Where to start the walk.
        config: Tuning (extensions, exclusions, scan budget).
        max_hits: Hard cap on returned hits — independent of
            ``config.n_targets`` (the caller picks N out of these).
    """
    extensions = tuple(e.lower() for e in (config.extensions or _DEFAULT_EXTENSIONS))
    excluded = {Path(p).name for p in (config.excluded_paths or [])}

    hits: list[TodoHit] = []
    files_scanned = 0
    for file_path in _iter_repo_files(root):
        if files_scanned >= config.max_files_scanned:
            break
        if any(part in excluded for part in file_path.parts):
            continue
        if file_path.suffix.lower() not in extensions:
            continue
        files_scanned += 1
        try:
            with file_path.open(encoding="utf-8", errors="replace") as fh:
                lines = fh.readlines()
        except OSError:
            continue
        for idx, raw in enumerate(lines):
            if len(raw) > 500:
                continue
            m = _TODO_MARKERS_RE.search(raw)
            if not m:
                continue
            label = (m.group(2) or m.group(4) or "").strip()
            if not label:
                continue
            excerpt = _format_excerpt(lines, idx)
            hits.append(
                TodoHit(
                    path=file_path,
                    line=idx + 1,
                    label=label[:120],
                    excerpt=excerpt,
                )
            )
            if len(hits) >= max_hits:
                break
        if len(hits) >= max_hits:
            break
    return hits


def _iter_repo_files(root: Path) -> list[Path]:
    """Yield files in deterministic order, preferring tracked files when in git.

    Importantly: the listing never *escapes* ``root``. ``git rev-parse
    --show-toplevel`` walks parent directories looking for a ``.git``,
    so calling it from a transient subdir (a test ``tmp_path`` inside
    a developer's checkout, say) would otherwise return the *parent*
    repo's root and pull in tens of thousands of unrelated files. We
    only honour the git listing when the resolved git-root IS ``root``;
    in every other case we fall back to a plain rglob restricted to
    ``root``.
    """
    git_root = _git(["rev-parse", "--show-toplevel"], str(root))
    if git_root:
        try:
            git_root_path = Path(git_root).resolve()
            root_resolved = root.resolve()
        except (OSError, ValueError):
            git_root_path = None
            root_resolved = root
        if git_root_path is not None and git_root_path == root_resolved:
            listing = _git(["ls-files"], git_root)
            if listing:
                return [
                    git_root_path / line
                    for line in listing.splitlines()
                    if line.strip()
                ]
    # Non-git fallback (and the safe path when root is a tmp dir):
    # plain rglob, strictly inside ``root``.
    return sorted(p for p in root.rglob("*") if p.is_file())


def _format_excerpt(lines: list[str], idx: int) -> str:
    """Pull a small window around ``idx`` for prompt context."""
    start = max(0, idx - 2)
    end = min(len(lines), idx + 3)
    return "".join(lines[start:end]).rstrip()


# --------------------------------------------------------------------------- #
# Run a dream pass                                                            #
# --------------------------------------------------------------------------- #


def _select_targets(hits: list[TodoHit], n: int) -> list[TodoHit]:
    """Pick N targets from ``hits``.

    Uses an even-stride sampler so consecutive runs surface variety
    rather than always the first N — but with no PRNG so the same hits
    yield the same selection (reproducibility for tests + replays).
    """
    if len(hits) <= n:
        return list(hits)
    stride = max(1, len(hits) // n)
    return [hits[i] for i in range(0, len(hits), stride)][:n]


def _render_targets_for_prompt(targets: list[TodoHit]) -> str:
    parts = ["Targets to explore:\n"]
    for t in targets:
        parts.append(f"--- {t.path}:{t.line} — {t.label} ---")
        parts.append(t.excerpt)
        parts.append("")
    return "\n".join(parts)


async def run_dream(
    app: object,
    *,
    cwd: Path | None = None,
    config: DreamConfig | None = None,
) -> DreamRun:
    """Execute one dream pass and persist the artifact.

    Args:
        app: Running app instance (used for active-model resolution).
        cwd: Working directory to scan; defaults to the app's cwd.
        config: Optional in-memory override of the on-disk config.

    Returns:
        :class:`DreamRun` describing what was found and what was written.

    Raises:
        RuntimeError: When no model spec can be resolved.
        ValueError: When the scanner finds no TODOs at all.
    """
    from bog_agents_cli.config import create_model_with_fallback

    cfg = config or load_dream_config()
    work_dir = cwd or Path(getattr(app, "_cwd", Path.cwd()))
    hits = scan_for_todos(work_dir, cfg)
    if not hits:
        msg = (
            f"no TODO/FIXME/XXX markers found under {work_dir} "
            f"(scanned extensions: {cfg.extensions})"
        )
        raise ValueError(msg)

    targets = _select_targets(hits, cfg.n_targets)
    spec = cfg.model or resolve_active_model_spec(app)
    if not spec:
        msg = "no active model — run /model first or set dream.model in config"
        raise RuntimeError(msg)
    profile = getattr(app, "_profile_override", None)
    model_result = create_model_with_fallback(spec, profile_overrides=profile)

    start = time.monotonic()
    body = await invoke_model(
        model_result.model,
        DREAM_SYSTEM_PROMPT,
        _render_targets_for_prompt(targets),
        timeout_seconds=180.0,
    )
    elapsed = time.monotonic() - start

    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    path = write_artifact(
        _DREAM_OUTPUT_SUBDIR,
        stamp,
        _wrap_with_frontmatter(body, spec, targets),
    )
    return DreamRun(
        config=cfg, targets=targets, body=body, path=path, elapsed_seconds=elapsed
    )


def _wrap_with_frontmatter(body: str, model_spec: str, targets: list[TodoHit]) -> str:
    lines = [
        "---",
        f"model: {model_spec}",
        f"generated: {time.strftime('%Y-%m-%dT%H:%M:%S')}",
        f"targets: {len(targets)}",
        "kind: dream",
        "---",
        "",
        body,
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Daemon install helper                                                       #
# --------------------------------------------------------------------------- #


def render_daemon_job_yaml(*, cron: str = "0 3 * * *", working_dir: str = ".") -> str:
    """Render a YAML snippet the user can pipe into bog-agents-daemon.

    Stays as plain text (rather than calling the daemon HTTP API
    directly) so the install path works even when the daemon is on a
    different machine or not yet running.
    """
    return (
        "# Save this to a file and run:\n"
        "#   bog-agents-daemon import dream-job.yaml\n"
        "# Or POST it to the daemon's /jobs endpoint.\n\n"
        "name: nightly-dream\n"
        "description: 'Overnight ideation over TODO comments'\n"
        "prompt: ''  # supplied by the /dream command at runtime\n"
        f"working_dir: {working_dir!r}\n"
        "triggers:\n"
        f"  - type: cron\n    cron: {cron!r}\n"
        "outputs:\n"
        "  - target: file\n"
        f"    file_path: '{Path.home() / '.bog-agents' / 'dreams' / 'daemon-{run_id}.md'}'\n"
        "    append: false\n"
    )


# --------------------------------------------------------------------------- #
# App handler glue                                                            #
# --------------------------------------------------------------------------- #


async def handle_dream_subcommand(app: object, raw_arg: str) -> None:
    """Dispatch ``/dream <sub>`` subcommands."""
    from bog_agents_cli.widgets.chat_messages import AppMessage, ErrorMessage

    arg = raw_arg.strip()
    head, _, rest = arg.partition(" ")
    head = head.lower()
    rest = rest.strip()

    if head == "config":
        cfg = load_dream_config()
        msg = (
            f"[bold]Dream configuration[/bold] [cyan]{dream_config_path()}[/cyan]\n"
            f"  model:           {cfg.model or '(use active model)'}\n"
            f"  n_targets:       {cfg.n_targets}\n"
            f"  max_files:       {cfg.max_files_scanned}\n"
            f"  extensions:      {', '.join(cfg.extensions)}\n"
            f"  excluded paths:  {', '.join(cfg.excluded_paths)}\n\n"
            "Edit the TOML to change these values."
        )
        # Bootstrap the file if it doesn't exist yet.
        if not dream_config_path().exists():
            with suppress(OSError):
                save_dream_config(cfg)
                msg += f"\n\n[dim]Wrote default config to {dream_config_path()}.[/dim]"
        await app._mount_message(AppMessage(msg))  # type: ignore[attr-defined]
        return

    if head == "list":
        dreams_dir = feature_state_dir() / _DREAM_OUTPUT_SUBDIR
        if not dreams_dir.exists():
            await app._mount_message(  # type: ignore[attr-defined]
                AppMessage("[dim]No dreams yet — run [bold]/dream run[/bold].[/dim]")
            )
            return
        files = sorted(dreams_dir.glob("*.md"), reverse=True)
        if not files:
            await app._mount_message(  # type: ignore[attr-defined]
                AppMessage("[dim]No dreams yet — run [bold]/dream run[/bold].[/dim]")
            )
            return
        lines = [f"[bold]{len(files)} dreams in[/bold] [cyan]{dreams_dir}[/cyan]\n"]
        for f in files[:25]:
            ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(f.stat().st_mtime))
            lines.append(f"  [bold]{f.name}[/bold]  [dim]{ts}[/dim]")
        if len(files) > 25:
            lines.append(f"\n[dim]… and {len(files) - 25} older[/dim]")
        await app._mount_message(AppMessage("\n".join(lines)))  # type: ignore[attr-defined]
        return

    if head == "install":
        snippet = render_daemon_job_yaml()
        await app._mount_message(  # type: ignore[attr-defined]
            AppMessage(
                "[bold]Daemon job template[/bold]\n\n"
                f"```yaml\n{snippet}\n```\n"
                "[dim]Edit and apply via the daemon's /jobs API.[/dim]"
            )
        )
        return

    # Default action: run a dream pass now.
    if getattr(app, "_agent_running", False):
        await app._mount_message(  # type: ignore[attr-defined]
            ErrorMessage("Cannot run /dream while the agent is busy.")
        )
        return

    await app._set_spinner("Dreaming")  # type: ignore[attr-defined]
    try:
        cwd = Path(getattr(app, "_cwd", Path.cwd()))
        result = await run_dream(app, cwd=cwd)
    except ValueError as exc:
        await app._set_spinner("")  # type: ignore[attr-defined]
        await app._mount_message(AppMessage(f"[dim]/dream: {exc}[/dim]"))  # type: ignore[attr-defined]
        return
    except Exception as exc:
        logger.exception("/dream failed")
        await app._set_spinner("")  # type: ignore[attr-defined]
        await app._mount_message(  # type: ignore[attr-defined]
            ErrorMessage(f"/dream failed: {exc}")
        )
        return
    finally:
        await app._set_spinner("")  # type: ignore[attr-defined]

    await app._mount_message(  # type: ignore[attr-defined]
        AppMessage(
            f"[bold]Dream saved to[/bold] [cyan]{result.path}[/cyan] "
            f"({len(result.targets)} targets, "
            f"[dim]{result.elapsed_seconds:.1f}s[/dim])\n\n"
            f"{result.body}"
        )
    )


__all__ = [
    "DreamConfig",
    "DreamRun",
    "TodoHit",
    "handle_dream_subcommand",
    "load_dream_config",
    "render_daemon_job_yaml",
    "run_dream",
    "save_dream_config",
    "scan_for_todos",
]
