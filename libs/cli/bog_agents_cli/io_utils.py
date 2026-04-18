"""I/O utility helpers for bog-agents-cli."""

# ruff: noqa: DOC502
from __future__ import annotations

from pathlib import Path


def atomic_write_text(path: Path, content: str, *, encoding: str = "utf-8") -> None:
    """Write *content* to *path* atomically via a sibling .tmp file + rename.

    Prevents partial/corrupt files when the process is interrupted mid-write.
    The rename is atomic on POSIX; on Windows ``Path.replace`` is used which
    is atomic when src and dst share the same volume.

    Args:
        path: Destination file path.
        content: Text content to write.
        encoding: Text encoding. Default ``"utf-8"``.

    Raises:
        OSError: If the write or rename fails.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(content, encoding=encoding)
        tmp.replace(path)
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
