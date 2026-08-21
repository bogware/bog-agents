"""I/O utility helpers for bog-agents-cli."""

# ruff: noqa: DOC502
from __future__ import annotations

import os
import sys
from pathlib import Path


def atomic_write_text(
    path: Path,
    content: str,
    *,
    encoding: str = "utf-8",
    mode: int | None = None,
) -> None:
    """Write *content* to *path* atomically via a sibling .tmp file + rename.

    Prevents partial/corrupt files when the process is interrupted mid-write.
    The rename is atomic on POSIX; on Windows ``Path.replace`` is used which
    is atomic when src and dst share the same volume.

    Args:
        path: Destination file path.
        content: Text content to write.
        encoding: Text encoding. Default ``"utf-8"``.
        mode: Optional POSIX file mode (e.g. ``0o600`` for token files).
            Marks the write as secret-bearing: the temp file is *created*
            with this mode via ``os.open`` (never briefly umask-default
            world-readable, CT-6), a missing parent directory is created
            ``0o700``, and on Windows — where the numeric mode itself is
            meaningless — the temp file is locked owner-only via ``icacls``
            (`vars_store._secure_owner_only`) before the rename, so the
            destination never exists with a wider ACL.

    Raises:
        OSError: If the write or rename fails.
    """
    if mode is None:
        path.parent.mkdir(parents=True, exist_ok=True)
    else:
        # Secret-bearing parent dirs are born owner-only on POSIX (the mode
        # applies only to newly created leaf dirs; pre-existing dirs are the
        # caller's responsibility, e.g. via _secure_owner_only(is_dir=True)).
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        if mode is None:
            tmp.write_text(content, encoding=encoding)
        else:
            # Create the temp file with the final mode from the start so the
            # plaintext never exists at umask-default permissions (CT-6).
            tmp.unlink(missing_ok=True)  # a stale .tmp would break O_EXCL
            fd = os.open(str(tmp), os.O_CREAT | os.O_EXCL | os.O_WRONLY, mode)
            with os.fdopen(fd, "w", encoding=encoding) as fh:
                fh.write(content)
            if sys.platform == "win32":
                # Numeric modes are a no-op on Windows; harden the temp file
                # BEFORE the rename so the destination inherits the tight ACL
                # (Path.replace keeps the source file's security descriptor).
                from bog_agents_cli.vars_store import _secure_owner_only

                _secure_owner_only(tmp)
        tmp.replace(path)
    except BaseException:
        # ``except BaseException`` is intentional: we want to clean up
        # the temp file on KeyboardInterrupt and SystemExit too,
        # otherwise an interrupted write leaves a stray ``.tmp`` next to
        # the real file. The bare ``raise`` re-raises the original
        # exception so callers see what went wrong.
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
