"""Teammate file exchange (ROADMAP #76): `send_file`, `send_patch`, `receive_files` over the team `Mailbox`.

Teammates coordinate through `bog_agents.teams.Mailbox`; text messages are not
enough when one worker produced a fixture, a generated file or a patch another
worker needs. An `Attachment` is a typed, content-addressed copy staged under
the project's exchange directory (`.bog-agents/team/exchange/<id>/`): text
files and patches go through the DLP scanner (redacted copy, detection count
recorded), directories are zipped with build junk skipped, and every send is
audit-logged through an injected sink. `receive_files` copies what is addressed
to the member into its inbox directory without consuming the text messages the
claim loop still needs.
"""

from __future__ import annotations

import hashlib
import io
import logging
import shutil
import time
import uuid
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool

from bog_agents.teams import Attachment, Mailbox

logger = logging.getLogger(__name__)

EXCHANGE_RELATIVE = Path(".bog-agents") / "team" / "exchange"
INBOX_RELATIVE = Path(".bog-agents") / "team" / "inbox"
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_DIR_BYTES = 64 * 1024 * 1024
SKIP_DIRS = frozenset({".git", "node_modules", ".venv", "venv", "__pycache__", ".mypy_cache", ".ruff_cache", "dist", "build", "target"})
ScanFn = Callable[[str], tuple[str, int]]
"""`text -> (redacted text, detections)`."""
AuditFn = Callable[[str, dict[str, Any]], None]
"""`(event kind, data) -> None`."""
GitFn = Callable[[Path, list[str]], str]
"""`(repo dir, git args) -> stdout` (argv form, no shell)."""


def default_scan() -> ScanFn:
    """The SDK DLP patterns as a scan function."""
    from bog_agents.middleware.dlp import DEFAULT_PATTERNS, _redact_text, _scan_text

    def _scan(text: str) -> tuple[str, int]:
        hits = sum(count for _pattern, count in _scan_text(text, DEFAULT_PATTERNS))
        return (_redact_text(text, DEFAULT_PATTERNS) if hits else text), hits

    return _scan


def exchange_dir(root: str | Path) -> Path:
    """`<root>/.bog-agents/team/exchange`."""
    return Path(root) / EXCHANGE_RELATIVE


def inbox_dir(root: str | Path, member: str) -> Path:
    """`<root>/.bog-agents/team/inbox/<member>`."""
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in member) or "member"
    return Path(root) / INBOX_RELATIVE / safe


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _is_text(data: bytes) -> bool:
    if b"\x00" in data[:4096]:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _zip_dir(source: Path, *, max_bytes: int) -> bytes:
    """Zip `source` (skipping build junk); raises ValueError past `max_bytes`."""
    buffer = io.BytesIO()
    total = 0
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(source.rglob("*")):
            if any(part in SKIP_DIRS for part in path.relative_to(source).parts):
                continue
            if path.is_symlink() or not path.is_file():
                continue
            total += path.stat().st_size
            if total > max_bytes:
                msg = f"directory {source} exceeds {max_bytes} bytes (build directories are already skipped)"
                raise ValueError(msg)
            archive.write(path, arcname=str(path.relative_to(source)))
    return buffer.getvalue()


def stage_attachment(
    *,
    root: str | Path,
    kind: str,
    name: str,
    data: bytes,
    scan: ScanFn | None,
    source: str = "",
) -> Attachment:
    """Write one attachment into the exchange directory, scanning text content; returns the record."""
    folder = exchange_dir(root) / uuid.uuid4().hex[:12]
    folder.mkdir(parents=True, exist_ok=True)
    redactions = 0
    if scan is not None and kind in {"file", "patch"} and _is_text(data):
        redacted, redactions = scan(data.decode("utf-8"))
        if redactions:
            data = redacted.encode("utf-8")
    target = folder / name
    target.write_bytes(data)
    return Attachment(kind=kind, name=name, path=str(target), sha256=_sha256(data), size=len(data), redactions=redactions, source=source)


def _resolve_inside(root: Path, raw: str) -> Path:
    candidate = (root / raw).resolve() if not Path(raw).is_absolute() else Path(raw).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        msg = f"{raw!r} is outside the team's working tree"
        raise ValueError(msg) from exc
    return candidate


def _default_git(repo: Path, args: list[str]) -> str:
    import subprocess

    from bog_agents.git_env import NO_EXTERNAL_DIFF, hardened_git_env

    argv = ["git", *NO_EXTERNAL_DIFF, *args]
    result = subprocess.run(  # noqa: S603 - argv form, no shell; git resolved from PATH like every other git call
        argv, cwd=str(repo), capture_output=True, text=True, timeout=60, check=False, env=hardened_git_env()
    )
    if result.returncode != 0:
        msg = f"git {' '.join(args)} failed: {result.stderr.strip()}"
        raise RuntimeError(msg)
    return result.stdout


def _audit_fields(attachment: Attachment) -> dict[str, Any]:
    """The attachment as audit-event fields (`attachment_kind`, so the event kind stays `team_file`)."""
    data = attachment.to_dict()
    data["attachment_kind"] = data.pop("kind")
    return data


def team_file_tools(
    mailbox: Mailbox | Any,  # noqa: ANN401 - MailboxStore shares the API without a base class
    member: str,
    *,
    root: str | Path,
    scan: ScanFn | None = None,
    audit: AuditFn | None = None,
    run_git: GitFn | None = None,
    max_file_bytes: int = MAX_FILE_BYTES,
    max_dir_bytes: int = MAX_DIR_BYTES,
) -> list[BaseTool]:
    """The three exchange tools bound to `member` on `mailbox` (any object with `send` / `inbox`)."""
    base = Path(root)
    scanner = scan if scan is not None else default_scan()
    git = run_git or _default_git
    delivered: set[str] = set()

    def _audit(data: dict[str, Any]) -> None:
        if audit is None:
            return
        try:
            audit("team_file", data)
        except Exception:  # noqa: BLE001 - an audit sink must never break a send
            logger.debug("team_file audit sink failed", exc_info=True)

    def _post(recipient: str, note: str, attachment: Attachment) -> str:
        mailbox.send(member, recipient or Mailbox.ALL, note or f"{attachment.kind}: {attachment.name}", attachments=(attachment,))
        _audit({"from": member, "to": recipient or Mailbox.ALL, **_audit_fields(attachment)})
        redacted = f" ({attachment.redactions} secret(s) redacted)" if attachment.redactions else ""
        return f"Sent {attachment.kind} {attachment.name} ({attachment.size} bytes, {attachment.sha256[:19]}) to {recipient or 'everyone'}{redacted}."

    def send_file(recipient: str, path: str, note: str = "") -> str:
        """Send a file or directory from the working tree to a teammate (`@all` broadcasts).

        Text files are DLP-scanned and secrets redacted before they leave;
        directories are zipped with build folders skipped. The teammate
        receives it with `receive_files`.
        """
        try:
            source = _resolve_inside(base, path)
        except ValueError as exc:
            return f"Error: {exc}"
        if source.is_dir():
            try:
                data = _zip_dir(source, max_bytes=max_dir_bytes)
            except ValueError as exc:
                return f"Error: {exc}"
            attachment = stage_attachment(root=base, kind="dir", name=f"{source.name}.zip", data=data, scan=None, source=str(source))
        elif source.is_file():
            size = source.stat().st_size
            if size > max_file_bytes:
                return f"Error: {path} is {size} bytes; the limit is {max_file_bytes}."
            attachment = stage_attachment(root=base, kind="file", name=source.name, data=source.read_bytes(), scan=scanner, source=str(source))
        else:
            return f"Error: {path} does not exist."
        return _post(recipient, note, attachment)

    def send_patch(recipient: str, note: str = "", include_untracked: bool = True) -> str:
        """Send your uncommitted changes as a patch (`git diff HEAD`, plus untracked files) to a teammate.

        The patch is DLP-scanned; the teammate applies it with `git apply`
        after `receive_files`.
        """
        try:
            diff = git(base, ["diff", "HEAD", "--binary"])
            if include_untracked:
                untracked = [line for line in git(base, ["ls-files", "--others", "--exclude-standard"]).splitlines() if line.strip()]
                for rel in untracked:
                    diff += _untracked_patch(base, rel)
        except Exception as exc:  # noqa: BLE001 - reported to the model, never raised into the loop
            return f"Error: {exc}"
        if not diff.strip():
            return "Nothing to send: the working tree matches HEAD."
        stamp = time.strftime("%Y%m%d-%H%M%S")
        attachment = stage_attachment(
            root=base, kind="patch", name=f"{member}-{stamp}.patch", data=diff.encode("utf-8"), scan=scanner, source="git diff HEAD"
        )
        return _post(recipient, note, attachment)

    def receive_files(dest: str = "") -> str:
        """Copy files, directories and patches teammates sent you into your inbox (or `dest`) and list them."""
        target = _resolve_inside(base, dest) if dest else inbox_dir(base, member)
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return f"Error: cannot create {target}: {exc}"
        lines: list[str] = []
        for message in mailbox.inbox(member):
            for attachment in getattr(message, "attachments", ()):
                if attachment.sha256 in delivered:
                    continue
                staged = Path(attachment.path)
                if not staged.is_file():
                    lines.append(f"- {attachment.name} from {message.sender}: staged copy missing ({staged})")
                    delivered.add(attachment.sha256)
                    continue
                if attachment.kind == "dir":
                    out_dir = target / staged.stem
                    with zipfile.ZipFile(staged) as archive:
                        _safe_extract(archive, out_dir)
                    where = out_dir
                else:
                    where = target / attachment.name
                    shutil.copy2(staged, where)
                delivered.add(attachment.sha256)
                note = f" — {message.body}" if message.body and not message.body.startswith(f"{attachment.kind}: ") else ""
                hint = " (apply with `git apply`)" if attachment.kind == "patch" else ""
                lines.append(f"- {attachment.kind} {attachment.name} from {message.sender} → {where}{hint}{note}")
                _audit({"received_by": member, "from": message.sender, **_audit_fields(attachment)})
        return "\n".join(lines) if lines else "No new files from teammates."

    return [
        StructuredTool.from_function(func=send_file, name="send_file"),
        StructuredTool.from_function(func=send_patch, name="send_patch"),
        StructuredTool.from_function(func=receive_files, name="receive_files"),
    ]


def _untracked_patch(repo: Path, rel: str) -> str:
    """A `git diff`-style creation hunk for an untracked text file (binary files are skipped)."""
    path = repo / rel
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    if not _is_text(data):
        return ""
    lines = data.decode("utf-8").splitlines(keepends=True)
    body = "".join(f"+{line}" if line.endswith("\n") else f"+{line}\n\\ No newline at end of file\n" for line in lines)
    return f"diff --git a/{rel} b/{rel}\nnew file mode 100644\n--- /dev/null\n+++ b/{rel}\n@@ -0,0 +1,{len(lines)} @@\n{body}"


def _safe_extract(archive: zipfile.ZipFile, dest: Path) -> None:
    """Extract without path traversal."""
    dest.mkdir(parents=True, exist_ok=True)
    base = dest.resolve()
    for info in archive.infolist():
        target = (dest / info.filename).resolve()
        try:
            target.relative_to(base)
        except ValueError:
            continue
        if info.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(info) as src, target.open("wb") as out:
            shutil.copyfileobj(src, out)


__all__ = ["AuditFn", "GitFn", "ScanFn", "default_scan", "exchange_dir", "inbox_dir", "stage_attachment", "team_file_tools"]
