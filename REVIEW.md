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
