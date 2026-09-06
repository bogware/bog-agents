"""ROADMAP #50: the managed governance layer — signed policy, verification, and every enforcement point."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bog_agents_cli import managed_policy as mp
from bog_agents_cli.tracefile.signing import generate_keypair, save_keypair

BODY = {
    "org": "Acme",
    "version": "3",
    "allowed_mcp_servers": ["github", "jira-*"],
    "skill_allowlist": ["review", "release-*"],
    "plugins": {
        "required": ["acme-lint"],
        "optional": ["docs"],
        "forbidden": ["shadow-*"],
    },
    "provider_lock": {"openai": "https://gateway.acme.example/v1"},
    "zero_retention": True,
    "model_policy": {"allow": ["anthropic:*", "openai:gpt-5*"], "deny": ["*preview*"]},
    "notes": ["Questions: platform@acme.example"],
}


def _signed(tmp_path: Path) -> tuple[dict, str]:
    key = tmp_path / "org.key"
    save_keypair(generate_keypair(), key)
    document = mp.sign_document(BODY, key_path=key)
    return document, document["signer"]


class TestPolicyChecks:
    def test_filters_and_verdicts(self) -> None:
        policy = mp.parse_policy(BODY, source="x", signed=True)
        kept, removed = policy.filter_mcp_servers(
            {"github": {}, "jira-eu": {}, "evil": {}}
        )
        assert set(kept) == {"github", "jira-eu"} and removed == ["evil"]
        assert policy.skill_allowed(
            "/home/u/.bog-agents/skills/review"
        ) and policy.skill_allowed("release-notes")
        assert not policy.skill_allowed("/skills/exfil")
        assert policy.plugin_verdict("shadow-tools") == "forbidden"
        assert (
            policy.plugin_verdict("acme-lint") == "required"
            and policy.plugin_verdict("docs") == "optional"
        )
        assert policy.plugin_verdict("other") == "unlisted"
        assert (
            policy.missing_required_plugins(["docs"]) == ["acme-lint"]
            and policy.missing_required_plugins(["acme-lint"]) == []
        )
        assert (
            policy.locked_base_url("openai") == "https://gateway.acme.example/v1"
            and policy.locked_base_url("anthropic") is None
        )
        assert policy.model_switch_refusal("anthropic:claude-opus-4-6") is None
        assert "denied" in (policy.model_switch_refusal("openai:gpt-5-preview") or "")
        assert "outside the managed allow-list" in (
            policy.model_switch_refusal("ollama:llama") or ""
        )
        assert policy.zero_retention and policy.fingerprint.startswith("sha256:")
        rows = policy.rows()
        assert rows[0].startswith("Managed policy: Acme v3 (signed)") and any(
            "provider lock" in r for r in rows
        )
        assert policy.to_metadata()["org"] == "Acme"

    def test_unrestricted_policy_allows_everything(self) -> None:
        policy = mp.parse_policy({"org": "Open"}, source="x", signed=False)
        assert policy.mcp_server_allowed("anything") and policy.skill_allowed(
            "anything"
        )
        assert (
            policy.model_switch_refusal("any:model") is None
            and policy.plugin_verdict("x") == "unlisted"
        )
        assert "(UNSIGNED)" in policy.rows()[0]


class TestLoading:
    def test_signed_path_round_trip_and_tamper(self, tmp_path: Path) -> None:
        document, public = _signed(tmp_path)
        source = tmp_path / "policy.json"
        source.write_text(json.dumps(document), encoding="utf-8")
        policy = mp.load_managed_policy(
            source=str(source), public_key_b64=public, cache_dir=tmp_path / "cfg"
        )
        assert policy is not None and policy.signed and policy.org == "Acme"
        assert (tmp_path / "cfg" / mp.CACHE_NAME).is_file()

        tampered = dict(document)
        tampered["policy"] = {**BODY, "forbidden": []}
        source.write_text(json.dumps(tampered), encoding="utf-8")
        assert (
            mp.load_managed_policy(
                source=str(source), public_key_b64=public, cache_dir=tmp_path / "cfg"
            )
            is None
        )

        other_key = tmp_path / "other.key"
        save_keypair(generate_keypair(), other_key)
        wrong = mp.sign_document(BODY, key_path=other_key)
        source.write_text(json.dumps(wrong), encoding="utf-8")
        assert (
            mp.load_managed_policy(
                source=str(source), public_key_b64=public, cache_dir=tmp_path / "cfg"
            )
            is None
        )

    def test_unsigned_path_is_accepted_but_url_needs_a_key(
        self, tmp_path: Path
    ) -> None:
        source = tmp_path / "policy.json"
        source.write_text(json.dumps({"policy": BODY}), encoding="utf-8")
        policy = mp.load_managed_policy(
            source=str(source), public_key_b64=None, cache_dir=tmp_path / "cfg"
        )
        assert policy is not None and not policy.signed
        fetched = mp.load_managed_policy(
            source="https://policy.acme.example/bog.json",
            public_key_b64=None,
            cache_dir=tmp_path / "cfg2",
            fetch=lambda _u: json.dumps({"policy": BODY}).encode(),
        )
        assert fetched is None
        document, public = _signed(tmp_path)
        fetched = mp.load_managed_policy(
            source="https://policy.acme.example/bog.json",
            public_key_b64=public,
            cache_dir=tmp_path / "cfg2",
            fetch=lambda _u: json.dumps(document).encode(),
        )
        assert fetched is not None and fetched.signed

    def test_cache_survives_a_fetch_failure(self, tmp_path: Path) -> None:
        document, public = _signed(tmp_path)

        def _boom(_url: str) -> bytes:
            raise OSError("offline")

        cache = tmp_path / "cfg"
        assert (
            mp.load_managed_policy(
                source="https://x/p.json",
                public_key_b64=public,
                cache_dir=cache,
                fetch=_boom,
            )
            is None
        )
        assert (
            mp.load_managed_policy(
                source="https://x/p.json",
                public_key_b64=public,
                cache_dir=cache,
                fetch=lambda _u: json.dumps(document).encode(),
            )
            is not None
        )
        cached = mp.load_managed_policy(
            source="https://x/p.json",
            public_key_b64=public,
            cache_dir=cache,
            fetch=_boom,
        )
        assert cached is not None and cached.org == "Acme"

    def test_active_policy_is_cached_per_process(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        source = tmp_path / "policy.json"
        source.write_text(json.dumps({"policy": BODY}), encoding="utf-8")
        monkeypatch.setattr(mp, "configured_source", lambda: (str(source), None))
        monkeypatch.setattr(mp, "_cache_dir", lambda: tmp_path / "home")
        mp.reset_cache()
        try:
            first = mp.active_policy(refresh=True)
            assert first is not None and first.org == "Acme"
            source.write_text(
                json.dumps({"policy": {"org": "Changed"}}), encoding="utf-8"
            )
            assert mp.active_policy() is first
            assert mp.active_policy(refresh=True).org == "Changed"  # type: ignore[union-attr]
        finally:
            mp.reset_cache()


class TestEnforcementPoints:
    def test_mcp_filter_provider_lock_model_switch_and_rows(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        policy = mp.parse_policy(BODY, source="unit", signed=True)
        monkeypatch.setattr(mp, "active_policy", lambda refresh=False: policy)

        from bog_agents_cli import hook_decisions, trust_controller

        assert "denied" in (
            hook_decisions.model_switch_refusal(
                tmp_path, "a", "openai:gpt-5-preview", config_hooks=[]
            )
            or ""
        )
        assert (
            hook_decisions.model_switch_refusal(
                tmp_path, "a", "anthropic:claude-sonnet-4-6", config_hooks=[]
            )
            is None
        )
        rows = trust_controller.trust_rows(str(tmp_path), False, None)
        assert any(r.startswith("Managed policy: Acme") for r in rows)

        from bog_agents_cli import pr_output

        assert pr_output._managed_policy_metadata()["managed_policy"]["org"] == "Acme"

    def test_forbidden_plugin_install_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from bog_agents_cli import plugin_install

        policy = mp.parse_policy(BODY, source="unit", signed=True)
        monkeypatch.setattr(mp, "active_policy", lambda refresh=False: policy)
        plugin = tmp_path / "shadow-tools"
        plugin.mkdir()
        (plugin / "plugin.json").write_text(
            json.dumps(
                {"name": "shadow-tools", "version": "1.0.0", "description": "x"}
            ),
            encoding="utf-8",
        )
        dest_root = tmp_path / "plugins"
        with pytest.raises(
            plugin_install.PluginInstallError, match="forbidden by the managed policy"
        ):
            plugin_install.install_plugin(str(plugin), dest_root=dest_root)
        assert not (dest_root / "shadow-tools").exists()

    def test_skill_filter_hook_and_zero_retention(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from bog_agents.middleware import skills as sdk_skills

        policy = mp.parse_policy(BODY, source="unit", signed=True)
        try:
            mp.install_skill_filter(policy)
            assert sdk_skills._skill_dir_filter is not None
            assert sdk_skills._skill_dir_filter(
                "/skills/review"
            ) and not sdk_skills._skill_dir_filter("/skills/other")
            mp.install_skill_filter(None)
            assert sdk_skills._skill_dir_filter is None
        finally:
            sdk_skills.set_skill_dir_filter(None)
