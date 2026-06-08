"""Middleware for the Bog Agents agent.

## Overview

The LLM receives tools through two paths:

1. **SDK middleware** (this package) -- tools, system-prompt injection, and
   request interception that any SDK consumer gets automatically.
2. **Consumer-provided tools** -- plain callable functions passed via the
   `tools` parameter to `create_agent()`. The CLI uses this path for
   lightweight, consumer-specific tools.

Both are merged by `create_agent()` into the final tool set the LLM sees.

## Why middleware instead of plain tools?

Middleware subclasses `AgentMiddleware`, overriding its `wrap_model_call()`
hook that **intercepts every LLM request** before it is sent. This lets
middleware:

* **Filter tools dynamically** -- e.g. `FilesystemMiddleware` removes the
  `execute` tool at call-time when the resolved backend doesn't support it.
* **Inject system-prompt context** -- e.g. `MemoryMiddleware` and
  `SkillsMiddleware` inject relevant instructions into the system message on
  every call so the LLM knows how to use the tools they provide.
* **Transform messages** -- e.g. `SummarizationMiddleware` counts tokens,
  truncates old tool arguments, and replaces history with summaries when the
  context window fills up.
* **Maintain cross-turn state** -- middleware can read/write a typed state
  dict that persists across agent turns (e.g. summarization events).

A plain tool function in a `tools=[]` list cannot do any of this -- it is
only invoked *by* the LLM, not *before* the LLM call.

## When to use each path

Use **middleware** when the tool needs to:

* Modify the system prompt or tool list per-call
* Track state across turns
* Be available to all SDK consumers (not just the CLI)

Use a **plain tool** when:

* The function is stateless and self-contained
* No system-prompt or request modification is needed
* The tool is specific to a single consumer (e.g. CLI-only)

## Lazy imports

Middleware classes are loaded lazily via the ``_LAZY_IMPORTS`` map and the
module-level ``__getattr__``. This keeps ``from bog_agents.middleware import
X`` cheap — only ``X`` is imported, not 95 other modules including the
``aiohttp`` / browser_agent / computer_use chains. CLAUDE.md documents this
contract; ``tests/unit_tests/test_lazy_import_health.py`` asserts it.

Fixes P0-B in REVIEW.md.
"""

from __future__ import annotations

import importlib
from typing import Any

# Map of exported name -> (module suffix under ``bog_agents.middleware``,
# attribute name to fetch from that module). The two-tuple shape lets us
# rename / consolidate downstream without losing public API stability.
_LAZY_IMPORTS: dict[str, tuple[str, str]] = {
    "AdaptiveContextMiddleware": ("adaptive_context", "AdaptiveContextMiddleware"),
    "AgentReplayMiddleware": ("agent_replay", "AgentReplayMiddleware"),
    "AirGappedMiddleware": ("air_gapped", "AirGappedMiddleware"),
    "ApprovalGatesMiddleware": ("approval_gates", "ApprovalGatesMiddleware"),
    "ArchitectMiddleware": ("architect", "ArchitectMiddleware"),
    "AsyncSubAgent": ("async_subagents", "AsyncSubAgent"),
    "AsyncSubAgentMiddleware": ("async_subagents", "AsyncSubAgentMiddleware"),
    "AuditTrailMiddleware": ("audit_trail", "AuditTrailMiddleware"),
    "AutoQualityMiddleware": ("auto_quality", "AutoQualityMiddleware"),
    "AutomationsMiddleware": ("automations", "AutomationsMiddleware"),
    "BackgroundJobsMiddleware": ("background_jobs", "BackgroundJobsMiddleware"),
    "BrowserAgentFAMiddleware": ("browser_agent_fa", "BrowserAgentFAMiddleware"),
    "BrowserAgentMiddleware": ("browser_agent", "BrowserAgentMiddleware"),
    "CheckpointingMiddleware": ("checkpointing", "CheckpointingMiddleware"),
    "CitationsMiddleware": ("citations", "CitationsMiddleware"),
    "CloudSandboxMiddleware": ("cloud_sandbox", "CloudSandboxMiddleware"),
    "CodeIntelligenceMiddleware": ("code_intelligence", "CodeIntelligenceMiddleware"),
    "CodeReviewMiddleware": ("code_review", "CodeReviewMiddleware"),
    "CollaborativeSessionsMiddleware": ("collaborative_sessions", "CollaborativeSessionsMiddleware"),
    "CompactionEvent": ("intelligent_compaction", "CompactionEvent"),
    "CompetitiveIntelMiddleware": ("competitive_intel", "CompetitiveIntelMiddleware"),
    "CompiledSubAgent": ("subagents", "CompiledSubAgent"),
    "ComputerUseMiddleware": ("computer_use", "ComputerUseMiddleware"),
    "ContextPackingMiddleware": ("context_packing", "ContextPackingMiddleware"),
    "ConversationBranchMiddleware": ("conversation_branch", "ConversationBranchMiddleware"),
    "CostTrackerMiddleware": ("cost_tracker", "CostTrackerMiddleware"),
    "DLPMiddleware": ("dlp", "DLPMiddleware"),
    "DashboardMiddleware": ("dashboard", "DashboardMiddleware"),
    "DeepResearchMiddleware": ("deep_research", "DeepResearchMiddleware"),
    "EnhancedSkillsMiddleware": ("enhanced_skills", "EnhancedSkillsMiddleware"),
    "EnterpriseMiddleware": ("enterprise", "EnterpriseMiddleware"),
    "ExpertRulesMiddleware": ("expert_rules", "ExpertRulesMiddleware"),
    "FactCheckMiddleware": ("fact_check", "FactCheckMiddleware"),
    "FilesystemMiddleware": ("filesystem", "FilesystemMiddleware"),
    "FilesystemPermission": ("permissions", "FilesystemPermission"),
    "FilesystemPermissionsMiddleware": ("permissions", "FilesystemPermissionsMiddleware"),
    "GitToolsMiddleware": ("git_tools", "GitToolsMiddleware"),
    "HallucinationDetectionMiddleware": ("hallucination_detection", "HallucinationDetectionMiddleware"),
    "HookEvent": ("lifecycle_hooks", "HookEvent"),
    "HotReloadSkillsMiddleware": ("hot_reload_skills", "HotReloadSkillsMiddleware"),
    "HttpHooksMiddleware": ("http_hooks", "HttpHooksMiddleware"),
    "HybridSearchMiddleware": ("hybrid_search", "HybridSearchMiddleware"),
    "ImageInputMiddleware": ("image_input", "ImageInputMiddleware"),
    "ImagePdfInputMiddleware": ("image_pdf_input", "ImagePdfInputMiddleware"),
    "IntelligentCompactionMiddleware": ("intelligent_compaction", "IntelligentCompactionMiddleware"),
    "JobRecord": ("background_jobs", "JobRecord"),
    "KnowledgeGraphMiddleware": ("knowledge_graph", "KnowledgeGraphMiddleware"),
    "LangSmithMiddleware": ("langsmith_integration", "LangSmithMiddleware"),
    "LifecycleHooksMiddleware": ("lifecycle_hooks", "LifecycleHooksMiddleware"),
    "MemoryMiddleware": ("memory", "MemoryMiddleware"),
    "MessagingIntegrationMiddleware": ("messaging_integration", "MessagingIntegrationMiddleware"),
    "ModelCascadeMiddleware": ("model_cascade", "ModelCascadeMiddleware"),
    "ModelPortfolioMiddleware": ("model_portfolio", "ModelPortfolioMiddleware"),
    "MultiModelMiddleware": ("multi_model", "MultiModelMiddleware"),
    "MultiRepoMiddleware": ("multi_repo", "MultiRepoMiddleware"),
    "NLQueryMiddleware": ("nl_query", "NLQueryMiddleware"),
    "NotificationsMiddleware": ("notifications", "NotificationsMiddleware"),
    "OfflineModeMiddleware": ("offline_mode", "OfflineModeMiddleware"),
    "OpenSearchRAGMiddleware": ("opensearch_rag", "OpenSearchRAGMiddleware"),
    "PRManagementMiddleware": ("pr_management", "PRManagementMiddleware"),
    "ParallelAgentsMiddleware": ("parallel_agents", "ParallelAgentsMiddleware"),
    "ParallelWorktreeMiddleware": ("worktree", "ParallelWorktreeMiddleware"),
    "PlanModeMiddleware": ("plan_mode", "PlanModeMiddleware"),
    "PluginSystemMiddleware": ("plugin_system", "PluginSystemMiddleware"),
    "ProviderRetryMiddleware": ("provider_retry", "ProviderRetryMiddleware"),
    "RBACMiddleware": ("rbac", "RBACMiddleware"),
    "ReasoningChainMiddleware": ("reasoning_chain", "ReasoningChainMiddleware"),
    "RepoConfig": ("multi_repo", "RepoConfig"),
    "RepoMapMiddleware": ("repo_map", "RepoMapMiddleware"),
    "ResultSynthesisMiddleware": ("result_synthesis", "ResultSynthesisMiddleware"),
    "ResultSynthesisState": ("result_synthesis", "ResultSynthesisState"),
    "RubricMiddleware": ("rubric", "RubricMiddleware"),
    "RulesMiddleware": ("rules", "RulesMiddleware"),
    "SafeToolsConfig": ("safe_tools", "SafeToolsConfig"),
    "SavedPromptsMiddleware": ("saved_prompts", "SavedPromptsMiddleware"),
    "ScheduledReportsMiddleware": ("scheduled_reports", "ScheduledReportsMiddleware"),
    "ScheduledRunsMiddleware": ("scheduled_runs", "ScheduledRunsMiddleware"),
    "SecurityAuditMiddleware": ("security_audit", "SecurityAuditMiddleware"),
    "SelfImprovingMiddleware": ("self_improving", "SelfImprovingMiddleware"),
    "SkillsMiddleware": ("skills", "SkillsMiddleware"),
    "SmartApprovalsMiddleware": ("smart_approvals", "SmartApprovalsMiddleware"),
    "SmartContextMiddleware": ("smart_context", "SmartContextMiddleware"),
    "SubAgent": ("subagents", "SubAgent"),
    "SubAgentMiddleware": ("subagents", "SubAgentMiddleware"),
    "SummarizationMiddleware": ("summarization", "SummarizationMiddleware"),
    "SummarizationToolMiddleware": ("summarization", "SummarizationToolMiddleware"),
    "TestGenerationMiddleware": ("test_generation", "TestGenerationMiddleware"),
    "ThinkingMiddleware": ("thinking", "ThinkingMiddleware"),
    "ToolCallParserMiddleware": ("tool_call_parser", "ToolCallParserMiddleware"),
    "UsageInfo": ("intelligent_compaction", "UsageInfo"),
    "VersionControlMiddleware": ("version_control", "VersionControlMiddleware"),
    "VoiceIOMiddleware": ("voice_io", "VoiceIOMiddleware"),
    "WorktreeMiddleware": ("worktree", "WorktreeMiddleware"),
    "create_summarization_tool_middleware": ("summarization", "create_summarization_tool_middleware"),
    "detect_project": ("auto_quality", "detect_project"),
    "is_tool_safe": ("safe_tools", "is_tool_safe"),
    "load_all_jobs": ("background_jobs", "load_all_jobs"),
    "load_job": ("background_jobs", "load_job"),
    "load_workspace": ("multi_repo", "load_workspace"),
    "make_job_id": ("background_jobs", "make_job_id"),
    "parse_tool_calls_from_text": ("tool_call_parser", "parse_tool_calls_from_text"),
    "save_job": ("background_jobs", "save_job"),
}


def __getattr__(name: str) -> Any:
    """Resolve a lazy export on first attribute access.

    Called by Python whenever ``bog_agents.middleware.X`` is accessed and
    ``X`` is not already in the module's namespace. We import the backing
    module on demand, fetch the attribute, and cache it on the module for
    future lookups.
    """
    target = _LAZY_IMPORTS.get(name)
    if target is None:
        msg = f"module 'bog_agents.middleware' has no attribute {name!r}"
        raise AttributeError(msg)
    module_suffix, attr_name = target
    module = importlib.import_module(f"bog_agents.middleware.{module_suffix}")
    value = getattr(module, attr_name)
    globals()[name] = value  # cache so subsequent access skips importlib
    return value


def __dir__() -> list[str]:
    """Surface lazy exports for IDE completion and ``dir()``."""
    return sorted(set(globals()) | set(_LAZY_IMPORTS))


# ruff's PLE0605 wants ``__all__`` to be a tuple/list LITERAL — a
# ``sorted()`` call resolves to a list at runtime but the static
# analyzer can't see that. Materialize the sorted keys into a list
# literal at import time so the static check passes without losing
# the alphabetical surface order users see in IDE autocomplete.
__all__ = [*sorted(_LAZY_IMPORTS.keys())]  # see comment above
