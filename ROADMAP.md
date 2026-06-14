# ROADMAP.md — bog-agents

> Strategic product roadmap for making bog-agents the best coding agent available
> and keeping a durable competitive edge. Grounded in a four-front competitive
> survey (CLI agents, IDE agents, frameworks/SDKs, autonomous/background agents)
> and a full internal architecture audit. Companion to `REVIEW.md` (which tracks
> the correctness/quality findings this roadmap assumes get fixed first).

**Author's thesis in one line:** bog-agents has already won the *engine* war
(~90 middleware, MCP, skills, memory, street-sweeper, prompt-routing, sandboxes,
evals, daemon, ACP) and is at or ahead of every competitor on raw capability. We
are losing the *platform & trust* war — getting the agent off the local terminal,
making its output provably correct, and making it a composable node in larger
systems. The roadmap is deliberately weighted toward that gap.

---

## Competitive landscape

Status legend: **have** (shipping, competitive) · **partial** (primitive exists,
not packaged into the headline experience) · **missing** (no implementation).
"Leader" = the competitor whose version sets the bar.

### Agent engine / core loop

| Capability | Status | Leader | Notes |
|---|---|---|---|
| Base tool set (fs/shell/plan/subagent) | have | tie | Parity+ via `tools/bundles.py`, base tools. |
| Middleware extensibility | **have (moat)** | bog-agents | ~90 middleware; no competitor matches breadth. |
| Context pruning / long-context working set | have | bog-agents | `street_sweeper.py` (lossless-first, per-call) is ahead of one-shot compaction. |
| MCP **client** (registry, OAuth, trust) | have | tie | `mcp_config_manager.py`, `oauth_mcp.py`, `mcp_registry.py`. |
| MCP **server** (expose agent AS a tool) | missing | OpenAI Codex (`codex mcp-server`) | We can't be delegated to. `serve.py` is the foundation. |
| AGENTS.md cross-tool memory standard | have | tie | `project_memory.py` cascade already reads `AGENTS.md`→`CLAUDE.md`→`.bog-agents.md` (REVIEW T-4). |
| Repo-committed `.prompt.md` → `/command` | partial | GitHub Copilot, Windsurf | We have `saved_prompts`, `recipes.py`, skills; missing the auto-registered in-repo convention. |
| Repo-committed `hooks.json` lifecycle hooks | partial | Copilot | `lifecycle_hooks.py`/`http_hooks.py`/`project_hooks.py` exist; missing the portable committed-file convention. |
| Plan-then-execute with previewed plan | have | tie | `plan_mode.py`, `architect.py`, butcher/jtbd, permission-mode plan. |
| Per-role / per-difficulty model routing | have | bog-agents | operator-mode + `model_cascade`/`model_portfolio`/`multi_model` — ahead of Continue's roles. |

### Retrieval & memory

| Capability | Status | Leader | Notes |
|---|---|---|---|
| Keyword/ripgrep + symbol repo-map | have | tie | `repo_map.py`, `code_intelligence.py`. |
| Persistent **semantic** codebase index (`@codebase`) | partial | Cursor, Continue | `hybrid_search.py` does embedding search w/ `.bog-agents/embeddings.json` cache, but it's not an always-on, incrementally-maintained index surfaced as `@codebase`. |
| Agent-**written** auto-memories | partial | Windsurf (Cascade Memories) | `memory.py` injects + stores, but the *agent proactively writes durable memories from observed work* loop is missing. |
| Living codebase wiki / onboarding doc | partial | Devin (DeepWiki) | We have ephemeral repo-map; no persisted browsable architecture wiki artifact. |
| Knowledge graph | have | bog-agents | `knowledge_graph.py` exceeds most competitors. |

### Multi-agent orchestration

| Capability | Status | Leader | Notes |
|---|---|---|---|
| Parent→child delegation (`task` tool) | have | tie | `subagents.py`. |
| Peer-to-peer **handoff / swarm** (sticky active agent) | partial | OpenAI Agents SDK, langgraph-swarm, AG2 | We only do parent→child; no lateral control transfer with sticky active-agent memory. **#1 framework gap.** |
| Parallel agents + worktrees + synthesis | have | bog-agents | `parallel_agents.py`, `worktree.py`, `result_synthesis.py`, butcher. |
| Per-child **isolated cloud VM** + structured-output contract | partial | Devin, Cursor | Children run in *local* worktrees; no per-child cloud isolation, no typed output schema per child. |
| Unified parallel **control surface** (Mission Control) | partial | Cursor, Zed | `dashboard.py` exists; no TUI control-room to launch/monitor/diff N runs. |

### Autonomy & verification (the 2026 battleground)

| Capability | Status | Leader | Notes |
|---|---|---|---|
| Local auto-quality (lint/test after edits) | have | tie | `auto_quality.py`. |
| **CI-aware self-fix loop** (read my PR's CI logs, push fix until green) | **missing** | Devin, Copilot | The single clearest "runs while you sleep" gap. |
| **Evidence-bearing self-verification** (record demo/screenshots/logs → PR) | partial | Cursor, Devin | `computer_use.py`/`browser_agent.py` exist; no artifact-capture-and-attach pipeline. |
| Inbound conversational PR loop (comment → resume *same* session) | partial | Devin (sleeping-session wake), Copilot | Daemon emits PRs but pipelines are one-shot; no comment→resume-session. |
| Self-review own diff before human (Devin Review) | partial | Devin, CodeRabbit | `code_review.py`/`security_audit.py`/`rubric.py` exist; not wired into one pre-submit gate. |
| Spec→plan→tasks→implement persisted pipeline | have | tie | jtbd + butcher are this family, ahead on verify. |
| Declarative per-run sandbox env + firewall allowlist + snapshots | partial | Copilot (`copilot-setup-steps`), Devin | `cloud_sandbox.py`/daytona exist; no declarative setup file or egress allowlist. |
| Reusable event/label-triggered playbooks + auto-recalled knowledge | partial | Devin (Playbooks), OpenHands (microagents) | `skills`/`saved_prompts`/`automations` scattered; not event-wired to daemon. |

### Platform & distribution (where the leaders pulled ahead)

| Capability | Status | Leader | Notes |
|---|---|---|---|
| Cloud / web-hosted async runner | partial | Claude Code web, Codex cloud, Amp | `remote.py`/LangGraph Cloud + daemon pieces exist; no first-party hosted surface or `apply`-back. |
| Mobile continuation | missing | Claude Code, Amp | No mobile surface. |
| Shareable thread permalinks + `/export` transcript | partial | Amp (signature), Claude Code | `/export` exists as **TraceFile** (signed, content-addressed); no *web permalink* / share-by-URL. |
| Team collaboration (leaderboards, shared usage) | missing | Amp | No org/team layer. |
| First-party GitHub **Action/App** (PR review, @-mention to act) | partial | Claude Code, Codex | `/pr-review` + `pr_management.py` are local-only; no CI entry point. |
| Native IDE diff overlay + inline accept/reject | partial | Claude Code, Cline, Cursor | VS Code ext is v0.1.0 thin webview; no diff overlay, no JetBrains. **Weakest surface.** |
| Real-time "watch it work" stream (per-tool timing) | partial | Devin, Copilot | `dashboard.py`/`audit_trail.py`/`notifications.py`; no unified live stream for background runs. |

### Framework / SDK DX

| Capability | Status | Leader | Notes |
|---|---|---|---|
| Structured output, HITL, streaming, checkpointing | have | tie | LangGraph passthrough + `HumanInTheLoopMiddleware`. |
| Safety/guardrail middleware depth | **have (moat)** | bog-agents | SafeTools config, DLP, RBAC, ApprovalGates, Hallucination, FactCheck, ExpertRules. |
| Evals as a first-class importable SDK module | partial | Pydantic Evals, Mastra | harbor is a *benchmark harness*; `rubric.py` is runtime; no `bog_agents.evals` (datasets+scorers+CI). |
| Output guardrails as a single fail-fast tripwire façade | partial | OpenAI Agents SDK | Capability exists scattered; no one declarative I/O-guardrail abstraction. |
| Typed dependency injection (`deps_type`) | missing | Pydantic-AI | Ad-hoc context via ToolRuntime; no typed deps surface for SDK consumers. |
| Crash-proof durable execution (Temporal/DBOS/Prefect) | partial | Pydantic-AI | LangGraph checkpointer ≠ replayable durable execution for long-horizon daemon runs. |
| A2A cross-framework protocol endpoint | missing | Pydantic-AI, Google ADK | `serve.py` + ACP exist; no A2A. |
| Deferred / long-running tools as a primitive | partial | Pydantic-AI, OpenAI Agents SDK | HITL interrupt + background jobs cover most; no clean "tool returns deferred, resume on callback". |

**Bottom line:** Of ~40 capabilities surveyed, bog-agents is **have** on ~17,
**partial** on ~18, **missing** on ~5. Almost every gap is *packaging an existing
primitive into a headline experience* rather than greenfield research. The five
true *missing* items — MCP-server, agent-written auto-memories' write loop,
CI-aware self-fix, typed deps, A2A — are all small-to-medium and high-leverage.

---

## Killer features

Ranked by `impact × leverage ÷ effort`, with ties broken toward items that build
a moat (hard for competitors to copy because they exploit our middleware depth).

Strategic axes: **AUT** = autonomy · **MA** = multi-agent · **ENT** = enterprise ·
**UX** = UX/reliability · **PLAT** = platform/distribution.

---

### 1. CI-aware self-fix loop ("ship it while you sleep")

- **Problem:** The agent opens a PR, CI goes red, and the human has to notice,
  read the Actions logs, and re-prompt. This breaks the core promise of unattended
  long-horizon work — the agent claims "done" but can't verify against the team's
  real gate.
- **What to build:** A `CIWatchMiddleware` + daemon trigger that, after the agent
  pushes a branch/PR, polls GitHub Actions (or generic CI) status, ingests failing
  job logs via the GitHub API, diagnoses, and pushes a fix commit to the *same*
  branch — looping until green or a budget (max iterations / wall-clock / token
  cap) trips. Reuse `pr_management.py` for the GitHub surface, `auto_quality.py`'s
  diagnose→fix shape, `provider_retry.py`'s budget pattern, and the daemon's
  `github-comment` dispatch target for status updates. Add a `ci_status` /
  `ci_logs` tool bundle so the loop is also usable interactively.
- **Inspired by:** Devin (CI status + job logs in review chat), Copilot coding
  agent (runs in Actions, branch-protection-gated), Cursor Bugbot / CodeRabbit Autofix.
- **Axis:** AUT · **Impact:** 5 · **Effort:** M · **Deps:** GitHub App auth (#9), budget primitive (have).

### 2. Evidence-bearing self-verification (proof-of-work artifacts)

- **Problem:** "The agent says it works" is not reviewable. A human still has to
  pull the branch and run it. This is the #1 trust blocker for long-horizon output.
- **What to build:** A `VerificationEvidenceMiddleware` that, on task completion,
  drives the change through a capture pipeline — run the test suite and save the
  log, optionally launch the app and use `browser_agent.py`/`computer_use.py` to
  navigate the affected UI, capturing a screen recording + screenshots — then
  bundles artifacts into `.bog-agents/evidence/<run-id>/` and attaches them to the
  PR (via `pr_management.py`) or the headless result. Gate behind a verify recipe
  so it only fires on tasks that touch runnable surfaces.
- **Inspired by:** Cursor cloud agents (video demos + screenshots on PR), Devin
  (recorded test runs), CodeRabbit/Qodo proof-of-work norms.
- **Axis:** AUT + UX · **Impact:** 5 · **Effort:** M · **Deps:** browser/computer-use (have), PR surface (have).

### 3. Self-review-my-own-diff gate (pre-human Devin Review)

- **Problem:** PRs go to humans with avoidable bugs, duplicated code, and security
  smells the agent already has the tools to catch — it just never runs them on its
  own diff before submitting.
- **What to build:** A `SelfReviewMiddleware` (or a `/self-review` recipe) that, on
  pre-submit, clusters the diff into logical change groups, fans the existing
  reviewers — `code_review.py`, `security_audit.py`, `rubric.py`,
  `hallucination_detection.py`, `fact_check.py` — across the diff, and feeds
  findings back into a bounded fix loop. Output a structured review summary that
  rides along on the PR. This is **pure orchestration over middleware we already
  own** — the moat is that nobody else has five reviewers to compose.
- **Inspired by:** Devin Review, Qodo, CodeRabbit, Cursor Bugbot.
- **Axis:** AUT + UX · **Impact:** 4 · **Effort:** S · **Deps:** none (composes existing).

### 4. Expose bog-agents AS an MCP server (`bog-agents mcp-server`)

- **Problem:** We're a strong MCP *client* but an island — no other agent or
  orchestrator can delegate a whole coding task to us. We can't be a node in the
  multi-agent meshes forming in 2026.
- **What to build:** A `bog-agents mcp-server` subcommand that wraps the compiled
  graph behind an MCP stdio/SSE server exposing a small tool surface (`run_task`,
  `resume_task`, `get_status`) on top of the existing `serve.py` `/invoke` +
  `/stream`. Map MCP tool calls onto graph invocations; thread permission mode and
  HITL through. Tiny effort, large reach (Claude Desktop, Cursor, Zed, Copilot can
  all delegate to us).
- **Inspired by:** OpenAI Codex CLI (`codex mcp-server`).
- **Axis:** PLAT + MA · **Impact:** 4 · **Effort:** S · **Deps:** `serve.py` (have).

### 5. Always-on semantic `@codebase` index

- **Problem:** ripgrep + symbol repo-map misses semantically-related code that
  doesn't share keywords ("find the auth flow" when nothing is named `auth`).
  Cursor reports ~12.5% agent-accuracy lift from semantic retrieval — this is the
  biggest retrieval-quality lever the IDE agents have.
- **What to build:** Promote `hybrid_search.py` (already does embedding search with
  an incremental `.bog-agents/embeddings.json` cache) to an always-on,
  file-watcher-maintained index. Add a true `@codebase` mention in `mentions.py`
  that returns ranked semantic chunks (vs today's keyword `@search`), and an
  internal `codebase_search` tool the agent calls autonomously. Incremental
  re-index on file change reuses the daemon's file-change trigger plumbing.
- **Inspired by:** Cursor (@codebase vector index), Continue (@codebase provider), Windsurf.
- **Axis:** UX + AUT · **Impact:** 5 · **Effort:** M · **Deps:** `hybrid_search.py`, `mentions.py` (have).

### 6. Peer-to-peer handoffs / swarm with sticky active-agent

- **Problem:** We only do parent→child delegation (`task` tool). The dominant
  2025-2026 multi-agent pattern is *lateral* handoff — control and active-agent
  state move to a peer specialist, who keeps it for the next turn. OpenAI reports
  ~40% lower latency and one fewer LLM call/turn vs a supervisor middleman.
- **What to build:** A `HandoffMiddleware` + `handoff(to_agent, summary, context)`
  tool that transfers control to a peer agent and persists the "last active agent"
  in LangGraph state so the next turn resumes with that specialist. Build on
  `subagents.py` and langgraph-swarm patterns; expose a declarative roster of named
  specialist agents (reviewer, debugger, refactorer) each with its own
  tools/prompt. Composes with `result_synthesis.py` for fan-in.
- **Inspired by:** OpenAI Agents SDK (handoffs), langgraph-swarm, AG2 Swarm, CrewAI delegation.
- **Axis:** MA · **Impact:** 4 · **Effort:** M · **Deps:** subagent infra (have).

### 7. First-party GitHub Action + App ("@bog-agents, fix this issue")

- **Problem:** The async-in-CI motion (assign an issue → get a draft PR; @-mention
  on a PR → agent iterates) is now table stakes among leaders, and we have the
  *logic* (`/pr-review`, `pr_management.py`, github-issue-autofix) but **no CI
  entry point**.
- **What to build:** A maintained `bog-agents-action` (composite GitHub Action) +
  a GitHub App that: (a) runs PR review on `pull_request`, (b) on issue assignment
  / `@bog-agents` comment, spins an ephemeral run that opens a draft PR, and (c)
  — combined with #8 — resumes the *same* durable session when reviewers comment.
  Packages existing commands; the daemon's `github-comment` target already exists.
- **Inspired by:** Claude Code GitHub Action/App, Codex GitHub integration, Sweep, OpenHands resolver.
- **Axis:** PLAT + AUT · **Impact:** 5 · **Effort:** M · **Deps:** GitHub App auth, #8 for the iterate loop.

### 8. Durable, resumable sessions ("sleeping sessions wake on comment")

- **Problem:** Daemon pipelines are one-shot. A reviewer comment can't continue
  an existing run — it restarts from scratch, losing context and cost. Devin's
  "sleeping session auto-wakes on PR-comment retrigger" is the inbound half of the
  conversational PR loop.
- **What to build:** Persist each background/daemon run as a resumable LangGraph
  thread keyed to its PR/issue. Add a daemon webhook handler that, on a PR/issue
  comment mentioning the agent, looks up the thread and resumes it with the new
  message. Pairs an optional **durable-execution backend** (Temporal/DBOS/Prefect
  wrapper, à la Pydantic-AI) so long-horizon runs survive process restarts — a
  differentiator we currently have no answer for.
- **Inspired by:** Devin (sleeping sessions), Copilot (iterate-on-PR), Pydantic-AI (durable execution).
- **Axis:** AUT + ENT · **Impact:** 4 · **Effort:** L · **Deps:** daemon (have), checkpointer (have).

### 9. `bog_agents.evals` — evals as a first-class SDK primitive

- **Problem:** SDK consumers can't score *their own* agent against a dataset with
  reusable scorers in CI. harbor is a benchmark harness, not an importable eval
  API — and "evals as a headline primitive" is what teams use to gate releases.
- **What to build:** A cohesive `bog_agents.evals` module: `Dataset` of cases,
  composable `Scorer`s (LLM-as-judge, rule-based, statistical) built from the raw
  materials we own — `rubric.py` grader, `hallucination_detection.py`,
  `fact_check.py` — a `run_evals()` API, CI integration, and LangSmith viz via
  `langsmith_integration.py`. Reuse harbor's runner under the hood.
- **Inspired by:** Pydantic Evals, Mastra scorers, CrewAI `crew.test()`, OpenAI Agents SDK.
- **Axis:** ENT + UX · **Impact:** 4 · **Effort:** M · **Deps:** rubric/factcheck (have), harbor (have).

### 10. Native IDE diff overlay + inline accept/reject (VS Code v1, then JetBrains)

- **Problem:** Our VS Code extension is v0.1.0 — a thin webview chat that shells
  out to the CLI. No native diff overlay, no inline accept/reject, no JetBrains.
  This is the most *visible* place bog-agents does it worse than Cline/Cursor/Claude Code.
- **What to build:** Upgrade the extension to render agent edits as native VS Code
  diffs (`vscode.diff` / `TextEditorDecorationType`) with per-hunk accept/reject,
  feed editor selection as context automatically, and stream agent activity into
  the panel. Drive it over **ACP** (we already ship `libs/acp`) instead of shelling
  the binary — which simultaneously gets us Zed/JetBrains via the same protocol.
  ACP-first is the leverage move: one protocol, three editors.
- **Inspired by:** Claude Code (VS Code diff overlay, JetBrains bridge), Cline/Roo (rich diff + per-action approval).
- **Axis:** UX + PLAT · **Impact:** 5 · **Effort:** L · **Deps:** `libs/acp` (have), ACP multi-session fix (REVIEW P1).

### 11. Mission Control — TUI control-room for parallel/background runs

- **Problem:** We have the parallel-agent *plumbing* (`parallel_agents.py`,
  `worktree.py`, `result_synthesis.py`, `dashboard.py`, async subagents) but no
  single pane to launch N runs, watch each one's files/decisions/timing live, and
  compare/merge results. Today it's plumbing, not a product.
- **What to build:** A Textual "Mission Control" screen: a grid of active agents
  (local parallel, butcher slices, daemon/background, future cloud), each showing
  current tool call + timing + files touched, with launch/stop/diff/merge actions.
  Backed by a unified run-event stream (also powers #12). This turns our deepest
  orchestration advantage into something users can actually see.
- **Inspired by:** Cursor Mission Control (8 parallel agents), Zed multi-agent threads.
- **Axis:** MA + UX · **Impact:** 4 · **Effort:** L · **Deps:** #12 event stream, existing parallel infra.

### 12. Live "watch it work" run stream (web + Slack + TUI)

- **Problem:** Unattended long-horizon runs are only observable *after the fact*
  via audit logs. Leaders stream every step/tool-call with timing while it runs.
- **What to build:** A unified, real-time run-event stream emitting per-tool-call
  start/end/timing/intermediate-commit/status events, consumable by the TUI
  (powers #11), the daemon's Slack/webhook targets, and a minimal web view (powers
  the cloud surface #15). Generalize `dashboard.py`/`notifications.py`/
  `audit_trail.py` onto one event bus.
- **Inspired by:** Devin ("Watch Devin Work"), Copilot live session logs, Cursor async progress.
- **Axis:** UX + PLAT · **Impact:** 3 · **Effort:** M · **Deps:** none (refactor existing observability).

### 13. Agent-written auto-memories (Cascade-style learning loop)

- **Problem:** Users re-explain project conventions and gotchas every session.
  `memory.py` injects and stores, but the agent never *proactively decides* what's
  worth remembering from observed work.
- **What to build:** Extend `memory.py` with an `after_model` reflection step that
  detects durable facts (conventions, decisions, gotchas, fix patterns), proposes
  them, and — with a confirmation policy (auto / ask / off) — writes
  workspace-scoped memories auto-injected on later sessions. Distinct from
  user-authored rules; tag provenance so users can audit/prune.
- **Inspired by:** Windsurf Cascade Memories, Devin Knowledge (auto-recalled per-repo).
- **Axis:** UX + AUT · **Impact:** 4 · **Effort:** M · **Deps:** `memory.py` (have).

### 14. Repo-committed `.prompt.md` → auto-registered `/command` + event-triggered playbooks

- **Problem:** Teams want shareable, committed-in-repo workflow recipes that show
  up as slash commands and can auto-fire on repo events — Devin Playbooks +
  Copilot `.prompt.md`. We have the parts (`saved_prompts`, `recipes.py`,
  `automations`, skills) but no unified convention.
- **What to build:** A loader that auto-registers `.bog-agents/prompts/*.prompt.md`
  (Markdown + YAML frontmatter) as `/`-commands discoverable in
  `widgets/autocomplete.py`, plus daemon wiring so a playbook can be label/comment
  triggered (e.g. `Bug` label → `/triage-bug`). Mostly convention + loader.
- **Inspired by:** Copilot `.prompt.md`, Windsurf Workflows, Devin Playbooks, OpenHands microagents.
- **Axis:** UX + ENT · **Impact:** 3 · **Effort:** S · **Deps:** command registry (have), daemon (have).

### 15. Cloud / web async runner with `apply`-back and mobile monitoring

- **Problem:** The biggest 2025-2026 platform shift: fire-and-forget tasks in a
  managed cloud container you steer from a browser/phone and pull back local. We
  have `remote.py` (LangGraph Cloud submit), `cloud_sandbox.py`, daytona, and the
  daemon — the pieces — but no assembled first-party hosted surface.
- **What to build:** Assemble a "submit task → managed sandbox runs it → monitor
  via #12 web stream → `bog-agents apply <run-id>` pulls the diff/PR local"
  workflow. Phase 1: orchestrate over the daemon + a sandbox provider with a thin
  web monitor. Phase 2: a mobile-friendly read/steer view. This is the headline
  platform play and the heaviest lift — sequenced as a moonshot, built on #8/#12.
- **Inspired by:** Claude Code on the web + mobile, Codex cloud (`codex apply`), Amp (control from web, continue from mobile), Cursor Background Agents.
- **Axis:** PLAT + AUT · **Impact:** 5 · **Effort:** L · **Deps:** #8 durable sessions, #12 stream, sandbox env (#16).

### 16. Declarative per-run sandbox env + network egress allowlist + snapshots

- **Problem:** Safe unattended cloud execution needs reproducible, pre-provisioned
  environments and *bounded network access* — nobody is watching egress at 3am.
  We have sandbox primitives but no declarative setup file or firewall allowlist.
- **What to build:** A `.bog-agents/sandbox.toml` (preinstall steps, runner size,
  network allowlist) consumed by `cloud_sandbox.py`/daytona, reusable snapshots so
  setup isn't repeated, and egress-violation reporting surfaced on the run/PR.
  Critical safety substrate for #15. Also closes the daemon's `virtual_mode=False`
  guardrail gap flagged in REVIEW.
- **Inspired by:** Copilot `copilot-setup-steps` + firewall allowlist, Devin snapshots, OpenHands per-task Docker.
- **Axis:** ENT + AUT · **Impact:** 3 · **Effort:** M · **Deps:** `cloud_sandbox.py` (have).

### 17. Shareable thread permalinks + team layer

- **Problem:** A CLI session is a solo artifact. Amp's signature wedge is the
  shareable thread URL (review, teach, cross-reference) + team leaderboards. We
  ship signed portable **TraceFiles** (`/export`) but no web permalink / share link.
- **What to build:** A `/share` command that uploads the (already content-addressed,
  signed) TraceFile to a hosted endpoint and returns a permalink with a read-only
  web transcript viewer; layer optional org/team usage + leaderboards on top.
  Leverages the TraceFile work we already have — share is the missing *hosting*.
- **Inspired by:** Amp (share thread by URL + leaderboards), Claude Code web share links.
- **Axis:** PLAT + ENT · **Impact:** 3 · **Effort:** M · **Deps:** TraceFile export (have), hosted endpoint (new).

### 18. Typed dependency injection + output-guardrail tripwire façade (SDK DX)

- **Problem:** Two Pydantic-AI / OpenAI-Agents-SDK DX selling points we lack: a
  typed `deps` container injected into tools, and a single declarative I/O guardrail
  abstraction with fail-fast tripwire semantics. We have the guardrail *capability*
  scattered across safety middleware but no ergonomic façade.
- **What to build:** (a) A thin `deps_type` wrapper over `create_agent`'s context
  giving statically-typed dependency injection into custom tools/system prompts.
  (b) A `guardrails=[...]` parameter that composes input/output validators (wrapping
  DLP/Hallucination/FactCheck/ExpertRules) with parallel fail-fast tripwire
  semantics. Both are consolidation/API-surface wins over existing capability.
- **Inspired by:** Pydantic-AI (`deps_type`, output validators), OpenAI Agents SDK (parallel guardrails), AG2 DI.
- **Axis:** ENT + UX · **Impact:** 3 · **Effort:** M · **Deps:** existing safety middleware (have).

### 19. Per-child isolated cloud fan-out with structured-output contracts (butcher v2)

- **Problem:** Our butcher/parallel children run *sequentially in local worktrees*
  with no typed output. Devin proved 100k-item fan-out by running each child in its
  own isolated cloud VM returning structured output (Nubank). This is the scale
  ceiling on our coordinator pattern.
- **What to build:** Upgrade butcher/`parallel_agents.py` so slices can run in
  *parallel*, each in an isolated sandbox (reusing #16), each returning a typed
  structured-output schema the coordinator merges/conflict-resolves. Turns butcher
  into a true large-scale coordinator/fan-out.
- **Inspired by:** Devin managed-Devins fan-out, Cursor 10-50 parallel agents best-of-n.
- **Axis:** MA + AUT · **Impact:** 3 · **Effort:** L · **Deps:** #16 sandbox env, butcher (have).

### 20. Living codebase wiki + A2A endpoint (companion wins)

- **Problem:** Two clean additive wins: (a) a persisted, browsable architecture
  wiki (diagrams + summaries + source links) that doubles as agent grounding and
  reviewer docs (DeepWiki); (b) an A2A protocol endpoint so other frameworks'
  agents can discover/invoke us in multi-vendor meshes.
- **What to build:** (a) A `wiki` generator over `repo_map.py`/`code_intelligence.py`
  that persists a navigable `.bog-agents/wiki/` (kept fresh via file-change
  triggers), used as task grounding. (b) An A2A server on top of `serve.py`
  (companion to the MCP-server #4 and ACP we already ship).
- **Inspired by:** Devin DeepWiki; Pydantic-AI / Google ADK A2A.
- **Axis:** UX (wiki) + PLAT (A2A) · **Impact:** 2 · **Effort:** M · **Deps:** repo-map (have), `serve.py` (have).

---

## Sequenced plan

> **Gate before Wave 1:** the `REVIEW.md` P0/P1 correctness fixes are prerequisites,
> not optional — several killer features stand directly on the affected code.
> Specifically: the parametrized every-middleware fake-turn CI gate (so new
> middleware ship safe), the Memory↔PromptCaching ordering bug, the
> daytona/langgraph lockfile break, the ACP multi-session state-bleed (blocks #10),
> daemon `virtual_mode`/HITL hardening (blocks #16/#19), CI coverage for
> acp/harbor/partners, and a Dependabot config. Build on a clean foundation.

### Wave 1 — Quick wins (weeks, mostly composing what we own)

**Goal: bank visible autonomy/trust wins and platform reach with small effort.**

- **#3 Self-review-my-own-diff gate** (S) — pure orchestration over 5 reviewers we own; instant PR-quality lift; the moat is our reviewer depth.
- **#4 MCP server** (S) — tiny lift over `serve.py`, makes us delegatable across the whole MCP ecosystem.
- **#14 `.prompt.md` → `/command` + playbooks** (S) — convention + loader; team-shareable; rides the AGENTS.md standardization we already adopted.
- **#1 CI-aware self-fix loop** (M) — the headline "runs while you sleep" gap; #3 makes its fixes higher quality.
- **#5 always-on `@codebase` index** (M) — biggest retrieval-accuracy lever; `hybrid_search.py` is 70% there.
- **#9 `bog_agents.evals`** (M) — packages rubric/factcheck/harbor into the primitive teams gate releases on.

*Rationale:* every Wave 1 item reuses existing middleware/infra, so effort is
packaging not research. #3+#1 together immediately make our autonomous output
*trustworthy*, which is the precondition for everything in Waves 2-3.

### Wave 2 — Differentiators (the trust + orchestration moat)

**Goal: turn capability breadth into experiences competitors can't cheaply copy.**

- **#2 Evidence-bearing self-verification** (M) — "shows it works" beats "says it works"; exploits our browser/computer-use + reviewer stack.
- **#7 GitHub Action/App** (M) — async-in-CI is table stakes; packages local PR logic into a CI entry point.
- **#6 Peer-to-peer handoffs/swarm** (M) — closes the #1 framework gap; the dominant 2026 multi-agent pattern.
- **#13 Agent-written auto-memories** (M) — compounding context advantage; reduces re-explanation every session.
- **#12 Live run stream** (M) — observability substrate that unlocks #11 and #15.
- **#18 Typed deps + guardrail tripwire façade** (M) — SDK DX parity with Pydantic-AI; consolidation, not new capability.
- **#16 Declarative sandbox env + egress allowlist** (M) — safety substrate for safe unattended cloud runs; also fixes the daemon guardrail gap.

*Rationale:* Wave 2 is where the "verify-centric philosophy" (jtbd/butcher already
embody it) becomes a visible product line. #12 and #16 are deliberately here as
load-bearing substrate for the moonshots.

### Wave 3 — Moonshots (platform plays + scale)

**Goal: get the agent off the local terminal and to true large-scale autonomy.**

- **#10 Native IDE diff overlay (ACP-first → VS Code + JetBrains + Zed)** (L) — fixes our weakest surface; ACP-first gets three editors from one protocol.
- **#8 Durable resumable sessions (sleeping-session wake)** (L) — inbound conversational PR loop + crash-proof long-horizon runs.
- **#11 Mission Control TUI** (L) — turns our parallel-orchestration plumbing into a product (needs #12).
- **#15 Cloud/web/mobile async runner with `apply`-back** (L) — the biggest platform shift among leaders; built on #8 + #12 + #16.
- **#17 Shareable permalinks + team layer** (M) — org-adoption wedge on top of our existing signed TraceFiles.
- **#19 Per-child isolated cloud fan-out (butcher v2)** (L) — raises the scale ceiling on our coordinator pattern (needs #16).
- **#20 Living wiki + A2A endpoint** (M) — companion grounding + interop wins.

*Rationale:* these require new hosted infrastructure and editor surfaces, so they
come last — but each is explicitly architected on Wave 1/2 substrate (#8, #12,
#16) so the moonshots are assembly, not greenfield.

---

## What makes us win

**The durable advantage is depth-as-a-platform, expressed as trust.**

1. **Nobody else has ~90 composable middleware.** Every competitor that wants
   evidence-bearing verification, five-reviewer self-review, swarm handoffs, typed
   guardrails, and CI self-fix has to *build each one*. We have to *compose* them.
   Our highest-ranked Wave 1/2 features (#3, #2, #9, #18) are almost entirely
   orchestration over capability we already own — a structural cost advantage on
   exactly the features that matter most in 2026.

2. **We bet on verification early and the industry validated it.** jtbd's outcome
   verification and butcher's per-slice verify→retry→escalate ladder already embody
   the lesson every competitor is now learning the hard way: *a loop is only as
   trustworthy as its ability to check its own work.* The roadmap doubles down —
   CI-aware self-fix (#1), evidence artifacts (#2), self-review gate (#3) — so
   "bog-agents output you can trust unattended" becomes the brand, not a feature.

3. **Provider- and editor-agnostic by construction.** BYO-key across every
   provider (incl. Bedrock SigV4 *and* bearer-token), ACP for cross-editor reach,
   MCP client today and MCP/A2A server next. We don't depend on one model lab's
   roadmap or one editor's surface — we ride *all* of them. As the ecosystem
   fragments into multi-vendor agent meshes, the framework that is a *participant
   everywhere* (MCP + A2A + ACP) beats the one that is a walled product.

4. **The street-sweeper + 1M-context strategy means we don't fight the
   context war on competitors' terms.** Lossless-first per-call pruning lets us
   ingest large working sets without aggressive lossy compaction, and it composes
   cleanly with summarization and prompt-caching — an engineering invariant
   competitors don't have.

**The honest risk** — and why the roadmap is weighted the way it is — is that all
of this depth is currently *trapped on the local terminal*. The leaders pulled
ahead in 2025-2026 not on agent quality but on *distribution*: cloud/web/mobile
runners, shareable threads, native IDE UX, CI entry points. So we win by pairing
our irreplaceable engine depth with the three platform moves that get it off the
laptop — **GitHub Action + durable resumable sessions + ACP-native IDE UX, then
the hosted cloud runner.** Depth is the moat; distribution is the unlock; trust is
the wedge that makes both matter.
