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

# From repo root: lint/format all packages
make lint
make format
```

SDK type checking: `uv run --all-groups ty check bog_agents` (from `libs/bog-agents/`)

## Architecture

### SDK (`libs/bog-agents/`)

The entry point is `create_agent()` which returns a compiled LangGraph graph. The agent ships with base tools (filesystem, shell, planning, sub-agents) and a composable **middleware stack**.

**Middleware** (`bog_agents/middleware/`) is the primary extension mechanism. 100+ middleware implementations handle concerns like git tools, repo mapping, cost tracking, checkpointing, plan mode, auto-quality checks, context packing, summarization, memory, skills, and the **Expert Mode** rule engine (`expert_rules.py` + `expert_engine/`). All middleware inherits from `AgentMiddleware`.

**Expert Mode (`ExpertRulesMiddleware`)** — a small forward+backward-chaining rule engine that loads YAML policies from `.bog-agents/expert_rules/*.yaml`, asserts a `tool_call` fact before every tool call, and can deny / modify / require-approval the call. CLI surface: `/expert`, `/why`, `/prove`. The engine is opt-in (default `enabled=False`) and composes with `RulesMiddleware` (the prose rule injector — different feature, same family of names).

**STUB middleware (P0-A)** — a cluster of vertical-market modules (`financial_data`, `due_diligence`, `earnings_analysis`, `tax_optimization`, `portfolio_analysis`, `market_sentiment`, `peer_comparison`, `meeting_prep`, `regulatory_alerts`, `regulatory_impact`, `scenario_engine`, `client_knowledge_base`, `client_reports`, `firm_deployment`, plus `agent_teams` and `multi_agent_orchestrator`) ship as **scaffolds, not implementations**. Each module's docstring carries a "STUB — NOT FOR PRODUCTION USE" banner. Do not enable these in flows whose output reaches a customer-facing surface. The plan in REVIEW.md is to extract them to a sister package once real.

**Lazy loading**: Both `bog_agents/__init__.py` AND `bog_agents/middleware/__init__.py` use `_LAZY_IMPORTS` dicts and `__getattr__` so `import bog_agents.middleware` does NOT eagerly pull every submodule (was 95, now ~8 transitive — see `tests/unit_tests/test_lazy_import_health.py`). Follow this pattern when adding new middleware: append to `_LAZY_IMPORTS`, do NOT add a top-level `from … import …` line.

**Backends**: Pluggable file system backends (local, composite, sandbox), state management backends, and shell execution backends. `LocalShellBackend._DANGEROUS_PATTERNS` is an **accident-catcher**, not a security boundary; the real safeguard for adversarial input is HITL + `SafeToolsMiddleware`.

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

### Daemon (`libs/daemon/`)

A long-running FastAPI service that fires agents on cron, interval, file-change, webhook, or git-push triggers and dispatches results to log, stdout, file, Slack, webhook, email, or GitHub-comment targets. Currently the healthiest satellite (v0.8.7, Beta) — keep its 6 test files passing when touching the SDK's `create_agent` signature.

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

## REVIEW.md

`REVIEW.md` at the repo root tracks the Senior Principal Engineer audit
findings (P0/P1/P2) and the long-arc feature roadmap. When you ship a
change that addresses a P0/P1 entry, note the cross-reference in the
commit message (e.g. "fixes P0-G") so reviewers can map back.
