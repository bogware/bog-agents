/**
 * Bog Agents VS Code Extension
 *
 * Feature #19: VS Code extension scaffold — integrates Bog Agents
 * into VS Code with chat panel, context menu actions, and inline
 * code assistance.
 *
 * v6 SAT-4: the activity-bar view (`bog-agents.chatView`) is backed by a real
 * `WebviewViewProvider` (it was declared in package.json but never
 * registered, so the sidebar stayed empty); replies stream into one bubble
 * per prompt instead of overwriting the previous answer; and the
 * `autoApprove` setting is actually passed to the CLI.
 */

import * as vscode from 'vscode';
import { ChildProcess, spawn } from 'child_process';
import * as fs from 'fs';
import * as path from 'path';

let chatPanel: vscode.WebviewPanel | undefined;
let chatView: vscode.WebviewView | undefined;
let activeRun: ChildProcess | undefined;
let outputChannel: vscode.OutputChannel | undefined;
let extensionContext: vscode.ExtensionContext | undefined;

export function activate(context: vscode.ExtensionContext): void {
    extensionContext = context;
    outputChannel = vscode.window.createOutputChannel('Bog Agents');
    context.subscriptions.push(outputChannel);
    outputChannel.appendLine('Bog Agents extension activated');

    context.subscriptions.push(
        vscode.window.registerWebviewViewProvider('bog-agents.chatView', new ChatViewProvider(context), {
            webviewOptions: { retainContextWhenHidden: true },
        }),
        vscode.commands.registerCommand('bog-agents.openChat', () => openChat(context)),
        vscode.commands.registerCommand('bog-agents.reviewSelection', () => handleSelection('review')),
        vscode.commands.registerCommand('bog-agents.explainSelection', () => handleSelection('explain')),
        vscode.commands.registerCommand('bog-agents.fixSelection', () => handleSelection('fix')),
        vscode.commands.registerCommand('bog-agents.runDoctor', () => runDoctor()),
    );
}

export function deactivate(): void {
    if (activeRun) {
        activeRun.kill();
        activeRun = undefined;
    }
    if (chatPanel) {
        chatPanel.dispose();
        chatPanel = undefined;
    }
    chatView = undefined;
    extensionContext = undefined;
}

/**
 * Sidebar chat, i.e. the `bog-agents.chatView` contribution in package.json.
 * Shares the HTML, the message protocol and the CLI runner with the editor
 * panel, so a reply reaches whichever surface is open (or both).
 */
class ChatViewProvider implements vscode.WebviewViewProvider {
    constructor(private readonly context: vscode.ExtensionContext) {}

    resolveWebviewView(view: vscode.WebviewView): void {
        chatView = view;
        view.webview.options = {
            enableScripts: true,
            localResourceRoots: [vscode.Uri.joinPath(this.context.extensionUri, 'resources')],
        };
        view.webview.html = getChatHtml(view.webview);
        view.webview.onDidReceiveMessage(handleWebviewMessage, undefined, this.context.subscriptions);
        view.onDidDispose(() => {
            if (chatView === view) {
                chatView = undefined;
            }
        });
    }
}

/** Messages the webviews post back to the extension host. */
async function handleWebviewMessage(message: { type?: unknown; text?: unknown }): Promise<void> {
    if (typeof message?.type !== 'string') {
        return;
    }
    if (message.type === 'send' && typeof message.text === 'string') {
        await sendToCli(message.text);
    }
}

type ChatEvent =
    | { type: 'start'; text: string }
    | { type: 'response'; text: string }
    | { type: 'done'; code: number | null };

/** Post one event to every open chat surface. */
function postToChat(event: ChatEvent): void {
    chatPanel?.webview.postMessage(event);
    chatView?.webview.postMessage(event);
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
 * `bog-agents.autoApprove` maps onto the CLI's `--auto-approve` flag. A
 * `--print` run cannot answer approval prompts, so without it a tool call that
 * needs approval ends the turn; with it the CLI runs the tool.
 */
function getAutoApprove(): boolean {
    const config = vscode.workspace.getConfiguration('bog-agents');
    return config.get<boolean>('autoApprove', false);
}

/**
 * Build a minimal env for the CLI subprocess, deliberately stripping
 * variables an untrusted workspace could plant via dotenv-style auto-loaders
 * in the parent VS Code process.
 *
 * Provider credentials are explicitly allowlisted so the documented env-var
 * auth path (e.g. `ANTHROPIC_API_KEY`, see README "Requirements") reaches the
 * CLI child. The list mirrors the CLI's `PROVIDER_API_KEY_ENV` registry in
 * `libs/cli/bog_agents_cli/model_config.py` plus the AWS credential-chain and
 * Google application-credential companions the CLI actually reads — keep the
 * two in sync when adding a provider.
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
        // Provider API keys (values of PROVIDER_API_KEY_ENV in the CLI).
        'ANTHROPIC_API_KEY',
        'AZURE_OPENAI_API_KEY',
        'BASETEN_API_KEY',
        'COHERE_API_KEY',
        'DEEPSEEK_API_KEY',
        'FIREWORKS_API_KEY',
        'GOOGLE_API_KEY',
        'GOOGLE_CLOUD_PROJECT',
        'GROQ_API_KEY',
        'HUGGINGFACEHUB_API_TOKEN',
        'LITELLM_API_KEY',
        'MISTRAL_API_KEY',
        'NVIDIA_API_KEY',
        'OPENAI_API_KEY',
        'OPENROUTER_API_KEY',
        'PERPLEXITY_API_KEY', // alias the CLI promotes to PPLX_API_KEY
        'PPLX_API_KEY',
        'TOGETHER_API_KEY',
        'WATSONX_APIKEY',
        'XAI_API_KEY',
        // AWS credential chain (Bedrock).
        'AWS_ACCESS_KEY_ID',
        'AWS_SECRET_ACCESS_KEY',
        'AWS_SESSION_TOKEN',
        'AWS_REGION',
        'AWS_DEFAULT_REGION',
        'AWS_PROFILE',
        'AWS_WEB_IDENTITY_TOKEN_FILE',
        'AWS_BEARER_TOKEN_BEDROCK',
        // Google ADC service-account path (Vertex AI).
        'GOOGLE_APPLICATION_CREDENTIALS',
        // Local-provider endpoint (Ollama).
        'OLLAMA_HOST',
        // Corporate proxy / TLS interception (v6 DEL-4): without these the
        // child CLI cannot reach any provider from a proxied network while
        // the same CLI works from the user's terminal.
        'HTTP_PROXY',
        'HTTPS_PROXY',
        'NO_PROXY',
        'ALL_PROXY',
        'http_proxy',
        'https_proxy',
        'no_proxy',
        'all_proxy',
        'SSL_CERT_FILE',
        'SSL_CERT_DIR',
        'REQUESTS_CA_BUNDLE',
        'CURL_CA_BUNDLE',
        'NODE_EXTRA_CA_CERTS',
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

    chatPanel.webview.onDidReceiveMessage(handleWebviewMessage, undefined, context.subscriptions);

    chatPanel.onDidDispose(() => {
        chatPanel = undefined;
    });
}

/** Make sure a reply has somewhere to land before a context-menu action runs. */
function ensureChatSurface(): void {
    if (chatPanel || chatView) {
        return;
    }
    if (extensionContext) {
        openChat(extensionContext);
    }
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
    ensureChatSurface();
    postToChat({ type: 'start', text: `${action}: ${path.basename(filePath)}:${startLine}-${endLine}` });
    await sendToCli(prompt, { announced: true });
}

async function sendToCli(prompt: string, options: { announced?: boolean } = {}): Promise<void> {
    const cliPath = getCliPath();
    if (cliPath === null) {
        return;
    }
    if (activeRun) {
        vscode.window.showWarningMessage('Bog Agents is still answering the previous prompt.');
        return;
    }
    const model = getModel();
    const workspaceFolder = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;

    try {
        const args = ['--model', model, '--print', prompt, '--no-stream'];
        if (getAutoApprove()) {
            args.push('--auto-approve');
        }
        const proc = spawn(cliPath, args, {
            cwd: workspaceFolder,
            env: buildChildEnv(),
            // shell: false (default) — never let the CLI path go through a shell.
        });
        activeRun = proc;
        if (!options.announced) {
            // The webview already rendered the user's bubble; open the reply bubble.
            postToChat({ type: 'start', text: '' });
        }

        let output = '';
        proc.stdout?.on('data', (data: Buffer) => {
            output += data.toString();
            postToChat({ type: 'response', text: output });
        });

        proc.stderr?.on('data', (data: Buffer) => {
            outputChannel?.appendLine(`[CLI stderr] ${data.toString().trimEnd()}`);
        });

        proc.on('error', (err: NodeJS.ErrnoException) => {
            if (activeRun === proc) {
                activeRun = undefined;
            }
            postToChat({ type: 'done', code: null });
            if (err.code === 'ENOENT') {
                vscode.window.showErrorMessage(
                    'bog-agents CLI not found. Install with: pip install bog-agents-cli',
                );
            } else {
                vscode.window.showErrorMessage(`Failed to run Bog Agents CLI: ${err.message}`);
            }
        });

        proc.on('close', (code: number | null) => {
            if (activeRun === proc) {
                activeRun = undefined;
            }
            postToChat({ type: 'done', code });
            if (code !== 0) {
                vscode.window.showErrorMessage(`Bog Agents CLI exited with code ${code}`);
            }
        });
    } catch (error) {
        activeRun = undefined;
        postToChat({ type: 'done', code: null });
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

        function streamingBubble() {
            const aiMessages = messages.querySelectorAll('.ai-message');
            const last = aiMessages[aiMessages.length - 1];
            return last && last.dataset.streaming === 'true' ? last : null;
        }

        function openBubble() {
            const div = document.createElement('div');
            div.className = 'message ai-message';
            div.dataset.streaming = 'true';
            div.textContent = '…';
            messages.appendChild(div);
            return div;
        }

        window.addEventListener('message', (event) => {
            const msg = event.data;
            if (!msg || typeof msg !== 'object') return;
            if (msg.type === 'start') {
                // A context-menu action carries a label for the user's side.
                if (typeof msg.text === 'string' && msg.text) addMessage(msg.text, true);
                openBubble();
            } else if (msg.type === 'response') {
                const text = typeof msg.text === 'string' ? msg.text.slice(0, MAX_MSG_LEN) : '';
                const bubble = streamingBubble() || openBubble();
                bubble.textContent = text;
            } else if (msg.type === 'done') {
                // Close the bubble so the next reply gets its own instead of
                // overwriting this one (the pre-v6 bug).
                const bubble = streamingBubble();
                if (bubble) {
                    if (bubble.textContent === '…') bubble.textContent = '(no output)';
                    delete bubble.dataset.streaming;
                }
            } else {
                return;
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
