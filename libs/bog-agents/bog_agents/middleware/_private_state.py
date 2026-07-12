"""Introspection helpers for private (schema-omitted) state keys.

Middleware state schemas mark bookkeeping fields with langchain's
`PrivateStateAttr` marker so they are omitted from the agent's input/output
schemas. Consumers that build state *by hand* -- most notably the sub-agent
middleware, which forwards a curated slice of parent state into a child graph --
need to know which keys those are without hard-coding a list that silently rots
whenever a schema grows a field.

This module is deliberately named `_private_state` rather than `_state`: the
latter name is already taken in this package by the unrelated `MiddlewareState`
lock holder.
"""

from __future__ import annotations

import logging
from typing import Annotated, NotRequired, Required, get_args, get_origin, get_type_hints

from langchain.agents.middleware.types import PrivateStateAttr

__all__ = ["private_state_field_names"]

logger = logging.getLogger(__name__)

# Wrappers we look *through* when hunting for the marker. Recursing through
# arbitrary generics instead would mis-flag containers whose element type is
# private, e.g. `dict[str, Annotated[int, PrivateStateAttr]]`.
_TRANSPARENT_ORIGINS = (NotRequired, Required)


def _has_marker(annotation: object, marker: object) -> bool:
    """Check whether an annotation carries `marker`, through any wrapper nesting.

    bog-agents state schemas use both nesting orders -- `NotRequired[Annotated[X, marker]]`
    (rubric, skills, memory) and `Annotated[NotRequired[X], marker]` (summarization) --
    so both must be unwrapped in either order.

    Args:
        annotation: A resolved type annotation, as returned by `get_type_hints(..., include_extras=True)`.
        marker: The marker object to look for in `Annotated` metadata.

    Returns:
        True if `marker` appears in the annotation's `Annotated` metadata at any wrapper depth.
    """
    origin = get_origin(annotation)
    if origin is Annotated:
        args = get_args(annotation)
        metadata = args[1:]
        if any(meta is marker or meta == marker for meta in metadata):
            return True
        # `Annotated[NotRequired[Annotated[X, marker]], other]` -- keep digging.
        return _has_marker(args[0], marker)
    if origin in _TRANSPARENT_ORIGINS:
        return any(_has_marker(arg, marker) for arg in get_args(annotation))
    return False


def private_state_field_names(*schemas: type) -> frozenset[str]:
    """Collect the names of every field marked `PrivateStateAttr` across state schemas.

    Args:
        schemas: State schemas (TypedDict subclasses, or anything `get_type_hints` accepts).
            Schemas whose annotations cannot be resolved are skipped rather than raising,
            so a single un-importable forward reference can't take down agent construction.

    Returns:
        The union of private field names across all schemas. Empty if none are marked.
    """
    names: set[str] = set()
    for schema in schemas:
        # An unresolvable schema must not take down agent construction, but it must also not
        # pass silently: the caller uses this set to *withhold* keys from a sub-agent, so an
        # empty result leaks private state rather than failing closed.
        try:
            hints = get_type_hints(schema, include_extras=True)
        except (NameError, AttributeError, TypeError):
            logger.warning(
                "Could not resolve annotations for state schema %r; its private keys will not be filtered.",
                getattr(schema, "__name__", schema),
                exc_info=True,
            )
            continue
        names.update(name for name, annotation in hints.items() if _has_marker(annotation, PrivateStateAttr))
    return frozenset(names)
