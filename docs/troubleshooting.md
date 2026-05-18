# Troubleshooting

> First read this. Then check `~/.bog-agents/crash/<ts>.log` if there
> is one. Then run `bog-agents --doctor-deep`. Most problems surface in
> one of those three places.

## Startup problems

### "No AI provider credentials detected and stdin is not a TTY"

You ran a non-interactive flag (`-p`, `-n`, piped stdin, daemon, CI)
without a provider API key in the environment. The error names the
env vars to set:

```bash
export ANTHROPIC_API_KEY=sk-ant-...    # Claude
export OPENAI_API_KEY=sk-...           # GPT
export GOOGLE_API_KEY=AI...            # Google AI
```

If you want the interactive setup wizard instead, run `bog-agents`
with no args from a real terminal.

### `ModuleNotFoundError: No module named 'langchain_xxx'`

You're trying to use a provider whose package isn't installed. Install
the matching extra:

```bash
pip install 'bog-agents-cli[anthropic]'        # Claude
pip install 'bog-agents-cli[openai]'           # GPT
pip install 'bog-agents-cli[bedrock]'          # AWS Bedrock
pip install 'bog-agents-cli[google-genai]'     # Gemini
pip install 'bog-agents-cli[all-providers]'    # everything
```

### "Missing required CLI dependencies!"

A `pip install` was interrupted partway through. Recover:

```bash
pip install --upgrade --force-reinstall bog-agents-cli
```

### The TUI shows `[<35;57;14M[` garbage in the input box

A previous bog-agents process exited abnormally and left the terminal
in mouse-tracking mode. Two fixes:

```bash
reset                    # reset the terminal (POSIX)
printf '\033[?1003l'     # disable mouse tracking specifically
```

Or just close the terminal and open a new one. The CLI installs an
atexit handler that prevents this, but a hard kill can still leak.

### `bog-agents --version` is slow

It shouldn't be — the version check is a fast path that skips heavy
imports. If it's slow, something in your environment is fighting the
import sort:

```bash
PYTHONDONTWRITEBYTECODE=1 bog-agents --version    # skip .pyc writes
```

If still slow, attach `bog-agents --doctor-deep` output to a bug
report.

## First-run problems

### Provider setup wizard hangs

You're not in a TTY (Docker without `-it`, piped stdin, CI). Fixed in
0.8.7 — the wizard now refuses with an actionable error in non-TTY
contexts. If you're on an older version, upgrade.

### "AWS credentials not configured"

The Bedrock provider needs working AWS credentials. The fastest path:

```bash
aws configure              # or: export AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=...
aws sts get-caller-identity   # confirms credentials work
```

The CLI uses boto3's credential chain (env vars, profile, SSO,
instance role). Anything boto3 finds, bog-agents finds.

### "MCP server `foo` did not start in 15s — disabled"

A stdio MCP server (typically `npx -y something`) didn't initialize
in time. The other servers in your config will still work; the slow
one is disabled for this session.

Causes:

- Cold npm cache (first run downloads the package).
- The server is blocked on OAuth and waiting for a browser callback.
- The package crashes on startup.

Bump the timeout for a one-off:

```bash
BOG_AGENTS_MCP_STARTUP_TIMEOUT=60 bog-agents
```

Find the actual error in `~/.bog-agents/logs/cli.log` (search for the
server name).

## Mid-session problems

### Agent says "I don't have permission to do X"

The agent is sandboxed. By default:

- Filesystem reads / writes are confined to the working directory.
- Path traversal (`../../etc/passwd`) is blocked.
- Symlinks pointing outside the working directory are refused.

Opt out (only when you have a reason):

```bash
BOG_AGENTS_FS_UNSANDBOXED=1 bog-agents
```

For shell commands, the agent's allow-list is in `--shell-allow-list`:

```bash
bog-agents --shell-allow-list recommended
bog-agents --shell-allow-list "git,npm,pytest,uv"
bog-agents --shell-allow-list all          # disables the gate
```

### Agent hangs on a long tool call

Two timeouts at play:

- **Per-tool**: shell commands hit a 60s default. Set
  `BOG_AGENTS_SHELL_TIMEOUT_SECONDS` to extend.
- **Per-chunk**: streaming chunks from the model hit a 600s default.
  Set `BOG_AGENTS_STREAM_CHUNK_TIMEOUT_SECONDS` (=0 disables).

When the chunk timeout fires you'll see in the log:

```
Agent stream stalled — no chunk received in 605s (timeout 600s).
The remote may be unreachable or the model may be hung.
```

If your model genuinely needs more than 600s between chunks, raise
the env var. If chunks should arrive faster, you've got a network
problem.

### "Cost exceeded $X — continue?" but you set no budget

You're in `/expert` mode and a rule with `require_approval` on cost
just fired. Either:

- Approve once (the rule re-fires next turn).
- `/expert` to disable the engine temporarily.
- Edit `.bog-agents/expert_rules/*.yaml` to raise the threshold.

### Approval menu won't take input

The approval menu owns the keyboard while it's up. Use `y` (approve),
`n` (deny), `a` (approve and auto-approve this tool for the session),
or arrow keys + Enter.

If nothing responds, your terminal might be sending arrow keys as
escape sequences the TUI doesn't recognize. Try the digit keys:
`1` (approve), `2` (auto), `3` (deny).

### Mid-session the agent forgets what we discussed

Context window filled and `SummarizationMiddleware` compressed
older messages. That's by design. The summary preserves the gist
but the literal text of older messages is gone.

To force-compress before the auto-summarizer kicks in:

```text
/compact
```

To see what's about to get compressed (and stop it):

```text
/threads
```

Each thread is checkpointed; `/resume <thread-id>` brings you back to
any of them. Switching threads frees the current context entirely.

## Tool-specific problems

### Git tools fail with "refusing branch name `--foo`"

Wave X security fix. Branch names that look like flags are rejected
before they reach `git`. If you genuinely have a branch named
`--foo`, rename it. (You don't; some script generated that name.)

### `start_preview_server` refuses your command

```
Error: refusing to start preview server with shell metacharacters in command.
Use the shell execute tool instead for piped or redirected commands.
```

The preview-server tool spawns one process. Pipes (`|`), redirects
(`>`, `<`), backticks, command substitution (`$(...)`), and chains
(`;`, `&`) belong in the shell `execute` tool, not in a tool whose job
is "start a dev server."

### MCP tool returns garbage after auth

The OAuth token cached in `~/.bog-agents/oauth/tokens.json` expired.
Look at the structured log lines:

```
oauth: stored token for server=jira expired (exp=..., now=...) but
refresh_token available; caller should refresh before use
```

If a refresh isn't happening, re-run the auth flow:

```text
/mcp re-auth jira
```

If the refresh succeeded but the server still 401s, the server has
revoked the token on its end. Re-auth.

## SDK-side problems

### `bind_tools` raises NotImplementedError on a custom model

You're subclassing `BaseChatModel` and didn't override `bind_tools`.
The agent factory calls `bind_tools` during graph construction. For
response-only fakes:

```python
def bind_tools(self, tools, *, tool_choice=None, **kwargs):
    return self   # we don't actually use tools — no-op
```

For real models, `bind_tools` should attach the tool list to the
request. See `bog_agents.drive.replay_model` for the conventional
implementations of fake / replay / record wrappers.

### `ImportError: cannot import name 'FinancialDataMiddleware'`

Wave V removed 17 stub middleware modules. They returned fake data
in production (e.g. `fetch_quote → price=0.0`) and have been deleted.
Migration: remove the `enable_<name>=True` calls; there is no
replacement. See [CHANGELOG.md](../CHANGELOG.md) 0.8.5.

### `bog_agents_cli.replay.build_replay_prompt` is gone

Removed in Wave W (0.8.6) — it was unwired (never called by
production). `/replay run` now drives each recorded user message
through the agent individually. If you were calling it programmatically,
use the drive runner instead: `bog_agents_cli.drive.run_script_path`.

## Daemon-side problems

### Jobs persist but never fire

The scheduler tick is configurable via `--tick-seconds N` (default
30). Anything finer needs `interval` triggers, not `cron`.

Check the daemon is actually running:

```bash
bog-agents-daemon status
```

If `status` shows the daemon as running but jobs don't fire, look at
`~/.bog-agents/daemon/scheduler.log`. The most common cause is a
malformed cron expression — the daemon logs the parse error and
disables the trigger.

### Output target says "delivered" but Slack/email never arrives

Look at the run record:

```bash
bog-agents-daemon runs show <run-id>
```

Per-target failures land in `JobRun.dispatch_errors` (Wave X) so you
can tell from the runs table that delivery failed even when the
agent itself completed. If `dispatch_errors` is empty and the target
shows successful, the failure is downstream of the daemon — check
Slack's webhook logs / your SMTP relay.

### `JobRun.dispatch_errors` shows `(overflow)`

You had more than 20 output targets fail in one run. The cap (Wave Y)
truncates to keep the JSON bounded. Reduce the fanout or fix the
underlying outage.

## Reporting bugs

When you hit a real bug, attach two things:

```bash
~/.bog-agents/crash/<ts>.log    # if there is one
bog-agents --doctor-deep        # one-page health summary
```

Both have secrets redacted by `_panic.py` patterns (sk-…, xoxb-…,
ghp_…, JWTs, AKIA, generic `api[_-]?key=`). Read what's in the file
before sending if you want to double-check.

File at <https://github.com/bogware/bog-agents/issues>.

---

*A bog rarely surprises you twice in the same place. Mark where you
slipped. Walk a different line.*
