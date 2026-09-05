# REVIEW.md v6 — Current-State Audit + Killer-Feature Cycle (2026-09-04)

> **Scope this cycle (agreed 2026-09-04):** feature-heavy, light audit. v5's full
> re-audit was two weeks old, so the audit half re-scored v5's deferred items and
> every ROADMAP v1/v2 entry against code and spot-checked the 20 commits since
> 2026-08-19; it did not hunt for new P0/P1 across every package. The feature
> half — five competitor buckets, 30 products, 85 candidates novelty-checked
> against the code — lives in `ROADMAP.md` § "Killer features v3". Target users
> for 1.0, in priority order: solo power users / OSS (Windows-first, local
> models, cost-sensitive), small dev teams, enterprise / regulated. SDK-DX-only
> work is deliberately low priority.
>
> **Method:** 16 agents in one workflow — three package inventories (SDK, CLI,
> satellites/delivery) each followed by an adversarial refuter for every claimed
> P0/P1; five competitor researchers (blind to the code) each followed by a
> novelty checker that grepped the tree; plus an inline completeness pass over
> four products the buckets skipped. Every code claim below cites a `path:line`
> an agent opened; every competitor claim in the ROADMAP carries a URL and date,
> and each research file lists what could not be verified. Tests run during the
> audit: SDK full unit suite (2,926 passed / 148 skipped / 2 xfailed, 70 s,
> Windows), daemon (233 passed), targeted CLI (129 passed), acp 52, harbor 61,
> daytona 5. Local checkout was 0.9.12 + one commit; `origin/main` is 0.9.13
> (release 2026-08-24) with no functional diff, so line numbers are HEAD-of-checkout.

## 1. Where we stand (2026-09-04, main @ 0.9.13)

Three inventory agents read every package against the "wired end-to-end + tested" bar; every P0/P1 they raised went to an independent refuter. Headline: **the correctness center held — the SDK suite is fully green on Windows (2,926 passed), the daemon suite is green (233), the six risky post-v5 commits are cleanly applied with regression tests, and every v5 Wave A–F fix verified in place.** What did *not* move is the feature surface: of the 26 roadmap items re-scored, four advanced (#21 teams and #31 best-of-N shipped; #29 evidence and #30 assign-to-bog reached "partial"), two regressed to "stale" (#26 deepagents parity — GA passed five weeks ago unscouted; the drop-in badge is now unverified against what users install), and twenty are byte-for-byte where July left them, including both Tier-1 table-stakes items with CLI surfaces (#23 trust profiles, #28 changes tray).

| Package | Health | One line |
|---|---|---|
| **SDK core** | Strong | `create_agent`/FeatureConfig/builder/deepagents-compat all green. Two honesty gaps: FeatureConfig has 80 fields (CLAUDE.md says ~150) and the headline enterprise middleware (ExpertRules, StopGate, Rubric, GoalTools, EvidenceBundle, Guardrails, LangSmith) has **no FeatureConfig field** — an SDK consumer must hand-assemble `middleware=[…]` and know the load-bearing order. |
| **SDK context** | Strong cores, two unrealized promises | Sweeper, overflow-heal, output-truncation, deferred-tools are coherent and tested. But the sweeper's two promises do not hold on either real surface: plain `agent.invoke()` mints a fresh thread id per call so `recall_swept` never finds the offload (new SDK-D2), and the CLI still splices it inner of summarization so savings never defer compaction (CTX-2, open). |
| **SDK safety** | Well-built, three P2s still open | Wave-B fixes verified. Expert-rules still fails *open* on a first-load parse error (SAFE-3); RBAC still enforces only at request time with no tool-call re-check (SAFE-4); LLM guardrails silently skip on the sync path (SAFE-5). The self-mod guard's authority list is CLI-only — pure-SDK consumers get none. |
| **SDK orchestration** | Primitives real, wiring partial | `TaskLedger`/`Mailbox`/`run_team`, `CostLedger`/`RunawayCaps`, `EvidenceBundle.merge_ready` are pure, tested logic. But `CostLedger` is consulted **only** by `run_team` — the main agent's `task` subagents spawn uncounted (violates the CLAUDE.md invariant; #25 stays partial), and `EvidenceBundleMiddleware` is reachable from nothing (no FeatureConfig field, no CLI flag, no daemon dispatch). |
| **SDK observability** | LangSmith-bound | Cost tracker + audit trail shipped. OTel exports only through LangSmith's exporter; zero `gen_ai.*` attributes; no OTLP endpoint option; no test drives an enabled OTel path (#38 not started). |
| **SDK backends** | Mature | Atomic writes, symlink refusal, background shell, PTY (ConPTY on Windows), egress proxy, bwrap/seatbelt sandbox all verified. **No Windows sandbox launcher** — `require_sandbox=True` fails closed on the project's own primary platform, and README markets "OS-level sandbox" without the caveat. |
| **serve** | One P1 | `/stream` still never records the assistant reply (SDKC-2, confirmed live): every multi-turn SSE conversation on the default checkpointer-less wiring replays a user-only transcript. No A2A, no SDK-side MCP transport. |
| **CLI commands** | 118 working / 7 partial / 3 dead of 129 | The three v4 dead commands are dead for the **third consecutive cycle**: `/think` scans a `self._middleware` that is never assigned; `/checkpoint load` sends a prose "please resume" prompt and restores nothing; `/worktrees cancel` flips a status field while the worker keeps editing. `/help` still advertises all three, and the registry has no `available=False` mechanism. |
| **CLI turn lifecycle** | Choke point holds for 4 of 13 | `_start_tracked_session` + 11 regression tests make the v5 pump class structurally impossible for `/butcher`, `/team run`, `/best-of-n`, `/jury` and operator escalation. **Nine model-calling commands still bypass it** — `/orchestrate`, `/race`, `/sidecar`, `/squad`, `/imagine`, `/devil`, `/handoff`, `/rubric draft`, `/teach` — so Esc does nothing and a typed prompt starts a concurrent turn against the same files. |
| **CLI first-30-minutes** | Rough for the target user | Anthropic auto-default is Opus (most expensive tier) with no cost hint; the wizard has no OpenRouter/xAI/LM Studio/Azure entries; `/auto`'s risk judge is hard-bound to the `anthropic` package so Ollama/OpenAI/Bedrock-only users get rules-only auto mode while `--help` promises "Haiku eval"; `/diff` is monochrome raw text while edit widgets are coloured; the documented `bog-agents command "/help"` breaks under Git Bash on Windows (MSYS mangles the slash). |
| **CLI god class** | 17,873 lines | 32 of 124 handlers (26%) have no controller module; six are ≥60-line inline implementations (`/diff`, `/branch`, `/undo`, `/worktree`, `/qa`, `/peat`). |
| **daemon** | Engine solid, execution hollow | 233 tests green; cron catch-up, watchdog, retry, quarantine, orphan reconciliation, fail-closed HMAC, shell-less unattended backend all real. But `_build_prompt` returns `job.prompt` **verbatim**: `trigger_context` (issue number/body/branch from the GitHub front door, CI payload from webhooks) never reaches the model. The quickstart's three canonical patterns rely on `{trigger_context_json}`/`{pr_number}` placeholders and a `--file-change` flag that do not exist, and every REST example uses `Authorization: Bearer` while the API accepts only `X-Daemon-Token`. #30 is a 14-test parser in front of nothing. |
| **Satellites** | Unshippable | ACP: one agent/cwd/cancel flag shared across sessions (open since v3). VS Code: sidebar view declares a provider that is never registered, each reply overwrites the last, `autoApprove` is inert, **Marketplace listing 404** while README says "search the extensions panel". Daytona: distribution named `langchain-daytona`, which on PyPI is langchain-ai's package — bog's can never be published as-is and its README installs the wrong thing. Harbor still calls the deprecated `als_info` scheduled for 1.0 removal. |
| **Delivery** | Good release engineering, no distribution | release-please linked versions, OIDC PyPI publish, blocking lock-check, six-package relock loop — all verified. But: no installer (winget/scoop/brew/MSI/one-liner), no Dockerfile or compose, no GitHub App, fork PRs get **zero CI** (every job gated to in-repo branches on self-hosted runners), py3.11 "temporarily disabled" while every package advertises it, no macOS leg for macOS-specific code (launchd, seatbelt, PTY). `bog-agents daemon install` on Windows silently writes a systemd unit. |

**Adoption signal:** public since 2026-03-16; 3 stars, 0 forks, 0 watchers, 0 issues, 0 external PRs. This is a pre-adoption codebase with a post-adoption feature count. Every choice below is weighed against that fact.

### 1.1 What is genuinely world-class (verified, under-marketed)

1. **Street Sweeper** — per-call, lossless-first context *view transformation* that never changes message count or order, so it composes with summarization cutoffs and prompt-cache prefixes by construction; every sweep priced in USD from the real catalog; originals recoverable. No comparator ships continuous pruning that stays cache-stable.
2. **Expert Mode** — a forward+backward-chaining rule engine at the tool-call boundary with `/why` explanations and `/prove` proof trees. Every competitor's auto-approval is a classifier plus prose; nobody else has a policy engine you can *prove things about*.
3. **Governed multi-agent primitives that no OSS CLI ships together** — `/team run` over an atomic dependency-aware `TaskLedger` + `Mailbox` under `RunawayCaps`; `/best-of-n` with real worktree attempts and a rubric judge; `/jury`; `/butcher` slice-and-verify on weak models — all tracked, cancellable, tested with injected invokers.
4. **Layered deterministic approval before any LLM judge** — `ask_list → git_ops classifier → exec_risk → bash_hygiene → Haiku`, failing toward "ask"; a permission cycle that cannot reach bypass by accident.
5. **Middleware ordering as a tested contract** — canonical-order test, `requires:` validated at build time, rendered-prompt snapshots pinned.
6. **A family of resilience primitives with declared failure directions** — exec-risk (toward prompting), stop-gate (fail-open, bounded), background shell (never kill; Windows `taskkill /T`), output-truncation merge, deferred tools, semantic coercion — all model-free and unit-tested.
7. **Session archaeology** — FTS5 `/threads search` (~5 ms at 1,000 threads), `/rewind` to any checkpoint, `/btw` sidechains, signed TraceFile export/verify. Stronger recall than Claude Code's `--resume`.
8. **Backend hardening most frameworks skip** — sibling-temp + fsync + atomic replace with symlink refusal on every write; bwrap/seatbelt with `--unshare-net` hard cut or a label-boundary CONNECT allowlist proxy; `require_sandbox` fails closed.
9. **Daemon reliability posture** — shell-less backend for every unattended trigger, fail-closed HMAC, croniter catch-up, quarantine, orphan reconciliation — further than most OSS agent daemons.
10. **Cross-ecosystem compatibility as a feature** — Claude Code and Cursor hook files load unchanged; Claude skills/commands/MCP configs import; MCP OAuth through the SDK's `OAuthClientProvider`; managed ripgrep with per-platform SHA pins and an offline probe.
11. **Editor-grade input** — pure vim state machine through Textual's undo stack, `ctrl+x` external editor, registered theme system, a `--drive` YAML harness that scripts the real TUI with a replay model for CI.
12. **Hybrid memory with zero heavy deps** — FTS5 BM25 fused with an injected embedder and pure-Python cosine, decay, MMR; a rebuildable cache over hand-editable Markdown.

### 1.2 The three structural findings

**F1 — "Shipped" still means "has a module", not "reachable by a user."** Evidence bundle (no FeatureConfig field, no flag, no dispatch), cost ledger (counts teams, not `task` subagents), sandbox.toml's GitHub-Action consumer, `/think`, `/checkpoint load`, `/worktrees cancel`, the daemon's `trigger_context`, the VS Code sidebar. The v4 recommendation for a `/doctor --features` self-test that exercises every advertised surface was never built; three cycles later the same three commands are dead and `/help` still promises them.

**F2 — The parity treadmill stalled.** deepagents 0.7.0 went GA on 2026-07-24 and is at 0.7.13; the SDK tracks 0.7.0b2 and does not even install deepagents in its own venv, so CI cannot catch a break. The "drop-in" claim on the README is currently unverified against every version a user can install.

**F3 — Depth is trapped on the laptop, and now also trapped on POSIX.** The roadmap's own thesis (distribution is the unlock) is unchanged, but the market moved to execute-on-your-machines with no Windows worker anywhere — and bog, the Windows-first project, has no Windows sandbox, a daemon installer that writes systemd units on Windows, a headless surface that breaks under Git Bash, and no installer of any kind.

---

## 2. Findings — fix / refine / upgrade backlog (light audit, adversarially verified)

IDs are `v6 <ID>`. Every P1 below survived an independent refutation pass with a live or code-traced reproduction; P2s are recorded from the inventories without a refuter (severity provisional). Prior-cycle IDs are cross-referenced so commit messages can say "fixes v6 SDK-1 (= v5 SDKC-2)".

### P0 — none this cycle

### P1 — confirmed (6)

| ID | Package | Defect | Effort | Prior |
|---|---|---|---|---|
| **v6 SDK-1** | serve | `POST /stream` never records the assistant reply; on the default checkpointer-less wiring every multi-turn SSE conversation replays a user-only transcript while `/history` implies continuity (`serve.py:331`). Live-reproduced. | M | = v5 SDKC-2 |
| **v6 DMN-1** | daemon | `_build_prompt` returns `job.prompt` verbatim (`runner.py:160-186`); `trigger_context` (GitHub issue number/body/branch, CI webhook payload, changed file path) is stored on the run record and **never reaches the model**. The quickstart's three canonical patterns depend on `{trigger_context_json}`, `{pr_number}`, `{date}`, `{trigger_path}` placeholders and a `--file-change` flag that do not exist; `--output-github-issue "{pr_number}"` is an argparse int error. | M | new (root of #30 "hollow") |
| **v6 DMN-2** | daemon | `/webhooks/github` (assign-to-bog front door) parses the event and dispatches, but no `jobs create` flag can create a `github` trigger (REST only), README/quickstart never mention it, and — because of DMN-1 — the agent never learns which issue it was assigned. | M | new; same root as DMN-1 |
| **v6 CLI-1** | CLI | **`self._middleware` is never assigned on `BogAgentsApp`**, so every handler that introspects it is dead: `/think` (`app.py:10561`) always prints "ThinkingMiddleware is not active", and **all of `/worktrees`** — spawn, status, merge, cancel (`app.py:12356-12369`) — prints "ParallelWorktreeMiddleware is not active". `create_cli_agent` returns only `(graph, backend)` while `agent.py:1929-1939` appends ThinkingMiddleware to a local list. `/help` advertises both. Third cycle open; the refuter found the shared root cause. | S | = v4 P1-25 + P1-32 |
| **v6 CLI-2** | CLI | `/checkpoint load` resolves the checkpoint then sends a prose "resume from checkpoint" prompt and restores nothing (`app.py:8388-8395`); `_resume_thread` (`app.py:17443`) is the real switch path and is never called. The model confabulates a resume. Third cycle open. | M | = v4 P1-27 |
| **v6 CLI-3** | CLI | Nine model-calling commands run inline on the App pump and never register with TurnManager (v5 Wave A covered only four): `/orchestrate` (`app.py:12218`), `/sidecar` (:12253), `/race` (:5946), `/imagine` (:10918), `/devil` (:10974), `/handoff` (:10677), `/squad` (:10966), `/rubric draft` (:11933), `/teach` (:6149). Esc does nothing; a typed prompt starts a concurrent turn against the same files. | M | = v5 CLIC-2 class, residual |

### Downgraded to P2 by the refuters (real, bounded or already-decided)

- **v6 SAT-1** (was P1) — daemon docs say `Authorization: Bearer`; the API accepts only `X-Daemon-Token` (`api.py:200`); README lists a nonexistent `GET /metrics` and the wrong token path. Every documented REST example returns 401. (S)
- **v6 SAT-2** (was P1) — `libs/partners/daytona` is named `langchain-daytona`, which on PyPI is langchain-ai's deepagents package; bog's can never be published as-is and its README installs the upstream package. (S: rename to `bog-agents-daytona`)
- **v6 SAT-3** (was P1) — ACP server shares one `_agent`/`_cwd`/`_cancelled` across sessions (`server.py:94-108,429`). Deferred by decision in v4 and v5; keep deferred or fix with a per-session dict (M). = v5 SAT-4 / P1-61
- **v6 SAT-4** (was P1) — VS Code: `bog-agents.chatView` declared but no `WebviewViewProvider` registered; `dataset.streaming` never cleared so each reply overwrites the last; `autoApprove` inert; Marketplace listing 404 while README says "search the extensions panel". (M) = v5 SAT-6

### P2 — important, not urgent (unverified by design)

**SDK context / perf**
- **v6 SDK-2** (new) — `_get_thread_id` mints `session_<uuid4>` per call when no `configurable.thread_id`, so offload write and `recall_swept` read target different files: plain `agent.invoke()` with the sweeper on can never recall (`street_sweeper.py:785`). (S)
- **v6 SDK-3** — CLI splices the sweeper inner of summarization; sweep savings never defer compaction (`cli/agent.py:2091`, `graph.py:1430`). (M) = v5 CTX-2
- **v6 SDK-4** — offload rewrites the entire ever-growing file each call and writes `swept_context/` into the user's CWD (`street_sweeper.py:855`). (M) = v5 PERF-4
- **v6 SDK-5** — memory vector mode embeds N paragraphs serially with `embed_query` at agent build (`tools/bundles.py:417`); 200 round-trips before the first prompt. (S) = v5 PERF-5
- **v6 SDK-6** — `import bog_agents` eagerly imports the graph stack: 2.76 s warm / 19.9 s cold / 2,335 modules on Windows, ~5% worse than v5 (`__init__.py:10`). (M) = v5 PERF-6

**SDK safety / governance**
- **v6 SDK-7** (new; CLAUDE.md invariant violation) — `CostLedger`/`RunawayCaps` consulted only by `teams.run_team`; `SubAgentMiddleware`'s `task` tool and `AsyncSubAgentMiddleware` register no spawns, so `max_subagent_spawns` never fires on the default fan-out path (`subagents.py:302`). (M)
- **v6 SDK-8** — expert rules fail *open* on a first-load parse error: a tab in one YAML file silently disables the whole deny policy with a WARNING (`expert_rules.py:301`). (S) = v5 SAFE-3
- **v6 SDK-9** — RBAC enforces pinned roles only at request time; a hallucinated/injected `execute` tool call still runs because the tool node keeps it bound (`rbac.py:423`). (M) = v5 SAFE-4
- **v6 SDK-10** — `GuardrailMiddleware` silently skips async-only guardrails (all `LLMGuardrail`s) on the sync `.invoke()` path at DEBUG level (`guardrails/middleware.py:87`). (S) = v5 SAFE-5
- **v6 SDK-11** — `EvidenceBundleMiddleware` is reachable from nothing: no FeatureConfig field, no CLI flag, no daemon dispatch. (S to wire) — feeds #29
- **v6 SDK-12** — deepagents parity tracks 0.7.0b2; PyPI is 0.7.13 (13 patch releases incl. a breaking `handoff→isolated` rename); deepagents is not installed in the SDK venv so CI cannot catch a break. (M) — feeds #26
- **v6 SDK-13** — CLAUDE.md drift: FeatureConfig is 80 fields not ~150; "new features get a FeatureConfig field" is false for ExpertRules/StopGate/Rubric/GoalTools/EvidenceBundle/Guardrails/LangSmith; untracked empty `libs/partners/runloop/`; stale "Feature #74 A2A" label in `code_intelligence.py:16`. (S)

**CLI first-run / UX**
- **v6 CLI-5** — `/diff` mounts raw `git diff` as a plain `AppMessage` (monochrome, Rich-markup risk) while edit widgets use `DiffMessage`/`EnhancedDiff` (`app.py:8098`). (S)
- **v6 CLI-6** — Anthropic auto-default is `claude-opus-4-7` (most expensive) with no cost hint in wizard or banner (`provider_catalog.py:66`). (S)
- **v6 CLI-7** — Bedrock wizard branch saves the catalog-preferred spec without the hittability probe the auto-detect path uses, and its hard fallback is the retired `claude-sonnet-4-20250514` id (`config.py:1932`; also `main.py:404` help text). (S)
- **v6 CLI-8** — `bog-agents command "/help"` is MSYS-mangled under Git Bash on Windows (`/c:/program is not available…`); the bare form works but no error or `--help` says so; `cmd_daemon.py:75` already has `_recover_msys_path`. (S)
- **v6 CLI-9** — `/auto`'s risk judge is hard-bound to the `anthropic` package + `claude-haiku-4-5` (`auto_mode.py:455-490`); OpenAI/Ollama/Bedrock-only users get rules-only auto mode while `--help` promises "Haiku eval". (M)
- **v6 CLI-10** — `/sidecar` ships with no parent context despite its docstring (`app.py:12247`). (M)
- **v6 CLI-11** — `/race` is a bare chat-completion fan-out (`race.py:137`) while its docstring and ROADMAP #31 describe a worktree fleet. (M)
- **v6 CLI-12** — `/help` truth: three advertised behaviours are not real and the registry has zero `available=False` entries — no mechanism exists to mark a command not-yet-working. (S)
- **v6 CLI-13** — wizard offers five providers; OpenRouter/xAI/LM Studio/Azure only via `-M` incantations. (S)

**daemon / delivery / satellites**
- **v6 DMN-3** — `bog-agents daemon install` on Windows writes a systemd unit under `%USERPROFILE%\.config\systemd\user\` and prints `systemctl` instructions; README says "no Windows service installer yet" (`cmd_daemon.py:459`). (S)
- **v6 DEL-1** — optional-extra hints name extras that do not exist: `[acp]`, `[modal]`, `[daytona]`, `[runloop]` (real: `modal-sandbox`, `daytona-sandbox`, `runloop-sandbox`; no `acp` extra; `bog-agents-acp` unpublished); ACP README uses `uv upgrade` (not a verb). (S)
- **v6 DEL-2** — SDK quickstart imports `DaytonaBackend, ModalBackend` from `bog_agents.backends` (neither exists); README calls `bog-agents drive` (only `--drive PATH` exists). (S)
- **v6 DEL-3** — CI: py3.11 "temporarily disabled" while every package advertises it; every job gated to in-repo branches on self-hosted runners so **fork PRs get zero checks**; no macOS leg; VS Code extension has no PR compile/lint leg. (M)
- **v6 DEL-4** — VS Code `buildChildEnv` still strips `HTTPS_PROXY`/`HTTP_PROXY`/`NO_PROXY`/`SSL_CERT_FILE`/`REQUESTS_CA_BUNDLE`; corporate-proxy users get connection errors from the child CLI while the terminal works. (S) — residual of v5 SAT-2
- **v6 SAT-5** — harbor still calls deprecated `als_info` (scheduled for 1.0 removal) and `asyncio.get_event_loop()`; README's `cp .env.example .env` targets a file that does not exist. (S) = v5 SAT-8

### Systemic themes (what the P1/P2 clusters have in common)

1. **Advertised ≠ reachable.** CLI-1/2/12, SDK-11, DMN-1/2, SAT-4, DEL-1/2. Fix pattern: a **feature self-test** (`bog-agents doctor --features`) that constructs every advertised command/middleware and drives it once with a replay model; `available=False` in `SlashCommandSpec` with a drift test that every advertised subcommand has a handler branch; a docs drift test that resolves every documented command/flag/env/placeholder against argparse and the manifest.
2. **One choke point, incomplete coverage.** CLI-3 is v5's theme 1 again: the fix (`_start_tracked_session`) exists; nine surfaces never traverse it. Fix pattern: a `runs_model=True` flag on `SlashCommandSpec` plus a test that fails when a handler body awaits a model-calling callable without the choke point.
3. **Governance counted in one place.** SDK-7 (ledger counts teams only), SDK-8/9/10 (rules/RBAC/guardrails enforce on one path). Fix pattern: enforce at the tool-call boundary and count at the spawn site, with build-time coverage assertions.
4. **Windows-first project, POSIX-shaped edges.** No sandbox launcher, systemd unit written on Windows, MSYS-mangled headless surface, no installer. Fix pattern: a Windows leg in every delivery decision (see §3, features #47/#57).
5. **Parity treadmill needs CI, not a document.** SDK-12. Fix pattern: a CI leg that installs `deepagents` latest and runs the 24 compat tests plus the deepagents smoke import.

---

## 3. Agreed sequencing (this cycle)

### Wave 0 — Land this report + make "advertised" mean "reachable" (days)
1. Fix the six P1s: **v6 CLI-1** (assign `self._middleware` from `create_cli_agent`; resurrects `/think` *and* all of `/worktrees`), **CLI-2** (`/checkpoint load` → `_resume_thread`), **CLI-3** (route the nine inline model-calling commands through `_start_tracked_session`; add the `runs_model` guard test), **SDK-1** (`/stream` records the assistant reply), **DMN-1/DMN-2** (template `trigger_context` into the prompt; `jobs create --trigger github`; fix the quickstart placeholders and the `Authorization: Bearer` drift).
2. `available=False` on `SlashCommandSpec` + a drift test that every advertised subcommand has a handler branch; `bog-agents doctor --features` that constructs every advertised command/middleware and drives one replay-model turn (the v4 §4.1 recommendation, third time proposed — make it a Wave 0 exit criterion).
3. deepagents CI leg (install latest, run the 24 compat tests + smoke import) — **v6 SDK-12**.
4. Docs drift: **SAT-1**, **DEL-1**, **DEL-2**, **SDK-13**, the daytona rename (**SAT-2**).

### Wave A — Governance counted everywhere (S/M)
**SDK-7** (count `task`/async subagent spawns and web searches in `CostLedger`), **SDK-8** (expert rules fail closed on first-load parse error), **SDK-10** (guardrails: raise, don't skip, on the sync path), **SDK-11** (wire `EvidenceBundleMiddleware` into FeatureConfig + `--pr`), **CLI-9** (provider-agnostic `/auto` judge). These are the prerequisites for ROADMAP #47/#51/#67.

### Wave B — First-30-minutes polish (S each)
**CLI-5** (coloured `/diff`), **CLI-6** (Sonnet-class default + cost line), **CLI-7** (Bedrock wizard probe + retired id), **CLI-8** (Git Bash headless), **CLI-12/13** (help truth; wizard providers), **DMN-3** (Task Scheduler on Windows), **DEL-4** (proxy env vars in VS Code child).

### Wave C — Context/perf tail (M)
**SDK-2** (stable thread id for the sweeper), **SDK-3** (= CTX-2, splice the sweeper outer of summarization), **SDK-4** (= PERF-4, write-once offload outside CWD), **SDK-5** (batch embeddings), **SDK-6** (= PERF-6, lazy `bog_agents` import).

### Wave D — Delivery truth (M)
**DEL-3** (fork-PR CI on hosted runners for lint + one test leg; re-enable 3.11 or drop the classifier; a macOS leg for launchd/seatbelt/PTY; VS Code compile leg), **SAT-4** (VS Code sidebar provider + streaming fix, then **publish to the Marketplace — decided 2026-09-04**), **SAT-3** (ACP per-session state — needed before listing in Zed's ACP registry, ROADMAP #65), **SAT-5** (harbor `als_info`).

## 4. Shipped in Wave 0 (2026-09-04, branch `fix/review-v6-wave0` off `docs/review-v6`)

One commit per finding, each with regression tests. Suites at the end of the
wave: SDK 2,930 passed / 148 skipped / 2 xfailed; CLI 5,616 passed / 118 skipped;
daemon 239; daytona 5; acp 52; `ty` clean on SDK, CLI and daemon; app.py at 17,857
lines (under its 17,900 ratchet — the `/worktrees` logic moved to
`worktrees_controller.py`). `bog-agents --doctor-features` reports 128 commands, 0
problems.

**All six P1s fixed:**
- **v6 SDK-1** — `serve` `/stream` records the assistant reply from the last `on_chat_model_end` event (plain text and content blocks), so checkpointer-less multi-turn SSE conversations replay the full transcript.
- **v6 DMN-1** — `_build_prompt` renders `{placeholder}` references from the trigger (date/time, job fields, trigger type, the context as JSON, every top-level context key, `number`/`pr_number`/`issue_number`, `trigger_path`); `--output-file` and `--output-github-issue` render too (the latter accepts a placeholder string). Quickstart documents the table and the real `--watch-dir` flag.
- **v6 DMN-2** — `bog-agents daemon jobs create --github` creates the GitHub trigger; assembly is a pure `_triggers_from_args`; quickstart gains the "assign an issue to bog" pattern with the secret / bot-login env vars.
- **v6 CLI-1** — root cause confirmed: the TUI's agent runs in the LangGraph server process, so `self._middleware` can never hold in-process middleware. `/think` is now per-session state carried through `CLIContext.thinking_enabled` / `thinking_budget_tokens`, honoured by `ThinkingMiddleware` from `request.runtime.context` on both paths; `/worktrees` fronts the managed worktree tasks behind `/agent spawn --worktree` (status / spawn / merge / cancel are real, cancel stops the worker).
- **v6 CLI-2** — `/checkpoint load` switches to the saved thread via `_resume_thread` instead of prompting the model to "resume".
- **v6 CLI-3** — `_start_model_command` routes `/orchestrate`, `/sidecar`, `/race`, `/imagine`, `/devil`, `/handoff`, `/squad review`, `/rubric draft` and `/teach` through the tracked-session choke point; Esc cancels them and submissions queue.

**Wave 0 items 2–4:**
- **`bog-agents --doctor-features`** (`feature_selftest.py`) — static audit that every registered command has a handler, implements its advertised subcommands (following same-class and module delegation) and depends on no dead `self._middleware` lookup; also a unit test, so the surface cannot drift silently again. `/search index` now delegates to `/index`; `/compress auto|threshold` say honestly that auto-compaction is server-side (**CLI-12**).
- **deepagents CI leg** — overlays the latest `deepagents`, smoke-imports both packages, runs the compat + upstream-parity tests (**SDK-12**).
- **Docs drift** — daemon REST auth header / phantom `/metrics` / token path (**SAT-1**); install hints name extras that exist and say `bog-agents-acp` is unpublished (**DEL-1**); SDK quickstart sandbox example and the `--drive` spelling (**DEL-2**); CLAUDE.md FeatureConfig count and the `middleware=`-only list, stale A2A label (**SDK-13**); daytona distribution renamed to `bog-agents-daytona` (**SAT-2**).

**Corrections to this report from the fix work:** the inventory's claim that `libs/partners/runloop/` contains only `__pycache__` was wrong — it holds an untracked `langchain_runloop` package with its own venv and tests; it was left untouched (untracked, not ours to delete). The registry *does* have an `available` field; every spec sets it `True`, so the finding stands as "no spec was ever marked unavailable".

**Next:** Wave C and Wave D (see §7), then ROADMAP #51 cost certainty on the #47 substrate.

## 5. Shipped in Wave A (2026-09-04, same branch, one commit)

Governance now counts and enforces everywhere it claimed to. Each item carries regression tests; suites re-run green afterwards (SDK / CLI full suites, daemon, `ty` on all three).

- **v6 SDK-7** — `CostLedger` / `RunawayCaps` gate the default fan-out path: `create_agent(cost_ledger=…)` threads the ledger into `SubAgentMiddleware` (the `task` tool) and `AsyncSubAgentMiddleware`; the cost cap is checked first, then each spawn is counted against `max_subagents`, and a refused spawn returns a tool result telling the model to finish with what it has. `create_cli_agent(cost_ledger=…)` forwards it. The CLAUDE.md invariant ("every team/subagent spawn must be counted") is now true; wiring a default session ledger with caps from config is ROADMAP #51.
- **v6 SDK-8** — expert rules fail closed on a first-load parse error: until the rulebook has parsed once, every tool call returns an error `ToolMessage` naming the YAML error (and the `fail_open=True` escape hatch). A parse error *after* a good load still keeps the last good rule set live.
- **v6 SDK-10** — guardrails enforce on the sync path: an async-only guardrail (every `LLMGuardrail`) is driven to completion with `asyncio.run` (or on a worker thread when a loop is already running) instead of being closed and skipped at DEBUG level.
- **v6 SDK-11** — `FeatureConfig(enable_evidence_bundle=True, evidence_check_commands=…)` attaches `EvidenceBundleMiddleware`; `bog-agents -n … --pr --pr-evidence` enables it on the PR-mode agent and appends a proof-of-work section (changed files, test outcome) rendered by the SDK's `render_evidence_markdown` to the PR body. First reachable surface for ROADMAP #29/#67.
- **v6 CLI-9** — the `/auto` risk judge is provider-agnostic: `resolve_risk_judge` builds a reviewer from the active provider (OpenAI → its cheap tier; Ollama / Bedrock / Google / … → the active model; an explicit `provider:model` in `haiku_eval.model` always wins; Anthropic keeps the SDK Haiku path) and `haiku_risk_eval(invoke=…)` uses it, failing closed on any judge error. Help and `/auto` wording no longer promise Haiku regardless of provider.

## 6. Shipped: Wave B + ROADMAP #47 Governed Auto Mode (2026-09-04, same branch, two commits)

**Wave B — first-30-minutes polish** (one commit): **CLI-5** `/diff` renders through the coloured `DiffMessage` (600-line cap; `--stat`/`--name-only` stay plain text with markup escaped); **CLI-6** the Anthropic auto-default is Sonnet-class and the wizard / `/model --default` confirm a model with its $/1M-token price and a `/cost` pointer; **CLI-7** the Bedrock wizard branch runs the hittability probe before saving, and the retired `claude-sonnet-4-20250514` id is gone; **CLI-8** headless commands recover from Git Bash path mangling; **CLI-13** the wizard offers OpenRouter and xAI lanes; **DMN-3** `daemon install` on Windows registers a Task Scheduler task (`--platform windows`, injectable runner); **DEL-4** the VS Code child env passes proxy / TLS variables (compiles clean).

**ROADMAP #47 — Governed Auto Mode** (one commit; the Wave 1 lead, built on Wave A's judge substrate):
- **One batched review per turn.** Everything the deterministic chain leaves `default` is graded by a single review-model call against the user's stated goal, on a `low|medium|high|critical` ladder (`batch_risk_eval`); `high`/`critical` ask a human. Fails closed: a judge error or an ungraded index is `critical`.
- **Every decision is explainable.** `ApprovalLedger` keeps the session's decisions (rule source, verdict, reason, judge); `/auto why [n]` renders them and each one is asserted as an `approval_decision` fact into the client-side expert engine, so `/why approval_decision tool=execute` and YAML rules that react to approvals work. Expert rules still gate every call at the tool boundary, so a YAML deny overrides the reviewer by construction — the `/auto on` message now says so.
- **Circuit breaker.** `breaker_threshold` (default 3) consecutive risky verdicts pause auto mode for the session: calls are marked `paused`, a one-time notice is mounted, `/auto status` shows the state, `/auto on` re-arms.
- **Provider-agnostic for everyone.** `resolve_risk_judge` now also wraps the Anthropic SDK path as a judge, so the batched review has one interface across providers.
- **Auto is the wizard-recommended default.** The first-run wizard asks "Approval mode: auto (recommended) / ask" and writes `auto_mode.enabled` to `~/.bog-agents/settings.json` (new `save_user_section` writer); the CLI honours it when no permission mode or legacy approval flag is given.

Gates after both commits: CLI 5,600+ passed, daemon green, `ty` and ruff clean, app.py at 17,899 lines under its 17,900 ratchet (the `/auto status` rendering moved to `auto_mode.render_auto_mode_status`).

## 7. Shipped: Wave C + Wave D (2026-09-05, same branch, two commits)

Both waves ran in parallel as planned; every gate green afterwards (SDK 2,960 / CLI 5,656 / daemon 243 / acp 53 / harbor 66; `ty` and ruff clean; the VS Code extension compiles, lints and packages).

**Wave C — context / perf tail (SDK-2/3/4/5/6):**
- **SDK-2** `StreetSweeperMiddleware._get_thread_id` mints the fallback `session_<id>` once per instance and caches it, so a plain `agent.invoke` session offloads to, and recalls from, one place.
- **SDK-3** `create_cli_agent` now passes `config=FeatureConfig(enable_street_sweeper=True)` whenever it attaches the `/sweep` singleton, so `_apply_custom_middleware` *replaces* the built-in by name at the canonical slot (inside CostTracker, outside Summarization) instead of splicing it after the core stack. Pinned by `test_custom_street_sweeper_replaces_builtin_in_place` and a CLI test on the `create_agent` call.
- **SDK-4** Offload is one write-once file per marker at `<prefix>/<thread>/<marker>.md` (no more download-and-rewrite of a growing file every model call; a `WriteResult.error` is now treated as a failed write). `recall_swept` reads by marker and lists via `ls`. The CLI routes `/swept_context/` to `~/.bog-agents/swept/` through the composite backend, so nothing lands in the project tree.
- **SDK-5** `memory_search_tool_bundle(embedder=, embed_batch=, db_path=)`: chunk ids are content hashes; nothing is embedded at build time; the first search embeds every vector-less chunk in one `embed_documents` call and stores it; with the per-project `~/.bog-agents/memory_index/<key>.db` a restart re-embeds only changed text, and chunks whose text left the sources are pruned. `HybridMemoryIndex.add` keeps a stored vector when re-adding without one; new `chunks_missing_embeddings` / `set_embeddings` / `prune_except`.
- **SDK-6** `bog_agents/__init__.py` resolves `AgentBuilder`, `AgentConfig`, `FeatureConfig`, `DeepAgentState` and `create_agent` through the existing `__getattr__` map (with a `TYPE_CHECKING` block for static analysis); a subprocess test asserts `import bog_agents` loads neither `langgraph` nor `bog_agents.graph`.

**Wave D — delivery truth (DEL-3, SAT-4, SAT-3, SAT-5):**
- **DEL-3** `ci.yml` gains ungated GitHub-hosted legs: `hosted-lint` (sdk/cli/daemon on ubuntu), `hosted-test` (sdk on ubuntu py3.12 + py3.11, sdk + daemon on macOS — macOS advisory until its first green run), and a `vscode-extension` job that compiles, lints and packages a VSIX artifact on every PR. py3.11 is back in the self-hosted matrix: SDK 2,943 / CLI 5,654 / daemon 243 green on CPython 3.11.15 locally (one order-dependent CLI flake in `test_anthropic_httpx_per_loop` fixed by re-running the idempotent install hook in the test).
- **SAT-4** The activity-bar view is backed by a `ChatViewProvider` (it was declared but never registered); one reply bubble per prompt with `start`/`response`/`done` events (the old stream overwrote the previous answer); `autoApprove` passes `--auto-approve`; context-menu actions open a chat surface and label their bubble; a second prompt while one is running is refused rather than interleaved. Publishability blockers removed: PNG icon (vsce rejects SVG), `LICENSE`, an ESLint config (`npm run lint` failed with "no configuration file", so the release workflow could never pass), version 0.2.0. `vsce package` verified locally. **Still needs the user:** a `VSCE_PAT` secret and the `bog-agents` publisher, then run `VS Code Extension Release` with `publish=true`.
- **SAT-3** ACP `AgentServerACP` keeps a `_SessionRuntime(cwd, agent, cancelled)` per session; `set_session_mode` rebuilds only its own agent, `cancel` flips only its own flag (unknown ids are a no-op), `@file` resources resolve against the session's cwd. Publishing `bog-agents-acp` and the Zed registry listing (#65) remain open — release-please only knows the three core packages.
- **SAT-5** harbor reads `als` (structured `LsResult`; a listing error degrades to "empty"), `run_sync` uses `asyncio.run` with a real generic signature, `.env.example` exists, and the wrapper's prompt context finally has unit tests.

## 8. Shipped: ROADMAP #51 Cost certainty (2026-09-05, same branch, one commit)

Built on the Wave A ledger gating and the #47 substrate; every gate green afterwards (SDK 2,990 / CLI 5,676 / daemon 255; `ty` and ruff clean; app.py 17,892 under its 17,900 ratchet).

- **Budgets that pause, not kill.** `CostTrackerMiddleware(on_budget="interrupt")` is the default: at the cap the next model call raises a LangGraph `budget_reached` interrupt (`spent_usd`, `budget_usd`, `model`, `message`) and loops until the resume value raises the cap above the spend (`parse_budget_resume` accepts `{"budget_usd": N}`, a number, or `"$3.50"`); outside a checkpointed graph it falls back to the old `RuntimeError`, `on_budget="raise"|"warn"` keep the old modes, and `strict_budget=False` still means warn. A per-turn `CLIContext.budget_usd` (the `/think` pattern) lets `/cost budget <N|off>` reach the server-process middleware. The TUI resolves a pause through the existing ask-user widget ("enter a new budget or cancel"); declining stops the turn with a pointer to `/cost budget`.
- **Caps that fire.** Six `cost.*` manifest keys (config.toml `[cost]` / `BOG_AGENTS_*`, listed by `bog-agents command "/config"`): `budget_usd`, `daily_ceiling_usd`, `warn_at_percent` (80), `max_subagents` (8), `max_web_searches` (50), `preflight_threshold_usd` (1.0); `resolve_option(key)` is the consumer-side resolver the manifest lacked. `create_cli_agent` now builds a `CostLedger` from them for every agent (the Wave A gate was inert in the TUI because nothing passed a ledger), and `tools.web_search` registers each search so `max_web_searches` actually refuses. `/cost caps` shows the effective values.
- **A spend ledger that survives the session.** `bog_agents.spend_ledger.SpendLedger` (SQLite, `~/.bog-agents/spend.db` beside `sessions.db`; scopes `user`, `project:<key>`, `daemon:<job>`) records every finished turn priced from its per-model usage; `gate_turn` refuses new turns once the user's daily ceiling is reached and warns once past `warn_at_percent`; `/cost` shows session spend, the active budget and today's ceiling; `/cost today` breaks it down by scope.
- **Pre-flight before a burst.** `estimate_run_cost(agents, model)` brackets a run (60k–250k in / 4k–20k out tokens per agent); `/team run`, `/butcher` (planner + 2 workers nominal) and `/best-of-n` show `PreflightConfirmScreen` when the high estimate crosses the threshold (`'off'` disables; unpriced models never prompt, so test harnesses are unaffected).
- **Daemon.** `AmbientJob.budget_usd` turns on cost tracking with an in-memory checkpointer keyed by run id; a hit budget parks the run as `status=paused` (no dispatch, no retry) and `POST /runs/{run_id}/resume {"budget_usd": N}` / `bog-agents daemon jobs resume <run> --budget-usd N` continues the same graph; a pause lives in daemon memory, so a restart loses it (recorded on the run). `daily_ceiling_usd` skips runs once the job's recorded spend reaches it; every run records its priced tokens under `daemon:<job_id>`. Both fields ride `jobs create/edit --budget-usd/--daily-ceiling-usd`.
- **Not done / follow-ups.** Per-project daily ceilings (recorded, not yet gated); a durable daemon pause (needs a persistent checkpointer); `/cost explain` and the per-message usage strip are #52.

## 9. Shipped: ROADMAP #52, #66, #62 (2026-09-05, same branch, three commits)

Wave 1 continues in order; every gate green after each (SDK 2,998 / CLI 5,690+ / daemon 255; `ty` + ruff clean; app.py 17,805 under its 17,900 ratchet after the `/cost` and `/plugin` handlers moved to controllers). The hosted CI legs from Wave D went green on their first run — including both macOS cells — so macOS is now blocking.

- **#52 Usage you can read.** `usage_controller.py` prices every streamed `usage_metadata` (uncached input, cache read at 10%, cache write at 125%, output) and hangs a dim strip under the assistant message (`AssistantMessage.set_usage`): tokens in/out, cache read/write, $, TTFT (first text minus request start per namespace), tok/s, and `subagent` when the namespace was nested. The status bar shows session $ and cache-hit ratio (`StatusBar.set_spend`). `/cost tree` breaks spend down by category, `/cost cache` reads the per-thread JSONL written by the new SDK `CacheBustDetectorMiddleware` (innermost, `FeatureConfig.enable_cache_diagnostics`; fingerprints the system prompt and message prefix every call and names the markdown section or the history compaction/rewrite that diverged), and `/cost explain <question>` runs the serialized ledger through the provider-agnostic review model under `_start_model_command`.
- **#66 Turn-end changes tray.** SDK `diff_ordering.py` splits a unified diff per file and ranks by explanatory power (entry points and public signatures first; docs/config lower; tests, snapshots, lockfiles last; lockfiles/generated muted); `render_evidence_markdown` lists files in that order. The CLI folds the adapter's `FileOpTracker` records into one net change per file (`changes_controller.py`), mounts the tray after every turn that wrote files, and serves `/changes show|revert [hunk]|keep`; per-hunk revert is pure text (`diff_hunks.py`), so it works on untracked files. `/diff --ordered` reorders git's blocks the same way.
- **#62 Agent Plugins 1.0 + import.** `plugin_spec.py` reads the `plugin.json` layout (skills/, agents/, commands/, hooks/, mcp.json) into `ExtensionManifest`, discovers `~/.agents/plugins`, workspace `.agents/plugins` (disabled until `/plugin trust <name>` / `bog-agents plugin trust`) and `<config>/plugins`; `extensibility.py` lists them as `agent-plugin` and surfaces their skills and commands. `plugin_install.py` installs from dir / zip / zip URL / git / `marketplace.json` with a SHA-256 pin, traversal-safe extraction and a `.bog-plugin-lock.json`. `plugin_import.py` imports what bog does not read natively — Claude Code skills, agents, user-level hooks, memories, MCP; Cursor user hooks and MCP; Codex global AGENTS.md and `[mcp_servers]` — and reports the rules/hooks that already load natively; idempotent, `--dry-run`. `session_import.py` parses Claude Code JSONL, Codex rollouts and Cline tasks into checkpointed threads through the CLI's own checkpointer (verified: `/threads` lists them with agent, message count, label and `imported` tag) and exports `com.bogware.thread` JSONL that re-imports. Surfaces: `bog-agents plugin list|install|import|trust|untrust`, `bog-agents threads import|export`, `/plugin import|trust`, `/onboard import <tool> [N]`.
- **Open:** opencode session import (no stable transcript format found), antigravity config import (no documented layout), Zed/ACP registry listing (#65) and the VS Code Marketplace publish still need the user's accounts.

## 10. Shipped: ROADMAP #49 (2026-09-05, same branch, one commit)

- **Steerable approvals.** `widgets/approval.py` grows from three options to five: option 4 opens an inline `Input` whose text becomes a `redirect` decision (the adapter rejects the call with the user's instruction in the `ToolMessage`, so the model's next step is the redirect, not a guess); option 5 is `never_allow`, which the adapter persists through `auto_mode.record_never_allow` into the project's `settings.json` and which `AutoModeRuleEngine` now evaluates *before* allow/ask (`AutoDecision.DENY`, reason `never allowed: …`), so a denied call never reaches the menu again — `textual_adapter` folds `denied_indexes` into the same blocked-index path the ledger and guardrails use. A countdown (`approvals.timeout_seconds` in the manifest, env `BOG_AGENTS_APPROVAL_TIMEOUT`, off by default) resolves the menu as `timeout` → rejected, fail-closed, for detached or unattended sessions.
- **Hostile-repo hardening.** SDK `bog_agents/git_env.py`. `hardened_git_env()` uses git's `GIT_CONFIG_COUNT` override (highest precedence, git ≥ 2.31) to pin `core.fsmonitor`, `core.hooksPath`, `core.pager`, `core.sshCommand`, `core.editor`, `core.askPass`, `core.alternateRefsCommand`, `credential.helper`, `gpg.program`, `sequence.editor`. Two things learned the hard way while landing it: (1) `GIT_CONFIG_NOSYSTEM=1` is wrong — the Windows system gitconfig carries `core.autocrlf=true`, and dropping it made hardened git see every CRLF checkout as dirty (`worktree` merge failed with "local changes would be overwritten") — so the pinned value is now whatever the **trusted scopes** (system + global, discovered once per process with `git config --system|--global --list --null`, fail-closed to inert) say, and only editors/pager are forced inert; a global `credential.helper=manager` therefore keeps working while a repo-level `!cmd` helper is reset away. (2) `diff.external` cannot be neutralised by any override — git spawns even an empty value — so every patch-producing internal diff (evidence, checkpoints, the `git_diff` tool, `/diff`, `/jury`, best-of-n, PR review) passes `NO_EXTERNAL_DIFF` (`--no-ext-diff --no-textconv`) and the scan reports the key. `scan_repo_config()` parses `.git/config` (worktree pointer files and `commondir` handled) and flags the always-suspicious keys, `!` credential helpers and aliases, non-LFS filters, diff/merge drivers, remote helpers and `insteadOf` rewrites; the CLI's `repo_trust.py` blocks `/diff`, `/review`, `/pr` until `/permissions trust-git-config` records the config's SHA-256 fingerprint in `~/.bog-agents/repo_trust.json`.
- **Verified:** real-git tests for the pager override, `diff.external` in a repo config (plain git fails, hardened evidence still renders the diff), worktree pointer files; widget tests for every decision type, the countdown, and navigation across five options; never-allow round trip through settings; the timeout from manifest/env. Gates: SDK + CLI ruff and `ty` clean, `app.py` 17,843 (ratchet 17,900).

## 11. Shipped: ROADMAP #54 (2026-09-05, same branch, one commit)

- **Attribution, not estimation.** `bog_agents/token_audit.py`: `audit_agent(build)` calls `build(RecordingChatModel())`, runs one probe turn through the compiled graph and reads what the model was actually sent (system message, bound tool schemas, injected messages). Per-middleware numbers come from instrumenting each instance's `wrap_model_call` / `awrap_model_call` *before* LangChain binds them — `create_agent` now calls `notify_assembly(middleware, tools, prompt)` right before `_langchain_create_agent`, a no-op outside `capture_assembly()` — so each entry reports the net prompt/tool/message delta it produced. Counting uses `tiktoken` `o200k_base` when it is importable and cached, else a deterministic offline approximation (`approx_tokens`, ~8% high on prose, ~19% on JSON schemas); the smoke-test baseline always uses the approximation so CI stays offline.
- **What the numbers said.** SDK default agent: 7,619 tokens per turn before the user's words — 1,652 system prompt (base 461 + Todo 285 + Filesystem 377 + SubAgent 529) and 5,967 of tool schemas, of which `task` alone is 1,664 and `write_todos` 982. The CLI is the 33k-vs-7k story in miniature: 21,088 per turn, because its middleware stack binds **104 tools** (13,996 tokens of schema) and the CLI prompt plus memory (1,098) and skills (641) push the system prompt to 7,092.
- **`lean` profile + `--mini`.** `profiles/harness/_lean.py` registers `lean`: a three-sentence base prompt, one-line descriptions for the twelve core tools (argument schemas untouched; `FilesystemMiddleware` now lets `tool_description_overrides` reach the bundled `multi_edit_file` / `read_many_files` too), `TodoListMiddleware` excluded. `FeatureConfig.harness_profile` merges a named profile over the model's own (`named_harness_profile`, `_merge_profiles`). In the CLI, `--mini` → `ServerConfig.harness_profile` → `create_cli_agent(harness_profile="lean")`, which also turns on the new allowlist mode of `DeferredToolsMiddleware` (`keep_names` / `FeatureConfig.deferred_keep_tools`, `MINI_KEEP_TOOLS` in `agent.py`): every tool outside the core twelve sits behind `tool_search` / `select`. Result: SDK 2,789 per turn (−63%), CLI 8,565 (−59%, 14 visible tools).
- **Surfaces and the gate.** `/tokens middleware` (alias `/cost middleware`) rebuilds the session's stack around the recording model in a worker thread and mounts the report; headless twin `bog-agents command "tokens middleware [--mini]"` returns the same text plus JSON. `tests/unit_tests/smoke_tests/test_harness_overhead.py` pins `per_turn_overhead`, `system_prompt_tokens` and `tool_schema_tokens` for default and lean in `snapshots/harness_overhead.json`, fails on >5% growth or a >50% collapse, and refreshes with `make update-snapshots`. README carries the numbers.
- **Open:** the Harbor pass rate the roadmap wanted beside the number needs a benchmark run; the CLI's own prompt sections (memory 1,098, skills 641, filesystem 709) are user content and were left alone.

## 12. Shipped: ROADMAP #61 (2026-09-05, same branch, one commit)

- **Install without knowing what pipx is.** `install.ps1` and `install.sh` pick `uv tool` → `pipx` → `pip --user`, install uv (which brings its own Python) when the machine has none, refuse the Microsoft Store execution aliases for `python`/`uv` (zero-byte stubs under `WindowsApps` that fail with WinError 5), put the tool directory on the user PATH, and end with `bog-agents --doctor`. Both are idempotent and take `--version` / `--extras` / `--method`.
- **Standalone Windows build.** `packaging/pyinstaller/bog-agents.spec` walks the installed dependency closure of `bog-agents-cli` and, per distribution, copies its metadata and collects its top-level packages — the two things a naive PyInstaller run gets wrong (the first build printed `--version` fine but `--doctor` reported every dependency "Not installed" and `/tokens` died on the lazy `langchain_anthropic.middleware` import). `build.py` builds, smoke-tests the exe (`--version`, `command "/version"`), zips with a sha256 sidecar. `release.yml` gains `windows-standalone` (hosted `windows-latest`, CLI releases only) and `github-release` attaches the zip when the job succeeded; a failure never blocks the PyPI publish. Signing is a commented `azure/trusted-signing-action` step waiting on the org's certificate profile.
- **winget + Homebrew, ready to submit.** `packaging/winget/generate_manifest.py` writes the manifest trio for a release zip as a portable nested installer with the `bog-agents` alias; `packaging/homebrew/bog-agents-cli.rb` is the formula for a `bogware/homebrew-tap` (resources via `brew update-python-resources`). Both need the maintainer's accounts to publish.
- **PowerShell as a first-class tool.** `bog_agents/tools/powershell.py`: `powershell_tool_bundle(backend)` runs the model's script as one argv element through `pwsh` (7) or `powershell.exe` (5.1) with `-NoProfile -NonInteractive -ExecutionPolicy Bypass`, never through `cmd.exe`; it reuses the shell tool's `_DANGEROUS_PATTERNS` through the new public `dangerous_command_match`, truncates output, reports exit codes and timeouts as text. Opt-in via `tools.powershell` (`BOG_AGENTS_POWERSHELL_TOOL`); the CLI adds `powershell` to `SHELL_TOOL_NAMES` and auto-mode's shell-like sets so exec-risk, git classification, never-allow entries and the approval menu treat it exactly like `execute` (the argument is named `command` for that reason). `find_powershell` skips the Store's zero-byte `pwsh.exe` alias; `--doctor` and `--doctor-deep` both name the PowerShell they found or explain the alias trap.
- **Already there:** Task Scheduler `daemon install` on Windows (v6 DMN-3) and MSYS path recovery for headless commands (v6 CLI-8).

## 13. Shipped: ROADMAP #68 (2026-09-05, same branch, one commit) — Wave 1 complete

- **One tree, not five registries.** `tasks_controller.py` folds what the TUI process can actually see into `TaskNode`s: the interactive thread, its prompt queue, `BackgroundAgentManager` tasks (and `PersistentJobsManager` jobs), `/remote` tasks, `/team run` sessions and the daemon's jobs and recent runs (new `daemon_client.list_daemon_runs`). Status vocabularies (enums, ledger constants, daemon strings) normalise to one set; every node carries duration, tokens/spend where known, and the verbs that apply to it.
- **"Waiting on you" is a fact, not a guess.** The main-thread node reads `app._pending_approval_widget` — the same object the approval flow keeps — and names the tool call it is waiting for; `/recap` lists it under *Needs you* together with failed work.
- **Team runs become steerable.** `run_team_session` accepts an injected `ledger`, `mailbox`, `cost_ledger` and a `pause_gate`; the App registers a `TeamRunHandle` before the session starts (`register_team_run` / `finish_team_run`), so `/tasks` shows each ledger task with its owner, `steer` posts to the mailbox (a task id targets its owner, the run id broadcasts), `pause` clears the gate so no new task is claimed while running ones finish, `kill` cancels the tracked worker. Background and remote tasks are steered through the same metadata inbox `/team message` writes.
- **Session-UX table stakes.** Editable queue (`/tasks queue edit|drop <n>`, widgets updated in place), `/recap` (turns, tokens, spend, file records, running work, needs-you, the thread's `/btw` notes from `sidechain.py`), `/threads group pr [all]` (by git branch — the closest thing the checkpointer records to a PR), `/threads archive|unarchive|unread|read <id>` as ordinary thread tags.
- **Honest limits.** The SDK `background_shell` registry and `WorktreeMiddleware` tasks live inside the LangGraph server process and stay invisible to the TUI (v6 CLI-12 territory); the thread-selector modal does not yet hide archived threads (the text listing does). Wave 1 is now complete: #47, #49, #54, #61, #68 (and #51, #52, #66, #62 from the same push).

## 14. Shipped: ROADMAP #55 (2026-09-05, same branch, one commit) — Wave 2 begins

- **The thread survives the hand-off.** A daemon job may now carry `thread_id`, `checkpoint_db` and `goal_ref`. When it fires, `runner._invoke_agent` opens `AsyncSqliteSaver` on the CLI's `sessions.db` (or the job's own path), runs on that thread, and prefixes the prompt with `[ambient: <trigger> trigger for job …; this continues your earlier thread]` plus `Goal: <objective>` read from the goal file — so the checkpointed history, memory and `/goal` state the interactive session built are all there when the event lands. A missing database or a missing `langgraph-checkpoint-sqlite` logs a warning and runs fresh instead of failing the job (the package is now a daemon dependency; lockfile refreshed).
- **Agents can subscribe themselves.** `bog_agents/tools/daemon_tools.py` is a bundle with an injectable client: `schedule` turns "in 2 hours" / "at 09:30" / ISO / cron / "every 30 minutes" into a cron or interval trigger (one-shot forms set `max_runs=1`), `subscribe` maps `github:pr:<n>` etc. onto a GitHub trigger scoped by number and kinds with `until_runs` as the attempt cap, both POST `/jobs` with the runtime's `configurable.thread_id`; `list_subscriptions` / `unsubscribe` round it out. Errors come back as `Error:` strings, never exceptions into the model. `create_cli_agent` registers the four tools only while the daemon is running (four schemas otherwise wasted on every turn).
- **Attempt caps and PR scoping in the daemon.** `max_runs` is enforced in `DaemonScheduler.dispatch` (a skipped run explains "attempt cap reached"), in the tick loop, and by `record_run_result`, which disables the job once the cap is spent so `/tasks` shows it as done. The GitHub webhook used to fan out to *every* job with a GitHub trigger; `github_trigger_matches` now checks `github_number` and `github_kinds`, so a PR-42 subscription never fires for PR 43. API models, PATCH, the store and `bog-agents daemon jobs create --max-runs/--thread/--github-number` all carry the new fields.
- **Open (the last slice of #55):** draft-PR etiquette. Plan: `bog_agents/pr_etiquette.py` — `open_draft_pr(repo_dir, title)` (gh CLI, `[WIP]` prefix, body from the goal + plan), `push_progress(commit_message)` after each accepted change set (`auto_commit` + push), `update_description(evidence)` from `render_evidence_markdown`, and a `pr_review_comment` subscription created automatically by `/pr --draft` so review comments become revisions on the same thread; CLI `/pr --draft` and daemon output target `github_pr_update`. Blocked on nothing but time; the subscription primitive above is the hard part and is done.

## 15. Prepared: file-level plans for what is left (2026-09-05)

Status after this branch: Wave 0, A–D, Wave 1 (#47, #49, #54, #61, #68 plus #51, #52, #66, #62) and the first Wave 2 item (#55, minus draft-PR etiquette) are shipped. Everything below is scoped to files so the next session (or a subagent per item) can start without re-deriving the design. Order is the ROADMAP's recommended order; each plan names the modules to add or touch, the tests, and the one decision that needs the user.

### Wave 2 (remaining)

- **#67 Evidence on every PR + self-review loop.** *(Shipped 2026-09-06, see §16; kept for the trace.)* *Have:* `EvidenceBundleMiddleware` via `FeatureConfig(enable_evidence_bundle=True)`, `--pr --pr-evidence`, daemon dispatch. *Add:* `libs/cli/bog_agents_cli/self_review.py` — `SelfReviewMemo` (`.bog-agents/self-review/<branch>.json`: `diff_sha`, `base`, `effort`, `reviewed_at`, `findings_sha`), `diff_fingerprint(repo, base)` (sha256 of `git diff <base>...HEAD` with `NO_EXTERNAL_DIFF`), `should_skip(memo, fingerprint)`, `marker_comment(fingerprint)` (`<!-- bog-review:<sha12> -->`); `cmd_pr_review.py` gains `--since-last`, `--effort default|high|custom:"<rule>"` (threads a rule line into the review prompt and the memo) and prints the marker so a CI step can `gh api` dedupe; `/review` in `app.py` delegates to the same memo (controller: `review_controller.py`, keep app.py under the ratchet); post-PR jury pass = `jury.py` runner invoked from `--pr` with `--post-review` posting each finding as `gh api repos/{r}/pulls/{n}/comments` (path+line) — new `github_review.py` with an injected `post` callable; resolution ingester = `/resolve <id> addressed|wontfix|incorrect` writing `.bog-agents/self-review/dispositions.jsonl` and `rubric_feedback.py` folding dispositions into `RubricMiddleware` weights (`GoalToolsMiddleware` already holds the rubric file). *Tests:* memo round trip, skip logic, marker stability, effort threading, review-comment payloads with the injected poster. *User decision:* whether the jury pass posts comments by default or only with `--post-review`.
- **#48 Trust profiles, `--restricted`, workspace trust.** *Add:* `libs/cli/bog_agents_cli/trust_profiles.py` — `TrustProfile` (permission mode, expert-rule pack, sandbox level, egress allowlist, RBAC role) loaded from `profiles.py` entries (`[profiles.audit.trust]`), applied in `create_cli_agent` (`auto_mode` settings, `SandboxConfig.build_local_sandbox`, `ExpertRulesMiddleware` pack) — `bog-agents --profile audit`; `--restricted` in `main.py` → `TrustProfile.restricted()` (strip `execute`/`powershell`/`git_*` tools via `excluded_tools`, `FilesystemMiddleware` confined to `working_dir` for reads and writes, approvals cannot be bypassed: `auto_mode` forced to `ask`, `/permissions` cannot lower it); `workspace_trust.py` mirroring `repo_trust.py` (fingerprint of `.bog-agents/`, `.claude/`, `.cursor/`, `.mcp.json`, hooks) gating repo-controlled instructions, hooks, expert rules and `.mcp.json` until `/permissions trust-workspace`; extend `_PROJECT_AUTHORITY_PATTERNS` (in `authority_file_permissions`) with `.github/workflows/*`, `.vscode/*.json`, `.idea/*`, `Makefile`, `pyproject.toml` `[tool.*]` and add a fail-closed `deny` tier; `web_fetch` gets `allowed_domains`/`blocked_domains` from the manifest (`web.allowed_domains`). *Tests:* restricted profile strips tools and forces ask; workspace trust blocks hooks until acknowledged; authority deny tier. *User decision:* default trust posture for a first-opened repo (prompt vs. silent restricted).
- **#56 Detach / attach, session registry, `bog queue`.** *Add:* `libs/cli/bog_agents_cli/session_registry.py` (`~/.bog-agents/sessions/<id>.json`: name, cwd, model, state, pid, heartbeat; written by the TUI on start/turn, the daemon per run, `serve`), `bog-agents sessions` listing; `libs/bog-agents/bog_agents/mailbox_store.py` — SQLite-backed `Mailbox` (same API as `teams.Mailbox`, keyed by thread) so any process can enqueue; `bog-agents queue --session <name> [--wait] "<prompt>"` (cmd in `main.py`, consumed by the TUI's `_pending_messages` drain via a small poller); session broker = `session_broker.py` hosting the agent loop in a detached process (POSIX `start_new_session`, Windows under the daemon service) with a local socket, `bog persist`, `/detach`, `bog attach <id>` (reconnect the TUI to the broker's LangGraph server URL — the server-per-session model already exists); drain: LangGraph `RunControl.request_drain()` wired into daemon SIGTERM and TUI Ctrl+C so runs checkpoint as RESUMABLE, `bog daemon drain|upgrade`. *Tests:* registry round trip + heartbeat expiry, SQLite mailbox cross-process, queue consumed on next drain. *User decision:* Windows broker hosting (daemon service vs. detached console).
- **#53 Cost-objective routing.** *Add:* `operator_mode.py` `objective = intelligence|balance|cost` + `[operator.pool]` (task class → model) in `operator.toml`; `operator_decisions.py` (`~/.bog-agents/operator-decisions.jsonl`: tier, model, cost, rubric verdict) with a `bias()` that shifts the judge threshold from history; `/cost` counterfactual line from the decisions log; `libs/bog-agents/bog_agents/middleware/provider_failover.py` generalising `bedrock_resilience.py` (429/quota headers → rotate `[models].fallbacks`, window-aware cooldown, "parked, auto-resuming at HH:MM" via `CLIContext` + status bar). *Tests:* objective changes the routed tier on a fixed judge; failover rotates and parks. *User decision:* whether `cost` may route to a local model for code edits by default.
- **#64 Hook bus v2.** *Touch:* `hook_decisions.py` (`PostToolUse` honours `{"tool_result": …}`; new `PermissionRequest`, `Interrupt`, `PreModelSwitch`/`PostModelSwitch` decision hooks), `hook_middleware.py` (tool-result replacement before the `ToolMessage` is built), `/model` handler (`PreModelSwitch` deny), `hooks.py` loader (Open Plugins `hooks.json`, per-hook `on_failure: deny|allow|ask` overriding fail-open, trust-by-hash of hook scripts in `plugin_trust.json`), new `prompt_hooks.py` (a `prompt` hook type evaluated by an injected small-model invoke on the Expert-Mode fail-closed path). *Tests:* result replacement reaches the model, deny on model switch, on_failure=deny blocks when the script crashes, hash mismatch refuses. *User decision:* none.
- **#71 Parity treadmill + fork subagents.** *Touch:* `.github/workflows/ci.yml` (`deepagents-parity` leg installs `deepagents` latest and runs the 24 compat tests + smoke import — job exists, pin it to latest), SDK `subagents.py` (`mode: isolated|fork` on `SubAgent`; fork seeds the child with the parent's post-sweeper messages + identical prompt/tool schemas so the first call is a cache hit, runs in the background, returns a `ToolMessage`), `backends` (`ReadResult` pagination notice, bounded `grep_max_count`), `profiles` (opt-in TodoList via profile — `lean` already excludes it), CLI `/subtask <prompt>`, `/fork` (copy the checkpoint thread into a background session on a fresh worktree via `worktrees_controller` + `BackgroundAgentManager`), `team_executor` / `butcher` workers with fork mode. *Tests:* fork child's first request equals the parent's prefix (use `token_audit.RecordingChatModel`). *User decision:* none.
- **#74 Compliance artefact.** *Add:* `libs/bog-agents/bog_agents/action_log.py` (hash-chained per-run JSONL: each event carries `sha256(prev)`; approval decisions, Expert verdicts, cost events; retention policy; signed export reusing the TraceFile Ed25519 signer), `otel_export.py` (vendor-neutral OTLP exporter emitting GenAI-semconv spans for model/tool/middleware/subagent with cost attributes; `LangSmithMiddleware` becomes one exporter), daemon `usage_export.py` (per user/model/job daily aggregates → OTLP + CSV). *Tests:* chain verification detects tampering; span attributes per semconv; CSV totals equal `SpendLedger`. *User decision:* OTLP endpoint/auth conventions.
- **#72 Governed Code Mode.** *Add:* `libs/bog-agents/bog_agents/middleware/code_mode.py` — interpreter (QuickJS via `quickjs` wheel, or a subprocess Python runner inside `LocalSandbox`/Docker) exposing an allowlisted `tools.*` namespace whose every call re-enters the normal tool path (Expert rules, SafeTools, HITL, `CostLedger`), a `task()` global dispatching subagents/teams with a response schema and counted spawns, `execute_mcp_script` binding connected MCP tools, fan-out/vote helpers. *Tests:* a script calling a denied tool is blocked by Expert rules; spawn counts hit `RunawayCaps`. *User decision:* interpreter choice (QuickJS vs. subprocess Python) — the sandbox story differs on Windows until #60.
- **#50 Managed governance layer.** *Add:* `_settings_cascade.py` `managed` layer (signed URL or repo path, fetched at start, cached, Ed25519-verified) with `allowed_mcp_servers`, `skill_allowlist`, required/optional/forbidden plugins (soft-fail), `provider_lock` (gateway-only `base_url`), `zero_retention`, model-switch policy; enforcement points: MCP discovery, `SkillsMiddleware` trust checker, `create_model`, `plugin_install`, `/model` (asserts a `model_switch` fact for YAML rules); org-pinned rows in `/permissions` and `/doctor`; recorded in the evidence pack. *Tests:* forbidden plugin refused, provider lock rewrites base_url, tampered policy rejected. *User decision:* signing key distribution.

### Wave 3

- **#60 Native Windows sandbox (committed 1.x headline).** `local_sandbox.py` Windows launcher: unelevated = `CreateRestrictedToken` (write-restricted + synthetic SID) with explicit ACL grants on working dir / temp / caches / `writable_roots` (pywin32 optional dep); elevated = two low-privilege local users + WFP block-all or egress via the existing CONNECT allowlist proxy; secret-env stripping and read-deny reused; `SandboxConfig.build_local_sandbox` selects it on win32; `doctor --windows`; per-OS support matrix + CI badge. *Tests:* token creation and ACL grant on a temp dir (Windows CI leg from #61's `windows-latest` job).
- **#57 `bog worker`.** `libs/cli/bog_agents_cli/cmd_worker.py` (`worker start --pool <name>`: outbound token-authenticated WebSocket to daemon/serve, registers OS + sandbox level), `libs/bog-agents/bog_agents/backends/remote_backend.py` (network implementation of the shell/files/PTY/browser protocols over that connection), daemon `pool_scheduler.py` (atomic claims modelled on `TaskLedger.claim_next`, owner-locking, `--retire-at`, drain grace, idle hibernate), `/handoff --queue` (thread + diff + untracked files package).
- **#63 Governed host for other vendors' agents.** `libs/cli/bog_agents_cli/acp_teammate.py` — `AcpTeammateRunner` spawning `claude-agent-acp` / `codex-acp` / opencode / goose / dcode over stdio with the ACP client, permission requests mapped onto Expert rules + HITL, every turn counted in `CostLedger`, results wrapped in `EvidenceBundle`; `/team run --worker acp:<agent>`; `HarnessSubAgentBackend` for `claude -p --output-format stream-json` / `codex exec --json` with a bog hook installed into the child's hook mechanism calling back over a local socket.
- **#65 Protocol currency.** Bump `mcp` once a 2026-07-28 release exists (stateless sessions, cacheable tool lists, Tasks extension, `input_required` → HITL dialog, `destructiveHint`-gated approvals); `bog-agents serve --a2a` on `a2a-python 1.1.x` + `RemoteA2AAgent`; publish `bog-agents-acp` + Zed registry listing (needs the user's PyPI account); `bog-agents mcp-server` exposing the `@codebase` hybrid index.
- **#58 Structured human decisions from Slack/email/daemon.** `ask_user` gains `multi_select`/`confirm`/`file_pick`; daemon dispatch renders Block Kit / email with a signed callback, parks the run in the checkpointer until `POST /runs/{id}/answer` (the #55 thread-resume path is the foundation); Slack Events consumer (signing-secret verified, `app_mention` → run bound to `thread_ts`, `!fast`/`!ask` overrides); every answer recorded in the evidence bundle.
- **#59 Scan jobs + findings ledger** and **#70 Security-scan recipe** (share `findings_store.py`: SQLite keyed by stable fingerprint, triage states, SARIF, CI gate, `--max-cost`; `scan` job kind on `AmbientJob`; `/findings`, `/remediate <id>` → PR with evidence; recipe = architecture map → threat model → hunter subagents on `TaskLedger` → `/jury` → sandbox reproduction).
- **#73 Agent-authored workflows.** `workflow.py` schema (context → work → review → verify → synthesize, each phase a team fan-out under `RunawayCaps`), `author_workflow(description)` tool writing `.bog-agents/workflows/<name>.yaml` loaded as `/name` by `prompt_commands.py`, runner persisting phase state for pause/resume, per-agent meters and a hard per-workflow budget.
- **#69 Plan review screen + `--plan --auto`.** Shared `PlanReviewScreen` (butcher manifests, JTBD specs, plan-mode output; line-addressed comment staging → one revision prompt → re-plan loop), per-slice checkboxes into `ButcherJob`, full-screen execution view on `dashboard.py`, `bog-agents --plan "<prompt>" --auto` in `non_interactive.py`.
- **#75 Memory rebuild + advisor.** `memory_rebuild.py` (pure, injected `invoke`: dedup / contradiction resolution / provenance; candidate store under `.bog-agents/memory.rebuild/`, diff, swap on approval, daemon-schedulable) and an `ask_advisor` tool (one bounded question to the `hard` tier, counted and capped).
- **#76 Team v2.** Typed `Attachment` on `Message` + `send_file`/`receive_files`, SQLite-persisted `Mailbox` (shared with #56), `/add-dir` on `CompositeBackend`, `[worktree] reuse` env cache under `~/.bog-agents/envcache/`, sandbox snapshot templates in `.bog-agents/sandbox.lock`.

### Still needs the user (unchanged)
VS Code Marketplace publish; ACP PyPI + Zed registry; Azure Trusted Signing secrets for the Windows zip; winget submission + Homebrew tap; a Harbor pass rate beside the README overhead number; the #55 draft-PR etiquette decision (open a draft PR at start by default, or only with `/pr --draft`).

## 16. Shipped: ROADMAP #67 (2026-09-06, same branch, one commit)

- **A review that remembers.** `self_review_memo.py` fingerprints the exact text a review covers (the same git commands the `/self-review` prompt tells the agent to run, under `hardened_git_env` + `NO_EXTERNAL_DIFF`, with `.bog-agents/` excluded from the untracked list so the memo cannot move its own fingerprint) and stores `<branch>.json` with base, effort and verdict. `/self-review --since-last` skips when the sha and effort match (asking for `high` after a `default` review runs again); `--effort` is quote-aware (`shlex`) so `custom:"never flag docstrings"` survives; the announcement carries the `<!-- bog-review:<sha12> -->` marker. The parser bug where flags after `--branch` were dropped is fixed on the way.
- **Rulings feed the next review.** `/finding <id> addressed|wontfix|incorrect [note]` appends to `dispositions.jsonl`; `lessons_block` turns the `incorrect`/`wontfix` rulings into a "do not repeat" section the prompt carries. Deterministic, no model in the loop — the rubric learns by exclusion first.
- **The jury posts to the PR.** `--pr --pr-review [--pr-effort …]` runs `pr_review_pass.run_post_pr_review` after the PR opens: branch diff → configured jurors → `github_review.build_review_payload` (findings naming `path:line` in the changed files become line comments, the rest go into the body with the marker) → `gh api` through a temp `--input` file, deduped on the marker, with an anchor-free retry when GitHub rejects a stale line. Jurors, jury runner and `gh` are injectable; tests cover payload shaping, dedupe, fallback and the end-to-end pass on a real temp repo.
- **Open:** review-thread events → automatic dispositions (compose the #55 `subscribe("github:pr:<n>")` with a `pr_review_comment` handler that records `/finding` rulings); `--since-last` in the `/pr review` path (same memo, different scope) is a one-liner once someone wants it.

### Decisions taken 2026-09-04
Commit this report on `docs/review-v6` off `origin/main` and start Wave 0 immediately; Wave 1 leads with ROADMAP #47 Governed Auto Mode; 1.0 = Wave 0 + Wave 1 + Wave 2 + a written stability contract; #60 native Windows sandbox is a committed 1.x headline; the VS Code extension is fixed and published.

### Deferred by decision
**SDK-9** (RBAC tool-call re-check) rides with ROADMAP #48; **CLI-10/11** (`/sidecar` context, `/race` worktree mode) ride with #68/#71; the VS Code webview rework beyond SAT-4 stays parked until the extension has a user.

---


# REVIEW.md v5 — Current-State Audit (2026-08-20)

> **Scope:** Whole monorepo at v0.9.12, post the PR #165 v4-fix wave and the PR #181
> Tier-1 (grok-inspired) wave, on `chore/1.0-hardening`.
> **Method:** 37-agent workflow — 10 dimension finders (sdk-core, sdk-context, sdk-safety,
> sdk-backends, tier1-seams, cli-core, cli-config, daemon, satellites-delivery, perf) + 1
> v4-remainder re-scorer, then adversarial verification of every P0/P1 candidate (2
> independent refuters per P0, 1 per P1; P2s ship unverified and are labeled so).
> **Tally:** 1 P0 + 24 P1 candidates verified → **1 P0 + 23 P1 confirmed**, 1 downgraded to
> P2 (DMN-1), 36 P2s. Re-score of the v4/v3 remainder: **18 FIXED, 7 PARTIAL, 46 OPEN, 1
> OBSOLETE**.
> **Baseline test health:** SDK has 1 unit failure (`test_api_deprecation` stacklevel);
> CLI + daemon green (the Windows `--disable-socket` async-test quirk accounted for).
> **Verdict in one line:** The correctness core held — the v4 waves and the Tier-1 primitives'
> tested cores both passed adversarial re-audit. The new P0/P1s cluster in **five diseases**:
> commands running on the TUI event pump (the P0 + four freezes), approval gates that don't
> gate (batch-tool/exec-risk/git-classifier/hook-matcher bypasses), v4 fixes that didn't fully
> land (SDK-CORE-4, SB-3, RBAC remediation, RD-4, DEL-5/2, P0-9), event-loop-freezing hot
> paths (the two new Tier-1 CLI modules), and delivery drift where CI still doesn't look.

## 0. Executive summary — health by package

| Package | State | One-line |
|---|---|---|
| **SDK core** | **Strong center, two seam regressions** | The SDK-CORE-2 builder fix, the serve Wave-4 hardening, the canonical middleware order, and the deepagents drop-in all passed adversarial re-audit. But `enable_rbac` without a pinned role now hard-crashes `create_agent` (the MW-SAFE-2 remediation's own warning line is unbound — `v5 SDKC-1`), and serve's `/stream` path never records the assistant reply, so the SDK-CORE-4 fix healed `/invoke` only (`v5 SDKC-2`). |
| **SDK middleware (context)** | **Cores hold, the heal path lobotomizes** | Sweeper view-transform, event-based summarization, OutputTruncation merge, and the overflow tail-clip are coherent and tested. The one P1: the new context-length heal sends the untrimmed history back to the model that just rejected it, so it commits `"Error generating summary: …"` as the **permanent** summary on the default-path agent — silently evicting all pre-cutoff context on the exact failure the feature advertises healing (`v5 CTX-1`). |
| **SDK middleware (safety)** | **Plumbing solid, guarantees unwired** | `permissions.py`, the expert engine, air-gap pinning, and RBAC-pinned tool-stripping are genuinely well-built. But `multi_edit_file`/`read_many_files` still escape the permission boundary — now also defeating the CLI self-modification guard (`v5 SAFE-1`), and the deterministic exec-risk veto (Tier-1 #2) is called by nothing shipped, so the live route relies on a nondeterministic Haiku backstop (`v5 SAFE-2`). |
| **SDK backends** | **Mature, two tools slip the net** | Atomic-write, symlink/O_NOFOLLOW, the background-shell registry, egress proxy, and the PTY pure layers are all sound and the v4 SB-1/CTX-1 crashes are fixed. But the PR #181 PTY tools run arbitrary programs with zero HITL (`v5 SB-1`), and worktree merge/restore's `git checkout -- <branch>` treats the branch as a pathspec, so the merge tool is 100% non-functional and the branch-restore net never restores (`v5 SB-2`, = v4 SB-3). |
| **Tier-1 seams** | **Tested cores, unwired edges** | FTS injection is closed, and `stop_gate`/coercion/auto-background/PreToolUse-enforcement cores hold. The defects are at the seams: the single git classifier misreads git's own global options (`v5 T1-1`), the exec-risk veto is dead code in the live CLI route (`v5 T1-2`), and migrated wildcard/regex `PreToolUse` deny hooks silently never fire (`v5 T1-3`) — a security gate that fails open. |
| **CLI core** | **v4 lifecycle holds, new surfaces bypass it** | Every v4 turn-lifecycle fix verified in place and TurnManager is sound for the paths that use it. But the autonomy/utility surfaces added since v4 — `/telephone`, `/team run`, `/best-of-n`, `/jury`, interactive `/pipeline`, `/background`, `/butcher` — dispatch inline on the App message pump, invisible to TurnManager. `/telephone`'s success path hard-deadlocks the entire TUI (the cycle's P0, `v5 CLIC-1`). |
| **CLI config/trust** | **Well-engineered, two dead overrides** | CT-1 (`BOG_AGENTS_MCP_TRUST`), RD-1 (effort registry), skill-trust, theme, OAuth, and the manifest/env-var registries all verified honest. Residuals: `/butcher` always resolves the hardcoded Anthropic preset tiers so the documented active-model fallback is dead code (`v5 CT-1`, = v4 RD-4), and `BOG_AGENTS_HOME` is registered, manifest-surfaced, reported effective — and read nowhere (`v5 CT-3`). |
| **daemon** | **Reliability delivered, one inert filter** | Cron catch-up, token rotation, `dispatch_errors` capture, corrupt-`jobs.json` quarantine, and orphan-run reconciliation are all genuinely fixed and tested. The git-push branch filter still collapses `refs/heads/feature/x` to `x`, so `feature/*`-style patterns can never match (`v5 DMN-7`). The unattended git-tool env-inheritance gap is real but bounded (`v5 DMN-1`, downgraded to P2). |
| **Satellites** (harbor/acp/daytona/vscode) | **CI legs landed, edges still drift** | Harbor is rebased on `BaseSandbox`, acp is path-pinned and green, satellite CI legs exist. But the release relock loop covers only 3 of 6 packages so all three satellite locks are stale at HEAD (`v5 SAT-1`), the VS Code extension strips every provider credential from the CLI child env (`v5 SAT-2`, = P0-9), and the daemon README documents phantom install commands in three spellings (`v5 SAT-3`). |
| **perf** | **Bounded everywhere except two new modules** | Lazy-import discipline for the Tier-1 wave, entry-path speed, the app message pipeline, and per-tool-call gate costs are all sound. The two Tier-1 #4 CLI modules regressed the pattern: `/threads search` rebuilds the whole FTS index with a commit-per-row on the event loop (`v5 PERF-1`), and streaming tool-call args are re-joined and re-`json.loads`'d per chunk — O(n²) on a large `write_file` (`v5 PERF-2`). |

Throughline: **v4 was promises the code doesn't keep; v5 is the seams around the two newest waves.** The correctness center is genuinely strong, but the autonomy surfaces bolted onto the CLI bypass the turn state machine v4 built, and the Tier-1 safeguards shipped as tested cores that nothing in the product actually calls.

---

## 1. Prior-cycle scorecard

Re-score of the v4/v3 open remainder at `d26ef78` (v0.9.12): **18 FIXED, 7 PARTIAL, 46 OPEN, 1 OBSOLETE.**

- **PR #165 delivered its claimed scope almost exactly.** All six Wave-4 serve/builder items are verifiably fixed in code (**SDK-CORE-1/2/4/5/6/7** — f45c2b9, fc79ee9), as are **DMN-6** (watchdog file triggers, 75fb169), the delivery items **DEL-6/7/8**, the v3 CI stragglers **P1-66/69/70/71/72**, **P1-60** (harbor aglob), **P1-26** (replay busy-guard), and **QW-mcp-trust-verb**.
- **The P2 backlog and quick-wins were almost untouched.** 24 of 31 v4 P2s remain OPEN with the exact mechanism intact; the 3 PARTIALs are CTX-3 (CLI still passes no `model_name`), MW-SAFE-7 (truncation opt-in but intent-not-execution recording remains), and SAT-3 (VS Code still has no PR CI). 10 of 13 quick wins are undone — notably **QW-git-switch** (SB-3's `git checkout -- <branch>` pathspec bug, a confirmed v4 P1, is still live at `worktree.py:398`/`:550`; no commit references SB-3 — promoted back to `v5 SB-2`).
- **Still-open clusters:** VS Code (P0-9, P1-62/63/64, SAT-6, SAT-8 — all still open); ACP multi-session (P1-61/SAT-5); the CTX family (CTX-2/4/5/6/7/8); safety boundary re-checks (MW-SAFE-4/5/6); CLI dead commands (P1-25 `/think`, P1-27 `/checkpoint load`, P1-32 `/worktrees cancel` partial); daemon docs/schema (DMN-7/8, P1-86 phantom env schema, P1-9/18/22); config (CT-2/3/4). **SAT-7 is OBSOLETE** — no SAT-7 was ever defined in the v4 P2 list (numbering skipped SAT-6→SAT-8).

---

## 2. New findings — adversarially verified

IDs are formatted `v5 <ID>`; cross-reference them in commit messages (e.g. "fixes v5 CLIC-1").
**Note:** several v5 IDs collide with different v4 findings (v5 `CTX-1`, `CT-1`, `SB-1`, `DMN-1`,
`SAT-1`, `PERF-1` name different issues than their v4 namesakes) — always write the `v5` prefix
in this section. Every P0/P1 below survived independent refutation; almost all were
live-reproduced on the working tree. Effort is (S/M/L).

### P0 — ship-blocker (1)

- **v5 CLIC-1** — `/telephone <prompt>` hard-deadlocks the entire TUI (`app.py:6624`). `_handle_telephone_command` runs inline on the App message pump; on the success path (model resolves, rewrite non-empty) it mounts `TelephoneMenu` and blocks the pump on `decision = await future` with no timeout. The future is resolved only by the menu's key-binding actions, but Textual delivers all terminal input — including Esc and Ctrl+C — through the same blocked pump: a circular wait. Only an external process kill recovers. `/telephone` deadlocks precisely when it works (the pre-await early-return paths are the only non-deadlocking ones). Live-reproduced end-to-end under Textual 8.2.6 (two refuters). Fix: dispatch off the pump via `run_worker`, or restructure to a `TelephoneMenu.Decided` message handler (S).

### P1 — serious (confirmed)

**SDK**
- **v5 SDKC-1** — `create_agent(config=FeatureConfig(enable_rbac=True))` without `rbac_active_role`, and `AgentBuilder(...).with_rbac().build()`, both raise `UnboundLocalError` before the graph is built (`graph.py:1265`). The crashing line is the very warning the MW-SAFE-2 fix (d7c5271) added to tell operators unpinned RBAC enforces nothing — `_logging` is bound only inside the enhanced-skills branch, unbound on every other path. The builder's `with_rbac()` cannot set a role at all, so this first-class method can never build. A regression *in* the v4 MW-SAFE-2 remediation. Live-reproduced twice (S).
- **v5 SDKC-2** — serve's `/stream` never records the assistant reply (`serve.py:331`): the SDK-CORE-4 history-replay fix landed only for `/invoke`. On the documented default wiring (no checkpointer), every multi-turn conversation over `POST /stream` replays a user-only transcript — turn 2's model sees consecutive user turns with all its own prior answers missing, while `/history` implies continuity. Same defect class as the fixed SDK-CORE-4. (= v4 SDK-CORE-4, residual.) Live-reproduced (M).
- **v5 CTX-1** — the new context-length heal commits `"Error generating summary: …"` as the permanent conversation summary (`summarization.py:1468`). The default factory builds the middleware with `trim_tokens_to_summarize=None`, so the overflow-triggered summary call sends the entire untrimmed pre-cutoff history to the model that just rejected the request; langchain swallows the second overflow into an error string that `wrap_model_call` commits as the permanent `_summarization_event`. On small-window models (Ollama/custom — the CLI "local" preset) the token trigger can never fire first, so *every* compaction is overflow-triggered and drops all but 6 messages. (Sharpened v4 CTX-6.) Live-reproduced with no network (M).
- **v5 SAFE-1** — `multi_edit_file` and `read_many_files` escape `FilesystemPermissionsMiddleware` (`permissions.py:159`): both are absent from `_FS_TOOL_PATH_ARGS`, so `_resolve_denied_path` returns None and neither the deny check nor the interrupt `when` predicate fires. A `deny /secrets/**` rule is writable-through and readable-through, and the CLI self-modification guard (installed by default, docstring claims `multi_edit_file` is gated) is defeated — a model in auto-approve or prompt-injected mode can rewrite `.bog-agents/laws.md`, `expert_rules/*.yaml`, `.mcp.json` with no approval (the CVE-2026-25725 class). (= v4 SB-2, still open.) Live-reproduced (M).
- **v5 SAFE-2** — the exec-risk auto-approval veto is wired into zero shipped auto-approve routes (`safe_tools.py:142`). `command_has_exec_risk` is consumed only by `is_tool_safe`, which is called from nothing but tests. The live CLI route (`AutoModeRuleEngine._eval_shell`) never calls exec-risk, so stealth-exec commands (`git -c core.pager=…`, `sort --compress-program=…`, `tar --to-command=…`) return ALLOW/`default` and are caught only if the optional, nondeterministic Haiku classifier happens to flag them (and it fails open when `anthropic` is absent). The deterministic Tier-1 #2 safeguard protects no path a real user touches. (= v5 T1-2, same root, SDK framing.) Live-reproduced (S).
- **v5 SB-2** — worktree merge and branch-restore run `git checkout -- <branch>`, so `--` forces git to read the branch as a pathspec (`worktree.py:398`, `:550`). On any normal repo `merge_worktree` returns "Failed to checkout" every time — the merge never runs — and `_restore_branch` leaves the shared working tree parked on the wrong branch. A file named the same as the branch produces a silent wrong-branch merge reported as success. (= v4 SB-3, still open; the strongest re-score promotion candidate.) Live-reproduced against real git (S).

**CLI**
- **v5 CLIC-2** — `/team run`, `/best-of-n`, and `/jury` run whole multi-agent sessions inline on the App pump (`app.py:13484`, `:5997`, `:6144`). For the minutes-to-hours these run (N auto-approving agents mutating the repo working dir), every key event — typing, Esc, Ctrl+C — queues dead behind the pump, and the session is invisible to TurnManager (`_turns.busy` stays False). Contrast the butcher handler's own comment: "run in a worker so the TUI stays responsive." Live-reproduced (M).
- **v5 CLIC-3** — interactive `/pipeline` executes on the App pump while awaiting agent workers (`app.py:16320`): `on_step` blocks the pump in `wait_for(worker.wait(), 1800s)` per step. Any step needing HITL approval deadlocks — the approval keys are App-level bindings that can't dispatch while the pump waits on the worker that waits for the approval — and the aggravation is worse than claimed: the cap misfires (`WorkerCancelled` escapes the `except TimeoutError`), aborting all remaining steps while the wedged worker keeps running. The scheduled/file-watch paths (off-pump) prove only the interactive path is broken. Live-reproduced (S).
- **v5 CLIC-4** — `/background` and `/async` completion notifications never fire (`app.py:2621`). Tasks run via `asyncio.create_task` on the app's own loop, so `on_complete=lambda t: self.call_from_thread(...)` runs on the app thread, where `call_from_thread` unconditionally raises `RuntimeError`, swallowed at debug level. Every background task — including failures — completes silently unless the user polls `/background list`. Live-reproduced in a running Textual app (S).
- **v5 CLIC-5** — butcher jobs are uninterruptible (`app.py:11006`). Both dispatch sites (`/butcher` and the default-on operator escalation) start the worker with a bare `run_worker(...)` and discard the handle; `_run_butcher_task` sets `_agent_running=True` but never calls `_turns.begin_agent`, so `_agent_worker` stays None. Every interrupt path requires both flags, so Esc does nothing and the only escape is quitting the app while weak workers run LLM-authored shell commands. The `_cleanup` docstring's claim that butcher "never spawn[s] a worker" is factually wrong. Live-reproduced (S).
- **v5 SB-1** — the PR #181 PTY tools (`pty_start`/`pty_send`) run arbitrary programs with no HITL approval (`cli/agent.py:1663`). Wired into every default shell-enabled agent on POSIX; `pty_start` runs an arbitrary argv via `shlex.split` + `os.execvpe` with the full inherited env, and neither tool is in the interrupt map or the auto-mode risk lists. In default full-HITL *and* auto mode, `pty_start('x','bash')` + `pty_send('x','curl …|sh<CR>')` executes and drives arbitrary programs with zero prompts, also bypassing the OS sandbox/egress proxy — the same guarantee v4 CLI-CORE-2 was filed to protect. Live-checked (M).
- **v5 T1-1** — `classify_git_command` misreads git's own global options as the subcommand (`git_ops.py:114`): `_classify_segment` takes `tokens[1]` after peeling only wrappers/env, never skipping `-c`/`-C`/`--no-pager`/`--git-dir`. So `git --no-pager push --force`, `git -c x=y reset --hard`, `git -C /repo clean -fdx` all classify MUTATING (not DESTRUCTIVE) and auto-mode ALLOWs them — routine flags an agent naturally emits defeat the single-classifier invariant CLAUDE.md documents. (= v5 CT-2, cli-config twin.) Live-reproduced at all three layers (M).
- **v5 T1-2** — the exec-risk veto is dead code in the CLI auto-mode path (`auto_mode.py:392`): `_eval_shell` consults ask-patterns, `classify_git_command`, allow-patterns, and bash-hygiene — never `command_has_exec_risk`. Under smart auto-mode the exec-risk vectors are auto-approved (`default`) rather than deterministically vetoed to HITL; the CLAUDE.md/docstring "fails toward prompting" claim is disconnected from the shipped route. (= v5 SAFE-2, same root, CLI framing.) Live-reproduced (S).
- **v5 T1-3** — migrated Claude/Cursor `PreToolUse` deny hooks with wildcard (`"*"`) or alternation (`"Edit|Write"`) matchers silently never fire (`hook_decisions.py:344`). Matcher handling is exact-string equality after a single-name alias lookup, so `"*" != "execute"` and the deny is skipped and the call allowed — a security gate that fails open exactly where CLAUDE.md promises hook files "load unchanged" and enforcement is fail-closed. Only wildcard/regex matchers are affected (empty/exact/aliased work). Live-reproduced (S).
- **v5 CT-1** — `/butcher` always resolves the hardcoded Anthropic preset tiers, so the documented active-model fallback is unreachable (`butcher.py:1060`). `_resolve_models` calls `ensure_session(app).tiers` unconditionally and `resolve_tiers` always seeds from `BUILTIN_PRESETS["anthropic"]` even with operator mode off, so `tiers["max"].model` is always non-empty and the `or active` fallback is dead. A Bedrock/Ollama-only user gets every model resolved to Anthropic specs and a misleading "try a more concrete prompt" error; an Anthropic user with a different active model is silently switched. (= v4 RD-4, still open — 79a0f94 fixed only RD-5.) Live-reproduced (S).
- **v5 CT-2** — cli-config twin of `v5 T1-1`: the same git global-option gap, confirmed independently to downgrade `git -c x=y push -f` / `git -C . push -f` destructive→mutating and yield ALLOW/`default` in the deterministic auto-mode gate (`git_ops.py:110`). d4b05ec hardened the post-subcommand arg side (`-ff` clusters) but missed the pre-subcommand side. Live-reproduced (S).
- **v5 CT-3** — `BOG_AGENTS_HOME` is a dead override (`_env_vars.py:92`): documented to relocate the config/vault/state home, surfaced in the manifest as `paths.home`, and reported effective by `config get` with source `env (BOG_AGENTS_HOME)` — but read nowhere. All ~75 sites hardcode `Path.home()/".bog-agents"`, so a user redirecting secret storage keeps writing to the old path while the config surface confirms the override is active. Same defined-surfaced-printed-but-read-nowhere class as v4 CT-1. Secondary: `BOG_AGENTS_MODEL` is read only as a dreamscape argparse default. Live-reproduced (M).

**daemon**
- **v5 DMN-7** — the git-push branch filter never matches any branch with a `/` (`api.py:745`): the endpoint normalizes the pushed ref to its last path segment (`ref.split("/")[-1]`), so `refs/heads/feature/x` becomes `x` and a `feature/*`/`release/*`/`dependabot/*` pattern can never match — the documented filter is inert for the dominant branch-naming convention, with no error surfaced. Aggravator: `refs/heads/wip/main` collapses to `main` and over-matches a `main` pattern. (= v4 DMN-7, still open.) Live-reproduced against the real FastAPI app (S).

**Satellites & delivery**
- **v5 SAT-1** — all three satellite lockfiles are stale at HEAD while the release pipeline re-creates the drift (`ci.yml:127`): the v4 DEL-5 blocking `uv lock --check` was demoted to `continue-on-error`, and release-please's relock loop covers only `bog-agents`/`cli`/`daemon`, so every linked-version release leaves acp/harbor/daytona pinned to the old SDK version. `make lock-check` fails for every fresh contributor; the satellites CI leg papers over it by re-resolving without `--frozen`. (= v4 DEL-5.) Live-reproduced with uv 0.11.6 in a pristine worktree (S).
- **v5 SAT-2** — the VS Code extension strips every provider credential from the CLI child env (`extension.ts:99`): `buildChildEnv()` allowlists only PATH/HOME/locale/temp + `BOG_AGENTS_*`, so `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `AWS_*`, `GOOGLE_APPLICATION_CREDENTIALS` never reach the spawned CLI. A user following the extension's own README gets an auth failure on every action; only the undocumented CLI-vault path (APPDATA passes) escapes. Two audits have now flagged this. (= v3/v4 P0-9, still open — only a CVE dep patch since.) Live-reproduced under Node 23.6 (S).
- **v5 SAT-3** — the daemon README's "Running as a service" section and the CLI's install help document phantom commands in three spellings (`daemon/README.md:172`): `install-systemd`/`install-windows-task`/`install-launchd` all fail with "invalid choice" (the binary accepts only start/run/stop/status), `install-windows-task` has no implementation anywhere, and the TUI prints a third nonexistent flag spelling — while the one real path (`bog-agents daemon install`) is mentioned by no document. The DEL-2 drift-guard covers only the quickstart. (= v4 DEL-2 class.) Live-reproduced (S).

**perf**
- **v5 PERF-1** — `/threads search` freezes the whole TUI for seconds (`session_search.py:244`): every invocation drops and rebuilds the entire FTS index from scratch — one `index()` per thread (up to the 1000 cap), each issuing DELETE+INSERT plus its own COMMIT — over a synchronous `sqlite3` connection awaited directly on the event loop. Measured 3.4–7.3 s at the 1000-thread cap vs ~8 ms for the same rows in one transaction (~2–3 orders of magnitude), regressing the aiosqlite+WAL pattern `sessions.py` uses. Live-reproduced (S).
- **v5 PERF-2** — streaming tool-call args are re-joined and re-`json.loads`'d per chunk (`textual_adapter.py:1167`): each `tool_call_chunk` rebuilds the full accumulated arg string and attempts a full-prefix `json.loads` that fails on every chunk until args complete — O(n²) in arg size, on the event loop. A large `write_file`/`multi_edit_file` (~15–60-char Anthropic deltas over whole-file content) measured ~2.6 s at 400 KB. Distributed event-loop CPU: stuttered rendering + seconds of added turn latency. Live-reproduced (S).

### Downgraded after verification

- **v5 DMN-1** (P1→P2) — the unattended-trigger "no host shell / env not exposed" guarantee is partly overstated: git tools are enabled for every trigger and `_run_git` spawns real `git` subprocesses with no `env=`, inheriting the daemon's full `os.environ` (`runner.py:502`). But the central P1 exploit narrative is **refuted** — no code path templates trigger payloads (git-push commit text, GitHub issue bodies) into the model prompt (`_build_prompt` uses operator-authored sources only), the unattended backend disables the shell so the model can't read the env, and the git bundle exposes no remote/push/config and takes structured args. A genuine doc/hardening gap (scope or scrub the git subprocess env), not the network-reachable secret-exfil P1 claimed.

### P2 — important, not urgent (36, unverified by design)

Recorded from the dimension audits without adversarial verification; treat severity as provisional. Grouped by area:

- **SDK core:** subclassed `ParallelWorktreeMiddleware` + `enable_result_synthesis` still crashes `create_agent` on exact-type ordering validation — now self-inconsistent with the assembly that positioned it (SDKC-3, = v4 SDK-CORE-3); `with_kwargs(config=…)` overwrites builder-assembled flags wholesale — a regression from the SDK-CORE-2 fix (SDKC-4); user-supplied `AnthropicPromptCachingMiddleware` + `memory=` inverts the memory-before-caching invariant (SDKC-5); keyless-localhost serve is still drivable cross-site via no-cors POST and DNS-rebinding reads despite CORS `[]` (SDKC-6, = v4 SDK-CORE-1 residual); the top-level export surface still omits first-class middleware the docs point at — `SkillsMiddleware`, `ResultSynthesis`, `StopGate`, `OutputTruncation`, … (SDKC-7).
- **SDK context:** the CLI still splices the sweeper *inner* of summarization, inverting the documented order so sweep savings never defer compaction (CTX-2, = v4 CTX-8); the CLI cost tracker is still built with an empty model name → every model billed at (5,15) and a 1M context window reported for 200K models (CTX-3, = v4 CTX-3+CTX-4); the sweeper marks content offloaded before the write, so a failed offload breaks `recall_swept` forever (CTX-4, = v4 CTX-5); deferred-tools activation is process-global per compiled agent — cross-thread leak and lost activations on resume (CTX-5).
- **SDK safety:** the expert-rules engine fails *open* on a first-load parse error when rules are disk-only (SAFE-3, = v4 MW-SAFE-6); operator-pinned RBAC enforces only at request time (tool-stripping) with no tool-call-boundary re-check (SAFE-4, = v4 MW-SAFE-4); `GuardrailMiddleware` silently skips async-only guardrails on the sync path (SAFE-5, = v4 MW-SAFE-5).
- **SDK backends:** `write_file` overwrites in place with O_TRUNC while `edit_file` is atomic — a crash mid-write loses the original (SB-3, = v4 SB-5); auto-background-on-timeout reports `exit_code=0` and the execute tool prints "succeeded" for a still-running command, worsened by a stale "Defaults ON at 60s" comment (SB-4).
- **tier1-seams:** auto-mode Haiku escalation fails open when the `anthropic` package is absent, so non-Anthropic installs auto-approve every default-verdict shell command (T1-4).
- **CLI core:** peat scheduled runs can wait on and harvest the wrong turn when the queue drain starts a user turn between busy-wait and dispatch (CLIC-6); `_resume_thread` has no busy guard, so a thread switch can swap `_lc_thread_id`/`session_state` under a live turn (CLIC-7).
- **CLI config:** one undefined `${VAR}` in one server's headers still disables ALL MCP servers and hard-exits the ACP session (CT-4, = v4 CT-2); butcher's per-slice file allowlist is enforced only on `write_file`/`edit_file` — `run_command` writes outside it (CT-5, = v4 RD-5 residual); MCP OAuth token writes still have a world-readable temp window and the oauth dir is never dir-secured (CT-6, = v4 CT-3); the manifest omits the provider-scoped thinking config.toml locations the app reads, so `config get models.thinking` misreports (CT-7, = v4 CT-4).
- **daemon:** file-change triggers silently drop changes that occur while the daemon is down (DMN-9); PATCH/POST stores the `***` redaction placeholder verbatim as the real secret on a read-modify-write, making webhook HMAC forgeable (DMN-8); file-change polling runs a synchronous up-to-50k-stat `os.walk` on the event loop (DMN-6); run records are written non-atomically, so a crash mid-write corrupts a run that is then silently skipped forever (DMN-10); plus **DMN-1** (downgraded above).
- **satellites:** the ACP server shares one agent, one cwd, and one cancel flag across all sessions (SAT-4, = v4 SAT-5/V3-15); the daytona integration suite can't even collect (ImportError on `SandboxIntegrationTests`) (SAT-5, = v4 SAT-4); the VS Code sidebar chat view has no provider and the panel webview overwrites the previous reply on every prompt (SAT-6, = v4 SAT-6/SAT-8); acp ships its PEP 561 marker as importable `py.typed.py` and its README installs an unpublished package with a nonexistent uv verb (SAT-7); the harbor wrapper rides the deprecated `als_info` API scheduled for 1.0 removal and `run_sync` uses `asyncio.get_event_loop()` (SAT-8).
- **perf:** the sweeper re-plans full canonical history per model call with an O(E²) superseded-read scan — up to ~0.9 s/call on long threads (PERF-3); sweeper offload rewrites the entire ever-growing offload file on every call (cumulative O(N²)) and writes it into the user's CWD (PERF-4); `BOG_AGENTS_MEMORY_VECTOR=1` embeds memory one paragraph at a time with `embed_query` at agent build — N sequential network calls on startup (PERF-5); `import bog_agents` is ~2.9 s / 2221 modules despite the "loaded lazily" contract because line 10 eagerly imports the graph stack (PERF-6).

---

## 3. Systemic themes

1. **Commands running on the TUI event pump.** The P0 and four P1s (CLIC-1/2/3/4/5) are one disease: autonomy/utility surfaces added since v4 (`/telephone`, `/team`, `/best-of-n`, `/jury`, interactive `/pipeline`, `/background`, `/butcher`) dispatch inline on the App message pump instead of a worker, so they freeze the keyboard, are invisible to TurnManager, and (telephone/pipeline) deadlock outright. The v4 TurnManager exists and is sound — these paths never traverse it. Fix pattern: every long-running dispatch goes through `run_worker` + `_turns.begin_agent(...)` so Esc/Ctrl+C cancel it and the submit path queues; add a test that presses a key while each surface is live.
2. **Approval gates that don't gate.** SAFE-1 (batch tools escape permissions), SAFE-2/T1-2 (exec-risk dead code), T1-1/CT-2 (git classifier misreads global options), T1-3 (wildcard hook matchers fail open), SB-1 (PTY tools unwired from HITL) — each is a control whose enforcement is either at the wrong layer (request-shaping, not the tool-call boundary) or simply not called by the shipped route. Fix pattern: enforce at the tool-call boundary, wire the deterministic analyzers into the *live* auto-approve engine, and add build-time coverage assertions that every write/read-class tool and every matcher shape is gated.
3. **v4 fixes that didn't fully land.** SDKC-1 (broke the MW-SAFE-2 remediation), SDKC-2 (SDK-CORE-4 fixed `/invoke` only), CTX-1 (sharpened CTX-6), SAFE-1 (= SB-2), SB-2 (= SB-3), CT-1 (= RD-4), CT-2/T1-1 (d4b05ec's sibling gap), SAT-1 (= DEL-5), SAT-2 (= P0-9), SAT-3 (= DEL-2 class). Fix pattern: derive a behavioral test from each fix's own claim, and when a fix targets one path (invoke, arg-side, quickstart) explicitly cover its siblings (stream, global-option side, README).
4. **Event-loop-freezing hot paths.** The two new Tier-1 CLI modules regressed the async discipline the rest of the CLI keeps: PERF-1 (per-row-commit full FTS rebuild on the loop) and PERF-2 (per-chunk O(n²) tool-arg parse), with PERF-3/4/5/6 as the SDK-side P2 tail. Fix pattern: heavy/sync work goes off the loop (`to_thread`/worker), sqlite writes batch into one WAL transaction, and per-chunk work stays O(1) — parse args once at completion, not per delta.
5. **Delivery drift where CI doesn't look.** SAT-1 (release relock loop misses 3 of 6 packages), SAT-2 (VS Code strips creds — never CI'd), SAT-3 (phantom daemon install commands), plus the satellite P2 tail. Fix pattern: extend the release-please relock loop to all six packages and re-promote `lock-check` to blocking; add drift-guard tests that assert README/help command strings resolve against the real argparse surface; give the VS Code extension a push/PR compile+lint leg.

---

## 4. Agreed sequencing (this cycle)

### Wave 0 — Docs
Land this report; fix the stale command/env docs the findings name in passing (daemon README/quickstart, the `BOG_AGENTS_HOME`/`paths.home` manifest lie, the `_cleanup` "butcher never spawns a worker" comment).

### Wave A — CLI event-pump cluster (theme 1)
v5 CLIC-1 (P0) first, then CLIC-2/3/4/5, then CLIC-6/7. Route each surface through `run_worker` + TurnManager registration so a single-flight guard turns the class into a structural impossibility.

### Wave B — Approval-gate truth (theme 2)
v5 T1-1/T1-2/T1-3, SAFE-1/SAFE-2, SB-1, T1-4, CT-5 — wire the deterministic analyzers into `_eval_shell`, cover the batch tools + wildcard matchers, gate PTY tools, and enforce the butcher slice allowlist on `run_command`.

### Wave C — SDK correctness
v5 SDKC-1/SDKC-2/SDKC-4, CTX-1/CTX-2/CTX-3, SB-2/SB-3/SB-4 — plus fix the `test_api_deprecation` stacklevel failure so the SDK suite is green again.

### Wave D — Performance (theme 4)
v5 PERF-1/PERF-2 (default-path CLI freezes) first, then PERF-3/4/5/6.

### Wave E — Daemon + config
v5 DMN-7/DMN-8/DMN-10, CT-1/CT-3/CT-4/CT-6.

### Wave F — Satellites / delivery (cheap-only)
v5 SAT-1/SAT-2/SAT-3/SAT-5 — re-lock + extend the relock loop, unbreak the VS Code env allowlist, repoint the daemon README, guard the daytona import.

### Deferred by decision
Full ACP multi-session rework (SAT-4/P1-61), the VS Code webview rework (SAT-6 provider + streaming-flag fix), and the remaining P2s / undone quick-wins — schedule against 1.0 scope, not this cycle.

---

## 5. Shipped in the 1.0-hardening cycle (2026-08-20/21)

Branch `chore/1.0-hardening` (off `origin/main` @ v0.9.12). Every fix landed with
regression tests; SDK / CLI / daemon unit suites green throughout.

**Fixed (with tests):**
- **P0** — CLIC-1 (`/telephone` pump deadlock).
- **Wave A** (CLI event-pump) — CLIC-1..7: a single `_start_tracked_session`
  choke point puts `/butcher`, `/team run`, `/best-of-n`, `/jury` in
  TurnManager-tracked workers off the pump; `/telephone` and `/pipeline` run
  off-pump; `/background` completion fires via `_spawn`; peat waits on its own
  dispatched worker; `_resume_thread` is busy-guarded.
- **Wave B** (approval gates) — T1-1 (git global-option skip), T1-2/SAFE-2
  (exec-risk veto wired into `_eval_shell`), T1-3 (hook wildcard/regex/alternation
  matchers), T1-4 (Haiku ImportError fails closed), SAFE-1 (batch tools honor
  permissions + self-mod guard), SB-1 (PTY tools HITL-gated), CT-5 (butcher
  `run_command` allowlist).
- **Wave C** (SDK) — SDKC-1 (rbac-build crash), SDKC-4 (builder merge),
  SB-2 (`git switch`), SB-3 (atomic `write_file`), SB-4 (honest background exit),
  the `test_api_deprecation` baseline, CTX-1 (never commit a failed summary),
  CTX-3 (real model pricing + empty-name window guard).
- **Wave D** (perf) — PERF-1 (`/threads search` incremental + off-loop; ~3.4s→~5ms
  at 1000 threads), PERF-2 (incremental streamed-args scan), PERF-3 (sweeper
  memoization + O(E) reverse-pass), plus CTX-4 (offload-after-success retry).
- **Wave E** (daemon/config) — DMN-7 (full-name branch match), DMN-8 (`***`
  placeholder round-trip), DMN-10 (atomic run writes), CT-1 (butcher active-model),
  CT-3 (`BOG_AGENTS_HOME` wiring), CT-4 (per-server MCP `${VAR}` isolation),
  CT-6 (secure token writes).
- **Wave F** (satellites/delivery) — SAT-1 (re-lock + blocking lock-check +
  six-package relock loop), SAT-2 (VS Code provider-cred allowlist), SAT-3 (daemon
  README + quickstart command surface), SAT-5 (daytona `SandboxConformanceSuite`).

**Deferred to a follow-up (noted, not lost):**
- **CTX-2** (sweeper spliced inner of summarization) — the fix needs fragile
  private-class-name matching in `_apply_custom_middleware` plus canonical-order
  test changes; it is a not-yet-realized optimization (the sweeper still trims
  content), so it was held out of the 1.0 ordering surface rather than risked.
- **PERF-4** (per-marker write-once offload out of CWD) — a coherent rewrite that
  an interrupted change left half-applied; reverted cleanly, keeping PERF-3 + CTX-4.
- **PERF-5/PERF-6, SDKC-2, and the remaining unverified P2s / quick-wins** — carried
  forward.

---

# REVIEW.md v4 — Current-State Audit (2026-07-21)

> **Scope:** Whole monorepo, post the resiliency-hardening (2026-06-22), deepagents-parity
> (2026-07-11), and CLI world-class (2026-07-12) waves on `chore/resiliency-hardening`.
> **Method:** 58-agent workflow — 10 dimension auditors (SDK core / context middleware /
> safety middleware / backends / CLI core / CLI config+trust / routing+dreamscape / daemon /
> satellites / delivery), adversarial verification of every P0/P1 candidate (2 independent
> refuters per P0, 1 per P1; P2s ship unverified and are labeled so), plus status re-scores
> of v3's P0/P1s, CLI_AUDIT.md's fix-now list, and ROADMAP.md's 20 killer features.
> **Tally:** 65 findings → **1 P0 + 21 P1 confirmed** (4 of those are re-confirmed
> still-open v3 items), 8 downgraded, 1 refuted, 30 unverified P2s.
> **Killer-feature output** of this cycle lives in `ROADMAP.md` → "Killer features v2".
> **Verdict in one line:** The correctness core is now genuinely strong — the prior waves
> held, and every subsystem's load-bearing center passed adversarial audit. What v4 exposes
> is a **truth gap at the seams**: controls that the constrained party can switch off
> (RBAC/air-gap/butcher), guarantees that exist only in docstrings (daemon dispatch errors,
> cron catch-up, serve threads), two dispatch paths that bypass the turn state machine, and
> a delivery/satellite edge where everything CI never exercises turned out broken on first
> inspection (`pip install bog-agents-cli[all]` fails for every user today).

## 0. Executive summary — health by package

| Package | State | One-line |
|---|---|---|
| **SDK core** | **Strong center, demo-grade edges** | `create_agent` assembly, profiles, exclusion audits, and the deepagents drop-in all passed adversarial re-audit; prior fixes verified in place. The rot is in the two newest satellites: `builder.py` force-enables cost tracking and rides the deprecated kwarg backdoor it will not survive at 1.0, and `serve.py`'s thread API is an illusion (no history replay, no checkpointer on any documented path). |
| **SDK middleware (context)** | **Good core, seam defects** | Sweeper view-transform, event-based summarization, and eviction helpers are coherent and tested. Defects cluster where tests don't cross: CLI splice order inverts the documented sweeper-outside-summarization ordering, and `CheckpointingMiddleware` crashes every mutating tool call when git is missing/slow — on the CLI's default path. |
| **SDK middleware (safety)** | **Plumbing solid, framing outruns guarantees** | `permissions.py` and the expert engine are genuinely well-built. The recurring anti-pattern: **the constrained party administers the control** — RBAC and air-gap expose their own policy setters as model-callable tools, so they bound a cooperative model, not an adversarial one. |
| **SDK backends** | **Mature and well-hardened, two tools slipped the net** | Symlink/O_NOFOLLOW, atomic-write, and result-filtering work is thoughtful. But `multi_edit_file`/`read_many_files` bypass permission rules entirely, a dangerous-command `PermissionError` crashes the turn instead of becoming a tool error, and `git checkout -- <branch>` can never switch branches (breaks worktree merge). |
| **CLI core** | **Hardened in the small, ambient state machine in the large** | Resume/rewind and headless HITL are in good shape. But turn lifecycle is bare booleans on a 17,504-line god class (one line under its ratchet), and it shows: a defensive `finally` from a prior hardening pass corrupts the next queued turn (the cycle's one P0), and pipelines/file-watchers dispatch prompts with no busy-guard. |
| **CLI config/trust** | **Unusually well-engineered** | The freshly-landed OAuth, skill-trust, theme, and manifest modules passed audit with only integration-seam defects — headline: `BOG_AGENTS_MCP_TRUST` is documented, printed in the deny message, and read nowhere. |
| **CLI routing/dreamscape** | **Strong cores, thin integration shell** | The two audited invariants hold (judge failures never block a turn; dreamscape is a verified no-op unless enabled). Defects are all at TUI wiring seams: `/expert watch` is dead in the live app, `/butcher` is pinned to hardcoded Anthropic presets, and the effort registry misses Bedrock/Haiku — silently capping output at 1024 tokens on operator-routed easy turns. |
| **daemon** | **Good shape, undelivered promises** | Clean separation, atomic writes, HMAC auth. But several self-documented guarantees fail in code: dispatch errors can never be captured for any network target, cron misses fires with no catch-up, corrupt `jobs.json` is silently wiped on the next write, and token rotation never reaches the webhook endpoint (v3 P1-51, still). |
| **Satellites** (harbor/acp/daytona/vscode) | **Drifting where CI doesn't look** | Harbor's suite is green while every eval run has broken ls/grep/glob (drifted off the backend API). ACP is red at HEAD against a stale PyPI lock. Only daytona could be handed to a user without embarrassment. None are in CI — which is exactly why. |
| **Delivery** | **Spine works, edges all broken** | The three-package release pipeline demonstrably works. Every never-exercised path failed on first inspection: `[acp]`/`[all]` extras depend on a package that doesn't exist on PyPI, the GitHub Action's skills install always aborts, the daemon quickstart's first command doesn't exist, harbor+daytona lockfiles are stale today and nothing checks. |

Throughline: **v3 was about safety nets with holes; v4 is about promises the code doesn't keep.** Nearly every confirmed finding is a place where a docstring, config flag, or security framing asserts a guarantee the implementation quietly lacks.

---

## 1. Prior-cycle scorecard

- **v3 P0s: 8/10 fixed.** Still open: **P0-9** (VS Code extension strips provider API keys from the child env, so the documented env-var path cannot work) and **P0-10** (daemon quickstart documents a nonexistent command surface — re-confirmed this cycle as DEL-2).
- **v3 P1s: roughly two-thirds fixed.** The open remainder clusters in: satellites (ACP shared-agent V3-15/P1-61, VS Code P1-62/63/64), CI/workflows (V3-8/19/20, P1-66/69/70/71/73), daemon (P1-51 = DMN-2, P1-86), harbor (P1-59 partial, P1-60), and a CLI stragglers set (P1-9 lifecycle-BLOCK no-op, P1-18 scheduled-reports stub, P1-22 plaintext MCP install secrets, P1-25/26/27/32 dead-or-partial commands, P1-46 dreamscape counter, P1-48 = RD-2/RD-3 family).
- **CLI_AUDIT.md (2026-07-12): everything landed.** All six fix-now items (SEC-1, SEC-2, TEST-1, HITL, git-branch cache, ARCH-1 ratchet) verified fixed with tests; ports 1–8 shipped (port-8 stage 2 pending its `settings_screen` consumer); all three "defer" items (theme system, skill trust store, spec-compliant OAuth) subsequently shipped and verified.
- **ROADMAP.md killer features: 4 shipped / 8 partial / 8 not-started.** Detailed per-feature status is now recorded in ROADMAP.md ("Killer features v1 scorecard").

---

## 2. New findings — adversarially verified

IDs are stable (`v4 <ID>`); cross-reference them in commit messages (e.g. "fixes v4 CTX-1").
Every P0/P1 below survived independent refutation attempts with code-reading (and in seven
cases live-reproduction) evidence. Items marked *(= v3 X)* are re-confirmed prior findings,
listed for completeness but not counted as new.

### P0 — ship-blockers (1)

- **CLI-CORE-1** — `_cleanup_agent_task`'s `finally` clobbers the queued next turn's worker state (`app.py:14906`). The try block's last statement drains the queue, which *starts turn 2* (sets `_agent_running=True`, assigns `_agent_worker`) — then the `finally` unconditionally re-asserts `_agent_running=False; _agent_worker=None` while turn 2's coroutine is live. Turn 2 becomes uninterruptible, a third message runs concurrently instead of queueing. Live-reproduced. Fix: drain the queue *after* the finally restores state (S).

### P1 — serious (confirmed)

**SDK**
- **SDK-CORE-2** — every `AgentBuilder.build()` silently force-enables cost tracking (`CostConfig.enabled=True` default) and forwards it via the deprecated kwarg backdoor, spamming `DeprecationWarning` and guaranteeing wholesale `TypeError` at 1.0 (`builder.py:613`). Live-reproduced (S).
- **SDK-CORE-4** — serve's thread API is stateless-amnesia: only the newest message is ever sent to the agent, and both documented wirings build the agent with no checkpointer — turn 2 silently loses all context while `/history` implies continuity (`serve.py:211`) (M).
- **CTX-1** — `CheckpointingMiddleware._run_git` is a bare `subprocess.run` with no OSError/TimeoutExpired handling; on a machine without git in PATH, **every default-path CLI `write_file`/`edit_file`/`execute` crashes the turn** (`checkpointing.py:125`; CLI defaults `enable_checkpointing=True`). Live-reproduced (S).
- **MW-SAFE-2** — RBAC is fail-open and self-administered: `enable_rbac=True` restricts nothing until the model itself calls `set_active_role`; there is no operator surface to pin a role (`rbac.py:331`) (M).
- **SB-1** — dangerous-command `PermissionError` (incl. common `rm -r`) is uncaught by the execute tool and propagates out of the graph, aborting the turn instead of returning the intended "blocked" tool message (`filesystem.py:1704`). Live-reproduced (S).
- **SB-2** — `multi_edit_file` and `read_many_files` bypass filesystem permission rules at both the boundary middleware and in-tool layers: a `deny /secrets/**` rule is writable-through and readable-through (`permissions.py:159`) (M).
- **SB-3** — `git checkout -- <branch>` treats the branch as a pathspec (verified with real git), so `merge_worktree` never merges and the branch-restore safety net never restores (`worktree.py:398`) (S).

**CLI**
- **CLI-CORE-2** — default-mode HITL gates exactly six tools, but `GitToolsMiddleware` (default-on) ships mutating `git_commit`/`git_add`/`git_branch(checkout)`/`git_stash drop` with no approval prompt (`agent.py:1065`) (S).
- **CLI-CORE-3** — `/clear` resets `session_state.thread_id` but leaves `_lc_thread_id` stale: `/compact` then reads *and mutates* the pre-clear thread's checkpoint while reporting success for the current conversation (`app.py:3150`) (S).
- **CLI-CORE-4** — scheduled pipelines and file-watchers dispatch prompts via `_send_prompt_to_agent` with no busy-guard, spawning a second concurrent turn on the live thread and stealing the user's worker handle (`app.py:14377`) (M).
- **CT-1** — `BOG_AGENTS_MCP_TRUST` / `mcp.trust` is a dead override: defined in the registry, exposed in the manifest, printed in the non-TTY deny message ("set BOG_AGENTS_MCP_TRUST=1 to override") — and read nowhere. A CI user following the printed instruction is silently denied (`main.py:1526`) (S).
- **RD-1** — the native reasoning-effort registry has no Bedrock branch and no Haiku match, so the legacy `{max_tokens: 8192/1024, temperature: 0.7}` caps apply — the operator anthropic preset's easy tier truncates every routed turn at 1024 output tokens; the entire builtin bedrock preset is capped on all four tiers (`reasoning_effort.py:326`) (M).
- **RD-2** — `/expert watch start|stop` is dead in the live TUI: dispatched via `asyncio.to_thread`, where `asyncio.get_event_loop()` raises on Python ≥3.11; the only working start path (K2 resume) then can't be stopped. Live-reproduced (`expert_watch.py:361`) (S).
- **RD-4** — `/butcher` model resolution always resolves the hardcoded Anthropic preset tiers (operator `ensure_session` builds them even when operator mode is off), so the documented fall-back-to-active-model branch is unreachable — Bedrock/Ollama-only users get a hard failure, Anthropic users get silently switched models (`butcher.py:1030`) (S).
- **RD-5** — butcher containment is weaker than documented: the claimed job-level approval gate does not exist (plan → immediate execution, and operator auto-escalates prompts to butcher by default), and the slice file-allowlist is prompt-only — workers run LLM-authored `shell=True` commands screened only by the accident-catcher patterns, bypassing the CLI's HITL/permission system entirely *(sharpens v3 P1-42)* (`butcher.py:437`) (M).

**daemon**
- **DMN-1** — unattended-trigger "shell guardrails" are a documented no-op: `virtual_mode=True` restricts only file tools; the shell tool runs unrestricted on the host with `inherit_env=os.environ.copy()` handed to jobs whose prompts can ingest attacker-authored content (git-push triggers) *(downgraded from P0: requires a configured daemon job; still the daemon's biggest posture gap)* (`runner.py:438`) (M).
- **DMN-3** — `dispatch_errors` capture is dead code for every network target: email/slack/webhook/github dispatchers swallow their own failures, so a run whose delivery failed persists `COMPLETED` with an empty error — the exact failure the field was added to fix. Live-reproduced against all three (`runner.py:674`) (S).
- **DMN-4** — a corrupt or partially-invalid `jobs.json` loads as empty (one bad enum poisons the whole list-comprehension), and the next mutation **atomically replaces the file with the empty view** — destroying every job plus embedded secrets, no backup. Live-reproduced (`store.py:211`) (M).
- **DMN-5** — cron triggers silently miss fires: the matcher only fires when a tick's wall-clock matches the expression, so any restart, host sleep, or >60s tick overrun spanning the scheduled minute drops the job for the day; the docstring claims interval catch-up that was never implemented (`scheduler.py:104`) (M).
- *(= v3 P1-51)* **DMN-2** — token rotation never reaches the webhook endpoint; the leaked old token authenticates (and skips HMAC) forever (`api.py:804`) (S).

**Satellites & delivery**
- **SAT-1** — `HarborSandbox` drifted off the SDK backend API: the structured `als`/`agrep`/`aglob` surface raises `NotImplementedError`, so **every harbor eval run has broken ls/grep/glob tools** while harbor's legacy-name tests stay green. Empirically reproduced (`harbor/backend.py:412`) (M).
- **DEL-1** — published `[acp]`/`[all]` extras depend on `bog-agents-acp`, which does not exist on PyPI — `pip install 'bog-agents-cli[all]'` fails resolution for every user today; no release path exists for the acp package (`cli/pyproject.toml:137`) (S).
- **DEL-5** — lockfile drift is unguarded and present: harbor + daytona `uv.lock` fail `uv lock --check` today; root `make lock-check` fails for any fresh contributor and CI never runs it (`ci.yml:42`) (S).
- *(= v3 P0-10)* **DEL-2** — daemon quickstart's `bog-agents-daemon run` (and the whole `runs` family) doesn't exist; the systemd unit in the docs would crash-loop (S).
- *(= v3 P1-69)* **DEL-3** — the GitHub Action's skills install always aborts on the first skill: `((SKILL_COUNT++))` returns exit 1 under `bash -e`. Empirically reproduced (`action.yml:190`) (S).
- *(= v3 P1-66/70)* **DEL-4** — the VS Code release workflow runs bash-only syntax under the Windows default pwsh shell; the only build/publish path cannot complete (S).

### Downgraded to P2 after verification (real, but bounded)

- **SDK-CORE-1** — serve defaults `cors_origins=["*"]` with keyless localhost: a drive-by web page can drive the agent and read responses. Bounded because the default backend is ephemeral state (no host FS/shell) — impact is API-credit burn + reading the user's own local threads. Still an unforced error; default CORS to `[]`.
- **SDK-CORE-3** — middleware ordering validation is exact-type, so a *subclassed* `ParallelWorktreeMiddleware` + `enable_result_synthesis=True` crashes `create_agent` despite subclassing being the documented pattern.
- **CTX-2** — with the sweeper enabled, the overflow-clip path persists the *swept* view into canonical state and offloads elided text to the "full content" recovery file — breaking the lossless invariant on a narrow reachable path.
- **CTX-3** — cost/budget accounting uses exact-match pricing and the CLI passes no `model_name` at all, so every model is billed at the (5,15) default; a strict `budget_usd` can overshoot 3–5× (Opus, Bedrock ids).
- **MW-SAFE-1** — air-gap egress policy is model-mutable: `set_data_policy(allow_external=true)` is a model-callable tool that lifts the restriction the middleware exists to enforce.
- **RD-3** — the expert watcher runs its blocking LLM proposer directly on the TUI event loop (freeze reproduced; bounded by RD-2 making the watcher hard to start).
- **DMN-1** — see above (P0→P1).
- **SAT-2** — ACP's lock resolves bog-agents 0.8.7 from PyPI (repo is 0.9.9); its flagship HITL test fails at HEAD. (Root cause is a missing `[tool.uv.sources]` path pin, not the version skew per se.)

### Refuted (1) — do not re-flag

- **MW-SAFE-3** ("DLP redact mode never scans tool-call arguments"): the code observation is accurate, but in redact mode the model only ever *receives* redacted views, so it cannot emit the raw secret in tool args — the exfiltration scenario is unreachable as claimed. (An egress-DLP `wrap_tool_call` remains a worthwhile *enhancement*; see quick wins.)

### P2 — important, not urgent (30, unverified by design)

Recorded from the dimension audits without adversarial verification; treat severity as provisional. Highlights by theme — full evidence in the audit transcripts:

- **serve:** slow SSE clients hold concurrency slots forever (SDK-CORE-5); `enable_streaming`/`enable_websocket` are dead flags and the advertised WebSocket doesn't exist (SDK-CORE-6); `with_mcp()`/`with_sandbox(allow_dangerous)` are silent no-ops behind documented promises (SDK-CORE-7).
- **context:** empty-string model names prefix-match the first table entry → bogus 1M context window (CTX-4); a failed sweeper offload write is never retried yet `recall_swept` is advertised (CTX-5); a failed summarizer call commits `"Error generating summary: …"` as the permanent summary, and the default factory disables trimming for the summary call (CTX-6); overflow read_file slices drop image blocks (CTX-7); the CLI attaches the sweeper via `middleware=`, splicing it *inner* of summarization — inverting the documented ordering so sweep savings never defer compaction (CTX-8 — arguably the most consequential P2 here).
- **safety:** RBAC has no tool-call-boundary re-check (MW-SAFE-4); sync-path guardrails silently discard tripped async-only guardrails (MW-SAFE-5); a first-load rulebook parse error fails *open* with an empty rulebook (MW-SAFE-6); AuditTrail records model intent, not executions, while claiming FINRA-grade provenance and "immutable" logs that truncate (MW-SAFE-7).
- **backends:** CompositeBackend write/edit don't remap `files_update` keys (SB-4); `write_file` is in-place O_TRUNC while edit/upload are atomic (SB-5).
- **config/trust:** one undefined `${VAR}` header disables ALL MCP servers (CT-2); the OAuth token dir isn't owner-only-secured and the temp file is briefly 0644 (CT-3); the manifest under-reports provider-scoped thinking config despite its no-drift promise (CT-4).
- **dreamscape:** stale on-disk IMAGINING/DREAMING states survive crashes and falsely credit imagination success stats, skewing the auto-disable kill-switch (RD-6).
- **daemon:** file-change scan blocks the event loop up to 50k stats per tick (DMN-6); git-push branch patterns never match `feature/*` (DMN-7); PATCH accepts the `***` redaction placeholder as a literal secret — making webhook HMAC forgeable via the natural read-modify-write flow (DMN-8).
- **satellites/delivery:** satellites absent from CI (SAT-3 = V3-8); daytona integration suite imports a vanished upstream class (SAT-4); ACP caches one agent for all sessions (SAT-5 = V3-15); VS Code sidebar view has no provider (SAT-6 = P1-62) and the webview overwrites prior replies (SAT-8); README overclaims per-package CI (DEL-6); the uv version pin in CI is a silent no-op (DEL-7 = V3-20); release-please daemon version marker missing (DEL-8).

---

## 3. Systemic themes

1. **The constrained party administers the control.** RBAC and air-gap expose policy setters as model tools; butcher's approval gate exists only in a docstring; default-on git tools sit outside HITL. Controls that a cooperative model honors and an adversarial one switches off are *worse* than nothing — they emit false assurance. One fix pattern: policy pinned at construction (FeatureConfig/operator config), mutation tools dropped or HITL-gated, plus a unit test asserting no security middleware exposes policy-mutation tools to the model.
2. **Documented-but-undelivered guarantees.** Daemon dispatch-error capture, cron catch-up, serve's thread continuity, `enable_websocket`, checkpointing's "missing git is logged-and-disabled", butcher's gate, `BOG_AGENTS_MCP_TRUST` — each was *believed* shipped because a docstring/flag/message says so. The countermeasure is behavioral tests derived from the doc claims (the daemon's flag-assert tests are the cautionary example).
3. **Two dispatch paths, one ambient state machine.** CLI-CORE-1/-4 are the same disease: `_agent_running`/`_agent_worker` are bare attributes mutated from five call sites. A small single-flight TurnManager (asyncio.Lock + begin/end context managers) that *every* path (submit, queue drain, pipelines, peat, butcher) must traverse turns the bug class into a structural impossibility.
4. **CI blindness at the edges = guaranteed rot.** Every surface with no automated run on record was broken on first inspection (harbor's drifted backend, acp at HEAD, the Action's skills loop, the VS Code workflow, stale lockfiles, phantom PyPI extras). The cheapest structural fix in this report: a satellite CI leg + `uv lock --check` job + a nightly published-install canary + an action.yml self-test.
5. **Registry/table divergence.** Three divergent model tables (pricing, context windows ×2) with two lookup semantics; an effort registry missing the repo's own primary provider (Bedrock). One `bog_agents/_model_catalog.py` with normalized lookup ends the class.

---

## 4. Recommended sequencing

### Wave 0 — Default-path correctness (days; all S/M)
CLI-CORE-1 (P0), CLI-CORE-4, CLI-CORE-3, CTX-1, SB-1, RD-1 — every one is reachable by a normal user on defaults. Then the TurnManager refactor (theme 3) to lock the class.

### Wave 1 — Trust honesty (the "constrained party" sweep)
CLI-CORE-2 (git tools into HITL + a build-time mutating-tool/interrupt coverage assertion), RD-5 (butcher plan-approval gate + enforced slice allowlist), DMN-1 (drop shell or sandbox it for unattended triggers, stop inheriting env), MW-SAFE-2 + MW-SAFE-1 (operator-pinned RBAC/air-gap policy), CT-1, plus the self-modification guard for `.bog-agents` trust surfaces (see ROADMAP v2 #24).

### Wave 2 — Delivery & satellites truth (mostly S; highest embarrassment-per-fix)
DEL-1 (publish or de-advertise acp), DEL-2/3/4 (docs, Action, workflow), DEL-5 + satellite CI legs + nightly install canary + action.yml self-test, SAT-1 (rebase HarborSandbox on BaseSandbox + a shared `SandboxConformanceSuite` in `bog_agents.testing`), SAT-2 (path pin).

### Wave 3 — Daemon reliability
DMN-2/3/4/5 + croniter-style next-fire computation with persisted schedule state, watchdog-based file triggers (adopts the deferred watchdog dependency from the PR #150 backlog), startup recovery for orphaned RUNNING runs, per-job retry/backoff, secret *references* in jobs.json instead of cleartext.

### Wave 4 — serve as a real surface
SDK-CORE-4 (history replay for checkpointer-less agents — turns the flaw into a feature), SDK-CORE-1 (CORS default `[]` + key-required), SDK-CORE-5 (decouple production from consumption), SDK-CORE-6/7 (kill or implement dead flags), SDK-CORE-2 (builder → FeatureConfig assembly). This wave is a prerequisite for ROADMAP v2's fleet/teleport features.

### Quick wins (each ≤ half a day)
`/queue` inspect/drop; seed the token tracker on thread resume; `/clear` undo hint (+ fixes CLI-CORE-3 at the same site); `git switch` helper (SB-3); atomic `write_file` (SB-5); export `ResultSynthesisMiddleware`/`GuardrailMiddleware` in lazy imports; serve OpenAPI drift test; version-coherence tests per package; root `make ci`; egress-DLP `wrap_tool_call` (the constructive residue of refuted MW-SAFE-3); `/theme preview` + `/theme export`; `/mcp trust` verb for remote-only projects; dreamscape state doctor.

---

# REVIEW.md v3 — Current-State Audit (2026-06-14)

> **Scope:** Whole monorepo — SDK (`libs/bog-agents`), CLI (`libs/cli`), daemon, ACP, harbor, partners/daytona, VS Code extension, CI/packaging, docs.
> **Method:** 4 parallel Senior-Principal-Engineer auditors (SDK / CLI / satellites / engineering-quality), each cross-checking findings against the *installed* langchain + langgraph and the running source. All anchors below were spot-verified against current code on branch `fix/dependabot-vulns`.
> **Builds on:** the v2 audit (June 12, 2026). The v2 Wave-0 correctness sweep largely landed — **bug classes A/B/C, the three default-reachable P0s (P0-1 AGENTS.md overwrite, P0-6 `--default-model`, P0-8 project-hook trust gate), and the first round of secret-at-rest/injection hardening are fixed** and are *not* re-litigated here. The v2 "shipped-but-dead" CLI cluster (`/think`, `/checkpoint load`, `/worktrees`) is likewise treated as resolved unless a finding below names a *new* regression.
> **Already shipped on this branch (do NOT re-flag):** (1) unified Claude-Code-style permission mode — Shift+Tab cycles default→accept-edits→plan, `--permission-mode` flag, status indicator, headless parity; (2) the `apply_stdin_pipe` non-tty deadlock fix; (3) Bedrock via SigV4 **and** bearer-token API key.
> **Verdict in one line:** The correctness crisis v2 diagnosed is over — the bug-class regressions are fixed and the headline features work on first use. What remains is a **maturity gap, not a stability gap**: the canonical-order safety net has two un-asserted holes that silently defeat prompt caching and audit-redaction, the `create_agent` docstring still advertises a safety stack that isn't wired (including a `SafeToolsMiddleware` class that does not exist), the v2 "highest-leverage" CI gate shipped as a 6-of-59 hand-picked test instead of the parametrized structural fix, and CI scope + dependency automation never caught up to the breadth of the codebase. None of these are hard; together they are exactly the seam through which the *next* silent regression will ship.

---

## 0. Executive summary — health by package

| Package | State | One-line |
|---|---|---|
| **SDK** (`libs/bog-agents`) | **Healthy, two ordering hazards** | Lazy-loading, street-sweeper invariant, HITL filesystem-permission glue, and public-API discipline are all genuinely solid. But two un-asserted middleware-ordering bugs ship today (Memory-after-PromptCaching defeats caching; Audit-before-DLP writes unredacted secrets), and the `create_agent` docstring describes a safety stack that is largely fictional. |
| **CLI** (`libs/cli`) | **Coherent, one real correctness bug** | The new permission-mode is well-wired with a single source of truth and defensive headless parity. The one live bug: Bedrock SSO auto-refresh's interactive `aws sso login` path is unreachable in the TUI because it derives interactivity from the server subprocess's stdin. The project's mandated encoding-safety lint (PLW1514) is disabled in this package's ruff config, with live regressions already present. God-class accretion continues. |
| **daemon** | **Healthiest satellite, two posture gaps** | FastAPI/dispatch path is well-hardened (bounded errors, TOCTOU re-resolve, overlap protection). Gaps are runtime safety (`virtual_mode=False`, no HITL on an unattended runner reachable by webhook/git-push) and dependency surface (advertises unlanded modal/runloop sandbox extras; uses the deprecated legacy-feature-flag kwarg path). Never type-checked. |
| **acp** | **Works for single-session, latent multi-session bug** | Clean ACP-error UX. But `_agent`/`_cwd` are effectively global, so a mode switch on one session can re-root another session's agent; client-supplied `mcp_servers` is silently dropped. |
| **harbor** | **Functional, conventions drift** | Eval harness runs (70 tests). Trajectory write/read omit `encoding=utf-8` (P0-H violation, crashes on non-en-US Windows); hardcoded `version()="0.0.1"`; no CI. |
| **partners/daytona** | **Build-broken** | Test suite cannot import at all — stale `uv.lock` pins langgraph 1.1.0 while the SDK now imports `langgraph.channels.delta.DeltaChannel` (needs ≥1.2.0). Verified: `graph.py:19` imports it, `daytona/uv.lock:1271` pins `1.1.0`. |
| **vscode-extension** | **Cleanest satellite** | Strict CSP nonce, env allowlisting, absolute-path validation, spawn-without-shell. Only nit: `Math.random()` nonce. (v2's P0-9/P1-62/63/64 are separate, still-open items.) |
| **CI / quality gates** | **Systematic holes** | The flagship "drive a fake turn through every middleware" gate is a 6-middleware regression test. CI lints/tests only sdk/cli/daemon — acp/harbor/daytona have none. No Dependabot despite hand-maintained CVE floors. Daemon never type-checked. ty is configured so permissively on SDK/CLI it would not have caught last cycle's outage. |

The throughline: **v2 was about features that crash; v3 is about safety nets with holes.** Every P1 below is a place where a correct-looking change can regress silently because the test/lint/doc that should catch it is absent, scoped too narrowly, or actively disabled.

---

## 1. Issues — deduplicated, severity-ranked

IDs are stable (`V3-N`). "v2 xref" links the lineage where relevant.

### P0 — ship-blockers

#### V3-1 — daytona partner is build-broken: stale `uv.lock` pins langgraph 1.1.0, SDK needs ≥1.2.0 (`DeltaChannel`)
- **Why it matters:** `pytest --collect-only` fails for all three daytona test files with `ModuleNotFoundError: No module named 'langgraph.channels.delta'`. The SDK at `graph.py:19` does `from langgraph.channels.delta import DeltaChannel` (langgraph 1.2.0+) and declares `langgraph>=1.2.0,<2.0.0`, but the partner depends on the SDK editable and was never re-locked after the floor bump — `daytona/uv.lock:1271` still resolves `langgraph==1.1.0`. The published `langchain-daytona` integration cannot import. This is the *exact* lockfile-drift class V3-19 (no CI lock-check) guarantees will recur.
- **Files:** `libs/partners/daytona/uv.lock`, `libs/partners/daytona/pyproject.toml`, `libs/bog-agents/bog_agents/graph.py:19`
- **Fix:** `uv lock` in `libs/partners/daytona` to resolve `langgraph>=1.2.0`; add the package to `make lock-check` + CI (see V3-19). Re-lock `acp` in the same pass (its lock pins langgraph 1.1.6, also below floor — imports today by luck).

---

### P1 — serious

#### V3-2 — `MemoryMiddleware` runs *after* `AnthropicPromptCachingMiddleware`, silently defeating prompt caching whenever `memory=` is set
- **Why it matters:** Verified ordering in `create_agent`: PromptCaching appended at `graph.py:1012`, then Memory at `graph.py:1014`. Later list position = inner wrapper, so Memory runs **after** caching. Memory's `modify_request` appends a *new* system content block (`memory.py` → `append_to_system_message`, `_utils.py:6-23`); PromptCaching tags the *last* system block with `cache_control`. Net: the cache breakpoint is no longer on the last block and the injected memory text falls outside the cached prefix — and if memory varies per thread/run the marked prefix shifts and busts hits. This is precisely the invariant `test_middleware_canonical_order.py` exists to protect, but it only asserts `names[-1] == "AnthropicPromptCachingMiddleware"` on the *no-memory* stack, so the regression is untested. Same family as v2's "PromptCaching must be innermost" rule.
- **Files:** `libs/bog-agents/bog_agents/graph.py:1012-1014`, `middleware/memory.py`, `middleware/_utils.py`, `tests/unit_tests/test_middleware_canonical_order.py`
- **Fix:** Append `MemoryMiddleware` **before** `AnthropicPromptCachingMiddleware` (it's a context-preparation concern, exactly where the docstring at `graph.py:328` already places it); extend the canonical-order test to assert caching is last *with* `memory=[...]` supplied.

#### V3-3 — `AuditTrailMiddleware` is wired *before* `DLPMiddleware`, contradicting the docstring's own DLP→Audit invariant (compliance hazard)
- **Why it matters:** The `create_agent` docstring (`graph.py:341`) states "`DLPMiddleware` must run before `AuditTrailMiddleware` if you want redacted values to land in audit logs." The actual wiring appends Audit at `graph.py:803` and DLP at `graph.py:861` — Audit is outer, DLP inner — so on the inbound path Audit records the request **before** DLP redacts it, writing unredacted secrets to the audit trail: the exact failure the docstring warns against. `_validate_middleware_ordering` cannot catch it (no `requires=` declared between them). Either the wiring or the documented invariant is wrong; both ship today. Distinct from v2's P1-5 (audit logging empty tool-calls, fixed).
- **Files:** `libs/bog-agents/bog_agents/graph.py:803,861`, `middleware/dlp.py`, `middleware/audit_trail.py`
- **Fix:** Decide intended semantics, reorder the appends so DLP precedes Audit, and add a `requires=[DLPMiddleware]` on Audit (or a canonical-order assertion) so it can't regress.

#### V3-4 — `create_agent` docstring documents a default middleware stack that does not exist; ~9 named middleware are never wired (incl. safety-relevant ones)
- **Why it matters:** The "Middleware execution order" docstring (`graph.py:317-348`) enumerates `LifecycleHooksMiddleware`, `HttpHooksMiddleware`, `LangSmithMiddleware`, `SafeToolsMiddleware`, `ExpertRulesMiddleware`, `RulesMiddleware`, `ThinkingMiddleware`, `ContextPackingMiddleware`, `CodeIntelligenceMiddleware` (as unconditional), and `IntelligentCompactionMiddleware` as if installed by default. Grep of the assembly code shows **none** are referenced outside the docstring (CodeIntelligence is gated behind `enable_code_intelligence` at `graph.py:784`). A user reading the docstring will believe safety/observability middleware are active out of the box when they are not — dangerous because the list includes the safety-relevant `SafeToolsMiddleware`/`ExpertRulesMiddleware`.
- **Files:** `libs/bog-agents/bog_agents/graph.py:317-348`
- **Fix:** Rewrite the docstring to the real assembly: TodoList → optional skills/permissions → the `enable_*` feature block in append order → default tail (Filesystem/SubAgent/Summarization/PatchToolCalls) → user middleware → profile extras → PromptCaching → Memory → HITL. (Pairs with V3-2/V3-3 — fix the order, then document what's actually there.)

#### V3-5 — Bedrock SSO auto-refresh's interactive `aws sso login` path is unreachable in the TUI (interactivity derived from server-subprocess stdin)
- **Why it matters:** `BedrockRefreshMiddleware` is attached with `interactive=sys.stdin.isatty()` evaluated at graph-build time (`agent.py:1698`). But the graph is built by `make_graph()` inside the langgraph-dev **server subprocess** (`server_graph.py` → `create_cli_agent`), whose stdin is not a terminal and whose stderr is redirected to the per-port log. So `isatty()` is always False in the server process: the middleware always takes the non-interactive branch (banner to the server log + re-raise), and the feature's whole point — spawn `aws sso login`, open a browser, retry — never fires in the primary interactive flow. It only works when `create_agent` is built in-process (tests); the `-p` headless path is non-interactive by design anyway. (Note: the Bedrock dual-auth itself works — this is about the *refresh-on-expiry UX*, which is not in the "already shipped, don't re-flag" set.) This is a specific instance of the broader server-subprocess boundary smell in V3-themes.
- **Files:** `libs/cli/bog_agents_cli/agent.py:1698`, `bedrock_refresh.py`, `server_graph.py`
- **Fix:** Flow real client-side interactivity through `ServerConfig`/env (the way `auto_approve`/`interactive` already do) instead of re-deriving it from subprocess stdin; or surface the refresh prompt to the client process where the terminal/browser actually are.

#### V3-6 — `PLW1514` (unspecified-encoding) is disabled in the CLI ruff config, contradicting CLAUDE.md and leaving the P0-H encoding sweep unenforced — with live regressions
- **Why it matters:** CLAUDE.md's file-handling section mandates "ruff's `PLW1514` should be left enabled going forward" (the safety net for the Windows non-en-US cp1252/cp932/cp949 decode crashes closed by the P0-H sweep). But `libs/cli/pyproject.toml:275` lists `"PLW1514"` in `ignore`. The net is off in the most file-heavy package, and regressions already exist: `server_manager.py` `_write_checkpointer` (~line 155) and `_write_pyproject` (~line 184) call `Path.write_text(content)` with no `encoding=`. `_write_pyproject` interpolates `cli_dir.as_uri()` — on a machine whose install path contains non-ASCII (a non-ASCII Windows username) that content is non-ASCII and the bare write re-encodes through the locale codec: exactly the crash class P0-H closed. The documented invariant is enforced by neither lint nor review.
- **Files:** `libs/cli/pyproject.toml:275`, `libs/cli/bog_agents_cli/server_manager.py`, `CLAUDE.md`
- **Fix:** Remove `PLW1514` from CLI `ignore` (or add it to the CI gate) and fix the existing call sites with `encoding="utf-8"`. Sweep `harbor`/`daemon` for the same (see V3-12, V3-13).

#### V3-7 — The flagship every-middleware fake-turn CI gate shipped as a narrow ~6-middleware regression test, not the parametrized structural fix v2 prescribed
- **Why it matters:** v2 §2/§5 named "a CI gate that constructs every middleware and drives one fake-model turn" as "the single highest-leverage action" that "prevents the entire class from recurring." The implemented `test_model_call_smoke.py` hard-codes ~6 middleware (RepoMap, PlanMode, Thinking, AutoQuality, CostTracker, AuditTrail) — i.e. the modules already known broken last cycle. 59 middleware override `wrap_model_call`/`awrap_model_call`; the 12 former bug-class-B modules (`adaptive_context`, `agent_replay`, `hot_reload_skills`, `http_hooks`, `model_cascade`, `offline_mode`, `provider_retry`, `scheduled_runs`, `security_audit`, `self_improving`, `smart_approvals`) are exercised by no model-turn test, and ~60 of 86 middleware modules have no dedicated test file. The exact regression class — a future langchain/langgraph bump silently breaking a hook — can still ship green. This is the root cause v2 identified, left structurally open.
- **Files:** `libs/bog-agents/tests/unit_tests/middleware/test_model_call_smoke.py`, `middleware/model_cascade.py`, `middleware/smart_approvals.py`
- **Fix:** Replace the hand-picked list with a `pytest.mark.parametrize` enumerating every middleware (or every entry `create_agent` can wire), constructing each with minimal deps and driving one fake `wrap_model_call` + `awrap_model_call`, asserting no exception and the message-count/order invariant.

#### V3-8 — CI does not lint or test acp, harbor, or partners/daytona; VS Code extension only builds on manual dispatch
- **Why it matters:** `ci.yml`'s lint/test matrices enumerate only `{sdk, cli, daemon}`. `libs/acp` (ships to PyPI at 0.0.4), `libs/harbor` (the eval harness, py3.12+), and `libs/partners/daytona` (published, with unit+integration tests) get no PR/main CI. `.pre-commit-config.yaml` *does* wire harbor+acp into format+lint — so the only thing checking them is a developer's local hook, which CI cannot assume ran (and which has silently diverged from the CI matrix). The VS Code extension compiles/lints only on `workflow_dispatch`, so a TS regression in `extension.ts` lands on main unchecked (v2's P0-9/P1-63/64 live there). V3-1 (daytona build-broken) is the direct consequence: a CI job would have caught it the moment the SDK bumped its langgraph floor.
- **Files:** `.github/workflows/ci.yml`, `.github/workflows/vscode-extension.yml`, `libs/acp/pyproject.toml`, `libs/harbor/pyproject.toml`, `libs/partners/daytona/pyproject.toml`
- **Fix:** Add acp/harbor to the lint+test matrices (harbor on py3.12+ only), add a partners/daytona job, and add a push/PR-triggered compile+lint job for the extension (publish stays manual).

#### V3-9 — No Dependabot/Renovate config — every CVE floor is hand-maintained, guaranteeing the manual sweep must be repeated each cycle
- **Why it matters:** No `.github/dependabot.yml` (or `renovate.json`) exists (verified). The entire transitive-CVE hardening on *this* branch (cryptography, urllib3, requests, pyasn1, pygments, aiohttp, pillow, langsmith, python-multipart, pyjwt — each with an inline CVE id across the sdk/cli/daemon pyprojects) is hand-applied across five separate `uv.lock` files. Without automated dependency PRs + an advisory feed, the next wave of advisories ages silently until someone re-audits by hand — directly undercutting the value of the sweep that produced this branch. (The daytona `urllib3>=2.6.3` vs daemon `>=2.7.0` drift in the tech-debt list is a live example of floors already diverging.)
- **Files:** `.github/dependabot.yml` (new), `libs/{bog-agents,cli,daemon}/pyproject.toml`
- **Fix:** Add `dependabot.yml` covering pip/uv for all five package dirs + github-actions; group minor/patch bumps to cut PR noise.

#### V3-10 — Daemon source is never type-checked, and daemon test-lint failures are swallowed with `|| true`
- **Why it matters:** CLAUDE.md calls the daemon "the healthiest satellite" and asks contributors to keep it green when `create_agent` changes — but `libs/daemon/Makefile`'s `lint` target runs only `ruff check` with **no `ty`** (unlike sdk/cli/acp/harbor). A network-facing FastAPI service that handles secrets and dispatches to Slack/webhook/email/GitHub gets zero static type analysis. Worse, the test-lint line `ruff check tests/ --ignore=ANN,S,ARG || true` swallows any failure. The daemon also has only 6 test files for 8 source modules with the broadest blast radius in the repo.
- **Files:** `libs/daemon/Makefile`, `libs/daemon/pyproject.toml`
- **Fix:** Add a `type` target running `ty check bog_agents_daemon`, wire it into `make lint`, drop the `|| true`, add `ty` to the daemon test dependency group.

#### V3-11 — daemon runs agents fully autonomously with `virtual_mode=False` (path guardrails disabled) and no HITL/permissions
- **Why it matters:** `runner.py:384` builds `LocalShellBackend(..., virtual_mode=False)`, which emits a runtime DeprecationWarning: "disables path-based guardrails: absolute paths and `..` can bypass root_dir." The same `create_agent` call passes no `interrupt_on`, no permissions, no HITL (grep returns nothing). The daemon is unattended *by design* and can be triggered by webhook/git-push whose `trigger_context` (and, for skill/pipeline jobs, file contents) is partially attacker-influenced — so disabling the one remaining path-containment guardrail in an auto-approve runner is a real defense-in-depth gap. Related to V3-15 (the safeguard CLAUDE.md points operators at doesn't exist).
- **Files:** `libs/daemon/bog_agents_daemon/runner.py:384`
- **Fix:** Default `virtual_mode=True` (or gate `False` behind an explicit opt-in env var); document that operators must scope `working_dir` tightly.

#### V3-12 — harbor trajectory write/read omit `encoding=utf-8` — P0-H violation, crashes eval on non-en-US Windows
- **Why it matters:** `bog_agents_wrapper.py:404` does `trajectory_path.write_text(json.dumps(...))` with no encoding, and `:199` does `config_path.read_text()` with no encoding. Trajectory JSON embeds raw model output + the task instruction (arbitrary Unicode); the harbor config is user-authored. On a Windows non-en-US locale a single non-ASCII char aborts the eval with `UnicodeEncodeError`/`DecodeError`. Sibling files `reporter.py`/`export.py` already do this correctly, so it's an inconsistency — and `PLW1514` (mandated "left enabled," but see V3-6) would flag it.
- **Files:** `libs/harbor/bog_agents_harbor/bog_agents_wrapper.py:199,404`
- **Fix:** Add `encoding="utf-8"` to both calls; run a PLW1514 pass across harbor.

#### V3-13 — Satellites depend on the SDK's deprecated legacy-feature-flag kwarg path (removed at bog-agents 1.0)
- **Why it matters:** `daemon/runner.py:387` passes `enable_git_tools=True` as a bare `create_agent` kwarg; harbor passes `enable_memory`/`enable_skills`/`enable_shell` to `create_cli_agent`. In the SDK these flow through `**legacy_feature_flags` → `_resolve_feature_config` (`graph.py:259-285`), which emits a DeprecationWarning on every call (tagged P1-6) and is explicitly slated for deletion at 1.0. All satellites cap the SDK at `<1.0.0` so nothing breaks yet, but every daemon job run emits a deprecation warning today and the integration hard-breaks the moment the SDK majors — with no CI smoketest asserting the satellites still build against the latest SDK.
- **Files:** `libs/daemon/bog_agents_daemon/runner.py:387`, `libs/harbor/bog_agents_harbor/bog_agents_wrapper.py`, `libs/bog-agents/bog_agents/graph.py:259-285`
- **Fix:** Migrate satellites to `config=FeatureConfig(enable_git_tools=True, ...)` now; add a CI smoketest building satellites against the latest local SDK.

#### V3-14 — Dead/aspirational sandbox deps: daemon advertises `modal` + `runloop-api-client` extras that were never landed
- **Why it matters:** CLAUDE.md (P0-F) states only daytona exists; modal/runloop/quickjs "were never landed." Yet `daemon/pyproject.toml` exposes `sandbox=[...modal, runloop-api-client...]`, `modal-sandbox`, `runloop-sandbox`, and the daemon `uv.lock` carries both. No daemon code wires a modal/runloop backend, so these install heavyweight deps for a capability that does not exist — the same dead-feature class P0-F was meant to close, relocated into the daemon's dependency surface. (Note: the CLI's `--sandbox modal`/`runloop` extras *are* real and import-graceful — so CLAUDE.md's "only daytona" partners note is itself now stale; see docs-drift theme.)
- **Files:** `libs/daemon/pyproject.toml`, `libs/daemon/uv.lock`
- **Fix:** Drop modal/runloop extras from the daemon (keep `daytona-sandbox`), or only ship an extra once a corresponding partner package exists.

#### V3-15 — `ACP` server is not multi-session safe: shared `self._agent`/`self._cwd` leak state across sessions
- **Why it matters:** `AgentServerACP` keys cwd/mode by session (`_session_cwds`, `_session_modes`) but holds a single `self._agent` and `self._cwd`. `_reset_agent` (`server.py:429`) builds `AgentSessionContext(cwd=self._cwd, ...)` from the shared attribute, not `self._session_cwds[session_id]`. `prompt()` refreshes `self._cwd` first; `set_session_mode()` calls `_reset_agent(session_id)` **without** refreshing it — so a mode switch on session B rebuilds B's agent rooted at whichever session last ran `prompt()`. And `self._agent` is overwritten per prompt, so two interleaved sessions clobber one compiled graph. Fine for Zed's typical single-session use, latent correctness bug otherwise. (v2's P1-61 named the symptom; this is the verified mechanism + fix site.)
- **Files:** `libs/acp/bog_agents_acp/server.py:429`
- **Fix:** Key `_agent` by `session_id` and source cwd from `self._session_cwds` inside `_reset_agent`.

---

### P2 — important, not urgent

#### V3-16 — `SafeToolsMiddleware` is cited as the primary adversarial safeguard in 3 places but the class does not exist
- **Why it matters:** CLAUDE.md says the real safeguard is "HITL + SafeToolsMiddleware"; `backends/local_shell.py:43-44` repeats it; the `create_agent` docstring lists it in the safety section (`graph.py:326`). But `safe_tools.py` defines only `SafeToolRule`, `SafeToolsConfig`, `is_tool_safe()`, `load_safe_tools_config()` — **no `SafeToolsMiddleware` class** (verified: grep returns zero matches in the module). The documented safety story points users at a component that cannot be instantiated. Folds into V3-4 (fictional default stack) and V3-11 (daemon points operators here too).
- **Files:** `libs/bog-agents/bog_agents/middleware/safe_tools.py`, `backends/local_shell.py`, `graph.py:326`
- **Fix:** Either implement `SafeToolsMiddleware` (a `wrap_tool_call` middleware consuming `SafeToolsConfig` to auto-approve matched calls and gate the rest), or correct all three references to name the real mechanism (`HumanInTheLoopMiddleware` + `FilesystemPermissionsMiddleware` + `ExpertRulesMiddleware`).

#### V3-17 — Permission-mode name drift across surfaces: `acceptEdits` (flag/help) vs `accept-edits` (TUI/indicator/`/permissions`), plus a wrong help string
- **Why it matters:** The flag uses camelCase `acceptEdits` (`main.py:607`, echoed in help at `main.py:616` and `ui.py:123`); the TUI cycle, `_current_permission_mode`, status indicator, and `/permissions` all use kebab `accept-edits` (`app.py:815,9057,15039`; `widgets/status.py:281`). So `bog-agents --permission-mode accept-edits` errors even though every in-app surface shows `accept-edits`. Worse, `main.py:616` help claims "Shift+Tab cycles default → acceptEdits → plan" — a spelling the TUI never displays (it shows `accept-edits`/AUTO-EDIT). No test asserts flag↔TUI naming consistency. (This is *within* the just-shipped permission-mode feature — a polish gap, not a re-flag of the feature itself.) Note the borrowed `acceptEdits` here means the smart rule-engine auto-mode, not Claude Code's literal auto-approve-edits semantics.
- **Files:** `libs/cli/bog_agents_cli/main.py:607,616`, `ui.py:123`, `app.py`, `widgets/status.py:281`
- **Fix:** Pick one canonical spelling (accept the camelCase flag as Claude-Code-compatible *and* accept a kebab alias, or normalize everything to kebab); add a parity test walking the flag choices against the cycle + status label map; doc the semantic difference.

#### V3-18 — `ty` is configured so permissively across SDK and CLI that it catches almost nothing
- **Why it matters:** `libs/bog-agents/pyproject.toml [tool.ty.rules]` sets `invalid-argument-type`, `invalid-assignment`, `no-matching-overload`, `unresolved-attribute`, `missing-argument`, `invalid-return-type`, `not-iterable`, `unused-ignore-comment` (and more) all to `ignore`; CLI adds `possibly-missing-attribute`, `not-subscriptable`, `unsupported-operator`, `call-non-callable`, `unknown-argument`. With attribute/argument/overload checking off, ty would not have caught last cycle's bug-class-A `AttributeError` nor bug-class-B wrong-arity hook. Some suppressions are legitimately needed for `AgentMiddleware` generics, but the blanket disabling removed the safety that would have prevented the v2 outage.
- **Files:** `libs/bog-agents/pyproject.toml`, `libs/cli/pyproject.toml`
- **Fix:** Re-enable `unresolved-attribute` and `missing-argument` at minimum, scoped via per-file `ty: ignore` at the real generics false-positive sites; treat `unused-ignore-comment` as a warning so dead suppressions get cleaned.

#### V3-19 — No coverage threshold, no CI lockfile-freshness check, and a `.coveragerc` referenced by a Makefile target that doesn't exist
- **Why it matters:** Three gaps: (1) no package enforces a coverage floor (`make test` prints term-missing but gates nothing, so coverage erodes silently); (2) `make lock-check` exists at repo root but is never invoked by `ci.yml` — a PR editing a pyproject dependency without re-locking passes CI (this is exactly how V3-1's daytona drift shipped); (3) `libs/bog-agents/Makefile`'s `coverage` target passes `--cov-config=.coveragerc` but no such file exists anywhere.
- **Files:** `.github/workflows/ci.yml`, `libs/bog-agents/Makefile`, root `Makefile`
- **Fix:** Add a modest `fail_under`; run `make lock-check` as a CI job; create the `.coveragerc` or drop the flag.

#### V3-20 — Composite action's `UV_VERSION` pin is a dead no-op; CI matrix omits the declared 3.11 floor and never tests harbor's 3.12 floor
- **Why it matters:** (Re-confirmation of v2 P1-71, still open.) `.github/actions/uv_setup/action.yml` declares `env: UV_VERSION: "0.8.17"` at the action root and references `${{ env.UV_VERSION }}` in the step — a composite action's root `env:` is not exposed to step expressions, so `version:` resolves empty and setup-uv installs `latest`; every CI run can float to a new uv. Separately, all packages declare `requires-python >=3.11` but `ci.yml` tests only 3.12/3.13 ("3.11 temporarily disabled") — a 3.11-only regression ships to users on the supported floor — and harbor (`>=3.12`) is in no matrix.
- **Files:** `.github/actions/uv_setup/action.yml`, `.github/workflows/ci.yml`, `libs/harbor/pyproject.toml`
- **Fix:** Move the pin into the step's own `env:` (or hard-code it); restore 3.11 to the matrix (or raise the published floor to match reality); add harbor on 3.12.

#### V3-21 — God-class handlers ignore the controller-delegation convention (`_handle_team_command` is ~700 lines inside `app.py`)
- **Why it matters:** CLAUDE.md mandates thin handlers that delegate to a standalone controller (the `expert_controller.py` pattern). The pattern exists (expert/sidecar/orchestrator/sweep controllers) but most large handlers don't follow it: in `app.py` (~16.7k lines, 341 methods) the longest are inline business logic — `_handle_team_command` ~700, `_handle_mcp_command` ~417, `_handle_peat_command` ~400, `_handle_harbor_command` ~366, `_handle_agent_command` ~349, `_handle_qa_command` ~321. `_handle_team_command` has no `team_controller.py` even though `team_config.py`/`team_orchestration.py` already exist to delegate to. The deferred app.py-refactor (MEMORY.md) tracks the broad extraction; the near-term actionable is landing controllers for the handful of >300-line handlers.
- **Files:** `libs/cli/bog_agents_cli/app.py`, `team_config.py`, `team_orchestration.py`
- **Fix:** Extract `team_controller.py` first (modules exist), then the other >300-line handlers; logic becomes testable without the TUI.

#### V3-22 — `/permissions` (the permission-mode inspector) has no headless twin
- **Why it matters:** The permission-mode feature shipped with `--permission-mode` headless parity (sets the booleans) but no way to *inspect* the resolved posture non-interactively: `/permissions` is TUI-only and absent from `headless_commands.HEADLESS_COMMANDS`. CLAUDE.md's headless guidance says informational/config slash commands should get a headless twin — `/permissions` is exactly that. An agent driving the CLI via `bog-agents command "/permissions"` gets the generic not-headless error.
- **Files:** `libs/cli/bog_agents_cli/headless_commands.py`, `app.py`
- **Fix:** Add `_cmd_permissions(args)` reading `settings.shell_allow_list` + the resolved mode, mirroring `_cmd_config`.

#### V3-23 — `PlanModeMiddleware` is effectively dead in the SDK default path: `enabled=False` with no `create_agent`-level toggle
- **Why it matters:** `enable_plan_mode` wires `PlanModeMiddleware(enabled=False)` at `graph.py:679`. The only way to flip it is the model calling `toggle_plan_mode` or external code mutating `.enabled` — `create_agent` exposes no parameter to start in plan mode, and the instance is function-local so callers can't reach it. The CLI implements its own permission-mode `plan` independently (the v2-anticipated "two plan-mode impls"). Net: `enable_plan_mode=True` ships an extra tool but no actual plan-mode behavior for a programmatic SDK user.
- **Files:** `libs/bog-agents/bog_agents/middleware/plan_mode.py`, `graph.py:679`, `libs/cli/bog_agents_cli/widgets/status.py`
- **Fix:** Add a `start_in_plan_mode`/`plan_mode` kwarg threaded into `PlanModeMiddleware(enabled=...)`, or document that plan mode is model-/CLI-driven only and the flag controls only tool presence.

#### V3-24 — User-supplied `ResultSynthesisMiddleware` via `middleware=` crashes the build with a `requires` ValueError that the flag path silently auto-fixes
- **Why it matters:** `ResultSynthesisMiddleware` declares `requires=[ParallelWorktreeMiddleware]` (`result_synthesis.py:47`), enforced by `_validate_middleware_ordering`. The `enable_result_synthesis` flag path auto-creates a `ParallelWorktreeMiddleware` when absent (`graph.py:973-979`); a user passing `ResultSynthesisMiddleware()` directly via `middleware=` gets no such auto-fix and hits `ValueError` at `graph.py:1037` unless they also remembered to pass the dependency. Inconsistent contract between the two supported entry points for the same component.
- **Files:** `libs/bog-agents/bog_agents/middleware/result_synthesis.py:47`, `graph.py:973-979,1037`
- **Fix:** Apply the same auto-provisioning to user-supplied middleware, or document the requirement prominently on the class.

#### V3-25 — ACP `new_session` accepts `mcp_servers` from the client and silently drops it
- **Why it matters:** `server.py:new_session` (~139-156) takes `mcp_servers`, normalizes `None`→`[]`, then never references it — no MCP wiring, no warning. A Zed client that configures session MCP servers sees them accepted by the protocol but the agent has zero MCP tools.
- **Files:** `libs/acp/bog_agents_acp/server.py:139-156`
- **Fix:** Wire MCP registration into the agent, or log a clear "MCP servers not yet supported" notice so the gap isn't silent.

#### V3-26 — daytona partner dependency floor `daytona>=0.1.0` is meaningless vs the API it uses (~0.148)
- **Why it matters:** `partners/daytona/pyproject.toml` pins `daytona>=0.1.0` but `sandbox.py` uses `FileDownloadRequest`/`FileUpload`/`SessionExecuteRequest(run_async=True)`/`process.execute_session_command`/`process.get_session_command_logs` — APIs that exist only in a far newer daytona (lock resolves 0.148.0; the daemon pins `>=0.113.0,<1.0.0`). A clean install can resolve an ancient daytona lacking every symbol `sandbox.py` imports, producing a confusing `ImportError` instead of a clear version error.
- **Files:** `libs/partners/daytona/pyproject.toml`, `libs/daemon/pyproject.toml`
- **Fix:** Raise the floor to match the daemon (`>=0.113.0,<1.0.0`).

#### V3-27 — Documentation count-drift: READMEs and CLAUDE.md disagree with each other and with the source on middleware count
- **Why it matters:** `README.md` and `libs/bog-agents/README.md` say "80+ / ~80 middlewares," CLAUDE.md says "~90," and the source has 101 files in `bog_agents/middleware` (86 non-underscore modules; 59 override `wrap_model_call`). All wrong by different amounts, with no drift test (unlike the help-screen drift test the project maintains). Part of the broader doc-drift family (V3-14's stale partners note; V3-4's fictional stack; v2 P0-10/P1-86 daemon docs still open).
- **Files:** `README.md`, `libs/bog-agents/README.md`, `CLAUDE.md`
- **Fix:** Pick one source-of-truth count, or add a drift test asserting the README number matches `len(_LAZY_IMPORTS)`.

#### V3-28 — daemon token-file read/write omit `encoding=utf-8` (secret-bearing file, convention/lint)
- **Why it matters:** `main.py:57/59/285` and `api.py:179` read/write the daemon auth token with no encoding. Tokens are hex so no crash in practice, but CLAUDE.md mandates `encoding="utf-8"` on all `read_text`/`write_text` and PLW1514 should flag it. (The surrounding security handling — chmod 0600 + Windows `icacls /inheritance:r` — is otherwise solid and cross-platform.)
- **Files:** `libs/daemon/bog_agents_daemon/main.py:57,59,285`, `api.py:179`
- **Fix:** Add `encoding="utf-8"` for convention/lint compliance.

#### V3-29 — Stale upstream metadata + leftover non-tracked `partners/runloop/` cruft
- **Why it matters:** `acp/pyproject.toml:53` and `harbor/pyproject.toml:50` set `[project.urls] Twitter = "https://x.com/LangChain"` and harbor's description says "Harbor integration with LangChain Bog Agents" — copy-paste remnants that ship in the published wheel metadata of the bogware fork. Separately, `libs/partners/runloop/` exists on disk but is not git-tracked, containing only a stale `.venv` (editable `bog_agents 0.6.4`) + `.pytest_cache` — confusing cruft that makes it look like a second partner exists (corroborates P0-F that runloop was never landed).
- **Files:** `libs/acp/pyproject.toml:53`, `libs/harbor/pyproject.toml:50`, `libs/partners/runloop` (delete)
- **Fix:** Fix the URLs/description; delete the ignored `partners/runloop/` directory.

---

## 2. Systemic themes

- **Safety nets with un-asserted holes (the v3 signature).** The canonical-order test, the model-call smoke test, and the PLW1514 lint all exist *as concepts* but are scoped narrowly enough (no-memory stack only; 6-of-59 middleware; disabled in the file-heaviest package) that the very regressions they're meant to catch — V3-2 caching-defeat, V3-3 audit/DLP order, V3-6/V3-12 encoding crashes, V3-7 next dependency-bump break — slip straight through. The fix everywhere is "widen the assertion to cover the real surface," not "write a new test."

- **Docstring/doc vs. reality drift, now safety-relevant.** v2 was "shipped features don't work"; v3 is "docs describe a system that isn't built." The `create_agent` docstring advertises a 9-middleware safety/observability default stack that is never wired (V3-4) and names a `SafeToolsMiddleware` class that doesn't exist (V3-16) — cited in three places including a backend that tells operators it's their adversarial safeguard, and a daemon that runs with guardrails off (V3-11). Plus middleware-count drift (V3-27) and a stale partners inventory (V3-14). Doc-drift here is a security-posture lie, not a cosmetic one.

- **CI scope never caught up to the codebase breadth.** Five Python packages + a TS extension, but CI gates only three of them (V3-8); no Dependabot (V3-9); daemon never type-checked (V3-10); no lock-check/coverage gate (V3-19); a dead uv pin and a too-narrow Python matrix (V3-20). The daytona build-break (V3-1) is the proof: a one-line SDK floor bump silently broke a published package because nothing tested it.

- **ty is decorative on the two largest packages.** Attribute/argument/overload/assignment categories globally ignored on SDK + CLI (V3-18) — the suppression list grew to absorb `AgentMiddleware`-generics false positives but took genuine safety with it. Needs scoped per-file ignores so the global config can re-enable real checks.

- **God-class accretion continues.** `app.py` at ~16.7k lines / 341 methods (V3-21) keeps absorbing inline handler logic against the project's own controller-delegation convention; the refactor is deferred (MEMORY.md) but the near-term controllers (team first) are landable now.

- **Server-subprocess boundary smell.** Any client-side affordance (tty, browser, terminal prompts) computed inside the graph build will be wrong because the graph runs in the langgraph-dev subprocess — V3-5 (Bedrock interactive refresh) is the first concrete victim. Worth a documented rule + grep that client-interactivity must flow through `ServerConfig`/env, not be re-derived in `server_graph.py`.

- **Headless/TUI parity is close but incomplete.** Permission-mode headless parity shipped well, but the inspector (`/permissions`, V3-22) and several informational commands have no headless twin, and the permission-mode spelling drifts across flag/TUI/help/inspector (V3-17). A registry-driven headless set + a single naming-parity test would close the family.

---

## 3. Quick wins (high-impact, low-effort)

Ordered by impact-per-diff. Each is a small, high-confidence change.

1. **V3-2 / V3-3 — reorder two middleware appends in `graph.py`** (Memory before PromptCaching; DLP before Audit) and extend `test_middleware_canonical_order.py` to assert both with a non-empty `memory=`/DLP+Audit stack. Two-line wiring fix that closes a silent cache-defeat and a compliance hazard, plus the test that locks them.
2. **V3-1 + V3-19(2) — `uv lock` daytona (and re-lock acp), then wire `make lock-check` into CI.** Unbreaks a published package and prevents the whole lockfile-drift class with one CI job.
3. **V3-6 / V3-12 / V3-28 — re-enable PLW1514 in the CLI ruff config and fix the handful of bare `write_text`/`read_text` sites** (server_manager, harbor wrapper, daemon token). Restores a documented invariant the lint will then enforce forever.
4. **V3-9 — add `.github/dependabot.yml`** for pip/uv across the five package dirs + github-actions. One file; makes the CVE sweep self-sustaining.
5. **V3-10 — add a `ty` target to the daemon Makefile and drop the `|| true`** on its test-lint line. Brings the secret-handling network service up to the repo's static-analysis baseline.
6. **V3-4 / V3-16 — rewrite the `create_agent` docstring to the real assembly and fix the three `SafeToolsMiddleware` references.** Pure-text; removes a safety-posture lie. (Implementing the class is a larger follow-up.)
7. **V3-14 + V3-29 — drop the daemon's modal/runloop extras, fix the two `x.com/LangChain` URLs, delete the untracked `partners/runloop/` directory.** Cosmetic/metadata cleanup that removes reviewer confusion and dead heavyweight deps.
8. **V3-17 — accept a kebab `--permission-mode accept-edits` alias and fix the `main.py:616` help string**, plus a one-line parity test walking flag choices vs the status label map.
9. **V3-22 — register a `_cmd_permissions` headless twin** mirroring `_cmd_config`.
10. **V3-13 — migrate the daemon to `config=FeatureConfig(enable_git_tools=True)`** to stop the per-job DeprecationWarning ahead of the 1.0 break.

> The single highest-leverage *non-trivial* item remains **V3-7**: replace the 6-middleware smoke test with the parametrized every-middleware fake-turn gate v2 prescribed. It is the only structural fix that stops the next silent regression — and it is the through-thread behind V3-1, V3-2, V3-3, and V3-8.

---

# Bog Agents — Holistic Review & Roadmap v2 (June 12, 2026)

> **Scope:** Whole monorepo — SDK (`libs/bog-agents`), CLI (`libs/cli`), daemon, ACP, harbor, VS Code extension, partners, CI/packaging, docs.
> **Method:** Multi-agent audit. Phase 1 re-verified all 88 prior REVIEW.md items against today's code. Phase 2 ran 20 parallel subsystem deep-readers producing 217 fresh findings. Phase 3 (adversarial verification) was interrupted by a model outage; **all 96 P0/P1 candidates were then re-verified one-by-one against the current code and the *installed* langchain 1.2.15 API** before anything below was trusted. 95/96 confirmed, 1 false positive.
> **Supersedes:** the May 16, 2026 review (now the "prior cycle"). Prior items are scored, not re-litigated.
> **Verdict in one line:** The May ship-blockers were almost all fixed and three flagship roadmap features actually shipped — but a **langchain 1.x API migration was completed for ~30 middlewares and silently missed ~18 others**, so a large fraction of opt-in middleware and several headline CLI features now **crash the moment they're enabled or are quietly dead**. The framework's real problem flipped from "stub vertical-market code" to "shipped-but-broken core features." None of the fixes are hard; the gap is a test suite that never drives a real model call through each middleware.

---

## 0. Executive summary

Since May the team did the unglamorous work: 22 of the prior cycle's items are **fixed** (8 of 11 P0s, including the SSRF gate's core, the lazy-import contract, the Windows-ACL honesty, the API-key registry merge, the worktree task-rooting, and the regex demotion), and — impressively — **four top-tier roadmap features actually landed**: `/sidecar` (T-1), the AGENTS.md memory cascade (T-4), `/orchestrate` (T-8), and the neuro-symbolic **Expert Mode engine** (T-11). The vertical-market stub cluster (P0-A) that dominated the last review is **deleted**. The package classifier was honestly downgraded to Beta.

But the audit surfaced a **new and more dangerous failure mode than the one it replaced.** LangChain 1.x changed the middleware contract: `ModelRequest` became an immutable dataclass mutated via `request.override(...)`, and `wrap_model_call` settled on a two-argument `(self, request, handler)` signature. About 30 middlewares were migrated correctly. **Roughly 18 were not** — and because the test suite exercises these middlewares' constructors and tool lists but almost never drives a real model call through them, every one of these regressions shipped green. The result:

- **Two mechanical bug classes** (detailed in §2) break ~18 middlewares + several CLI features. Each one raises `AttributeError` or `TypeError` on the **first model call after the feature is enabled**. This includes `/think`, plan mode, the repo-map (which the built-in `review`/`refactor`/`careful` profiles turn on), `cost_tracker`, `audit_trail`, `model_cascade`, and the dreamscape `laws`/`shared_memory`/`imagination` layers.
- **A "shipped-but-dead" pattern** across the CLI: `/think` toggles an attribute nothing reads; `/checkpoint load` restores nothing; `/worktrees cancel` lies; `/worktrees merge` can never reach its success state; the VS Code activity-bar chat view is contributed but never registered; OAuth-for-MCP is advertised but unwired.
- **Three genuinely default-reachable P0s**: the `conversation_branch` memory tier silently **overwrites the user's hand-authored AGENTS.md** on the first `remember()`; `--default-model` crashes on an `ImportError` typo; and project-local `.bog-agents/hooks/` execute **arbitrary code from any cloned repo with no trust gate** — an RCE on `git clone && bog-agents`.

The honest framing for a 1.0: the **breadth of the feature surface is now a liability**, because a meaningful share of it doesn't work when switched on, and nothing in CI catches that. The competitive position (below) is genuinely strong — dreamscape, expert-rules with `/why`/`/prove`, operator/butcher/JTBD routing, cross-editor surface — but every one of those is undercut if a skeptical user's *first* `/think on` throws a traceback.

**The single highest-leverage action is not any one fix — it is a CI gate that constructs every middleware and drives one fake-model turn through it.** That one test would have caught ~25 of the findings below, and prevents the entire class from recurring.

---

## 1. Prior-cycle scorecard

Re-verified against today's code (June 12, 2026):

| Status | Count | Items |
|--------|-------|-------|
| **fixed** | 22 | P0-B, P0-D, P0-E, P0-F, P0-G, P0-I, P0-J, P0-K, P1-1, P1-2, P1-3, P1-4, P1-6, P1-10, P1-22, OSS-3, OSS-4, OSS-7, T-1 /sidecar, T-4 AGENTS.md cascade, T-8 /orchestrate, T-11 expert mode |
| **partial** | 15 | P0-A, P0-C, P0-H, P1-5, P1-8, P1-9, P1-14, P1-19, P2-8, P2-11, OSS-1, T-2 blocking hooks, T-10 MCP marketplace UI, D-1 dreamscape self-improvement, D-5 citations default |
| **open** | 50 | P1-11, P1-12, P1-13, P1-15, P1-16, P1-17, P1-18, P1-20, P1-21, P1-23, P1-24, P1-25, P2-1..7, P2-9, P2-10, P2-12..17, OSS-2, OSS-5, OSS-6, OSS-8..10, T-3, T-5, T-6, T-7, T-9, M-1..M-10, D-2, D-3 |
| **obsolete** | 1 | P1-7 (SSOAuthMiddleware — removed) |

**Notable since May:** `/sidecar`, AGENTS.md cascade, `/orchestrate`, and the full Expert Mode engine (`expert_rules.py` + `expert_engine/` + `/why`/`/prove` + a dreamscape `rule_proposer.py`) all shipped. T-2 blocking hooks landed as a *new* `project_hooks.py` with a real `{"action":"block|allow|modify"}` decision contract (but introduced P0-8 — no trust gate). T-10 MCP marketplace UI shipped (`/mcp marketplace`). D-5 added an `enable_provenance_loop` umbrella flag.

> **P0-A remnants:** the vertical cluster is deleted, but `enable_multi_agent=True` still routes to a `from bog_agents.middleware.multi_agent_orchestrator import …` that no longer exists → `ModuleNotFoundError` (new finding **P1-1**); `browser_agent_fa.py`, `model_portfolio.py`, and `competitive_intel.py` survive as FA-flavored in-memory stubs; and the `pyproject.toml:13` comment still describes the deleted "STUB banner" scheme. **P0-C / P1-6 / P1-74:** the SSRF gate's hostname/IP check shipped and is tested, but it does **not** re-validate HTTP redirects or guard DNS-rebinding TOCTOU — a public URL that 302-redirects to `169.254.169.254` still reaches IMDS. **OSS-1:** `get_default_model()` still hard-returns Anthropic even when only `OPENAI_API_KEY` is set.

---

## 2. The systemic root cause — an incomplete langchain 1.x migration

Two mechanical patterns account for the majority of the confirmed correctness P0/P1s. Both are verified against the installed **langchain 1.2.15**, where `ModelRequest` is `@dataclass(init=False)` with fields `model, messages, system_message, tools, …`, an `override(**kwargs)` method, and `wrap_model_call(self, request, handler)` / `awrap_model_call(self, request, handler)`.

### Bug class A — `append_to_system_message(request, …)` (should be `request.system_message`)

`append_to_system_message(system_message, text)` expects a `SystemMessage | None` and reads `system_message.content_blocks`. Passing the whole `ModelRequest` raises `AttributeError: 'ModelRequest' object has no attribute 'content_blocks'`, and the returned `SystemMessage` is then handed to `call_next` in place of the request. The correct idiom (used by ~30 siblings) is:
```python
request = request.override(system_message=append_to_system_message(request.system_message, TEXT))
```
**Broken in 6 modules:** `repo_map.py` (636,646), `plan_mode.py` (146,161), `thinking.py` (296,323), `auto_quality.py` (346,364), dreamscape `laws.py` (427), dreamscape `shared_memory.py` (424). (Findings P0-2,3,4,7 · P1-4,44.) `thinking.py` is doubly broken: it also calls `request.model_copy(...)` — a Pydantic method absent on the dataclass — and its `_get_model_name()` reads `request.model` (a `BaseChatModel`) as if it were a `str`, so it always returns `""` and native thinking is never bound (use `bog_agents._models.get_model_identifier`).

### Bug class B — `wrap_model_call(self, request, call_next, runtime)` (three-arg, should be two)

These define an async hook with an extra `runtime` parameter and call `await call_next(request, runtime)`. When langchain invokes `mw.wrap_model_call(request, handler)` it binds `call_next=handler` and leaves `runtime` unfilled → `TypeError` on the first model call. **Broken in 12 SDK middlewares** — `adaptive_context`, `agent_replay`, `hot_reload_skills`, `http_hooks`, `model_cascade`, `offline_mode`, `provider_retry`, `scheduled_runs`, `security_audit`, `self_improving`, `smart_approvals` — **plus CLI `bedrock_refresh.py`**. (Findings P0-5 · P1-7,8,13,14,15,37 and others.) Fix: rename to the correct two-arg `wrap_model_call`/`awrap_model_call` and call `handler(request)`; read `runtime` from `request.runtime` where needed.

### Bug class C — `ModelResponse` attribute misuse

`cost_tracker` (P1-3) never records usage and never enforces the budget; `audit_trail` (P1-5) logs empty tool-calls for every response; dreamscape `laws` output-check (P1-43) is dead — all three read attributes off `ModelResponse` that don't exist on the installed type. These need the response shape corrected against the real `ModelResponse` API.

> **Why CI missed all of this:** `test_lazy_import_health` and the per-middleware tests assert construction and `tool_names`, never a model turn. A single parametrized test — *construct every middleware, run one fake-model `wrap_model_call`* — is the structural fix (see §5, Wave 0).

---

## 3. New findings (verified)

### New P0 — ship-blockers (verified, default-reachable)

#### P0-1 — conversation_branch `remember()` silently overwrites (destroys) the user's AGENTS.md
- **Where:** `libs/bog-agents/bog_agents/middleware/conversation_branch.py`:93 | data-loss | default
- **Impact:** The `project` memory tier's `source_path` is hardwired to the user's `AGENTS.md`, and `remember()`'s default tier is `project`. `_save_memory_tier` does a truncating `Path.write_text`, rewriting the file as a `# project memory` + `key: value` dump. The first time the agent calls `remember(...)` (or `promote_memory(to_tier="project")`), the user's hand-authored AGENTS.md is destroyed. The CLI wires this middleware unconditionally (`agent.py:1641`), so the SDK feature flag does not protect CLI users.
- **Fix:** Point the writable tier at a managed file (`.bog-agents/memory/project.jsonl`); treat AGENTS.md as read-only context; write atomically; stop parsing arbitrary prose as `key: value`.

#### P0-6 — `--default-model` crashes with ImportError (`detectprovider` typo)
- **Where:** `libs/cli/bog_agents_cli/main.py`:1801 (and :1949) | correctness | default
- **Impact:** `from bog_agents_cli.config import detectprovider` — the real symbol is `detect_provider`. The documented `bog-agents --default-model <spec>` flag crashes with a traceback + panic dump before parsing. Reproduced live.
- **Fix:** `detectprovider` → `detect_provider` at both import + call sites; add a CLI argv test.

#### P0-8 — Project-local hooks execute arbitrary code from a cloned repo with no trust gate
- **Where:** `libs/cli/bog_agents_cli/project_hooks.py`:218 | security | default
- **Impact:** `.bog-agents/hooks/` scripts in any cloned repo auto-execute on the first user prompt (`user-prompt` hook) and before every tool call (`pre-tool` hook) — an RCE on `git clone && bog-agents`. No approval, no fingerprint.
- **Fix:** Gate behind the same per-project trust mechanism as stdio MCP (`mcp_trust` fingerprint + prompt-once + re-prompt on change). Do not execute any project hook until approved.

### New P1 — serious (verified)

| ID | Cat | Reach | Finding | Location |
|----|-----|-------|---------|----------|
| P0-10 | doc-drift | default | Daemon docs document a CLI that doesn't exist (`run`, `job add`, `runs`, `install-*`) + wrong port | `docs/daemon/quickstart.md`:43 |
| P0-9 | correctness | default | VS Code `buildChildEnv` strips all provider API keys (+DBus/proxy) — README path can't work | `libs/vscode-extension/src/extension.ts`:117 |
| P1-25 | stub-as-real | default | `/think` is permanently dead — scans `self._middleware`, never assigned | `app.py`:9969 |
| P1-27 | stub-as-real | default | `/checkpoint load` restores nothing — just prompts the model claiming a switch | `app.py`:7825 |
| P1-29 | ux-breakage | default | `/worktree create` with an invalid branch name raises uncaught ValueError | `app.py`:9847 |
| P1-33 | ux-breakage | default | Long-running slash handlers run inline on the App message pump — `/async wait` freezes input | `app.py`:12438 |
| P1-34 | correctness | default | Butcher/`/compact` reset `_agent_running` without draining the queue — queued msgs stuck | `app.py`:10446 |
| P1-36 | test-gap | default | Non-interactive API-key/Bedrock pre-flight silently dead — same `detectprovider` typo | `main.py`:1949 |
| P1-38 | security | default | Setup wizard writes API key to `~/.bog-agents/.env` plaintext, default perms | `config.py`:1768 |
| P1-49 | security | default | Project-level remote (SSE/HTTP) MCP servers auto-loaded with no trust gate | `mcp_tools.py`:749 |
| P1-52 | data-loss | default | `JobRun.dispatch_errors` silently dropped on every read-back from disk | `daemon/store.py`:152 |
| P1-53 | correctness | default | One malformed cron field aborts the scheduler tick — starves all later jobs | `daemon/scheduler.py`:56 |
| P1-54 | security | default | Job secrets (SMTP pw, GitHub token, webhook HMAC) persisted world-readable | `daemon/store.py`:200 |
| P1-55 | correctness | default | Manual `/jobs/{id}/run` bypasses overlap protection — concurrent double-exec | `daemon/api.py`:629 |
| P1-56 | data-loss | default | Completing a run clobbers concurrent job-config edits with a stale snapshot | `daemon/runner.py`:92 |
| P1-58 | security | default | ACP allowlist parser misses `;` newline `$()` backticks — "Always allow" auto-approves injection | `acp/utils.py`:237 |
| P1-59 | data-loss | default | Harbor `aedit` corrupts/fails edits — unescaped replacement in perl `s///` | `harbor/backend.py`:331 |
| P1-60 | correctness | default | Harbor `aglob_info` never matches wildcards — `shlex.quote` disables globbing | `harbor/backend.py`:483 |
| P1-61 | correctness | default | ACP shares one agent/cwd/cancel flag across all sessions | `acp/server.py`:445 |
| P1-63 | ux-breakage | default | VS Code context-menu Review/Explain/Fix discard the response if chat panel closed | `vscode/extension.ts`:212 |
| P1-64 | correctness | default | VS Code spawned CLI processes never tracked/killed — `cliProcess` dead code | `vscode/extension.ts`:203 |
| P1-71 | supply-chain | default | `UV_VERSION` pin in composite action is a silent no-op | `.github/actions/uv_setup/action.yml`:23 |
| P1-72 | correctness | default | SDK imports `langgraph` in ~15 modules but pyproject doesn't declare it | `libs/bog-agents/pyproject.toml`:28 |
| P1-73 | security | default | Action persistent memory: default repo scope + bare restore-keys → cross-PR poisoning | `action.yml`:130 |
| P1-78 | data-loss | default | Parallel `edit_file` lost-update race parked as non-strict xfail | `tests/.../test_file_system_tools.py`:357 |
| P1-86 | doc-drift | default | Daemon README trigger/output YAML schema is invented (env-var indirection fields) | `daemon/README.md`:100 |
| P0-2 | correctness | opt-in | RepoMapMiddleware crashes every model call (bug class A) — breaks review/refactor/careful profiles | `repo_map.py`:636 |
| P0-3 | correctness | opt-in | PlanModeMiddleware crashes when plan mode active (bug class A) | `plan_mode.py`:146 |
| P0-4 | correctness | opt-in | ThinkingMiddleware crashes once `/think on` (bug class A + `model_copy` + dead `_get_model_name`) | `thinking.py`:296 |
| P0-5 | correctness | opt-in | smart_approvals + 3 others wrong `wrap_model_call` signature (bug class B) | `smart_approvals.py`:380 |
| P0-7 | correctness | opt-in | LawsMiddleware never injects laws/constitution — AttributeError swallowed every call | `dreamscape/laws.py`:427 |
| P1-1 | correctness | opt-in | `enable_multi_agent=True` hard-crashes `create_agent` (module deleted, flag+wiring remain) | `graph.py`:714 |
| P1-2 | security | opt-in | Command injection in `BaseSandbox.grep_raw` via unquoted `glob` | `backends/sandbox.py`:369 |
| P1-3 | correctness | opt-in | cost_tracker never records usage / enforces budget (bug class C) | `cost_tracker.py`:415 |
| P1-4 | correctness | opt-in | auto_quality crashes on model call (bug class A) | `auto_quality.py`:346 |
| P1-5 | correctness | opt-in | audit_trail logs empty tool_calls for every response (bug class C) | `audit_trail.py`:462 |
| P1-7 | correctness | opt-in | HttpHooksMiddleware wrong signature (bug class B) | `http_hooks.py`:501 |
| P1-8 | correctness | opt-in | HotReloadSkillsMiddleware wrong signature (bug class B) | `hot_reload_skills.py`:245 |
| P1-9 | security | opt-in | LifecycleHooksMiddleware BLOCK action is a no-op for model calls | `lifecycle_hooks.py`:332 |
| P1-12 | correctness | opt-in | RulesMiddleware crashes when a project rule matches (bug class A variant) | `rules.py`:490 |
| P1-13 | correctness | opt-in | ProviderRetryMiddleware incompatible hook signature (bug class B) | `provider_retry.py`:177 |
| P1-15 | correctness | opt-in | OfflineModeMiddleware broken signature + dead tool-blocking + global socket timeout | `offline_mode.py`:428 |
| P1-16 | security | opt-in | Notification tool injects LLM text into AppleScript & PowerShell | `notifications.py`:82 |
| P1-17 | security | opt-in | ParallelWorktree merge passes LLM-controlled `target_branch` to git unvalidated | `worktree.py`:525 |
| P1-18 | stub-as-real | opt-in | `scheduled_reports.run_report_now` reports success without generating a report | `scheduled_reports.py`:285 |
| P1-19 | stub-as-real | opt-in | Expert Mode only asserts the `tool_call` fact — the rest of the fact substrate is missing | `expert_rules.py`:302 |
| P1-22 | security | opt-in | `/mcp install` copies vault secrets into plaintext `~/.bog-agents/.mcp.json` + argv | `app.py`:3261 |
| P1-26 | correctness | opt-in | `/replay run` fires all recorded steps concurrently | `app.py`:7241 |
| P1-28 | security | opt-in | Replay "in-memory only" secret vars inlined in cleartext into saved scripts | `app.py`:6949 |
| P1-31 | correctness | opt-in | `/worktrees merge` can never succeed — requires status "done" never set | `app.py`:11551 |
| P1-32 | stub-as-real | opt-in | `/worktrees cancel` lies — sets cancelled but task keeps running | `app.py`:11539 |
| P1-35 | correctness | opt-in | `_run_agent_task` TimeoutError handler formats `turn_timeout_seconds` that can be None | `app.py`:13954 |
| P1-37 | stub-as-real | opt-in | BedrockRefreshMiddleware can never attach (`isinstance(model, str)` always False) | `agent.py`:1694 |
| P1-40 | cross-platform | opt-in | `reset_agent` writes AGENTS.md without encoding — UnicodeEncodeError on Windows | `agent.py`:701 |
| P1-41 | correctness | opt-in | `/orchestrate --parallel` crashes (TypeError) when a subtask hits the outer timeout | `orchestrator.py`:536 |
| P1-42 | security | opt-in | Butcher runs LLM/worker shell commands via `shell=True` with no HITL/sandbox | `butcher.py`:744 |
| P1-43 | stub-as-real | opt-in | LawsMiddleware output check + reject_on_violation dead (bug class C) | `dreamscape/laws.py`:470 |
| P1-44 | correctness | opt-in | SharedMemoryMiddleware injection dead (bug class A) | `dreamscape/shared_memory.py`:424 |
| P1-45 | stub-as-real | opt-in | ImaginationMiddleware outcome detection dead — failure counter never increments | `dreamscape/imagination.py`:437 |
| P1-46 | correctness | opt-in | LifecycleMiddleware resets `consecutive_tool_failures` on every model call | `dreamscape/lifecycle.py`:239 |
| P1-47 | correctness | opt-in | `/expert watch start|stop` fail from the TUI — `asyncio.get_event_loop()` inside a worker | `expert_watch.py`:361 |
| P1-48 | performance | opt-in | Expert watcher runs a synchronous `model.invoke` on the Textual event loop | `expert_watch.py`:270 |
| P1-51 | security | opt-in | Daemon token rotation doesn't invalidate the old token for `/webhooks/{path}` | `daemon/api.py`:802 |
| P1-57 | security | opt-in | Generated git post-receive hook embeds the daemon token in a world-readable (0755) file | `daemon/install.py`:200 |
| P1-6 / P1-74 | security | opt-in | browser_agent SSRF gate doesn't re-validate redirects / guard DNS rebinding | `browser_agent.py`:244 |
| P1-62 | stub-as-real | opt-in | VS Code activity-bar chat view contributed but no `WebviewViewProvider` registered | `vscode/package.json`:104 |
| P1-66 / P1-70 | cross-platform | opt-in | Release workflows run bash-only syntax on a Windows runner (PowerShell default) | `.github/workflows/vscode-extension.yml`:53 |
| P1-69 | correctness | opt-in | Action skills_repo install always fails — `((SKILL_COUNT++))` returns exit 1 under `set -e` | `action.yml`:190 |
| P1-75 | security | opt-in | Pre-tool hook "block" auto-approves every sibling tool call in the same interrupt batch | `textual_adapter.py`:1389 |
| P1-81 | doc-drift | opt-in | Expert-rules doc YAML doesn't load — `assert:`/`route:` raise RuleLoadError, `not:` silently ignored | `docs/advanced/expert-rules.md`:78 |

### Downgraded to P2 after verification (still real)

P1-10 (LifecycleHooks fires 2 of 15 events), P1-11 (Enterprise role perms never enforced), P1-14 (ModelCascade non-functional — unreachable), P1-20 (`redact_token_args` starter rule leaks the secret it claims to redact), P1-21 (`once: true` re-fires across tool calls), P1-23 (`/mcp install` "picked up automatically" false), P1-24 (`_set_spinner("")` never hides the spinner — ~25 sites leave a phantom widget), P1-39 (server_graph MCP-failure surfacing fictional), P1-65/67/68 (VS Code streaming flag / missing ESLint config / SVG icon blocks `vsce package`), P1-76/77 (vacuous Windows shell-injection tests / `/compact` tests mock private API), P1-79/80/82/83/84/85 (doc drift: "secrets never touch disk" false; non-existent hook API taught; non-existent pip extras; ImportError sandbox example; wrong default-backend claim; wrong daemon REST docs).

### False positive (1)
- **P1-30** — "Operator/JTBD seam awaits judge before `_agent_running` is set → concurrent prompts." The seam is only reached from `_handle_user_message`, which the Textual input already serializes; not reachable. No action.

### Confirmed but unreachable (cleanup/delete)
- **P1-50** OAuth-for-MCP dead code, **P1-14** ModelCascade non-functional, **P1-80** docs teach a non-existent hook API.

---

## 4. Architecture observations

**4.1 "Shipped" now overstates "works."** The most important truth this cycle is the gap between *contributed/advertised* and *wired/functional*. `/think`, `/checkpoint load`, `/worktrees cancel|merge`, OAuth-MCP, the VS Code chat view, `scheduled_reports.run_report_now`, `EnterpriseMiddleware` policy enforcement, and the dreamscape `laws`/`imagination` outcome loops all present as features and do nothing (or crash). This is more corrosive than the old stub cluster because these are *flagship-sounding* surfaces. Recommendation: a `/doctor --features` self-test that actually exercises each advertised command/middleware and reports red/green, plus a per-feature maturity badge in docs.

**4.2 The library needs the model-call test gate, not more middleware.** ~90 middleware modules; the marginal risk is no longer sprawl but that any one can silently break on a dependency bump because nothing drives a turn through it. Invest in the harness, not the count.

**4.3 Expert Mode shipped but its substrate is half-wired.** The engine, YAML loader, `/why`/`/prove`, and a dreamscape rule-proposer all exist (a real achievement). But the only asserted fact is `tool_call` (P1-19), the shipped `redact_token_args` starter rule leaks the secret it claims to redact (P1-20), `once: true` re-fires across tool calls (P1-21), and the documented YAML uses actions the loader rejects (P1-81). The feature is ~70% there; the last 30% is what makes the demo true.

**4.4 Security debt clusters in three places:** (a) command construction — `grep_raw` glob injection (P1-2), notification AppleScript/PowerShell injection (P1-16), butcher `shell=True` no-HITL (P1-42), ACP allowlist bypass (P1-58); (b) secret-at-rest — setup `.env`, daemon job secrets, git post-receive token, replay vars (P1-38,54,57,28,22); (c) trust gates — project hooks (P0-8) and remote MCP (P1-49) auto-exec from untrusted repos. The first two are mechanical; the third should block a 1.0.

**4.5 The satellites have independent, shippable bugs.** Daemon (cron tick abort; lost-update; dropped dispatch_errors), Harbor (`aedit` perl corruption; `aglob_info` never matches), ACP (one shared agent across sessions), VS Code (strips provider keys; unkilled processes; SVG icon blocks packaging) each have multiple confirmed P1s. Less load-bearing than SDK/CLI, but each is someone's first impression.

---

## 5. Recommended sequencing

### Wave 0 — Correctness restoration (the regression sweep)
Make "enabled" mean "works." Bug classes A + B + C (§2); the three default P0s (AGENTS.md overwrite, `--default-model` crash, project-hook trust gate); the dead-feature cluster (`/think`, `/checkpoint load`, `/worktrees`). **Definition of done:** a new CI test constructs every middleware and drives one fake-model turn; `/doctor --features` goes green. Mostly small, high-confidence diffs — this is where fixing starts.

### Wave 1 — Security & secrets hardening
Project-hook + remote-MCP trust gates (P0-8, P1-49); secret-at-rest (P1-38,54,57,28,22); command injection (P1-2,16,42,58); SSRF redirect/rebinding (P1-6/74); pre-tool block bypass (P1-75). Closes the prior cycle's partial P0-C.

### Wave 2 — Satellite repair & docs truth
Daemon (P1-52,53,55,56,51), Harbor (P1-59,60), ACP (P1-58,61), VS Code (P0-9, P1-62,63,64,67,68), CI (P1-66,69,70,71,72). Then the doc-drift block (P0-10 + P1-79..86) — every one is a wrong command a newcomer will run.

### Wave 3 — Finish what shipped
Close the Expert Mode substrate (P1-19,20,21,81), the dreamscape outcome loops (P1-43,45,46), and `cost_tracker`/budget enforcement — converting "impressive demo" into "true claim."

---

## 6. Competitive landscape & killer-feature roadmap v2

Research date June 12, 2026, across 11 coding CLIs and 4 framework clusters. The field moved hard since May: **the flat-rate era ended and metered pricing took over** (Cursor, Windsurf, Amp, Copilot all triggered pricing-revolt churn); **local/open models crossed the agentic-coding threshold** (GLM-5 at 77.8% SWE-bench Verified under MIT; Devstral Small 2 ~68% on a single 4090); **Roo Code shut down** and **Gemini CLI is migrating to a closed-source successor** (trust shocks); **fanout (worktree-per-agent + PRs) became table stakes** — the unsolved half everyone names is *review/merge/synthesis*; and **regulation goes operational** (EU GPAI enforcement Aug 2, 2026).

### The five strategic bets

1. **Be the best harness for open/local models, full stop.** Operator routing + butcher decomposition already point here — but recalibrate: local models are now *mid-tier*, not "weak." Make `local`/`hybrid` operator presets first-class, publish escalation-aware benchmarks from Harbor, lean into air-gapped as the regulated story no competitor has.
2. **Turn Expert Mode into the compliance/evidence product.** EU GPAI + SOC 2 AI-evidence demand is real and unmet in OSS. `/why` + `/prove` provenance + `audit_trail` → an exportable evidence pack ("policy-gated agents with proof"). The most defensible thing in the codebase once §5 Wave 3 lands.
3. **Own the review/merge/synthesis layer for parallel agents.** Fanout is commoditized; the back half isn't. A **trajectory/result critic** (OpenHands: +17.7 early-stop, 73.8% Best@8) on `/race` + operator escalation, a **dependency-DAG task board** (Cline Kanban) for `/orchestrate`, and **summary-only result contracts** at the subagent boundary (Roo) to prevent context poisoning.
4. **Make cost a first-class, standards-based observability surface with published savings.** Finish `cost_tracker` + `/budget` caps (T-9, open), then emit **OpenTelemetry `gen_ai.*` spans** natively (the ~90-middleware design makes every concern a span). Routing that *proves* it halved cost beats routing that claims to.
5. **Sandbox-by-default with a brutally honest threat model.** The "accident-catcher not boundary" stance is now publicly vindicated by 2026's CVE drumbeat. Make `SafeToolsMiddleware` + a sandbox tier the CLI default; map graduated autonomy (allowlist → sandbox → prompt, Cursor 3.6) onto the expert-rules engine.

### Concrete features worth stealing (new since the prior roadmap)

| # | Feature | Source | Effort | Why |
|---|---------|--------|--------|-----|
| **N-1** | **Programmatic / code-as-action tool calling** (model emits one sandboxed script calling N tools) | deepagents QuickJS, MS MAF CodeAct (−63.9% tokens), smolagents | M–L | Collapses tool round-trips; huge for butcher-decomposed local models; fits `tools/bundles.py` + daytona |
| **N-2** | **Trajectory/result critic** for `/race` winner-pick + operator escalation | OpenHands (published Best@k) | M | Owns the "synthesis" half of bet 3; supervises routing with evidence |
| **N-3** | **Modes-as-data** (`.bog-agents/modes/*.yaml`: role + tool groups + fileRegex) filtering tools before the model sees them | Roo Code | S–M | Composes with expert-rules deny gates; saves prompt tokens |
| **N-4** | **Auto-memory inbox**: background extractor drafts SKILL.md patches to a review inbox, atomic apply on approval | Gemini CLI, Cursor | M | Turns dreamscape effectiveness signal into reviewable skill artifacts; `rule_proposer` is the seed |
| **N-5** | **Recipes**: parameterized YAML (prompt+tools+config), `bog recipe run x --param k=v`, `bog://` deep links | Goose | S–M | Stickiest team-sharing artifact |
| **N-6** | **Lazy MCP tool-schema loading** (inject on first use, not at startup) | Amp, this harness | S | Cuts hundreds of tokens/turn; complements the fixed MCP startup timeout |
| **N-7** | **Turn-level `/rewind`** over existing checkpoints (restore files+messages to turn N, optional summarize-tail) | Claude Code | M | Checkpointing middleware exists; pairs with street-sweeper |
| **N-8** | **Verification artifacts on PRs** (drive the app, attach video/screenshots/logs) | Cursor | M | Differentiator for the daemon's GitHub-comment output target |
| **N-9** | **Graduated autonomy run mode** (allowlist→sandbox→prompt) on expert-rules | Cursor 3.6 | S | Bet 5; the rules engine already has the action vocabulary |
| **N-10** | **`whenToUse` delegation routing** for `/orchestrate` subagents | Roo Code | S | Cheap metadata so orchestration picks the right specialist |

### Already-shipped, under-marketed (surface once green)
Dreamscape long-term memory; the working parts of Expert Mode (`/why`/`/prove`); operator/butcher/JTBD; `/sidecar`; `/orchestrate`; `/race`/`/squad`/`/jury`/`/devil`; air-gapped + offline; MCP trust+OAuth (once wired); four-surface reach (CLI + daemon + ACP + VS Code). Caveat from §4.1: market only what passes `/doctor --features`.

---

## 7. Closing

The prior cycle's verdict was "great core, stub perimeter." This cycle's is sharper: **the core grew faster than the harness that proves it works.** The team shipped real, hard features — an expert-rules engine, a memory cascade, orchestration, sidecar — and fixed almost every prior ship-blocker. But a dependency migration quietly broke a chunk of the middleware library and several headline commands, and the test suite's shape let it all ship green. Fix the two mechanical bug classes, add the one model-call CI gate that prevents their return, close the three default-reachable P0s, and finish the 30% of Expert Mode that makes its story true — and bog-agents is not two-to-three sprints from a credible 1.0, it's one focused correctness wave away. The competitive moat (local-model harness + compliance-grade provenance + review/synthesis layer) is real and largely un-contested; it just has to stand on features that don't throw on first use.

— Senior Principal Engineer's report, June 12, 2026
