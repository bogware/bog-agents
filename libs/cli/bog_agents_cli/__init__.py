"""Bog Agents CLI - Interactive AI coding assistant."""

from bog_agents_cli._version import __version__


def cli_main() -> None:  # noqa: RUF067
    """Run the CLI entry point without importing heavy modules on package import."""
    from bog_agents_cli.main import cli_main as _cli_main

    _cli_main()


__all__ = [
    "__version__",
    "cli_main",
]
