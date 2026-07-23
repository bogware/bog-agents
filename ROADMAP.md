# ROADMAP.md — bog-agents

> Strategic product roadmap for making bog-agents the best coding agent available
> and keeping a durable competitive edge. Grounded in a four-front competitive
> survey (CLI agents, IDE agents, frameworks/SDKs, autonomous/background agents)
> and a full internal architecture audit. Companion to `REVIEW.md` (which tracks
> the correctness/quality findings this roadmap assumes get fixed first).
>
> **Refreshed 2026-07-21** (REVIEW v4 cycle): the original 20 killer features are
> now score-carded below (4 shipped / 8 partial / 8 not-started), and a second
> generation — "Killer features v2" — was added from a fresh live four-front
> survey with per-idea novelty checks against the code. The original feature
> write-ups (§1–20) are kept verbatim as the reference spec for the partials.

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

### Killer features v1 scorecard (verified against code, 2026-07-21)

| # | Feature | Status | What exists / what's missing |
|---|---|---|---|
| 1 | CI-aware self-fix loop | **partial** | `/ci-fix` (gh-backed status + failing logs + fix prompt) shipped; missing the autonomous half — no CI-failed daemon trigger, no push-until-green budget loop. → completed by **v2 #30**. |
| 2 | Evidence self-verification | **partial** | `bog-agents verify` + jtbd outcome verification exist; no `VerificationEvidenceMiddleware`, no evidence bundle attached to PRs. → completed by **v2 #29**. |
| 3 | Self-review-own-diff gate | **shipped** | `self_review_controller.py` (five lenses, SHIP/FIX-FIRST verdict, `--fix` loop) + `/self-review`. Minor gap: docstring claims a headless subcommand that isn't registered. |
| 4 | MCP server mode | **shipped** | `mcp_server.py` stdio server (`run_task` with thread continuity, `get_info`) + `bog-agents mcp-server`. |
| 5 | Semantic `@codebase` index | **partial** | `@codebase` mention with on-demand incremental embedding index + hybrid search shipped; missing the always-on file-watcher-maintained index. |
| 6 | P2P handoffs / swarm | **not-started** | Sub-agent infra remains strictly hierarchical. → superseded by **v2 #21** (agent teams). |
| 7 | GitHub Action + App | **not-started** | `action.yml` is a run-the-CLI action; no @-mention handler, no issue-assignment entry point. → folded into **v2 #30**. |
| 8 | Durable resumable sessions | **not-started** | Daemon runs are one-shot; no comment→resume, no durable-execution backend. → sharpened by **v2 #33/#39**. |
| 9 | `bog_agents.evals` | **shipped** | `bog_agents/evals/` (Case/Dataset/Scorer/run_evals/EvalReport + `assert_pass_rate`; Contains/ExactMatch/Regex/LLMJudge scorers). |
| 10 | IDE diff overlay | **not-started** | Extension unchanged (thin webview, no diff/ACP). → v2 #28 covers the TUI half; the extension needs the ACP-client rebuild. |
| 11 | Mission Control TUI | **partial** | `/dashboard` live multi-agent grid shipped (watch half); missing launch/stop/diff/merge actions (control half). |
| 12 | Live run event stream | **not-started** | No unified event bus; observability still split across audit trail / notifications / dashboard callbacks. |
| 13 | Agent-written auto-memories | **shipped** | `auto_memory.py` — agent-callable remember tool, provenance-tagged, deduped, auto-injected via the project-memory cascade. |
| 14 | `.prompt.md` → `/command` | **partial** | Loader shipped (`prompt_commands.py`, frontmatter, `$ARGUMENTS`, precedence); missing the event-triggered playbook half (daemon wiring). |
| 15 | Cloud/web async runner | **partial** | Pieces exist (`/remote` submit, RemoteGraph client, SSH sandbox seeding, daemon); no assembly, no `apply`-back, no web/mobile surface. → **v2 #42**. |
| 16 | Declarative sandbox env | **partial** | `sandbox_config.py` loads `.bog-agents/sandbox.toml` incl. network allowlist — but `load_sandbox_config()` has **no consumers**; egress enforcement missing. → **v2 #22**. |
| 17 | Shareable permalinks + team | **not-started** | Signed TraceFile export/import/verify exists; no `/share`, no hosted viewer, no team layer. |
| 18 | Typed deps + guardrail façade | **partial** | Guardrail half shipped (`bog_agents/guardrails/`, tripwire semantics); typed-deps half absent. |
| 19 | Butcher v2 cloud fan-out | **not-started** | Slices still sequential, in-place, untyped. → prerequisite work in **v2 #21/#31**. |
| 20 | Living wiki + A2A | **not-started** | No wiki generator; no A2A surface (one stale docstring mention). → A2A half is **v2 #41**. |

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

## Killer features v2 — 2026-07-21 refresh

From a fresh live survey of all four fronts (CLI agents, IDE agents,
frameworks/SDKs, background agents), with every idea novelty-checked against the
code so nothing below re-proposes something already shipped. Numbering continues
from v1 (#21+). Each entry names the competitor that set the bar and the *delta*
over what bog already has.

**What the market moved to since the v1 survey (June):** shared-tasklist agent
teams with peer messaging (Claude Code, Devin, Factory); OS-level local sandboxes
with network allowlists (Claude `/sandbox`, Codex) — with **no native Windows
sandbox anywhere** (open flank); named trust profiles replacing blanket auto-approve
(Codex); runtime-evidence debugging (Cursor Debug Mode — "arguably its best
feature"; Junie drives the real debugger); per-line provenance (Cursor Blame);
evidence-bearing merge-ready PRs (Cursor cloud agents: video/screenshots attached,
30-35% of internal merged PRs agent-authored); assign-the-issue acquisition UX
(Copilot, Jules); CI-red auto-repair (Jules); ACP as the settled editor-interop
layer (JetBrains co-develops, 50+ agents / 12+ clients); A2A at 150+ orgs under
Linux Foundation; CodeAct converging across smolagents/MAF/OpenAI; and **deepagents
0.7 alphas already shipping breaking changes** against our 0.6.12 parity badge.

### Tier 1 — Table stakes (users now expect these; mostly S/M)

- **#23 Named trust profiles + risk-graded reviewer approvals** *(Codex)* — bundle
  RBAC/expert-rules/safe-tools/auto-mode into user-nameable trust profiles
  (`bog --profile audit`: read-only, no-network, write-limited) and route the
  approval long tail through a cheap reviewer model that stamps a risk level on the
  HITL dialog. `profiles.py` exists but bundles model/effort, not trust policy.
  Requires the REVIEW v4 Wave-1 pinning work (RBAC/air-gap operator-owned) first. (M)
- **#24 Self-modification guard** *(CVE-2026-25725 class)* — the agent must not be
  able to edit its own authority. Hooks and stdio-MCP configs are already
  fingerprint-gated; extend the same fail-closed posture to `.bog-agents/expert_rules/`,
  `laws.md`, the skill/MCP trust stores, and `config.toml` — enforced below the tool
  layer, with `/why` explaining denials. Every reviewer now checks for this attack. (S)
- **#25 Per-agent cost ledger + runaway caps** *(Claude Code, June 2026)* — attribute
  cost per subagent/worktree/teammate (TUI tree + `/cost`), and session-wide default
  caps on subagent spawns and web searches. Prerequisite: the model-pricing catalog
  fix (REVIEW v4 CTX-3). As bog leans into parallelism this is a liability shield,
  not polish. (M)
- **#26 deepagents 0.7 parity pack** — upstream 0.7 alphas (a1–a7 live) ship breaking
  changes: ls/glob "No files found" sentinel, read_file pagination/gutter format,
  backend delete protocol shape, bounded streaming grep, private subagent state.
  Parity is now a treadmill, not a badge — track the alpha line and land the pack
  before 0.7 GA invalidates the drop-in claim. (M)
- **#27 Wire the declarative environment spec** — `sandbox_config.py` implements
  `.bog-agents/sandbox.toml` (v1 #16) but has **zero consumers**. Wire it through the
  sandbox factory, daemon, and GitHub Action; enforce the network allowlist in the
  backends. Prerequisite for #22, #30, and #42. (M)
- **#28 Turn-end changes tray in the TUI** *(VS Code Changes panel, Cline, Kiro)* —
  every competitor ends a turn with a reviewable changeset. bog has checkpoint
  commits, `/diff`, `/undo`, `/rewind` — add the surface: per-file diff stats after
  each turn, side-by-side view, per-hunk accept/reject wired to the existing
  checkpoints. (M)
- **#38 OTel GenAI-semconv observability** *(MAF, ADK)* — vendor-neutral OTLP spans
  for model/tool/middleware/subagent events with cost attributes. The plumbing
  exists but is LangSmith-bound; make LangSmith one exporter among many. Unblocks
  enterprise checklists (Datadog/Grafana/MLflow). (M)

### Tier 2 — Differentiators (win deals; exploit the middleware moat)

- **#21 Agent Teams — the first *governed* team mode** *(Claude Code teams, Devin
  teams, Factory Missions)* — peer teammates in parallel worktrees with a shared
  claimable task ledger and inter-agent mailboxes. bog's `/team` registry, parallel
  worktrees, and orchestrator are 60% of it; the delta is peer messaging + task
  claiming + lead-as-coordinator. The moat: run it under expert rules, DLP, audit
  trail, and per-agent cost caps (#25) — Claude's version is an ungoverned
  experiment behind an env flag. Supersedes v1 #6. (L)
- **#22 Native local OS sandbox — own Windows** *(Claude `/sandbox`, Codex)* —
  `sandbox/local_sandbox.py` already implements bubblewrap/seatbelt wrapping but is
  **unwired**. Wire it into LocalShellBackend, add the localhost network-allowlist
  proxy, and ship the thing nobody has: a native **Windows** sandbox (AppContainer +
  Job Objects). bog is Windows-first by development reality — this is the open
  flank. Completes v1 #16 with #27. (L)
- **#29 Evidence bundle on every autonomous PR** *(Cursor's merge-ready bar)* —
  `EvidenceBundleMiddleware` packaging test output, before/after screenshots,
  optional browser-session recording, and the rubric verdict into one artifact
  attached to the PR/dispatch target. All ingredients shipped (`/qa`, rubric,
  browser/computer use, audit trail); zero packaging. Completes v1 #2. (M)
- **#30 Assign-to-bog + draft-PR etiquette + CI-red auto-repair** *(Copilot, Jules)* —
  the background-agent acquisition UX: daemon trigger on issue-assigned/labeled,
  open a `[WIP]` draft PR immediately, stream commits, keep the description updated,
  treat review comments as revision triggers; plus a check-run consumer that re-enters
  the originating session on a red build with attempt caps and human escalation.
  Completes v1 #1/#7 and the inbound half of #8. (L)
- **#31 Best-of-N attempts with rubric auto-judge** *(Codex `--attempts`)* — extend
  `/race` + `/jury` (currently bare chat completions) to full agent runs in isolated
  worktrees, optionally across models via the portfolio, scored by `/rubric`//`/qa`,
  ranked diff comparison, keep the winner. Nearly free on bog's primitives; no OSS
  framework ships it end-to-end. (M)
- **#32 Two-way Slack sessions** *(Cursor's Slack agents)* — upgrade Slack from
  outbound dispatch to conversational surface: Events API consumer, @bog in a thread
  launches a run with the thread as context, replies steer it, PR + evidence bundle
  land back in-thread. How background agents spread inside orgs. (M)
- **#33 Session teleport across surfaces** *(Claude teleport — but both directions)* —
  `bog attach <run-id>` hydrates a daemon/serve session into the TUI mid-flight;
  push a TUI session to the daemon to keep running after the laptop closes.
  Checkpointing makes this transport + locking. Every incumbent is one-directional;
  the community explicitly begs for the inverse. Groundwork for #42. (L)
- **#34 Cross-ecosystem plugin importer** *(Qwen Code's smartest move)* — extend
  `claude_code_compat.py` (already imports Claude skills/commands and syncs MCP
  configs) into full install-time conversion of Claude Code Marketplace plugins and
  the now-orphaned Gemini CLI extensions. Ecosystem gravity is the hardest moat to
  build organically; borrow the two biggest. (M)
- **#35 Recipes v2 + registry** *(Goose: 60% of Block on this one primitive)* —
  `recipes.py`/`pipeline.py` shipped the run-time; add typed parameter validation,
  auto-provisioning of declared MCP servers/extensions, repo-committed recipes, and
  a shareable registry. The unit of adoption that spreads an agent beyond power
  users. (M)
- **#36 CodeAct execution mode** *(smolagents, MAF, OpenAI "in development";
  deepagents QuickJS)* — a middleware exposing the tool registry as a scriptable API
  inside bog's sandbox backends, with expert rules intercepting each tool invocation
  from generated code. This is the value argument the deferred Wave-4 QuickJS item
  was waiting for — and bog's rule-engine governance makes its version defensible. (L)
- **#39 Durable crash-safe runs** *(PydanticAI + Temporal/Restate; LangGraph 1.x
  positioning)* — resume-after-death for serve and daemon: durable run IDs,
  idempotency keys on tool execution, per-tool at-least-once/at-most-once replay
  policy (destructive tools re-confirm), optional Temporal/Restate adapter.
  Sharpens v1 #8's durable-execution half. (L)
- **#40 Versioned immutable AgentSpecs** *(Anthropic Managed Agents architecture)* —
  daemon triggers and serve sessions pin an immutable spec version (model, prompt,
  tools, MCP, skills); rollback, traffic-split A/B, and an audit-trail link from
  every run to the spec that produced it. Ops-grade answer to "which config did
  this?". (M)
- **#41 A2A server + A2A sub-agents** *(150+ orgs, Linux Foundation)* — publish an
  Agent Card over serve mode (bog as a callable peer for ADK/MAF/enterprise
  orchestrators) and an `A2ASubAgentBackend` so remote A2A agents slot into the
  sub-agent machinery. Same move as MCP-server, one layer up. Completes v1 #20's
  A2A half. (M)
- **#43 `/debug` — runtime-evidence debugging** *(Cursor Debug Mode, Junie)* — the
  agent instruments code with log statements reporting to a local collector server,
  asks you to reproduce, fixes from captured runtime data, strips instrumentation;
  later, a DAP client for real breakpoints. **No terminal agent has this.** (L)
- **#44 `bog blame` — per-line provenance** *(Cursor Blame)* — sidecar index mapping
  file/line ranges → (session, turn, model, tool) recorded at checkpoint time,
  reconciled with git blame; `/blame file:line` opens the originating conversation;
  headless twin for CI. Natural fit for bog's enterprise audit posture. (M)

### Tier 3 — Moonshots

- **#42 `bog fleet` — self-hostable cloud runner** *(Anthropic Managed Agents,
  Cursor self-hosted cloud agents)* — compose serve (sessions API) + daemon
  (queue/triggers) + sandbox providers + #27's environment spec into one
  deployable: submit task → isolated sandbox → stream events → draft PR with
  evidence bundle → steer from the TUI via #33. Self-hostable first — the
  enterprise-shaped variant the incumbents charge for. Sharpens v1 #15. (XL)
- **#45 Outcome-graded self-improvement loop** *(Anthropic dreaming + outcomes,
  Mastra goals — none close the loop end-to-end)* — durable outcomes attached to
  daemon triggers and serve sessions, graded by the existing rubric engine after
  every run; dreamscape's dormancy learning grounded in the graded history instead
  of ungraded transcripts; expert-rule proposals generated from failure patterns.
  Uniquely composable from parts only bog owns. (XL)

### Deferred items — re-evaluated this cycle

- **`app.py` god-class extraction** (deferred 2026-05-07): partially reopen.
  The full 3-PR mixin split stays parked, but REVIEW v4's P0 argues for extracting
  a **TurnManager** (single-flight turn lifecycle) now, and continuing the
  controller-per-handler pattern under the existing ratchet.
- **Wave 4 satellites** (deferred 2026-07-11): QuickJS code interpreter →
  **answered** by #36 (CodeAct) — build it as governed CodeAct, not a port. Deploy
  CLI → subsumed by #42. Talon + eval-product → remain deferred.
- **PR #150 backlog** (2026-06-18): watchdog dependency → **adopt** (REVIEW v4
  Wave 3 daemon file-triggers). Sandbox providers → already largely shipped (five
  wired via `sandbox_factory.py`); remaining delta is E2B + Manifest-style
  credential isolation, folded into #27/#42. Dreamscape backends → remain deferred.

### Sequencing update (2026-07-21)

REVIEW v4's Waves 0–2 (default-path correctness, trust honesty, delivery truth)
gate everything — several Tier-1/2 items stand directly on code v4 flags (e.g.
#23 needs the RBAC/air-gap pinning; #25 needs the pricing catalog; #30 needs the
daemon reliability wave). After that: **Tier 1 in one wave** (small, mostly
packaging), then Tier 2 opening with **#21 Agent Teams + #29 Evidence bundles +
#30 Assign-to-bog** — the three that most directly convert bog's engine depth
into the trust-and-distribution story the original thesis says we're losing.

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
