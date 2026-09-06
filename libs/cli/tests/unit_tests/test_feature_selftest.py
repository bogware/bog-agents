"""The advertised slash-command surface must stay honest (v6 Wave 0, CLI-12).

This is the drift test behind `bog-agents --doctor-features`: every registered
command has a handler, implements the subcommands its spec advertises, and does
not depend on the never-assigned `self._middleware` (the v6 CLI-1 root cause).

The real audit runs in a fresh interpreter so it sees the modules exactly as a
user's `bog-agents --doctor-features` would, unaffected by whatever other tests
in the same xdist worker patched onto `BogAgentsApp` or into `sys.modules`.
"""

from __future__ import annotations

import subprocess
import sys

from bog_agents_cli.feature_selftest import (
    CommandAudit,
    audit_command_surface,
    render_audit,
)

_AUDIT_SCRIPT = (
    "import sys\n"
    "from bog_agents_cli.feature_selftest import audit_command_surface, render_audit\n"
    "audits = audit_command_surface()\n"
    "print(render_audit(audits))\n"
    "print('COUNT', len(audits))\n"
    "sys.exit(0 if all(a.ok for a in audits) else 1)\n"
)


def test_every_registered_command_is_honest() -> None:
    result = subprocess.run(
        [sys.executable, "-c", _AUDIT_SCRIPT],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    count = int(result.stdout.strip().rsplit("COUNT", 1)[1])
    assert count >= 120


def test_previously_dead_commands_are_covered() -> None:
    names = {a.name for a in audit_command_surface()}
    assert {"/think", "/worktrees", "/checkpoint", "/search", "/compress"} <= names


def test_missing_handler_is_reported() -> None:
    class _NoHandlers:
        pass

    audits = audit_command_surface(app_cls=_NoHandlers)
    assert audits and all(not a.ok for a in audits)
    assert any("is missing on _NoHandlers" in p for a in audits for p in a.problems)


def test_dead_middleware_lookup_is_flagged() -> None:
    class _App:
        async def _handle_think_command(self, command: str) -> None:
            mw = next((m for m in getattr(self, "_middleware", []) if m), None)
            assert mw is None

    audits = {a.name: a for a in audit_command_surface(app_cls=_App)}
    think = audits["/think"]
    assert any("self._middleware" in p for p in think.problems), think


def test_render_audit_summarises() -> None:
    ok = CommandAudit(name="/a", handler="_handle_a_command")
    bad = CommandAudit(
        name="/b",
        handler="_handle_b_command",
        problems=("advertised subcommand 'x' is not implemented by the handler",),
    )
    text = render_audit([ok, bad])
    assert "2 commands audited, 1 with problems" in text
    assert "/b" in text and "'x'" in text
