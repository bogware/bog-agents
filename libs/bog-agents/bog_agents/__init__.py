"""Bog Agents package.

Every public symbol except `__version__` is loaded lazily — imported on first
attribute access through the module `__getattr__` below. This keeps
`import bog_agents` free of LangGraph / LangChain (v6 SDK-6): the CLI's
entry points, the daemon, and `bog-agents --version` no longer pay the full
graph-module import just to read the version or reach one helper.
"""

from typing import TYPE_CHECKING

from bog_agents._version import __version__

if TYPE_CHECKING:
    # Static-analysis view of the lazily-resolved core exports. Runtime access
    # goes through `__getattr__`; keep this block in sync with the first five
    # `_LAZY_IMPORTS` entries.
    from bog_agents.builder import AgentBuilder as AgentBuilder, AgentConfig as AgentConfig
    from bog_agents.feature_config import FeatureConfig as FeatureConfig
    from bog_agents.graph import DeepAgentState as DeepAgentState, create_agent as create_agent

# Lazy-loaded symbols: maps exported name → (module_path, attribute_name)
_LAZY_IMPORTS: dict[str, tuple[str, str]] = {
    # Core exports (v6 SDK-6: previously eager, pulling in langgraph on import).
    "AgentBuilder": ("bog_agents.builder", "AgentBuilder"),
    "AgentConfig": ("bog_agents.builder", "AgentConfig"),
    "FeatureConfig": ("bog_agents.feature_config", "FeatureConfig"),
    "DeepAgentState": ("bog_agents.graph", "DeepAgentState"),
    "create_agent": ("bog_agents.graph", "create_agent"),
    # deepagents compatibility surface (see bog_agents.deepagents)
    "create_deep_agent": ("bog_agents.deepagents", "create_deep_agent"),
    "create_sub_agent": ("bog_agents.middleware.subagents", "create_sub_agent"),
    "SUBAGENT_RESPONSE_FORMAT_CONFIG_KEY": ("bog_agents.middleware.subagents", "SUBAGENT_RESPONSE_FORMAT_CONFIG_KEY"),
    "SystemPromptConfig": ("bog_agents.graph", "SystemPromptConfig"),
    "FsToolName": ("bog_agents.middleware.filesystem", "FsToolName"),
    "FilesystemPermission": ("bog_agents.middleware.permissions", "FilesystemPermission"),
    "FilesystemPermissionsMiddleware": ("bog_agents.middleware.permissions", "FilesystemPermissionsMiddleware"),
    "RubricMiddleware": ("bog_agents.middleware.rubric", "RubricMiddleware"),
    # Evidence bundles (#29) — proof-of-work packaging for autonomous changes.
    "EvidenceBundle": ("bog_agents.evidence", "EvidenceBundle"),
    "render_evidence_markdown": ("bog_agents.evidence", "render_evidence_markdown"),
    "EvidenceBundleMiddleware": ("bog_agents.middleware.evidence_bundle", "EvidenceBundleMiddleware"),
    # Per-agent cost ledger + runaway caps (#25); CTX-3-fixed pricing lookup.
    "CostLedger": ("bog_agents.cost_ledger", "CostLedger"),
    "RunawayCaps": ("bog_agents.cost_ledger", "RunawayCaps"),
    # Cost certainty (ROADMAP #51): pre-flight estimates + the durable daily spend ledger.
    "CostEstimate": ("bog_agents.cost_ledger", "CostEstimate"),
    "estimate_run_cost": ("bog_agents.cost_ledger", "estimate_run_cost"),
    "SpendLedger": ("bog_agents.spend_ledger", "SpendLedger"),
    "check_ceiling": ("bog_agents.spend_ledger", "check_ceiling"),
    "parse_budget_resume": ("bog_agents.middleware.cost_tracker", "parse_budget_resume"),
    # Proof-ordered diffs (ROADMAP #66).
    "rank_changes": ("bog_agents.diff_ordering", "rank_changes"),
    # Harness overhead audit + named profiles (ROADMAP #54).
    "audit_agent": ("bog_agents.token_audit", "audit_agent"),
    "audit_create_agent": ("bog_agents.token_audit", "audit_create_agent"),
    "TokenAudit": ("bog_agents.token_audit", "TokenAudit"),
    "count_tokens": ("bog_agents.token_audit", "count_tokens"),
    "named_harness_profile": ("bog_agents.profiles.harness.harness_profiles", "named_harness_profile"),
    # Hostile-repo hardening (ROADMAP #49).
    "hardened_git_env": ("bog_agents.git_env", "hardened_git_env"),
    "scan_repo_config": ("bog_agents.git_env", "scan_repo_config"),
    "reorder_unified_diff": ("bog_agents.diff_ordering", "reorder_unified_diff"),
    "split_unified_diff": ("bog_agents.diff_ordering", "split_unified_diff"),
    "price_for_model": ("bog_agents.middleware.cost_tracker", "price_for_model"),
    # Governed agent teams (#21) — claimable task ledger + mailboxes + coordinator.
    "TaskLedger": ("bog_agents.teams", "TaskLedger"),
    "Mailbox": ("bog_agents.teams", "Mailbox"),
    "run_team": ("bog_agents.teams", "run_team"),
    "TeamReport": ("bog_agents.teams", "TeamReport"),
    "HarnessProfile": ("bog_agents.profiles.harness.harness_profiles", "HarnessProfile"),
    "HarnessProfileConfig": ("bog_agents.profiles.harness.harness_profiles", "HarnessProfileConfig"),
    "GeneralPurposeSubagentProfile": ("bog_agents.profiles.harness.harness_profiles", "GeneralPurposeSubagentProfile"),
    "register_harness_profile": ("bog_agents.profiles.harness.harness_profiles", "register_harness_profile"),
    "ProviderProfile": ("bog_agents.profiles.provider.provider_profiles", "ProviderProfile"),
    "register_provider_profile": ("bog_agents.profiles.provider.provider_profiles", "register_provider_profile"),
    "AdaptiveContextMiddleware": ("bog_agents.middleware.adaptive_context", "AdaptiveContextMiddleware"),
    "AgentReplayMiddleware": ("bog_agents.middleware.agent_replay", "AgentReplayMiddleware"),
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
    "CacheBustDetectorMiddleware": ("bog_agents.middleware.cache_diagnostics", "CacheBustDetectorMiddleware"),
    "CheckpointingMiddleware": ("bog_agents.middleware.checkpointing", "CheckpointingMiddleware"),
    "CitationsMiddleware": ("bog_agents.middleware.citations", "CitationsMiddleware"),
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
    "DeferredToolsMiddleware": ("bog_agents.middleware.deferred_tools", "DeferredToolsMiddleware"),
    "EnhancedSkillsMiddleware": ("bog_agents.middleware.enhanced_skills", "EnhancedSkillsMiddleware"),
    "EnterpriseMiddleware": ("bog_agents.middleware.enterprise", "EnterpriseMiddleware"),
    "ExpertRulesMiddleware": ("bog_agents.middleware.expert_rules", "ExpertRulesMiddleware"),
    "FactCheckMiddleware": ("bog_agents.middleware.fact_check", "FactCheckMiddleware"),
    "FilesystemMiddleware": ("bog_agents.middleware.filesystem", "FilesystemMiddleware"),
    "GitToolsMiddleware": ("bog_agents.middleware.git_tools", "GitToolsMiddleware"),
    "HallucinationDetectionMiddleware": ("bog_agents.middleware.hallucination_detection", "HallucinationDetectionMiddleware"),
    "HotReloadSkillsMiddleware": ("bog_agents.middleware.hot_reload_skills", "HotReloadSkillsMiddleware"),
    "HttpHooksMiddleware": ("bog_agents.middleware.http_hooks", "HttpHooksMiddleware"),
    "ImageInputMiddleware": ("bog_agents.middleware.image_input", "ImageInputMiddleware"),
    "ImagePdfInputMiddleware": ("bog_agents.middleware.image_pdf_input", "ImagePdfInputMiddleware"),
    "KnowledgeGraphMiddleware": ("bog_agents.middleware.knowledge_graph", "KnowledgeGraphMiddleware"),
    "LifecycleHooksMiddleware": ("bog_agents.middleware.lifecycle_hooks", "LifecycleHooksMiddleware"),
    "MemoryMiddleware": ("bog_agents.middleware.memory", "MemoryMiddleware"),
    "MessagingIntegrationMiddleware": ("bog_agents.middleware.messaging_integration", "MessagingIntegrationMiddleware"),
    "ModelCascadeMiddleware": ("bog_agents.middleware.model_cascade", "ModelCascadeMiddleware"),
    "ModelPortfolioMiddleware": ("bog_agents.middleware.model_portfolio", "ModelPortfolioMiddleware"),
    "MultiModelMiddleware": ("bog_agents.middleware.multi_model", "MultiModelMiddleware"),
    "NLQueryMiddleware": ("bog_agents.middleware.nl_query", "NLQueryMiddleware"),
    "NotificationsMiddleware": ("bog_agents.middleware.notifications", "NotificationsMiddleware"),
    "OfflineModeMiddleware": ("bog_agents.middleware.offline_mode", "OfflineModeMiddleware"),
    "OpenSearchRAGMiddleware": ("bog_agents.middleware.opensearch_rag", "OpenSearchRAGMiddleware"),
    "PRManagementMiddleware": ("bog_agents.middleware.pr_management", "PRManagementMiddleware"),
    "ParallelAgentsMiddleware": ("bog_agents.middleware.parallel_agents", "ParallelAgentsMiddleware"),
    "PlanModeMiddleware": ("bog_agents.middleware.plan_mode", "PlanModeMiddleware"),
    "PluginSystemMiddleware": ("bog_agents.middleware.plugin_system", "PluginSystemMiddleware"),
    "RBACMiddleware": ("bog_agents.middleware.rbac", "RBACMiddleware"),
    "ReasoningChainMiddleware": ("bog_agents.middleware.reasoning_chain", "ReasoningChainMiddleware"),
    "RepoMapMiddleware": ("bog_agents.middleware.repo_map", "RepoMapMiddleware"),
    "SafeToolsConfig": ("bog_agents.middleware.safe_tools", "SafeToolsConfig"),
    "SavedPromptsMiddleware": ("bog_agents.middleware.saved_prompts", "SavedPromptsMiddleware"),
    "ScheduledReportsMiddleware": ("bog_agents.middleware.scheduled_reports", "ScheduledReportsMiddleware"),
    "ScheduledRunsMiddleware": ("bog_agents.middleware.scheduled_runs", "ScheduledRunsMiddleware"),
    "SecurityAuditMiddleware": ("bog_agents.middleware.security_audit", "SecurityAuditMiddleware"),
    "SelfImprovingMiddleware": ("bog_agents.middleware.self_improving", "SelfImprovingMiddleware"),
    "SmartApprovalsMiddleware": ("bog_agents.middleware.smart_approvals", "SmartApprovalsMiddleware"),
    "SmartContextMiddleware": ("bog_agents.middleware.smart_context", "SmartContextMiddleware"),
    "StreetSweeperMiddleware": ("bog_agents.middleware.street_sweeper", "StreetSweeperMiddleware"),
    "SubAgent": ("bog_agents.middleware.subagents", "SubAgent"),
    "SubAgentMiddleware": ("bog_agents.middleware.subagents", "SubAgentMiddleware"),
    "TestGenerationMiddleware": ("bog_agents.middleware.test_generation", "TestGenerationMiddleware"),
    "VersionControlMiddleware": ("bog_agents.middleware.version_control", "VersionControlMiddleware"),
    "VoiceIOMiddleware": ("bog_agents.middleware.voice_io", "VoiceIOMiddleware"),
    "WorktreeMiddleware": ("bog_agents.middleware.worktree", "WorktreeMiddleware"),
    # --- New features ---
    "BackgroundJobsMiddleware": ("bog_agents.middleware.background_jobs", "BackgroundJobsMiddleware"),
    "HybridSearchMiddleware": ("bog_agents.middleware.hybrid_search", "HybridSearchMiddleware"),
    "IntelligentCompactionMiddleware": ("bog_agents.middleware.intelligent_compaction", "IntelligentCompactionMiddleware"),
    "LangSmithMiddleware": ("bog_agents.middleware.langsmith_integration", "LangSmithMiddleware"),
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
    "__version__",
    *_LAZY_IMPORTS,
]
