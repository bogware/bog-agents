"""Tests for the theme registry, user themes, and the `/theme` controller.

The load-bearing guarantee is that removing the `app.tcss` palette-override
block leaves the default look byte-identical: the registered `bog` theme must
resolve every variable the old block hard-coded to the exact same hex, and
`bog` must be the default. `test_bog_theme_is_byte_identical_to_old_tcss`
proves this by resolving the theme through a real (minimal) Textual app.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from bog_agents_cli import theme
from bog_agents_cli.config import load_selected_theme, save_selected_theme
from bog_agents_cli.theme import (
    DEFAULT_THEME_NAME,
    ThemeSpec,
    all_theme_specs,
    builtin_theme_specs,
    handle_theme_command,
    load_user_theme_specs,
    resolve_theme_name,
)

# The exact values the removed app.tcss override block hard-coded. The default
# theme MUST resolve every one of these to the identical hex or the default
# look changes.
OLD_TCSS_VARS = {
    "primary": "#7aa888",
    "primary-lighten-1": "#9cc4a7",
    "primary-lighten-2": "#c1d8c8",
    "primary-darken-1": "#557a63",
    "primary-darken-2": "#3a5a48",
    "highlight": "#1f3328",
    "highlight-soft": "#16271d",
    "secondary": "#6a9b9b",
    "background": "#060a07",
    "surface": "#0d1410",
    "surface-lighten-1": "#16201a",
    "surface-darken-1": "#040705",
    "boundary": "#243828",
    "text": "#c8d4ca",
    "text-muted": "#6f8478",
    "success": "#7aa888",
    "warning": "#b89968",
    "error": "#b86a78",
}


# ---------------------------------------------------------------------------
# Default-look preservation
# ---------------------------------------------------------------------------


def test_bog_is_the_default_theme() -> None:
    assert DEFAULT_THEME_NAME == "bog"
    assert builtin_theme_specs()[0].name == "bog"


def test_bog_spec_carries_old_tcss_hex_values() -> None:
    """The `bog` spec's base fields + variables equal the old tcss hex.

    A pure-data check (no Textual) so the guarantee is asserted even where a
    TUI cannot be mounted.
    """
    bog = theme.BOG_SPEC
    # Base fields.
    assert bog.primary == OLD_TCSS_VARS["primary"]
    assert bog.secondary == OLD_TCSS_VARS["secondary"]
    assert bog.background == OLD_TCSS_VARS["background"]
    assert bog.surface == OLD_TCSS_VARS["surface"]
    assert bog.success == OLD_TCSS_VARS["success"]
    assert bog.error == OLD_TCSS_VARS["error"]
    # Pinned variables (derived / app-specific / drift-prone).
    for key in (
        "primary-lighten-1",
        "primary-lighten-2",
        "primary-darken-1",
        "primary-darken-2",
        "surface-lighten-1",
        "surface-darken-1",
        "highlight",
        "highlight-soft",
        "boundary",
        "text",
        "text-muted",
        "warning",
    ):
        assert bog.variables[key] == OLD_TCSS_VARS[key], key


async def test_bog_theme_is_byte_identical_to_old_tcss() -> None:
    """Resolve `bog` through a real Textual app and compare every old var.

    This is the authoritative proof: it exercises Textual's own variable
    generation + `variables` merge, catching any derivation/rounding drift
    (e.g. warning #b89968 -> #b79968) that a pure-data check would miss.
    """
    from textual.app import App

    bog_theme = theme.BOG_SPEC.to_theme()

    class _App(App):
        def on_mount(self) -> None:
            self.register_theme(bog_theme)
            self.theme = "bog"

    app = _App()
    async with app.run_test():
        resolved = {k: str(v).lower() for k, v in app.get_css_variables().items()}

    mismatches = {
        key: (expected, resolved.get(key))
        for key, expected in OLD_TCSS_VARS.items()
        if resolved.get(key) != expected.lower()
    }
    assert not mismatches, f"theme drift vs old tcss: {mismatches}"


async def test_accent_matches_prior_default_for_referenced_widgets() -> None:
    """`$accent` (used by two widget borders) stays its prior resolved value."""
    from textual.app import App

    bog_theme = theme.BOG_SPEC.to_theme()

    class _App(App):
        def on_mount(self) -> None:
            self.register_theme(bog_theme)
            self.theme = "bog"

    app = _App()
    async with app.run_test():
        accent = str(app.get_css_variables()["accent"]).lower()
    assert accent == "#fea62b"


# ---------------------------------------------------------------------------
# Registry / built-ins
# ---------------------------------------------------------------------------


def test_builtin_themes_present_and_valid() -> None:
    names = {spec.name for spec in builtin_theme_specs()}
    assert {"bog", "bog-light", "abyss", "ember"} <= names


def test_every_theme_supplies_required_custom_vars() -> None:
    """Every theme must carry highlight/highlight-soft/boundary (not derived)."""
    for spec in builtin_theme_specs():
        for key in ("highlight", "highlight-soft", "boundary"):
            assert key in spec.variables, f"{spec.name} missing {key}"


def test_all_themes_build_a_textual_theme() -> None:
    for spec in builtin_theme_specs():
        built = spec.to_theme()
        assert built.name == spec.name
        assert built.dark == spec.dark


def test_themespec_rejects_bad_hex() -> None:
    with pytest.raises(ValueError, match="hex color"):
        ThemeSpec(
            name="bad",
            label="Bad",
            dark=True,
            primary="not-a-color",
            secondary="#6a9b9b",
            background="#060a07",
            surface="#0d1410",
            foreground="#c8d4ca",
            success="#7aa888",
            warning="#b89968",
            error="#b86a78",
            accent="#ffa62b",
            variables={
                "highlight": "#1f3328",
                "highlight-soft": "#16271d",
                "boundary": "#243828",
            },
        )


def test_themespec_requires_custom_vars() -> None:
    with pytest.raises(ValueError, match="missing required variables"):
        ThemeSpec(
            name="incomplete",
            label="Incomplete",
            dark=True,
            primary="#7aa888",
            secondary="#6a9b9b",
            background="#060a07",
            surface="#0d1410",
            foreground="#c8d4ca",
            success="#7aa888",
            warning="#b89968",
            error="#b86a78",
            accent="#ffa62b",
            variables={"highlight": "#1f3328"},
        )


# ---------------------------------------------------------------------------
# User themes
# ---------------------------------------------------------------------------


def test_load_user_theme_from_config() -> None:
    config = {
        "themes": {
            "solar": {
                "label": "My Solarized",
                "dark": True,
                "primary": "#268bd2",
                "warning": "#b58900",
            }
        }
    }
    specs = load_user_theme_specs(config)
    assert len(specs) == 1
    solar = specs[0]
    assert solar.name == "solar"
    assert solar.label == "My Solarized"
    assert solar.dark is True
    assert solar.primary == "#268bd2"
    assert solar.warning == "#b58900"
    # Missing base colors fall back to the bog (dark) palette.
    assert solar.background == theme.BOG_SPEC.background
    # Required custom vars fall back to the base so CSS always resolves.
    for key in ("highlight", "highlight-soft", "boundary"):
        assert key in solar.variables


def test_user_theme_appears_in_registry_after_builtins() -> None:
    config = {"themes": {"solar": {"label": "Solar", "dark": True}}}
    specs = all_theme_specs(config)
    names = [s.name for s in specs]
    assert names[: len(builtin_theme_specs())] == [
        s.name for s in builtin_theme_specs()
    ]
    assert "solar" in names


def test_user_theme_missing_label_skipped() -> None:
    config = {"themes": {"nolabel": {"dark": True, "primary": "#268bd2"}}}
    assert load_user_theme_specs(config) == []


def test_user_theme_bad_hex_falls_back() -> None:
    config = {
        "themes": {"sloppy": {"label": "Sloppy", "primary": "purple", "dark": False}}
    }
    specs = load_user_theme_specs(config)
    assert len(specs) == 1
    # Invalid primary falls back to the light base palette rather than crashing.
    assert specs[0].primary == theme.BOG_LIGHT_SPEC.primary


def test_user_theme_cannot_shadow_builtin() -> None:
    config = {"themes": {"bog": {"label": "Fake Bog", "primary": "#ff0000"}}}
    assert load_user_theme_specs(config) == []


# ---------------------------------------------------------------------------
# /theme command controller
# ---------------------------------------------------------------------------


def test_theme_command_switch_known() -> None:
    result = handle_theme_command("abyss")
    assert result.apply == "abyss"
    assert not result.is_error
    assert "abyss" in result.message.lower()


def test_theme_command_switch_is_case_insensitive() -> None:
    result = handle_theme_command("ABYSS")
    assert result.apply == "abyss"


def test_theme_command_unknown_errors_gracefully() -> None:
    result = handle_theme_command("does-not-exist")
    assert result.apply is None
    assert result.is_error
    assert "unknown" in result.message.lower()


def test_theme_command_list() -> None:
    result = handle_theme_command("list", current="bog")
    assert result.apply is None
    assert not result.is_error
    assert "bog" in result.message
    assert "abyss" in result.message
    # Active theme is marked.
    assert "* bog" in result.message


def test_theme_command_empty_lists() -> None:
    result = handle_theme_command("", current="bog")
    assert result.apply is None
    assert "Available themes" in result.message


def test_resolve_theme_name() -> None:
    assert resolve_theme_name("bog") == "bog"
    assert resolve_theme_name("EMBER") == "ember"
    assert resolve_theme_name("nope") is None


# ---------------------------------------------------------------------------
# Persistence round-trip
# ---------------------------------------------------------------------------


def test_theme_persistence_round_trip(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    assert load_selected_theme(config_path) is None

    assert save_selected_theme("ember", config_path) is True
    assert load_selected_theme(config_path) == "ember"

    # Overwriting keeps it a round-trip and preserves other keys.
    config_path.write_text(
        textwrap.dedent(
            """
            [models]
            default = "anthropic:claude-sonnet-4-5"

            [ui]
            theme = "abyss"
            """
        ),
        encoding="utf-8",
    )
    assert load_selected_theme(config_path) == "abyss"
    assert save_selected_theme("bog-light", config_path) is True
    assert load_selected_theme(config_path) == "bog-light"
    # The unrelated [models] key survives the read-modify-write.
    import tomllib

    with config_path.open("rb") as f:
        data = tomllib.load(f)
    assert data["models"]["default"] == "anthropic:claude-sonnet-4-5"
    assert data["ui"]["theme"] == "bog-light"


def test_load_selected_theme_missing_file(tmp_path: Path) -> None:
    assert load_selected_theme(tmp_path / "nope.toml") is None


def test_load_selected_theme_ignores_blank(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text('[ui]\ntheme = "   "\n', encoding="utf-8")
    assert load_selected_theme(config_path) is None


# ---------------------------------------------------------------------------
# Theme picker modal
# ---------------------------------------------------------------------------


async def test_theme_selector_navigates_and_returns_choice() -> None:
    """The picker mounts (its CSS resolves), navigates, and returns a name.

    Exercises the widget through a real host app so its `$highlight` /
    `$primary-lighten-1` CSS references resolve from the registered themes.
    """
    from textual.app import App

    from bog_agents_cli.theme import all_theme_specs, register_all_themes
    from bog_agents_cli.widgets.theme_selector import ThemeSelectorScreen

    names = [spec.name for spec in all_theme_specs()]
    chosen: list[str | None] = []

    class _App(App):
        def on_mount(self) -> None:
            register_all_themes(self)
            self.theme = "bog"

    app = _App()
    async with app.run_test() as pilot:
        app.push_screen(ThemeSelectorScreen(current="bog"), chosen.append)
        await pilot.pause()
        await pilot.press("down")
        await pilot.press("enter")
        await pilot.pause()

    assert chosen == [names[1]]


async def test_theme_selector_cancel_restores_original() -> None:
    """Esc returns `None` and restores the theme active on open."""
    from textual.app import App

    from bog_agents_cli.theme import register_all_themes
    from bog_agents_cli.widgets.theme_selector import ThemeSelectorScreen

    result: list[str | None] = []

    class _App(App):
        def on_mount(self) -> None:
            register_all_themes(self)
            self.theme = "bog"

    app = _App()
    async with app.run_test() as pilot:
        app.push_screen(ThemeSelectorScreen(current="bog"), result.append)
        await pilot.pause()
        await pilot.press("down")  # live-previews a different theme
        await pilot.press("escape")
        await pilot.pause()
        assert app.theme == "bog"

    assert result == [None]
