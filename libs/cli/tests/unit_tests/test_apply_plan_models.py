"""Tests for the apply-model and plan-model config slots."""

from __future__ import annotations

from pathlib import Path

import tomli_w

from bog_agents_cli.model_config import (
    ModelConfig,
    clear_caches,
    get_apply_model,
    get_plan_model,
    save_apply_model,
    save_plan_model,
)


def test_default_apply_and_plan_are_none(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    clear_caches()
    config = ModelConfig.load(cfg)
    assert config.apply_model is None
    assert config.plan_model is None


def test_save_apply_then_load(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    clear_caches()
    assert save_apply_model("anthropic:claude-haiku-4-5", cfg)
    assert get_apply_model(cfg) == "anthropic:claude-haiku-4-5"


def test_save_plan_then_load(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    clear_caches()
    assert save_plan_model("openai:gpt-5", cfg)
    assert get_plan_model(cfg) == "openai:gpt-5"


def test_clear_apply_via_empty_string(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    clear_caches()
    save_apply_model("anthropic:claude-haiku-4-5", cfg)
    assert save_apply_model("", cfg)
    assert get_apply_model(cfg) is None


def test_save_apply_preserves_other_models_section_keys(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    with cfg.open("wb") as f:
        tomli_w.dump({"models": {"default": "anthropic:claude-sonnet-4-6"}}, f)
    clear_caches()
    save_apply_model("anthropic:claude-haiku-4-5", cfg)
    config = ModelConfig.load(cfg)
    assert config.default_model == "anthropic:claude-sonnet-4-6"
    assert config.apply_model == "anthropic:claude-haiku-4-5"


def test_apply_and_plan_independent(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    clear_caches()
    save_apply_model("a:apply", cfg)
    save_plan_model("p:plan", cfg)
    config = ModelConfig.load(cfg)
    assert config.apply_model == "a:apply"
    assert config.plan_model == "p:plan"
