"""Tests for the config manifest and the manifest-driven headless config command."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

from bog_agents_cli import config_manifest, model_config
from bog_agents_cli.config_manifest import (
    ConfigOption,
    OptionKind,
    get_config_options,
    get_option,
    option_keys,
    resolve_scalar,
)
from bog_agents_cli.headless_commands import _cmd_config


@pytest.fixture(autouse=True)
def _clear_manifest_caches() -> Iterator[None]:
    """Reset the manifest's `lru_cache`s around every test.

    The credential options are generated once from `PROVIDER_API_KEY_ENV` and
    cached; a test that monkeypatches that registry must not leak its mutated
    view into (or out of) the cache.
    """
    get_config_options.cache_clear()
    config_manifest._options_by_key.cache_clear()
    yield
    get_config_options.cache_clear()
    config_manifest._options_by_key.cache_clear()


@pytest.fixture
def _temp_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the manifest at a temp `config.toml` (absent until written)."""
    cfg = tmp_path / "config.toml"
    monkeypatch.setattr(model_config, "DEFAULT_CONFIG_PATH", cfg)
    return cfg


# --- __post_init__ coherence ------------------------------------------------


def test_every_declared_default_matches_its_kind() -> None:
    """Building the manifest exercises `__post_init__` on every option.

    A mistyped default would raise `TypeError` at construction, so the mere fact
    that `get_config_options()` returns proves each declared default is coherent
    with its kind. Assert the coherence explicitly for good measure.
    """
    for option in get_config_options():
        if option.default is None:
            continue
        expected = config_manifest._KIND_DEFAULT_TYPES[option.kind]
        assert isinstance(option.default, expected)
        if option.kind in {OptionKind.INT, OptionKind.FLOAT}:
            assert not isinstance(option.default, bool)


def test_mistyped_default_raises_at_construction() -> None:
    """An INT option defaulting to a str fails immediately (import-time guard)."""
    with pytest.raises(TypeError):
        ConfigOption(
            key="bad.int",
            group="Test",
            summary="",
            kind=OptionKind.INT,
            default="not-an-int",
        )


def test_bool_default_rejected_for_int_kind() -> None:
    """`bool` is an `int` subclass, but an INT default must not be a bool."""
    with pytest.raises(TypeError):
        ConfigOption(
            key="bad.boolint",
            group="Test",
            summary="",
            kind=OptionKind.INT,
            default=True,
        )


def test_mutable_default_rejected() -> None:
    """A mutable default is unsafe under the shared lru_cache and is rejected."""
    with pytest.raises(TypeError):
        ConfigOption(
            key="bad.mutable",
            group="Test",
            summary="",
            kind=OptionKind.STR,
            default=["a"],
        )


def test_fallback_env_vars_must_be_tuple_of_nonempty_strings() -> None:
    """An empty fallback name never matches an env var and is rejected."""
    with pytest.raises(TypeError):
        ConfigOption(
            key="bad.fallback",
            group="Test",
            summary="",
            kind=OptionKind.STR,
            fallback_env_vars=("",),
        )


# --- resolve_scalar precedence ----------------------------------------------


def test_resolve_precedence_env_beats_toml_beats_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Env wins over config.toml, which wins over the typed default."""
    option = ConfigOption(
        key="test.scalar",
        group="Test",
        summary="",
        kind=OptionKind.STR,
        default="from-default",
        env_var="BOG_AGENTS_TEST_SCALAR_XYZ",
        toml_keys=("test", "scalar"),
    )
    toml_data = {"test": {"scalar": "from-toml"}}

    monkeypatch.delenv("BOG_AGENTS_TEST_SCALAR_XYZ", raising=False)
    value, source = resolve_scalar(option, toml_data={})
    assert value == "from-default"
    assert source == "default"

    value, source = resolve_scalar(option, toml_data=toml_data)
    assert value == "from-toml"
    assert source == "config.toml"

    monkeypatch.setenv("BOG_AGENTS_TEST_SCALAR_XYZ", "from-env")
    value, source = resolve_scalar(option, toml_data=toml_data)
    assert value == "from-env"
    assert source == "env (BOG_AGENTS_TEST_SCALAR_XYZ)"


def test_resolve_empty_env_falls_through(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty env value counts as unset and falls through to the default."""
    option = ConfigOption(
        key="test.empty",
        group="Test",
        summary="",
        kind=OptionKind.STR,
        default="fallback",
        env_var="BOG_AGENTS_TEST_EMPTY_XYZ",
    )
    monkeypatch.setenv("BOG_AGENTS_TEST_EMPTY_XYZ", "")
    value, source = resolve_scalar(option, toml_data={})
    assert value == "fallback"
    assert source == "default"


def test_resolve_bool_coercion(monkeypatch: pytest.MonkeyPatch) -> None:
    """A recognized boolean token coerces; an unrecognized one falls through."""
    option = ConfigOption(
        key="test.flag",
        group="Test",
        summary="",
        kind=OptionKind.BOOL,
        default=False,
        env_var="BOG_AGENTS_TEST_FLAG_XYZ",
    )
    monkeypatch.setenv("BOG_AGENTS_TEST_FLAG_XYZ", "yes")
    assert resolve_scalar(option, toml_data={}) == (
        True,
        "env (BOG_AGENTS_TEST_FLAG_XYZ)",
    )

    monkeypatch.setenv("BOG_AGENTS_TEST_FLAG_XYZ", "maybe")
    assert resolve_scalar(option, toml_data={}) == (False, "default")


def test_resolve_int_from_toml() -> None:
    """A well-typed TOML int resolves; a wrong-typed one falls back."""
    option = ConfigOption(
        key="test.count",
        group="Test",
        summary="",
        kind=OptionKind.INT,
        default=1,
        toml_keys=("test", "count"),
    )
    assert resolve_scalar(option, toml_data={"test": {"count": 7}}) == (
        7,
        "config.toml",
    )
    # A string in the TOML slot is the wrong shape -> default.
    assert resolve_scalar(option, toml_data={"test": {"count": "x"}}) == (1, "default")


# --- Credentials derived from PROVIDER_API_KEY_ENV --------------------------


def test_credentials_present_and_redacted() -> None:
    """Every provider in the registry yields a redacted credential option."""
    unique_env_vars = set(model_config.PROVIDER_API_KEY_ENV.values())
    credentials = [o for o in get_config_options() if o.group == "Credentials"]
    assert len(credentials) == len(unique_env_vars)
    assert {c.env_var for c in credentials} == unique_env_vars
    assert all(c.redacted for c in credentials)
    assert all(c.provider is not None for c in credentials)


def test_new_provider_flows_into_manifest(monkeypatch: pytest.MonkeyPatch) -> None:
    """Adding a provider to PROVIDER_API_KEY_ENV surfaces it as a credential.

    Proves the credential surface is *derived* from the registry (P0-G) rather
    than hand-listed.
    """
    monkeypatch.setitem(model_config.PROVIDER_API_KEY_ENV, "acme", "ACME_API_KEY")
    get_config_options.cache_clear()
    config_manifest._options_by_key.cache_clear()

    option = get_option("credentials.acme")
    assert option is not None
    assert option.env_var == "ACME_API_KEY"
    assert option.redacted is True
    assert option.provider == "acme"


# --- Headless config command ------------------------------------------------


def test_config_show_lists_options(_temp_config: Path) -> None:
    """`config show` lists every manifest option with type and source."""
    result = _cmd_config("show")
    assert result.ok
    assert result.data is not None
    keys_in_output = {row["key"] for row in result.data["options"]}
    assert set(option_keys()) == keys_in_output
    # Grouped, human-readable text form.
    assert "[Models]" in result.text
    assert "models.default" in result.text


def test_config_show_redacts_secret_values(
    monkeypatch: pytest.MonkeyPatch, _temp_config: Path
) -> None:
    """A set credential is reported as set but its raw value never surfaces."""
    secret = "sk-super-secret-value-abc123"
    monkeypatch.setenv("ANTHROPIC_API_KEY", secret)

    result = _cmd_config("show")
    assert result.ok
    assert result.data is not None

    # Neither the human text nor the serialized JSON payload leaks the secret.
    assert secret not in result.text
    assert secret not in json.dumps(result.data)

    row = next(r for r in result.data["options"] if r["key"] == "credentials.anthropic")
    assert row["set"] is True
    assert row["redacted"] is True
    assert row["value"] != secret


def test_config_get_returns_value_and_source(
    monkeypatch: pytest.MonkeyPatch, _temp_config: Path
) -> None:
    """`config get <key>` resolves a single option's value and source."""
    monkeypatch.setenv("BOG_AGENTS_OFFLINE", "1")
    result = _cmd_config("get runtime.offline")
    assert result.ok
    assert result.data is not None
    assert result.data["option"]["value"] == "True"
    assert result.data["option"]["source"] == "env (BOG_AGENTS_OFFLINE)"
    assert "runtime.offline" in result.text


def test_config_get_redacts_credential(
    monkeypatch: pytest.MonkeyPatch, _temp_config: Path
) -> None:
    """`config get` on a credential never returns the raw secret."""
    secret = "sk-do-not-print-me-999"
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    result = _cmd_config("get credentials.openai")
    assert result.ok
    assert result.data is not None
    assert secret not in result.text
    assert secret not in json.dumps(result.data)
    assert result.data["option"]["set"] is True


def test_config_get_unknown_key_errors(_temp_config: Path) -> None:
    """`config get` on an unknown key reports failure without raising."""
    result = _cmd_config("get nope.not.real")
    assert not result.ok
    assert result.data is not None
    assert result.data["error"] == "unknown_key"


def test_config_default_verb_is_show(_temp_config: Path) -> None:
    """Bare `config` behaves like `config show`."""
    bare = _cmd_config("")
    show = _cmd_config("show")
    assert bare.data is not None
    assert show.data is not None
    assert {r["key"] for r in bare.data["options"]} == {
        r["key"] for r in show.data["options"]
    }
