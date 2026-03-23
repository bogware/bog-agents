"""Knowledge graph builder middleware.

Feature #17: Entity extraction and relationship mapping for building structured
knowledge graphs during agent conversations.

## Overview

The knowledge graph middleware provides tools for:

- Adding entities with types and attributes
- Defining relationships between entities
- Querying entities and their connections
- Generating graph summaries

## Entity Types

Supported entity types include: company, person, product, regulation, metric,
organization, location, event, and any custom type.

## Relationship Types

Common relationship types: owns, regulates, competes_with, manages, advises,
invested_in, supplies, partners_with, and any custom type.

## Usage

```python
from bog_agents.middleware.knowledge_graph import KnowledgeGraphMiddleware

middleware = KnowledgeGraphMiddleware()
```
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Annotated

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ContextT,
    ModelRequest,
    ModelResponse,
    ResponseT,
)
from langchain.tools import ToolRuntime
from langchain_core.tools import BaseTool, StructuredTool
from typing_extensions import TypedDict

from bog_agents.middleware._utils import append_to_system_message

logger = logging.getLogger(__name__)


@dataclass
class Entity:
    """A node in the knowledge graph.

    Attributes:
        entity_id: Unique identifier for this entity.
        name: Human-readable name.
        entity_type: Type of entity (company, person, product, regulation, metric).
        attributes: Key-value attributes for this entity.
    """

    entity_id: int
    name: str
    entity_type: str
    attributes: dict[str, str] = field(default_factory=dict)


@dataclass
class Relationship:
    """An edge in the knowledge graph.

    Attributes:
        from_entity: Name of the source entity.
        to_entity: Name of the target entity.
        relationship_type: Type of relationship (owns, regulates, competes_with, etc.).
        properties: Key-value properties for this relationship.
    """

    from_entity: str
    to_entity: str
    relationship_type: str
    properties: dict[str, str] = field(default_factory=dict)


@dataclass
class KnowledgeGraph:
    """A graph of entities and relationships.

    Attributes:
        entities: Map of entity name to Entity.
        relationships: List of all relationships.
    """

    entities: dict[str, Entity] = field(default_factory=dict)
    relationships: list[Relationship] = field(default_factory=list)
    _next_id: int = field(default=1, repr=False)

    def add_entity(
        self,
        *,
        name: str,
        entity_type: str,
        attributes: dict[str, str] | None = None,
    ) -> Entity:
        """Add an entity to the graph.

        Args:
            name: Human-readable name.
            entity_type: Type of entity.
            attributes: Key-value attributes.

        Returns:
            The newly created or updated entity.
        """
        if name in self.entities:
            existing = self.entities[name]
            existing.entity_type = entity_type
            if attributes:
                existing.attributes.update(attributes)
            logger.debug("Updated entity: %s", name)
            return existing

        entity = Entity(
            entity_id=self._next_id,
            name=name,
            entity_type=entity_type,
            attributes=attributes or {},
        )
        self.entities[name] = entity
        self._next_id += 1
        logger.debug("Added entity #%d: %s (%s)", entity.entity_id, name, entity_type)
        return entity

    def add_relationship(
        self,
        *,
        from_entity: str,
        to_entity: str,
        relationship_type: str,
        properties: dict[str, str] | None = None,
    ) -> Relationship:
        """Add a relationship between two entities.

        Args:
            from_entity: Name of the source entity.
            to_entity: Name of the target entity.
            relationship_type: Type of relationship.
            properties: Key-value properties.

        Returns:
            The newly created relationship.
        """
        rel = Relationship(
            from_entity=from_entity,
            to_entity=to_entity,
            relationship_type=relationship_type,
            properties=properties or {},
        )
        self.relationships.append(rel)
        logger.debug("Added relationship: %s -[%s]-> %s", from_entity, relationship_type, to_entity)
        return rel

    def get_entity_relationships(self, name: str) -> list[Relationship]:
        """Get all relationships involving an entity.

        Args:
            name: Entity name to query.

        Returns:
            List of relationships where the entity is source or target.
        """
        return [r for r in self.relationships if name in (r.from_entity, r.to_entity)]

    def format_summary(self) -> str:
        """Format a human-readable summary of the knowledge graph.

        Returns:
            Formatted knowledge graph summary string.
        """
        if not self.entities and not self.relationships:
            return "Knowledge graph is empty. Use `add_entity` and `add_relationship` to build it."

        lines = [
            "## Knowledge Graph Summary",
            f"Entities: {len(self.entities)} | Relationships: {len(self.relationships)}",
            "",
        ]

        # Entity types breakdown
        type_counts: dict[str, int] = {}
        for entity in self.entities.values():
            type_counts[entity.entity_type] = type_counts.get(entity.entity_type, 0) + 1
        if type_counts:
            lines.append("### Entity Types")
            for etype, count in sorted(type_counts.items()):
                lines.append(f"- {etype}: {count}")
            lines.append("")

        # Relationship types breakdown
        rel_counts: dict[str, int] = {}
        for rel in self.relationships:
            rel_counts[rel.relationship_type] = rel_counts.get(rel.relationship_type, 0) + 1
        if rel_counts:
            lines.append("### Relationship Types")
            for rtype, count in sorted(rel_counts.items()):
                lines.append(f"- {rtype}: {count}")
            lines.append("")

        # Entity listing
        lines.append("### Entities")
        for entity in self.entities.values():
            attr_str = ""
            if entity.attributes:
                attr_str = f" — {json.dumps(entity.attributes)}"
            lines.append(f"- [{entity.entity_type}] {entity.name}{attr_str}")
        lines.append("")

        # Relationship listing
        if self.relationships:
            lines.append("### Relationships")
            for rel in self.relationships:
                prop_str = ""
                if rel.properties:
                    prop_str = f" {json.dumps(rel.properties)}"
                lines.append(f"- {rel.from_entity} -[{rel.relationship_type}]-> {rel.to_entity}{prop_str}")
            lines.append("")

        return "\n".join(lines)


KNOWLEDGE_GRAPH_SYSTEM_PROMPT = """## Knowledge Graph Builder

You have access to a knowledge graph for tracking entities and their relationships.

**Entity Types:** company, person, product, regulation, metric, organization, location, event
**Relationship Types:** owns, regulates, competes_with, manages, advises, invested_in, supplies, partners_with

**Workflow:**
1. Use `add_entity` to register entities as you discover them
2. Use `add_relationship` to connect entities
3. Use `query_entity` to explore connections
4. Use `graph_summary` to review the full graph

Always extract entities and relationships from research data to build a comprehensive knowledge map."""


class KnowledgeGraphState(TypedDict):
    """State for knowledge graph middleware."""


class KnowledgeGraphMiddleware(AgentMiddleware[KnowledgeGraphState, ContextT, ResponseT]):
    """Middleware for building knowledge graphs during agent conversations.

    Provides tools for adding entities, defining relationships, querying
    connections, and generating graph summaries.
    """

    state_schema = KnowledgeGraphState

    def __init__(self) -> None:
        self.graph = KnowledgeGraph()
        self.tools: list[BaseTool] = self._build_tools()

    def _build_tools(self) -> list[BaseTool]:
        """Build knowledge graph tools."""
        mw = self

        def add_entity(
            runtime: ToolRuntime[None, KnowledgeGraphState],
            name: Annotated[str, "Name of the entity"],
            entity_type: Annotated[str, "Type: company, person, product, regulation, metric, organization, location, event"],
            attributes: Annotated[str, "JSON string of key-value attributes"] = "{}",
        ) -> str:
            """Add an entity to the knowledge graph."""
            try:
                attrs = json.loads(attributes) if attributes else {}
            except json.JSONDecodeError:
                return f"Error: Invalid JSON for attributes: {attributes}"
            entity = mw.graph.add_entity(
                name=name,
                entity_type=entity_type,
                attributes=attrs,
            )
            return f"Entity #{entity.entity_id} added: [{entity_type}] {name}"

        def add_relationship(
            runtime: ToolRuntime[None, KnowledgeGraphState],
            from_entity: Annotated[str, "Name of the source entity"],
            to_entity: Annotated[str, "Name of the target entity"],
            relationship_type: Annotated[str, "Type: owns, regulates, competes_with, manages, advises, invested_in, supplies, partners_with"],
            properties: Annotated[str, "JSON string of relationship properties"] = "{}",
        ) -> str:
            """Add a relationship between two entities in the knowledge graph."""
            try:
                props = json.loads(properties) if properties else {}
            except json.JSONDecodeError:
                return f"Error: Invalid JSON for properties: {properties}"
            mw.graph.add_relationship(
                from_entity=from_entity,
                to_entity=to_entity,
                relationship_type=relationship_type,
                properties=props,
            )
            return f"Relationship added: {from_entity} -[{relationship_type}]-> {to_entity}"

        def query_entity(
            runtime: ToolRuntime[None, KnowledgeGraphState],
            name: Annotated[str, "Name of the entity to query"],
        ) -> str:
            """Get an entity and all its relationships from the knowledge graph."""
            entity = mw.graph.entities.get(name)
            if entity is None:
                return f"Entity '{name}' not found in the knowledge graph."

            lines = [
                f"## Entity: {entity.name}",
                f"Type: {entity.entity_type}",
                f"ID: {entity.entity_id}",
            ]
            if entity.attributes:
                lines.append(f"Attributes: {json.dumps(entity.attributes)}")

            rels = mw.graph.get_entity_relationships(name)
            if rels:
                lines.append("")
                lines.append(f"### Relationships ({len(rels)})")
                for rel in rels:
                    if rel.from_entity == name:
                        lines.append(f"- -[{rel.relationship_type}]-> {rel.to_entity}")
                    else:
                        lines.append(f"- <-[{rel.relationship_type}]- {rel.from_entity}")
                    if rel.properties:
                        lines.append(f"  Properties: {json.dumps(rel.properties)}")
            else:
                lines.append("\nNo relationships found for this entity.")

            return "\n".join(lines)

        def graph_summary(
            runtime: ToolRuntime[None, KnowledgeGraphState],
        ) -> str:
            """Format the entire knowledge graph summary."""
            return mw.graph.format_summary()

        def clear_graph(
            runtime: ToolRuntime[None, KnowledgeGraphState],
        ) -> str:
            """Clear all entities and relationships from the knowledge graph."""
            mw.graph.entities.clear()
            mw.graph.relationships.clear()
            mw.graph._next_id = 1
            return "Knowledge graph cleared. Ready to build a new graph."

        return [
            StructuredTool.from_function(
                name="add_entity",
                description="Add an entity to the knowledge graph with a type and optional attributes.",
                func=add_entity,
            ),
            StructuredTool.from_function(
                name="add_relationship",
                description="Add a relationship between two entities in the knowledge graph.",
                func=add_relationship,
            ),
            StructuredTool.from_function(
                name="query_entity",
                description="Get an entity and all its relationships from the knowledge graph.",
                func=query_entity,
            ),
            StructuredTool.from_function(
                name="graph_summary",
                description="Format the entire knowledge graph summary with entity and relationship breakdowns.",
                func=graph_summary,
            ),
            StructuredTool.from_function(
                name="clear_graph",
                description="Clear all entities and relationships from the knowledge graph.",
                func=clear_graph,
            ),
        ]

    def modify_request(self, request: ModelRequest[ContextT]) -> ModelRequest[ContextT]:
        """Inject knowledge graph instructions into the system prompt.

        Args:
            request: Model request to modify.

        Returns:
            Modified request with knowledge graph instructions.
        """
        new_system_message = append_to_system_message(request.system_message, KNOWLEDGE_GRAPH_SYSTEM_PROMPT)
        return request.override(system_message=new_system_message)

    def wrap_model_call(
        self,
        request: ModelRequest[ContextT],
        call_next: Callable[[ModelRequest[ContextT]], ModelResponse[ResponseT]],
    ) -> ModelResponse[ResponseT]:
        """Inject knowledge graph instructions.

        Args:
            request: Model request.
            call_next: Handler function.

        Returns:
            Model response.
        """
        modified = self.modify_request(request)
        return call_next(modified)

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        call_next: Callable[[ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]],
    ) -> ModelResponse[ResponseT]:
        """Async version of wrap_model_call.

        Args:
            request: Model request.
            call_next: Async handler function.

        Returns:
            Model response.
        """
        modified = self.modify_request(request)
        return await call_next(modified)


__all__ = ["Entity", "KnowledgeGraph", "KnowledgeGraphMiddleware", "Relationship"]
