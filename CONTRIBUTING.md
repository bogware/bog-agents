# Contributing to Bog Agents

Thanks for your interest in contributing! This guide covers everything you need to get started.

## Prerequisites

- **Python 3.11+**
- **[uv](https://docs.astral.sh/uv/)** — used for dependency management across all packages
- **git** — conventional commits required (see below)

## Repository Structure

```
libs/
├── bog-agents/      # Core SDK (bog-agents on PyPI)
├── cli/             # Terminal UI (bog-agents-cli on PyPI)
├── acp/             # Agent Client Protocol (Zed editor)
├── harbor/          # Evaluation framework
├── vscode-extension/# VS Code extension
└── partners/        # Sandbox integrations (daytona, modal, runloop, quickjs)
```

Each package has its own `pyproject.toml`, `uv.lock`, and `Makefile`.

## Getting Started

```bash
# Clone the repo
git clone https://github.com/bogware/bog-agents.git
cd bog-agents

# Windows PowerShell bootstrap for the main development packages
./scripts/repo.ps1 init

# Install SDK dependencies
cd libs/bog-agents && uv sync && cd ../..

# Install CLI dependencies (links to local SDK)
cd libs/cli && uv sync && cd ../..

# Run the CLI locally
cd libs/cli && uv run bog-agents
```

## Running Tests

```bash
# From any package directory:
make test                    # Unit tests (no network, parallel)
make integration_test        # Integration tests (network required)
make lint                    # Ruff + ty type checker
make format                  # Auto-format

# Single test file:
uv run --group test pytest tests/unit_tests/test_specific.py

# From repo root - lint/format all packages:
make lint
make format
```

## Upgrades and Lockfiles

```bash
# Check lockfiles for the SDK + CLI
./scripts/repo.ps1 lock-check

# Check lockfiles for every managed package under libs/
./scripts/repo.ps1 lock-check -AllPackages

# Refresh lockfiles for every managed package under libs/
./scripts/repo.ps1 lock -AllPackages
```

## Code Conventions

- **Type hints** on all public functions — no `Any` types in public APIs
- **Google-style docstrings** with `Args:`, `Returns:`, `Raises:` sections
- **Line length**: 150 characters (ruff)
- **Single backticks** for inline code in docstrings (not Sphinx double-backtick)
- **No `@pytest.mark.asyncio`** — all packages use `asyncio_mode = "auto"`
- **No mocks** where possible — test the actual implementation
- **Test structure** mirrors source: `tests/unit_tests/`, `tests/integration_tests/`

## Commit Standards

We use [Conventional Commits](https://www.conventionalcommits.org/) with lowercase and a required scope:

```
feat(sdk): add new chat completion feature
fix(cli): resolve type hinting issue
chore(harbor): update infrastructure dependencies
refactor(sdk): simplify middleware registration
docs(cli): update slash command documentation
test(sdk): add coverage for parallel agents
```

**Allowed types:** `feat`, `fix`, `chore`, `refactor`, `docs`, `test`

- `feat` triggers a minor version bump
- `fix` triggers a patch version bump

## Adding Middleware

1. Create your middleware in `libs/bog-agents/bog_agents/middleware/your_middleware.py`
2. Subclass `AgentMiddleware` and implement `wrap_model_call()`
3. Add a lazy import entry in `libs/bog-agents/bog_agents/__init__.py` (`_LAZY_IMPORTS` dict)
4. Add the eager import and `__all__` entry in `libs/bog-agents/bog_agents/middleware/__init__.py`
5. Add a feature flag and wiring in `libs/bog-agents/bog_agents/graph.py`
6. Write tests in `libs/bog-agents/tests/unit_tests/`

## Adding a Model Provider (CLI)

1. Add to `PROVIDER_API_KEY_ENV` in `libs/cli/bog_agents_cli/model_config.py` (alphabetical)
2. Add optional dependency in `libs/cli/pyproject.toml` and include in `all-providers`
3. Add assertion in `libs/cli/tests/unit_tests/test_model_config.py`
4. Only add `detect_provider()` entry if the provider has a distinctive model name prefix

## Public API Stability

- Preserve function signatures, argument positions, and names for exported methods
- Use keyword-only args for new parameters: `*, new_param: str = "default"`
- Check `__init__.py` exports before modifying any public interface

## Pull Requests

- PRs are validated by CI (lint + tests across Python 3.11-3.14)
- PR titles must follow Conventional Commits format
- All tests must pass before merge
- Prefer small, focused PRs over large changes

## Questions?

Open an issue at [github.com/bogware/bog-agents/issues](https://github.com/bogware/bog-agents/issues).
