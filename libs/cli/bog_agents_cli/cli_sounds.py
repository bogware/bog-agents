"""Cross-platform notification sounds for the bog-agents CLI.

All sound calls run in daemon threads to avoid blocking the Textual event loop.
All errors are silently swallowed — sounds are purely cosmetic.
"""

from __future__ import annotations

import os
import platform
import threading

# Module-level toggle, overridden by BOG_AGENTS_SOUNDS env var at import time.
_sound_enabled: bool = os.environ.get("BOG_AGENTS_SOUNDS", "1").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}


def is_sound_enabled() -> bool:
    """Return True if sounds are enabled.

    Reads the BOG_AGENTS_SOUNDS environment variable (default enabled).
    Can be overridden for the session via `toggle_sounds()`.

    Returns:
        True if sounds should play; False otherwise.
    """
    return _sound_enabled


def toggle_sounds(enabled: bool) -> None:
    """Enable or disable sounds for the session.

    Args:
        enabled: True to enable sounds, False to disable.
    """
    global _sound_enabled  # noqa: PLW0603
    _sound_enabled = enabled


def _play_sound(sound_type: str) -> None:
    r"""Internal: attempt to play a sound using platform-specific methods.

    Tries in order:
    1. plyer.notification (cross-platform, if installed)
    2. afplay /System/Library/Sounds/<name>.aiff (macOS)
    3. paplay /usr/share/sounds/freedesktop/stereo/<name>.oga (Linux)
    4. winsound.Beep (Windows)
    5. terminal bell (\a) as last resort

    Args:
        sound_type: Either ``"completion"`` or ``"error"``.
    """
    # 1. plyer (cross-platform, optional dependency)
    try:
        from plyer import notification  # type: ignore[import-untyped]

        title = "bog-agents" if sound_type == "completion" else "bog-agents error"
        message = "Task complete" if sound_type == "completion" else "An error occurred"
        notification.notify(title=title, message=message, timeout=2)
        return
    except Exception:  # noqa: S110
        pass

    system = platform.system()

    # 2. macOS afplay
    if system == "Darwin":
        import subprocess  # noqa: S404

        sound_file = (
            "/System/Library/Sounds/Glass.aiff"
            if sound_type == "completion"
            else "/System/Library/Sounds/Sosumi.aiff"
        )
        try:
            subprocess.run(  # noqa: S603
                ["afplay", sound_file], capture_output=True, timeout=3, check=False
            )
            return
        except Exception:  # noqa: S110
            pass

    # 3. Linux paplay / aplay
    elif system == "Linux":
        import subprocess  # noqa: S404

        oga_name = "complete.oga" if sound_type == "completion" else "dialog-error.oga"
        oga_path = f"/usr/share/sounds/freedesktop/stereo/{oga_name}"
        for cmd in (["paplay", oga_path], ["aplay", oga_path]):
            try:
                subprocess.run(cmd, capture_output=True, timeout=3, check=False)  # noqa: S603
                return
            except Exception:  # noqa: S112
                continue

    # 4. Windows winsound
    elif system == "Windows":
        try:
            import winsound  # type: ignore[import-not-found]

            freq = 880 if sound_type == "completion" else 440
            winsound.Beep(freq, 200)
            return
        except Exception:  # noqa: S110
            pass

    # 5. Terminal bell — last resort
    try:
        print("\a", end="", flush=True)  # noqa: T201
    except Exception:  # noqa: S110
        pass


def _play_in_thread(sound_type: str) -> None:
    """Dispatch sound playback to a daemon thread.

    Args:
        sound_type: Either ``"completion"`` or ``"error"``.
    """
    if not _sound_enabled:
        return
    t = threading.Thread(target=_play_sound, args=(sound_type,), daemon=True)
    t.start()


def play_completion_sound() -> None:
    r"""Play a brief completion sound when the agent finishes a task.

    Tries in order:
    1. plyer.notification (cross-platform, if installed)
    2. afplay /System/Library/Sounds/Glass.aiff (macOS)
    3. paplay /usr/share/sounds/freedesktop/stereo/complete.oga (Linux)
    4. winsound.Beep(880, 200) (Windows)
    5. print('\a') as last resort (terminal bell)

    Never raises — all errors are silently swallowed.
    Sound is played in a daemon thread to avoid blocking the Textual event loop.
    """
    _play_in_thread("completion")


def play_error_sound() -> None:
    r"""Play a brief error sound when the agent encounters an error.

    Tries in order:
    1. plyer.notification (cross-platform, if installed)
    2. afplay /System/Library/Sounds/Sosumi.aiff (macOS)
    3. paplay /usr/share/sounds/freedesktop/stereo/dialog-error.oga (Linux)
    4. winsound.Beep(440, 200) (Windows)
    5. print('\a') as last resort (terminal bell)

    Never raises — all errors are silently swallowed.
    Sound is played in a daemon thread to avoid blocking the Textual event loop.
    """
    _play_in_thread("error")
