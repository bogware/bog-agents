"""Bog Agents package.

Middleware classes are loaded lazily — they are only imported when first accessed.
This keeps ``import bog_agents`` fast.
"""

from bog_agents._version import __version__
from bog_agents.builder import AgentBuilder, AgentConfig
from bog_agents.feature_config import FeatureConfig
from bog_agents.graph import create_agent

# Lazy-loaded symbols: maps exported name → (module_path, attribute_name)
_LAZY_IMPORTS: dict[str, tuple[str, str]] = {
    "AdaptiveContextMiddleware": ("bog_agents.middleware.adaptive_context", "AdaptiveContextMiddleware"),
    "AgentReplayMiddleware": ("bog_agents.middleware.agent_replay", "AgentReplayMiddleware"),
    "AgentTeamsMiddleware": ("bog_agents.middleware.agent_teams", "AgentTeamsMiddleware"),
    "AirGappedMiddleware": ("bog_agents.middleware.air_gapped", "AirGappedMiddleware"),
    "AsyncSubAgent": ("bog_agents.middleware.async_subagents", "AsyncSubAgent"),
    "AsyncSubAgentMiddleware": ("bog_agents.middleware.async_subagents", "AsyncSubAgentMiddleware"),
    "ApprovalGatesMiddleware": ("bog_agents.middleware.approval_gates", "ApprovalGatesMiddleware"),
    "ArchitectMiddleware": ("bog_agents.middleware.architect", "ArchitectMiddleware"),
    "AuditTrailMiddleware": ("bog_agents.middleware.audit_trail", "AuditTrailMiddleware"),
    "AutoQualityMiddleware": ("bog_agents.middleware.auto_quality", "AutoQualityMiddleware"),
    "AutomationsMiddleware": ("bog_agents.middleware.automations", "AutomationsMiddleware"),
    "BrowserAgentFAMiddleware": ("bog_agents.middleware.browser_agent_fa", "BrowserAgentFAMiddleware"),
    "BrowserAgentMiddleware": ("bog_agents.middleware.browser_agent", "BrowserAgentMiddleware"),
    "CheckpointingMiddleware": ("bog_agents.middleware.checkpointing", "CheckpointingMiddleware"),
    "CitationsMiddleware": ("bog_agents.middleware.citations", "CitationsMiddleware"),
    "ClientKnowledgeBaseMiddleware": ("bog_agents.middleware.client_knowledge_base", "ClientKnowledgeBaseMiddleware"),
    "ClientReportsMiddleware": ("bog_agents.middleware.client_reports", "ClientReportsMiddleware"),
    "CloudSandboxMiddleware": ("bog_agents.middleware.cloud_sandbox", "CloudSandboxMiddleware"),
    "CodeIntelligenceMiddleware": ("bog_agents.middleware.code_intelligence", "CodeIntelligenceMiddleware"),
    "CodeReviewMiddleware": ("bog_agents.middleware.code_review", "CodeReviewMiddleware"),
    "CollaborativeSessionsMiddleware": ("bog_agents.middleware.collaborative_sessions", "CollaborativeSessionsMiddleware"),
    "CompetitiveIntelMiddleware": ("bog_agents.middleware.competitive_intel", "CompetitiveIntelMiddleware"),
    "CompiledSubAgent": ("bog_agents.middleware.subagents", "CompiledSubAgent"),
    "ComputerUseMiddleware": ("bog_agents.middleware.computer_use", "ComputerUseMiddleware"),
    "ContextPackingMiddleware": ("bog_agents.middleware.context_packing", "ContextPackingMiddleware"),
    "ConversationBranchMiddleware": ("bog_agents.middleware.conversation_branch", "ConversationBranchMiddleware"),
    "CostTrackerMiddleware": ("bog_agents.middleware.cost_tracker", "CostTrackerMiddleware"),
    "DLPMiddleware": ("bog_agents.middleware.dlp", "DLPMiddleware"),
    "DashboardMiddleware": ("bog_agents.middleware.dashboard", "DashboardMiddleware"),
    "DeepResearchMiddleware": ("bog_agents.middleware.deep_research", "DeepResearchMiddleware"),
    "DueDiligenceMiddleware": ("bog_agents.middleware.due_diligence", "DueDiligenceMiddleware"),
    "EarningsAnalysisMiddleware": ("bog_agents.middleware.earnings_analysis", "EarningsAnalysisMiddleware"),
    "EnhancedSkillsMiddleware": ("bog_agents.middleware.enhanced_skills", "EnhancedSkillsMiddleware"),
    "EnterpriseMiddleware": ("bog_agents.middleware.enterprise", "EnterpriseMiddleware"),
    "FactCheckMiddleware": ("bog_agents.middleware.fact_check", "FactCheckMiddleware"),
    "FilesystemMiddleware": ("bog_agents.middleware.filesystem", "FilesystemMiddleware"),
    "FinancialDataMiddleware": ("bog_agents.middleware.financial_data", "FinancialDataMiddleware"),
    "FirmDeploymentMiddleware": ("bog_agents.middleware.firm_deployment", "FirmDeploymentMiddleware"),
    "GitToolsMiddleware": ("bog_agents.middleware.git_tools", "GitToolsMiddleware"),
    "HallucinationDetectionMiddleware": ("bog_agents.middleware.hallucination_detection", "HallucinationDetectionMiddleware"),
    "HotReloadSkillsMiddleware": ("bog_agents.middleware.hot_reload_skills", "HotReloadSkillsMiddleware"),
    "HttpHooksMiddleware": ("bog_agents.middleware.http_hooks", "HttpHooksMiddleware"),
    "ImageInputMiddleware": ("bog_agents.middleware.image_input", "ImageInputMiddleware"),
    "ImagePdfInputMiddleware": ("bog_agents.middleware.image_pdf_input", "ImagePdfInputMiddleware"),
    "KnowledgeGraphMiddleware": ("bog_agents.middleware.knowledge_graph", "KnowledgeGraphMiddleware"),
    "LifecycleHooksMiddleware": ("bog_agents.middleware.lifecycle_hooks", "LifecycleHooksMiddleware"),
    "MarketSentimentMiddleware": ("bog_agents.middleware.market_sentiment", "MarketSentimentMiddleware"),
    "MeetingPrepMiddleware": ("bog_agents.middleware.meeting_prep", "MeetingPrepMiddleware"),
    "MemoryMiddleware": ("bog_agents.middleware.memory", "MemoryMiddleware"),
    "MessagingIntegrationMiddleware": ("bog_agents.middleware.messaging_integration", "MessagingIntegrationMiddleware"),
    "ModelCascadeMiddleware": ("bog_agents.middleware.model_cascade", "ModelCascadeMiddleware"),
    "ModelPortfolioMiddleware": ("bog_agents.middleware.model_portfolio", "ModelPortfolioMiddleware"),
    "MultiAgentOrchestratorMiddleware": ("bog_agents.middleware.multi_agent_orchestrator", "MultiAgentOrchestratorMiddleware"),
    "MultiModelMiddleware": ("bog_agents.middleware.multi_model", "MultiModelMiddleware"),
    "NLQueryMiddleware": ("bog_agents.middleware.nl_query", "NLQueryMiddleware"),
    "NotificationsMiddleware": ("bog_agents.middleware.notifications", "NotificationsMiddleware"),
    "OfflineModeMiddleware": ("bog_agents.middleware.offline_mode", "OfflineModeMiddleware"),
    "OpenSearchRAGMiddleware": ("bog_agents.middleware.opensearch_rag", "OpenSearchRAGMiddleware"),
    "PRManagementMiddleware": ("bog_agents.middleware.pr_management", "PRManagementMiddleware"),
    "ParallelAgentsMiddleware": ("bog_agents.middleware.parallel_agents", "ParallelAgentsMiddleware"),
    "PeerComparisonMiddleware": ("bog_agents.middleware.peer_comparison", "PeerComparisonMiddleware"),
    "PlanModeMiddleware": ("bog_agents.middleware.plan_mode", "PlanModeMiddleware"),
    "PluginSystemMiddleware": ("bog_agents.middleware.plugin_system", "PluginSystemMiddleware"),
    "PortfolioAnalysisMiddleware": ("bog_agents.middleware.portfolio_analysis", "PortfolioAnalysisMiddleware"),
    "RBACMiddleware": ("bog_agents.middleware.rbac", "RBACMiddleware"),
    "ReasoningChainMiddleware": ("bog_agents.middleware.reasoning_chain", "ReasoningChainMiddleware"),
    "RegulatoryAlertsMiddleware": ("bog_agents.middleware.regulatory_alerts", "RegulatoryAlertsMiddleware"),
    "RegulatoryImpactMiddleware": ("bog_agents.middleware.regulatory_impact", "RegulatoryImpactMiddleware"),
    "RepoMapMiddleware": ("bog_agents.middleware.repo_map", "RepoMapMiddleware"),
    "SSOAuthMiddleware": ("bog_agents.middleware.sso_auth", "SSOAuthMiddleware"),
    "SafeToolsConfig": ("bog_agents.middleware.safe_tools", "SafeToolsConfig"),
    "SavedPromptsMiddleware": ("bog_agents.middleware.saved_prompts", "SavedPromptsMiddleware"),
    "ScenarioEngineMiddleware": ("bog_agents.middleware.scenario_engine", "ScenarioEngineMiddleware"),
    "ScheduledReportsMiddleware": ("bog_agents.middleware.scheduled_reports", "ScheduledReportsMiddleware"),
    "ScheduledRunsMiddleware": ("bog_agents.middleware.scheduled_runs", "ScheduledRunsMiddleware"),
    "SecurityAuditMiddleware": ("bog_agents.middleware.security_audit", "SecurityAuditMiddleware"),
    "SelfImprovingMiddleware": ("bog_agents.middleware.self_improving", "SelfImprovingMiddleware"),
    "SmartApprovalsMiddleware": ("bog_agents.middleware.smart_approvals", "SmartApprovalsMiddleware"),
    "SmartContextMiddleware": ("bog_agents.middleware.smart_context", "SmartContextMiddleware"),
    "SubAgent": ("bog_agents.middleware.subagents", "SubAgent"),
    "SubAgentMiddleware": ("bog_agents.middleware.subagents", "SubAgentMiddleware"),
    "TaxOptimizationMiddleware": ("bog_agents.middleware.tax_optimization", "TaxOptimizationMiddleware"),
    "TestGenerationMiddleware": ("bog_agents.middleware.test_generation", "TestGenerationMiddleware"),
    "VersionControlMiddleware": ("bog_agents.middleware.version_control", "VersionControlMiddleware"),
    "VoiceIOMiddleware": ("bog_agents.middleware.voice_io", "VoiceIOMiddleware"),
    "WorktreeMiddleware": ("bog_agents.middleware.worktree", "WorktreeMiddleware"),
    # --- New features ---
    "BackgroundJobsMiddleware": ("bog_agents.middleware.background_jobs", "BackgroundJobsMiddleware"),
    "HybridSearchMiddleware": ("bog_agents.middleware.hybrid_search", "HybridSearchMiddleware"),
    "IntelligentCompactionMiddleware": ("bog_agents.middleware.intelligent_compaction", "IntelligentCompactionMiddleware"),
    "MultiRepoMiddleware": ("bog_agents.middleware.multi_repo", "MultiRepoMiddleware"),
    "ParallelWorktreeMiddleware": ("bog_agents.middleware.worktree", "ParallelWorktreeMiddleware"),
    "RulesMiddleware": ("bog_agents.middleware.rules", "RulesMiddleware"),
    "ThinkingMiddleware": ("bog_agents.middleware.thinking", "ThinkingMiddleware"),
}


def __getattr__(name: str) -> object:
    """Lazy-load middleware classes on first access."""
    if name in _LAZY_IMPORTS:
        module_path, attr_name = _LAZY_IMPORTS[name]
        import importlib

        module = importlib.import_module(module_path)
        value = getattr(module, attr_name)
        # Cache in module namespace so subsequent accesses are fast
        globals()[name] = value
        return value
    msg = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(msg)


__all__ = [  # noqa: PLE0604
    "AgentBuilder",
    "AgentConfig",
    "FeatureConfig",
    "__version__",
    "create_agent",
    *_LAZY_IMPORTS,
]
