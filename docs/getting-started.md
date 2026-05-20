# Getting Started

> Five minutes from `pip install` to a working agent. Twenty minutes
> to a setup you'll be happy to live with.

## Install

```bash
pipx install bog-agents-cli       # recommended — isolated, clean PATH
```

Or `uv tool install bog-agents-cli` if you have uv. Or plain pip.

You'll also want at least one model provider:

```bash
pip install 'bog-agents-cli[anthropic]'        # Claude
pip install 'bog-agents-cli[openai]'           # GPT
pip install 'bog-agents-cli[all-providers]'    # one-and-done
```

## Set a key

The CLI looks for any of these in the environment:

```bash
export ANTHROPIC_API_KEY=sk-ant-...   # Claude
export OPENAI_API_KEY=sk-...          # GPT
export GOOGLE_API_KEY=AI...           # Gemini
# or: AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY (Bedrock)
# or: GROQ_API_KEY, MISTRAL_API_KEY, DEEPSEEK_API_KEY, ...
```

For Bedrock — the AWS-hosted path — see the dedicated walkthrough at
**[providers/bedrock.md](providers/bedrock.md)**. Inference profiles,
model access, the six-step probe.

Running with no key launches an interactive setup wizard. Running in
a non-TTY context (CI, daemon, piped stdin) with no key gives you an
actionable error pointing at the env vars above — no hangs.

## Verify

```bash
bog-agents --doctor-deep
```

One-page health summary in under a second: Python, config dirs, git,
provider keys, network reachability, MCP config, recent crash dumps.
Green across the board means you're set.

## First run

```bash
bog-agents
```

Drops you into the TUI. Type a question, press Enter. The agent has
filesystem, shell, git, and code-edit tools out of the box.

Try:

```text
list the python files in this repo and tell me which one is biggest
```

You'll see the agent call `list_files`, then `read_file` once or
twice, then print its answer. The tools it touched are logged inline
with their arguments — nothing's hidden.

## The five commands you'll actually use

| | |
|---|---|
| `/help` | The full slash command reference |
| `/model` | Switch model mid-session |
| `/diff` | Show every file the agent touched since session start |
| `/compact` | Summarize and prune the current thread (free up context) |
| `/quit` | Leave (or Ctrl+D) |

Type `/` and a fuzzy menu shows you everything else. There are 120+
commands but you'll learn the ones that matter through normal use —
no need to memorize them up front.

## One-shot mode

When you don't want the TUI:

```bash
bog-agents -p "explain what this module does" < src/agent.py
```

`-p` (print) gives you clean stdout — pipeable, scriptable, no
chrome. `-n` (non-interactive) is similar but keeps the diagnostic
output on stderr.

## Scripted mode

When you want the same TUI session reproduced reliably, `bog-agents
drive` runs a YAML script through the actual TUI surface:

```yaml
# smoke.yaml
session:
  model: fake:hello from the bog
  approval_mode: auto-all
steps:
  - "/help"
  - wait_for_idle: 5
  - expect_transcript_contains: "(?i)help|usage"
```

```bash
bog-agents --drive smoke.yaml
```

See [docs/cli/drive.md](cli/drive.md) for the full grammar. Useful in
CI when you want to know your agent surface still works after an
upgrade.

## Where things live

```
~/.bog-agents/
├── .env                  # API keys (created by the setup wizard)
├── settings.json         # User-global config
├── threads/              # Persistent conversation history
├── crash/                # Panic dumps (attach when filing a bug)
├── replays/              # /record output
└── prompt_library.toml   # Saved prompts for --prompt NAME
```

And per-project:

```
<repo>/.bog-agents/
├── settings.json         # Project-local config (overrides user-global)
├── AGENTS.md             # Auto-injected memory the agent reads on every turn
├── skills/               # Custom agent skills
├── expert_rules/         # YAML rule files for /expert
├── pipelines/            # Saved multi-step prompts for --pipeline NAME
├── qa-plans/             # Plans from /qa
└── jobs/                 # /peat scheduled jobs (when used per-project)
```

You don't need to create any of these by hand. The agent creates what
it needs on first use.

## Sensible defaults you get for free

- **Filesystem is sandboxed.** The agent is confined to the working
  directory unless you set `BOG_AGENTS_FS_UNSANDBOXED=1`. Path
  traversal blocked. Symlink escapes blocked.
- **Shell commands time out.** Hung commands get killed after 60
  seconds (configurable). Interactive prompts that expect stdin get
  EOF immediately so the agent can't be tricked into hanging forever.
- **Tools that mutate state require approval.** File writes, edits,
  shell commands, URL fetches all pop an approval dialog by default.
  `--auto-approve` opts out; `--auto` is a smarter middle ground.
- **Secrets stay in memory.** The vault keeps API keys, OAuth tokens,
  and `/qa` secrets in process memory only. Nothing serializes to
  disk except your `~/.bog-agents/.env` for the bootstrap.
- **Crashes leave a redacted dump.** `~/.bog-agents/crash/<ts>.log`
  with secrets already stripped. Attach when you file a bug.

## Next steps

- **Reading order: [Cookbook](cookbook.md)** — fifteen real recipes.
- If you're going to embed an agent in Python, read [SDK
  Quickstart](sdk/quickstart.md).
- If you want an agent that wakes itself on cron / file change /
  webhook, read [Daemon Quickstart](daemon/quickstart.md).
- If something didn't work, [Troubleshooting](troubleshooting.md)
  covers every common failure mode by error message.

---

*The bog is calm. So is the agent. Take your time.*
