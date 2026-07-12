"""External editor support for composing prompts.

Pops the current input buffer into `$VISUAL`/`$EDITOR` (or a platform default)
so long prompts can be composed with a real editor, then drops the result back
into the chat input. The logic here is pure + subprocess only so it stays
importable and testable without spinning up the TUI.
"""

from __future__ import annotations

import contextlib
import logging
import os
import shlex
import subprocess  # noqa: S404
import sys
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

GUI_WAIT_FLAG: dict[str, str] = {
    "code": "--wait",
    "cursor": "--wait",
    "zed": "--wait",
    "atom": "--wait",
    "subl": "-w",
    "windsurf": "--wait",
}
"""Mapping of GUI editor base names to their blocking flag.

GUI editors return immediately after launching a window unless told to wait for
the file to close, which would make `open_in_editor` read back the unedited
buffer. These flags make them block until the user is done.
"""

VIM_EDITORS = {"vi", "vim", "nvim"}
"""Set of vim-family editor base names that receive the `-i NONE` flag."""


def resolve_editor() -> list[str] | None:
    """Resolve the editor command from the environment.

    Checks `$VISUAL`, then `$EDITOR`, then falls back to a platform default
    (`notepad` on Windows, `vi` elsewhere).

    Returns:
        Tokenized command list, or `None` if an env var was set but empty
        after tokenization.
    """
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if not editor:
        if sys.platform == "win32":
            return ["notepad"]
        return ["vi"]
    tokens = shlex.split(editor)
    return tokens or None


def _prepare_command(cmd: list[str], filepath: str) -> list[str]:
    """Build the full command list with appropriate flags.

    Adds `--wait`/`-w` for GUI editors and `-i NONE` for vim-family editors,
    then appends the file path.

    Args:
        cmd: Base editor command (already tokenized).
        filepath: Path to the temp file to open.

    Returns:
        The complete command list with flags and file path appended. The input
        `cmd` list is not mutated.
    """
    cmd = list(cmd)  # copy — never mutate the caller's list
    exe = Path(cmd[0]).stem.lower()

    # Auto-inject the wait flag for GUI editors so they block until close.
    if exe in GUI_WAIT_FLAG:
        flag = GUI_WAIT_FLAG[exe]
        if flag not in cmd:
            cmd.insert(1, flag)

    # Vim workaround: avoid viminfo errors in temp environments.
    if exe in VIM_EDITORS and "-i" not in cmd:
        cmd.extend(["-i", "NONE"])

    cmd.append(filepath)
    return cmd


def open_in_editor(current_text: str) -> str | None:
    """Open `current_text` in an external editor and read back the result.

    Writes the buffer to a temp `.md` file, launches the resolved editor, then
    reads the edited content back and cleans up the temp file.

    Args:
        current_text: The text to pre-populate in the editor.

    Returns:
        The edited text with normalized line endings, or `None` if the editor
        could not be resolved/found, exited non-zero, or the result was
        empty/whitespace-only (treated as a cancellation).
    """
    cmd = resolve_editor()
    if cmd is None:
        return None

    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".md",
            prefix="bog-agents-edit-",
            delete=False,
            mode="w",
            encoding="utf-8",
        ) as tmp:
            tmp_path = tmp.name
            tmp.write(current_text)

        full_cmd = _prepare_command(cmd, tmp_path)

        # S603: the editor command comes from the user's own $VISUAL/$EDITOR.
        result = subprocess.run(  # noqa: S603
            full_cmd,
            stdin=sys.stdin,
            stdout=sys.stdout,
            stderr=sys.stderr,
            check=False,
        )
        if result.returncode != 0:
            logger.warning(
                "Editor exited with code %d: %s", result.returncode, full_cmd
            )
            return None

        edited = Path(tmp_path).read_text(encoding="utf-8")

        # Normalize line endings so a Windows editor's CRLF doesn't leak back.
        edited = edited.replace("\r\n", "\n").replace("\r", "\n")

        # Most editors append a final newline on save (POSIX convention).
        # Strip exactly one so the cursor lands on content, not a blank line,
        # while preserving any intentional trailing newlines the user added.
        edited = edited.removesuffix("\n")

        # Treat an empty result as a cancellation.
        if not edited.strip():
            return None
    except FileNotFoundError:
        return None
    except Exception:
        logger.warning("External editor failed", exc_info=True)
        return None
    else:
        return edited
    finally:
        if tmp_path is not None:
            with contextlib.suppress(OSError):
                Path(tmp_path).unlink(missing_ok=True)
