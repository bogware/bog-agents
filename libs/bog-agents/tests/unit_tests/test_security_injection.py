"""Security regression tests for Wave 1 injection / SSRF fixes (REVIEW.md v2).

* P1-2  — BaseSandbox.grep must never let a glob reach the shell unquoted.
* P1-16 — desktop notifications must not interpolate text into shell/AppleScript.
* P1-6 / P1-74 — browser_agent must re-validate redirect targets (no SSRF via 3xx).
"""

from __future__ import annotations

from typing import Any

import pytest

# ---------------------------------------------------------------------------
# P1-2 — grep glob injection
# ---------------------------------------------------------------------------


def _make_recording_sandbox() -> Any:
    """Construct a concrete BaseSandbox that records the command, not runs it."""
    from bog_agents.backends.sandbox import BaseSandbox

    class _S(BaseSandbox):
        last_cmd: str = ""

        @property
        def id(self) -> str:
            return "rec"

        def execute(self, command: str, **_kwargs: Any) -> Any:
            type(self).last_cmd = command
            return type("R", (), {"output": "", "exit_code": 0})()

        def upload_files(self, *_args: Any, **_kwargs: Any) -> Any:
            return None

        def download_files(self, *_args: Any, **_kwargs: Any) -> Any:
            return None

    return _S()


def test_grep_quotes_glob() -> None:
    """A slash-free glob goes to GNU grep, so it must be a single shlex-quoted token."""
    import shlex

    sb = _make_recording_sandbox()
    malicious = "x'; touch pwned #"
    sb.grep(pattern="needle", path=".", glob=malicious)
    cmd = type(sb).last_cmd

    # The glob must appear as exactly one shlex-quoted token. That is the whole
    # security property: the payload cannot break out of the quoting. (The old
    # `--include='{glob}'` form produced `--include='x'; touch ...` — an
    # unquoted `; touch` that the shell would execute.)
    assert f"--include={shlex.quote(malicious)}" in cmd
    # And the dangerous old form is gone.
    assert "--include='x'; touch" not in cmd


def test_grep_path_glob_is_base64_encoded() -> None:
    """A glob containing `/` takes the in-sandbox Python route.

    GNU `--include` only matches basenames, so `src/**/*.py` is routed to a
    Python script instead. That route must carry the glob as base64 — never as
    shell text — or the quoting property above would be silently lost for every
    path-shaped glob.
    """
    import base64

    sb = _make_recording_sandbox()
    malicious = "x'; touch /tmp/pwned #"
    sb.grep(pattern="needle", path=".", glob=malicious)
    cmd = type(sb).last_cmd

    assert malicious not in cmd
    assert base64.b64encode(malicious.encode()).decode() in cmd
    assert "; touch /tmp/pwned" not in cmd


# ---------------------------------------------------------------------------
# P1-16 — notification injection
# ---------------------------------------------------------------------------


def test_macos_notification_passes_text_as_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    import bog_agents.middleware.notifications as notif

    captured: dict[str, Any] = {}

    def fake_run(args, **kwargs):  # noqa: ANN003
        captured["args"] = args
        captured["kwargs"] = kwargs

    monkeypatch.setattr(notif.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(notif.subprocess, "run", fake_run)
    evil = 'pwned" with title "x" \nend run\ndo shell script "touch /tmp/x'
    notif.send_desktop_notification(title="t", message=evil)
    args = captured["args"]
    # The evil text must be a standalone argv item, never spliced into the -e script.
    assert evil in args
    script_idx = args.index("-e") + 1
    assert "do shell script" not in args[script_idx]
    assert "on run argv" in args[script_idx]


def test_windows_notification_uses_env_not_command(monkeypatch: pytest.MonkeyPatch) -> None:
    import bog_agents.middleware.notifications as notif

    captured: dict[str, Any] = {}

    def fake_run(args, **kwargs):  # noqa: ANN003
        captured["args"] = args
        captured["kwargs"] = kwargs

    monkeypatch.setattr(notif.platform, "system", lambda: "Windows")
    monkeypatch.setattr(notif.subprocess, "run", fake_run)
    evil = '$(rm -rf ~)"; Remove-Item C:\\ -Recurse'
    notif.send_desktop_notification(title="t", message=evil)
    # The malicious text travels via env, not the -Command string.
    cmd_idx = captured["args"].index("-Command") + 1
    assert evil not in captured["args"][cmd_idx]
    assert captured["kwargs"]["env"]["BOG_NOTIFY_MESSAGE"] == evil
    assert "$env:BOG_NOTIFY_MESSAGE" in captured["args"][cmd_idx]


# ---------------------------------------------------------------------------
# P1-6 / P1-74 — SSRF via redirect
# ---------------------------------------------------------------------------


def test_safe_urlopen_blocks_redirect_to_metadata_ip() -> None:
    import urllib.error
    import urllib.request

    from bog_agents.middleware import browser_agent

    # Simulate the first hop returning a 302 to the cloud-metadata IP.
    class _FakeOpener:
        def open(self, req, timeout=30):
            raise urllib.error.HTTPError(
                req.full_url,
                302,
                "Found",
                {"Location": "http://169.254.169.254/latest/meta-data/"},
                None,
            )

    browser_agent.urllib.request.build_opener = lambda *a, **k: _FakeOpener()  # type: ignore[assignment]
    req = urllib.request.Request("https://example.com/", method="GET")
    with pytest.raises(PermissionError) as exc:
        browser_agent.safe_urlopen(req, allow_private_ips=False)
    assert "169.254" in str(exc.value) or "link-local" in str(exc.value).lower() or "metadata" in str(exc.value).lower()
