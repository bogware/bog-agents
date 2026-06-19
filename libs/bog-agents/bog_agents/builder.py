"""Fluent builder API for creating bog-agents agents.

Provides a type-safe, discoverable alternative to the ``create_agent()``
function's 80+ keyword arguments. Group related settings into named
``with_X()`` methods; call ``.build()`` to compile the graph.

Quick start::

    from bog_agents import AgentBuilder

    agent = (
        AgentBuilder("claude-opus-4-7")
        .with_git(auto_commit=True, code_review=True, repo_map=True)
        .with_memory()
        .with_plan_mode()
        .with_cost_tracking(max_cost_usd=5.0)
        .build()
    )

``create_agent()`` remains fully backward-compatible — the builder is an
additive layer that calls it with accumulated keyword arguments.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from langgraph.graph.state import CompiledStateGraph


# ---------------------------------------------------------------------------
# Configuration dataclasses — one per logical group
# ---------------------------------------------------------------------------


@dataclass
class GitConfig:
    """Git and code-quality feature settings."""

    enabled: bool = True
    auto_commit: bool = False
    code_review: bool = False
    repo_map: bool = False
    auto_lint: bool = False
    auto_test: bool = False
    test_framework: str = "pytest"
    worktree: bool = False
    working_dir: str | None = None


@dataclass
class MemoryConfig:
    """Agent memory settings."""

    sources: list[str] = field(default_factory=list)
    knowledge_graph: bool = False


@dataclass
class PlanningConfig:
    """Planning and effort settings."""

    plan_mode: bool = False
    effort_level: str = "medium"
    architect_model: str | None = None
    reviewer_model: str | None = None


@dataclass
class SandboxConfig:
    """Execution sandbox settings."""

    backend: Any | None = None  # BackendProtocol | BackendFactory | None
    allow_dangerous: bool = False


@dataclass
class CostConfig:
    """Cost tracking settings."""

    enabled: bool = True
    max_cost_usd: float | None = None
    budget_usd: float | None = None


@dataclass
class ObservabilityConfig:
    """Audit, checkpointing, and tracing settings."""

    audit_trail: bool = False
    audit_session_id: str = ""
    audit_advisor_id: str = ""
    checkpointing: bool = False
    reasoning_chain: bool = False
    citations: bool = False


@dataclass
class SafetyConfig:
    """Approval gates and safety guardrails."""

    approval_gates: bool = False
    rbac: bool = False
    dlp: bool = False
    dlp_mode: str = "redact"
    air_gapped: bool = False
    hallucination_detection: bool = False


@dataclass
class MultiAgentConfig:
    """Multi-agent and team coordination settings."""

    enabled: bool = False
    max_threads: int = 10
    agent_teams: bool = False


@dataclass
class AgentConfig:
    """Full typed configuration for an agent built via `AgentBuilder`.

    Each field corresponds to one or more parameters of ``create_agent()``.
    """

    # Core settings
    model: str | None = None
    system_prompt: str = ""
    name: str | None = None
    debug: bool = False

    # Feature groups
    git: GitConfig = field(default_factory=GitConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    planning: PlanningConfig = field(default_factory=PlanningConfig)
    sandbox: SandboxConfig = field(default_factory=SandboxConfig)
    cost: CostConfig = field(default_factory=CostConfig)
    observability: ObservabilityConfig = field(default_factory=ObservabilityConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    multi_agent: MultiAgentConfig = field(default_factory=MultiAgentConfig)

    # Pass-through collections (merged into kwargs)
    tools: list[Any] = field(default_factory=list)
    middleware: list[Any] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    mcp_servers: list[str] = field(default_factory=list)
    subagents: list[Any] = field(default_factory=list)

    # Escape hatch for any param not yet surfaced by the builder
    extra_kwargs: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# AgentBuilder
# ---------------------------------------------------------------------------


class AgentBuilder:
    """Fluent builder for ``create_agent()``.

    Each ``with_X()`` method returns ``self`` for chaining.
    Call ``.build()`` when ready to compile the LangGraph state machine.

    Example::

        agent = (
            AgentBuilder("claude-opus-4-7")
            .with_git(auto_commit=True, repo_map=True)
            .with_plan_mode()
            .with_cost_tracking(max_cost_usd=2.0)
            .with_human_in_the_loop()
            .build()
        )
    """

    def __init__(self, model: str | None = None) -> None:
        """Initialize the builder.

        Args:
            model: LLM model identifier, e.g. ``"claude-opus-4-7"``.
                Accepts any format supported by ``create_agent()``.
        """
        self._config = AgentConfig(model=model)

    # ------------------------------------------------------------------
    # Core settings
    # ------------------------------------------------------------------

    def with_model(self, model: str) -> AgentBuilder:
        """Set the LLM model.

        Args:
            model: Model identifier string.
        """
        self._config.model = model
        return self

    def with_system_prompt(self, prompt: str) -> AgentBuilder:
        """Set the system prompt.

        Args:
            prompt: System prompt text. Appended to the base agent prompt.
        """
        self._config.system_prompt = prompt
        return self

    def with_name(self, name: str) -> AgentBuilder:
        """Set the agent name (appears in LangSmith traces).

        Args:
            name: Agent display name.
        """
        self._config.name = name
        return self

    def with_debug(self, enabled: bool = True) -> AgentBuilder:
        """Enable debug logging from the agent graph.

        Args:
            enabled: True to enable debug output.
        """
        self._config.debug = enabled
        return self

    # ------------------------------------------------------------------
    # Git & code-quality
    # ------------------------------------------------------------------

    def with_git(
        self,
        *,
        auto_commit: bool = False,
        code_review: bool = False,
        repo_map: bool = False,
        auto_lint: bool = False,
        auto_test: bool = False,
        test_framework: str = "pytest",
        worktree: bool = False,
        working_dir: str | None = None,
    ) -> AgentBuilder:
        """Enable git and code-quality tooling.

        Args:
            auto_commit: Auto-commit agent changes after each turn.
            code_review: Enable AI code review on diffs.
            repo_map: Build and maintain a repo map for context packing.
            auto_lint: Run linter after file edits.
            auto_test: Run test suite after code changes.
            test_framework: Test framework name (default: "pytest").
            worktree: Use git worktrees for parallel agent isolation.
            working_dir: Override the working directory for git commands.
        """
        self._config.git = GitConfig(
            enabled=True,
            auto_commit=auto_commit,
            code_review=code_review,
            repo_map=repo_map,
            auto_lint=auto_lint,
            auto_test=auto_test,
            test_framework=test_framework,
            worktree=worktree,
            working_dir=working_dir,
        )
        return self

    # ------------------------------------------------------------------
    # Memory
    # ------------------------------------------------------------------

    def with_memory(
        self,
        *,
        sources: list[str] | None = None,
        knowledge_graph: bool = False,
    ) -> AgentBuilder:
        """Configure agent memory.

        Args:
            sources: List of memory source paths (project/global memory files).
            knowledge_graph: Enable knowledge-graph-backed long-term memory.
        """
        self._config.memory = MemoryConfig(
            sources=sources or [],
            knowledge_graph=knowledge_graph,
        )
        return self

    # ------------------------------------------------------------------
    # Planning
    # ------------------------------------------------------------------

    def with_plan_mode(
        self,
        *,
        effort_level: str = "medium",
        architect_model: str | None = None,
        reviewer_model: str | None = None,
    ) -> AgentBuilder:
        """Enable plan mode — agent proposes a plan before executing.

        Args:
            effort_level: Effort hint ("low", "medium", "high").
            architect_model: Override model for the architect/planning phase.
            reviewer_model: Override model for code review.
        """
        self._config.planning = PlanningConfig(
            plan_mode=True,
            effort_level=effort_level,
            architect_model=architect_model,
            reviewer_model=reviewer_model,
        )
        return self

    def with_effort(self, level: str) -> AgentBuilder:
        """Set the effort level without enabling plan mode.

        Args:
            level: One of "low", "medium", "high".
        """
        self._config.planning.effort_level = level
        return self

    # ------------------------------------------------------------------
    # Sandbox / backend
    # ------------------------------------------------------------------

    def with_sandbox(
        self,
        backend: Any = None,  # noqa: ANN401
        *,
        allow_dangerous: bool = False,
    ) -> AgentBuilder:
        """Configure the execution sandbox backend.

        Args:
            backend: A ``BackendProtocol`` instance or ``BackendFactory``
                callable. Defaults to ``LocalShellBackend``.
            allow_dangerous: Pass through ``allow_dangerous=True`` to a
                ``LocalShellBackend`` (disables blocking of dangerous patterns).
        """
        self._config.sandbox = SandboxConfig(
            backend=backend,
            allow_dangerous=allow_dangerous,
        )
        return self

    # ------------------------------------------------------------------
    # Cost tracking
    # ------------------------------------------------------------------

    def with_cost_tracking(
        self,
        *,
        max_cost_usd: float | None = None,
        budget_usd: float | None = None,
    ) -> AgentBuilder:
        """Enable token cost tracking with optional hard limits.

        Args:
            max_cost_usd: Hard cost limit per session in USD. Agent stops
                when this threshold is exceeded.
            budget_usd: Alias for ``max_cost_usd`` (preferred name).
        """
        self._config.cost = CostConfig(
            enabled=True,
            max_cost_usd=max_cost_usd,
            budget_usd=budget_usd or max_cost_usd,
        )
        return self

    # ------------------------------------------------------------------
    # Observability
    # ------------------------------------------------------------------

    def with_audit_trail(
        self,
        *,
        session_id: str = "",
        advisor_id: str = "",
        checkpointing: bool = False,
    ) -> AgentBuilder:
        """Enable audit trail logging.

        Args:
            session_id: Session identifier for audit records.
            advisor_id: Advisor identifier (for financial advisor use cases).
            checkpointing: Also enable LangGraph checkpointing.
        """
        self._config.observability = ObservabilityConfig(
            audit_trail=True,
            audit_session_id=session_id,
            audit_advisor_id=advisor_id,
            checkpointing=checkpointing,
        )
        return self

    def with_checkpointing(self) -> AgentBuilder:
        """Enable LangGraph session checkpointing."""
        self._config.observability.checkpointing = True
        return self

    def with_citations(self) -> AgentBuilder:
        """Enable citation tracking in agent responses."""
        self._config.observability.citations = True
        return self

    def with_reasoning_chain(self) -> AgentBuilder:
        """Enable visible reasoning chain in agent responses."""
        self._config.observability.reasoning_chain = True
        return self

    # ------------------------------------------------------------------
    # Safety
    # ------------------------------------------------------------------

    def with_human_in_the_loop(self) -> AgentBuilder:
        """Enable human approval gates before sensitive tool calls."""
        self._config.safety.approval_gates = True
        return self

    def with_rbac(self) -> AgentBuilder:
        """Enable role-based access control middleware."""
        self._config.safety.rbac = True
        return self

    def with_dlp(self, *, mode: str = "redact") -> AgentBuilder:
        """Enable data-loss prevention scanning.

        Args:
            mode: "redact" (mask sensitive data) or "block" (reject the message).
        """
        self._config.safety.dlp = True
        self._config.safety.dlp_mode = mode
        return self

    # ------------------------------------------------------------------
    # Multi-agent
    # ------------------------------------------------------------------

    def with_multi_agent(
        self,
        *,
        max_threads: int = 10,
        agent_teams: bool = False,
    ) -> AgentBuilder:
        """Enable parallel agent execution (the ``parallel_tasks`` tool).

        The in-process multi-agent orchestrator was removed in V1; this now
        wires `ParallelAgentsMiddleware`, which lets the agent fan work out to
        concurrent sub-agents via a ``parallel_tasks`` tool.

        Args:
            max_threads: Deprecated / inert — retained for back-compat with
                older configs. Concurrency is no longer capped here.
            agent_teams: Deprecated / inert — the agent-teams middleware was
                removed; retained only so older YAML configs still load.
        """
        self._config.multi_agent = MultiAgentConfig(
            enabled=True,
            max_threads=max_threads,
            agent_teams=agent_teams,
        )
        return self

    # ------------------------------------------------------------------
    # Tools, middleware, skills
    # ------------------------------------------------------------------

    def with_tools(self, *tools: Any) -> AgentBuilder:
        """Add extra tools to the agent.

        Args:
            *tools: LangChain ``BaseTool`` instances, callables, or tool dicts.
        """
        self._config.tools.extend(tools)
        return self

    def with_middleware(self, *middleware: Any) -> AgentBuilder:
        """Add middleware to the agent middleware stack.

        Args:
            *middleware: ``AgentMiddleware`` instances.
        """
        self._config.middleware.extend(middleware)
        return self

    def with_skills(self, *skills: str) -> AgentBuilder:
        """Add named skills to the agent.

        Args:
            *skills: Skill names to load (resolved from the skills registry).
        """
        self._config.skills.extend(skills)
        return self

    def with_mcp(self, *servers: str) -> AgentBuilder:
        """Enable MCP servers by registry ID.

        Args:
            *servers: Server IDs from the MCP registry (e.g. "github", "jira").
        """
        self._config.mcp_servers.extend(servers)
        return self

    def with_subagents(self, *subagents: Any) -> AgentBuilder:
        """Add sub-agents available to the main agent.

        Args:
            *subagents: SubAgent / CompiledSubAgent / AsyncSubAgent instances.
        """
        self._config.subagents.extend(subagents)
        return self

    # ------------------------------------------------------------------
    # Escape hatch
    # ------------------------------------------------------------------

    def with_kwargs(self, **kwargs: Any) -> AgentBuilder:
        """Pass arbitrary keyword arguments directly to ``create_agent()``.

        Use this for parameters not yet exposed through named builder methods.

        Example::

            builder.with_kwargs(enable_voice_io=True, session_name="my-session")
        """
        self._config.extra_kwargs.update(kwargs)
        return self

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------

    def get_config(self) -> AgentConfig:
        """Return the accumulated configuration (for inspection or testing)."""
        return self._config

    def build(self) -> CompiledStateGraph:
        """Compile and return the agent state graph.

        Translates the builder configuration into ``create_agent()`` keyword
        arguments, then calls it.

        Returns:
            Compiled LangGraph ``CompiledStateGraph`` ready to invoke.
        """
        from bog_agents.graph import create_agent

        kwargs: dict[str, Any] = {}

        # Core
        if self._config.model is not None:
            kwargs["model"] = self._config.model
        if self._config.system_prompt:
            kwargs["system_prompt"] = self._config.system_prompt
        if self._config.name is not None:
            kwargs["name"] = self._config.name
        if self._config.debug:
            kwargs["debug"] = True

        # Git / code-quality
        git = self._config.git
        if git.enabled:
            if git.auto_commit:
                kwargs["enable_git_tools"] = True
            if git.code_review:
                kwargs["enable_code_review"] = True
            if git.repo_map:
                kwargs["enable_repo_map"] = True
            if git.auto_lint:
                kwargs["auto_lint"] = True
            if git.auto_test:
                kwargs["auto_test"] = True
                kwargs["test_framework"] = git.test_framework
            if git.worktree:
                kwargs["enable_worktree"] = True
            if git.working_dir is not None:
                kwargs["working_dir"] = git.working_dir

        # Memory
        mem = self._config.memory
        if mem.sources:
            kwargs["memory"] = mem.sources
        if mem.knowledge_graph:
            kwargs["enable_knowledge_graph"] = True

        # Planning
        plan = self._config.planning
        if plan.plan_mode:
            kwargs["enable_plan_mode"] = True
        if plan.effort_level != "medium":
            kwargs["effort_level"] = plan.effort_level
        if plan.architect_model is not None:
            kwargs["architect_model"] = plan.architect_model
        if plan.reviewer_model is not None:
            kwargs["reviewer_model"] = plan.reviewer_model

        # Sandbox
        sbx = self._config.sandbox
        if sbx.backend is not None:
            kwargs["backend"] = sbx.backend
        # Note: ``allow_dangerous`` is consumed at the *backend* layer
        # (e.g. LocalShellBackend), not by ``create_agent`` directly. The
        # builder accepts it for ergonomics but the value is only honored
        # when the caller also constructs the sandbox backend themselves.
        # Forwarding the flag as a top-level kwarg crashes ``create_agent``
        # — drop it here and surface the limitation in the docstring.

        # Cost
        cost = self._config.cost
        if cost.enabled:
            kwargs["enable_cost_tracking"] = True
        if cost.budget_usd is not None:
            kwargs["budget_usd"] = cost.budget_usd

        # Observability
        obs = self._config.observability
        if obs.audit_trail:
            kwargs["enable_audit_trail"] = True
            if obs.audit_session_id:
                kwargs["audit_session_id"] = obs.audit_session_id
            if obs.audit_advisor_id:
                kwargs["audit_advisor_id"] = obs.audit_advisor_id
        if obs.checkpointing:
            kwargs["enable_checkpointing"] = True
        if obs.citations:
            kwargs["enable_citations"] = True
        if obs.reasoning_chain:
            kwargs["enable_reasoning_chain"] = True

        # Safety
        safety = self._config.safety
        if safety.approval_gates:
            kwargs["enable_approval_gates"] = True
        if safety.rbac:
            kwargs["enable_rbac"] = True
        if safety.dlp:
            kwargs["enable_dlp"] = True
            kwargs["dlp_mode"] = safety.dlp_mode
        if safety.hallucination_detection:
            kwargs["enable_hallucination_detection"] = True

        # Collections
        if self._config.tools:
            kwargs["tools"] = list(self._config.tools)

        # Middleware (plus multi-agent). The in-process orchestrator was removed
        # in V1, so an enabled multi-agent config now wires the live
        # ParallelAgentsMiddleware (the `parallel_tasks` tool) instead of the
        # old no-op `enable_multi_agent` flag. max_threads / agent_teams remain
        # on the config schema for back-compat with older YAML but are inert.
        middleware_list = list(self._config.middleware) if self._config.middleware else []
        if self._config.multi_agent.enabled:
            from bog_agents.middleware.parallel_agents import ParallelAgentsMiddleware

            middleware_list.append(ParallelAgentsMiddleware())
        if middleware_list:
            kwargs["middleware"] = middleware_list
        if self._config.skills:
            kwargs["skills"] = list(self._config.skills)
        if self._config.subagents:
            kwargs["subagents"] = list(self._config.subagents)

        # MCP servers — there is no dedicated ``create_agent`` kwarg today,
        # and forwarding ``mcp_servers=...`` crashes the early-validation in
        # ``_resolve_feature_config``. Users wiring MCP must construct the
        # corresponding middleware (``mcp_tools.load_mcp_tools_for_agents``)
        # and pass it via ``with_middleware(...)`` for now.

        # Extra kwargs override everything above
        kwargs.update(self._config.extra_kwargs)

        return create_agent(**kwargs)

    def __repr__(self) -> str:  # noqa: D105
        return f"AgentBuilder(model={self._config.model!r})"
