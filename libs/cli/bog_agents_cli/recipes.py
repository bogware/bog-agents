"""Recipes-as-Pipelines — curated YAML pipelines with a registry.

A Recipe is a parameterized YAML pipeline shipped with bog-agents. Users
``install`` a recipe, which copies it into their pipelines directory
(``~/.bog-agents/pipelines/<id>.yaml``) where the existing
``pipeline.py`` runtime can pick it up. Installed recipes can then run
via ``/pipeline <id>`` or be scheduled via the daemon.

This module owns the curated catalog and the install/uninstall actions;
it intentionally does *not* re-implement YAML parsing — it delegates to
:mod:`bog_agents_cli.pipeline`.

The CLI entry point ``bog-agents recipe install <id>`` and the
``/recipe`` slash command both delegate here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Recipe:
    """One curated recipe definition."""

    id: str
    title: str
    summary: str
    yaml: str
    tags: tuple[str, ...] = field(default_factory=tuple)
    notes: str = ""


# ---------------------------------------------------------------------------
# Curated catalog
# ---------------------------------------------------------------------------


_CODE_REVIEW_YAML = """\
name: code-review
description: Multi-pass code review of staged changes — security, perf, style.
variables:
  - severity_floor
steps:
  - id: gather
    type: slash
    command: /diff staged
  - id: review
    type: message
    text: |
      Review the staged changes above. Severity floor: {{severity_floor}}.
      Output a triaged list of findings (security / perf / style) with
      suggested fixes.
"""


_TEST_GAP_YAML = """\
name: test-gap-analysis
description: Inspect a module and identify untested branches + propose tests.
variables:
  - module_path
steps:
  - id: inspect
    type: message
    text: |
      Read {{module_path}} and its existing tests. Output:
      1. Functions with no test coverage (file:line list)
      2. Branches that look reachable but untested
      3. A prioritized list of test cases to add (top 5)
"""


_TYPECHECK_FIX_YAML = """\
name: typecheck-fix
description: Run the project's type checker and fix the first 5 errors.
steps:
  - id: run-types
    type: slash
    command: /test
  - id: fix
    type: message
    text: |
      Pick the 5 highest-leverage type errors from the output above and
      fix them. Prefer changing the implementation over loosening the
      type when both are viable.
"""


_DEPENDENCY_AUDIT_YAML = """\
name: dependency-audit
description: Audit dependencies for CVEs and outdated majors.
steps:
  - id: audit
    type: message
    text: |
      Run language-specific dependency audits (npm audit, pip-audit, etc.)
      where available. Group findings by severity. For CVEs, include CVSS
      score and the version that fixes them. Do NOT mutate lockfiles.
"""


_INCIDENT_TRIAGE_YAML = """\
name: incident-triage
description: Triage a production incident from a stack trace + recent diff.
variables:
  - stack_trace
steps:
  - id: triage
    type: message
    text: |
      A production incident was reported with this stack trace:

      ```
      {{stack_trace}}
      ```

      Steps:
      1. Identify the file/function at the top of the trace.
      2. Read the file and recent diff history.
      3. Determine the most likely root cause.
      4. Propose the minimal hot-fix.
      5. Output: { root_cause, hotfix_diff_sketch, follow_ups }.
"""


CATALOG: tuple[Recipe, ...] = (
    Recipe(
        id="code-review",
        title="Code Review",
        summary="Multi-pass review of staged changes — security / perf / style triage.",
        yaml=_CODE_REVIEW_YAML,
        tags=("quality", "review", "git"),
    ),
    Recipe(
        id="test-gap-analysis",
        title="Test Gap Analysis",
        summary="Find untested branches in a module and propose tests.",
        yaml=_TEST_GAP_YAML,
        tags=("quality", "tests"),
    ),
    Recipe(
        id="typecheck-fix",
        title="Typecheck → Fix",
        summary="Run the project's type checker and fix the first 5 errors.",
        yaml=_TYPECHECK_FIX_YAML,
        tags=("quality", "types"),
    ),
    Recipe(
        id="dependency-audit",
        title="Dependency Audit",
        summary="CVE + outdated-major sweep across language ecosystems.",
        yaml=_DEPENDENCY_AUDIT_YAML,
        tags=("security", "dependencies"),
    ),
    Recipe(
        id="incident-triage",
        title="Incident Triage",
        summary="Take a stack trace + repo state, output root cause + hot-fix sketch.",
        yaml=_INCIDENT_TRIAGE_YAML,
        tags=("ops", "incident"),
    ),
)


def get_recipe(recipe_id: str) -> Recipe | None:
    """Return one recipe by id (case-insensitive), or ``None``."""
    needle = recipe_id.strip().lower()
    for recipe in CATALOG:
        if recipe.id.lower() == needle:
            return recipe
    return None


def list_recipes(*, tag: str | None = None) -> list[Recipe]:
    """Return the catalog, optionally filtered by tag."""
    if tag is None:
        return list(CATALOG)
    needle = tag.lower()
    return [r for r in CATALOG if needle in (t.lower() for t in r.tags)]


def install_recipe(
    recipe_id: str,
    *,
    pipelines_dir: Path | None = None,
    overwrite: bool = False,
) -> Path:
    """Copy a recipe's YAML into the user's pipelines directory.

    Args:
        recipe_id: Catalog id (case-insensitive).
        pipelines_dir: Override target directory (mostly for tests). Defaults
            to ``~/.bog-agents/pipelines``.
        overwrite: When ``True``, replace an existing pipeline file with the
            same id; otherwise raise ``FileExistsError``.

    Returns:
        Path to the newly written pipeline file.

    Raises:
        ValueError: If ``recipe_id`` is not in the catalog.
        FileExistsError: If a pipeline with the same id is already installed
            and ``overwrite`` is ``False``.
    """
    recipe = get_recipe(recipe_id)
    if recipe is None:
        msg = f"No recipe with id '{recipe_id}'."
        raise ValueError(msg)

    target_dir = (
        pipelines_dir if pipelines_dir is not None else _default_pipelines_dir()
    )
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{recipe.id}.yaml"

    if target.exists() and not overwrite:
        msg = f"{target} already exists; pass overwrite=True to replace it."
        raise FileExistsError(msg)

    target.write_text(recipe.yaml, encoding="utf-8")
    return target


def uninstall_recipe(
    recipe_id: str,
    *,
    pipelines_dir: Path | None = None,
) -> bool:
    """Delete a previously-installed recipe pipeline file.

    Returns ``True`` if a file was removed, ``False`` if it was not
    installed in the first place.
    """
    target_dir = (
        pipelines_dir if pipelines_dir is not None else _default_pipelines_dir()
    )
    target = target_dir / f"{recipe_id.strip().lower()}.yaml"
    if not target.exists():
        return False
    try:
        target.unlink()
    except OSError:
        logger.warning("could not delete %s", target, exc_info=True)
        return False
    return True


def is_installed(
    recipe_id: str,
    *,
    pipelines_dir: Path | None = None,
) -> bool:
    """Return ``True`` if a pipeline file with this recipe id exists."""
    target_dir = (
        pipelines_dir if pipelines_dir is not None else _default_pipelines_dir()
    )
    return (target_dir / f"{recipe_id.strip().lower()}.yaml").is_file()


def _default_pipelines_dir() -> Path:
    """Return ``~/.bog-agents/pipelines``, creating the parent if needed."""
    return Path.home() / ".bog-agents" / "pipelines"


__all__ = [
    "CATALOG",
    "Recipe",
    "get_recipe",
    "install_recipe",
    "is_installed",
    "list_recipes",
    "uninstall_recipe",
]
