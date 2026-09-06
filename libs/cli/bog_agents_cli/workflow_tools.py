"""Agent tools for authoring workflows (ROADMAP #73): `author_workflow`, `list_workflows`.

Registered by `create_cli_agent` when the project already has a
`.bog-agents/workflows/` directory or `tools.workflows` is on, so projects
that never use workflows pay no schema tokens. The agent writes the YAML
itself (it is the author); the tool validates it against the schema, saves it,
and tells the agent how the user runs it.
"""

from __future__ import annotations

from pathlib import Path

from langchain_core.tools import BaseTool, StructuredTool

from bog_agents_cli.workflow import (
    AUTHOR_SCHEMA,
    describe_workflows,
    discover_workflows,
    parse_workflow,
    save_workflow,
)


def workflow_tools_bundle(project_root: str | Path) -> list[BaseTool]:
    """Two tools bound to `project_root`."""
    root = Path(project_root)

    def author_workflow(yaml_text: str) -> str:
        """Save a reusable multi-phase workflow as `.bog-agents/workflows/<name>.yaml`; the user then runs it as `/<name> [args]`.

        Write the whole workflow as YAML in `yaml_text`. Phases run in order
        (kinds: context, work, review, verify, synthesize); each phase's tasks
        fan out over `workers` teammates under the session's spawn / spend
        caps; review and verify phases are gates whose tasks must end with
        `VERDICT: PASS`. `{argname}` in a title or prompt is filled from the
        declared `args`; `{context}` inserts earlier phases' results.
        Returns the saved path, or the validation error with the schema.
        """
        try:
            workflow = parse_workflow(yaml_text)
        except ValueError as exc:
            return f"Error: {exc}\n\nSchema:\n{AUTHOR_SCHEMA}"
        path = save_workflow(root, workflow)
        return f"Saved {workflow.usage()} to {path} ({len(workflow.phases)} phases, {workflow.task_count} tasks). Tell the user to run it with {workflow.usage()}."

    def list_workflows() -> str:
        """List the project's saved workflows and how to run them."""
        return describe_workflows(list(discover_workflows(root).values()))

    return [
        StructuredTool.from_function(func=author_workflow, name="author_workflow"),
        StructuredTool.from_function(func=list_workflows, name="list_workflows"),
    ]


__all__ = ["workflow_tools_bundle"]
