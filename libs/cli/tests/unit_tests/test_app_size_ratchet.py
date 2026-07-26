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

# Ceiling on app.py's line count. Bumped 2026-07-12 to 17,510 for the `/skills
# trust` routing: `_handle_skills_command` gains a ~10-line dispatch that
# delegates to the TUI-free `skill_trust_controller.handle_skills_command`
# (which owns all trust logic). The routing must live on BogAgentsApp because it
# mounts messages via `self._mount_message`; the controller stays testable
# without the TUI. Measured 17,504 after that change. Previous ceiling 17,500
# landed with the UX-polish modal openers (`/effort` picker, `/goal review`).
# This is a deliberate ratchet, NOT a target: it only ever moves DOWN for free.
# If you are a legitimate grower and this test fails, do NOT silence it by
# stuffing more into app.py — add new handlers to commands/*.py plus a testable
# controller module (see CLAUDE.md and expert_controller.py for the pattern).
# Only bump this constant when the growth genuinely belongs on BogAgentsApp, and
# bump it deliberately in the same change that adds the lines.
#
# Bumped 2026-07-23 to 17,575 for the v4 Wave-0 turn-lifecycle work: the
# CLI-CORE-1/-3/-4 correctness fixes (queue-drain reorder, /clear thread-id
# sync, busy-guarded deferral in _send_prompt_to_agent) plus the TurnManager
# delegation. The lifecycle LOGIC moved OUT to the new turn_manager.py module;
# what stays on BogAgentsApp is thin — three delegating properties (so the ~25
# read sites are untouched) and begin/end calls at the dispatch sites — which is
# the sanctioned "logic in a module, thin app surface" pattern this ratchet
# exists to encourage.
#
# Bumped 2026-07-26 to 17,635 for the `/best-of-n` handler (killer feature #31).
# All of the orchestration — worktree fan-out, rubric judging, ranking, winner
# selection, worktree wiring — lives in the testable `best_of_n.py` module (12
# unit tests, injected runner+judge). What lands on BogAgentsApp is only the
# thin arg-parse + spinner + result-mount glue that needs `self._mount_message`
# / `self._model_override` — exactly the sanctioned split.
APP_PY_LINE_CEILING = 17_635

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
