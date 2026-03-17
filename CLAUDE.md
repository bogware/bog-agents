# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Bog Agents is a Python monorepo providing an opinionated, production-ready AI agent framework built on LangGraph. It includes a core SDK (`create_agent`), an interactive CLI (Textual-based TUI), and supporting packages for evaluation, editor integrations, and sandbox providers.

## Monorepo Structure

```
libs/
├── bog-agents/      # Core SDK - agent creation, middleware, backends
├── cli/             # Terminal UI (bog-agents-cli) - Textual framework
├── acp/             # Agent Client Protocol (Zed editor)
├── harbor/          # Evaluation/benchmark framework (Terminal Bench 2.0)
├── vscode-extension/# VS Code extension
└── partners/        # Sandbox integrations (daytona, modal, runloop, quickjs)
```

Each package has its own `pyproject.toml`, `uv.lock`, and `Makefile`.

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

**Middleware** (`bog_agents/middleware/`) is the primary extension mechanism. 80+ middleware implementations handle concerns like git tools, repo mapping, cost tracking, checkpointing, plan mode, auto-quality checks, context packing, summarization, memory, and skills. All middleware inherits from `AgentMiddleware`.

**Lazy loading**: Middleware uses `_LAZY_IMPORTS` dict and `__getattr__` in `__init__.py` to keep `import bog_agents` fast. Follow this pattern when adding new middleware.

**Backends**: Pluggable file system backends (local, composite, sandbox), state management backends, and shell execution backends.

### CLI (`libs/cli/`)

Built with Textual. Key patterns:
- Workers (`@work` decorator) for async operations
- Message passing for widget communication
- Reactive attributes for state management
- Slash commands defined in `libs/cli/bog_agents_cli/widgets/autocomplete.py` as `(name, description, hidden_keywords)` tuples
- Heavy imports deferred to runtime (never at module level in entry points)
- Help screen hand-maintained in `ui.show_help()` with drift-detection test against argparse
- SDK version pinned exactly in `libs/cli/pyproject.toml` — bump when using new SDK features

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

1. `libs/cli/bog_agents_cli/model_config.py` — add to `PROVIDER_API_KEY_ENV` (alphabetical)
2. `libs/cli/pyproject.toml` — add optional dependency and include in `all-providers`
3. `libs/cli/tests/unit_tests/test_model_config.py` — add assertion

Only add `detect_provider()` entry if the provider has a distinctive model name prefix.
