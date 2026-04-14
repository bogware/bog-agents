"""Tests for team orchestration and dashboard rendering."""

from __future__ import annotations

from pathlib import Path

from bog_agents_cli.dashboard import (
    AgentPanelState,
    DashboardState,
    create_dashboard_layout,
)
from bog_agents_cli.team_orchestration import (
    TeamMember,
    TeamMessage,
    TeamProfile,
    TeamRegistry,
    format_team_profile,
    load_team_registry,
    save_team_registry,
    summarize_team_activity,
)


class TestTeamOrchestration:
    """Tests for persisted team state and summaries."""

    def test_team_registry_round_trip(self, tmp_path: Path) -> None:
        """Team registry persistence should preserve members, notes, and active team."""
        registry = TeamRegistry(
            active_team="Core",
            teams=[
                TeamProfile(
                    name="Core",
                    summary="Release lane",
                    members=[TeamMember(name="Scout", role="reviewer")],
                    messages=[TeamMessage(body="Check install flow", sender="lead")],
                )
            ],
        )

        save_team_registry(registry, tmp_path)
        loaded = load_team_registry(tmp_path)

        assert loaded.active_team == "Core"
        assert len(loaded.teams) == 1
        assert loaded.teams[0].members[0].name == "Scout"
        assert loaded.teams[0].messages[0].body == "Check install flow"

    def test_summarize_team_activity_includes_notes_and_task_results(self) -> None:
        """Summary generation should blend existing notes with task output."""
        team = TeamProfile(
            name="Core",
            summary="Protect release quality",
            messages=[TeamMessage(body="Watch startup regressions")],
        )

        summary = summarize_team_activity(
            team,
            [
                "Validated provider fallback behavior.",
                "Smoke-tested remote sandbox flow.",
            ],
        )

        assert "Protect release quality" in summary
        assert "Watch startup regressions" in summary
        assert "Validated provider fallback behavior." in summary

    def test_format_team_profile_renders_operational_counts(self) -> None:
        """Team profile formatting should include workload and inbox visibility."""
        team = TeamProfile(
            name="Core",
            summary="Ship with confidence",
            members=[TeamMember(name="Scout", role="reviewer")],
            messages=[TeamMessage(body="Check the preview server", sender="lead")],
        )

        text = format_team_profile(
            team,
            active=True,
            local_tasks=2,
            remote_tasks=1,
            inbox_count=3,
        )

        assert "* Core" in text
        assert "local=2" in text
        assert "remote=1" in text
        assert "inbox=3" in text


class TestDashboardRendering:
    """Tests for team-aware dashboard rendering."""

    def test_create_dashboard_layout_includes_team_and_inbox_details(self) -> None:
        """Dashboard output should surface team summaries and pending inbox work."""
        state = DashboardState(team_summaries={"Core": "Stabilize release readiness"})
        panel = AgentPanelState(
            agent_id="bg-001",
            name="Scout",
            status="running",
            current_action="Inspecting provider config",
            tool_calls=3,
            team_name="Core",
            inbox_count=2,
        )
        state.agents[panel.agent_id] = panel

        rendered = create_dashboard_layout(state)

        assert "Teams:" in rendered
        assert "Core | agents: 1" in rendered
        assert "Team: Core" in rendered
        assert "Inbox: 2 pending messages" in rendered
