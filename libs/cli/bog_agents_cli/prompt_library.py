"""Full-featured prompt library with CRUD, variable substitution, and storage.

Prompts are stored in ``~/.bog-agents/prompt_library.toml``.  Each prompt
has a name, body, optional description, and an optional list of variable
names that are substituted at run-time using ``{{variable}}`` syntax.

Example ``prompt_library.toml``::

    [prompts.security-review]
    description = "Full security review of a module"
    body = \"\"\"
    Perform a thorough security review of {{module}}.
    Focus on: {{focus_areas}}.
    Output a prioritised finding list.
    \"\"\"
    variables = ["module", "focus_areas"]

    [prompts.explain-function]
    description = "Explain what a function does"
    body = "Explain {{function_name}} in {{file}} step by step."
    variables = ["function_name", "file"]
"""

from __future__ import annotations

import logging
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_LIBRARY_PATH = Path.home() / ".bog-agents" / "prompt_library.toml"
_VAR_RE = re.compile(r"\{\{(\w+)\}\}")

# Module-level cache — cleared by clear_cache()
_cache: dict[str, PromptEntry] | None = None


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class PromptEntry:
    """A single prompt in the library."""

    name: str
    body: str
    description: str = ""
    variables: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        """Auto-detect variables from body if not explicitly set."""
        if not self.variables:
            self.variables = _VAR_RE.findall(self.body)

    def render(self, values: dict[str, str]) -> str:
        """Substitute ``{{var}}`` placeholders with *values*.

        Args:
            values: Mapping of variable name → replacement string.

        Returns:
            The prompt body with all placeholders substituted.

        Raises:
            KeyError: If a required variable is missing from *values*.
        """
        result = self.body
        for var in self.variables:
            if var not in values:
                msg = f"Missing required variable '{{var}}' for prompt '{self.name}'"
                raise KeyError(msg)
            result = result.replace(f"{{{{{var}}}}}", values[var])
        return result

    def missing_variables(self, provided: dict[str, str]) -> list[str]:
        """Return variable names present in the body but not in *provided*.

        Args:
            provided: Partially-filled variable mapping.

        Returns:
            List of variable names still needed.
        """
        return [v for v in self.variables if v not in provided]


# ---------------------------------------------------------------------------
# Storage helpers
# ---------------------------------------------------------------------------


def _load_raw(path: Path) -> dict[str, Any]:
    """Load raw TOML data from *path*.  Returns empty dict on any error."""
    if not path.exists():
        return {}
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except (tomllib.TOMLDecodeError, OSError) as exc:
        logger.warning("Could not read prompt library from %s: %s", path, exc)
        return {}


def _save_raw(data: dict[str, Any], path: Path) -> None:
    """Atomically write *data* to *path* as TOML."""
    import contextlib
    import os
    import tempfile

    import tomli_w

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            tomli_w.dump(data, fh)
        Path(tmp).replace(path)
    except BaseException:
        with contextlib.suppress(OSError):
            Path(tmp).unlink()
        raise


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def clear_cache() -> None:
    """Invalidate the in-memory prompt library cache."""
    global _cache  # noqa: PLW0603
    _cache = None


def load_library(path: Path | None = None) -> dict[str, PromptEntry]:
    """Load all prompts from the library file.

    Results are cached after the first call.  Call :func:`clear_cache` to
    force a reload.

    Args:
        path: Override the default library path.  Mainly used in tests.

    Returns:
        Mapping of prompt name → :class:`PromptEntry`.
    """
    global _cache  # noqa: PLW0603
    if path is None and _cache is not None:
        return _cache

    lib_path = path or _LIBRARY_PATH
    data = _load_raw(lib_path)
    prompts_raw = data.get("prompts", {})

    entries: dict[str, PromptEntry] = {}
    for name, raw in prompts_raw.items():
        if not isinstance(raw, dict):
            logger.warning("Skipping invalid prompt entry '%s' (expected table)", name)
            continue
        body = raw.get("body", "")
        if not body:
            logger.warning("Skipping prompt '%s': empty body", name)
            continue
        entries[name] = PromptEntry(
            name=name,
            body=body,
            description=str(raw.get("description", "")),
            variables=list(raw.get("variables", [])) or _VAR_RE.findall(body),
        )

    if path is None:
        _cache = entries
    return entries


def get_prompt(name: str, path: Path | None = None) -> PromptEntry | None:
    """Retrieve a single prompt by name.

    Args:
        name: Prompt name (case-sensitive).
        path: Override library path.

    Returns:
        :class:`PromptEntry` or ``None`` if not found.
    """
    return load_library(path).get(name)


def save_prompt(entry: PromptEntry, path: Path | None = None) -> None:
    """Persist *entry* to the library file.

    Creates the file and ``[prompts]`` section if they don't exist.  Existing
    prompts with other names are preserved.

    Args:
        entry: The prompt to save.
        path: Override library path.
    """
    lib_path = path or _LIBRARY_PATH
    data = _load_raw(lib_path)
    prompts = data.setdefault("prompts", {})
    prompts[entry.name] = {
        "description": entry.description,
        "body": entry.body,
        "variables": entry.variables,
    }
    _save_raw(data, lib_path)
    clear_cache()
    logger.info("Saved prompt '%s' to %s", entry.name, lib_path)


def delete_prompt(name: str, path: Path | None = None) -> bool:
    """Remove a prompt from the library.

    Args:
        name: Prompt name to delete.
        path: Override library path.

    Returns:
        ``True`` if the prompt existed and was deleted, ``False`` otherwise.
    """
    lib_path = path or _LIBRARY_PATH
    data = _load_raw(lib_path)
    prompts = data.get("prompts", {})
    if name not in prompts:
        return False
    del prompts[name]
    _save_raw(data, lib_path)
    clear_cache()
    logger.info("Deleted prompt '%s' from %s", name, lib_path)
    return True


def render_prompt(name: str, values: dict[str, str], path: Path | None = None) -> str:
    """Load *name* and substitute *values*.

    Args:
        name: Prompt name.
        values: Variable substitutions.
        path: Override library path.

    Returns:
        Rendered prompt string.

    Raises:
        KeyError: If the prompt doesn't exist or a variable is missing.
    """
    entry = get_prompt(name, path)
    if entry is None:
        msg = f"Prompt '{name}' not found in library"
        raise KeyError(msg)
    return entry.render(values)
