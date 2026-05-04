"""Tests for the Recipes-as-Pipelines DSL and registry."""

from __future__ import annotations

from pathlib import Path

import pytest

from bog_agents_cli.recipes import (
    CATALOG,
    get_recipe,
    install_recipe,
    is_installed,
    list_recipes,
    uninstall_recipe,
)


def test_catalog_non_empty() -> None:
    assert len(CATALOG) >= 5


def test_each_recipe_has_yaml_and_id() -> None:
    for r in CATALOG:
        assert r.id
        assert r.yaml.strip()
        assert r.title


def test_get_case_insensitive() -> None:
    r = get_recipe("CODE-REVIEW")
    assert r is not None
    assert r.id == "code-review"


def test_get_unknown_returns_none() -> None:
    assert get_recipe("nope") is None


def test_install_writes_yaml(tmp_path: Path) -> None:
    target = install_recipe("code-review", pipelines_dir=tmp_path)
    assert target.exists()
    content = target.read_text(encoding="utf-8")
    assert "name: code-review" in content


def test_install_refuses_overwrite_by_default(tmp_path: Path) -> None:
    install_recipe("code-review", pipelines_dir=tmp_path)
    with pytest.raises(FileExistsError):
        install_recipe("code-review", pipelines_dir=tmp_path)


def test_install_overwrite_succeeds(tmp_path: Path) -> None:
    install_recipe("code-review", pipelines_dir=tmp_path)
    target = install_recipe("code-review", pipelines_dir=tmp_path, overwrite=True)
    assert target.exists()


def test_install_unknown_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="No recipe"):
        install_recipe("does-not-exist", pipelines_dir=tmp_path)


def test_uninstall_removes_file(tmp_path: Path) -> None:
    install_recipe("code-review", pipelines_dir=tmp_path)
    assert uninstall_recipe("code-review", pipelines_dir=tmp_path) is True
    assert not is_installed("code-review", pipelines_dir=tmp_path)


def test_uninstall_returns_false_when_absent(tmp_path: Path) -> None:
    assert uninstall_recipe("code-review", pipelines_dir=tmp_path) is False


def test_list_recipes_filter_by_tag() -> None:
    quality = list_recipes(tag="quality")
    assert any(r.id == "code-review" for r in quality)
    none_match = list_recipes(tag="zzzzzz")
    assert none_match == []


def test_recipe_command_registered() -> None:
    from bog_agents_cli.commands import COMMAND_HANDLER_MAP

    assert "/recipe" in COMMAND_HANDLER_MAP
    assert COMMAND_HANDLER_MAP["/recipe"] == "_handle_recipe_command"


def test_recipe_yaml_loads_via_existing_pipeline_loader(tmp_path: Path) -> None:
    """Recipes must survive the canonical pipeline.load_pipeline parser."""
    from bog_agents_cli.pipeline import load_pipeline

    target = install_recipe("code-review", pipelines_dir=tmp_path)
    pipeline = load_pipeline(target)
    assert pipeline.name == "code-review"
    assert pipeline.steps  # at least one step
