/**
 * Bog Agents VS Code Extension
 *
 * Feature #19: VS Code extension scaffold — integrates Bog Agents
 * into VS Code with chat panel, context menu actions, and inline
 * code assistance.
 */

import * as vscode from 'vscode';
import { ChildProcess, spawn } from 'child_process';
import * as path from 'path';

let chatPanel: vscode.WebviewPanel | undefined;
let cliProcess: ChildProcess | undefined;

export function activate(context: vscode.ExtensionContext): void {
    console.log('Bog Agents extension activated');

    // Register commands
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

function getCliPath(): string {
    const config = vscode.workspace.getConfiguration('bog-agents');
    const configuredPath = config.get<string>('cliPath', '');
    return configuredPath || 'bog-agents';
}

function getModel(): string {
    const config = vscode.workspace.getConfiguration('bog-agents');
    return config.get<string>('model', 'anthropic:claude-sonnet-4-6');
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
        }
    );

    chatPanel.webview.html = getChatHtml();

    chatPanel.webview.onDidReceiveMessage(
        async (message: { type: string; text?: string }) => {
            if (message.type === 'send' && message.text) {
                await sendToCli(message.text);
            }
        },
        undefined,
        context.subscriptions
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
    const model = getModel();
    const workspaceFolder = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;

    try {
        const args = ['--model', model, '--print', prompt, '--no-stream'];
        const proc = spawn(cliPath, args, {
            cwd: workspaceFolder,
            env: { ...process.env },
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
            console.error('CLI stderr:', data.toString());
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
    const terminal = vscode.window.createTerminal('Bog Agents Doctor');
    terminal.show();
    terminal.sendText(`${cliPath} --doctor`);
}

function getChatHtml(): string {
    return `<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
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
    <script>
        const vscode = acquireVsCodeApi();
        const messages = document.getElementById('messages');
        const input = document.getElementById('input');
        const sendBtn = document.getElementById('send-btn');

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
            if (msg.type === 'response') {
                // Update or create AI message
                const aiMessages = messages.querySelectorAll('.ai-message');
                const last = aiMessages[aiMessages.length - 1];
                if (last && last.dataset.streaming === 'true') {
                    last.textContent = msg.text;
                } else {
                    const div = document.createElement('div');
                    div.className = 'message ai-message';
                    div.dataset.streaming = 'true';
                    div.textContent = msg.text;
                    messages.appendChild(div);
                }
                messages.scrollTop = messages.scrollHeight;
            }
        });
    </script>
</body>
</html>`;
}
