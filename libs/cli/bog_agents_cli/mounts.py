"""`/add-dir` (ROADMAP #76): extra directories mounted into the agent's filesystem as `/mnt/<name>/`.

A multi-repo task needs files from a sibling checkout without making it the
working directory. Mounts are recorded in `.bog-agents/mounts.json` (repo-local,
reviewable) and `create_cli_agent` adds each one as a `FilesystemBackend` route
under `/mnt/<name>/` on the `CompositeBackend`, so `read_file("/mnt/api/src/x.py")`
just works while the default backend stays rooted at the project. A mount added
mid-session takes effect on the next agent start (the backend graph is built
once per server).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

MOUNTS_RELATIVE = Path(".bog-agents") / "mounts.json"
MOUNT_PREFIX = "/mnt/"
USAGE = (
    "Usage: /add-dir <path> [--name <name>] | /add-dir list | /add-dir remove <name>"
)
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,40}$")


@dataclass(frozen=True)
class Mount:
    """One extra directory."""

    name: str
    path: str

    @property
    def route(self) -> str:
        """The virtual path prefix the agent sees."""
        return f"{MOUNT_PREFIX}{self.name}/"


def mounts_path(project_root: str | Path) -> Path:
    """`<root>/.bog-agents/mounts.json`."""
    return Path(project_root) / MOUNTS_RELATIVE


def load_mounts(project_root: str | Path) -> list[Mount]:
    """Recorded mounts (missing or malformed file → none)."""
    try:
        data = json.loads(mounts_path(project_root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    items = data.get("mounts") if isinstance(data, dict) else data
    out: list[Mount] = []
    if not isinstance(items, list):
        return out
    for item in items:
        if (
            isinstance(item, dict)
            and _NAME_RE.match(str(item.get("name", "")))
            and item.get("path")
        ):
            out.append(Mount(name=str(item["name"]), path=str(item["path"])))
    return out


def save_mounts(project_root: str | Path, mounts: list[Mount]) -> Path:
    """Write the list."""
    path = mounts_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"mounts": [{"name": m.name, "path": m.path} for m in mounts]}, indent=2
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def default_name(path: Path) -> str:
    """A route name from a directory name."""
    raw = re.sub(r"[^A-Za-z0-9._-]+", "-", path.name).strip("-.") or "dir"
    return raw[:40]


def add_mount(
    project_root: str | Path, raw_path: str, *, name: str | None = None
) -> Mount:
    """Validate and record a mount.

    Raises:
        ValueError: When the path is not an existing directory, sits inside the
            project (already reachable), or the name is taken / malformed.
    """
    root = Path(project_root).resolve()
    directory = Path(raw_path).expanduser()
    if not directory.is_absolute():
        directory = (root / directory).resolve()
    directory = directory.resolve()
    if not directory.is_dir():
        msg = f"{raw_path} is not a directory"
        raise ValueError(msg)
    try:
        directory.relative_to(root)
    except ValueError:
        pass
    else:
        msg = (
            f"{directory} is inside the project already; the agent can read it directly"
        )
        raise ValueError(msg)
    mount_name = name or default_name(directory)
    if not _NAME_RE.match(mount_name):
        msg = f"mount name {mount_name!r} must be letters, digits, dots, dashes or underscores"
        raise ValueError(msg)
    mounts = load_mounts(root)
    if any(m.name == mount_name for m in mounts):
        msg = f"a mount named {mount_name!r} already exists; pick --name"
        raise ValueError(msg)
    mount = Mount(name=mount_name, path=str(directory))
    save_mounts(root, [*mounts, mount])
    return mount


def remove_mount(project_root: str | Path, name: str) -> bool:
    """Drop a mount by name; `True` when it existed."""
    mounts = load_mounts(project_root)
    kept = [m for m in mounts if m.name != name]
    if len(kept) == len(mounts):
        return False
    save_mounts(project_root, kept)
    return True


def describe_mounts(mounts: list[Mount]) -> str:
    """Rows for `/add-dir list`."""
    if not mounts:
        return "No extra directories mounted. /add-dir <path> mounts one at /mnt/<name>/ (next agent start)."
    lines = [f"{'ROUTE':<24} PATH"]
    lines.extend(
        f"{m.route:<24} {m.path}" + ("" if Path(m.path).is_dir() else "  (missing)")
        for m in mounts
    )
    return "\n".join(lines)


def run_add_dir_command(command: str, project_root: str | Path) -> str:
    """Body of `/add-dir`."""
    tokens = command.strip().split()[1:]
    if not tokens or tokens[0] in {"help", "-h", "--help"}:
        return USAGE + "\n\n" + describe_mounts(load_mounts(project_root))
    verb = tokens[0].lower()
    if verb == "list":
        return describe_mounts(load_mounts(project_root))
    if verb == "remove":
        if len(tokens) < 2:
            return USAGE
        return (
            f"Removed mount {tokens[1]!r}."
            if remove_mount(project_root, tokens[1])
            else f"No mount named {tokens[1]!r}."
        )
    name: str | None = None
    rest = list(tokens)
    if "--name" in rest:
        index = rest.index("--name")
        name = rest[index + 1] if index + 1 < len(rest) else None
        del rest[index : index + 2]
    raw_path = " ".join(rest)
    try:
        mount = add_mount(project_root, raw_path, name=name)
    except ValueError as exc:
        return f"Cannot mount: {exc}"
    return f"Mounted {mount.path} at {mount.route} — available to the agent on the next start (this session's backend is already built)."


def mount_routes(project_root: str | Path) -> dict[str, Path]:
    """`{route: directory}` for `create_cli_agent` (only mounts whose directory exists)."""
    return {
        m.route: Path(m.path)
        for m in load_mounts(project_root)
        if Path(m.path).is_dir()
    }


__all__ = [
    "MOUNT_PREFIX",
    "USAGE",
    "Mount",
    "add_mount",
    "describe_mounts",
    "load_mounts",
    "mount_routes",
    "mounts_path",
    "remove_mount",
    "run_add_dir_command",
    "save_mounts",
]
