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
    assert hasattr(module, attr), (
        f"_LAZY_IMPORTS[{name!r}] points at {module_path}.{attr} but that "
        f"attribute does not exist in {module_path}."
    )


def test_lazy_import_keys_match_module_attr_access() -> None:
    """Every advertised name must resolve through ``bog_agents.<name>``."""
    import bog_agents

    for name in _LAZY_IMPORTS:
        obj = getattr(bog_agents, name)
        assert obj is not None, f"bog_agents.{name} resolved to None"
