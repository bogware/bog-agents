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
        enable_multi_agent: Enable multi-agent orchestration.
        max_agent_threads: Max concurrent agent threads (default 10).
        enable_smart_context: Enable smart context window management.
        max_context_tokens: Token budget for smart context (default 200 000).
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
        enable_meeting_prep: Enable meeting prep middleware.
        enable_enhanced_skills: Enable enhanced skills.
        enhanced_skills_sources: Skill sources for enhanced skills.
        enhanced_skills_cache_dir: Cache directory for enhanced skills.
        enable_saved_prompts: Enable saved prompts.
        saved_prompts_sources: Prompt sources for saved prompts.
        enable_portfolio_analysis: Enable portfolio analysis.
        portfolio_risk_free_rate: Risk-free rate (default 0.05).
        enable_client_reports: Enable client report generation.
        client_reports_firm_name: Firm name for client reports.
        client_reports_advisor_name: Advisor name for client reports.
        enable_deep_research: Enable deep research middleware.
        enable_dlp: Enable data loss prevention.
        dlp_mode: DLP mode (default ``"redact"``).
        enable_version_control: Enable version control middleware.
        enable_scenario_engine: Enable scenario engine.
        enable_tax_optimization: Enable tax optimization middleware.
        enable_nl_query: Enable natural language query middleware.
        enable_peer_comparison: Enable peer comparison middleware.
        enable_code_review: Enable code review middleware.
        enable_financial_data: Enable financial data middleware.
        enable_regulatory_alerts: Enable regulatory alerts.
        enable_model_portfolio: Enable model portfolio middleware.
        enable_knowledge_graph: Enable knowledge graph middleware.
        enable_client_knowledge_base: Enable client knowledge base.
        enable_rbac: Enable role-based access control.
        enable_fact_check: Enable fact-checking middleware.
        enable_approval_gates: Enable approval gates.
        enable_earnings_analysis: Enable earnings analysis.
        enable_regulatory_impact: Enable regulatory impact analysis.
        enable_browser_agent_fa: Enable browser agent (financial advisor).
        enable_agent_teams: Enable agent teams.
        enable_automations: Enable automation middleware.
        enable_image_pdf_input: Enable image/PDF input processing.
        enable_cloud_sandbox: Enable cloud sandbox.
        enable_computer_use: Enable computer-use middleware.
        enable_opensearch_rag: Enable OpenSearch RAG middleware.
        enable_firm_deployment: Enable firm deployment middleware.
        enable_air_gapped: Enable air-gapped mode.
        enable_sso_auth: Enable SSO authentication.
        enable_dashboard: Enable dashboard middleware.
        enable_scheduled_reports: Enable scheduled reports.
        enable_collaborative_sessions: Enable collaborative sessions.
        enable_messaging_integration: Enable messaging integration.
        enable_voice_io: Enable voice I/O middleware.
        enable_due_diligence: Enable due diligence middleware.
        enable_market_sentiment: Enable market sentiment analysis.
        enable_competitive_intel: Enable competitive intelligence.
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
    enable_multi_agent: bool = False
    max_agent_threads: int = 10
    enable_smart_context: bool = False
    max_context_tokens: int = 200000
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

    # Skills & prompts
    enable_meeting_prep: bool = False
    enable_enhanced_skills: bool = False
    enhanced_skills_sources: list[str] | None = None
    enhanced_skills_cache_dir: str | None = None
    enable_saved_prompts: bool = False
    saved_prompts_sources: list[str] | None = None

    # Financial advisor features
    enable_portfolio_analysis: bool = False
    portfolio_risk_free_rate: float = 0.05
    enable_client_reports: bool = False
    client_reports_firm_name: str = ""
    client_reports_advisor_name: str = ""
    enable_deep_research: bool = False
    enable_dlp: bool = False
    dlp_mode: str = "redact"
    enable_version_control: bool = False
    enable_scenario_engine: bool = False
    enable_tax_optimization: bool = False
    enable_nl_query: bool = False
    enable_peer_comparison: bool = False
    enable_code_review: bool = False
    enable_financial_data: bool = False
    enable_regulatory_alerts: bool = False
    enable_model_portfolio: bool = False
    enable_knowledge_graph: bool = False
    enable_client_knowledge_base: bool = False
    enable_rbac: bool = False
    enable_fact_check: bool = False
    enable_approval_gates: bool = False
    enable_earnings_analysis: bool = False
    enable_regulatory_impact: bool = False

    # Batch 4 features
    enable_browser_agent_fa: bool = False
    enable_agent_teams: bool = False
    enable_automations: bool = False
    enable_image_pdf_input: bool = False
    enable_cloud_sandbox: bool = False
    enable_computer_use: bool = False
    enable_opensearch_rag: bool = False
    enable_firm_deployment: bool = False
    enable_air_gapped: bool = False
    enable_sso_auth: bool = False
    enable_dashboard: bool = False
    enable_scheduled_reports: bool = False
    enable_collaborative_sessions: bool = False
    enable_messaging_integration: bool = False
    enable_voice_io: bool = False
    enable_due_diligence: bool = False
    enable_market_sentiment: bool = False
    enable_competitive_intel: bool = False
