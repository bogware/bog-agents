"""Tests for bog_agents_cli.prompts — custom prompt overrides."""

from __future__ import annotations

from pathlib import Path

from bog_agents_cli.prompts import get_prompt, load_custom_prompts, save_custom_prompt


class TestLoadCustomPrompts:
    def test_returns_empty_when_no_file(self, tmp_path: Path) -> None:
        assert load_custom_prompts(tmp_path / "nonexistent.toml") == {}

    def test_loads_prompts_section(self, tmp_path: Path) -> None:
        cfg = tmp_path / "config.toml"
        cfg.write_text('[prompts]\ninit = "custom init prompt"\n')
        result = load_custom_prompts(cfg)
        assert result == {"init": "custom init prompt"}

    def test_ignores_non_string_values(self, tmp_path: Path) -> None:
        cfg = tmp_path / "config.toml"
        cfg.write_text('[prompts]\ngood = "yes"\nbad = 42\n')
        result = load_custom_prompts(cfg)
        assert result == {"good": "yes"}

    def test_returns_empty_when_prompts_not_a_table(self, tmp_path: Path) -> None:
        cfg = tmp_path / "config.toml"
        cfg.write_text('prompts = "not a table"\n')
        assert load_custom_prompts(cfg) == {}

    def test_returns_empty_on_invalid_toml(self, tmp_path: Path) -> None:
        cfg = tmp_path / "config.toml"
        cfg.write_bytes(b"[invalid\n")
        assert load_custom_prompts(cfg) == {}

    def test_returns_empty_when_no_prompts_section(self, tmp_path: Path) -> None:
        cfg = tmp_path / "config.toml"
        cfg.write_text('[other]\nkey = "value"\n')
        assert load_custom_prompts(cfg) == {}

    def test_multiple_prompts(self, tmp_path: Path) -> None:
        cfg = tmp_path / "config.toml"
        cfg.write_text('[prompts]\ninit = "a"\nonboard = "b"\ncustom = "c"\n')
        result = load_custom_prompts(cfg)
        assert result == {"init": "a", "onboard": "b", "custom": "c"}


class TestGetPrompt:
    def test_returns_default_when_no_override(self) -> None:
        # get_prompt reads from ~/.bog-agents/config.toml by default;
        # without a custom config, it should return the default.
        result = get_prompt("nonexistent_command_xyz", "fallback")
        assert result == "fallback"

    def test_returns_default_for_missing_command(self) -> None:
        assert get_prompt("surely_not_configured", "default text") == "default text"


class TestSaveCustomPrompt:
    def test_creates_file_and_saves(self, tmp_path: Path) -> None:
        cfg = tmp_path / "sub" / "config.toml"
        save_custom_prompt("init", "my custom prompt", config_path=cfg)
        assert cfg.exists()
        result = load_custom_prompts(cfg)
        assert result["init"] == "my custom prompt"

    def test_preserves_existing_prompts(self, tmp_path: Path) -> None:
        cfg = tmp_path / "config.toml"
        save_custom_prompt("init", "first", config_path=cfg)
        save_custom_prompt("onboard", "second", config_path=cfg)
        result = load_custom_prompts(cfg)
        assert result == {"init": "first", "onboard": "second"}

    def test_overwrites_existing_prompt(self, tmp_path: Path) -> None:
        cfg = tmp_path / "config.toml"
        save_custom_prompt("init", "old", config_path=cfg)
        save_custom_prompt("init", "new", config_path=cfg)
        result = load_custom_prompts(cfg)
        assert result["init"] == "new"

    def test_preserves_other_config_sections(self, tmp_path: Path) -> None:
        cfg = tmp_path / "config.toml"
        cfg.write_text('[settings]\nmodel = "gpt-4"\n')
        save_custom_prompt("init", "custom", config_path=cfg)
        # Verify prompts saved
        result = load_custom_prompts(cfg)
        assert result["init"] == "custom"
        # Verify other section preserved
        import tomllib

        with cfg.open("rb") as f:
            data = tomllib.load(f)
        assert data["settings"]["model"] == "gpt-4"
