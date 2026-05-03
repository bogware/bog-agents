"""Tests for the curated MCP catalog and featured set."""

from __future__ import annotations

from bog_agents_cli.mcp_registry import (
    _REGISTRY,
    FEATURED_IDS,
    get_entry,
    list_entries,
)


def test_featured_ids_are_all_in_registry() -> None:
    """Every id surfaced by ``/mcp featured`` must resolve in the registry."""
    missing = [sid for sid in FEATURED_IDS if get_entry(sid) is None]
    assert not missing, f"FEATURED_IDS reference unknown servers: {missing}"


def test_user_requested_servers_present() -> None:
    """User asked for: jira, github, terraform, azure-devops, aws, postgres."""
    required = {"jira", "github", "terraform", "azure-devops", "aws", "postgres"}
    have = set(_REGISTRY.keys())
    assert required.issubset(have), f"Missing curated servers: {required - have}"


def test_aws_entry_uses_local_credentials_chain() -> None:
    """The AWS entry must NOT require an API key — it uses ~/.aws/credentials."""
    aws = get_entry("aws")
    assert aws is not None
    assert aws.required_env == []
    # Profile and region should be advertised as optional.
    assert "AWS_PROFILE" in aws.optional_env
    assert "AWS_REGION" in aws.optional_env


def test_datadog_entry_requires_both_keys() -> None:
    """Datadog needs API key + Application key; site is optional."""
    dd = get_entry("datadog")
    assert dd is not None
    assert "DD_API_KEY" in dd.required_env
    assert "DD_APP_KEY" in dd.required_env
    assert "DD_SITE" in dd.optional_env


def test_kubernetes_entry_uses_kubeconfig() -> None:
    """The K8s entry should default to local kubeconfig (no required env)."""
    k8s = get_entry("kubernetes")
    assert k8s is not None
    assert k8s.required_env == []
    assert "KUBECONFIG" in k8s.optional_env


def test_featured_order_starts_with_github() -> None:
    """Curated order leads with the dev-team workhorses."""
    assert FEATURED_IDS[0] == "github"
    assert "jira" in FEATURED_IDS[:5]


def test_list_entries_includes_new_curated_entries() -> None:
    ids = {e.id for e in list_entries()}
    for required in ("aws", "datadog", "kubernetes"):
        assert required in ids


def test_aws_install_notes_warn_about_credentials() -> None:
    aws = get_entry("aws")
    assert aws is not None
    assert "aws configure" in aws.install_notes.lower() or "credentials" in aws.install_notes.lower()
