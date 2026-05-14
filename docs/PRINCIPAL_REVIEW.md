# Bog Agents — Principal Engineer Review (May 2026)

> **Scope:** SDK (`libs/bog-agents/`), CLI (`libs/cli/`), Daemon (`libs/daemon/`).
> **Lens:** Resiliency, stability, observability — plus competitive positioning and a long-arc feature roadmap.
> **Method:** 5 parallel codebase audits + 2 parallel competitor research waves, all run May 2026.
> **Verdict in one line:** Genuinely differentiated stack. **Not yet 1.0-safe** — 3 critical bugs and 1 god-module need to land before tagging. **Massive opportunity** in the diff-sandbox / replayable-eventlog / TUI-native-skill space that no competitor has cracked.

---

## 0. Executive Summary

Bog Agents sits at an unusual intersection: a Textual TUI + ambient daemon **on top of** a real middleware library (80+ middlewares, LangGraph). Library-only competitors (deepagents) win on composability but ship no UX; CLI-only competitors (Claude Code, Codex, Aider, Cursor) win on UX but lock extension to MCP-only or a YAML recipe DSL. **Bog Agents has both halves of the stack and almost nobody else does.**

What I found:

- **3 critical bugs** that block 1.0 (shell injection in QA executor, daemon webhook auth bypass, hardcoded 200K context window that already misses Opus 4.7 1M).
- **A god-class problem** (`app.py` is 14,939 lines, 92 `_handle_*` methods) that won't crash production but will strangle contributor velocity.
- **A genuinely best-in-class resilience story** — the two-layer Anthropic event-loop patch + the stall watchdog with `asyncio.Task` introspection are things competitors do not have and most don't even know they need.
- **Five competitive features worth building** that compound the existing stack rather than fighting it: diff-sandbox apply, replayable event log + time-travel debug, plan branches, TUI-native skill marketplace, and `bog-agents-action` for PRs.

The rest of this document is the detail.

---

## 1. Production Readiness Verdict

### 1.0-Ready? **Not yet — 2 to 4 weeks of focused work.**

**Must fix before tagging 1.0** (in priority order):

| # | Risk | Severity | Effort | File:Line |
|---|------|----------|--------|-----------|
| 1 | QA executor `subprocess.Popen(..., shell=True)` with user-controlled command — shell injection via VarBundle values or plan steps | **CRITICAL** | S (1 day) | `libs/cli/bog_agents_cli/qa/executor.py:149-151` |
| 2 | Daemon webhook fires when `webhook_secret` is empty AND caller lacks token — comment claims "empty does not mean public" but code does exactly that | **CRITICAL** | S (1 day) | `libs/daemon/.../api.py:788-795` |
| 3 | `MODEL_CONTEXT_WINDOWS` defaults to `200_000` and has no entry for `claude-opus-4-7` — silently truncates 1M-capable models to 200K | **CRITICAL** | S (1 day) | `libs/bog-agents/bog_agents/middleware/adaptive_context.py:126-159`, `cost_tracker.py:153` |
| 4 | `ProviderRetryMiddleware` and `ToolCallParserMiddleware` exported from `bog_agents.middleware.__init__` but missing from `bog_agents.__init__._LAZY_IMPORTS` — `from bog_agents import ProviderRetryMiddleware` raises `AttributeError` | **HIGH** | XS (1 hour) | `libs/bog-agents/bog_agents/__init__.py:13-109` |
| 5 | No timeout wrapping `resolve_and_load_mcp_tools()` — a slow-starting stdio MCP server hangs the entire `langgraph dev` startup | **HIGH** | S (1 day) | `libs/cli/bog_agents_cli/server_graph.py:456` |
| 6 | Path traversal in `resolve_physical_path` — no `cwd.resolve()` jail, follows symlinks | **HIGH** | S (1 day) | `libs/cli/bog_agents_cli/file_ops.py:131-151` |
| 7 | SSE retry only fires for transient errors; permanent-error paths (400, malformed tool JSON) dump partial state and exit silently | **HIGH** | M (3-5 days) | `libs/cli/bog_agents_cli/remote_client.py:42-50` |
| 8 | Daemon has no exponential backoff on transient trigger/notification failures; retry storms possible | **HIGH** | M (3-5 days) | `libs/daemon/.../scheduler.py:20-93`, `runner.py:483-653` |
| 9 | Daemon has no exclusive lockfile; two daemon instances can fire the same job concurrently | **HIGH** | S (1-2 days) | `libs/daemon/.../main.py:82, 187-194` |
| 10 | Bare model timeout is per-chunk only; extended-thinking call that emits a token every 5 min never times out | **MEDIUM** | S (1-2 days) | `libs/cli/bog_agents_cli/timeouts.py:67-73` |

**Total estimated effort to clear the must-fix list:** ~10–15 engineering days for one developer, or one focused week with two engineers in parallel.

**Ship-ready after:**

- The 10 items above are merged.
- A `mypy --strict` / `ty check` gate exists in CI for the SDK's public modules.
- The CHANGELOG has a `## BREAKING CHANGES` section per release.
- A documented API stability policy lives in `docs/API_STABILITY.md` (deprecation cycle: warn in X, remove in X+2).

---

## 2. Critical & High-Severity Findings (the bug list)

### 2.1 Security (5 of 10 highest-severity)

**[CRITICAL] Shell injection in QA executor.** `libs/cli/bog_agents_cli/qa/executor.py:149-151` does `subprocess.Popen(..., shell=True, cwd=cwd or None, env=env)` with `rendered_run` — a string built from user-supplied VarBundle values after substitution. Quoting/escaping is absent. A QA plan YAML that sets `vars: {foo: "; rm -rf $HOME"}` and references `${foo}` in a step pwns the host. **Fix:** drop `shell=True`, use argv-array form; if shell is genuinely needed, wrap interpolated values in `shlex.quote()`. **No tests cover this path** (`tests/unit_tests/test_qa_executor.py` only exercises happy-path substitution).

**[HIGH] MCP trust = fingerprint-of-file-content.** `libs/cli/bog_agents_cli/mcp_trust.py:109-159` records trust based on a hash of the config file. A maintainer who edits `.bog-agents/mcp.json` to add a malicious stdio server gets auto-approved on next launch once any user approves *any* version of the file. There's no per-tool capability scoping, no sandboxed stdio subprocess. **Fix:** capability tokens declared per MCP server (`reads_files`, `writes_files`, `network`, `executes`) with enforcement at the tool-call boundary; signed registry for the marketplace (post-1.0).

**[HIGH] Path traversal in file write.** `libs/cli/bog_agents_cli/file_ops.py:131-151` resolves absolute paths directly and relative paths against `Path.cwd()`. No `(target.resolve().relative_to(cwd.resolve()))` check; symlinks are followed without warning. A prompt that asks the agent to "write to `../../../etc/passwd`" succeeds outside the project. **Fix:** explicit jail check, symlink rejection on write paths.

**[MEDIUM] `.env` gitignore status not verified at startup.** `libs/cli/bog_agents_cli/api_keys.py:52-82` reads `.env` without checking whether the file is tracked by git. **Fix:** at first read of `.env`, shell out to `git check-ignore .env` (or read `.gitignore`) and warn loudly if `.env` is git-tracked.

**[MEDIUM] Auto-mode Haiku risk eval is JSON-injection-shaped.** `libs/cli/bog_agents_cli/auto_mode.py:417-532` formats tool args as JSON inside a Haiku prompt. A tool arg with a crafted JSON string can close the JSON block early and influence Haiku's verdict. **Fix:** escape interpolated values, or template the prompt with explicit delimiters that aren't user-reachable.

### 2.2 Resilience (top 5 of 10)

**[CRITICAL] Cross-loop binding risk in 13 `asyncio.run` callsites.** `libs/cli/bog_agents_cli/main.py` calls `asyncio.run` at lines 1434, 1520, 1781, 1793, 1824-1832, 1919, 1965-1967, 1981, 1994, 2026, 2062, 2077. Each creates and tears down a loop. **The recent two-layer Anthropic patch in `server_graph.py:253-406` is the model for what every other cached async resource needs**, but CLI-side callsites aren't all covered. **Fix:** audit each callsite; if a singleton is touched, ensure the singleton is constructed inside the loop, not at module import.

**[HIGH] 295 bare `except Exception:` handlers across 134 files.** Examples: `api_keys.py:27-28`, `app.py:1180, 1244, 2535`, `server_graph.py:499-516`. Many log at DEBUG level only — failures vanish unless the user opens the log file. **Fix:** triage. Roughly 40 of these are correctly defensive (best-effort cleanup, optional features); the rest should narrow to specific exception types and re-raise or surface to the user.

**[HIGH] No total-time timeout for model invocation.** `libs/cli/bog_agents_cli/timeouts.py:67-73` has `_DEFAULT_MODEL_READ_SECS=600` but that's **per-chunk** — an extended-thinking call that emits a keepalive every 5 min runs forever. **Fix:** wrap each model call with `asyncio.wait_for(..., timeout=total_budget)` separate from the per-chunk read deadline. Make `total_budget` configurable per profile.

**[HIGH] No circuit-breaker on auth-class provider errors.** `libs/cli/bog_agents_cli/remote_client.py:42-50` retries transient errors 3 times pre-first-event and 2 times mid-stream. There's no halt-on-N-consecutive-401s. A user with a malformed API key sees 5 retry attempts with the same bad key, all buried in retry-log noise. **Fix:** 1 strike on 401/403 → fail fast with the existing smoketest hint card.

**[MEDIUM] Subprocess `langgraph dev` stderr is buffered.** `libs/cli/bog_agents_cli/server.py:14` imports subprocess; the spawn site doesn't set explicit line-buffering and stderr forwarding. A native-code hang in the subprocess leaves the parent staring at a log file 5 minutes stale. **Fix:** spawn with `stderr=subprocess.PIPE`, drain in a daemon thread with `bufsize=1, text=True`.

### 2.3 Daemon (top 3)

**[CRITICAL] Webhook auth bypass when secret is empty.** `libs/daemon/.../api.py:788-795` accepts unauthenticated requests when `webhook_secret == ""` even though the comment claims otherwise. **Fix:** if no token AND no signature, reject 401 — no exceptions.

**[HIGH] No exponential backoff on trigger / notification failure.** `scheduler.py:20-93` retries on the next 30s tick. `runner.py:483-653` doesn't retry at all on Slack/email/webhook 5xx. **Fix:** add `tenacity` or hand-roll exponential backoff with jitter (start 1s, cap 60s, max 5 attempts), and surface backoff state to `/jobs/{id}` so operators can see why jobs are quiet.

**[HIGH] No multi-instance coordination.** `main.py:82` overwrites the PID file unconditionally; `store.py:29` uses an in-memory threading lock. Two daemons on the same `agent_home` fire the same job twice. **Fix:** advisory lockfile (`fcntl.flock` on POSIX, `LockFileEx` on Windows), check at startup.

### 2.4 Architecture / Maintenance

**[HIGH] `app.py` is 14,939 lines, 92 `_handle_*` methods, 67+ instance variables.** This is the single biggest threat to contributor velocity. A new engineer cannot read it in a sitting. Recommended split (see §6.1).

**[HIGH] `summarization.py` is 1,505 lines.** Single class mixes token counting, message truncation, and LLM summarization. **Fix:** split into `_counter.py`, `_truncator.py`, `_summarizer.py` per the SDK audit.

**[HIGH] `filesystem.py:1057-1151` has 40+ lines of sync/async duplication.** `wrap_model_call` and `awrap_model_call` are byte-equivalent except for `await`. **Fix:** extract `_build_filtered_request(request, backend)` and call from both.

---

## 3. What Works Well (the credit-where-due section)

These are the parts of the stack that punch above their weight. **Don't lose them in any refactor.**

1. **Two-layer Anthropic event-loop fix** (`libs/cli/bog_agents_cli/server_graph.py:253-406`). Defeats `@lru_cache` on `_get_default_async_httpx_client` AND `@cached_property` on `ChatAnthropic._async_client`. Idempotent, checks for `__wrapped__`, logs gracefully on upstream changes. Defense-in-depth. This is the kind of fix most agents never make and most teams can't even diagnose.

2. **Stall watchdog with `asyncio.Task` introspection** (`server_graph.py:138-250`). Walks `gc.get_objects()`, follows `cr_await` chains, dumps faulthandler stacks **only when activity has actually stopped**. Filters known-noisy loggers so healthy agents don't trigger false alarms. This alone is a moat — competitors who hit a deadlock have no equivalent surface.

3. **Lazy-load middleware** (`bog_agents/__init__.py:13-134`). `_LAZY_IMPORTS` + `__getattr__` keeps `import bog_agents` blazingly fast. Cached in `globals()` after first access. This is genuinely production-ready and most Python AI stacks don't do it.

4. **AgentBuilder fluent API** (`bog_agents/builder.py:160-670`). `.with_git()`, `.with_memory()`, `.with_sandbox()` etc. accumulate into a typed `AgentConfig`. Type errors at call site. `extra_kwargs` escape hatch for forward compat. Vastly better DX than `create_agent(enable_git_tools=True, ...)`.

5. **Backend protocol** (`bog_agents/backends/protocol.py:168-200+`). Real `typing.Protocol` with clean methods (`ls_info`, `read`, `write`, `execute`) and a structured `FileOperationError` literal so models can reason about failures. Sync + async variants.

6. **Structured event logging without external infra** (`libs/cli/bog_agents_cli/_observability.py:1-233`). `log_event()` emits structured records to stdlib logging AND bumps an in-process counter. Zero-setup observability — `/peat metrics` works on first run with no Prometheus collector required. Cardinality cap (256) prevents unbounded growth.

7. **MCP failures are non-fatal to server startup** (`server_graph.py:499-516`). One bad MCP server doesn't crash the daemon — agent comes up without it, user fixes inline with `/mcp list` + `/mcp remove`. Inverts the typical "server crash → restart → grep logs" flow.

8. **Token-based daemon auth + HMAC webhooks** (`libs/daemon/.../api.py:142-165, 796-805`). Constant-time comparison (`hmac.compare_digest`), `0o600` mode, attempted Windows ACL hardening via `icacls`. The webhook signature verification is the right shape — the empty-secret bypass (§2.3) is the only flaw.

9. **Durable job persistence** (`libs/daemon/.../store.py:183-205`, `runner.py:288-307`). `os.fsync` + atomic rename. A hard kill mid-write won't corrupt `jobs.json`. Production-grade.

10. **Model picker overhaul** (`libs/cli/bog_agents_cli/widgets/model_selector.py`, `smoketest.py`). Display names ("Claude Sonnet 4.6"), inline Ctrl+T smoketest, Bedrock inference profile awareness, build-once + filter-in-place architecture. Recently shipped (PR #76).

11. **Bedrock error categorization** (`libs/cli/bog_agents_cli/_bedrock.py`). Categorical errors (`CREDENTIALS_MISSING`, `MODEL_ACCESS_DENIED`, `REGION_INVALID`) with copy-paste fixes. Shared between `/bedrock test` and smoketest. Same pattern should expand to OpenAI/Anthropic.

12. **Replay/record system** (`libs/cli/bog_agents_cli/replay.py`). Captures turn-by-turn agent decisions as YAML with auto-variabilization. Closest equivalent in the industry is OpenHands' EventLog, but bog-agents emits human-editable YAML rather than NDJSON — better for regression testing.

---

## 4. Competitive Landscape (May 2026)

### 4.1 The market is bifurcated

- **Library plays** (deepagents): composability ✓, UX ✗
- **CLI plays** (Claude Code, Codex, Aider, Cursor, Continue, OpenHands, Plandex, Goose): UX ✓, library-grade composability ✗

Bog Agents is the rare both-halves play. Lean into that.

### 4.2 Gap matrix

| Feature | Claude Code | Codex CLI | Cursor 3 | Aider | Continue | OpenHands | Plandex | deepagents | Goose | **Bog Agents** |
|---|---|---|---|---|---|---|---|---|---|---|
| MCP host | ✓ | ✓ | ✓ | – | ✓ | ✓ | – | partial | ✓ | ✓ |
| Subagent primitive | ✓ | ✓ | ✓ (fleet) | architect/editor | – | ✓ | branches | ✓ | ✓ | ✓ |
| Replayable event log | – | – | – | – | – | **✓** | rewind | LangSmith | – | partial (replay.py) |
| Diff-sandbox before apply | – | – | – | – | – | – | **✓** | – | – | – |
| Cron / scheduler | – | – | – | – | – | – | – | – | **✓** | **✓ (daemon)** |
| Hub/marketplace | ✓ (4,200+) | ✓ + bundles | – | – | ✓ | – | – | – | ✓ | partial (MCP marketplace) |
| Cloud-hosted async | – | – | ✓ | – | ✓ | ✓ | sunset | v0.5 | – | – |
| Per-subagent capability tokens | – | – | – | – | – | partial | – | **✓** | partial | partial (middleware) |
| Custom UI rendered by extension | – | – | – | – | – | – | – | – | **✓ (Desktop)** | – |
| Source-controlled CI checks | – | – | – | – | **✓** | ✓ | – | – | – | – |
| Native TUI (terminal-first polish) | ✓ | ✓ | – | basic | – | – | basic | – | basic | **✓ (Textual)** |
| 1M-context model support today | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | **broken** (§2 #3) |
| Ambient daemon + recipes | – | – | – | – | – | – | – | – | partial | **✓** |
| Per-loop event-loop hygiene | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | **✓ unique** |
| Stall watchdog with task introspection | – | – | – | – | – | – | – | – | – | **✓ unique** |

**Three categories where bog-agents already leads or could lead:**
- Resilience instrumentation (uniquely strong).
- Both-halves architecture (CLI + library).
- MCP marketplace + ambient daemon combo (nobody else has both).

**Three categories where bog-agents is behind and must close:**
- Diff-sandbox apply (Plandex).
- Replayable event log with time-travel (OpenHands).
- Plugin lockfile + signed marketplace (Claude Code shipping it now retroactively).

### 4.3 What each rival is bad at (so we don't repeat their mistakes)

- **Claude Code:** weekly usage caps, edit-happy model, opaque token accounting.
- **Codex:** slow (10-minute responses for simple queries), usage-tracking UI buggy.
- **Cursor 3:** pricing communication, MDC rules ignored 95% of the time per user reports.
- **Aider:** too aggressive, hard-coupled to git, can't see new files when `--no-git`.
- **Continue:** 5–10s cold start, autocomplete slows VS Code, 15-30 min first-time config.
- **OpenHands:** heavyweight Docker setup, v0 monorepo brittle, headless mode "continue forever" bug.
- **Plandex:** cloud wound down Oct 2025; no MCP.
- **Goose:** got phished in their own red-team (Operation Pale Fire) — recipes are the attack surface.

The pattern: **everyone's pricing/usage story is a mess** and **everyone has a security incident waiting to happen with skills/recipes/plugins.** Both are wedges we can use.

---

## 5. The Killer-Feature Roadmap (Long Arc)

Organized in three waves. Each feature names the **moat** (why competitors can't trivially copy it).

### 5.1 Wave 1 — Land in next 8 weeks (1.0 → 1.1)

These are the "ship within a quarter" features that compound with what already exists. Each is sized as S (≤1 week, 1 eng), M (2–4 weeks), L (1–3 months).

**A. Diff-Sandbox Apply Mode** [M] — Steal from Plandex; nobody else has it.
- Every agent write goes through a staging tree (overlay FS or `git worktree` under `~/.bog-agents/staging/<run-id>/`).
- `bog-agents review` TUI shows cumulative diff with per-hunk accept/reject and `--rewind`.
- Wires into existing auto_mode approval flow: thinking-middleware output renders alongside the diff as "why this change."
- **Moat:** bog-agents' middleware can attach the agent's reasoning to each hunk; Plandex has no equivalent context.

**B. Replayable EventLog + Time-Travel Debug** [M] — Steal from OpenHands.
- Every Action/Observation hits an append-only NDJSON log keyed by run-id (extend the existing `replay.py`).
- `bog-agents replay <run-id> --step --middleware-swap "+CheckpointingMiddleware"` reconstructs state at any point and re-runs with a *different middleware stack*.
- **Moat:** unique to bog-agents — only stack that has both an event log AND swappable middleware. "What if I'd had checkpointing on?" is something only this architecture can answer.

**C. Total-time model timeout + circuit breaker** [S] — Fixes §2.2 #3 and #4 in one go.
- `asyncio.wait_for` wrapping each model call with a configurable total budget.
- `CircuitBreaker(threshold=1, error_classes={AuthError})` for auth-class failures.
- Surface the smoketest hint card on circuit-open so the user knows what to fix.

**D. Signed MCP/skill registry with capability tokens** [L] — Leapfrog Claude Code's bolt-on `plugin prune` work.
- Per-skill manifest declares capabilities: `reads_files`, `writes_files`, `network`, `executes_shell`.
- Manifests are signed; `bog-agents skill add` checks signature.
- `bog.lock` file pins versions + content hashes (npm/uv style).
- Enforcement at tool-call boundary, not at the trust-prompt stage.
- **Moat:** Goose just got burned on Operation Pale Fire; Claude Code's plugin prune is reactive. We can ship security-first from day one.

**E. Plug the 10 must-fix bugs from §1** [M total]

### 5.2 Wave 2 — Land in next 6 months (1.1 → 1.3)

**F. Plan Branches with Token-Accounted Diffs** [M] — Steal from Plandex + extend.
- `bog-agents branch <name>` forks a session's plan + context.
- TUI shows side-by-side branches with cost, time, diff preview.
- Each branch can spawn its own subagent tree.
- Pairs with (A) diff-sandbox: a branch is just a different staging tree.

**G. TUI-Native Skill Marketplace with Rendered UIs** [L] — Steal from Goose Desktop, do it terminal-first.
- Skills declare an optional Textual widget spec (button row, form, chart).
- `bog-agents skill add <id>` installs into `~/.bog-agents/skills/`; on use, the widget renders inline in the chat.
- Recipes (skill bundles) installable from a signed registry.
- **Moat:** Goose Desktop renders UIs in a GUI. Bog-agents in the terminal is novel — and the existing Textual widget system makes it cheap.

**H. `bog-agents-action` GitHub Action** [M] — Steal from Continue + OpenHands.
- Headless mode runs the same agent in CI on every PR.
- Team-defined middleware/rules from `.bog-agents/team.toml`.
- Posts review comments; optional fixup commits.
- Bedrock inference profile support → enterprise compliance for free.
- **Moat:** the SDK's middleware stack means teams customize CI agents in ways Continue's MCP-only world can't.

**I. Two-layer Sandbox + Reviewer** [M] — Steal from Codex.
- Declarative sandbox profile (`workspace-write`, `network-deny`, etc.) separate from approval policy.
- Hook-based reviewer pipeline; each hook returns approve / deny / escalate.
- Built-in `auto_review` Haiku evaluator (we already have it from the auto_mode work).
- **Moat:** Codex has the model but lacks bog-agents' middleware composability; we get Codex's sandbox depth + Claude Code's hook flexibility.

**J. Adaptive context window auto-detection** [S] — Fixes the §1 #3 critical bug structurally.
- `AdaptiveContextMiddleware` introspects model profile at construction time; pulls `max_input_tokens` from langchain's `_PROFILES` or our curated catalog.
- Falls back to 200K only if both fail.
- Zero code change required when 2M / 10M models ship.

**K. Real-time Middleware Profiler** [S] — Unique to our architecture.
- Instrument each middleware's `wrap_model_call` with start/end timestamps.
- Render a flame-graph-style waterfall in the TUI: "FilesystemMiddleware 120ms → SubAgentMiddleware 80ms → SummarizationMiddleware 200ms ← bottleneck."
- **Moat:** competitors have no middleware seam; latency hides for them. We can show users where their tokens go.

### 5.3 Wave 3 — Long arc (6–18 months, the 1.x → 2.0 horizon)

**L. Skill Evolution / Self-Improving Skills** [L] — No competitor has anything close.
- After each turn, `SelfImprovingMiddleware` notes which skills succeeded; appends learned variants to `<skill>.improved.md`.
- Next turn loads baseline + improved variants.
- User reviews and accepts/rejects evolutionary additions weekly.
- **Moat:** requires both the skill system AND the middleware stack with state reducers. Library-only competitors can't bolt on the UX; CLI-only competitors lack the architectural seams.

**M. Async Subagents on a "Bog Cloud" runner** [L] — Steal from deepagents v0.5; differentiate via daemon integration.
- `task()` returns a task ID immediately, runs on a remote LangGraph runner.
- Parent keeps working; daemon pings on completion via notification adapter.
- Optional self-hosted runner so we never compete with cloud-first vendors on margin.

**N. Provable Plan-Mode Audit Trail** [M] — Unique enterprise feature.
- Every plan step hashes (tool_name, args, timestamp, approval).
- Hashes chain like git; tamper-evident.
- Signed audit log to `~/.bog-agents/audit/<run-id>.log`.
- Compliance-ready: "this agent never ran shell commands; here's cryptographic proof."

**O. PageRank Repo Map for Context Packing** [M] — Steal from Aider's core advantage.
- Tree-sitter symbol graph built on first run, cached.
- PageRank ranking surfaces top-K files by call/import edges.
- Wires into our existing context-packing middleware.
- Aider has no MCP, no daemon, no middleware — we can match their context quality and beat their everything-else.

**P. Cost-Aware Context Shrinking** [M] — Couples our existing AdaptiveContext + CostTracker.
- Budget knob: `agent.with_cost_aware_context(budget_usd=5.0, quality_floor=0.9)`.
- Progressive: shrink context window 10%, measure downstream quality, revert if it drops.
- **Moat:** competitors hardcode context; we have the state across turns to tune live.

---

## 6. Refactors That Pay Down Debt

### 6.1 The `app.py` god-class

14,939 lines is not sustainable. Recommended extraction (incremental — can land one per release):

| Service | Lines absorbed | Effort | When |
|---|---|---|---|
| `ModelService` (spec, caching, provider fallback) | ~600 (handlers + state) | S (3-4 days) | 1.1 |
| `AgentExecutor` (pump, interrupt, state tracking) | ~800 | S (1 week) | 1.1 |
| `ApprovalService` (auto-mode, ask-user, shell allow-list) | ~500 | S (3-4 days) | 1.2 |
| `SessionManager` (threads, history, resume) | ~1,500 | M (2-3 weeks) | 1.2 |
| `SettingsManager` (config cascade, hot-reload) | ~700 | M (1-2 weeks) | 1.3 |

End state: `app.py` <3,000 lines, pure Textual composition + event routing. Test surface area improves 10×.

### 6.2 The settings sprawl

Four ways to set a model temperature: `config.toml`, env var, `--model-params`, `--profile-override`. The precedence rules live in `agent.py:112-115` but are not documented anywhere a user can find them. **Fix:** a single `docs/SETTINGS.md` with the precedence matrix, plus a `/settings show <key>` command that traces *which* layer set the current value.

### 6.3 Middleware ordering contract

`graph.py:100` validates middleware but silently. **Fix:** declare ordering constraints as data:

```python
class FilesystemMiddleware(AgentMiddleware):
    ORDERING_BEFORE: ClassVar[set[type]] = {SubAgentMiddleware}
```

Validate at graph build time; raise with a readable message naming the conflict. Third-party middleware authors get a real contract.

---

## 7. Recommended Sequencing

**Now (next 2 weeks) — the 1.0 gate:**
1. Land §1 critical bugs (1, 2, 3).
2. Fix the lazy-import inconsistency.
3. Add `shlex.quote` / argv-array form in QA executor + a fuzz test.
4. Wrap MCP spawn in `asyncio.wait_for`.
5. Add the path-traversal jail.

**Next 8 weeks — 1.0 → 1.1:**
6. Diff-sandbox apply mode (A).
7. EventLog + replay swap (B).
8. Total-time timeout + circuit breaker (C).
9. Extract `ModelService` and `AgentExecutor` from `app.py`.
10. Document API stability policy + add `ty check` strict gate.

**6-month horizon — 1.1 → 1.3:**
11. Plan branches (F).
12. TUI-native skill marketplace (G).
13. `bog-agents-action` (H).
14. Two-layer sandbox + reviewer (I).
15. Adaptive context auto-detect (J) + middleware profiler (K).

**12–18 month horizon — toward 2.0:**
16. Skill evolution (L).
17. Bog Cloud async runner (M).
18. Provable audit trail (N).
19. PageRank repo map (O).
20. Cost-aware context shrinking (P).

---

## 8. The Three Things I'd Tweet About If I Were a Developer

(This is the marketing test — what would make a senior engineer screenshot and share?)

1. **"Type `Ctrl+T` on any model in the picker and it does a live smoketest with a 30-second timeout, costs ~$0.0001, and tells you exactly which env var is wrong if it fails."** — Already shipped (PR #76); needs promotion. Nobody else has this.

2. **"The deadlock dump tells you the exact `await foo()` line that's stuck, walks the `cr_await` chain through anyio primitives, and only fires when activity has actually stopped."** — Already shipped; nobody knows it exists. Needs a `docs/DEBUGGING.md` page.

3. **"`bog-agents review` shows a cumulative diff of everything the agent did, with the agent's thinking-trace attached to each hunk, and you can `--rewind` to any point."** — To build (Wave 1, item A). The diff-sandbox killer feature.

---

## 9. Closing Note

Bog Agents has more architectural moat than it knows about. The middleware seam alone enables features no competitor can match (live profiler, swappable middleware in replay, cost-aware context tuning, skill evolution). The blockers are not architectural — they're a small number of concrete bugs, a god-class, and a marketing problem (the best features are hidden).

Fix the 10 bugs, extract two services from `app.py`, ship the diff-sandbox + replayable event log, and the 1.x line has a credible path to the strongest extensible-coding-agent on the market.

Old Friend, this is the kind of stack people remember.
