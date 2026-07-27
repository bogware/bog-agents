# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Bog Agents is a Python monorepo providing an opinionated, production-ready AI agent framework built on LangGraph. It includes a core SDK (`create_agent`), an interactive CLI (Textual-based TUI), and supporting packages for evaluation, editor integrations, and sandbox providers.

## Monorepo Structure

```
libs/
├── bog-agents/      # Core SDK — agent creation, middleware, backends
├── cli/             # Terminal UI (bog-agents-cli) — Textual framework
├── daemon/          # Ambient runner (bog-agents-daemon) — cron / file-change / webhook triggers
├── acp/             # Agent Client Protocol (Zed editor)
├── harbor/          # Evaluation / benchmark framework (Terminal Bench 2.0)
├── vscode-extension/# VS Code extension (TypeScript)
└── partners/        # Sandbox integrations (currently: daytona only)
```

Each Python package has its own `pyproject.toml`, `uv.lock`, and `Makefile`.

> **Note (P0-F):** Earlier revisions of this file claimed `libs/partners/`
> shipped four sandboxes (daytona, modal, runloop, quickjs). Today only
> `daytona` is present as source; the rest were never landed. The
> `libs/daemon/` package was missing entirely from this section despite
> being the most-active satellite. Both are now reflected accurately.

## Common Commands

All packages use `uv` for dependency management. Run package-level commands from within the package directory.

```bash
# Install dependencies (from a package dir, e.g. libs/bog-agents/)
uv sync

# Run unit tests (no network, parallel)
make test                    # in any package dir

# Run a single test file
uv run --group test pytest tests/unit_tests/test_specific.py

# Run integration tests (network allowed)
make integration_test

# Lint (ruff + ty type checker)
make lint

# Format
make format

# From repo root: lint/format all packages, refresh/verify lockfiles
make lint
make format
make lock
make lock-check
```

SDK type checking: `uv run --all-groups ty check bog_agents` (from `libs/bog-agents/`)

## Architecture

### SDK (`libs/bog-agents/`)

The entry point is `create_agent()` which returns a compiled LangGraph graph. The agent ships with base tools (filesystem, shell, planning, sub-agents) and a composable **middleware stack**.

**Middleware** (`bog_agents/middleware/`) is the primary extension mechanism. ~90 middleware implementations handle concerns like git tools, repo mapping, cost tracking, checkpointing, plan mode, auto-quality checks, context packing, summarization, street-sweeper context pruning, memory, skills, persistent goals (`goal_tools.py`, surfaced as `/goal` + `/rubric` in the CLI), and the **Expert Mode** rule engine (`expert_rules.py` + `expert_engine/`). All middleware inherits from `AgentMiddleware`.

**Tool bundles vs. middleware** (W4, Wave W): a *bundle* is a free function in `bog_agents/tools/bundles.py` that returns `list[BaseTool]` — the right shape for "middleware whose only job is delivering tools". `git_tools_bundle`, `multi_edit_tool`, and `read_many_files_tool` are the canonical examples. New tool-only features should ship as bundles, not middleware. The corresponding middleware classes (`GitToolsMiddleware`, etc.) are kept as thin compatibility shims that delegate to the bundles.

**Expert Mode (`ExpertRulesMiddleware`)** — a small forward+backward-chaining rule engine that loads YAML policies from `.bog-agents/expert_rules/*.yaml`, asserts a `tool_call` fact before every tool call, and can deny / modify / require-approval the call. CLI surface: `/expert`, `/why`, `/prove`. The engine is opt-in (default `enabled=False`) and composes with `RulesMiddleware` (the prose rule injector — different feature, same family of names).

**Governed-autonomy primitives (0.10):** three pure-logic SDK modules underpin the CLI's autonomous surfaces — keep them dependency-light and injectable. `bog_agents/teams.py` (`TaskLedger` atomic dependency-aware claim, `Mailbox`, `run_team` coordinator; CLI `/team run` via `libs/cli/bog_agents_cli/team_executor.py`). `bog_agents/cost_ledger.py` (`CostLedger` + `RunawayCaps` spawn/search/spend caps — every team/subagent spawn must be counted). `bog_agents/evidence.py` (`EvidenceBundle`, `collect_git_evidence`, `render_evidence_markdown`; `merge_ready` gates on checks + rubric). The `/best-of-n` (`best_of_n.py`) and `/jury` CLI features build on these with the rubric grader. When touching any, preserve the "tested core + injected `invoke`/`runner`" split so they unit-test without live models.

**OS sandbox (#22):** `bog_agents/sandbox/local_sandbox.py` wraps shell commands in bubblewrap/seatbelt; `bog_agents/sandbox/egress_proxy.py` is a threaded localhost CONNECT **allowlist proxy** (`host_allowed` suffix-match on label boundaries). Wired into `LocalShellBackend(sandbox=..., require_sandbox=...)` — opt-in, fail-closed where no launcher exists (Windows today). Driven from `.bog-agents/sandbox.toml` (`local_sandbox` level + `network_allowlist`) via `SandboxConfig.build_local_sandbox`. Invariant: the allowlist is proxy-enforced (cooperating tools), NOT a kernel boundary — a hard cut needs `--unshare-net` (no allowlist). `bwrap` re-shares the net namespace (`--share-net`) only when egress is wanted.

**Middleware ordering** (Wave W): the order of middleware in `graph.py` is load-bearing for correctness (CostTracker must wrap before Summarization, Summarization must run before PromptCaching, etc.). The canonical order is locked by `tests/unit_tests/test_middleware_canonical_order.py` — when an intentional reorder is needed, audit the affected interactions and update the test assertions in the same commit. Hard ordering constraints (e.g. ResultSynthesis requires ParallelWorktree earlier in the list) are also declared via `requires: ClassVar` and enforced at build time by `_validate_middleware_ordering`.

**Street Sweeper (`street_sweeper.py`)** — continuous, lossless-first context pruning that runs on *every* model call (vs. `SummarizationMiddleware`'s one-shot compaction at ~85% full). It is a **view transformation**: canonical history stays untouched in LangGraph state; the sweeper reshapes only the per-call request via `request.override(messages=...)`, offloading dropped content to the backend (recoverable via the `recall_swept` tool). Invariant to preserve when touching it: the sweep **never changes message count or order** — only message text — which is what keeps it composable with `SummarizationMiddleware` (cutoff indices stay aligned) and `AnthropicPromptCachingMiddleware` (stable prefix).

**Lazy loading**: Both `bog_agents/__init__.py` AND `bog_agents/middleware/__init__.py` use `_LAZY_IMPORTS` dicts and `__getattr__` so `import bog_agents.middleware` does NOT eagerly pull every submodule. Follow this pattern when adding new middleware: append to `_LAZY_IMPORTS`, do NOT add a top-level `from … import …` line.

**Backends**: Pluggable file system backends (local, composite, sandbox), state management backends, and shell execution backends. `LocalShellBackend._DANGEROUS_PATTERNS` is an **accident-catcher**, not a security boundary; the real safeguard for adversarial input is HITL + `SafeToolsMiddleware`.

**deepagents compatibility** (Waves 1–3, tracked in `PARITY.md`): bog-agents is a source-level drop-in for the deepagents 0.6.12 public API and co-installable with it in one venv. `bog_agents/deepagents.py` provides the deepagents-style names (`create_deep_agent`, `DeepAgentState`, `FilesystemPermission`, …), re-exported from top-level `bog_agents`. Built-in harness + provider profiles live in `bog_agents/profiles/`. Parity is a maintained guarantee, not a one-time port — when changing backend result types (`FileData`, `LsResult`, …) or the public export surface, check `PARITY.md` for what deepagents expects.

### CLI (`libs/cli/`)

Built with Textual. Key patterns:
- Workers (`@work` decorator) for async operations
- Message passing for widget communication
- Reactive attributes for state management
- Slash commands declared as `SlashCommand(spec=SlashCommandSpec(...), handler_method="_handle_…_command")` entries in `libs/cli/bog_agents_cli/commands/*.py` (general, agent, analysis, config, enterprise, git, info, multimodal, quality, session, ui, web). `command_registry.get_slash_commands()` aggregates them; `widgets/autocomplete.py` imports the aggregated list. **When adding a new slash command, add the spec to one of the `commands/*.py` modules — NOT to `autocomplete.py`.**
- Handler methods live on `BogAgentsApp` in `libs/cli/bog_agents_cli/app.py` (the long-lived god class). Each handler is `async def _handle_<name>_command(self, command: str) -> None`. Keep handlers thin — delegate to a standalone controller module so the logic stays testable without spinning up the TUI (see `expert_controller.py` for the canonical pattern).
- Heavy imports deferred to runtime (never at module level in entry points)
- Help screen hand-maintained in `ui.show_help()` with drift-detection test against argparse
- SDK version constraint in `libs/cli/pyproject.toml` is currently `bog-agents>=0.7.0,<1.0.0` (range, not exact pin). When this is tightened back to `==`, this paragraph should be updated and a CI smoketest added that the latest SDK still satisfies the constraint.
- **Headless command surface** (`headless_commands.py`): `bog-agents command "/help"` runs a curated subset of slash commands without the TUI. Headless handlers are standalone functions `(args: str) -> HeadlessResult` registered in `HEADLESS_COMMANDS` — when adding an informational/config slash command, consider registering a headless twin.
- **Config surface**: `config_manifest.py` is the single source of truth for user-tunable scalar options (type, typed default, env var name, `config.toml` location; precedence: env var > `config.toml` > default). Every `BOG_AGENTS_*` env var must be defined as a constant in `_env_vars.py` — a drift-detection test greps the package for unregistered string literals. Provider credentials are derived automatically from `PROVIDER_API_KEY_ENV`, so adding a provider needs no manifest change.
- **Theme system** (`theme.py`): the matte-swamp palette is a registered Textual theme named `bog` (default), with user-defined themes via `/theme`. Do NOT re-introduce hard-coded `$primary:`-style variable overrides at the top of `app.tcss` — they shadow the active theme and break `/theme`.
- **Skill trust store** (`skill_trust.py` + `skill_trust_controller.py`): the SDK refuses symlinked skill directories by default (`_filter_skill_dirs`, enforced on both the sync and async listing paths); `/skills trust <path>` records an explicit per-directory exception in a persistent trust store, wired into `SkillsMiddleware` through its pluggable symlink-trust checker hook.
- **MCP OAuth** (`mcp_oauth.py`): remote MCP servers authenticate through the `mcp` SDK's `OAuthClientProvider` (RFC 9728 discovery, dynamic client registration, PKCE, auto-refresh — all inside the SDK). This module supplies only token storage (`~/.bog-agents/mcp-oauth/`), the browser redirect, and the loopback callback handler — don't reimplement OAuth steps by hand.
- **`/effort`** (`reasoning_effort.py`): maps `low/medium/high/max` onto each provider's real reasoning knob (Anthropic `output_config.effort`, OpenAI `reasoning.effort`, Gemini `thinking_level`, …). Never map effort back onto `max_tokens`/`temperature` — the legacy hack truncated reasoning models.

**Prompt-routing family** — three composable modes that intercept a plain user prompt in `_handle_user_message` (after @-mention resolution, before the agent worker launches): (1) **Operator** (`operator_mode.py`, `/operator`) — a judge model classifies each prompt `easy/medium/hard/max` and stages a one-turn model+effort override via `app._operator_turn_model` / `_operator_turn_effort` (consumed by `_build_cli_context`, cleared in `_run_agent_task`'s finally); presets (anthropic default, bedrock, local, hybrid) + user presets live in `~/.bog-agents/operator.toml`; the judge may also escalate a prompt to butcher or jtbd. Judge failures must never block a turn — every path falls through to the user's active model. (2) **Butcher** (`butcher.py`, `/butcher`) — a strong model slices a job into self-contained instruction files under `.bog-agents/butcher/<job-id>/` (manifest.json + slice-NN.md + report.md), then weak workers (sidecar-style async model→tool loop with scoped write tools) execute slices sequentially in-place, each verified by the butcher with a retry→escalation ladder. (3) **JTBD** (`jtbd.py`, `/jtbd`) — interview → Job Spec artifact (`.bog-agents/jtbd/<id>/job-spec.md`) → outcome-driven execution brief → outcome verification (`/jtbd verify`). All three are pure-logic modules with injected `invoke` callables; chat widgets import from `bog_agents_cli.widgets.messages` (NOT `widgets.chat_messages`, which never existed).

**Dreamscape (`libs/cli/bog_agents_cli/dreamscape/`)** — agent lifecycle (Awake/Idle/Dormant/Dreaming/Imagining), dormancy-triggered dream generation, imagination injection, and two-tier laws (`.bog-agents/laws.md` hard-reject vs `.bog-agents/constitution.md` soft/log-only). Two invariants: (1) opt-in by design — without `~/.bog-agents/dreamscape.toml` setting `enabled = true`, every dreamscape middleware must be a no-op; (2) dreamscape errors must never reach the user's prompt path — fall through to underlying agent behavior (see the `_safe` pattern in each middleware). `dreamscape/config.py:load_dreamscape_config()` is the single source of truth for toggles. Long-term effectiveness snapshots live in `docs/dreamscape-runs/` — consult them before changing dream/imagination behavior.

### Daemon (`libs/daemon/`)

A long-running FastAPI service that fires agents on cron, interval, file-change, webhook, or git-push triggers and dispatches results to log, stdout, file, Slack, webhook, email, or GitHub-comment targets. Currently the healthiest satellite — keep its tests (flat `tests/` directory, no unit/integration split) passing when touching the SDK's `create_agent` signature.

**Reliability posture (Wave 3):**
- **Cron uses `croniter`** (`scheduler.py:_is_cron_due`) with **missed-slot catch-up**: if the daemon was down across a scheduled slot, the job fires *once* on restart (not N-times backfill), because the baseline is `last_run_at`, not "now". Interval triggers already self-catch-up.
- **File triggers are event-driven** via `file_watch.py:FileWatchManager` (watchdog), activated only inside `run_forever` — a bare `_tick()` (unit tests) never starts an observer. First runs and unwatchable dirs fall back to the `os.walk` poll (`_check_file_trigger`); `_detect_file_change` picks which path. watchdog is a hard dep but the code degrades to polling if it's absent.
- **Per-job retry** is opt-in: `AmbientJob.max_retries` / `retry_backoff_seconds` (default 0 = single-shot, unchanged) retry both the agent invocation *and* each output dispatch with exponential backoff (`runner.py:_invoke_agent_with_retry`, `_dispatch_with_retry`). Prompt/skill resolution errors are deterministic and are NOT retried.
- **Startup reconciles orphaned runs** (`store.py:reconcile_orphaned_runs`, wired in `main.py:_run_daemon`): a run left `RUNNING` by a crash is stamped `FAILED` so `/runs` is honest.
- **Corrupt `jobs.json` is quarantined, never overwritten** (`store.py:_quarantine_corrupt_jobs`): unparseable content is renamed to `jobs.json.corrupt-<ts>` before the next save, so a bad file can't silently destroy every job. A transient `OSError` on read is NOT quarantined.
- **Network dispatch failures are recorded**: the webhook/slack/email/github dispatchers now raise on failure so `run_job` captures them in `run.dispatch_errors` (they used to swallow `URLError` and vanish into the log).

## Code Conventions

- **Type hints**: Mandatory on all public functions, no `any` type
- **Docstrings**: Google-style with Args/Returns/Raises sections
- **Line length**: 150 chars (ruff)
- **Single backticks** for inline code in docstrings — never Sphinx double-backtick (` ``code`` `)
- **Ruff suppression**: Use inline `# noqa: RULE` for individual exceptions; reserve `per-file-ignores` for categorical policy
- **Async tests**: Do NOT add `@pytest.mark.asyncio` — all packages set `asyncio_mode = "auto"`
- **No mocks** where possible — test actual implementation
- **Test structure** mirrors source structure (`tests/unit_tests/`, `tests/integration_tests/`)

## Public API Stability

Preserve function signatures, argument positions, and names for exported/public methods. Use keyword-only args for new parameters: `*, new_param: str = "default"`. Check `__init__.py` exports before modifying any public interface.

## Commit Standards

Conventional Commits format, lowercase, scope required:
```
feat(sdk): add new chat completion feature
fix(cli): resolve type hinting issue
chore(harbor): update infrastructure dependencies
```

Allowed types: feat, fix, chore, refactor, docs, test. `feat` triggers minor bump, `fix` triggers patch bump via release-please.

## Adding a New Model Provider (CLI)

1. `libs/cli/bog_agents_cli/model_config.py` — add to `PROVIDER_API_KEY_ENV` (alphabetical).
2. `libs/cli/bog_agents_cli/api_keys.py` — add to `_PROVIDER_KEY_METADATA` so the vault auto-injects the key (the helper at module load asserts these two registries stay in sync, see P0-G; missing metadata raises at import time).
3. `libs/cli/pyproject.toml` — add optional dependency and include in `all-providers`.
4. `libs/cli/tests/unit_tests/test_model_config.py` — add assertion.

Only add `detect_provider()` entry if the provider has a distinctive model name prefix.

## File-handling conventions

- **Always pass `encoding="utf-8"` to `Path.read_text` / `Path.write_text`** anywhere a user may have configured non-ASCII content (settings files, hooks, skills, prompts, oauth tokens, profile names). Windows in non-en-US locales decodes through cp1252/cp932/cp949 by default — a single smart quote in a hooks.json crashes the CLI. Fixed in P0-H sweep; ruff's `PLW1514` should be left enabled going forward.
- **For secret-bearing files** (vault, oauth tokens, audit trail) use `bog_agents_cli.io_utils.atomic_write_text` AND call `vars_store._secure_owner_only(path)` which is cross-platform (POSIX chmod 0600 / Windows icacls). Don't trust a bare `chmod` on Windows — it's a no-op. See P0-E.

## Root tracking docs

- `REVIEW.md` — Senior Principal Engineer audit findings (P0/P1/P2). When you
  ship a change that addresses a P0/P1 entry, note the cross-reference in the
  commit message (e.g. "fixes P0-G") so reviewers can map back.
- `ROADMAP.md` — strategic feature roadmap (companion to REVIEW.md, which
  tracks the correctness findings the roadmap assumes get fixed first).
- `PARITY.md` — deepagents 0.6.12 drop-in parity report. Waves 1–3 shipped;
  Wave 4 (satellites) deliberately deferred pending a value argument.
- `AGENTS.md` — generic agent-facing dev guidelines; overlaps this file's
  conventions (uv/make workflow, backtick and `# noqa` policy).
