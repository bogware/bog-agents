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
[info] loaded 0 jobs from ~/.bog-agents/daemon/jobs/
```

In another terminal, add a job:

```bash
bog-agents-daemon job add \
  --name morning-brief \
  --cron "0 9 * * 1-5" \
  --prompt "Summarize what changed in this repo since yesterday." \
  --output slack:#engineering
```

The job persists to `~/.bog-agents/daemon/jobs/morning-brief.yaml`.
The scheduler picks it up on the next tick.

To run it once immediately (without waiting for the cron tick):

```bash
bog-agents-daemon job run morning-brief
```

## Triggers

| Trigger | What it does | Example |
|---|---|---|
| `cron` | Standard cron expression | `"0 9 * * 1-5"` (9 AM every weekday) |
| `interval` | Every N seconds | `"60"` (every minute) |
| `file_change` | Watch a path or glob | `"~/Downloads/*"` |
| `webhook` | Inbound HTTP POST | URL: `http://localhost:7878/webhook/<job-name>` |
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

Per-job config lives in the job's YAML file at
`~/.bog-agents/daemon/jobs/<name>.yaml`. Hand-edit freely.

## REST API

The daemon exposes a JSON API on the bind address:

```bash
# List all jobs
curl -H "Authorization: Bearer $TOKEN" http://localhost:7878/api/jobs

# Get a specific job
curl -H "Authorization: Bearer $TOKEN" http://localhost:7878/api/jobs/morning-brief

# Trigger a job
curl -X POST -H "Authorization: Bearer $TOKEN" http://localhost:7878/api/jobs/morning-brief/run

# List recent runs
curl -H "Authorization: Bearer $TOKEN" http://localhost:7878/api/runs?job=morning-brief&limit=10

# Inbound webhook (no auth required — uses HMAC signature)
curl -X POST -H "X-Bog-Signature: sha256=..." -d '{...}' \
  http://localhost:7878/webhook/morning-brief
```

Full API at [`daemon/api.md`](api.md) (todo: write this).

## CLI reference

```bash
bog-agents-daemon run                       # foreground server
bog-agents-daemon status                    # is the daemon running?

bog-agents-daemon job add ...               # see flags below
bog-agents-daemon job list
bog-agents-daemon job show <name>
bog-agents-daemon job enable <name>
bog-agents-daemon job disable <name>
bog-agents-daemon job delete <name>
bog-agents-daemon job run <name>            # one-off invocation

bog-agents-daemon runs list                 # recent run history
bog-agents-daemon runs show <run-id>        # full run record
bog-agents-daemon runs prune --older-than 30d
```

Full flags: `bog-agents-daemon --help` and
`bog-agents-daemon job add --help`.

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
bog-agents-daemon job add \
  --name nightly-summary \
  --cron "0 22 * * *" \
  --prompt "Summarize what changed in this repo today. Surface anything risky." \
  --working-dir /work/myrepo \
  --output "slack:#engineering" \
  --output "file:./summaries/{date}.md"
```

### Pattern: webhook investigator

```bash
bog-agents-daemon job add \
  --name ci-failure-investigator \
  --webhook ci-failure-investigator \
  --prompt "A CI run failed. Context:\n{trigger_context_json}\n\nInvestigate and propose a fix." \
  --working-dir /work/myrepo \
  --output "github_comment:bogware/bog-agents#{pr_number}"
```

Trigger:

```bash
curl -X POST http://daemon.internal:7878/webhook/ci-failure-investigator \
  -H "X-Bog-Signature: $(echo -n "$BODY" | openssl dgst -sha256 -hmac "$SECRET" | awk '{print $2}')" \
  -d '{"pr_number": 42, "build_url": "..."}'
```

### Pattern: file watcher

```bash
bog-agents-daemon job add \
  --name downloads-classifier \
  --file-change "~/Downloads/*" \
  --prompt "Classify the new file at {trigger_path} into one of: invoices/, receipts/, screenshots/, misc/. Move it." \
  --working-dir ~/
```

## Where things live

```
~/.bog-agents/daemon/
├── config.toml          # global daemon config
├── jobs/                # one YAML per job
│   ├── morning-brief.yaml
│   └── ci-investigator.yaml
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
