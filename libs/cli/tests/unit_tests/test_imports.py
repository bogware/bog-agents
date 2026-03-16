"""Test importing files."""


def test_imports() -> None:
    """Test importing bog-agents modules."""
    from bog_agents_cli import (
        agent,
        integrations,
    )
    from bog_agents_cli.main import cli_main
