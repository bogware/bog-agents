"""Build-time smoke test for the bog_agents lazy-import map.

``bog_agents/__init__.py`` exposes ~100 middleware classes via a
``_LAZY_IMPORTS`` dict so ``import bog_agents`` stays fast. The cost of that
deferral is that a syntax error or a missing optional dependency inside a
middleware module surfaces only when a user first touches that attribute,
with a confusing stack trace originating in user code.

This test runs the full advertised import graph in CI so any breakage
shows up against the offending PR rather than a downstream user.
"""

from __future__ import annotations

import importlib

import pytest

from bog_agents import _LAZY_IMPORTS


@pytest.mark.parametrize(("name", "target"), sorted(_LAZY_IMPORTS.items()))
def test_lazy_import_resolves(name: str, target: tuple[str, str]) -> None:
    module_path, attr = target
    module = importlib.import_module(module_path)
    assert hasattr(module, attr), f"_LAZY_IMPORTS[{name!r}] points at {module_path}.{attr} but that attribute does not exist in {module_path}."


def test_lazy_import_keys_match_module_attr_access() -> None:
    """Every advertised name must resolve through ``bog_agents.<name>``."""
    import bog_agents

    for name in _LAZY_IMPORTS:
        obj = getattr(bog_agents, name)
        assert obj is not None, f"bog_agents.{name} resolved to None"


# ---------------------------------------------------------------------------
# P0-B regression: importing bog_agents.middleware must not eagerly load
# every submodule. This was the contract CLAUDE.md described and the
# previous __init__.py violated. See REVIEW.md.
# ---------------------------------------------------------------------------


def test_middleware_package_is_lazy() -> None:
    """A fresh subprocess that imports ``bog_agents.middleware`` must not
    pull in the full 95-module set. We check via subprocess so module
    caching from sibling tests can't mask the regression.
    """
    import subprocess
    import sys
    import textwrap

    script = textwrap.dedent(
        """
        import sys

        # Anything outside ``bog_agents.middleware.*`` may be pre-loaded
        # by Python's own startup, so we only compare middleware modules.
        before = {m for m in sys.modules if m.startswith('bog_agents.middleware.')}
        import bog_agents.middleware
        after = {m for m in sys.modules if m.startswith('bog_agents.middleware.')}
        loaded = sorted(after - before)
        print(len(loaded))
        for m in loaded:
            print(m)
        """
    )

    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=True,
    )
    lines = result.stdout.strip().splitlines()
    count = int(lines[0])
    # Before P0-B was fixed this was ~95. The fix brings it well under 20.
    # 30 is a generous ceiling that still trips on a regression to the
    # eager-import pattern.
    assert count < 30, (
        f"middleware/__init__.py loaded {count} submodules — should be < 30. "
        "Did someone re-add eager `from bog_agents.middleware.X import Y` lines? "
        f"Loaded:\n  {chr(10).join(lines[1:])}"
    )


def test_middleware_package_does_not_pull_aiohttp() -> None:
    """The browser_agent / http_hooks chain pulls aiohttp, and the old
    eager __init__.py pulled the whole chain on every ``import
    bog_agents.middleware``. We assert aiohttp stays unloaded until a
    caller explicitly asks for it.
    """
    import subprocess
    import sys
    import textwrap

    script = textwrap.dedent(
        """
        import sys

        import bog_agents.middleware  # noqa: F401

        # The lazy import contract: just importing the package must not
        # transitively load aiohttp. (Touching BrowserAgentMiddleware
        # would, but no one called __getattr__ yet.)
        loaded = [m for m in sys.modules if m == 'aiohttp' or m.startswith('aiohttp.')]
        print(len(loaded))
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=True,
    )
    count = int(result.stdout.strip())
    assert count == 0, (
        f"importing bog_agents.middleware pulled aiohttp ({count} modules) — P0-B regression. Run __getattr__ probe to find the offender."
    )


def test_attribute_access_resolves_lazily() -> None:
    """A symbol in ``_LAZY_IMPORTS`` must be reachable via attribute access
    AND must equal the canonical class on its backing module.
    """
    import bog_agents.middleware as m
    from bog_agents.middleware.filesystem import FilesystemMiddleware

    assert m.FilesystemMiddleware is FilesystemMiddleware


def test_unknown_attribute_raises_attribute_error() -> None:
    import bog_agents.middleware as m

    with pytest.raises(AttributeError, match="DefinitelyNotARealMiddleware"):
        _ = m.DefinitelyNotARealMiddleware
