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
