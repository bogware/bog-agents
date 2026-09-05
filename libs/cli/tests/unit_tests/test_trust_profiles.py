"""ROADMAP #48: trust profiles, `--restricted`, the web domain policy, workspace trust and authority tiers."""

from __future__ import annotations

import ipaddress
import json
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from bog_agents.middleware.permissions import _check_fs_permission

from bog_agents_cli import web_fetch, workspace_trust as wt
from bog_agents_cli._server_config import ServerConfig
from bog_agents_cli._server_constants import ENV_PREFIX
from bog_agents_cli.mcp_trust import (
    compute_config_fingerprint,
    is_project_hooks_trusted,
    is_project_mcp_trusted,
)
from bog_agents_cli.project_hooks import hooks_fingerprint
from bog_agents_cli.self_protection import authority_file_permissions
from bog_agents_cli.trust_profiles import (
    RESTRICTED_TOOL_NAMES,
    TrustProfile,
    mode_change_refusal,
    resolve_trust_profile,
    restricted_profile,
    trust_profile_from_settings,
)
from bog_agents_cli.web_fetch import (
    DomainPolicyError,
    SsrfError,
    WebFetchError,
    assert_fetch_allowed,
)
from bog_agents_cli.web_policy import (
    WebPolicy,
    get_web_policy,
    policy_from_strings,
    set_web_policy,
)

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture(autouse=True)
def _clear_policy() -> Iterator[None]:
    set_web_policy(None)
    yield
    set_web_policy(None)


# ---------------------------------------------------------------------------
# TrustProfile
# ---------------------------------------------------------------------------


class TestTrustProfile:
    def test_restricted_preset_strips_tool_families(self) -> None:
        profile = restricted_profile()
        assert profile.restricted and profile.lock_mode and profile.strips_shell
        for name in RESTRICTED_TOOL_NAMES:
            assert profile.tool_excluded(name), name
        # No allow-list: nothing to fetch from, so fetch_url goes too.
        assert profile.tool_excluded("fetch_url")
        assert not profile.tool_excluded("read_file")
        assert not profile.tool_excluded("edit_file")

    def test_restricted_keeps_fetch_url_with_an_allowlist(self) -> None:
        profile = restricted_profile(allowed_domains=("docs.python.org",))
        assert not profile.tool_excluded("fetch_url")
        assert profile.tool_excluded("http_request")

    def test_mode_change_refusals(self) -> None:
        restricted = restricted_profile()
        assert mode_change_refusal(restricted, "bypass")
        assert mode_change_refusal(restricted, "accept-edits")
        for mode in ("default", "plan", "paranoid"):
            assert mode_change_refusal(restricted, mode) is None, mode
        locked_plan = TrustProfile(
            name="reviewer", permission_mode="plan", lock_mode=True
        )
        assert "locks the permission mode" in (
            mode_change_refusal(locked_plan, "default") or ""
        )
        assert mode_change_refusal(locked_plan, "paranoid") is None
        assert mode_change_refusal(TrustProfile(), "bypass") is None

    def test_from_settings(self) -> None:
        assert trust_profile_from_settings("p", None) is None
        assert trust_profile_from_settings("p", {"other": 1}) is None
        profile = trust_profile_from_settings(
            "p",
            {
                "trust": {
                    "permission_mode": "plan",
                    "allowed_domains": ["Example.com", " docs.python.org "],
                    "blocked_domains": "evil.com, bad.org",
                    "excluded_tools": ["web_search"],
                }
            },
        )
        assert profile is not None
        assert profile.permission_mode == "plan"
        assert profile.lock_mode is False
        assert profile.allowed_domains == ("Example.com", "docs.python.org")
        assert profile.blocked_domains == ("evil.com", "bad.org")
        assert profile.tool_excluded("web_search") and not profile.tool_excluded(
            "execute"
        )
        restricted = trust_profile_from_settings("p", {"trust": {"restricted": True}})
        assert restricted is not None and restricted.restricted and restricted.lock_mode
        with pytest.raises(ValueError, match="unknown permission_mode"):
            trust_profile_from_settings("p", {"trust": {"permission_mode": "yolo"}})

    def test_resolve_from_profiles_json(self, tmp_path: Path) -> None:
        (tmp_path / "profiles.json").write_text(
            json.dumps(
                {
                    "locked": {
                        "description": "review only",
                        "custom_settings": {
                            "trust": {
                                "permission_mode": "plan",
                                "lock_mode": True,
                                "allowed_domains": ["example.com"],
                            }
                        },
                    },
                    "plain": {"description": "no policy"},
                }
            ),
            encoding="utf-8",
        )
        named = resolve_trust_profile(
            restricted=False, profile_name="locked", config_dir=tmp_path
        )
        assert (
            named.name == "locked"
            and named.permission_mode == "plan"
            and named.lock_mode
        )
        # --restricted wins but inherits the named profile's allow-list.
        forced = resolve_trust_profile(
            restricted=True, profile_name="locked", config_dir=tmp_path
        )
        assert forced.restricted and forced.allowed_domains == ("example.com",)
        assert (
            resolve_trust_profile(
                restricted=False, profile_name="plain", config_dir=tmp_path
            )
            == TrustProfile()
        )
        assert (
            resolve_trust_profile(
                restricted=False, profile_name="missing", config_dir=tmp_path
            )
            == TrustProfile()
        )
        assert resolve_trust_profile(restricted=False) == TrustProfile()


# ---------------------------------------------------------------------------
# WebPolicy + the fetch gate
# ---------------------------------------------------------------------------


class TestWebPolicy:
    def test_suffix_matching_on_label_boundaries(self) -> None:
        blocked = WebPolicy(blocked_domains=("evil.com",))
        assert blocked.violation("api.evil.com")
        assert blocked.violation("EVIL.com.")
        assert blocked.violation("notevil.com") is None
        allowed = WebPolicy(allowed_domains=("example.com",))
        assert allowed.violation("example.com") is None
        assert allowed.violation("api.example.com") is None
        assert "not on the allowed-domain list" in (
            allowed.violation("other.org") or ""
        )

    def test_blocklist_wins_and_empty_policy_is_a_noop(self) -> None:
        both = WebPolicy(
            allowed_domains=("example.com",), blocked_domains=("bad.example.com",)
        )
        assert both.violation("bad.example.com")
        assert both.violation("good.example.com") is None
        assert not WebPolicy().active
        assert WebPolicy().violation("anything.invalid") is None

    def test_merge_and_parse(self) -> None:
        merged = WebPolicy(allowed_domains=("a.com",)).merged(
            WebPolicy(allowed_domains=("a.com", "b.org"), blocked_domains=("x.net",))
        )
        assert merged == WebPolicy(
            allowed_domains=("a.com", "b.org"), blocked_domains=("x.net",)
        )
        assert WebPolicy(allowed_domains=("a.com",)).merged(None).allowed_domains == (
            "a.com",
        )
        assert policy_from_strings(" A.com, b.org ,", None) == WebPolicy(
            allowed_domains=("a.com", "b.org")
        )
        assert policy_from_strings(None, "") == WebPolicy()

    def test_process_policy_roundtrip(self) -> None:
        set_web_policy(WebPolicy(blocked_domains=("evil.com",)))
        assert get_web_policy().blocked_domains == ("evil.com",)
        set_web_policy(None)
        assert not get_web_policy().active


class TestFetchGate:
    def test_blocked_host_is_refused_before_dns(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        set_web_policy(WebPolicy(blocked_domains=("evil.com",)))

        def _no_dns(host: str) -> list[ipaddress.IPv4Address]:
            msg = f"DNS consulted for {host}"
            raise AssertionError(msg)

        monkeypatch.setattr(web_fetch, "_resolve_host_addresses", _no_dns)
        with pytest.raises(DomainPolicyError, match="blocked-domain"):
            assert_fetch_allowed("http://api.evil.com/x")
        assert issubclass(DomainPolicyError, WebFetchError)

    def test_allowlist_permits_and_refuses(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        set_web_policy(WebPolicy(allowed_domains=("example.com",)))
        monkeypatch.setattr(
            web_fetch,
            "_resolve_host_addresses",
            lambda host: [ipaddress.ip_address("93.184.216.34")],
        )
        assert_fetch_allowed("https://www.example.com/")
        with pytest.raises(DomainPolicyError, match="allowed-domain"):
            assert_fetch_allowed("https://example.org/")

    def test_ssrf_guard_still_runs_without_a_policy(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            web_fetch,
            "_resolve_host_addresses",
            lambda host: [ipaddress.ip_address("169.254.169.254")],
        )
        with pytest.raises(SsrfError, match="non-public"):
            assert_fetch_allowed("http://metadata.internal/latest")


# ---------------------------------------------------------------------------
# Authority tiers
# ---------------------------------------------------------------------------


class TestAuthorityTiers:
    def test_git_internals_are_denied_in_every_mode(self) -> None:
        for restricted in (False, True):
            rules = authority_file_permissions(restricted=restricted)
            assert (
                _check_fs_permission(rules, "write", "/.git/hooks/pre-commit") == "deny"
            )
            assert _check_fs_permission(rules, "write", "/.git/config") == "deny"
            assert _check_fs_permission(rules, "read", "/.git/config") == "allow"

    def test_automation_files_interrupt_by_default_and_deny_when_restricted(
        self,
    ) -> None:
        default = authority_file_permissions()
        for path in (
            "/.github/workflows/ci.yml",
            "/.vscode/tasks.json",
            "/.claude/settings.json",
            "/.cursor/rules/a.mdc",
            "/.bog-agents/sandbox.toml",
        ):
            assert _check_fs_permission(default, "write", path) == "interrupt", path
        restricted = authority_file_permissions(restricted=True)
        for path in (
            "/.github/workflows/ci.yml",
            "/.mcp.json",
            "/.bog-agents/hooks/pre.sh",
            "/.bog-agents/sandbox.toml",
        ):
            assert _check_fs_permission(restricted, "write", path) == "deny", path
        assert (
            _check_fs_permission(restricted, "write", "/.claude/settings.json")
            == "interrupt"
        )
        for path in (
            "/src/main.py",
            "/.bog-agents/goal.json",
            "/.bog-agents/memory/note.md",
        ):
            assert _check_fs_permission(restricted, "write", path) == "allow", path


# ---------------------------------------------------------------------------
# Workspace trust
# ---------------------------------------------------------------------------


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / ".bog-agents" / "hooks").mkdir(parents=True)
    (root / "CLAUDE.md").write_text("# rules\n", encoding="utf-8")
    (root / ".bog-agents" / "hooks" / "pre.sh").write_text(
        "echo hi\n", encoding="utf-8"
    )
    (root / ".mcp.json").write_text('{"mcpServers": {}}', encoding="utf-8")
    (root / "src").mkdir()
    (root / "src" / "main.py").write_text("print(1)\n", encoding="utf-8")
    return root


class TestWorkspaceTrust:
    def test_fingerprint_covers_only_repo_controlled_files(
        self, tmp_path: Path
    ) -> None:
        root = _repo(tmp_path)
        names = {p.relative_to(root).as_posix() for p in wt.workspace_files(root)}
        assert names == {"CLAUDE.md", ".bog-agents/hooks/pre.sh", ".mcp.json"}
        before = wt.workspace_fingerprint(root)
        (root / "src" / "main.py").write_text("print(2)\n", encoding="utf-8")
        assert wt.workspace_fingerprint(root) == before
        (root / "CLAUDE.md").write_text("# changed\n", encoding="utf-8")
        assert wt.workspace_fingerprint(root) != before

    def test_trust_change_and_revoke(self, tmp_path: Path) -> None:
        root = _repo(tmp_path)
        cfg = tmp_path / "config.toml"
        assert not wt.is_workspace_trusted(root, config_path=cfg)
        assert "never acknowledged" in wt.workspace_status(root, config_path=cfg)

        fingerprint = wt.trust_workspace(root, config_path=cfg)
        assert fingerprint.startswith("sha256:")
        assert wt.is_workspace_trusted(root, config_path=cfg)
        assert "trusted (" in wt.workspace_status(root, config_path=cfg)
        key = str(root.resolve())
        assert is_project_hooks_trusted(key, hooks_fingerprint(root), config_path=cfg)
        assert is_project_mcp_trusted(
            key, compute_config_fingerprint([root / ".mcp.json"]), config_path=cfg
        )

        (root / "CLAUDE.md").write_text("# tampered\n", encoding="utf-8")
        assert not wt.is_workspace_trusted(root, config_path=cfg)
        assert "CHANGED" in wt.workspace_status(root, config_path=cfg)

        assert wt.revoke_workspace_trust(root, config_path=cfg)
        assert not wt.revoke_workspace_trust(root, config_path=cfg)
        assert "never acknowledged" in wt.workspace_status(root, config_path=cfg)


# ---------------------------------------------------------------------------
# Plumbing: ServerConfig, argparse, create_cli_agent
# ---------------------------------------------------------------------------


class TestPlumbing:
    def test_server_config_round_trips_restricted(self) -> None:
        env_dict = ServerConfig(restricted=True).to_env()
        assert env_dict["RESTRICTED"] == "true"
        with patch.dict(os.environ, {}, clear=True):
            for suffix, value in env_dict.items():
                if value is not None:
                    os.environ[f"{ENV_PREFIX}{suffix}"] = value
            restored = ServerConfig.from_env()
        assert restored.restricted is True
        assert ServerConfig().restricted is False

    def test_restricted_flag_parses(self) -> None:
        from bog_agents_cli.main import parse_args

        with patch.object(sys, "argv", ["bog-agents", "--restricted"]):
            assert parse_args().restricted is True
        with patch.object(sys, "argv", ["bog-agents"]):
            assert parse_args().restricted is False

    def test_restricted_agent_drops_shell_git_daemon_and_web_tools(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from bog_agents.token_audit import audit_agent

        from bog_agents_cli.agent import create_cli_agent
        from bog_agents_cli.tools import fetch_url, http_request, web_search

        monkeypatch.setattr(
            "bog_agents_cli.daemon_client.is_daemon_running", lambda: True
        )

        def _names(restricted: bool) -> set[str]:
            def _build(model: object) -> object:
                return create_cli_agent(
                    model=model,  # type: ignore[arg-type]
                    assistant_id="agent",
                    tools=[http_request, fetch_url, web_search],
                    auto_approve=True,
                    cwd=tmp_path,
                    restricted=restricted,
                )

            return {t.name for t in audit_agent(_build, method="approx").tools}

        open_names = _names(False)
        assert {
            "execute",
            "http_request",
            "fetch_url",
            "web_search",
            "schedule",
        } <= open_names
        assert any(n.startswith("git_") for n in open_names)

        names = _names(True)
        assert "read_file" in names and "edit_file" in names
        assert not names & RESTRICTED_TOOL_NAMES, names & RESTRICTED_TOOL_NAMES
        for gone in (
            "execute",
            "http_request",
            "fetch_url",
            "web_search",
            "schedule",
            "subscribe",
        ):
            assert gone not in names, gone
        assert not any(n.startswith("git_") for n in names)

    def test_no_surviving_restricted_tool_spawns_processes(
        self, tmp_path: Path
    ) -> None:
        """Drift guard: a new process-spawning tool must be added to `RESTRICTED_TOOL_NAMES`."""
        import inspect
        import re

        from bog_agents.token_audit import audit_agent, capture_assembly

        from bog_agents_cli.agent import create_cli_agent

        pattern = re.compile(
            r"subprocess|urllib\.request|httpx|socket\.|webbrowser|Popen|os\.system|pyperclip"
        )
        safe_modules = {
            "notifications"
        }  # desktop notifications: fixed binaries, message text only
        captured: dict[str, list[object]] = {}

        def _build(model: object) -> object:
            with capture_assembly(
                lambda a: captured.setdefault("middleware", list(a.middleware))
            ):
                return create_cli_agent(
                    model=model, assistant_id="agent", cwd=tmp_path, restricted=True
                )  # type: ignore[arg-type]

        bound = {t.name for t in audit_agent(_build, method="approx").tools}
        offenders: set[str] = set()
        for mw in captured["middleware"]:
            for tool in getattr(mw, "tools", None) or []:
                name = getattr(tool, "name", "")
                if name not in bound:
                    continue
                fn = (
                    getattr(tool, "func", None)
                    or getattr(tool, "coroutine", None)
                    or tool
                )
                module = sys.modules.get(getattr(fn, "__module__", ""))
                if module is None or module.__name__.rsplit(".", 1)[-1] in safe_modules:
                    continue
                if pattern.search(inspect.getsource(module)):
                    offenders.add(f"{name} ({module.__name__})")
        assert not offenders, sorted(offenders)

    def test_web_policy_installed_from_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from bog_agents_cli.tokens_audit_controller import audit_cli_agent

        monkeypatch.setenv("BOG_AGENTS_WEB_BLOCKED_DOMAINS", "evil.com, bad.org")
        monkeypatch.setenv("BOG_AGENTS_WEB_ALLOWED_DOMAINS", "")
        audit_cli_agent(harness_profile=None, cwd=tmp_path, method="approx")
        assert get_web_policy().blocked_domains == ("evil.com", "bad.org")

    def test_config_allowlist_keeps_fetch_url_when_restricted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from bog_agents.token_audit import audit_agent

        from bog_agents_cli.agent import create_cli_agent
        from bog_agents_cli.tools import fetch_url

        monkeypatch.setenv("BOG_AGENTS_WEB_ALLOWED_DOMAINS", "docs.python.org")

        def _build(model: object) -> object:
            return create_cli_agent(
                model=model,  # type: ignore[arg-type]
                assistant_id="agent",
                tools=[fetch_url],
                cwd=tmp_path,
                restricted=True,
            )

        names = {t.name for t in audit_agent(_build, method="approx").tools}
        assert "fetch_url" in names
        assert get_web_policy().allowed_domains == ("docs.python.org",)


# ---------------------------------------------------------------------------
# /permissions glue
# ---------------------------------------------------------------------------


class TestTrustController:
    def test_verbs_and_rows(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from bog_agents_cli import mcp_trust, trust_controller

        root = _repo(tmp_path)
        monkeypatch.setattr(mcp_trust, "_DEFAULT_CONFIG_PATH", tmp_path / "config.toml")
        assert trust_controller.run_permissions_verb("nonsense", str(root)) is None
        assert "was not trusted" in (
            trust_controller.run_permissions_verb("revoke-workspace", str(root)) or ""
        )
        reply = (
            trust_controller.run_permissions_verb("trust-workspace", str(root)) or ""
        )
        assert "Trusted this workspace" in reply and "3 repo-controlled" in reply
        assert wt.is_workspace_trusted(root, config_path=tmp_path / "config.toml")
        assert "revoked" in (
            trust_controller.run_permissions_verb("revoke-workspace", str(root)) or ""
        )

        rows = trust_controller.trust_rows(str(root), True, None)
        assert rows[0].startswith("Trust profile: restricted")
        assert any(r.startswith("Web domains:") for r in rows)
        assert any(r.startswith("Workspace trust:") for r in rows)

    def test_mode_refusal_only_when_gated(self) -> None:
        from bog_agents_cli.trust_controller import mode_refusal

        assert mode_refusal("bypass", restricted=False, profile_name=None) is None
        refusal = mode_refusal("bypass", restricted=True, profile_name=None)
        assert refusal is not None and refusal[0].isupper()
        assert mode_refusal("plan", restricted=True, profile_name=None) is None
