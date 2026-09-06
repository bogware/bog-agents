# Bog Agents Daemon

> *The patient watcher. Wakes itself. Pass through in harmony.*

Quiet ambient runner for [`bog-agents`](https://github.com/bogware/bog-agents).
Schedules. File watches. Inbound webhooks. Git pushes. Sits in the
background, fires the agent when something happens, writes the result
wherever you point it.

No terminal needed. No hand-holding. It keeps watch through the night and
goes the distance.

[![PyPI](https://img.shields.io/pypi/v/bog-agents-daemon)](https://pypi.org/project/bog-agents-daemon/)
[![Python](https://img.shields.io/pypi/pyversions/bog-agents-daemon)](https://pypi.org/project/bog-agents-daemon/)
[![License](https://img.shields.io/pypi/l/bog-agents-daemon)](https://opensource.org/licenses/MIT)

---

## Why a daemon

The CLI is great when you're at the keyboard. The daemon is what you reach
for when you want an agent that watches *for* you — and reports back when
something matters.

- **Five trigger types**: `cron`, `interval`, `file_change`, `webhook`,
  `git_push`.
- **Seven output targets**: `log`, `stdout`, `file`, `slack`, `webhook`,
  `email`, `github_comment`.
- **Auth + integrity**: token-authenticated REST API; HMAC-validated
  inbound webhooks; tokens stored with `0o600` permissions.
- **Durable**: `os.fsync()`-durable job persistence so a hard kill never
  loses a freshly-created job.
- **Cross-platform**: systemd (Linux) / launchd (macOS) service install via
  `bog-agents daemon install`, or just `bog-agents-daemon run` in a shell on
  any platform (Windows included). Same config either way.

If the CLI *passes through in harmony*, the daemon is what keeps watch
through the night.

---

## Install

```bash
pip install bog-agents-daemon
```

Pulls in [`bog-agents`](https://pypi.org/project/bog-agents/) automatically.
Add provider extras you need:

```bash
pip install "bog-agents-daemon[anthropic]"
pip install "bog-agents-daemon[openai]"
pip install "bog-agents-daemon[bedrock]"
```

---

## 30-second tour

Start the daemon (foreground for a quick test):

```bash
bog-agents-daemon run --port 7878
```

Add a job that runs every weekday morning:

```bash
bog-agents daemon jobs create \
  --name morning-brief \
  --cron "0 9 * * 1-5" \
  --prompt "Summarize what changed in this repo since yesterday." \
  --output slack --output-slack "$SLACK_WEBHOOK_URL"
```

The job persists to `~/.bog-agents/daemon/jobs.json`. The scheduler picks it
up on the next tick and fires it on the configured cadence.

---

## Triggers

```yaml
# A cron job
triggers:
  - type: cron
    cron: "0 9 * * 1-5"      # 9am Mon–Fri

# An interval
triggers:
  - type: interval
    interval_seconds: 1800   # every 30 min

# A file watcher
triggers:
  - type: file_change
    watch_dir: src
    watch_patterns: ["**/*.py"]
    debounce_seconds: 5

# An inbound webhook (HMAC-validated when a secret is set)
triggers:
  - type: webhook
    webhook_path: /hooks/incident
    webhook_secret: "<shared secret for X-Hub-Signature-256>"

# A git push (fired by the post-receive hook that
# `bog-agents daemon install-git-hook <repo>` installs)
triggers:
  - type: git_push
    git_branch_pattern: main
```

A job can have multiple triggers. The agent fires on any of them.

---

## Outputs

Where the result of an agent run goes. Configure one or many:

```yaml
outputs:
  - target: log            # systemd journal / stderr
  - target: file
    file_path: ~/.bog-agents/runs/morning-brief.md
    append: true
  - target: slack
    slack_webhook_url: https://hooks.slack.com/services/T000/B000/XXXX
    slack_channel: "#engineering"   # optional override
  - target: webhook
    webhook_url: https://hooks.example.com/agent-output
    webhook_headers: { X-Source: bog-agents }   # optional extra headers
  - target: email
    to_addrs: [oncall@example.com]
    from_addr: bog-agents@example.com
    smtp_host: smtp.example.com
    smtp_port: 587                  # 587=STARTTLS, 465=SSL, 25=plain
    smtp_username: bog
    smtp_password: "<password>"     # persisted to jobs.json (owner-only)
  - target: github_comment
    github_repo: example/api
    github_issue_or_pr: 1234
    github_token: "<token>"
```

---

## REST API

Once the daemon is running, you've got a small authenticated REST API for
managing jobs:

| Endpoint | Method | What |
|---|---|---|
| `/jobs` | GET | List all jobs |
| `/jobs` | POST | Create a job |
| `/jobs/{id}` | GET | Job detail |
| `/jobs/{id}` | DELETE | Delete a job |
| `/jobs/{id}/runs` | GET | Run history |
| `/jobs/{id}/run` | POST | Fire the job manually |
| `/health` | GET | Liveness probe |

Every endpoint requires an `X-Daemon-Token: <daemon_token>` header. The token
is generated on first start, stored at `~/.bog-agents/daemon/token`
with `0o600` permissions, and printed once to the foreground log so you
can copy it.

---

## Running as a service

The `bog-agents-daemon` binary itself only knows `start` (alias `run`),
`stop`, and `status`. The service installer ships with the
[`bog-agents-cli`](https://pypi.org/project/bog-agents-cli/) package
(`pip install bog-agents-cli`):

```bash
bog-agents daemon install                     # auto-detects systemd (Linux) / launchd (macOS)
bog-agents daemon install --platform systemd  # or force a specific init system
```

### systemd (Linux)

`bog-agents daemon install` writes a user unit to
`~/.config/systemd/user/bog-agents-daemon.service` and prints the follow-up
commands:

```bash
systemctl --user daemon-reload
systemctl --user enable bog-agents-daemon
systemctl --user start bog-agents-daemon
systemctl --user status bog-agents-daemon
```

### macOS launchd

`bog-agents daemon install --platform launchd` writes
`~/Library/LaunchAgents/com.bogware.bog-agents-daemon.plist` and prints the
load command:

```bash
launchctl load ~/Library/LaunchAgents/com.bogware.bog-agents-daemon.plist
launchctl list com.bogware.bog-agents-daemon
```

### Windows

On Windows, `bog-agents daemon install` registers a Task Scheduler task (`BogAgentsDaemon`) that starts the daemon at logon; remove it with `schtasks /Delete /TN BogAgentsDaemon /F`.
(`bog-agents-daemon run`) or launch it in the background from the CLI
(`bog-agents daemon start`). If you want it to start at logon, point a Task
Scheduler task at the `bog-agents-daemon` executable yourself.

---

## Security model

- **Token-authenticated API.** Every request needs an `X-Daemon-Token` header.
  Tokens generated with `secrets.token_urlsafe`, compared with
  `hmac.compare_digest`, stored at `0o600`.
- **HMAC-validated inbound webhooks.** A webhook trigger configured with a
  `webhook_secret` requires a valid `X-Hub-Signature-256` header
  (HMAC-SHA256 of the raw body) on every request.
- **Secrets stored owner-only.** Provider API keys are read from env vars
  and never persisted. Secrets that live inside job configs (SMTP
  passwords, GitHub tokens, webhook HMAC secrets) are persisted to
  `~/.bog-agents/daemon/jobs.json`, which — like the API token — is
  restricted to owner-only permissions (POSIX `0o600` / Windows ACL).
- **Resource limits.** Configurable per-job CPU / memory / wall-clock
  caps via the same FeatureConfig shape as the SDK.

---

## What's new in 0.9.x

The daemon rides the synchronized monorepo version, so it inherits every
SDK hardening as it lands.

- **0.9.4** — deepagents parity and provider resilience flow up from the
  SDK: Anthropic, AWS Bedrock, and OpenAI are live-tested, so scheduled
  jobs survive a provider's bad night.
- **0.9.1** — **Bedrock, seamless.** Automatic inference-profile
  resolution and auto SSO-credential refresh mean a long-lived daemon
  keeps firing Bedrock jobs without a stale-credential outage.
- **0.9.0** — repo-wide security sweep; compliance auditing groundwork.
- **Carried forward** — `JobRun.dispatch_errors` per-target capture
  (capped at 20 entries with an `(overflow)` summary) so a wide-fanout
  outage shows up in the runs table without blowing up the record JSON;
  patient-by-default execution (provider retries, `stdin=/dev/null` for
  interactive commands, `virtual_mode=True` filesystem confinement,
  structured event logs ready for log shippers).

---

## When to use this vs. `/peat` in the CLI

| | Daemon | `/peat` |
|---|---|---|
| Survives reboot | ✓ | ✗ (only while CLI open) |
| Fires while you're asleep | ✓ | ✗ |
| Webhook / git-push triggers | ✓ | ✗ |
| Slack / email / GitHub-comment outputs | ✓ | ✗ |
| Reuses your interactive agent | ✗ | ✓ |
| Zero ops (no service install) | ✗ | ✓ |

`/peat` is the right tool when you're at the keyboard and want a personal
assistant for the duration of the session. The daemon is the right tool
when you want an agent that wakes itself.

---

## Documentation

- **Full docs**: <https://github.com/bogware/bog-agents/tree/main/docs>
  — [daemon quickstart](https://github.com/bogware/bog-agents/blob/main/docs/daemon/quickstart.md),
  [security model](https://github.com/bogware/bog-agents/blob/main/docs/security.md),
  [troubleshooting](https://github.com/bogware/bog-agents/blob/main/docs/troubleshooting.md)
- Repo: <https://github.com/bogware/bog-agents>
- Issues: <https://github.com/bogware/bog-agents/issues>
- Changelog: [`CHANGELOG.md`](https://github.com/bogware/bog-agents/blob/main/CHANGELOG.md)

---

## License

MIT. See [LICENSE](https://github.com/bogware/bog-agents/blob/main/LICENSE).

*Pass through in harmony.*
