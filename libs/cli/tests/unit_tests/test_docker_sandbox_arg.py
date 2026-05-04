"""Smoke tests for the ``--sandbox docker`` integration."""

from __future__ import annotations


def test_main_argparse_accepts_docker_sandbox(monkeypatch) -> None:
    """``--sandbox docker`` must be accepted by the CLI argparser."""
    import sys

    from bog_agents_cli.main import parse_args

    monkeypatch.setattr(sys, "argv", ["bog-agents", "--sandbox", "docker"])
    args = parse_args()
    assert args.sandbox == "docker"


def test_sandbox_factory_lists_docker_in_available_types() -> None:
    """The factory's working-dir map must know about docker."""
    from bog_agents_cli.integrations.sandbox_factory import _get_available_sandbox_types

    assert "docker" in _get_available_sandbox_types()


def test_sandbox_factory_default_working_dir_for_docker() -> None:
    from bog_agents_cli.integrations.sandbox_factory import get_default_working_dir

    assert get_default_working_dir("docker") == "/workspace"


def test_get_provider_dispatches_to_docker_factory() -> None:
    """``_get_provider("docker")`` must return a non-None SandboxProvider."""
    from bog_agents_cli.integrations.sandbox_factory import _get_provider

    provider = _get_provider("docker")
    # We don't actually start docker — just confirm dispatch worked.
    assert provider is not None
    # Provider must expose the SandboxProvider protocol surface.
    assert hasattr(provider, "get_or_create")
    assert hasattr(provider, "delete")
