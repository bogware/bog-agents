"""Drift-detection tests for the `BOG_AGENTS_*` environment-variable registry.

These tests guard the *future*: they ensure that any new `BOG_AGENTS_*`
variable introduced anywhere in the CLI package is also registered as a
named constant in `bog_agents_cli._env_vars`. They intentionally do NOT
forbid bare string literals at existing call sites — migrating those to import
the constants is a separate, incremental cleanup.

Also asserted here: the registry has no duplicate values, constant names are
all-uppercase, and the shared boolean helpers honour the documented
truthy/falsy set.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import bog_agents_cli._env_vars as _mod
from bog_agents_cli._env_vars import classify_env_bool, env_bool, is_env_truthy

_SRC_DIR = Path(__file__).resolve().parents[2] / "bog_agents_cli"
_REGISTRY_FILE = _SRC_DIR / "_env_vars.py"

# Matches a full BOG_AGENTS_* env var name inside quote characters. The [A-Z]
# after the prefix avoids matching a bare prefix constant if one is ever added.
_ENV_VAR_RE = re.compile(r"""["'](BOG_AGENTS_[A-Z][A-Z0-9_]+)["']""")


def _public_constants() -> list[str]:
    """Return public string-constant names from `_env_vars` in definition order."""
    return [
        k
        for k, v in vars(_mod).items()
        if isinstance(v, str) and not k.startswith("_") and v.startswith("BOG_AGENTS_")
    ]


def _registered_values() -> set[str]:
    """Collect every `BOG_AGENTS_*` string value exported by `_env_vars`."""
    return {getattr(_mod, k) for k in _public_constants()}


def _collect_package_literals() -> dict[str, set[str]]:
    """Map source files to the bare `BOG_AGENTS_*` literals they contain.

    Scans every `.py` file in the package except the registry itself, so the
    result is exactly the set of literals that must be covered by the registry.

    Returns:
        `{relative_path: {var_name, ...}}` for files with at least one hit.
    """
    hits: dict[str, set[str]] = {}
    for py_file in _SRC_DIR.rglob("*.py"):
        if py_file == _REGISTRY_FILE:
            continue
        matches = set(_ENV_VAR_RE.findall(py_file.read_text(encoding="utf-8")))
        if matches:
            hits[str(py_file.relative_to(_SRC_DIR))] = matches
    return hits


class TestEnvVarRegistryCompleteness:
    """Every `BOG_AGENTS_*` literal used in the package must be registered."""

    def test_every_literal_is_registered(self) -> None:
        """A new unregistered variable fails this test until added to _env_vars."""
        registered = _registered_values()
        used: set[str] = set()
        for names in _collect_package_literals().values():
            used |= names
        unregistered = used - registered
        assert not unregistered, (
            "BOG_AGENTS_* variables used in source but missing from "
            "bog_agents_cli/_env_vars.py: "
            f"{sorted(unregistered)}. Add each as a named constant."
        )

    def test_registry_covers_current_surface(self) -> None:
        """Sanity floor: the current known surface stays registered."""
        registered = _registered_values()
        # A representative, load-bearing subset; guards against accidental
        # deletion of core entries during future edits.
        expected_subset = {
            "BOG_AGENTS_OFFLINE",
            "BOG_AGENTS_DEBUG",
            "BOG_AGENTS_HOME",
            "BOG_AGENTS_MODEL",
            "BOG_AGENTS_DREAMSCAPE",
            "BOG_AGENTS_OPERATOR",
            "BOG_AGENTS_TRACEFILE_KEY",
            "BOG_AGENTS_SHELL_ALLOW_LIST",
        }
        missing = expected_subset - registered
        assert not missing, f"Core registry entries went missing: {sorted(missing)}"


class TestRegistryHygiene:
    """Structural invariants of the registry module."""

    def test_no_duplicate_values(self) -> None:
        """Each variable name maps to exactly one constant."""
        names = _public_constants()
        values = [getattr(_mod, n) for n in names]
        dupes = {v for v in values if values.count(v) > 1}
        assert not dupes, f"Duplicate BOG_AGENTS_* values in registry: {sorted(dupes)}"

    def test_constant_names_are_upper_snake(self) -> None:
        """Public constant names must be all-uppercase snake case."""
        offenders = [
            n for n in _public_constants() if not re.fullmatch(r"[A-Z][A-Z0-9_]*", n)
        ]
        assert not offenders, f"Non-uppercase constant names: {offenders}"

    def test_constants_sorted(self) -> None:
        """Public constant names are kept alphabetically sorted."""
        names = _public_constants()
        assert names == sorted(names), (
            "Constants in _env_vars.py are not sorted. Expected order:\n"
            + ", ".join(sorted(names))
        )

    def test_values_carry_prefix_and_match_name(self) -> None:
        """Every value is prefixed and mirrors its constant name."""
        for name in _public_constants():
            value = getattr(_mod, name)
            assert value == f"BOG_AGENTS_{name}", (name, value)


class TestIsEnvTruthy:
    """Parsing of on/off boolean env vars via `is_env_truthy` / `env_bool`."""

    def test_unset_returns_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Missing env var falls back to *default*."""
        monkeypatch.delenv("BOG_AGENTS_DEBUG", raising=False)
        assert is_env_truthy("BOG_AGENTS_DEBUG") is False
        assert is_env_truthy("BOG_AGENTS_DEBUG", default=True) is True
        assert env_bool("BOG_AGENTS_DEBUG") is False
        assert env_bool("BOG_AGENTS_DEBUG", True) is True

    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "On", "  true  "])
    def test_truthy_values(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        """Recognized truthy values enable the flag regardless of case/whitespace."""
        monkeypatch.setenv("BOG_AGENTS_DEBUG", value)
        assert is_env_truthy("BOG_AGENTS_DEBUG") is True
        assert env_bool("BOG_AGENTS_DEBUG") is True

    @pytest.mark.parametrize("value", ["0", "false", "FALSE", "no", "off", ""])
    def test_falsy_values(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        """Recognized falsy values disable the flag even when `default=True`.

        This is the key behaviour vs. `bool(os.environ.get(...))`, which treats
        `"0"` and `"false"` as truthy because they are non-empty strings.
        """
        monkeypatch.setenv("BOG_AGENTS_DEBUG", value)
        assert is_env_truthy("BOG_AGENTS_DEBUG") is False
        assert is_env_truthy("BOG_AGENTS_DEBUG", default=True) is False
        assert env_bool("BOG_AGENTS_DEBUG", True) is False

    def test_unrecognized_value_returns_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Values outside the truthy/falsy sets fall back to *default*."""
        monkeypatch.setenv("BOG_AGENTS_DEBUG", "maybe")
        assert is_env_truthy("BOG_AGENTS_DEBUG") is False
        assert is_env_truthy("BOG_AGENTS_DEBUG", default=True) is True
        assert env_bool("BOG_AGENTS_DEBUG", True) is True

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("1", True),
            ("ON", True),
            (" yes ", True),
            ("0", False),
            ("off", False),
            ("", False),
            ("maybe", None),
            ("2", None),
        ],
    )
    def test_classify_env_bool(self, raw: str, expected: bool | None) -> None:
        """`classify_env_bool` maps the documented token set correctly."""
        assert classify_env_bool(raw) is expected
