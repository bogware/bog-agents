"""Tests for the curated Standing Orders catalog."""

from __future__ import annotations

from bog_agents_cli.standing_orders import CATALOG, get_order, list_orders


def test_catalog_non_empty() -> None:
    assert len(CATALOG) >= 5


def test_each_order_has_required_fields() -> None:
    for order in CATALOG:
        assert order.id
        assert order.title
        assert order.summary
        assert order.tags  # at least one tag
        assert isinstance(order.job, dict)
        assert "name" in order.job
        assert "prompt" in order.job
        assert "triggers" in order.job


def test_each_order_id_unique() -> None:
    ids = [o.id for o in CATALOG]
    assert len(ids) == len(set(ids))


def test_get_order_case_insensitive() -> None:
    order = get_order("BUG-FINDER")
    assert order is not None
    assert order.id == "bug-finder"


def test_get_unknown_returns_none() -> None:
    assert get_order("does-not-exist") is None


def test_list_orders_filter_by_tag() -> None:
    quality = list_orders(tag="quality")
    assert any(o.id == "bug-finder" for o in quality)
    none_match = list_orders(tag="zzznosuchTag")
    assert none_match == []


def test_to_create_payload_is_independent_copy() -> None:
    """Mutating the returned payload must NOT corrupt the catalog singleton."""
    order = get_order("bug-finder")
    assert order is not None
    payload = order.to_create_payload()
    payload["prompt"] = "TAINTED"
    payload["triggers"][0]["git_branch_pattern"] = "main"
    fresh = order.to_create_payload()
    assert "TAINTED" not in fresh["prompt"]
    assert fresh["triggers"][0]["git_branch_pattern"] == "*"


def test_command_registered() -> None:
    from bog_agents_cli.commands import COMMAND_HANDLER_MAP

    assert "/standing-orders" in COMMAND_HANDLER_MAP
    assert COMMAND_HANDLER_MAP["/standing-orders"] == "_handle_standing_orders_command"


def test_bug_finder_present() -> None:
    """The Bug Finder template (#16) ships in the catalog."""
    bf = get_order("bug-finder")
    assert bf is not None
    assert bf.job["triggers"][0]["type"] == "git_push"
