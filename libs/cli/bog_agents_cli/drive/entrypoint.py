"""CLI dispatcher for ``bog-agents --drive`` / ``--drive-stdin``.

Kept separate from :mod:`bog_agents_cli.main` so the heavy Pilot import
chain only loads when a drive flag is actually present. The dispatcher
returns an integer exit code; :mod:`main` calls ``sys.exit`` with it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
from io import StringIO
from pathlib import Path
from typing import TextIO

import yaml

from bog_agents_cli.drive.actions import ScriptLoadError, load_script, parse_script
from bog_agents_cli.drive.runner import RunOptions, run_script

logger = logging.getLogger(__name__)


__all__ = ["run_drive_entrypoint"]


def run_drive_entrypoint(
    *,
    script_path: str | None,
    from_stdin: bool = False,
    artifact_dir: str | None = None,
    var_overrides: dict[str, str] | None = None,
    stop_on_failure: bool = False,
    output_path: str | None = None,
) -> int:
    """Resolve flags and run a single drive script. Returns an exit code."""
    if from_stdin:
        source = sys.stdin.read()
        try:
            data = yaml.safe_load(StringIO(source)) or {}
        except yaml.YAMLError as exc:
            sys.stderr.write(f"Error: --drive-stdin: invalid YAML: {exc}\n")
            return 2
        try:
            script = parse_script(data)
        except ScriptLoadError as exc:
            sys.stderr.write(f"Error: --drive-stdin: {exc}\n")
            return 2
    else:
        if not script_path:
            sys.stderr.write("Error: --drive requires a path argument\n")
            return 2
        path = Path(script_path)
        if not path.is_file():
            sys.stderr.write(f"Error: --drive: file not found: {path}\n")
            return 2
        try:
            script = load_script(path)
        except ScriptLoadError as exc:
            sys.stderr.write(f"Error: --drive: {exc}\n")
            return 2

    output_cm: contextlib.AbstractContextManager[TextIO]
    if output_path:
        try:
            output_cm = Path(output_path).open("w", encoding="utf-8")  # noqa: SIM115  # closed below via the ``with`` block
        except OSError as exc:
            sys.stderr.write(f"Error: --drive-output: {exc}\n")
            return 2
    else:
        # nullcontext keeps the `with` shape uniform whether we own the
        # file handle or are writing to stdout.
        output_cm = contextlib.nullcontext(sys.stdout)

    with output_cm as output_stream:
        options = RunOptions(
            artifact_dir=Path(artifact_dir) if artifact_dir else None,
            output_stream=output_stream,
            stop_on_failure=stop_on_failure,
            var_overrides=dict(var_overrides or {}),
        )
        try:
            result = asyncio.run(run_script(script, options=options))
        except Exception as exc:
            logger.exception("drive runner crashed")
            sys.stderr.write(f"Error: drive runner crashed: {exc}\n")
            return 1

    return result.exit_code


def parse_var_overrides(raw: list[str] | None) -> dict[str, str]:
    """Parse ``--drive-var name=value`` repeated flags into a dict."""
    out: dict[str, str] = {}
    for item in raw or []:
        if "=" not in item:
            sys.stderr.write(f"Error: --drive-var expects name=value, got {item!r}\n")
            sys.exit(2)
        name, _, value = item.partition("=")
        out[name.strip()] = value
    return out
