"""I/O utility helpers for bog-agents-cli."""

# ruff: noqa: DOC502
from __future__ import annotations

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
            Applied to the temp file *before* the rename so the destination
            never briefly exists with a wider mode. Ignored on Windows
            (``os.chmod`` permissions don't map cleanly).

    Raises:
        OSError: If the write or rename fails.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(content, encoding=encoding)
        if mode is not None and sys.platform != "win32":
            tmp.chmod(mode)
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
