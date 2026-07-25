"""Feature configuration for `create_agent()`.

Groups the many optional feature flags into a single dataclass, making it
easier to configure agents without a 100+ parameter function call.

Usage::

    from bog_agents import FeatureConfig, create_agent

    config = FeatureConfig(
        enable_git_tools=True,
        enable_cost_tracking=True,
        budget_usd=5.0,
    )
    agent = create_agent(features=config)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bog_agents.middleware.air_gapped import DataPolicy
    from bog_agents.middleware.rbac import Role


@dataclass
class FeatureConfig:
    """Configuration for optional agent features.

    All fields default to ``False`` / ``None`` so only the features you need
    must be specified.  Pass an instance to `create_agent(features=...)`.

    Attributes:
        working_dir: Shared working directory for file-aware middleware.
        enable_git_tools: Expose git-related tools.
        enable_repo_map: Build and expose a repository map.
        enable_checkpointing: Enable checkpoint-based recovery.
        enable_cost_tracking: Track token/cost usage.
        effort_level: Effort level hint (``"low"`` / ``"medium"`` / ``"high"``).
        budget_usd: Hard budget cap in USD (requires ``enable_cost_tracking``).
        architect_model: Model spec for architect review (enables architect middleware).
        reviewer_model: Model spec for code review (enables architect middleware).
        auto_lint: Automatically lint after edits.
        auto_test: Automatically run tests after edits.
        enable_plan_mode: Enable plan-mode tool.
        enable_worktree: Enable git-worktree isolation.
        enable_parallel_worktree: Enable parallel worktree execution with automatic sub-agents.
        enable_multi_agent: Enable multi-agent orchestration.
        max_agent_threads: Max concurrent agent threads (default 10).
        enable_smart_context: Enable smart context window management.
        max_context_tokens: Token budget for smart context (default 200 000).
        enable_street_sweeper: Continuously prune dead context each turn.
        street_sweeper_aggressive: Enable Tier 2 head/tail truncation of large old outputs.
        street_sweeper_keep_recent: Trailing messages the sweeper never touches.
        enable_conversation_branching: Enable conversation branching.
        enable_image_input: Enable image input processing.
        enable_browser: Enable browser-agent tools.
        allowed_browser_domains: Domain allow-list for browser agent.
        enable_pr_management: Enable PR management tools.
        enable_test_tools: Enable test-generation tools.
        test_framework: Test framework name (default ``"pytest"``).
        enable_enterprise: Enable enterprise team features.
        team_config_path: Path to team configuration file.
        current_role: Current user role for RBAC (default ``"developer"``).
        enable_multi_model: Enable model-switching tools.
        available_models: List of model specs available for switching.
        enable_code_intelligence: Enable code intelligence middleware.
        enable_plugin_system: Enable the plugin system.
        plugins_dir: Directory containing plugins.
        enable_notifications: Enable notification middleware.
        session_name: Session name for notifications.
        enable_audit_trail: Enable audit trail logging.
        audit_session_id: Session ID for audit trail.
        audit_advisor_id: Advisor ID for audit trail.
        enable_citations: Enable citation tracking.
        enable_reasoning_chain: Enable reasoning chain middleware.
        enable_hallucination_detection: Enable hallucination detection.
        enable_enhanced_skills: Enable enhanced skills.
        enhanced_skills_sources: Skill sources for enhanced skills.
        enhanced_skills_cache_dir: Cache directory for enhanced skills.
        enable_saved_prompts: Enable saved prompts.
        saved_prompts_sources: Prompt sources for saved prompts.
        enable_deep_research: Enable deep research middleware.
        enable_dlp: Enable data loss prevention.
        dlp_mode: DLP mode (default ``"redact"``).
        enable_version_control: Enable version control middleware.
        enable_nl_query: Enable natural language query middleware.
        enable_code_review: Enable code review middleware.
        enable_model_portfolio: Enable model portfolio middleware.
        enable_knowledge_graph: Enable knowledge graph middleware.
        enable_rbac: Enable role-based access control.
        enable_fact_check: Enable fact-checking middleware.
        enable_approval_gates: Enable approval gates.
        enable_browser_agent_fa: Enable browser agent (financial advisor).
        enable_automations: Enable automation middleware.
        enable_image_pdf_input: Enable image/PDF input processing.
        enable_cloud_sandbox: Enable cloud sandbox.
        enable_computer_use: Enable computer-use middleware.
        enable_opensearch_rag: Enable OpenSearch RAG middleware.
        enable_air_gapped: Enable air-gapped mode.
        enable_dashboard: Enable dashboard middleware.
        enable_scheduled_reports: Enable scheduled reports.
        enable_collaborative_sessions: Enable collaborative sessions.
        enable_messaging_integration: Enable messaging integration.
        enable_voice_io: Enable voice I/O middleware.
        enable_competitive_intel: Enable competitive intelligence.

    Note (Wave V): the vertical-market middleware flags
    (portfolio_analysis, client_reports, scenario_engine,
    tax_optimization, peer_comparison, financial_data,
    regulatory_alerts, client_knowledge_base, earnings_analysis,
    regulatory_impact, due_diligence, market_sentiment,
    meeting_prep, agent_teams, multi_agent_orchestrator,
    firm_deployment) were removed in V1 because their implementations
    were scaffolds returning placeholder values. Any kwarg with one
    of those names is now treated as a legacy_feature_flag and a
    deprecation warning is emitted.
    """

    # General
    working_dir: str | None = None
    effort_level: str = "medium"
    session_name: str = ""
    current_role: str = "developer"

    # Developer tools
    enable_git_tools: bool = False
    enable_repo_map: bool = False
    enable_checkpointing: bool = False
    enable_cost_tracking: bool = False
    budget_usd: float | None = None
    enable_plan_mode: bool = False
    architect_model: str | None = None
    reviewer_model: str | None = None
    auto_lint: bool = False
    auto_test: bool = False

    # Advanced agent features
    enable_worktree: bool = False
    enable_parallel_worktree: bool = False
    enable_multi_agent: bool = False  # DEPRECATED no-op: orchestrator module removed in V1 (REVIEW.md v2 P1-1)
    max_agent_threads: int = 10
    enable_smart_context: bool = False
    max_context_tokens: int = 200000
    # Street sweeper — continuous, lossless-first context pruning. Disabled by
    # default; runs on every model call to strip dead weight (ANSI/whitespace,
    # duplicate + stale-read tool results, and — when aggressive — head/tail
    # truncation of large old outputs) from the request the model sees, with
    # originals offloaded to the backend for `recall_swept`.
    enable_street_sweeper: bool = False
    street_sweeper_aggressive: bool = True
    street_sweeper_keep_recent: int = 6
    enable_conversation_branching: bool = False
    enable_image_input: bool = False
    enable_browser: bool = False
    allowed_browser_domains: list[str] | None = None
    enable_pr_management: bool = False
    enable_test_tools: bool = False
    test_framework: str = "pytest"
    enable_enterprise: bool = False
    team_config_path: str | None = None
    enable_multi_model: bool = False
    available_models: list[str] | None = None
    enable_code_intelligence: bool = False
    enable_plugin_system: bool = False
    plugins_dir: str | None = None
    enable_notifications: bool = False

    # Audit & compliance
    enable_audit_trail: bool = False
    audit_session_id: str = ""
    audit_advisor_id: str = ""
    enable_citations: bool = False
    enable_reasoning_chain: bool = False
    enable_hallucination_detection: bool = False
    # D-5 umbrella flag — turns on citations + hallucination_detection +
    # fact_check together and adds a system-prompt addendum telling the
    # LLM to register sources, cite claims, verify numbers, and submit
    # uncertain claims for fact-checking. The individual flags are
    # honored too; this just makes "every claim has provenance" a
    # single-knob default. See REVIEW.md D-5.
    enable_provenance_loop: bool = False

    # Skills & prompts
    enable_enhanced_skills: bool = False
    enhanced_skills_sources: list[str] | None = None
    enhanced_skills_cache_dir: str | None = None
    enable_saved_prompts: bool = False
    saved_prompts_sources: list[str] | None = None

    # Vertical-market features — Wave V removed 16 stubs that were
    # never real implementations (portfolio_analysis, client_reports,
    # scenario_engine, tax_optimization, peer_comparison, financial_data,
    # regulatory_alerts, client_knowledge_base, earnings_analysis,
    # regulatory_impact, due_diligence, market_sentiment, meeting_prep,
    # agent_teams, multi_agent_orchestrator, firm_deployment).
    # The flags lived alongside the stubs and produced false positives.
    enable_deep_research: bool = False
    enable_dlp: bool = False
    dlp_mode: str = "redact"
    enable_version_control: bool = False
    enable_nl_query: bool = False
    enable_code_review: bool = False
    enable_model_portfolio: bool = False
    enable_knowledge_graph: bool = False
    enable_rbac: bool = False
    rbac_roles: list[Role] | None = None
    """Operator-owned RBAC role definitions (MW-SAFE-2). Provide alongside
    `rbac_active_role` to run RBAC in operator-pinned mode."""
    rbac_active_role: str = ""
    """The RBAC role to pin. When set, the model cannot redefine/switch roles and
    access is deny-by-default."""
    enable_fact_check: bool = False
    enable_approval_gates: bool = False

    # Batch 4 features
    enable_browser_agent_fa: bool = False
    enable_automations: bool = False
    enable_image_pdf_input: bool = False
    enable_cloud_sandbox: bool = False
    enable_computer_use: bool = False
    enable_opensearch_rag: bool = False
    enable_air_gapped: bool = False
    air_gap_policy: DataPolicy | None = None
    """Operator-owned air-gap data-flow policy (MW-SAFE-1). When `enable_air_gapped`
    is set, the flag path pins a policy (this one, or a default fail-closed
    `DataPolicy`) so the model cannot lift egress restrictions via tools."""
    enable_dashboard: bool = False
    enable_scheduled_reports: bool = False
    enable_collaborative_sessions: bool = False
    enable_messaging_integration: bool = False
    enable_voice_io: bool = False
    enable_competitive_intel: bool = False
    enable_result_synthesis: bool = False
