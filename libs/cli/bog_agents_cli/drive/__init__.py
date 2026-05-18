"""``bog-agents drive`` — non-interactive scripted TUI driver.

Public surface:

* :func:`run_script_path` — load a YAML file and run it.
* :func:`run_script` — run a pre-parsed :class:`Script`.
* :func:`load_script` / :func:`parse_script` — script parsing.
* :class:`RunOptions`, :class:`ScriptResult`, :class:`StepResult`.

See :mod:`bog_agents_cli.drive.actions` for the action grammar and
:mod:`bog_agents_cli.drive.runner` for the dispatch loop.
"""

from __future__ import annotations

from bog_agents_cli.drive.actions import (
    Action,
    Script,
    ScriptLoadError,
    SessionConfig,
    load_script,
    parse_script,
)
from bog_agents_cli.drive.runner import (
    RunOptions,
    ScriptResult,
    StepResult,
    build_app_for_script,
    run_script,
    run_script_path,
)

__all__ = [
    "Action",
    "RunOptions",
    "Script",
    "ScriptLoadError",
    "ScriptResult",
    "SessionConfig",
    "StepResult",
    "build_app_for_script",
    "load_script",
    "parse_script",
    "run_script",
    "run_script_path",
]
