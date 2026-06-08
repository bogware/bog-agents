# Bog Agents Monorepo

> *Pass through in harmony. Opinionated where it matters.*

> [!IMPORTANT]
> Read the [Contributing Guide](https://github.com/bogware/bog-agents/blob/main/CONTRIBUTING.md)
> before opening a PR. If you are a coding agent reading this, stop and get the
> full picture of what's acceptable before you change anything here.

This repository is a monorepo. Every package lives under `libs/` and carries
its own `pyproject.toml`, `Makefile`, version, and `README.md`. The SDK, CLI,
and daemon are released together on synchronized version numbers; the rest tag
independently.

## Packages

| Package | What it is |
|---|---|
| [`bog-agents/`](bog-agents/) | **Core SDK.** `create_agent`, ~80 composable middlewares, pluggable backends, tool bundles, deepagents compatibility. |
| [`cli/`](cli/) | **Terminal CLI** (`bog-agents`). Textual TUI, 120+ slash commands, MCP marketplace, headless command surface, `bog-agents drive` scripted runner. |
| [`daemon/`](daemon/) | **Ambient daemon.** Cron / file-change / webhook / git-push triggers; Slack / email / GitHub / file / webhook outputs; REST API. |
| [`acp/`](acp/) | **Agent Client Protocol** connector for editors like [Zed](https://zed.dev/). |
| [`harbor/`](harbor/) | **Evaluation harness** for Terminal Bench 2.0. |
| [`vscode-extension/`](vscode-extension/) | **VS Code extension** (TypeScript). |
| [`partners/`](partners/) | **Sandbox provider integrations** (see below). |

Each package's own `README.md` carries the specifics — install, usage, and
reference.

## Sandbox integrations (`partners/`)

The first-party sandbox shipped as source in this tree today is **Daytona**:

* [Daytona](partners/daytona/) — [`langchain-daytona`](https://pypi.org/project/langchain-daytona/) on PyPI

The SDK's `SandboxBackend` can also target Modal, RunLoop, and LangSmith
sandboxes at runtime via their respective extras; those providers are not
shipped as first-party source packages here. See the
[SDK README](bog-agents/README.md#backends) for the backend matrix.

## Working from source

```bash
# from a package directory, e.g. libs/cli
uv sync          # install dependencies
make test        # unit tests, no network
make lint        # ruff check + ruff format --diff + ty
make format      # ruff fix + ruff format
```

CI runs `make lint` + `make test` per package on every PR
(`.github/workflows/ci.yml`).
