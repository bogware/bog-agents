/**
 * Bog Agents VS Code Extension
 *
 * Feature #19: VS Code extension scaffold — integrates Bog Agents
 * into VS Code with chat panel, context menu actions, and inline
 * code assistance.
 */

import * as vscode from 'vscode';
import { ChildProcess, spawn } from 'child_process';
import * as fs from 'fs';
import * as path from 'path';

let chatPanel: vscode.WebviewPanel | undefined;
let cliProcess: ChildProcess | undefined;
let outputChannel: vscode.OutputChannel | undefined;

export function activate(context: vscode.ExtensionContext): void {
    outputChannel = vscode.window.createOutputChannel('Bog Agents');
    context.subscriptions.push(outputChannel);
    outputChannel.appendLine('Bog Agents extension activated');

    context.subscriptions.push(
        vscode.commands.registerCommand('bog-agents.openChat', () => openChat(context)),
        vscode.commands.registerCommand('bog-agents.reviewSelection', () => handleSelection('review')),
        vscode.commands.registerCommand('bog-agents.explainSelection', () => handleSelection('explain')),
        vscode.commands.registerCommand('bog-agents.fixSelection', () => handleSelection('fix')),
        vscode.commands.registerCommand('bog-agents.runDoctor', () => runDoctor()),
    );
}

export function deactivate(): void {
    if (cliProcess) {
        cliProcess.kill();
        cliProcess = undefined;
    }
    if (chatPanel) {
        chatPanel.dispose();
        chatPanel = undefined;
    }
}

/**
 * Resolve and validate the CLI binary path.
 *
 * Returns null when the workspace is untrusted, the path is empty/relative
 * (rejected to stop a workspace from supplying a relative ./mallory shim),
 * or the file does not exist.
 */
function getCliPath(): string | null {
    if (!vscode.workspace.isTrusted) {
        vscode.window.showErrorMessage(
            'Bog Agents requires a trusted workspace to launch the CLI.',
        );
        return null;
    }
    const config = vscode.workspace.getConfiguration('bog-agents');
    const configured = config.get<string>('cliPath', '').trim();
    if (configured) {
        // Reject relative paths from workspace settings — they would resolve
        // against the workspace folder and let an untrusted-but-just-trusted
        // repo redirect the CLI.
        if (!path.isAbsolute(configured)) {
            vscode.window.showErrorMessage(
                `Bog Agents: bog-agents.cliPath must be an absolute path, got: ${configured}`,
            );
            return null;
        }
        try {
            const stat = fs.statSync(configured);
            if (!stat.isFile()) {
                vscode.window.showErrorMessage(
                    `Bog Agents: configured cliPath is not a regular file: ${configured}`,
                );
                return null;
            }
        } catch {
            vscode.window.showErrorMessage(
                `Bog Agents: configured cliPath does not exist: ${configured}`,
            );
            return null;
        }
        return configured;
    }
    // Fall back to PATH lookup for the literal name. spawn() will resolve it.
    return 'bog-agents';
}

function getModel(): string {
    const config = vscode.workspace.getConfiguration('bog-agents');
    return config.get<string>('model', 'anthropic:claude-sonnet-4-6');
}

/**
 * Build a minimal env for the CLI subprocess, deliberately stripping
 * variables an untrusted workspace could plant via dotenv-style auto-loaders
 * in the parent VS Code process.
 */
function buildChildEnv(): NodeJS.ProcessEnv {
    const allow = new Set([
        'PATH',
        'HOME',
        'USERPROFILE',
        'LANG',
        'LC_ALL',
        'LC_CTYPE',
        'TERM',
        'TMPDIR',
        'TMP',
        'TEMP',
        'SHELL',
        'COMSPEC',
        'SystemRoot',
        'APPDATA',
        'LOCALAPPDATA',
    ]);
    const env: NodeJS.ProcessEnv = {};
    for (const [k, v] of Object.entries(process.env)) {
        if (allow.has(k) || k.startsWith('BOG_AGENTS_')) {
            env[k] = v;
        }
    }
    return env;
}

function openChat(context: vscode.ExtensionContext): void {
    if (chatPanel) {
        chatPanel.reveal(vscode.ViewColumn.Beside);
        return;
    }

    chatPanel = vscode.window.createWebviewPanel(
        'bogAgentsChat',
        'Bog Agents Chat',
        vscode.ViewColumn.Beside,
        {
            enableScripts: true,
            retainContextWhenHidden: true,
            // Restrict what the webview is allowed to load.
            localResourceRoots: [vscode.Uri.joinPath(context.extensionUri, 'resources')],
        },
    );

    chatPanel.webview.html = getChatHtml(chatPanel.webview);

    chatPanel.webview.onDidReceiveMessage(
        async (message: { type?: unknown; text?: unknown }) => {
            if (typeof message?.type !== 'string') {
                return;
            }
            if (message.type === 'send' && typeof message.text === 'string') {
                await sendToCli(message.text);
            }
        },
        undefined,
        context.subscriptions,
    );

    chatPanel.onDidDispose(() => {
        chatPanel = undefined;
    });
}

async function handleSelection(action: string): Promise<void> {
    const editor = vscode.window.activeTextEditor;
    if (!editor) {
        vscode.window.showWarningMessage('No active editor');
        return;
    }

    const selection = editor.selection;
    const text = editor.document.getText(selection);
    if (!text) {
        vscode.window.showWarningMessage('No text selected');
        return;
    }

    const filePath = editor.document.uri.fsPath;
    const language = editor.document.languageId;
    const startLine = selection.start.line + 1;
    const endLine = selection.end.line + 1;

    const prompts: Record<string, string> = {
        review: `Review this ${language} code from ${filePath}:${startLine}-${endLine}:\n\n\`\`\`${language}\n${text}\n\`\`\``,
        explain: `Explain this ${language} code from ${filePath}:${startLine}-${endLine}:\n\n\`\`\`${language}\n${text}\n\`\`\``,
        fix: `Fix any issues in this ${language} code from ${filePath}:${startLine}-${endLine}:\n\n\`\`\`${language}\n${text}\n\`\`\``,
    };

    const prompt = prompts[action] || prompts['review'];
    await sendToCli(prompt);
}

async function sendToCli(prompt: string): Promise<void> {
    const cliPath = getCliPath();
    if (cliPath === null) {
        return;
    }
    const model = getModel();
    const workspaceFolder = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;

    try {
        const args = ['--model', model, '--print', prompt, '--no-stream'];
        const proc = spawn(cliPath, args, {
            cwd: workspaceFolder,
            env: buildChildEnv(),
            // shell: false (default) — never let the CLI path go through a shell.
        });

        let output = '';
        proc.stdout?.on('data', (data: Buffer) => {
            output += data.toString();
            if (chatPanel) {
                chatPanel.webview.postMessage({
                    type: 'response',
                    text: output,
                });
            }
        });

        proc.stderr?.on('data', (data: Buffer) => {
            outputChannel?.appendLine(`[CLI stderr] ${data.toString().trimEnd()}`);
        });

        proc.on('error', (err: NodeJS.ErrnoException) => {
            if (err.code === 'ENOENT') {
                vscode.window.showErrorMessage(
                    'bog-agents CLI not found. Install with: pip install bog-agents-cli',
                );
            } else {
                vscode.window.showErrorMessage(`Failed to run Bog Agents CLI: ${err.message}`);
            }
        });

        proc.on('close', (code: number | null) => {
            if (code !== 0) {
                vscode.window.showErrorMessage(`Bog Agents CLI exited with code ${code}`);
            }
        });
    } catch (error) {
        vscode.window.showErrorMessage(`Failed to run Bog Agents CLI: ${error}`);
    }
}

async function runDoctor(): Promise<void> {
    const cliPath = getCliPath();
    if (cliPath === null) {
        return;
    }
    // Use VS Code's terminal API with a structured argv list — this avoids
    // shell concatenation of the (validated, but still untrusted) cliPath.
    const terminal = vscode.window.createTerminal({
        name: 'Bog Agents Doctor',
        shellPath: cliPath,
        shellArgs: ['--doctor'],
    });
    terminal.show();
}

/**
 * Build the chat-panel HTML with a strict CSP. We use a per-load nonce so the
 * inline script can run while still blocking arbitrary injected scripts.
 */
function getChatHtml(webview: vscode.Webview): string {
    const nonce = makeNonce();
    const cspSource = webview.cspSource;
    const csp = [
        `default-src 'none'`,
        `style-src ${cspSource} 'unsafe-inline'`,
        `script-src 'nonce-${nonce}'`,
        `img-src ${cspSource} https: data:`,
        `font-src ${cspSource}`,
    ].join('; ');
    return `<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta http-equiv="Content-Security-Policy" content="${csp}">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Bog Agents Chat</title>
    <style>
        body {
            font-family: var(--vscode-font-family);
            color: var(--vscode-foreground);
            background: var(--vscode-editor-background);
            margin: 0;
            padding: 16px;
            display: flex;
            flex-direction: column;
            height: 100vh;
            box-sizing: border-box;
        }
        #messages {
            flex: 1;
            overflow-y: auto;
            margin-bottom: 16px;
        }
        .message {
            margin-bottom: 12px;
            padding: 8px 12px;
            border-radius: 6px;
            white-space: pre-wrap;
        }
        .user-message {
            background: var(--vscode-input-background);
            border: 1px solid var(--vscode-input-border);
        }
        .ai-message {
            background: var(--vscode-editor-inactiveSelectionBackground);
        }
        #input-area {
            display: flex;
            gap: 8px;
        }
        #input {
            flex: 1;
            padding: 8px;
            background: var(--vscode-input-background);
            color: var(--vscode-input-foreground);
            border: 1px solid var(--vscode-input-border);
            border-radius: 4px;
            font-family: inherit;
        }
        #send-btn {
            padding: 8px 16px;
            background: var(--vscode-button-background);
            color: var(--vscode-button-foreground);
            border: none;
            border-radius: 4px;
            cursor: pointer;
        }
        #send-btn:hover {
            background: var(--vscode-button-hoverBackground);
        }
    </style>
</head>
<body>
    <div id="messages"></div>
    <div id="input-area">
        <input type="text" id="input" placeholder="Ask Bog Agents..." />
        <button id="send-btn">Send</button>
    </div>
    <script nonce="${nonce}">
        const vscode = acquireVsCodeApi();
        const messages = document.getElementById('messages');
        const input = document.getElementById('input');
        const sendBtn = document.getElementById('send-btn');
        const MAX_MSG_LEN = 200000;

        function addMessage(text, isUser) {
            const div = document.createElement('div');
            div.className = 'message ' + (isUser ? 'user-message' : 'ai-message');
            div.textContent = text;
            messages.appendChild(div);
            messages.scrollTop = messages.scrollHeight;
        }

        function send() {
            const text = input.value.trim();
            if (!text) return;
            addMessage(text, true);
            vscode.postMessage({ type: 'send', text });
            input.value = '';
        }

        sendBtn.addEventListener('click', send);
        input.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') send();
        });

        window.addEventListener('message', (event) => {
            const msg = event.data;
            if (!msg || typeof msg !== 'object') return;
            if (msg.type !== 'response') return;
            const text = typeof msg.text === 'string' ? msg.text.slice(0, MAX_MSG_LEN) : '';
            const aiMessages = messages.querySelectorAll('.ai-message');
            const last = aiMessages[aiMessages.length - 1];
            if (last && last.dataset.streaming === 'true') {
                last.textContent = text;
            } else {
                const div = document.createElement('div');
                div.className = 'message ai-message';
                div.dataset.streaming = 'true';
                div.textContent = text;
                messages.appendChild(div);
            }
            messages.scrollTop = messages.scrollHeight;
        });
    </script>
</body>
</html>`;
}

function makeNonce(): string {
    const alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789';
    let s = '';
    for (let i = 0; i < 32; i++) {
        s += alphabet[Math.floor(Math.random() * alphabet.length)];
    }
    return s;
}
