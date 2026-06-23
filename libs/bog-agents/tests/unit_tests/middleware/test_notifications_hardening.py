"""Hardening regression tests for :func:`copy_to_clipboard`.

Guards S30: the xclip/pbcopy fallback previously called
``proc.communicate(text)`` with no timeout and only caught
``FileNotFoundError``/``OSError``. A wedged helper blocked the calling
thread forever and leaked the child. The fallback now uses a context
manager with ``timeout=5`` and kills+reaps the child on
``TimeoutExpired``, returning ``False`` instead of hanging.

These tests rely on ``pyperclip`` being absent (it is not a test
dependency), so :func:`copy_to_clipboard` always takes the
system-command fallback path that S30 hardened.
"""

from __future__ import annotations

import subprocess
from typing import Self
from unittest.mock import MagicMock, patch

from bog_agents.middleware.notifications import copy_to_clipboard


class _FakeProc:
    """Minimal stand-in for a ``subprocess.Popen`` used as a context manager."""

    def __init__(self, *, timeout_on_communicate: bool, returncode: int = 0) -> None:
        self._timeout = timeout_on_communicate
        self.returncode = returncode
        self.kill = MagicMock()
        self.communicate = MagicMock(side_effect=self._communicate)

    def _communicate(self, *args: object, timeout: float | None = None) -> tuple[bytes, bytes]:
        # The first call (with the payload) may time out; the reaping call
        # (no payload) after kill() must succeed.
        if self._timeout and args:
            raise subprocess.TimeoutExpired(cmd="xclip", timeout=timeout or 0)
        return (b"", b"")

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def test_fallback_passes_timeout() -> None:
    """The fallback must pass an explicit timeout to ``communicate``."""
    proc = _FakeProc(timeout_on_communicate=False, returncode=0)
    with (
        patch("bog_agents.middleware.notifications.platform.system", return_value="Linux"),
        patch("bog_agents.middleware.notifications.subprocess.Popen", return_value=proc) as popen,
    ):
        assert copy_to_clipboard("hello") is True
    popen.assert_called_once()
    # The payload call must carry timeout=5; a missing timeout is the bug.
    _args, kwargs = proc.communicate.call_args_list[0]
    assert kwargs.get("timeout") == 5


def test_wedged_helper_is_killed_and_returns_false() -> None:
    """A helper that never drains stdin is killed+reaped, returning False."""
    proc = _FakeProc(timeout_on_communicate=True)
    with (
        patch("bog_agents.middleware.notifications.platform.system", return_value="Darwin"),
        patch("bog_agents.middleware.notifications.subprocess.Popen", return_value=proc),
    ):
        assert copy_to_clipboard("stuck") is False
    proc.kill.assert_called_once()
    # communicate is called twice: once with the payload (times out),
    # once with no args to reap the killed child.
    assert proc.communicate.call_count == 2


def test_missing_helper_returns_false() -> None:
    """A missing xclip/pbcopy binary must not raise."""
    with (
        patch("bog_agents.middleware.notifications.platform.system", return_value="Linux"),
        patch("bog_agents.middleware.notifications.subprocess.Popen", side_effect=FileNotFoundError),
    ):
        assert copy_to_clipboard("text") is False


def test_unsupported_platform_returns_false() -> None:
    """Windows (no fallback helper) returns False without spawning a process."""
    with (
        patch("bog_agents.middleware.notifications.platform.system", return_value="Windows"),
        patch("bog_agents.middleware.notifications.subprocess.Popen") as popen,
    ):
        assert copy_to_clipboard("text") is False
    popen.assert_not_called()
