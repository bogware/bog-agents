# Bog Agents for VS Code

> *Pass through in harmony. Opinionated where it matters.*

A VS Code extension that brings the Bog Agents coding agent right into your editor — chat, review, explain, and fix without leaving the file you're in.

## Features

- **Sidebar Chat** — the Bog Agents view in the activity bar; every prompt gets its own reply bubble
- **Chat Panel** — the same chat as an editor tab (`Ctrl+Shift+A` / `Cmd+Shift+A`)
- **Context Menu Actions** — right-click to review, explain, or fix selected code; the reply lands in whichever chat is open
- **Configurable Model** — use any provider:model supported by Bog Agents
- **Auto-approve** — opt in to let the CLI run tool calls without asking (`bog-agents.autoApprove`)

## Requirements

- [Bog Agents CLI](https://pypi.org/project/bog-agents-cli/) installed and available in PATH
- An API key for your chosen model provider (e.g., `ANTHROPIC_API_KEY`)

## Installation

### From VS Code Marketplace

Search for "Bog Agents" in the VS Code extensions panel (publisher `bog-agents`).

### From a CI build

Every pull request's `VS Code extension` job attaches `bog-agents-vscode.vsix`
as a workflow artifact — download it and run
`code --install-extension bog-agents-vscode.vsix`.

### From source

```bash
cd libs/vscode-extension
npm install
npm run compile
npx @vscode/vsce package
code --install-extension bog-agents-vscode-0.2.0.vsix
```

## Extension Settings

| Setting | Default | Description |
|---------|---------|-------------|
| `bog-agents.model` | `anthropic:claude-sonnet-4-6` | Default model (provider:model format) |
| `bog-agents.autoApprove` | `false` | Pass `--auto-approve` so tool calls run without asking. The chat runs the CLI in `--print` mode, which cannot answer approval prompts, so with this off a tool call that needs approval ends the turn. |
| `bog-agents.cliPath` | (auto-detect) | Path to bog-agents CLI binary |

## Commands

| Command | Keybinding | Description |
|---------|-----------|-------------|
| `Bog Agents: Open Chat` | `Ctrl+Shift+A` | Open the chat panel |
| `Bog Agents: Review Selected Code` | (context menu) | Code review on selection |
| `Bog Agents: Explain Selected Code` | (context menu) | Explain selected code |
| `Bog Agents: Fix Selected Code` | (context menu) | Fix issues in selection |
| `Bog Agents: Run Diagnostics` | — | Run `/doctor` in terminal |

## Development

```bash
cd libs/vscode-extension
npm install
npm run compile    # build
npm run watch      # dev mode with auto-rebuild
npm run lint       # lint
```

## Publishing

Releases go through the manual `VS Code Extension Release` workflow
(`.github/workflows/vscode-extension.yml`): run it with the version to
release and `publish = true`. It needs a `VSCE_PAT` repository secret — a
Marketplace Personal Access Token for the `bog-agents` publisher — and fails
before publishing if the manifest is not marketplace-ready (a PNG icon, a
`LICENSE` file and a clean `npm run lint` are all checked on every PR by the
`VS Code extension` CI job).

To publish by hand instead:

```bash
npx @vscode/vsce login bog-agents
npx @vscode/vsce publish
```
