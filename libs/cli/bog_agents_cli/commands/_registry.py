"""Discovery logic for the modular slash-command registry.

Kept out of ``__init__.py`` so the package init module remains a tidy
re-export surface (per the lint-rule policy in the bog-agents-cli ruff
config).
"""

from __future__ import annotations

import importlib
import pkgutil

from bog_agents_cli.commands._base import SlashCommand


def discover() -> tuple[tuple[SlashCommand, ...], dict[str, str]]:
    """Walk the ``bog_agents_cli.commands`` package and collect commands.

    Returns:
        A 2-tuple of (immutable ordered ``COMMANDS`` tuple, ``handler_map``
        ``{slash_name: app_method_name}`` including aliases).

    Raises:
        TypeError: When a discovered ``COMMANDS`` export contains a
            non-:class:`SlashCommand` entry, or when two commands /
            aliases claim the same slash name. The duplicate check
            previously lived only in a test (``test_commands_registry``);
            U5 moved it here so an at-import-time mistake fails fast
            instead of "test only on CI".
    """
    import bog_agents_cli.commands as _pkg

    commands: list[SlashCommand] = []
    handler_map: dict[str, str] = {}
    # U5: track where each slash name was registered so duplicate
    # diagnostics include both call sites.
    origin: dict[str, str] = {}

    for module_info in pkgutil.iter_modules(_pkg.__path__):
        if module_info.name.startswith("_"):
            continue
        module = importlib.import_module(f"bog_agents_cli.commands.{module_info.name}")
        exported = getattr(module, "COMMANDS", None)
        if exported is None:
            continue
        for command in exported:
            if not isinstance(command, SlashCommand):
                msg = f"{module.__name__}.COMMANDS contains a non-SlashCommand entry: {command!r}"
                raise TypeError(msg)
            if command.name in handler_map:
                msg = (
                    f"Slash-command name collision: {command.name!r} declared by "
                    f"{module.__name__} but already registered by "
                    f"{origin.get(command.name, '<unknown>')}. Slash names "
                    "must be unique across commands/*.py."
                )
                raise TypeError(msg)
            commands.append(command)
            handler_map[command.name] = command.handler_method
            origin[command.name] = module.__name__
            for alias in command.spec.aliases:
                if alias in handler_map:
                    msg = (
                        f"Slash-command alias collision: {alias!r} (from "
                        f"{command.name} in {module.__name__}) already maps to "
                        f"{handler_map[alias]!r} via {origin.get(alias, '<unknown>')}."
                    )
                    raise TypeError(msg)
                handler_map[alias] = command.handler_method
                origin[alias] = module.__name__

    # Stable order: featured commands first (in their canonical order),
    # then everything else alphabetically. Featured commands match the
    # /help showcase so the no-search autocomplete dropdown surfaces the
    # commands a typical user reaches for first; the alphabetical tail
    # keeps the rest discoverable and tests like
    # ``/clear < /compact < /docs`` happy.
    featured_priority = {name: i for i, name in enumerate(_FEATURED_FIRST_ORDER)}
    commands.sort(
        key=lambda c: (
            featured_priority.get(c.name, len(featured_priority)),
            c.name,
        )
    )
    return tuple(commands), handler_map


# Curated head of the registry — these surface first in autocomplete /
# palette views regardless of alphabetical position. Mirrors the /help
# featured-commands grid so users see the same set in both surfaces.
_FEATURED_FIRST_ORDER: tuple[str, ...] = (
    "/help",
    "/commands",
    "/clear",
    "/model",
    "/profile",
    "/plan",
    "/effort",
    "/compact",
    "/resume",
    "/threads",
    "/session",
    "/permissions",
    "/diff",
    "/worktree",
    "/agent",
    "/tasks",
    "/mcp",
    "/trace",
    "/tokens",
    "/background",
    "/plugin",
    "/remote",
    "/review",
    "/quit",
)
