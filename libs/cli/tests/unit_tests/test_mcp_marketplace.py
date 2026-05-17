"""Unit tests for the /mcp marketplace + installer (Gap 5).

No real ~/.bog-agents/ writes — the user MCP-config path is monkey-
patched to a tmp_path per test so the catalog flow exercises the
real json round-trip without touching the developer's home dir.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bog_agents_cli import mcp_config_manager
from bog_agents_cli.mcp_marketplace import (
    CATALOG,
    InstallResult,
    MarketplaceEntry,
    find_entry,
    install,
    render_entry_detail,
    render_install_outcome,
    render_marketplace_listing,
    search_entries,
)
from bog_agents_cli.mcp_marketplace_controller import handle as mcp_handle


@pytest.fixture(autouse=True)
def _isolated_mcp_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the user-MCP config path so tests never touch ~/.bog-agents/.

    Returns the tmp path so individual tests can read it back.
    """
    cfg = tmp_path / ".mcp.json"
    monkeypatch.setattr(mcp_config_manager, "_USER_MCP_CONFIG", cfg)
    return cfg


# ---------------------------------------------------------------------------
# Catalog + look-up
# ---------------------------------------------------------------------------


class TestCatalog:
    def test_every_entry_has_required_fields(self):
        for entry in CATALOG:
            assert entry.name
            assert entry.title
            assert entry.summary
            assert entry.category
            assert entry.command
            assert isinstance(entry.args, tuple)

    def test_catalog_names_are_unique(self):
        names = [e.name for e in CATALOG]
        assert len(names) == len(set(names))

    def test_find_entry_exact_match(self):
        entry = find_entry("github")
        assert entry is not None
        assert entry.name == "github"

    def test_find_entry_case_insensitive(self):
        assert find_entry("GITHUB") is not None
        assert find_entry("  jira  ") is not None

    def test_find_entry_unknown_returns_none(self):
        assert find_entry("definitely-not-real") is None

    def test_search_by_tag(self):
        results = search_entries("vcs")
        names = {e.name for e in results}
        assert "github" in names or "git" in names

    def test_search_by_category(self):
        results = search_entries("data")
        names = {e.name for e in results}
        assert {"postgres", "sqlite"} <= names

    def test_search_empty_returns_all(self):
        assert len(search_entries("")) == len(CATALOG)


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------


class TestRenderers:
    def test_listing_groups_by_category(self):
        out = render_marketplace_listing()
        # Should contain at least one category header in [brackets].
        assert "[" in out
        assert "]" in out
        # Should mention install command.
        assert "/mcp install" in out

    def test_listing_handles_empty_result(self):
        out = render_marketplace_listing([])
        assert "No marketplace entries" in out

    def test_entry_detail_lists_required_env(self):
        entry = find_entry("jira")
        assert entry is not None
        out = render_entry_detail(entry)
        for var in entry.env_required:
            assert var in out

    def test_install_outcome_overwritten_flag(self):
        entry = find_entry("git")
        assert entry is not None
        result = InstallResult(
            entry=entry, server_name="git", was_overwritten=True
        )
        out = render_install_outcome(result)
        assert "Reinstalled" in out
        assert "git" in out

    def test_install_outcome_missing_required(self):
        entry = find_entry("jira")
        assert entry is not None
        result = InstallResult(
            entry=entry,
            server_name="jira",
            was_overwritten=False,
            missing_required=("JIRA_URL",),
        )
        out = render_install_outcome(result)
        assert "JIRA_URL" in out
        assert "Aborted" in out


# ---------------------------------------------------------------------------
# Install / uninstall
# ---------------------------------------------------------------------------


def _prompter(answers: dict[str, str]):
    """Build a CredentialPrompt stub returning canned values."""

    def _prompt(var: str, *, required: bool) -> str:
        _ = required
        return answers.get(var, "")

    return _prompt


class TestInstall:
    def test_install_with_inline_overrides(self):
        result = install(
            "git",
            env_overrides={"GIT_REPO_PATH": "/tmp/repo"},
        )
        assert result.was_overwritten is False
        assert result.missing_required == ()
        servers = mcp_config_manager.list_servers()
        assert "git" in servers
        assert servers["git"]["command"] == "uvx"

    def test_install_required_via_prompter(self):
        prompter = _prompter(
            {
                "JIRA_URL": "https://acme.atlassian.net",
                "JIRA_USERNAME": "scott@example.com",
                "JIRA_API_TOKEN": "tok-123",
            }
        )
        result = install("jira", prompt=prompter)
        assert result.missing_required == ()
        servers = mcp_config_manager.list_servers()
        assert servers["jira"]["env"]["JIRA_URL"] == "https://acme.atlassian.net"
        assert servers["jira"]["env"]["JIRA_API_TOKEN"] == "tok-123"

    def test_install_missing_required_without_prompter(self):
        result = install("jira")
        assert result.missing_required == (
            "JIRA_URL",
            "JIRA_USERNAME",
            "JIRA_API_TOKEN",
        )
        # And nothing was written.
        assert "jira" not in mcp_config_manager.list_servers()

    def test_install_duplicate_without_overwrite_raises(self):
        install("git", env_overrides={"GIT_REPO_PATH": "/x"})
        with pytest.raises(ValueError, match="already registered"):
            install("git", env_overrides={"GIT_REPO_PATH": "/y"})

    def test_install_overwrite_replaces_entry(self):
        install("git", env_overrides={"GIT_REPO_PATH": "/x"})
        result = install(
            "git",
            env_overrides={"GIT_REPO_PATH": "/new"},
            overwrite=True,
        )
        assert result.was_overwritten is True
        servers = mcp_config_manager.list_servers()
        assert servers["git"]["env"]["GIT_REPO_PATH"] == "/new"

    def test_install_unknown_name_raises(self):
        with pytest.raises(ValueError, match="No marketplace entry"):
            install("not-a-thing")

    def test_install_as_alias(self):
        install(
            "jira",
            env_overrides={
                "JIRA_URL": "u",
                "JIRA_USERNAME": "n",
                "JIRA_API_TOKEN": "t",
            },
            install_as="jira-staging",
        )
        servers = mcp_config_manager.list_servers()
        assert "jira-staging" in servers
        assert "jira" not in servers


# ---------------------------------------------------------------------------
# Controller dispatch
# ---------------------------------------------------------------------------


class TestController:
    def test_help_when_empty(self):
        out = mcp_handle("/mcp")
        assert "/mcp marketplace" in out
        assert "/mcp install" in out

    def test_marketplace_listing(self):
        out = mcp_handle("/mcp marketplace")
        assert "[git]" in out or "[ticketing]" in out
        assert "github" in out or "jira" in out

    def test_marketplace_with_query(self):
        out = mcp_handle("/mcp marketplace jira")
        assert "jira" in out

    def test_show_known_entry(self):
        out = mcp_handle("/mcp show postgres")
        assert "postgres" in out.lower()
        assert "POSTGRES_CONNECTION_STRING" in out

    def test_show_unknown_entry(self):
        out = mcp_handle("/mcp show not-a-thing")
        assert "No marketplace entry" in out

    def test_install_inline_kv_pairs(self):
        out = mcp_handle("/mcp install git GIT_REPO_PATH=/tmp")
        assert "Installed" in out or "git" in out
        assert "git" in mcp_config_manager.list_servers()

    def test_install_missing_required_renders_help(self):
        out = mcp_handle("/mcp install jira")
        assert "JIRA_URL" in out
        assert "Aborted" in out

    def test_install_overwrite_flag(self):
        mcp_handle("/mcp install git GIT_REPO_PATH=/x")
        out = mcp_handle("/mcp install git GIT_REPO_PATH=/y --overwrite")
        assert "Reinstalled" in out
        assert mcp_config_manager.list_servers()["git"]["env"]["GIT_REPO_PATH"] == "/y"

    def test_install_as_flag(self):
        out = mcp_handle(
            "/mcp install git GIT_REPO_PATH=/foo --as git-foo"
        )
        assert "git-foo" in out
        assert "git-foo" in mcp_config_manager.list_servers()

    def test_uninstall_existing(self):
        mcp_handle("/mcp install git GIT_REPO_PATH=/x")
        out = mcp_handle("/mcp uninstall git")
        assert "Removed" in out
        assert "git" not in mcp_config_manager.list_servers()

    def test_uninstall_missing(self):
        out = mcp_handle("/mcp uninstall ghost")
        assert "No MCP server" in out

    def test_unknown_subcommand_falls_back_to_help(self):
        out = mcp_handle("/mcp nope")
        assert "/mcp marketplace" in out

    def test_unparseable_args_handled(self):
        # Unbalanced quotes → shlex.split raises ValueError.
        out = mcp_handle('/mcp install jira "unbalanced')
        assert "Could not parse" in out


@pytest.fixture
def _entry_for_isolation() -> MarketplaceEntry:
    """Sanity fixture so failures point at MarketplaceEntry not the catalog."""
    return MarketplaceEntry(
        name="probe",
        title="Probe",
        summary="test entry",
        category="test",
        command="echo",
        args=("hello",),
    )
