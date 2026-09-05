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

- **#46 Unified effort ladder — one dial scales *both* parameters and algorithm**
  *(no comparator ships this; Claude Code/Cursor scale reasoning tokens only,
  bog already owns the algorithm half)* — today the two halves are separate:
  `/effort` (`reasoning_effort.py`) scales the **provider reasoning knob**
  (`output_config.effort` / `reasoning.effort` / `thinking_level`, per-model
  capability-gated), and `/operator` (`operator_mode.py`) does **judge-driven
  workflow routing** (`direct` → `butcher` → `jtbd`). Neither turns a single
  user-facing dial that *also* composes the heavier algorithms bog now owns.
  Build one **effort ladder** where each rung adds both more reasoning budget
  *and* a heavier algorithm: `low` = single-shot; `medium` = +reasoning effort;
  `high` = best-of-N (#31) with the rubric grader; `xhigh` = best-of-N +
  `/jury` adversarial verification + evidence bundle (#29); `max` = team
  decomposition (#21) / butcher with per-slice verification. The knob is one
  word; the escalation is deterministic (not judge-guessed) and composes parts
  only bog has end-to-end. Surfaces as `/effort` (manual) and as the automatic
  target of `/operator`'s difficulty classification. **Deps:** #21, #29, #31,
  `/jury`, operator + reasoning-effort (all shipped). (M–L)

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

## Killer features v3 — 2026-09-04 refresh

Method: five competitor buckets (30 products) researched live on 2026-09-04 by
agents blind to the code, producing 85 candidate features; every candidate was
then novelty-checked by a separate agent that grepped the codebase and marked it
shipped / partial / absent / proposed-not-built with `path:line` evidence.
The 85 were deduplicated into the 30 features below (#47–#76). Numbering
continues from v2 (#46). Each entry names the competitor that set the bar,
bog's verified status, the exact delta, the target users it serves
(**S**olo/OSS, **T**eams, **E**nterprise), and effort (S/M/L/XL). Per-candidate
detail with sources lives in `docs/competitive/killer-features-v3-candidates/`
(one JSON per bucket).

### The market, September 2026 — what moved since the July survey

Research date 2026-09-04 across 30 products in four buckets (frontier CLIs, IDE/terminal agents, background/cloud agents, frameworks). Every claim below carries a source in the per-bucket research files; items the researchers could not source from a primary page are listed as unverified in those files and are not used here.

#### Eight shifts that change what "killer" means

1. **Classifier-graded approval replaced human approval as the default.** Claude Code made auto mode the default on Aug 14 (classifier calls free on Pro/Max/Team) with prose allow/soft-deny/hard-deny rules and a `claude auto-mode {critique,reset}` CLI; Codex ships Guardian (a reviewer agent grades boundary crossings low→critical against a tenant-replaceable `policy.md`, circuit breaker after 3 consecutive denials, `/approve` override); Antigravity offers proceed-in-sandbox. Manual HITL is now the opt-in fallback. The differentiator has moved to the *rule DSL, the denial-retry UX, and lint/critique tooling around the classifier* — exactly the substrate bog's expert engine already is.
2. **Always-on is the new default shape.** Cursor (cloud-agent subscriptions, `/goal`, `agent persist`), Amp (event-driven orbs, self-scheduling agents), Grok Build (Workflows, timer scripts), Warp (Factories), Managed Agents (scheduled deployments + vaults + hard budgets that *pause* instead of kill), Kiro Crew (Apache-2.0 cron/webhook/script jobs), Devin (scheduled scans). A terminal agent that only runs while a human watches now reads as last-generation. bog's daemon is the right asset; its execution layer is hollow (§1).
3. **"Orchestrate anywhere, execute on your machines" became a product line — and none of it ships a Windows worker.** Devin Outposts (atomic-claim queue to your Mac mini/K8s), Cursor Self-Hosted Machines, Claude self-hosted runners (no Bedrock/Vertex, no ZDR), Codex remote executors over a Noise relay, Managed Agents EnvironmentWorker on Modal/Daytona/E2B, Warp Factories in-VPC. The control plane stays with the vendor; execution and secrets stay with the customer. Windows is absent from every one of these stories, and Claude Code's OS sandbox is still WSL2-only (issue closed "not planned"). Codex is the only product with a native Windows restricted-token sandbox.
4. **Cost unpredictability is the #1 user complaint everywhere, and usage attribution is now table stakes.** GitHub moved 4.7M seats to metered AI Credits on Jun 1 (10-50× bills for agentic users; discussion #198015 closed by staff); Cursor's credit billing remains its dominant grievance; Devin's ACUs are called opaque. The responses that users praise: `/usage` by skill/subagent/plugin/loop (Claude Code), spend-limit bars, prompt-cache hit ratios *with miss explanations*, per-message cost/TTFT (Goose, OpenHands, DeepSeek, Kilo), Amp's natural-language "Explain Usage", and Managed Agents' hard USD budgets that pause. Mastra and Pydantic shipped scoped monetary budgets with warn thresholds in August; ADK shipped `ADK_MAX_LLM_CALLS`.
5. **Harness overhead became a buying criterion.** The July 13 HN thread (Claude Code 33k pre-prompt tokens vs OpenCode 7k) drove real switching; deepagents 0.7 published a 65% input-token cut (5,395 → 1,895/turn) with an eval suite; NOOA and ADK market on tokens-per-turn. A harness that cannot *show measured* prompt overhead is assumed bloated. bog has never published its number.
6. **Packaging standards consolidated in one summer.** Agent Plugins 1.0 (spec Aug 6; TSC of Amazon, Anysphere, Microsoft, OpenAI, Vercel, Google; GA in Copilot Aug 12, Kiro Aug 7, OpenHands Aug 21, Cline Sep 3, Codex marketplaces for Bedrock *and* Claude Code), Agent Skills + AGENTS.md + MCP under the Linux Foundation's AAIF, Goose's Open Plugins hooks spec (PreToolUse denial, blocking Stop, `on_failure`). MCP 2026-07-28 went stateless and deprecated sampling/roots/logging/HTTP+SSE/DCR on a 12-month clock; langchain 1.4, Mastra 1.60 and MAF adopted it within six weeks. A harness that cannot load these formats unchanged is the odd one out.
7. **ACP flipped from "editor protocol" to "agent interop".** OpenHands, Goose and Devin Desktop run Claude Code / Codex / Pi as ACP *providers inside their own loops*; Warp's control plane runs Claude Code, Codex and Cursor side by side with cross-harness memory; Cline's Hub brokers one live session to many clients. The emerging shape is a *governed outer harness hosting other vendors' inner agents* — a bigger opportunity than being one more inner harness.
8. **Trust became a release feature, and the open-source map redrew itself.** Google killed consumer Gemini CLI on Jun 18 (100k stars, one month's notice, "bait-and-switch" at 406 HN points) for a closed Go binary; Grok Build was caught uploading whole repos (5.1 GiB vs 192 KB of model traffic) and source-dropped under Apache-2.0 within 72 hours with contributions closed; OpenAI disclosed on Aug 27 that its own research agents escaped a sandbox via an Artifactory zero-day; Goose's `goose review` executed attacker code from repo `.git/config`; Kiro shipped two CVEs (config rewrite from a poisoned page, workspace exfiltration). Users now ask *what leaves the machine* and expect a verifiable answer. Meanwhile DeepSeek Harness went from launch (Aug 13) to 212k stars in three weeks on "everything is a plugin", Pi (four tools, no MCP) passed 100k, OpenCode sits at 160-200k, and Aider is in maintenance (no release since Aug 2025) — a one-maintainer harness without a gateway or foundation behind it does not survive.

#### What is now commodity (do not market these; ship them quietly if missing)

Plan mode, `/rewind`, `/btw` side chats, fork-in-place, queued/editable messages, `/recap`, worktree-per-session subagents, nested subagent trees, "Waiting" states, MCP with OAuth, skills/plugins marketplaces, Slack integration, voice dictation, terminal image rendering, headless/CI modes, session export to Markdown/JSON, per-message cost display, context-window meter. Every one of these shipped across Devin, Factory, Copilot, Kiro, Cursor and Codex between June and September and is judged table stakes.

#### Open flanks nobody owns (where a small team can still win)

- **Native Windows isolation** (sandbox + egress allowlist + self-hosted worker). Only Codex has a Windows sandbox; nobody has a Windows worker for the execute-on-your-machines pattern.
- **A policy engine you can prove things about.** Every vendor's auto-approval is a classifier plus prose rules; nobody ships deterministic, explainable, provable policy (`/why`, `/prove`) as the *primary* gate with the classifier as backstop.
- **Governed host for other vendors' agents.** Run Claude Code / Codex / Pi / dcode as ACP providers *under* bog's expert rules, cost ledger, audit trail and sandbox — the reverse of what OpenHands/Goose/Warp do (they host but do not govern).
- **Self-hostable execute-anywhere with compliance-grade evidence.** Claude's self-hosted runners forbid Bedrock/Vertex and ZDR; Managed Agents cloud sessions are not ZDR/HIPAA-eligible; Cursor and Warp keep the control plane. An open, local-first control plane *plus* worker with tamper-evident logs is unowned.
- **Published harness overhead + published savings.** Nobody in the OSS field publishes tokens-per-turn and pre-run cost estimates side by side; bog has the sweeper, cost ledger and Harbor to do it honestly.
- **A stability contract at 1.0.** Release velocity is a top complaint against deepagents (13 patches in 6 weeks with a breaking rename), Pydantic (~2/week), Mastra (weekly renames), ADK 2.0 (BaseNode migration). LangGraph 1.0 LTS and Pydantic's 3-month no-breaking window are praised. A 1.0 with a written stability contract is a selling point in itself.

#### Coverage notes (completeness pass, done inline)

Four products outside the five buckets were checked because they matter to the target users:

- **JetBrains Junie** (GA; 6th on SWE-Rebench at 62.8% and a *published $1.14 average cost per task*; per-action allowlists for Terminal-with-regex / MCP / Build / RunTest; an interactive terminal UI). Two takeaways: publishing cost-per-task alongside a benchmark score is now normal, and capability-typed allowlists (not just command regexes) are the IDE-side expectation. Sources: jetbrains.com/help/ai-assistant/junie-agent.html, techzine.eu/blogs/applications/133356.
- **Augment Code** — its Context Engine ships as an **MCP server any agent can mount** (Feb 2026; claims +70% agent performance across Claude Code/Cursor/Codex); Remote Agents GA in isolated cloud VMs. Takeaway: a retrieval index is a *product other harnesses consume*, not just a feature inside one's own loop. Sources: augmentcode.com/changelog/context-engine-mcp-in-ga, siliconangle.com 2026-02-06.
- **Zed** — the **ACP Registry is live** (Claude Code, Codex, Copilot CLI, OpenCode, Gemini CLI listed); parallel agents in one window; Terminal Threads; Zed for Business. Takeaway: the registry is a free distribution channel bog's ACP package is absent from because it is unpublishable (§4 DEL-1). Sources: zed.dev/blog/acp-registry, zed.dev/docs/ai/external-agents.
- **Continue.dev** — acquired by Cursor on 2026-06-18; final 2.0.0 release; repository read-only. Takeaway: the third OSS harness to die or go closed in one summer (Gemini CLI, Continue, Aider-in-maintenance); reinforces §2.1 (8).

Not covered and deliberately out of scope: Replit, Trae, Qwen Code, Cody (Sourcegraph folded Cody into Amp), Tabnine — consumer/China-market or discontinued surfaces with no bearing on the three target users. Reddit sentiment was unavailable to every researcher (domain blocked); all sentiment derives from HN, GitHub issues/releases and blogs.

### v2 scorecard — re-verified against code, 2026-09-04

| # | Feature | July status | Now | Note |
|---|---|---|---|---|
| 21 | Governed Agent Teams | not started | **shipped** | `/team run` over `TaskLedger` + `Mailbox` under `RunawayCaps`; per-teammate cost in the TUI tree unverified |
| 22 | Native local OS sandbox (Windows) | not started | **partial** | wired + tested on POSIX; Windows launcher has zero code; README omits the caveat |
| 23 | Named trust profiles | not started | not started | `profiles.py` still bundles model/effort only |
| 24 | Self-modification guard | not started | **shipped** (CLI-owned) | pure-SDK consumers get no guard; `config.toml`/trust stores only shell-screened |
| 25 | Per-agent cost ledger + caps | not started | **partial** | ledger counts `run_team` only; `task` subagents uncounted (v6 SDK-7) |
| 26 | deepagents 0.7 parity pack | not started | **stale** | aligned to 0.7.0b2; GA 0.7.0 (07-24) → 0.7.13 (09-02) unscouted; deepagents not in the SDK venv |
| 27 | Wire declarative environment spec | not started | **shipped** (2 of 3) | sandbox factory + daemon consume it; GitHub Action consumer never landed |
| 28 | Turn-end changes tray | not started | not started | `/diff` is raw monochrome text |
| 29 | Evidence bundle on autonomous PRs | not started | **partial** | SDK primitive + middleware exist; reachable from nothing (no FeatureConfig field, flag, or dispatch) |
| 30 | Assign-to-bog + draft-PR + CI-red repair | not started | **partial (≈20%)** | 14-test event parser + HMAC front door; `trigger_context` never reaches the model (v6 DMN-1) |
| 31 | Best-of-N with rubric judge | not started | **shipped** | `/best-of-n` real worktree attempts; cross-model portfolio unverified |
| 32–35, 38–46 | Slack, teleport, plugin importer, recipes v2, OTel, durable runs, AgentSpecs, A2A, fleet, /debug, blame, outcome loop, effort ladder | not started | not started | #46's dependencies are now all shipped, so it is unblocked |

### The six bets (what "win" means this cycle)

1. **Governed Auto Mode** — beat Claude Code and Codex on the default they just
   shipped, by putting a deterministic, provable policy floor *under* a
   provider-agnostic classifier (#47, #48, #49). Nobody else can say "here is the
   rule that fired" for an auto-approved call.
2. **Cost certainty** — answer the single loudest complaint in every ecosystem
   with pre-flight estimates, budgets that *pause*, default-on spawn caps, and
   routing that shows its savings (#51–#54). bog owns the ledger; the delta is
   wiring and surfaces.
3. **Own Windows** — the one flank no vendor holds: native Windows isolation,
   Windows workers for execute-on-your-machines, and a signed installer
   (#60, #61, #57).
4. **The agent follows what it creates** — make the daemon's execution layer
   real, then let the agent subscribe to the PR it opened, schedule its own
   follow-ups, detach and re-attach (#55, #56, #58).
5. **Zero switching cost, governed host** — load Agent Plugins 1.0 and every
   vendor's hooks/sessions unchanged, then run *their* agents as teammates under
   bog's rules, ledger and evidence (#62, #63, #64, #65).
6. **Proof beats diff** — a turn-end tray, evidence on every PR, and a
   self-review loop that learns from human dispositions (#66, #67, #68).

Cross-cutting: **ship a written stability contract with 1.0** (no breaking
changes within a major, deprecations before removals, a 3-month window) —
release velocity is the top complaint against deepagents, Pydantic, Mastra and
ADK, and a contract is itself a feature to the target users.

### Tier 1 — Table stakes (users now expect these; S/M each)

- **#47 Governed Auto Mode** *(Claude Code auto-mode default Aug 14; Codex
  Guardian; Antigravity proceed-in-sandbox)* — **shipped 2026-09-04** (was partial; see REVIEW v6 §6). bog has the
  deterministic chain (`ask_list → git_ops → exec_risk → bash_hygiene`) and a Haiku
  backstop hard-bound to the `anthropic` package (`auto_mode.py:455-490`), off by
  default. Delta: (1) inject the judge through the model factory so Ollama /
  OpenAI / Bedrock users get a real reviewer (a local 8B runs it free and
  offline); (2) one *batched* structured review per turn (all pending calls + the
  user's stated outcome → `low|medium|high|critical` + rationale) instead of
  per-call; (3) assert an `approval_decision` fact (rule source, classifier
  verdict, rationale) through `ExpertRulesMiddleware` so `/why` answers "why did it
  let that through" and YAML rules can override the classifier; (4) a
  denial/timeout counter that drops back to the human (Codex's circuit breaker);
  (5) make `acceptEdits`/auto the wizard-recommended default. **S/T/E, M.**
- **#48 Trust profiles, `--restricted`, and workspace trust** *(Codex permission
  profiles + `trust_level`; Claude `--restricted` Aug 27, `--permission-prompts
  none` Sep 2; Kiro protected paths after CVE-2026-10591)* — **shipped
  2026-09-06** (REVIEW v6 §17). `trust_profiles.py`: a `TrustProfile`
  (permission mode + lock, restricted flag, sandbox level + egress allowlist,
  allowed/blocked fetch domains, excluded tools) read from `custom_settings.trust`
  of a `profiles.json` entry — `bog-agents --profile audit` applies it in
  `create_cli_agent`, and the App refuses the shift+tab / ctrl+t / `/profile`
  changes the profile locks. `--restricted` is the built-in preset: no shell,
  git/PR, raw HTTP, search, daemon, plugin, preview or other process-spawning
  tools (`RESTRICTED_TOOL_NAMES`, stripped from the tool list *and* from every
  middleware's tools; a drift test rebuilds the restricted agent and fails when a
  surviving tool's module spawns processes), bypass / accept-edits refused,
  `auto_approve` forced off, `fetch_url` kept only with a domain allow-list.
  `web_policy.py` + `web.allowed_domains` / `web.blocked_domains` gate every hop
  in `assert_fetch_allowed` before DNS (`DomainPolicyError`).
  `workspace_trust.py`: one fingerprint over the repo-controlled instruction and
  policy files (`.bog-agents/**`, `AGENTS.md`, `CLAUDE.md`, `.claude/**`,
  `.cursor/**`, `.agents/**`, `.mcp.json`, workflows) — `/permissions
  trust-workspace` records it and trusts hooks + MCP in the same step;
  `/permissions` shows trusted / CHANGED since you trusted it / never
  acknowledged. `authority_file_permissions` now carries a `deny` tier
  (`.git/hooks/**`, `.git/config`; under `--restricted` the CI / editor / hooks /
  `.mcp.json` files too) ahead of a wider `interrupt` tier (`.github/workflows`,
  `.vscode`, `.idea`, `.claude`, `.cursor`, `.agents`, CLI settings + sandbox
  config). *Was:* partial. Open: a profile's sandbox level / egress allowlist are
  carried but not yet applied to `SandboxConfig`; workspace trust is reported,
  not enforced, until the first-open posture decision (prompt vs. silent
  restricted). Completes v2 #23 + #24. **E/T/S, M.**
- **#49 Steerable approvals + hostile-repo hardening** *(Cursor "skip and tell the
  agent what to do", 15 s auto-reject; Grok persistent "Never allow"; Goose
  GHSA-r5pp-p5r8-466r fsmonitor RCE; Cursor hardened git Aug 11)* — **shipped
  2026-09-05** (REVIEW v6 §10): `ApprovalMenu` has five options — Approve /
  Auto-approve / Reject / "Reject and tell the agent what to do instead" (the
  redirect lands in the rejection `ToolMessage`) / "Never allow this in this
  project" (persisted to `.bog-agents/settings.json` `auto_mode.never_allow`,
  a tier above `ask` that denies before the menu ever opens) — plus a countdown
  auto-reject (`approvals.timeout_seconds` / `BOG_AGENTS_APPROVAL_TIMEOUT`,
  fail-closed). SDK `git_env.py`: `hardened_git_env()` pins the code-executing
  config keys through `GIT_CONFIG_COUNT` to the *trusted* (system + global)
  value or an inert one, editors and the pager always inert, and is used at
  every internal git call site in the SDK and CLI (34 calls); patch-producing
  diffs pass `NO_EXTERNAL_DIFF` because `diff.external` cannot be neutralised by
  override (verified on git 2.44); `scan_repo_config()` + `repo_trust.py` block
  `/diff`, `/review`, `/pr` until `/permissions trust-git-config` acknowledges
  the findings once per config fingerprint. *Was:* absent — `ApprovalMenu` had
  exactly Approve / Auto-approve / Reject and every git call inherited the
  repo's config. `exec_risk` still covers only the command-line vector.
- **#51 Cost certainty: pre-flight estimate, budgets that pause, caps that fire**
  *(Managed Agents `budget_reached` pause; Copilot AI-credits backlash #198015;
  OpenCode `subagent_depth`; Mastra TokenCostControl; ADK `ADK_MAX_LLM_CALLS`)* —
  **shipped 2026-09-05** (REVIEW v6 §8): `CostTrackerMiddleware(on_budget="interrupt")`
  pauses with a `budget_reached` interrupt that only a raise-cap resume clears (the
  TUI asks inline through the ask-user widget; `/cost budget <N|off>` rides on the
  per-turn context); `cost.*` manifest keys (`budget_usd`, `daily_ceiling_usd`,
  `warn_at_percent`, `max_subagents`, `max_web_searches`, `preflight_threshold_usd`)
  feed every CLI agent's `CostLedger`, and `web_search` finally counts; a durable
  `SpendLedger` (`~/.bog-agents/spend.db`) gates new turns on the user's daily
  ceiling; `/team run`, `/butcher`, `/best-of-n` confirm a projected bracket above
  the threshold; daemon jobs take `budget_usd` (run pauses, `POST /runs/{id}/resume`
  / `jobs resume`) and `daily_ceiling_usd` (runs skipped). Subagent depth stays 1 by
  construction (GP subagents never receive `task`). *Was:* `budget_usd` strict mode raised `RuntimeError` (turn died),
  `RunawayCaps` default to `None` (uncapped) and are consulted only by
  `run_team`. Delta: replace the raise with a `budget_reached` LangGraph
  interrupt that pauses and accepts only a raise-cap resume (`/cost budget <N>`,
  daemon `POST /runs/{id}/resume`); count `task` and async subagent spawns and
  web searches in `CostLedger` (v6 SDK-7); default caps surfaced in
  `config_manifest.py` (concurrency, depth 1, per-run USD); a pre-spawn modal
  ("N agents, model X, projected $A–$B") before `/team run`, `/butcher`,
  `/best-of-n` and any above-threshold burst; a durable `SpendLedger` (SQLite
  beside `sessions.db`) with user/project/daemon-job daily ceilings and
  `warn_at_percent`. **S/T/E, M.**
- **#52 Usage you can read: per-message strip, `/cost explain`, cache diagnostics**
  *(Goose per-message usage; Amp "Explain Usage"; Claude Loops breakdown +
  cache-miss explanations; Warp per-category $)* — **shipped 2026-09-05** (REVIEW v6
  §9): dim usage strip under every reply (in/out, cache read/write, $, TTFT, tok/s,
  subagent tag), session $ + cache-hit ratio in the status bar, `/cost tree` by
  category, `/cost explain <question>` over the serialized ledger with the review
  model, and the innermost `CacheBustDetectorMiddleware` behind `/cost cache` that
  names the system-prompt section or history rewrite that broke the prefix.
  *Was:* partial — CostTracker already
  recorded cache read/write tokens per request; the TUI showed one fixed number.
  Add a dim usage line under each assistant message (in/out/cache-read/write, $,
  TTFT, tok/s), session $ and cache-hit ratio in the status bar, `/cost` rendering
  `CostLedger.format_tree` by category (main/subagent/team/worktree/web/mcp),
  `/cost explain <question>` over the serialized ledger with an injected cheap
  `invoke`, and an innermost `CacheBustDetectorMiddleware` that hashes the prefix
  each call and names the middleware whose injected segment broke the cache.
  **S/T, S+S.**
- **#54 Published harness overhead + lean profile** *(deepagents 0.7: 5,395 →
  1,895 tokens/turn with an eval; HN 33k-vs-7k thread; NOOA, ADK)* — **shipped
  2026-09-05** (REVIEW v6 §11): SDK `token_audit.py` builds the agent around a
  recording model, runs one probe turn and attributes the fixed cost per
  middleware (instrumented `wrap_model_call` deltas) and per tool schema;
  `create_agent` hands its final stack to the audit through `notify_assembly`.
  Built-in `lean` profile (`FeatureConfig(harness_profile="lean")`: 3-sentence
  base prompt, one-line tool descriptions, no todo list) and `--mini` in the CLI
  (`lean` + every non-core tool schema deferred behind `tool_search`/`select`,
  allowlist mode of `DeferredToolsMiddleware`). `/tokens middleware` and the
  headless `bog-agents command "tokens middleware [--mini]"` print the report;
  `tests/unit_tests/smoke_tests/test_harness_overhead.py` pins the numbers and
  fails CI on a >5% regression. Measured (o200k_base): SDK default 7,619
  tokens/turn → `lean` 2,789; CLI with all 104 tools 21,088 → `--mini` 8,565
  (14 visible tools). *Was:* partial — `HarnessProfile` had the fields but no
  built-in lean profile, no attribution, no baseline. Open: a Harbor pass rate
  beside the number needs a benchmark run (`libs/harbor`). **S/E, M.**
- **#61 Windows distribution and first run** *(Cline signed Windows installer;
  Codex native Windows; Pi PowerShell tool; Kilo's WindowsApps EACCES regression)*
  — **shipped 2026-09-05** (REVIEW v6 §12): `install.ps1` / `install.sh`
  one-liners (uv → pipx → pip, install uv + a Python when absent, Store-alias
  warnings, PATH, doctor); `packaging/pyinstaller/` spec + `build.py` and a
  `windows-standalone` job in `release.yml` that attaches
  `bog-agents-<v>-windows-x64.zip` (+ sha256) to every CLI release (verified
  locally: 248 MB onedir, `--version`, `command "/version"`, `--doctor`,
  `command "tokens middleware"` all run frozen); `packaging/winget/
  generate_manifest.py` (portable nested installer) and a Homebrew formula
  skeleton; SDK `bog_agents.tools.powershell` — opt-in `powershell` tool
  (`tools.powershell` / `BOG_AGENTS_POWERSHELL_TOOL`) that runs scripts through
  `pwsh`/`powershell.exe` as argv, never `cmd.exe`, sharing `execute`'s
  `_DANGEROUS_PATTERNS` gate and, in the CLI, the auto-mode / never-allow /
  approval classification via `SHELL_TOOL_NAMES`; `find_powershell` skips the
  zero-byte `WindowsApps\pwsh.exe` execution alias and both doctors flag it.
  Task Scheduler `daemon install` (v6 DMN-3) and MSYS recovery for headless
  commands (v6 CLI-8) were already in. *Was:* absent. Open: Azure Trusted
  Signing (needs the org's certificate profile; the job has the step ready),
  winget submission and the Homebrew tap (need the maintainer's accounts).
  **S/E, M.**
- **#62 Agent Plugins 1.0 native + one-command import** *(spec Aug 6, TSC of
  Amazon/Anysphere/Microsoft/OpenAI/Vercel/Google; GA in Copilot, Kiro, OpenHands,
  Cline; Codex marketplaces for Bedrock and Claude Code; Cline session import)* —
  **shipped 2026-09-05** (REVIEW v6 §9): `plugin.json` layout mapped onto
  `ExtensionManifest` (`plugin_spec.py`), discovery of `~/.agents/plugins` and
  workspace `.agents/plugins` (disabled until `/plugin trust`), installs from dir /
  zip / zip URL / git / `marketplace.json` with a SHA-256 pin and a lock file
  (`plugin_install.py`), `bog-agents plugin import claude|codex|cursor` covering
  skills, agents, user-level hooks, memories and MCP on top of the native rules
  cascade (`plugin_import.py`; antigravity reports "no documented layout"),
  session import from Claude Code / Codex / Cline into checkpointed, searchable
  threads (`session_import.py`, `bog-agents threads import`, `/onboard import`),
  and a `com.bogware.thread` exporter (`threads export`). *Was:* partial —
  `plugin_marketplace.py:108` read a bog-specific `manifest.json` and
  installed by bare `copytree`. Delta: accept the `plugin.json` root layout
  (`skills/`, `mcp.json`, `agents/`, `commands/`, `hooks/`) mapped onto
  `ExtensionManifest`, discover `~/.agents/plugins` and project `.agents/plugins`
  (workspace ones disabled until trusted), install from git / `marketplace.json`
  / zip with SHA-256 pin, route through `skill_trust` + `mcp_trust`; `bog-agents
  plugin import codex|claude|cursor|antigravity` covering hooks, agents, rules and
  memories on top of today's skills+MCP import; **session import** from Claude
  Code (`~/.claude/projects/**/*.jsonl`), Codex, opencode and Cline into
  checkpointed, FTS-indexed threads as an `/onboard` step; an exporter under a
  `com.bogware` namespace. Supersedes v2 #34. **T/S/E, M.**
- **#66 Turn-end changes tray with proof-ordered diffs** *(every competitor ends a
  turn with a reviewable changeset; Amp intelligently ordered diffs Sep 1)* —
  **shipped 2026-09-05** (REVIEW v6 §9): every turn that wrote files ends with a
  tray of per-file stats in explanatory order (SDK `diff_ordering.py`, shared by
  `/diff --ordered` and `render_evidence_markdown`), `/changes show <n>` for the
  coloured diff, `/changes revert <n> [hunk]` for per-file or per-hunk revert
  without git (`diff_hunks.py`), `/changes keep`. *Was:* absent — `/diff` mounted
  raw text while `DiffMessage`/`EnhancedDiff` already existed for edit widgets. Per-file stats after each turn, coloured side-by-side
  view, per-hunk accept/reject wired to existing checkpoints/`/undo`, and a pure
  `diff_ordering.py` that ranks files by explanatory power (entry points and
  public signatures first; tests, snapshots, lockfiles muted last) used by the
  tray, `/diff --ordered` and `render_evidence_markdown`. Completes v2 #28. **S/T, M.**
- **#68 `/tasks` command center + session-UX table stakes** *(Copilot CLI `/tasks`;
  Codex `codex agents`; Devin nested views, queued/editable messages, `/recap`,
  "Waiting" status; Factory mark-unread/archive)* — **shipped 2026-09-05**
  (REVIEW v6 §13): `tasks_controller.py` builds one `TaskNode` tree over the
  interactive thread ("waiting on you" while an approval menu is open, driven
  by the pending HITL widget), the editable prompt queue (`/tasks queue edit|drop
  <n>`), background tasks / persistent jobs, remote tasks, `/team run` sessions
  (live `TeamRunHandle`: ledger tasks with owners, mailbox, `CostLedger` spend,
  pause gate) and the ambient daemon's jobs + `GET /runs`; verbs `kill`, `steer`
  (team mailbox / task inbox / next prompt), `pause`/`resume` (the coordinator
  stops claiming tasks — `run_team_session(pause_gate=…)`), `diff` (worktree
  branch). `/recap` renders turns, tokens, spend, files, running work, "needs
  you" and the thread's `/btw` notes. `/threads group pr [all]` groups by git
  branch; `/threads archive|unarchive|unread|read <id>` store flags as thread
  tags (no schema change, FTS sidecar untouched). *Was:* partial — `/dashboard`
  watch-only. Not folded in: the SDK `background_shell` registry and
  `WorktreeMiddleware` tasks live inside the server process and are not
  reachable from the TUI (v6 CLI-12); the thread-selector modal does not yet
  hide archived threads. Completes v1 #11. **S/T, M.**

### Tier 2 — Differentiators (win deals; exploit the moat)

- **#50 Managed governance layer** *(Cline remote MCP allowlists; Devin
  required/optional/forbidden plugin manifests; Factory org-wide hooks/MCP
  governance; OpenCode gateway-only routing; Claude managed settings)* — **absent.**
  A `managed` layer at the top of `_settings_cascade.py` sourced from a signed
  URL or repo path (fetched at start, cached, signature-verified) carrying
  `allowed_mcp_servers`, `skill_allowlist`, `required/optional/forbidden`
  plugins with soft-fail, `provider_lock` (gateway-only `base_url`),
  `zero_retention`, and model-switch policy; enforced in MCP discovery,
  `SkillsMiddleware`'s trust checker, `create_model`, plugin install and `/model`
  (assert a `model_switch` fact so YAML rules can deny). Surfaced as org-pinned
  rows in `/permissions` and `/doctor` and recorded in the evidence pack. **E/T, M.**
- **#53 Cost-objective routing with a local rung and provable savings** *(Cursor
  Router Intelligence/Balance/Cost, 60–68% cut; Amp Dial low = GLM-5.2; Factory
  Droid Core free pool + Router failover; OpenCode Go / ClinePass $10 lanes;
  Claude auto-continue after limits)* — **partial.** `operator_mode.py` has
  `local`/`hybrid` presets and a judge but no objective, no persistence of
  decisions, no failover beyond Bedrock. Delta: `objective = intelligence|balance|cost`
  + `[operator.pool]` mapping task classes (subagent exploration, summarization,
  `@codebase` research, judge, butcher workers) to a pool model; persist decisions
  to `~/.bog-agents/operator-decisions.jsonl` with tier/model/cost/rubric verdict
  and bias future choices from it; a counterfactual "saved $X by routing N calls
  to local" line in `/cost`; generalize `bedrock_resilience.py` into a
  provider-agnostic `ProviderFailoverMiddleware` (429/quota headers → rotate
  through `[models].fallbacks` incl. Ollama, window-aware cooldown); parse
  provider reset headers into a "parked, auto-resuming at HH:MM" state in TUI and
  daemon. Absorbs v2 #46's cost half. **S/T, M.**
- **#55 The daemon that actually executes: context injection, subscriptions, draft-PR
  etiquette** *(Cursor cloud-agent subscriptions + `/goal`; Amp self-scheduling
  agents; Copilot assign-to-agent; Jules CI auto-fix)* — **shipped 2026-09-05
  except draft-PR etiquette** (REVIEW v6 §14): SDK `bog_agents.tools.daemon_tools`
  — `schedule(prompt, when)` ("in 2 hours", "at 09:30", ISO, cron, "every 30
  minutes") and `subscribe(source, prompt, until_runs)` (`github:pr:<n>`,
  `github:issue:<n>`, `github`, `webhook:<path>`, `file:<dir>[:<glob>]`) plus
  `list_subscriptions` / `unsubscribe`, POSTing daemon jobs that carry the
  originating `thread_id` and `goal_ref`; the CLI registers the bundle while the
  daemon runs. Daemon: `AmbientJob.max_runs` (attempt cap — `dispatch` and the
  tick skip, `record_run_result` auto-disables), `thread_id` / `checkpoint_db` /
  `goal_ref`, `TriggerConfig.github_number` + `github_kinds` (the GitHub webhook
  fans out only to matching PR/issue subscriptions), and a runner path that
  reopens the CLI's SQLite checkpointer on the thread and frames the event as
  the next message with the goal quoted (`langgraph-checkpoint-sqlite` added to
  the daemon; a missing DB or package falls back to a fresh run with a warning).
  `bog-agents daemon jobs create --max-runs --thread --github-number`. DMN-1/2
  were fixed in Wave 0. *Was:* partial (front door only). **Open:** draft-PR
  etiquette — open a `[WIP]` draft PR at start, stream commits, update the
  description, treat review comments as revisions — is the remaining slice
  (plan in REVIEW v6 §14). Completes v2 #30 and v1 #1/#8/#14 except that. **T/S, L.**
- **#56 Detach / attach, session registry, `bog queue`** *(Cursor `agent persist`;
  Codex `codex queue` + `codex agents`; Claude cross-session `SendMessage`; Cline
  Hub multi-client + zero-loss upgrade)* — **shipped 2026-09-06** (REVIEW v6
  §18). SDK `session_registry.py` (`~/.bog-agents/sessions/<id>.json`: name,
  kind, cwd, model, state, pid, heartbeat, thread, server URL + pid; liveness =
  fresh heartbeat or live pid) and `mailbox_store.py` (SQLite `Mailbox` with the
  same API, WAL + `BEGIN IMMEDIATE` so `drain` is exactly-once across
  processes). CLI: `--name` names a session; `bog-agents sessions [--all|--prune]`
  lists TUI sessions, daemon runs and detached servers; `bog-agents queue
  --session <name> [--wait [--timeout S]] "<prompt>"` drops a prompt the TUI
  drains on its next idle tick (2 s poller, heartbeat included) and answers with
  the turn's last assistant text; `/detach` hands the LangGraph server off
  (`ServerProcess.detach`: atexit hook, log handle and config-dir ownership
  dropped) and `bog-agents attach <name>` reconnects the TUI to it on the same
  thread (`ServerProcess.adopt`; quitting an attached session stops the server,
  `/detach` again keeps it). Daemon: `POST /drain` + `bog-agents daemon drain
  [--stop]` / `daemon upgrade` (drain → stop → `uv tool upgrade` → start),
  SIGTERM and `/shutdown` drain first, a run cancelled mid-flight is recorded
  CANCELLED (a thread-linked job resumes from its checkpoint on the next run),
  every run is listed in the registry while it runs. *Was:* absent. Open: the
  Windows broker question is moot for now — the detached server is the TUI's
  own `langgraph dev` child and dies with the console window; hosting it under
  the daemon service is the follow-up. Absorbs v2 #33 and #39. **S/T, L.**
- **#57 `bog worker`: outbound-only self-hosted workers — including Windows**
  *(Cursor Self-Hosted Machines Sep 2; Claude self-hosted runners — no
  Bedrock/Vertex, no ZDR; Codex remote executors; Devin Outposts; Managed Agents
  EnvironmentWorker; none ship a Windows worker)* — **absent.** `bog-agents worker
  start --pool <name>` dials out (token-authenticated long-poll/WebSocket) to
  daemon/serve and registers OS + sandbox level; a network implementation of the
  SDK backend protocols (shell, files, PTY, browser) whose transport is that
  connection, so the graph runs where serve runs and tools execute on the worker;
  daemon-side pool scheduler with atomic claims modelled on `TaskLedger.claim_next`,
  owner-locking, `--retire-at`, drain grace, idle hibernate; `/handoff --queue`
  packages thread + diff + untracked files. The self-hostable half of v2 #42, and
  the only worker story with Windows in it. **E/T, L.**
- **#58 Structured human decisions from Slack, email and the daemon** *(Codex iOS
  interactive forms; Copilot team sessions in Slack/Teams Aug 24; Devin Slack)* —
  **partial.** `ask_user` exists with `text`/`multiple_choice` in the TUI only.
  Extend with `multi_select`/`confirm`/`file_pick`; make it work outside the TUI:
  daemon dispatch renders Slack Block Kit / email with a signed callback link,
  persists the interrupt in the checkpointer and parks the run until
  `POST /runs/{id}/answer`; a Slack Events consumer (signing-secret verified,
  `app_mention` in a thread → run bound to `thread_ts`, replies steer, `!fast` /
  `!ask` per-message overrides); every answer recorded in the evidence bundle as
  an explicit human decision. Completes v2 #32. **T/E, L.**
- **#63 Governed host for other vendors' agents** *(OpenHands and Goose run Claude
  Code / Codex / Pi as ACP providers; Warp Factories multi-harness control plane;
  Cline Kanban)* — **absent.** An `AcpTeammateRunner` that spawns `claude-agent-acp`,
  `codex-acp`, opencode, goose or dcode over stdio using the ACP client, maps their
  permission requests onto `ExpertRulesMiddleware` + HITL, counts every turn into
  `CostLedger`/`RunawayCaps`, emits OTel spans, wraps results in `EvidenceBundle`;
  `/team run --worker acp:<agent>`; for non-ACP harnesses a `HarnessSubAgentBackend`
  over `claude -p --output-format stream-json` / `codex exec --json` governed by
  installing a bog hook into the child's own hook mechanism that calls back over a
  local socket. The reverse of what OpenHands/Goose/Warp do: they host, bog
  governs. **T/E, L.**
- **#64 Hook bus v2** *(Codex v0.150/151: tool-result replacement, Interrupt,
  PermissionRequest, trust-by-hash, managed hooks; Claude PreModelSwitch /
  PostModelSwitch; Goose Open Plugins hooks spec, `on_failure`; OpenHands
  prompt-evaluated hooks)* — **partial.** Promote `PostToolUse` to MODIFY (honour
  `{"tool_result": …}` before the ToolMessage reaches the model); add
  decision-capable `PermissionRequest`, `Interrupt`, `PreModelSwitch` (deny) /
  `PostModelSwitch` wired into `/model`; load Open Plugins `hooks.json`; per-hook
  `on_failure: deny|allow|ask` overriding the fail-open default; a `prompt` hook
  type that evaluates a natural-language policy through an injected small-model
  invoke on the same fail-closed path as Expert Mode; trust-by-hash for hook
  scripts. **T/E, M.**
- **#67 Evidence on every PR + a self-review loop that learns** *(Cursor Bugbot
  incremental/deduped/effort-graded; Copilot reviews bot-authored PRs with
  resolution reasons Aug 27; Amp "proof of work")* — **shipped 2026-09-06**
  (REVIEW v6 §16). Evidence wiring (`FeatureConfig(enable_evidence_bundle=True)`,
  `--pr --pr-evidence`, daemon dispatch) landed in Wave A. New: `self_review_memo.py`
  — memo per branch (`.bog-agents/self-review/<branch>.json`: sha256 of the exact
  review text, base, effort, verdict) so `/self-review --since-last` skips an
  unchanged diff at the same-or-higher effort; `--effort default|high|custom:"<rule>"`
  (quote-aware) threads a rule into the prompt; every run prints the
  `<!-- bog-review:<sha12> -->` marker CI can dedupe on. `/finding <id>
  addressed|wontfix|incorrect [note]` (the `/resolve` name belongs to merge
  conflicts) appends to `dispositions.jsonl`, and the next review prompt carries
  the `incorrect`/`wontfix` rulings as a do-not-repeat block. `--pr --pr-review
  [--pr-effort]` runs the configured jury over the branch diff after the PR opens
  and posts a GitHub review (`github_review.py`: `path:line` findings become line
  comments, the rest go into the body, marker-deduped, anchor-free fallback when
  GitHub rejects a stale line). *Was:* partial. Open: review-thread events feeding
  dispositions automatically (needs the #55 `github:pr:<n>` subscription plus a
  `pr_review_comment` handler that calls `/finding`). **T/E, M.**
- **#71 Parity treadmill in CI + fork subagents** *(deepagents 0.7.13; Claude fork
  mode default Aug 10-14 and `/fork` into a worktree; dcode)* — **partial.**
  Install `deepagents` latest in a CI leg and run the 24 compat tests plus a
  smoke import on every PR; land the 0.7.x deltas (`mode: isolated|fork` on
  SubAgent, `ReadResult` pagination notice, bounded `grep_max_count`, opt-in
  TodoList via profile, output-format changes). Fork mode: seed the child with the
  parent's exact post-sweeper message list and identical system prompt/tool
  schemas so its first call is a prompt-cache hit, run in the background, return
  the final message as a ToolMessage; `/subtask <prompt>`; `/fork` copies the
  checkpoint thread into a new background session on a fresh worktree; extend
  `run_team`/butcher workers with fork mode. Supersedes v2 #26. **S/T, M.**
- **#72 Governed Code Mode** *(deepagents `CodeInterpreterMiddleware` + dynamic
  subagents; Mastra `createCodeMode`; Pydantic Code Mode; OpenCode code-mode MCP
  adapter; OpenAI programmatic tool calling — all shipped since July)* —
  **proposed-not-built.** Now table stakes. An interpreter middleware (QuickJS or
  a subprocess Python runner inside `LocalSandbox`/Docker) exposing an allowlisted
  `tools.*` namespace whose every call re-enters the normal tool path (Expert
  rules, SafeTools, HITL, `CostLedger` all fire), plus a `task()` global that
  dispatches subagents/teams with a response schema and counts each spawn; an
  `execute_mcp_script` variant binding connected MCP tools; fan-out/vote helpers.
  Promotes v2 #36 from L-later to M-now; bog's version is the only auditable one.
  **T/E/S, L.**
- **#74 Compliance artefact: hash-chained action log + OTel GenAI export + org usage
  export** *(MAF/ADK semconv; Kiro OTLP usage export Sep 1; GitHub per-user credit
  metrics; EU AI Act Annex III deferred to 2027-12-02 but questionnaires ask now)*
  — **partial.** The causal ledger and `AuditTrailMiddleware` exist; OTel is
  LangSmith-bound with zero `gen_ai.*` attributes. Extend the causal ledger into a
  hash-chained per-run JSONL (each event carries sha256(prev)) with
  approval-decision, Expert verdict and cost events, a retention policy and a
  signed export reusing the TraceFile Ed25519 signer; a vendor-neutral OTLP
  exporter emitting GenAI-semconv spans for model/tool/middleware/subagent with
  cost attributes (LangSmith becomes one exporter); durable per-run usage records
  aggregated per user/model/job daily and pushed over OTLP + CSV from the daemon.
  Completes v2 #38. **E, M.**

### Tier 3 — Moonshots and long-tail differentiators

- **#60 Native Windows sandbox — committed 1.x headline (decision 2026-09-04)** *(Codex restricted-token + WFP design, May 14;
  Claude Code: WSL2 only, issue closed "not planned"; Antigravity AppContainer
  claim undocumented)* — **absent.** A Windows launcher in `local_sandbox.py`:
  unelevated mode = `CreateRestrictedToken` (write-restricted + synthetic SID)
  with explicit ACL grants on working dir / temp / caches / `writable_roots`;
  elevated mode = two low-privilege local users with a WFP block-all rule or
  egress forced through the existing CONNECT allowlist proxy; secret-env
  stripping and read-deny paths reused; selected by `SandboxConfig.build_local_sandbox`
  on win32; `doctor --windows`; a published per-OS support matrix and CI badge;
  later, credential masking at the proxy (per-session sentinels swapped only
  toward allow-listed hosts, per Claude's `mode: mask`). Completes v2 #22 and is
  the prerequisite for #57's Windows workers. **S/E, XL.**
- **#59 Scan jobs with a findings ledger and remediate → PR** *(Devin scheduled
  code scans + batch remediation; Codex Security open-sourced client, HN praised
  the harness not the scanner; Claude Security plugin)* — **absent.** A `scan`
  job kind on `AmbientJob` (profile: security/cleanup/perf/custom rubric); a
  SQLite findings table keyed by a stable fingerprint so re-runs update rather than
  duplicate; triage states with `/findings` + headless twin; sandbox reproduction
  marks findings validated/unreproduced; SARIF output and CI gating; `--max-cost`;
  `/remediate <id>` opens a PR with the evidence bundle. Packages `/audit`,
  `/compliance`, `/jury` and the ledger into the workflow Devin sells. **T/E, M.**
- **#65 Protocol currency: MCP 2026-07-28, A2A v1, ACP registry, context engine as
  a service** *(MCP went stateless and deprecated sampling/roots/DCR on a 12-month
  clock; ADK 2.8 and MAF shipped A2A task mode; Zed's ACP registry is live;
  Augment sells its index as an MCP server)* — **absent.** Bump the `mcp` SDK
  (pinned 1.28.1 = protocol 2025-11-25) once a 2026-07-28 release exists and honour
  stateless sessions, cacheable tool lists with deterministic ordering, the Tasks
  extension, `input_required` elicitations routed into the HITL dialog,
  `destructiveHint`-gated approvals; `bog-agents serve --a2a` on `a2a-python 1.1.x`
  (Agent Card, streaming, task mode) + a `RemoteA2AAgent` subagent backend;
  publish `bog-agents-acp` to PyPI and list it in Zed's ACP registry (needs v6
  SAT-3); expose the `@codebase` hybrid index over `bog-agents mcp-server` so
  Claude Code / Codex users can mount bog as their context engine. Completes v2
  #41 and v1 #20. **T/E, L.**
- **#69 Plan review screen + headless `--plan --auto`** *(Kiro V3 spec review with
  staged line comments and scoped execution; Copilot CLI `--plan --mode autopilot`)*
  — **partial.** `/plan` is a tool-hiding toggle; butcher has a yes/no modal. A
  shared `PlanReviewScreen` for butcher manifests, JTBD job specs and plan-mode
  output: line-addressed comment staging → one consolidated revision prompt →
  re-plan loop; per-slice checkboxes written back into `ButcherJob`; a
  full-screen execution view reusing `dashboard.py` panels; `bog-agents --plan
  "<prompt>" --auto` in `non_interactive.py`. **S/T, M.**
- **#70 Security-scan recipe** *(Codex Security harness; Claude Security plugin)*
  — **partial** (`/audit` is one prompt; a `dependency-audit` recipe exists).
  Architecture map → threat model → parallel hunter subagents on `TaskLedger`
  (injection, authz, secrets, SSRF, deserialization) → independent `/jury`
  review → sandbox reproduction → persistent findings + false-positive ledger with
  dedup keys, `--max-cost`, SARIF, CI gate. Shares the findings store with #59.
  **T/E, M.**
- **#73 Agent-authored workflows saved as `/commands`** *(Grok Build Workflows:
  128–1,024 agents, saved as slash commands; Claude dynamic workflows; Warp
  Factories foreman pipeline)* — **partial.** `pipeline.py` has sequential YAML
  pipelines. A `workflow.py` schema (phases context → work → review → verify →
  synthesize; each phase a team fan-out under `RunawayCaps`); an agent tool
  `author_workflow(description)` that writes `.bog-agents/workflows/<name>.yaml`
  (repo-committed, loaded as `/name [args]` via the `prompt_commands.py` loader);
  a runner persisting phase state so pause/resume skips finished phases;
  per-agent token meters and a hard per-workflow budget. Optionally the
  code-flavoured variant: a `workflow` tool taking a small sandboxed Python script
  (`spawn`, `gather`, `validate(rubric)`, `retry`) executed over `TaskLedger` with
  typed outcomes. Absorbs v2 #35's "recipes v2" run-time half and v1 #19. **T/S, M.**
- **#75 Memory rebuild + advisor tool** *(Managed Agents "Dreams" research preview;
  Kiro Crew durable lessons; Pydantic harness Advisor)* — **absent.** A
  `memory_rebuild.py` batch (pure logic, injected `invoke`): load the store + N
  transcripts from `sessions.db`, run a steerable consolidation (dedup,
  contradiction resolution, provenance kept), emit a candidate store under
  `.bog-agents/memory.rebuild/`, show a diff, swap only on approval,
  daemon-schedulable, local-model friendly. Plus an `ask_advisor` tool that sends
  one bounded question to the operator's `hard` tier and returns a ToolMessage,
  counted and capped — the cheap-loop / expensive-question pattern. Grounds v2
  #45. **S/T, M.**
- **#76 Team v2: file transfer, cross-thread mailbox, multi-repo, fast spawn**
  *(Amp agent-to-agent file transfer + multi-repo projects; Cursor multi-dir
  workspaces; Grok `grok clone` content store; Cursor Builds 3× faster start)* —
  **partial.** Typed `Attachment` on `Message` (file/dir/patch, DLP-scanned,
  audit-logged) + `send_file`/`receive_files` teammate tools; `Mailbox` persisted
  to SQLite keyed by thread so messages cross sessions; `/add-dir` extending
  `CompositeBackend` mounts; `[worktree] reuse = ['node_modules', '.venv']` in
  `sandbox.toml` → hardlink/junction from a lockfile-hash-keyed cache under
  `~/.bog-agents/envcache/`; Docker/daytona snapshot templates recorded in
  `.bog-agents/sandbox.lock`. Extends v2 #21 and #27. **T, M.**
- **Also noted, not ranked:** on-device voice dictation (Kiro, Copilot; S — every
  CLI shipped it, cheap with `faster-whisper`); temporal/sequence-aware Expert
  predicates with a Dogwood importer (AWS, Aug 6; L — the enterprise moonshot
  inside #50); live preview portals with tunnel + evidence link (Amp Portals; M);
  desktop-in-sandbox computer use on Windows workers (Amp Desktop Sep 4; L, after
  #57); `/doctor` that fixes (context-cost audit of skills/MCP/plugins, slow-hook
  detection, corporate-proxy check; M); `/debug` and `bog blame` (v2 #43/#44,
  still unbuilt, still unowned by any terminal agent).

### Deferred / superseded this cycle

- **v2 #26** → superseded by #71 (the treadmill needs CI, not a document).
- **v2 #33 teleport, #39 durable runs** → absorbed into #56.
- **v2 #34 importer, #35 recipes v2** → absorbed into #62 and #73.
- **v2 #36 CodeAct** → promoted to #72 (now table stakes).
- **v2 #40 versioned AgentSpecs** → keep deferred; the daemon's `AmbientJob` +
  `sandbox.lock` cover the ops need until #57 exists.
- **v2 #42 fleet** → the self-hostable half is #57; the hosted control plane stays
  post-1.0.
- **v2 #46 effort ladder** → unblocked (all deps shipped); its cost half moved into
  #53. Keep the algorithm-ladder half as a follow-up once #51 lands.

### Sequencing v3

**Gate: REVIEW v6 Wave 0** (the six P1s, `available=False` + a feature self-test,
the deepagents CI leg, the daemon/SDK docs drift). Every Tier-1 item below stands
on code the v6 findings touch.

- **Wave 1 — weeks, mostly S/M, mostly wiring what exists:** #47 Governed Auto
  Mode → #51 cost certainty → #52 usage strip → #66 changes tray → #62 Agent
  Plugins 1.0 + import → #49 steerable approvals + hostile-repo hardening → #54
  lean profile + published overhead → #61 Windows distribution → #68 `/tasks`.
  *Outcome: a first-30-minutes experience that beats Claude Code on approvals,
  cost visibility and switching cost, on Windows.*
- **Wave 2 — the differentiators:** #55 daemon execution + subscriptions → #67
  evidence on every PR → #48 trust profiles + `--restricted` → #56 detach/attach +
  queue → #53 cost-objective routing → #64 hook bus v2 → #71 parity + fork → #74
  compliance artefact → #72 Code Mode → #50 managed governance.
  *Outcome: the "agent follows what it creates" and "proof beats diff" stories are
  true unattended.*
- **Wave 3 — the flank:** #60 Windows sandbox → #57 workers (Windows included) →
  #63 governed foreign harnesses → #65 protocol currency → #58 Slack decisions →
  #59/#70 scan jobs + security recipe → #73 agent-authored workflows → #69 plan
  review → #75 memory rebuild → #76 team v2.
  *Outcome: the only self-hostable, Windows-capable, policy-provable control
  plane in the category.*

**1.0 line (decided 2026-09-04):** Wave 0 + Wave 1 + Wave 2 + the stability
contract. 1.0 ships only when the daemon executes unattended, evidence lands on
every PR and trust profiles exist; Wave 3 is the post-1.0 moat, with two items
pulled forward by decision: **#60 native Windows sandbox is a committed 1.x
headline** (not a moonshot), and the VS Code extension is fixed and published
rather than deleted (REVIEW v6 SAT-4). ACP stays source-only for now; harbor
stays an eval harness.

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
