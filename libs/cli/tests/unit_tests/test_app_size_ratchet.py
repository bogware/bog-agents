"""Line-count ratchet for the ``app.py`` god class.

``bog_agents_cli/app.py`` is the long-lived ``BogAgentsApp`` god class. It is
large and still grows. The user's standing decision (2026-05-07) DEFERS the
mixin-extraction refactor, so this test does not try to shrink the file — it
pins a *ceiling* so new surface is pushed into ``commands/*.py`` and dedicated
controller modules instead of piling onto the god class. This is the same
"pin the shape" strategy used by the help-drift and canonical-middleware-order
tests.
"""

from __future__ import annotations

from pathlib import Path

# Ceiling on app.py's line count. Set 2026-07-12 from a measured 17,111 lines,
# rounded up to the next 100 (17,200) plus a 100-line buffer for in-flight work.
# This is a deliberate ratchet, NOT a target: it only ever moves DOWN for free.
# If you are a legitimate grower and this test fails, do NOT silence it by
# stuffing more into app.py — add new handlers to commands/*.py plus a testable
# controller module (see CLAUDE.md and expert_controller.py for the pattern).
# Only bump this constant when the growth genuinely belongs on BogAgentsApp, and
# bump it deliberately in the same change that adds the lines.
APP_PY_LINE_CEILING = 17_300

_APP_PY = Path(__file__).resolve().parents[2] / "bog_agents_cli" / "app.py"


def test_app_py_stays_under_ceiling() -> None:
    """Fail if app.py grows past its pinned ceiling.

    Guards against the god class accreting new surface that belongs in
    ``commands/*.py`` + controller modules.
    """
    assert _APP_PY.is_file(), f"expected app.py at {_APP_PY}"

    line_count = len(_APP_PY.read_text(encoding="utf-8").splitlines())

    assert line_count <= APP_PY_LINE_CEILING, (
        f"app.py has grown to {line_count} lines, over the "
        f"{APP_PY_LINE_CEILING}-line ceiling.\n"
        "The BogAgentsApp god class is deliberately capped so new surface is "
        "forced into commands/*.py + a testable controller module rather than "
        "piling onto app.py (see CLAUDE.md and expert_controller.py).\n"
        "If this growth genuinely belongs on BogAgentsApp, bump "
        "APP_PY_LINE_CEILING in this file deliberately, in the same change."
    )
