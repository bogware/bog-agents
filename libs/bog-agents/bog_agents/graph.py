"""Bog Agents come with planning, filesystem, and subagents."""

import os
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from langchain.agents import create_agent as _langchain_create_agent
from langchain.agents.middleware import HumanInTheLoopMiddleware, InterruptOnConfig, TodoListMiddleware
from langchain.agents.middleware.types import AgentMiddleware
from langchain.agents.structured_output import ResponseFormat
from langchain_anthropic import ChatAnthropic
from langchain_anthropic.middleware import AnthropicPromptCachingMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import SystemMessage
from langchain_core.tools import BaseTool
from langgraph.cache.base import BaseCache
from langgraph.graph.state import CompiledStateGraph
from langgraph.store.base import BaseStore
from langgraph.types import Checkpointer

from bog_agents._models import resolve_model
from bog_agents.backends import StateBackend
from bog_agents.backends.protocol import BackendFactory, BackendProtocol
from bog_agents.feature_config import FeatureConfig
from bog_agents.middleware.filesystem import FilesystemMiddleware
from bog_agents.middleware.memory import MemoryMiddleware
from bog_agents.middleware.patch_tool_calls import PatchToolCallsMiddleware
from bog_agents.middleware.skills import SkillsMiddleware
from bog_agents.middleware.subagents import (
    GENERAL_PURPOSE_SUBAGENT,
    CompiledSubAgent,
    SubAgent,
    SubAgentMiddleware,
)
from bog_agents.middleware.summarization import create_summarization_middleware

BASE_AGENT_PROMPT = """You are a Bog Agents agent, an AI assistant that helps users accomplish tasks using tools. You respond with text and tool calls. The user can see your responses and tool outputs in real time.

## Core Behavior

- Be concise and direct. Don't over-explain unless asked.
- NEVER add unnecessary preamble (\"Sure!\", \"Great question!\", \"I'll now...\").
- Don't say \"I'll now do X\" — just do it.
- If the request is ambiguous, ask questions before acting.
- If asked how to approach something, explain first, then act.

## Professional Objectivity

- Prioritize accuracy over validating the user's beliefs
- Disagree respectfully when the user is incorrect
- Avoid unnecessary superlatives, praise, or emotional validation

## Doing Tasks

When the user asks you to do something:

1. **Understand first** — read relevant files, check existing patterns. Quick but thorough — gather enough evidence to start, then iterate.
2. **Act** — implement the solution. Work quickly but accurately.
3. **Verify** — check your work against what was asked, not against your own output. Your first attempt is rarely correct — iterate.

Keep working until the task is fully complete. Don't stop partway and explain what you would do — just do it. Only yield back to the user when the task is done or you're genuinely blocked.

**When things go wrong:**
- If something fails repeatedly, stop and analyze *why* — don't keep retrying the same approach.
- If you're blocked, tell the user what's wrong and ask for guidance.

## Progress Updates

For longer tasks, provide brief progress updates at reasonable intervals — a concise sentence recapping what you've done and what's next."""  # noqa: E501


def get_default_model() -> ChatAnthropic:
    """Get the default model for bog-agents agents.

    Returns:
        `ChatAnthropic` instance configured with Claude Sonnet 4.6.
    """
    return ChatAnthropic(
        model_name="claude-sonnet-4-6",
    )


def create_agent(  # noqa: C901, PLR0912, PLR0915  # Complex graph assembly logic with many conditional branches
    model: str | BaseChatModel | None = None,
    tools: Sequence[BaseTool | Callable | dict[str, Any]] | None = None,
    *,
    system_prompt: str | SystemMessage | None = None,
    middleware: Sequence[AgentMiddleware] = (),
    subagents: list[SubAgent | CompiledSubAgent] | None = None,
    skills: list[str] | None = None,
    memory: list[str] | None = None,
    response_format: ResponseFormat | None = None,
    context_schema: type[Any] | None = None,
    checkpointer: Checkpointer | None = None,
    store: BaseStore | None = None,
    backend: BackendProtocol | BackendFactory | None = None,
    interrupt_on: dict[str, bool | InterruptOnConfig] | None = None,
    debug: bool = False,
    name: str | None = None,
    cache: BaseCache | None = None,
    features: FeatureConfig | None = None,
    # Individual feature flags (kept for backwards compatibility).
    # When ``features`` is provided, these are ignored.
    enable_git_tools: bool = False,
    enable_repo_map: bool = False,
    enable_checkpointing: bool = False,
    enable_cost_tracking: bool = False,
    enable_plan_mode: bool = False,
    effort_level: str = "medium",
    budget_usd: float | None = None,
    architect_model: str | BaseChatModel | None = None,
    reviewer_model: str | BaseChatModel | None = None,
    auto_lint: bool = False,
    auto_test: bool = False,
    working_dir: str | None = None,
    # New parameters for features #51-75
    enable_worktree: bool = False,
    enable_multi_agent: bool = False,
    max_agent_threads: int = 10,
    enable_smart_context: bool = False,
    max_context_tokens: int = 200000,
    enable_conversation_branching: bool = False,
    enable_image_input: bool = False,
    enable_browser: bool = False,
    allowed_browser_domains: list[str] | None = None,
    enable_pr_management: bool = False,
    enable_test_tools: bool = False,
    test_framework: str = "pytest",
    enable_enterprise: bool = False,
    team_config_path: str | None = None,
    current_role: str = "developer",
    enable_multi_model: bool = False,
    available_models: list[str] | None = None,
    enable_code_intelligence: bool = False,
    enable_plugin_system: bool = False,
    plugins_dir: str | None = None,
    enable_notifications: bool = False,
    session_name: str = "",
    # Financial advisor features
    enable_audit_trail: bool = False,
    audit_session_id: str = "",
    audit_advisor_id: str = "",
    enable_citations: bool = False,
    enable_reasoning_chain: bool = False,
    enable_hallucination_detection: bool = False,
    enable_meeting_prep: bool = False,
    enable_enhanced_skills: bool = False,
    enhanced_skills_sources: list[str] | None = None,
    enhanced_skills_cache_dir: str | None = None,
    enable_saved_prompts: bool = False,
    saved_prompts_sources: list[str] | None = None,
    # Batch 2 financial advisor features
    enable_portfolio_analysis: bool = False,
    portfolio_risk_free_rate: float = 0.05,
    enable_client_reports: bool = False,
    client_reports_firm_name: str = "",
    client_reports_advisor_name: str = "",
    enable_deep_research: bool = False,
    enable_dlp: bool = False,
    dlp_mode: str = "redact",
    enable_version_control: bool = False,
    enable_scenario_engine: bool = False,
    enable_tax_optimization: bool = False,
    enable_nl_query: bool = False,
    enable_peer_comparison: bool = False,
    # Batch 3 financial advisor features
    enable_code_review: bool = False,
    enable_financial_data: bool = False,
    enable_regulatory_alerts: bool = False,
    enable_model_portfolio: bool = False,
    enable_knowledge_graph: bool = False,
    enable_client_knowledge_base: bool = False,
    enable_rbac: bool = False,
    enable_fact_check: bool = False,
    enable_approval_gates: bool = False,
    enable_earnings_analysis: bool = False,
    enable_regulatory_impact: bool = False,
    # Batch 4 (final) features
    enable_browser_agent_fa: bool = False,
    enable_agent_teams: bool = False,
    enable_automations: bool = False,
    enable_image_pdf_input: bool = False,
    enable_cloud_sandbox: bool = False,
    enable_computer_use: bool = False,
    enable_opensearch_rag: bool = False,
    enable_firm_deployment: bool = False,
    enable_air_gapped: bool = False,
    enable_sso_auth: bool = False,
    enable_dashboard: bool = False,
    enable_scheduled_reports: bool = False,
    enable_collaborative_sessions: bool = False,
    enable_messaging_integration: bool = False,
    enable_voice_io: bool = False,
    enable_due_diligence: bool = False,
    enable_market_sentiment: bool = False,
    enable_competitive_intel: bool = False,
) -> CompiledStateGraph:
    """Create a bog-agents agent.

    !!! warning "Bog Agents agents require a LLM that supports tool calling!"

    By default, this agent has access to the following tools:

    - `write_todos`: manage a todo list
    - `ls`, `read_file`, `write_file`, `edit_file`, `glob`, `grep`: file operations
    - `execute`: run shell commands
    - `task`: call subagents

    The `execute` tool allows running shell commands if the backend implements `SandboxBackendProtocol`.
    For non-sandbox backends, the `execute` tool will return an error message.

    Args:
        model: The model to use.

            Defaults to `claude-sonnet-4-6`.

            Use the `provider:model` format (e.g., `openai:gpt-5`) to quickly switch between models.

            If an `openai:` model is used, the agent will use the OpenAI
            Responses API by default. To use OpenAI chat completions instead,
            initialize the model with
            `init_chat_model("openai:...", use_responses_api=False)` and pass
            the initialized model instance here. To disable data retention with
            the Responses API, use
            `init_chat_model("openai:...", use_responses_api=True, store=False, include=["reasoning.encrypted_content"])`
            and pass the initialized model instance here.
        tools: The tools the agent should have access to.

            In addition to custom tools you provide, bog-agents agents include built-in tools for planning,
            file management, and subagent spawning.
        system_prompt: Custom system instructions to prepend before the base bog-agents agent
            prompt.

            If a string, it's concatenated with the base prompt.
        middleware: Additional middleware to apply after the standard middleware stack
            (`TodoListMiddleware`, `FilesystemMiddleware`, `SubAgentMiddleware`,
            `SummarizationMiddleware`, `AnthropicPromptCachingMiddleware`,
            `PatchToolCallsMiddleware`).
        subagents: The subagents to use.

            Each subagent should be a `dict` with the following keys:

            - `name`
            - `description` (used by the main agent to decide whether to call the sub agent)
            - `system_prompt` (used as the system prompt in the subagent)
            - (optional) `tools`
            - (optional) `model` (either a `LanguageModelLike` instance or `dict` settings)
            - (optional) `middleware` (list of `AgentMiddleware`)
        skills: Optional list of skill source paths (e.g., `["/skills/user/", "/skills/project/"]`).

            Paths must be specified using POSIX conventions (forward slashes) and are relative
            to the backend's root. When using `StateBackend` (default), provide skill files via
            `invoke(files={...})`. With `FilesystemBackend`, skills are loaded from disk relative
            to the backend's `root_dir`. Later sources override earlier ones for skills with the
            same name (last one wins).
        memory: Optional list of memory file paths (`AGENTS.md` files) to load
            (e.g., `["/memory/AGENTS.md"]`).

            Display names are automatically derived from paths.

            Memory is loaded at agent startup and added into the system prompt.
        response_format: A structured output response format to use for the agent.
        context_schema: The schema of the bog-agents agent.
        checkpointer: Optional `Checkpointer` for persisting agent state between runs.
        store: Optional store for persistent storage (required if backend uses `StoreBackend`).
        backend: Optional backend for file storage and execution.

            Pass either a `Backend` instance or a callable factory like `lambda rt: StateBackend(rt)`.
            For execution support, use a backend that implements `SandboxBackendProtocol`.
        interrupt_on: Mapping of tool names to interrupt configs.

            Pass to pause agent execution at specified tool calls for human approval or modification.

            Example: `interrupt_on={"edit_file": True}` pauses before every edit.
        debug: Whether to enable debug mode. Passed through to `create_agent`.
        name: The name of the agent. Passed through to `create_agent`.
        cache: The cache to use for the agent. Passed through to `create_agent`.

    Returns:
        A configured bog-agents agent.
    """
    # If a FeatureConfig was provided, use its values for all feature flags.
    # This lets callers pass a single config object instead of 90+ kwargs.
    if features is not None:
        f = features
        enable_git_tools = f.enable_git_tools
        enable_repo_map = f.enable_repo_map
        enable_checkpointing = f.enable_checkpointing
        enable_cost_tracking = f.enable_cost_tracking
        enable_plan_mode = f.enable_plan_mode
        effort_level = f.effort_level
        budget_usd = f.budget_usd
        architect_model = f.architect_model
        reviewer_model = f.reviewer_model
        auto_lint = f.auto_lint
        auto_test = f.auto_test
        working_dir = f.working_dir
        enable_worktree = f.enable_worktree
        enable_multi_agent = f.enable_multi_agent
        max_agent_threads = f.max_agent_threads
        enable_smart_context = f.enable_smart_context
        max_context_tokens = f.max_context_tokens
        enable_conversation_branching = f.enable_conversation_branching
        enable_image_input = f.enable_image_input
        enable_browser = f.enable_browser
        allowed_browser_domains = f.allowed_browser_domains
        enable_pr_management = f.enable_pr_management
        enable_test_tools = f.enable_test_tools
        test_framework = f.test_framework
        enable_enterprise = f.enable_enterprise
        team_config_path = f.team_config_path
        current_role = f.current_role
        enable_multi_model = f.enable_multi_model
        available_models = f.available_models
        enable_code_intelligence = f.enable_code_intelligence
        enable_plugin_system = f.enable_plugin_system
        plugins_dir = f.plugins_dir
        enable_notifications = f.enable_notifications
        session_name = f.session_name
        enable_audit_trail = f.enable_audit_trail
        audit_session_id = f.audit_session_id
        audit_advisor_id = f.audit_advisor_id
        enable_citations = f.enable_citations
        enable_reasoning_chain = f.enable_reasoning_chain
        enable_hallucination_detection = f.enable_hallucination_detection
        enable_meeting_prep = f.enable_meeting_prep
        enable_enhanced_skills = f.enable_enhanced_skills
        enhanced_skills_sources = f.enhanced_skills_sources
        enhanced_skills_cache_dir = f.enhanced_skills_cache_dir
        enable_saved_prompts = f.enable_saved_prompts
        saved_prompts_sources = f.saved_prompts_sources
        enable_portfolio_analysis = f.enable_portfolio_analysis
        portfolio_risk_free_rate = f.portfolio_risk_free_rate
        enable_client_reports = f.enable_client_reports
        client_reports_firm_name = f.client_reports_firm_name
        client_reports_advisor_name = f.client_reports_advisor_name
        enable_deep_research = f.enable_deep_research
        enable_dlp = f.enable_dlp
        dlp_mode = f.dlp_mode
        enable_version_control = f.enable_version_control
        enable_scenario_engine = f.enable_scenario_engine
        enable_tax_optimization = f.enable_tax_optimization
        enable_nl_query = f.enable_nl_query
        enable_peer_comparison = f.enable_peer_comparison
        enable_code_review = f.enable_code_review
        enable_financial_data = f.enable_financial_data
        enable_regulatory_alerts = f.enable_regulatory_alerts
        enable_model_portfolio = f.enable_model_portfolio
        enable_knowledge_graph = f.enable_knowledge_graph
        enable_client_knowledge_base = f.enable_client_knowledge_base
        enable_rbac = f.enable_rbac
        enable_fact_check = f.enable_fact_check
        enable_approval_gates = f.enable_approval_gates
        enable_earnings_analysis = f.enable_earnings_analysis
        enable_regulatory_impact = f.enable_regulatory_impact
        enable_browser_agent_fa = f.enable_browser_agent_fa
        enable_agent_teams = f.enable_agent_teams
        enable_automations = f.enable_automations
        enable_image_pdf_input = f.enable_image_pdf_input
        enable_cloud_sandbox = f.enable_cloud_sandbox
        enable_computer_use = f.enable_computer_use
        enable_opensearch_rag = f.enable_opensearch_rag
        enable_firm_deployment = f.enable_firm_deployment
        enable_air_gapped = f.enable_air_gapped
        enable_sso_auth = f.enable_sso_auth
        enable_dashboard = f.enable_dashboard
        enable_scheduled_reports = f.enable_scheduled_reports
        enable_collaborative_sessions = f.enable_collaborative_sessions
        enable_messaging_integration = f.enable_messaging_integration
        enable_voice_io = f.enable_voice_io
        enable_due_diligence = f.enable_due_diligence
        enable_market_sentiment = f.enable_market_sentiment
        enable_competitive_intel = f.enable_competitive_intel

    if model is None:
        _api_key_vars = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY")
        if not any(os.environ.get(k) for k in _api_key_vars):
            import warnings

            warnings.warn(
                "No API key found (checked ANTHROPIC_API_KEY, OPENAI_API_KEY, GOOGLE_API_KEY). "
                "The default model (Claude Sonnet) requires ANTHROPIC_API_KEY. "
                "Set it before calling agent.invoke().",
                UserWarning,
                stacklevel=2,
            )
        model = get_default_model()
    else:
        model = resolve_model(model)

    backend = backend if backend is not None else (StateBackend)

    # Build general-purpose subagent with default middleware stack
    gp_middleware: list[AgentMiddleware[Any, Any, Any]] = [
        TodoListMiddleware(),
        FilesystemMiddleware(backend=backend),
        create_summarization_middleware(model, backend),
        AnthropicPromptCachingMiddleware(unsupported_model_behavior="ignore"),
        PatchToolCallsMiddleware(),
    ]
    if skills is not None:
        gp_middleware.append(SkillsMiddleware(backend=backend, sources=skills))
    if interrupt_on is not None:
        gp_middleware.append(HumanInTheLoopMiddleware(interrupt_on=interrupt_on))

    general_purpose_spec: SubAgent = {  # ty: ignore[missing-typed-dict-key]
        **GENERAL_PURPOSE_SUBAGENT,
        "model": model,
        "tools": tools or [],
        "middleware": gp_middleware,
    }

    # Process user-provided subagents to fill in defaults for model, tools, and middleware
    processed_subagents: list[SubAgent | CompiledSubAgent] = []
    for spec in subagents or []:
        if "runnable" in spec:
            # CompiledSubAgent - use as-is
            processed_subagents.append(spec)
        else:
            # SubAgent - fill in defaults and prepend base middleware
            subagent_model = spec.get("model", model)
            subagent_model = resolve_model(subagent_model)

            # Build middleware: base stack + skills (if specified) + user's middleware
            subagent_middleware: list[AgentMiddleware[Any, Any, Any]] = [
                TodoListMiddleware(),
                FilesystemMiddleware(backend=backend),
                create_summarization_middleware(subagent_model, backend),
                AnthropicPromptCachingMiddleware(unsupported_model_behavior="ignore"),
                PatchToolCallsMiddleware(),
            ]
            subagent_skills = spec.get("skills")
            if subagent_skills:
                subagent_middleware.append(SkillsMiddleware(backend=backend, sources=subagent_skills))
            subagent_middleware.extend(spec.get("middleware", []))

            processed_spec: SubAgent = {  # ty: ignore[missing-typed-dict-key]
                **spec,
                "model": subagent_model,
                "tools": spec.get("tools", tools or []),
                "middleware": subagent_middleware,
            }
            processed_subagents.append(processed_spec)

    if any(spec["name"] == GENERAL_PURPOSE_SUBAGENT["name"] for spec in processed_subagents):
        # If an agent with general purpose name already exists in subagents, then don't add it
        # This is how you overwrite/configure general purpose subagent
        all_subagents: list[SubAgent | CompiledSubAgent] = processed_subagents
    else:
        # Otherwise - add it!
        all_subagents = [general_purpose_spec, *processed_subagents]

    # Build main agent middleware stack
    agents_middleware: list[AgentMiddleware[Any, Any, Any]] = [
        TodoListMiddleware(),
    ]
    if memory is not None:
        agents_middleware.append(MemoryMiddleware(backend=backend, sources=memory))
    if skills is not None:
        agents_middleware.append(SkillsMiddleware(backend=backend, sources=skills))

    # New feature middleware (#1-50)
    _wd = Path(working_dir) if working_dir else None

    if enable_git_tools:
        from bog_agents.middleware.git_tools import GitToolsMiddleware

        agents_middleware.append(GitToolsMiddleware(working_dir=_wd))

    if enable_repo_map:
        from bog_agents.middleware.repo_map import RepoMapMiddleware

        agents_middleware.append(RepoMapMiddleware(working_dir=_wd))

    if enable_checkpointing:
        from bog_agents.middleware.checkpointing import CheckpointingMiddleware

        agents_middleware.append(CheckpointingMiddleware(working_dir=_wd))

    if enable_cost_tracking or budget_usd is not None:
        from bog_agents._models import get_model_identifier
        from bog_agents.middleware.cost_tracker import CostTrackerMiddleware

        model_name = get_model_identifier(model) or "" if isinstance(model, BaseChatModel) else str(model or "")
        agents_middleware.append(CostTrackerMiddleware(model_name=model_name, budget_usd=budget_usd, effort_level=effort_level))

    if enable_plan_mode:
        from bog_agents.middleware.plan_mode import PlanModeMiddleware

        agents_middleware.append(PlanModeMiddleware(enabled=False))

    if architect_model or reviewer_model:
        from bog_agents.middleware.architect import ArchitectMiddleware

        agents_middleware.append(
            ArchitectMiddleware(
                architect_model=architect_model,
                reviewer_model=reviewer_model,
            )
        )

    if auto_lint or auto_test:
        from bog_agents.middleware.auto_quality import AutoQualityMiddleware

        agents_middleware.append(
            AutoQualityMiddleware(
                working_dir=_wd,
                auto_lint=auto_lint,
                auto_test=auto_test,
            )
        )

    # Feature middleware: worktree, multi-agent, context, etc.
    if enable_worktree:
        from bog_agents.middleware.worktree import WorktreeMiddleware

        agents_middleware.append(WorktreeMiddleware(working_dir=_wd))

    if enable_multi_agent:
        from bog_agents.middleware.multi_agent_orchestrator import MultiAgentOrchestratorMiddleware

        agents_middleware.append(MultiAgentOrchestratorMiddleware(max_threads=max_agent_threads))

    if enable_smart_context:
        from bog_agents.middleware.smart_context import SmartContextMiddleware

        agents_middleware.append(SmartContextMiddleware(working_dir=_wd, max_context_tokens=max_context_tokens))

    if enable_conversation_branching:
        from bog_agents.middleware.conversation_branch import ConversationBranchMiddleware

        agents_middleware.append(ConversationBranchMiddleware(working_dir=_wd))

    if enable_image_input:
        from bog_agents.middleware.image_input import ImageInputMiddleware

        agents_middleware.append(ImageInputMiddleware(working_dir=_wd))

    if enable_browser:
        from bog_agents.middleware.browser_agent import BrowserAgentMiddleware

        agents_middleware.append(BrowserAgentMiddleware(working_dir=_wd, allowed_domains=allowed_browser_domains))

    if enable_pr_management:
        from bog_agents.middleware.pr_management import PRManagementMiddleware

        agents_middleware.append(PRManagementMiddleware(working_dir=_wd))

    if enable_test_tools:
        from bog_agents.middleware.test_generation import TestGenerationMiddleware

        agents_middleware.append(TestGenerationMiddleware(working_dir=_wd, test_framework=test_framework))

    if enable_enterprise:
        from bog_agents.middleware.enterprise import EnterpriseMiddleware

        agents_middleware.append(
            EnterpriseMiddleware(
                working_dir=_wd,
                team_config_path=Path(team_config_path) if team_config_path else None,
                current_role=current_role,
            )
        )

    if enable_multi_model:
        from bog_agents.middleware.multi_model import MultiModelMiddleware

        agents_middleware.append(MultiModelMiddleware(available_models=available_models))

    if enable_code_intelligence:
        from bog_agents.middleware.code_intelligence import CodeIntelligenceMiddleware

        agents_middleware.append(CodeIntelligenceMiddleware(working_dir=_wd))

    if enable_plugin_system:
        from bog_agents.middleware.plugin_system import PluginSystemMiddleware

        agents_middleware.append(PluginSystemMiddleware(plugins_dir=Path(plugins_dir) if plugins_dir else None))

    if enable_notifications:
        from bog_agents.middleware.notifications import NotificationsMiddleware

        agents_middleware.append(NotificationsMiddleware(session_name=session_name))

    # Financial advisor middleware
    if enable_audit_trail:
        from bog_agents.middleware.audit_trail import AuditTrailMiddleware

        agents_middleware.append(AuditTrailMiddleware(session_id=audit_session_id, advisor_id=audit_advisor_id))

    if enable_citations:
        from bog_agents.middleware.citations import CitationsMiddleware

        agents_middleware.append(CitationsMiddleware())

    if enable_reasoning_chain:
        from bog_agents.middleware.reasoning_chain import ReasoningChainMiddleware

        agents_middleware.append(ReasoningChainMiddleware())

    if enable_hallucination_detection:
        from bog_agents.middleware.hallucination_detection import HallucinationDetectionMiddleware

        agents_middleware.append(HallucinationDetectionMiddleware())

    if enable_meeting_prep:
        from bog_agents.middleware.meeting_prep import MeetingPrepMiddleware

        agents_middleware.append(MeetingPrepMiddleware())

    if enable_enhanced_skills and enhanced_skills_sources:
        from bog_agents.middleware.enhanced_skills import EnhancedSkillsMiddleware

        agents_middleware.append(
            EnhancedSkillsMiddleware(
                backend=backend,
                sources=enhanced_skills_sources,
                cache_dir=enhanced_skills_cache_dir,
            )
        )

    if enable_saved_prompts and saved_prompts_sources:
        from bog_agents.middleware.saved_prompts import SavedPromptsMiddleware

        agents_middleware.append(SavedPromptsMiddleware(backend=backend, sources=saved_prompts_sources))

    # Batch 2 financial advisor middleware
    if enable_portfolio_analysis:
        from bog_agents.middleware.portfolio_analysis import PortfolioAnalysisMiddleware

        agents_middleware.append(PortfolioAnalysisMiddleware(risk_free_rate=portfolio_risk_free_rate))

    if enable_client_reports:
        from bog_agents.middleware.client_reports import ClientReportsMiddleware

        agents_middleware.append(ClientReportsMiddleware(firm_name=client_reports_firm_name, advisor_name=client_reports_advisor_name))

    if enable_deep_research:
        from bog_agents.middleware.deep_research import DeepResearchMiddleware

        agents_middleware.append(DeepResearchMiddleware())

    if enable_dlp:
        from bog_agents.middleware.dlp import DLPMiddleware

        agents_middleware.append(DLPMiddleware(mode=dlp_mode))

    if enable_version_control:
        from bog_agents.middleware.version_control import VersionControlMiddleware

        agents_middleware.append(VersionControlMiddleware())

    if enable_scenario_engine:
        from bog_agents.middleware.scenario_engine import ScenarioEngineMiddleware

        agents_middleware.append(ScenarioEngineMiddleware())

    if enable_tax_optimization:
        from bog_agents.middleware.tax_optimization import TaxOptimizationMiddleware

        agents_middleware.append(TaxOptimizationMiddleware())

    if enable_nl_query:
        from bog_agents.middleware.nl_query import NLQueryMiddleware

        agents_middleware.append(NLQueryMiddleware())

    if enable_peer_comparison:
        from bog_agents.middleware.peer_comparison import PeerComparisonMiddleware

        agents_middleware.append(PeerComparisonMiddleware())

    # Batch 3 financial advisor middleware
    if enable_code_review:
        from bog_agents.middleware.code_review import CodeReviewMiddleware

        agents_middleware.append(CodeReviewMiddleware())

    if enable_financial_data:
        from bog_agents.middleware.financial_data import FinancialDataMiddleware

        agents_middleware.append(FinancialDataMiddleware())

    if enable_regulatory_alerts:
        from bog_agents.middleware.regulatory_alerts import RegulatoryAlertsMiddleware

        agents_middleware.append(RegulatoryAlertsMiddleware())

    if enable_model_portfolio:
        from bog_agents.middleware.model_portfolio import ModelPortfolioMiddleware

        agents_middleware.append(ModelPortfolioMiddleware())

    if enable_knowledge_graph:
        from bog_agents.middleware.knowledge_graph import KnowledgeGraphMiddleware

        agents_middleware.append(KnowledgeGraphMiddleware())

    if enable_client_knowledge_base:
        from bog_agents.middleware.client_knowledge_base import ClientKnowledgeBaseMiddleware

        agents_middleware.append(ClientKnowledgeBaseMiddleware())

    if enable_rbac:
        from bog_agents.middleware.rbac import RBACMiddleware

        agents_middleware.append(RBACMiddleware())

    if enable_fact_check:
        from bog_agents.middleware.fact_check import FactCheckMiddleware

        agents_middleware.append(FactCheckMiddleware())

    if enable_approval_gates:
        from bog_agents.middleware.approval_gates import ApprovalGatesMiddleware

        agents_middleware.append(ApprovalGatesMiddleware())

    if enable_earnings_analysis:
        from bog_agents.middleware.earnings_analysis import EarningsAnalysisMiddleware

        agents_middleware.append(EarningsAnalysisMiddleware())

    if enable_regulatory_impact:
        from bog_agents.middleware.regulatory_impact import RegulatoryImpactMiddleware

        agents_middleware.append(RegulatoryImpactMiddleware())

    # Batch 4 (final) middleware
    if enable_browser_agent_fa:
        from bog_agents.middleware.browser_agent_fa import BrowserAgentFAMiddleware

        agents_middleware.append(BrowserAgentFAMiddleware())

    if enable_agent_teams:
        from bog_agents.middleware.agent_teams import AgentTeamsMiddleware

        agents_middleware.append(AgentTeamsMiddleware())

    if enable_automations:
        from bog_agents.middleware.automations import AutomationsMiddleware

        agents_middleware.append(AutomationsMiddleware())

    if enable_image_pdf_input:
        from bog_agents.middleware.image_pdf_input import ImagePdfInputMiddleware

        agents_middleware.append(ImagePdfInputMiddleware())

    if enable_cloud_sandbox:
        from bog_agents.middleware.cloud_sandbox import CloudSandboxMiddleware

        agents_middleware.append(CloudSandboxMiddleware())

    if enable_computer_use:
        from bog_agents.middleware.computer_use import ComputerUseMiddleware

        agents_middleware.append(ComputerUseMiddleware())

    if enable_opensearch_rag:
        from bog_agents.middleware.opensearch_rag import OpenSearchRAGMiddleware

        agents_middleware.append(OpenSearchRAGMiddleware())

    if enable_firm_deployment:
        from bog_agents.middleware.firm_deployment import FirmDeploymentMiddleware

        agents_middleware.append(FirmDeploymentMiddleware())

    if enable_air_gapped:
        from bog_agents.middleware.air_gapped import AirGappedMiddleware

        agents_middleware.append(AirGappedMiddleware())

    if enable_sso_auth:
        from bog_agents.middleware.sso_auth import SSOAuthMiddleware

        agents_middleware.append(SSOAuthMiddleware())

    if enable_dashboard:
        from bog_agents.middleware.dashboard import DashboardMiddleware

        agents_middleware.append(DashboardMiddleware())

    if enable_scheduled_reports:
        from bog_agents.middleware.scheduled_reports import ScheduledReportsMiddleware

        agents_middleware.append(ScheduledReportsMiddleware())

    if enable_collaborative_sessions:
        from bog_agents.middleware.collaborative_sessions import CollaborativeSessionsMiddleware

        agents_middleware.append(CollaborativeSessionsMiddleware())

    if enable_messaging_integration:
        from bog_agents.middleware.messaging_integration import MessagingIntegrationMiddleware

        agents_middleware.append(MessagingIntegrationMiddleware())

    if enable_voice_io:
        from bog_agents.middleware.voice_io import VoiceIOMiddleware

        agents_middleware.append(VoiceIOMiddleware())

    if enable_due_diligence:
        from bog_agents.middleware.due_diligence import DueDiligenceMiddleware

        agents_middleware.append(DueDiligenceMiddleware())

    if enable_market_sentiment:
        from bog_agents.middleware.market_sentiment import MarketSentimentMiddleware

        agents_middleware.append(MarketSentimentMiddleware())

    if enable_competitive_intel:
        from bog_agents.middleware.competitive_intel import CompetitiveIntelMiddleware

        agents_middleware.append(CompetitiveIntelMiddleware())

    agents_middleware.extend(
        [
            FilesystemMiddleware(backend=backend),
            SubAgentMiddleware(
                backend=backend,
                subagents=all_subagents,
            ),
            create_summarization_middleware(model, backend),
            AnthropicPromptCachingMiddleware(unsupported_model_behavior="ignore"),
            PatchToolCallsMiddleware(),
        ]
    )

    if middleware:
        agents_middleware.extend(middleware)
    if interrupt_on is not None:
        agents_middleware.append(HumanInTheLoopMiddleware(interrupt_on=interrupt_on))

    # Combine system_prompt with BASE_AGENT_PROMPT
    if system_prompt is None:
        final_system_prompt: str | SystemMessage = BASE_AGENT_PROMPT
    elif isinstance(system_prompt, SystemMessage):
        final_system_prompt = SystemMessage(content_blocks=[*system_prompt.content_blocks, {"type": "text", "text": f"\n\n{BASE_AGENT_PROMPT}"}])
    else:
        # String: simple concatenation
        final_system_prompt = system_prompt + "\n\n" + BASE_AGENT_PROMPT

    return _langchain_create_agent(
        model,
        system_prompt=final_system_prompt,
        tools=tools,
        middleware=agents_middleware,
        response_format=response_format,
        context_schema=context_schema,
        checkpointer=checkpointer,
        store=store,
        debug=debug,
        name=name,
        cache=cache,
    ).with_config(
        {
            "recursion_limit": 1000,
            "metadata": {
                "ls_integration": "bog-agents",
            },
        }
    )
