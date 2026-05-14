"""Unit tests for release-train enrichment (config + sources + orchestration)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from bog_agents_cli.release_train import CommitEntry
from bog_agents_cli.release_train_config import (
    HaloSourceConfig,
    JiraSourceConfig,
    ReleaseTrainConfig,
    clear_cache,
    load_release_train_config,
    release_train_config_path,
    save_release_train_config,
)
from bog_agents_cli.release_train_sources import (
    ResolvedTicket,
    SourceResolution,
    attach_tickets,
    extract_keys,
    resolve_halo_transport,
    resolve_jira_transport,
)


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate config + env from the user's machine for every test."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in (
        "BOG_AGENTS_RELEASE_TRAIN_JIRA",
        "BOG_AGENTS_RELEASE_TRAIN_HALO",
        "JIRA_EMAIL",
        "JIRA_API_TOKEN",
        "HALO_CLIENT_ID",
        "HALO_CLIENT_SECRET",
    ):
        monkeypatch.delenv(var, raising=False)
    clear_cache()


class TestReleaseTrainConfig:
    def test_defaults_are_off(self) -> None:
        """Out of the box, both sources are off — pre-feature parity."""
        cfg = load_release_train_config()
        assert cfg.jira.enabled is False
        assert cfg.halo.enabled is False
        assert cfg.any_enabled is False

    def test_save_roundtrip_jira(self) -> None:
        """Enabling jira persists to the canonical TOML and survives reload."""
        cfg = load_release_train_config()
        cfg.jira.enabled = True
        cfg.jira.api_base_url = "https://example.atlassian.net"
        cfg.jira.project_keys = ["ABC", "XYZ"]
        path = save_release_train_config(cfg)
        assert path == release_train_config_path()
        assert path.exists()

        clear_cache()
        reloaded = load_release_train_config()
        assert reloaded.jira.enabled is True
        assert reloaded.jira.api_base_url == "https://example.atlassian.net"
        assert reloaded.jira.project_keys == ["ABC", "XYZ"]
        assert reloaded.halo.enabled is False

    def test_env_var_overrides_toml(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``BOG_AGENTS_RELEASE_TRAIN_JIRA=1`` forces jira on even when TOML says off."""
        cfg = load_release_train_config()
        cfg.jira.enabled = False
        save_release_train_config(cfg)

        monkeypatch.setenv("BOG_AGENTS_RELEASE_TRAIN_JIRA", "1")
        clear_cache()
        reloaded = load_release_train_config()
        assert reloaded.jira.enabled is True

    def test_malformed_toml_falls_back_to_defaults(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A garbage TOML file does not crash — we just get defaults back."""
        bog_dir = tmp_path / ".bog-agents"
        bog_dir.mkdir(parents=True, exist_ok=True)
        (bog_dir / "release-train.toml").write_text(
            "this is not toml = = =", encoding="utf-8"
        )
        clear_cache()
        cfg = load_release_train_config()
        assert cfg.jira.enabled is False
        assert cfg.halo.enabled is False

    def test_unknown_keys_ignored_forward_compat(self) -> None:
        """Extra keys in TOML must not break loading."""
        import tomli_w

        path = release_train_config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as fh:
            tomli_w.dump(
                {
                    "release_train": {
                        "jira": {
                            "enabled": True,
                            "unknown_future_field": "ignored",
                        }
                    }
                },
                fh,
            )
        clear_cache()
        cfg = load_release_train_config()
        assert cfg.jira.enabled is True


# ---------------------------------------------------------------------------
# Key extraction
# ---------------------------------------------------------------------------


class TestExtractKeys:
    def _commits(self, *subjects: str) -> list[CommitEntry]:
        return [
            CommitEntry(sha=f"sha{i}", type="features", scope="", subject=s)
            for i, s in enumerate(subjects)
        ]

    def test_jira_keys_extracted_from_subject(self) -> None:
        commits = self._commits("feat: ABC-123 add foo", "fix: PROJ-7 stop crash")
        keys = extract_keys(commits, pattern=r"\b([A-Z][A-Z0-9]{1,9}-\d+)\b")
        assert keys == ["ABC-123", "PROJ-7"]

    def test_project_filter_narrows_keys(self) -> None:
        commits = self._commits("feat: ABC-1 thing", "feat: XYZ-2 thing")
        keys = extract_keys(
            commits,
            pattern=r"\b([A-Z][A-Z0-9]{1,9}-\d+)\b",
            project_filter=["ABC"],
        )
        assert keys == ["ABC-1"]

    def test_dedup_preserves_first_seen_order(self) -> None:
        commits = self._commits("feat: ABC-1", "fix: ABC-2", "chore: ABC-1 again")
        keys = extract_keys(commits, pattern=r"\b([A-Z][A-Z0-9]{1,9}-\d+)\b")
        assert keys == ["ABC-1", "ABC-2"]

    def test_pr_title_is_searched_too(self) -> None:
        c = CommitEntry(sha="sha1", type="features", scope="", subject="feat: misc")
        c.pr_title = "feat: ABC-99 from PR"
        keys = extract_keys([c], pattern=r"\b([A-Z][A-Z0-9]{1,9}-\d+)\b")
        assert keys == ["ABC-99"]

    def test_max_keys_cap_respected(self) -> None:
        subjects = [f"feat: ABC-{i}" for i in range(100)]
        keys = extract_keys(
            self._commits(*subjects),
            pattern=r"\b([A-Z][A-Z0-9]{1,9}-\d+)\b",
            max_keys=10,
        )
        assert len(keys) == 10

    def test_halo_regex_default(self) -> None:
        commits = self._commits(
            "fix: INC-123 outage", "feat: CHG-456 schedule", "fix: random text"
        )
        keys = extract_keys(commits, pattern=r"(?i)\b(?:HALO|CHG|INC|TKT)-?(\d+)\b")
        assert keys == ["123", "456"]

    def test_invalid_regex_returns_empty(self) -> None:
        commits = self._commits("anything")
        keys = extract_keys(commits, pattern="(unbalanced")
        assert keys == []


# ---------------------------------------------------------------------------
# Transport resolution
# ---------------------------------------------------------------------------


class TestTransportResolution:
    def test_jira_off_when_disabled(self) -> None:
        cfg = JiraSourceConfig(enabled=False)
        transport, _ = resolve_jira_transport(cfg, env={})
        assert transport == "off"

    def test_jira_api_when_creds_present(self) -> None:
        cfg = JiraSourceConfig(
            enabled=True,
            mode="auto",
            api_base_url="https://acme.atlassian.net",
            mcp_server="__nonexistent__",
        )
        transport, _ = resolve_jira_transport(cfg, env={"JIRA_API_TOKEN": "secret"})
        assert transport == "api"

    def test_jira_mode_api_without_creds_resolves_off(self) -> None:
        cfg = JiraSourceConfig(enabled=True, mode="api", api_base_url="https://x")
        transport, detail = resolve_jira_transport(cfg, env={})
        assert transport == "off"
        assert "JIRA_API_TOKEN" in detail

    def test_jira_auto_no_creds_no_mcp_is_off(self) -> None:
        cfg = JiraSourceConfig(enabled=True, mode="auto", mcp_server="__nope__")
        transport, _ = resolve_jira_transport(cfg, env={})
        assert transport == "off"

    def test_halo_api_requires_both_secrets(self) -> None:
        cfg = HaloSourceConfig(
            enabled=True,
            mode="api",
            api_base_url="https://acme.halopsa.com",
        )
        # Only one of the two creds present → still off.
        transport, _ = resolve_halo_transport(cfg, env={"HALO_CLIENT_ID": "id-only"})
        assert transport == "off"
        transport, _ = resolve_halo_transport(
            cfg,
            env={"HALO_CLIENT_ID": "id", "HALO_CLIENT_SECRET": "secret"},
        )
        assert transport == "api"

    def test_halo_off_when_disabled(self) -> None:
        cfg = HaloSourceConfig(enabled=False)
        transport, _ = resolve_halo_transport(cfg, env={})
        assert transport == "off"

    def test_jira_diagnostic_names_only_missing_piece(self) -> None:
        """Diagnostic message must specify which credential is missing.

        Regression test for the workday-found bug where the message
        said 'no api_base_url or JIRA_API_TOKEN' even when api_base_url
        was set — confusing the user about which piece was missing.
        """
        # base_url set, token missing → message names ONLY the token
        cfg = JiraSourceConfig(
            enabled=True,
            api_base_url="https://acme.atlassian.net",
            mcp_server="__nope__",
        )
        _, detail = resolve_jira_transport(cfg, env={})
        assert "JIRA_API_TOKEN" in detail
        assert "api_base_url empty" not in detail

        # base_url empty, token set → message names ONLY the base_url
        cfg2 = JiraSourceConfig(enabled=True, api_base_url="", mcp_server="__nope__")
        _, detail2 = resolve_jira_transport(cfg2, env={"JIRA_API_TOKEN": "x"})
        assert "api_base_url empty" in detail2
        assert "JIRA_API_TOKEN unset" not in detail2

        # both missing → message names both
        cfg3 = JiraSourceConfig(enabled=True, api_base_url="", mcp_server="__nope__")
        _, detail3 = resolve_jira_transport(cfg3, env={})
        assert "api_base_url empty" in detail3
        assert "JIRA_API_TOKEN unset" in detail3

    def test_halo_diagnostic_names_only_missing_piece(self) -> None:
        """Same regression for Halo — message must name only what's actually missing."""
        # All present except client_secret
        cfg = HaloSourceConfig(
            enabled=True,
            api_base_url="https://acme.halopsa.com",
            mcp_server="__nope__",
        )
        _, detail = resolve_halo_transport(cfg, env={"HALO_CLIENT_ID": "x"})
        assert "HALO_CLIENT_SECRET" in detail
        assert "HALO_CLIENT_ID unset" not in detail
        assert "api_base_url empty" not in detail


# ---------------------------------------------------------------------------
# Ticket attachment
# ---------------------------------------------------------------------------


class TestAttachTickets:
    def test_attaches_to_matching_commits(self) -> None:
        commits = [
            CommitEntry(sha="a", type="fixes", scope="", subject="fix: ABC-1 thing"),
            CommitEntry(sha="b", type="fixes", scope="", subject="fix: unrelated"),
        ]
        tickets = [ResolvedTicket(key="ABC-1", source="jira", summary="Real bug")]
        links = attach_tickets(commits, tickets, attr="jira_tickets")
        assert links == 1
        assert commits[0].jira_tickets[0].summary == "Real bug"
        assert commits[1].jira_tickets == []

    def test_attaches_multiple_to_same_commit(self) -> None:
        commits = [
            CommitEntry(
                sha="a", type="fixes", scope="", subject="fix: ABC-1 ABC-2 both"
            )
        ]
        tickets = [
            ResolvedTicket(key="ABC-1", source="jira"),
            ResolvedTicket(key="ABC-2", source="jira"),
        ]
        links = attach_tickets(commits, tickets, attr="jira_tickets")
        assert links == 2

    def test_no_tickets_returns_zero(self) -> None:
        commits = [CommitEntry(sha="a", type="fixes", scope="", subject="x")]
        assert attach_tickets(commits, [], attr="jira_tickets") == 0


# ---------------------------------------------------------------------------
# Halo helpers
# ---------------------------------------------------------------------------


class TestHaloHelpers:
    def test_key_to_ticket_id_strips_prefix(self) -> None:
        from bog_agents_cli.release_train_sources.halo import _key_to_ticket_id

        assert _key_to_ticket_id("INC-123") == "123"
        assert _key_to_ticket_id("123") == "123"
        assert _key_to_ticket_id("HALO-456") == "456"
        assert _key_to_ticket_id("") == ""


# ---------------------------------------------------------------------------
# Source rendering
# ---------------------------------------------------------------------------


class TestTicketRender:
    def test_render_includes_all_fields(self) -> None:
        ticket = ResolvedTicket(
            key="ABC-1",
            source="jira",
            summary="Stop the bleeding",
            status="Done",
            issue_type="Bug",
            fix_versions=["1.2.3"],
        )
        rendered = ticket.render()
        assert "[ABC-1]" in rendered
        assert "(Bug)" in rendered
        assert "Stop the bleeding" in rendered
        assert "<Done>" in rendered
        assert "fix=1.2.3" in rendered

    def test_render_minimal_ticket(self) -> None:
        rendered = ResolvedTicket(key="ABC-1", source="jira").render()
        assert rendered == "[ABC-1]"


# ---------------------------------------------------------------------------
# Orchestration (no network, both sources off)
# ---------------------------------------------------------------------------


class TestEnrichOrchestration:
    async def test_disabled_sources_produce_no_resolutions(self) -> None:
        from bog_agents_cli.release_train_sources import enrich_commits

        commits = [CommitEntry(sha="a", type="fixes", scope="", subject="x")]
        cfg = ReleaseTrainConfig()
        resolutions = await enrich_commits(commits, cfg)
        assert resolutions == []

    async def test_jira_enabled_but_off_at_resolve_returns_off_resolution(
        self,
    ) -> None:
        """Resolver returns an 'off' resolution rather than raising.

        When jira is enabled but creds/MCP are missing, we still
        produce a :class:`SourceResolution` with ``transport='off'``.
        """
        from bog_agents_cli.release_train_sources import enrich_commits

        cfg = ReleaseTrainConfig()
        cfg.jira.enabled = True
        cfg.jira.mode = "api"
        cfg.jira.api_base_url = ""  # no creds, no url → off
        commits = [
            CommitEntry(sha="a", type="features", scope="", subject="feat: ABC-1 x")
        ]
        resolutions = await enrich_commits(commits, cfg)
        assert len(resolutions) == 1
        assert resolutions[0].source == "jira"
        assert resolutions[0].transport == "off"


# ---------------------------------------------------------------------------
# REST parsing — Jira issue payload
# ---------------------------------------------------------------------------


class TestJiraPayloadParsing:
    def test_parse_full_jira_issue(self) -> None:
        from bog_agents_cli.release_train_sources.jira import _parse_jira_issue

        payload: dict[str, Any] = {
            "key": "ABC-1",
            "fields": {
                "summary": "Page crashes on load",
                "status": {"name": "Done"},
                "issuetype": {"name": "Bug"},
                "fixVersions": [{"name": "1.0.0"}, {"name": "1.0.1"}],
            },
        }
        ticket = _parse_jira_issue("ABC-1", payload, "https://acme.atlassian.net")
        assert ticket is not None
        assert ticket.summary == "Page crashes on load"
        assert ticket.status == "Done"
        assert ticket.issue_type == "Bug"
        assert ticket.fix_versions == ["1.0.0", "1.0.1"]
        assert ticket.url == "https://acme.atlassian.net/browse/ABC-1"

    def test_parse_partial_jira_issue(self) -> None:
        from bog_agents_cli.release_train_sources.jira import _parse_jira_issue

        ticket = _parse_jira_issue(
            "ABC-1", {"fields": {"summary": "Only the summary"}}, ""
        )
        assert ticket is not None
        assert ticket.summary == "Only the summary"
        assert ticket.status == ""
        assert ticket.fix_versions == []

    def test_parse_non_dict_payload(self) -> None:
        from bog_agents_cli.release_train_sources.jira import _parse_jira_issue

        assert _parse_jira_issue("ABC-1", "not a dict", "") is None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# REST parsing — Halo ticket payload
# ---------------------------------------------------------------------------


class TestHaloPayloadParsing:
    def test_parse_full_halo_ticket(self) -> None:
        from bog_agents_cli.release_train_sources.halo import _parse_halo_ticket

        payload: dict[str, Any] = {
            "id": 42,
            "summary": "Network outage",
            "status": {"name": "Closed"},
            "tickettype": {"name": "Incident"},
        }
        ticket = _parse_halo_ticket("INC-42", payload, "https://acme.halopsa.com")
        assert ticket is not None
        assert ticket.summary == "Network outage"
        assert ticket.status == "Closed"
        assert ticket.issue_type == "Incident"
        assert ticket.url == "https://acme.halopsa.com/tickets/42"

    def test_parse_halo_with_string_status(self) -> None:
        from bog_agents_cli.release_train_sources.halo import _parse_halo_ticket

        ticket = _parse_halo_ticket(
            "INC-1",
            {"id": 1, "summary": "x", "status": "Open"},
            "",
        )
        assert ticket is not None
        assert ticket.status == "Open"
