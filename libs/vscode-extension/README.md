# Bog Agents for VS Code

> *Pass through in harmony. Opinionated where it matters.*

A VS Code extension that brings the Bog Agents coding agent right into your editor — chat, review, explain, and fix without leaving the file you're in.

## Features

- **Chat Panel** — conversational AI assistant in the sidebar
- **Context Menu Actions** — right-click to review, explain, or fix selected code
- **Keyboard Shortcut** — `Ctrl+Shift+A` / `Cmd+Shift+A` to open chat
- **Configurable Model** — use any provider:model supported by Bog Agents

## Requirements

- [Bog Agents CLI](https://pypi.org/project/bog-agents-cli/) installed and available in PATH
- An API key for your chosen model provider (e.g., `ANTHROPIC_API_KEY`)

## Installation

### From VS Code Marketplace

Search for "Bog Agents" in the VS Code extensions panel.

### From VSIX

```bash
cd libs/vscode-extension
npm install
npm run compile
npx @vscode/vsce package
code --install-extension bog-agents-vscode-0.1.0.vsix
```

## Extension Settings

| Setting | Default | Description |
|---------|---------|-------------|
| `bog-agents.model` | `anthropic:claude-sonnet-4-6` | Default model (provider:model format) |
| `bog-agents.autoApprove` | `false` | Auto-approve tool calls |
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

```bash
npx @vscode/vsce login <publisher-name>
npx @vscode/vsce publish
```
