# Daemon Quickstart

> The patient watcher. Wakes itself. Reports back. Goes the distance.

## When you want this

`bog-agents-cli` is great when you're at the keyboard. The daemon is
the right tool when you want an agent that watches *for* you — and
reports back when something matters.

Use the daemon for:

- A nightly summary of yesterday's PRs landed in Slack at 9 AM.
- A webhook from your CI that triggers an agent to investigate test
  failures.
- A file watcher on `~/Downloads/` that classifies new files into
  the right folder.
- A git push hook that runs a security review on every merge to
  main.

Use `/peat` (in the CLI) instead when:

- You want the jobs to share context with your interactive sessions.
- You want zero-ops (no service install).
- You're OK with the jobs only running while the CLI is open.

## Install

```bash
pip install bog-agents-daemon
pip install 'bog-agents-daemon[anthropic]'      # add a provider
pip install 'bog-agents-daemon[all-providers]'  # everything
```

You also need at least one provider API key in the environment
(same vars as the CLI: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, etc.).

## First run

Start the daemon in the foreground for a quick test:

```bash
bog-agents-daemon run --port 7878
```

You'll see:

```
[info] bog-agents-daemon listening on http://127.0.0.1:7878
[info] scheduler tick interval: 30s
[info] loaded 0 jobs from ~/.bog-agents/daemon/jobs.json
```

In another terminal, add a job:

```bash
bog-agents daemon jobs create \
  --name morning-brief \
  --cron "0 9 * * 1-5" \
  --prompt "Summarize what changed in this repo since yesterday." \
  --output slack \
  --output-slack "$SLACK_WEBHOOK_URL"
```

The job persists to `~/.bog-agents/daemon/jobs.json` (all jobs share one file).
The scheduler picks it up on the next tick.

To run it once immediately (without waiting for the cron tick):

```bash
bog-agents daemon jobs run morning-brief
```

## Triggers

| Trigger | What it does | Example |
|---|---|---|
| `cron` | Standard cron expression | `"0 9 * * 1-5"` (9 AM every weekday) |
| `interval` | Every N seconds | `"60"` (every minute) |
| `file_change` | Watch a path or glob | `"~/Downloads/*"` |
| `webhook` | Inbound HTTP POST | URL: `http://localhost:7878/webhooks/<webhook-path>` (from `--webhook-path`); HMAC via `X-Hub-Signature-256` |
| `git_push` | Receive a git push hook | Set up your remote's post-receive hook to POST to the daemon |

A job can have multiple triggers. They fire independently.

## Outputs

| Target | Format | Notes |
|---|---|---|
| `log` | Daemon's own log file | `~/.bog-agents/daemon/logs/<job-name>.log` |
| `stdout` | Daemon's stdout | Useful when running under systemd |
| `file:./path.md` | Write to a file | Each run appends or overwrites depending on `mode` |
| `slack:#channel` | Slack via webhook | Needs `SLACK_WEBHOOK_URL` env var |
| `webhook:https://...` | Outbound HTTP POST | Optionally HMAC-SHA256 signed |
| `email:to@example.com` | SMTP | Needs `SMTP_HOST` env vars |
| `github_comment:owner/repo#42` | GitHub PR / issue comment | Needs `GITHUB_TOKEN` |

A job can have multiple output targets. Each run dispatches to all
of them. Per-target failures are captured on `JobRun.dispatch_errors`
so you can see from the run record that delivery failed even when the
agent itself completed.

## Deploy as a service

### systemd (Linux)

`/etc/systemd/system/bog-agents-daemon.service`:

```ini
[Unit]
Description=Bog Agents daemon
After=network.target

[Service]
Type=simple
User=bog-agents
Group=bog-agents
EnvironmentFile=/etc/bog-agents/daemon.env
ExecStart=/usr/local/bin/bog-agents-daemon run --port 7878
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now bog-agents-daemon
sudo journalctl -u bog-agents-daemon -f    # tail the logs
```

### launchd (macOS)

`~/Library/LaunchAgents/com.bogware.bog-agents-daemon.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.bogware.bog-agents-daemon</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/local/bin/bog-agents-daemon</string>
        <string>run</string>
        <string>--port</string>
        <string>7878</string>
    </array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>EnvironmentVariables</key>
    <dict>
        <key>ANTHROPIC_API_KEY</key><string>sk-ant-...</string>
    </dict>
</dict>
</plist>
```

```bash
launchctl load ~/Library/LaunchAgents/com.bogware.bog-agents-daemon.plist
launchctl list | grep bog-agents
```

### Windows Task Scheduler

```powershell
$action = New-ScheduledTaskAction -Execute "bog-agents-daemon.exe" -Argument "run --port 7878"
$trigger = New-ScheduledTaskTrigger -AtStartup
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERNAME" -LogonType S4U -RunLevel Highest
Register-ScheduledTask -TaskName "BogAgentsDaemon" -Action $action -Trigger $trigger -Principal $principal
```

For Windows the daemon needs API keys in the user's environment
(System Properties → Environment Variables).

## Configuration

Per-daemon settings live in `~/.bog-agents/daemon/config.toml`:

```toml
[scheduler]
tick_seconds = 30                # check for due jobs every N seconds

[api]
token = "your-shared-secret"     # REST API auth token
bind = "127.0.0.1"               # change to 0.0.0.0 for remote access (with token!)
port = 7878

[outputs.slack]
default_webhook = "https://hooks.slack.com/services/..."

[outputs.email]
smtp_host = "smtp.example.com"
smtp_port = 587
from = "agent@example.com"
```

All job config lives in a single JSON file at
`~/.bog-agents/daemon/jobs.json`. Prefer `bog-agents daemon jobs edit <name>`
over hand-editing, since the file also holds secret values.

## REST API

The daemon exposes a JSON API on the bind address:

```bash
# List all jobs
curl -H "Authorization: Bearer $TOKEN" http://localhost:7878/jobs

# Get a specific job (by job id)
curl -H "Authorization: Bearer $TOKEN" http://localhost:7878/jobs/<job-id>

# Trigger a job
curl -X POST -H "Authorization: Bearer $TOKEN" http://localhost:7878/jobs/<job-id>/run

# List recent runs
curl -H "Authorization: Bearer $TOKEN" "http://localhost:7878/runs?limit=10"

# Inbound webhook (no bearer token — authenticated by HMAC signature)
curl -X POST -H "X-Hub-Signature-256: sha256=..." -d '{...}' \
  http://localhost:7878/webhooks/<webhook-path>
```

Full API at [`daemon/api.md`](api.md) (todo: write this).

## CLI reference

**Server lifecycle** — the `bog-agents-daemon` binary:

```bash
bog-agents-daemon run                        # foreground server (alias of `start`)
bog-agents-daemon stop                       # graceful shutdown
bog-agents-daemon status                     # is the daemon running?
```

**Job management** — the `bog-agents` CLI (needs `bog-agents-cli` installed) or
the daemon's HTTP API:

```bash
bog-agents daemon jobs create ...            # see flags below
bog-agents daemon jobs list
bog-agents daemon jobs show <name>
bog-agents daemon jobs enable <name>
bog-agents daemon jobs disable <name>
bog-agents daemon jobs delete <name>
bog-agents daemon jobs run <name>            # one-off invocation
```

Run history is exposed by the daemon's HTTP API (`GET /runs`,
`GET /jobs/{id}/runs`), not a `runs` subcommand.

Full flags: `bog-agents-daemon --help` and
`bog-agents daemon jobs create --help`.

## Observability

- **Structured logs.** Every state transition (job created, trigger
  fired, agent started, agent completed, dispatch sent) lands as a
  log line with stable event names prefixed `evt_*`. Ship them to
  Splunk / Loki / journald.
- **Run records.** Every agent invocation produces a JobRun with the
  prompt, output, status, started/finished timestamps, trigger
  context, and per-target dispatch errors. Records persist to
  `~/.bog-agents/daemon/runs/`.
- **Crash dumps.** Same `_panic.py` mechanism as the CLI. Files at
  `~/.bog-agents/crash/<ts>.log` with secrets redacted.

## Common patterns

### Pattern: nightly summary

```bash
bog-agents daemon jobs create \
  --name nightly-summary \
  --cron "0 22 * * *" \
  --prompt "Summarize what changed in this repo today. Surface anything risky." \
  --working-dir /work/myrepo \
  --output file \
  --output-file "./summaries/{date}.md"
```

### Pattern: webhook investigator

```bash
bog-agents daemon jobs create \
  --name ci-failure-investigator \
  --webhook-path /hooks/ci-failure-investigator \
  --prompt "A CI run failed. Context:\n{trigger_context_json}\n\nInvestigate and propose a fix." \
  --working-dir /work/myrepo \
  --output github_comment \
  --output-github-repo bogware/bog-agents \
  --output-github-issue "{pr_number}"
```

Trigger:

```bash
curl -X POST http://daemon.internal:7878/webhooks/hooks/ci-failure-investigator \
  -H "X-Hub-Signature-256: sha256=$(echo -n "$BODY" | openssl dgst -sha256 -hmac "$SECRET" | awk '{print $2}')" \
  -d '{"pr_number": 42, "build_url": "..."}'
```

### Pattern: file watcher

```bash
bog-agents daemon jobs create \
  --name downloads-classifier \
  --watch-dir ~/Downloads --watch-pattern "*" \
  --prompt "Classify the new file at {trigger_path} into one of: invoices/, receipts/, screenshots/, misc/. Move it." \
  --working-dir ~/
```

### Prompt and output placeholders

Prompts, `--output-file` paths and `--output-github-issue` may use
`{placeholder}` references that the daemon renders from the run's trigger just
before the agent is invoked (unknown names are left verbatim, so quoted JSON is
safe):

| Placeholder | Value |
|---|---|
| `{date}` / `{time}` / `{datetime}` | Local date, time, ISO timestamp of the run |
| `{job_name}` / `{job_id}` / `{working_dir}` | The job's own fields |
| `{trigger_type}` | `cron`, `interval`, `file_change`, `webhook`, `git_push`, `github`, `manual` |
| `{trigger_context_json}` | The whole trigger context as JSON (webhook payload, git-push ref/sha, GitHub event) |
| `{trigger_path}` | The path a file-change trigger fired on |
| `{number}` / `{pr_number}` / `{issue_number}` | The issue or PR number from a GitHub event or a webhook payload's `number` / `pr_number` |
| any top-level key of the trigger context | e.g. `{title}`, `{body}`, `{branch}`, `{actor}` for GitHub events; `{ref}`, `{new_sha}` for git-push |

## Where things live

```
~/.bog-agents/daemon/
├── config.toml          # global daemon config
├── jobs.json            # all job definitions (secrets stored here, owner-only)
├── runs/                # one JSON per invocation
│   ├── 2026-05-17T09:00:00Z.morning-brief.json
│   └── ...
├── logs/
│   └── scheduler.log
└── state.db             # SQLite for trigger fingerprints + dedup
```

## Next steps

- [Triggers + outputs](triggers-and-outputs.md) — the full
  configuration reference for every trigger type and output target
- [Deploy](deploy.md) — production deployment patterns
- [API](api.md) — REST surface

---

*The bog doesn't sleep. Neither does the daemon.*
