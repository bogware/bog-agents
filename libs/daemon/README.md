# Bog Agents Daemon

> *The patient watcher. Wakes itself. Pass through in harmony.*

Quiet ambient runner for [`bog-agents`](https://github.com/bogware/bog-agents).
Schedules. File watches. Inbound webhooks. Git pushes. Sits in the
background, fires the agent when something happens, writes the result
wherever you point it.

No terminal needed. No hand-holding. Goes the distance.

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
- **Cross-platform**: POSIX systemd, Windows Task Scheduler, or just
  `bog-agents-daemon run` in a shell. Same config either way.

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
bog-agents-daemon job add \
  --name morning-brief \
  --cron "0 9 * * 1-5" \
  --prompt "Summarize what changed in this repo since yesterday." \
  --output slack:#engineering
```

The job persists to `~/.bog-agents/daemon/jobs/`. The scheduler picks it up
on the next tick and fires it on the configured cadence.

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
    seconds: 1800            # every 30 min

# A file watcher
triggers:
  - type: file_change
    patterns: ["src/**/*.py"]
    debounce_seconds: 5

# An inbound webhook (HMAC-validated)
triggers:
  - type: webhook
    path: /hooks/incident
    secret_env: WEBHOOK_SECRET

# A git push (post-receive on a remote)
triggers:
  - type: git_push
    repo: /srv/git/main.git
    branch: main
```

A job can have multiple triggers. The agent fires on any of them.

---

## Outputs

Where the result of an agent run goes. Configure one or many:

```yaml
outputs:
  - type: log              # systemd journal / stderr
  - type: file
    path: ~/.bog-agents/runs/morning-brief.md
  - type: slack
    channel: "#engineering"
    token_env: SLACK_BOT_TOKEN
  - type: webhook
    url: https://hooks.example.com/agent-output
    hmac_secret_env: OUTBOUND_WEBHOOK_SECRET
  - type: email
    to: oncall@example.com
    from: bog-agents@example.com
    smtp_env_prefix: SMTP_     # SMTP_HOST, SMTP_USER, SMTP_PASSWORD
  - type: github_comment
    repo: example/api
    issue: 1234
    token_env: GITHUB_TOKEN
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
| `/metrics` | GET | Counter snapshot |

Every endpoint requires `Authorization: Bearer <daemon_token>`. The token
is generated on first start, stored at `~/.bog-agents/daemon/.token`
with `0o600` permissions, and printed once to the foreground log so you
can copy it.

---

## Running as a service

### systemd (Linux)

```bash
bog-agents-daemon install-systemd --user
systemctl --user enable --now bog-agents-daemon
systemctl --user status bog-agents-daemon
```

### Windows Task Scheduler

```powershell
bog-agents-daemon install-windows-task
```

Creates a `bog-agents-daemon` task that starts at logon and restarts on
failure. Manage it with `schtasks` or the Task Scheduler GUI.

### macOS launchd

```bash
bog-agents-daemon install-launchd --user
launchctl load ~/Library/LaunchAgents/com.bogware.bog-agents-daemon.plist
```

---

## Security model

- **Token-authenticated API.** Every request needs `Authorization: Bearer`.
  Tokens generated with `secrets.token_urlsafe`, compared with
  `hmac.compare_digest`, stored at `0o600`.
- **HMAC-validated inbound webhooks.** Each webhook trigger declares an
  env-var holding its shared secret; the daemon verifies the HMAC-SHA256
  signature on every request.
- **Outbound webhook signing.** Outputs that POST to a webhook can sign
  the body with HMAC-SHA256 so the receiver can verify it came from
  this daemon.
- **No secrets on disk.** Provider keys, Slack tokens, and webhook
  secrets are all read from env vars — the daemon never persists them.
- **Resource limits.** Configurable per-job CPU / memory / wall-clock
  caps via the same FeatureConfig shape as the SDK.

---

## What's new since 0.8.0

- **0.8.8 (Wave Y)** — `JobRun.dispatch_errors` capped at 20 entries
  with an `(overflow)` summary so a wide-fanout outage (e.g. 100
  Slack recipients hit during an API outage) doesn't blow up the
  run record JSON.
- **0.8.7 (Wave X)** — Per-target output dispatch failures (email,
  webhook, Slack) are captured on `JobRun.dispatch_errors` so
  operators can tell from the runs table that delivery failed even
  when the agent itself completed.
- **0.8.0** — Patient by default (provider retries, `stdin=/dev/null`
  for interactive commands, `virtual_mode=True` filesystem confinement,
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
