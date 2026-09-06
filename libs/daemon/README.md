# Bog Agents Daemon

> *The patient watcher. Wakes itself. Pass through in harmony.*

Quiet ambient runner for [`bog-agents`](https://github.com/bogware/bog-agents).
Schedules. File watches. Inbound webhooks. Git pushes. Scheduled repository
scans. Sits in the background, fires the agent when something happens, writes the
result wherever you point it.

No terminal needed. No hand-holding. It keeps watch through the night and goes
the distance.

[![PyPI](https://img.shields.io/pypi/v/bog-agents-daemon)](https://pypi.org/project/bog-agents-daemon/)
[![Python](https://img.shields.io/pypi/pyversions/bog-agents-daemon)](https://pypi.org/project/bog-agents-daemon/)
[![License](https://img.shields.io/pypi/l/bog-agents-daemon)](https://opensource.org/licenses/MIT)

---

## Why a daemon

The CLI is great when you're at the keyboard. The daemon is what you reach for
when you want an agent that watches *for* you — and reports back when something
matters.

- **Five trigger types**: `cron`, `interval`, `file_change`, `webhook`, `git_push`.
- **Seven output targets**: `log`, `stdout`, `file`, `slack`, `webhook`, `email`,
  `github_comment`.
- **Scan jobs**: a scheduled security / code-health / perf sweep whose findings
  land in a durable ledger your CI can gate on.
- **Bounded spend**: per-run budgets and a per-job daily ceiling. A run that hits
  its budget **pauses** (resumable) instead of burning through the cap.
- **Thread-linked jobs**: a job can continue an interactive CLI thread, so goal
  state and memory survive the hand-off from your keyboard to the schedule.
- **Auth + integrity**: token-authenticated REST API; HMAC-validated inbound
  webhooks; secrets stored owner-only.
- **Durable + drainable**: `os.fsync()`-durable persistence; orphaned runs
  reconciled on restart; a graceful `drain` before stop so no run is lost.
- **Cross-platform**: systemd (Linux) / launchd (macOS) / Task Scheduler
  (Windows) service install.

If the CLI *passes through in harmony*, the daemon is what keeps watch through
the night.

---

## Install

```bash
pip install bog-agents-daemon
```

Pulls in [`bog-agents`](https://pypi.org/project/bog-agents/) automatically. Add
provider extras you need:

```bash
pip install "bog-agents-daemon[anthropic]"   # or [openai], [bedrock], ...
```

---

## 30-second tour

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

The job persists to `~/.bog-agents/daemon/jobs.json`. The scheduler picks it up
on the next tick and fires it on the configured cadence. Cron uses missed-slot
catch-up: if the daemon was down across a scheduled slot, the job fires **once**
on restart, not N times.

---

## Scan jobs → a findings ledger you can gate CI on

Make a job a scan and its findings land in a durable, fingerprinted ledger beside
the scanned repo (`<working_dir>/.bog-agents/findings.db`). A re-scan updates
findings instead of duplicating them; a fixed issue closes itself; `--scan-gate`
marks the run red when anything at or above a severity is still open.

```bash
bog-agents daemon jobs create \
  --name nightly-security \
  --cron "0 2 * * *" \
  --working-dir /srv/app \
  --scan security \
  --scan-gate high \
  --output github_comment --output-github-repo example/app --output-github-issue 1
```

Read the ledger over the API (or with the CLI's `/findings` in that repo):

| Endpoint | What |
|---|---|
| `GET /findings?job_id=…` | Ledger rows, worst first |
| `GET /findings/gate?job_id=…&max_severity=high` | The CI yes/no (`passed`) |
| `GET /findings/sarif?job_id=…` | SARIF 2.1.0 for code-scanning uploads |
| `POST /findings/{fingerprint}/triage` | Set `triaged` / `fixed` / `wontfix` / `false_positive` |

Scan profiles: `security`, `cleanup`, `perf`, or `custom` (your `--prompt` is the
rubric). The same store powers the CLI's packaged `security-scan` recipe.

---

## Triggers

```yaml
triggers:
  - type: cron
    cron: "0 9 * * 1-5"          # 9am Mon–Fri
  - type: interval
    interval_seconds: 1800       # every 30 min
  - type: file_change
    watch_dir: src
    watch_patterns: ["**/*.py"]
    debounce_seconds: 5
  - type: webhook
    webhook_path: /hooks/incident
    webhook_secret: "<shared secret for X-Hub-Signature-256>"
  - type: git_push
    git_branch_pattern: main
```

A job can have multiple triggers and fires on any of them. `POST /webhooks/github`
turns an assigned issue, applied label, review comment, or red CI run into a job
(HMAC-verified, fail-closed).

---

## Outputs

```yaml
outputs:
  - target: log
  - target: file
    file_path: ~/.bog-agents/runs/morning-brief.md
    append: true
  - target: slack
    slack_webhook_url: https://hooks.slack.com/services/T000/B000/XXXX
    slack_channel: "#engineering"
  - target: webhook
    webhook_url: https://hooks.example.com/agent-output
  - target: email
    to_addrs: [oncall@example.com]
    from_addr: bog-agents@example.com
    smtp_host: smtp.example.com
    smtp_port: 587
  - target: github_comment
    github_repo: example/api
    github_issue_or_pr: 1234
    github_token: "<token>"
```

Network dispatch failures are captured on the run (`run.dispatch_errors`) so a
silent Slack/webhook outage shows up in the runs table.

---

## Cost, continuity, and draining

- **Per-run budget** (`--budget-usd`) pauses a run at the cap (`status=paused`);
  `POST /runs/{id}/resume` with a higher budget continues it. A **daily ceiling**
  (`--daily-ceiling-usd`) skips new runs once today's spend is reached.
- **Thread-linked jobs** (`--thread <id>`) reopen the CLI's checkpointer so the
  job continues an interactive thread instead of starting fresh; `--max-runs`
  caps attempts.
- **Draining**: `POST /drain` (also SIGTERM and `/shutdown`) refuses new
  dispatches and lets in-flight runs finish; `/health` reports `running` /
  `draining`; `bog-agents daemon drain` and `daemon upgrade` poll it so a restart
  never kills a live run.
- **Usage export**: `GET /usage` aggregates spend per job / model from the durable
  ledger; `POST /usage/export` (and `bog-agents daemon usage-export`) write CSV
  and/or post OTLP metrics.

---

## REST API

| Endpoint | Method | What |
|---|---|---|
| `/jobs` | GET / POST | List / create jobs |
| `/jobs/{id}` | GET / PATCH / DELETE | Detail / edit / delete |
| `/jobs/{id}/runs` | GET | Run history |
| `/jobs/{id}/run` | POST | Fire manually |
| `/runs/{id}/resume` | POST | Resume a budget-paused run |
| `/findings`, `/findings/gate`, `/findings/sarif` | GET | Scan-job ledger |
| `/usage`, `/usage/export` | GET / POST | Spend aggregates |
| `/drain`, `/health` | POST / GET | Graceful drain; liveness + drain state |

Every endpoint requires an `X-Daemon-Token: <token>` header. The token is
generated on first start, stored at `~/.bog-agents/daemon/token` (`0o600`), and
printed once to the foreground log.

---

## Running as a service

The service installer ships with the
[`bog-agents-cli`](https://pypi.org/project/bog-agents-cli/) package:

```bash
bog-agents daemon install                     # auto-detects systemd / launchd / Task Scheduler
bog-agents daemon install --platform systemd  # or force one
```

- **systemd (Linux)** — writes `~/.config/systemd/user/bog-agents-daemon.service`.
- **launchd (macOS)** — writes `~/Library/LaunchAgents/com.bogware.bog-agents-daemon.plist`.
- **Windows** — registers a Task Scheduler task (`BogAgentsDaemon`) that starts at
  logon; remove with `schtasks /Delete /TN BogAgentsDaemon /F`.

---

## Security model

- **Token-authenticated API.** `secrets.token_urlsafe`, compared with
  `hmac.compare_digest`, stored `0o600`.
- **HMAC-validated inbound webhooks.** A webhook trigger with a `webhook_secret`
  requires a valid `X-Hub-Signature-256` (HMAC-SHA256 of the raw body).
- **Secrets stored owner-only.** Provider keys are read from env and never
  persisted; secrets inside job configs (SMTP passwords, tokens, webhook secrets)
  are owner-only in `jobs.json` (POSIX `0o600` / Windows ACL).
- **Corrupt `jobs.json` is quarantined, never overwritten** — unparseable content
  is renamed aside before the next save.

---

## When to use this vs. `/peat` in the CLI

| | Daemon | `/peat` |
|---|---|---|
| Survives reboot | ✓ | ✗ |
| Fires while you're asleep | ✓ | ✗ |
| Webhook / git-push / scan triggers | ✓ | ✗ |
| Slack / email / GitHub-comment outputs | ✓ | ✗ |
| Reuses your interactive agent | ✗ | ✓ |
| Zero ops (no service install) | ✗ | ✓ |

---

## Documentation

- **Full docs**: <https://github.com/bogware/bog-agents/tree/main/docs>
  — [daemon quickstart](https://github.com/bogware/bog-agents/blob/main/docs/daemon/quickstart.md),
  [findings & security](https://github.com/bogware/bog-agents/blob/main/docs/cli/findings.md),
  [security model](https://github.com/bogware/bog-agents/blob/main/docs/security.md)
- Repo: <https://github.com/bogware/bog-agents> · Issues:
  <https://github.com/bogware/bog-agents/issues> ·
  Changelog: [`CHANGELOG.md`](https://github.com/bogware/bog-agents/blob/main/CHANGELOG.md)

---

## License

MIT. See [LICENSE](https://github.com/bogware/bog-agents/blob/main/LICENSE).

*Pass through in harmony.*
