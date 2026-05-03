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
        TypeError: If a discovered ``COMMANDS`` export contains an entry
            that isn't a ``SlashCommand``.
    """
    import bog_agents_cli.commands as _pkg

    commands: list[SlashCommand] = []
    handler_map: dict[str, str] = {}

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
            commands.append(command)
            handler_map[command.name] = command.handler_method
            for alias in command.spec.aliases:
                handler_map[alias] = command.handler_method

    return tuple(commands), handler_map
