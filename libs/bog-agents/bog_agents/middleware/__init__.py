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
"""

from bog_agents.middleware.adaptive_context import AdaptiveContextMiddleware
from bog_agents.middleware.agent_replay import AgentReplayMiddleware
from bog_agents.middleware.agent_teams import AgentTeamsMiddleware
from bog_agents.middleware.air_gapped import AirGappedMiddleware
from bog_agents.middleware.approval_gates import ApprovalGatesMiddleware
from bog_agents.middleware.architect import ArchitectMiddleware
from bog_agents.middleware.async_subagents import (
    AsyncSubAgent,
    AsyncSubAgentMiddleware,
)
from bog_agents.middleware.audit_trail import AuditTrailMiddleware
from bog_agents.middleware.auto_quality import AutoQualityMiddleware, detect_project
from bog_agents.middleware.automations import AutomationsMiddleware
from bog_agents.middleware.background_jobs import BackgroundJobsMiddleware, JobRecord, load_all_jobs, load_job, make_job_id, save_job
from bog_agents.middleware.browser_agent import BrowserAgentMiddleware
from bog_agents.middleware.browser_agent_fa import BrowserAgentFAMiddleware
from bog_agents.middleware.checkpointing import CheckpointingMiddleware
from bog_agents.middleware.citations import CitationsMiddleware
from bog_agents.middleware.client_knowledge_base import ClientKnowledgeBaseMiddleware
from bog_agents.middleware.client_reports import ClientReportsMiddleware
from bog_agents.middleware.cloud_sandbox import CloudSandboxMiddleware
from bog_agents.middleware.code_intelligence import CodeIntelligenceMiddleware
from bog_agents.middleware.code_review import CodeReviewMiddleware
from bog_agents.middleware.collaborative_sessions import CollaborativeSessionsMiddleware
from bog_agents.middleware.competitive_intel import CompetitiveIntelMiddleware
from bog_agents.middleware.computer_use import ComputerUseMiddleware
from bog_agents.middleware.context_packing import ContextPackingMiddleware
from bog_agents.middleware.conversation_branch import ConversationBranchMiddleware
from bog_agents.middleware.cost_tracker import CostTrackerMiddleware
from bog_agents.middleware.dashboard import DashboardMiddleware
from bog_agents.middleware.deep_research import DeepResearchMiddleware
from bog_agents.middleware.dlp import DLPMiddleware
from bog_agents.middleware.due_diligence import DueDiligenceMiddleware
from bog_agents.middleware.earnings_analysis import EarningsAnalysisMiddleware
from bog_agents.middleware.enhanced_skills import EnhancedSkillsMiddleware
from bog_agents.middleware.enterprise import EnterpriseMiddleware
from bog_agents.middleware.fact_check import FactCheckMiddleware
from bog_agents.middleware.filesystem import FilesystemMiddleware
from bog_agents.middleware.financial_data import FinancialDataMiddleware
from bog_agents.middleware.firm_deployment import FirmDeploymentMiddleware
from bog_agents.middleware.git_tools import GitToolsMiddleware
from bog_agents.middleware.hallucination_detection import HallucinationDetectionMiddleware
from bog_agents.middleware.hot_reload_skills import HotReloadSkillsMiddleware
from bog_agents.middleware.http_hooks import HttpHooksMiddleware
from bog_agents.middleware.hybrid_search import HybridSearchMiddleware
from bog_agents.middleware.image_input import ImageInputMiddleware
from bog_agents.middleware.image_pdf_input import ImagePdfInputMiddleware
from bog_agents.middleware.intelligent_compaction import CompactionEvent, IntelligentCompactionMiddleware, UsageInfo
from bog_agents.middleware.knowledge_graph import KnowledgeGraphMiddleware
from bog_agents.middleware.langsmith_integration import LangSmithMiddleware
from bog_agents.middleware.lifecycle_hooks import (
    HookEvent,
    LifecycleHooksMiddleware,
)
from bog_agents.middleware.market_sentiment import MarketSentimentMiddleware
from bog_agents.middleware.meeting_prep import MeetingPrepMiddleware
from bog_agents.middleware.memory import MemoryMiddleware
from bog_agents.middleware.messaging_integration import MessagingIntegrationMiddleware
from bog_agents.middleware.model_cascade import ModelCascadeMiddleware
from bog_agents.middleware.model_portfolio import ModelPortfolioMiddleware
from bog_agents.middleware.multi_agent_orchestrator import MultiAgentOrchestratorMiddleware
from bog_agents.middleware.multi_model import MultiModelMiddleware
from bog_agents.middleware.multi_repo import MultiRepoMiddleware, RepoConfig, load_workspace
from bog_agents.middleware.nl_query import NLQueryMiddleware
from bog_agents.middleware.notifications import NotificationsMiddleware
from bog_agents.middleware.offline_mode import OfflineModeMiddleware
from bog_agents.middleware.opensearch_rag import OpenSearchRAGMiddleware
from bog_agents.middleware.parallel_agents import ParallelAgentsMiddleware
from bog_agents.middleware.peer_comparison import PeerComparisonMiddleware
from bog_agents.middleware.plan_mode import PlanModeMiddleware
from bog_agents.middleware.plugin_system import PluginSystemMiddleware
from bog_agents.middleware.portfolio_analysis import PortfolioAnalysisMiddleware
from bog_agents.middleware.pr_management import PRManagementMiddleware
from bog_agents.middleware.rbac import RBACMiddleware
from bog_agents.middleware.reasoning_chain import ReasoningChainMiddleware
from bog_agents.middleware.regulatory_alerts import RegulatoryAlertsMiddleware
from bog_agents.middleware.regulatory_impact import RegulatoryImpactMiddleware
from bog_agents.middleware.repo_map import RepoMapMiddleware
from bog_agents.middleware.result_synthesis import ResultSynthesisMiddleware, ResultSynthesisState
from bog_agents.middleware.rules import RulesMiddleware
from bog_agents.middleware.safe_tools import SafeToolsConfig, is_tool_safe
from bog_agents.middleware.saved_prompts import SavedPromptsMiddleware
from bog_agents.middleware.scenario_engine import ScenarioEngineMiddleware
from bog_agents.middleware.scheduled_reports import ScheduledReportsMiddleware
from bog_agents.middleware.scheduled_runs import ScheduledRunsMiddleware
from bog_agents.middleware.security_audit import SecurityAuditMiddleware
from bog_agents.middleware.self_improving import SelfImprovingMiddleware
from bog_agents.middleware.skills import SkillsMiddleware
from bog_agents.middleware.smart_approvals import SmartApprovalsMiddleware
from bog_agents.middleware.smart_context import SmartContextMiddleware
from bog_agents.middleware.sso_auth import SSOAuthMiddleware
from bog_agents.middleware.subagents import CompiledSubAgent, SubAgent, SubAgentMiddleware
from bog_agents.middleware.summarization import (
    SummarizationMiddleware,
    SummarizationToolMiddleware,
    create_summarization_tool_middleware,
)
from bog_agents.middleware.tax_optimization import TaxOptimizationMiddleware
from bog_agents.middleware.test_generation import TestGenerationMiddleware
from bog_agents.middleware.thinking import ThinkingMiddleware
from bog_agents.middleware.tool_call_parser import (
    ToolCallParserMiddleware,
    parse_tool_calls_from_text,
)
from bog_agents.middleware.version_control import VersionControlMiddleware
from bog_agents.middleware.voice_io import VoiceIOMiddleware
from bog_agents.middleware.worktree import ParallelWorktreeMiddleware, WorktreeMiddleware

__all__ = [
    "AdaptiveContextMiddleware",
    "AgentReplayMiddleware",
    "AgentTeamsMiddleware",
    "AirGappedMiddleware",
    "ApprovalGatesMiddleware",
    "ArchitectMiddleware",
    "AsyncSubAgent",
    "AsyncSubAgentMiddleware",
    "AuditTrailMiddleware",
    "AutoQualityMiddleware",
    "AutomationsMiddleware",
    # New features
    "BackgroundJobsMiddleware",
    "BrowserAgentFAMiddleware",
    "BrowserAgentMiddleware",
    "CheckpointingMiddleware",
    "CitationsMiddleware",
    "ClientKnowledgeBaseMiddleware",
    "ClientReportsMiddleware",
    "CloudSandboxMiddleware",
    "CodeIntelligenceMiddleware",
    "CodeReviewMiddleware",
    "CollaborativeSessionsMiddleware",
    "CompactionEvent",
    "CompetitiveIntelMiddleware",
    "CompiledSubAgent",
    "ComputerUseMiddleware",
    "ContextPackingMiddleware",
    "ConversationBranchMiddleware",
    "CostTrackerMiddleware",
    "DLPMiddleware",
    "DashboardMiddleware",
    "DeepResearchMiddleware",
    "DueDiligenceMiddleware",
    "EarningsAnalysisMiddleware",
    "EnhancedSkillsMiddleware",
    "EnterpriseMiddleware",
    "FactCheckMiddleware",
    "FilesystemMiddleware",
    "FinancialDataMiddleware",
    "FirmDeploymentMiddleware",
    "GitToolsMiddleware",
    "HallucinationDetectionMiddleware",
    "HookEvent",
    "HotReloadSkillsMiddleware",
    "HttpHooksMiddleware",
    "HybridSearchMiddleware",
    "ImageInputMiddleware",
    "ImagePdfInputMiddleware",
    "IntelligentCompactionMiddleware",
    "JobRecord",
    "KnowledgeGraphMiddleware",
    "LangSmithMiddleware",
    "LifecycleHooksMiddleware",
    "MarketSentimentMiddleware",
    "MeetingPrepMiddleware",
    "MemoryMiddleware",
    "MessagingIntegrationMiddleware",
    "ModelCascadeMiddleware",
    "ModelPortfolioMiddleware",
    "MultiAgentOrchestratorMiddleware",
    "MultiModelMiddleware",
    "MultiRepoMiddleware",
    "NLQueryMiddleware",
    "NotificationsMiddleware",
    "OfflineModeMiddleware",
    "OpenSearchRAGMiddleware",
    "PRManagementMiddleware",
    "ParallelAgentsMiddleware",
    "ParallelWorktreeMiddleware",
    "PeerComparisonMiddleware",
    "PlanModeMiddleware",
    "PluginSystemMiddleware",
    "PortfolioAnalysisMiddleware",
    "RBACMiddleware",
    "ReasoningChainMiddleware",
    "RegulatoryAlertsMiddleware",
    "RegulatoryImpactMiddleware",
    "RepoConfig",
    "RepoMapMiddleware",
    "ResultSynthesisMiddleware",
    "ResultSynthesisState",
    "RulesMiddleware",
    "SSOAuthMiddleware",
    "SafeToolsConfig",
    "SavedPromptsMiddleware",
    "ScenarioEngineMiddleware",
    "ScheduledReportsMiddleware",
    "ScheduledRunsMiddleware",
    "SecurityAuditMiddleware",
    "SelfImprovingMiddleware",
    "SkillsMiddleware",
    "SmartApprovalsMiddleware",
    "SmartContextMiddleware",
    "SubAgent",
    "SubAgentMiddleware",
    "SummarizationMiddleware",
    "SummarizationToolMiddleware",
    "TaxOptimizationMiddleware",
    "TestGenerationMiddleware",
    "ThinkingMiddleware",
    "ToolCallParserMiddleware",
    "UsageInfo",
    "VersionControlMiddleware",
    "VoiceIOMiddleware",
    "WorktreeMiddleware",
    "create_summarization_tool_middleware",
    "detect_project",
    "is_tool_safe",
    "load_all_jobs",
    "parse_tool_calls_from_text",
    "load_job",
    "load_workspace",
    "make_job_id",
    "save_job",
]
