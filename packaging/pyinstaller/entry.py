"""PyInstaller entry point for the standalone `bog-agents` build (ROADMAP #61).

The wheel's console script is `bog_agents_cli:cli_main`; a frozen build needs a
real module to start from, so this file is that module. It deliberately does
nothing else — every import happens inside `cli_main` so the frozen bundle's
startup path matches the wheel's.
"""

from __future__ import annotations

import multiprocessing
import sys


def main() -> int:
    """Run the CLI exactly as the console script would."""
    from bog_agents_cli import cli_main

    return int(cli_main() or 0)


if __name__ == "__main__":
    # Frozen Windows builds re-import the entry module in child processes
    # (LangGraph server, background tasks); without this they would re-run main.
    multiprocessing.freeze_support()
    sys.exit(main())
